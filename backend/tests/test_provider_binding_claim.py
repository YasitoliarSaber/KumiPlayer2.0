"""provider 绑定：共享在线资料不合并本地作品，也不覆盖本作品已有身份。"""

from __future__ import annotations

from app.media_v4.jobs.scrape import _claim_provider_binding
from app.media_v4.persistence.database import V4Database


def _database(tmp_path) -> V4Database:
    database = V4Database(tmp_path / "binding-claim.db")
    database.initialize()
    return database


def _seed_work(conn, work_id: str, *, work_type: str = "series") -> None:
    conn.execute(
        "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'now', 'now')",
        (work_id, f"title:{work_id}", work_type, work_id),
    )


def _make_active(conn, work_id: str, *, revision_id: str, root_id: str) -> None:
    """把 work 挂到"活动来源的已确认 revision"上，使其成为活动作品。"""

    conn.execute(
        "INSERT OR IGNORE INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
        "VALUES (?, 'local', 'local_scan', 'now', 'now')",
        (root_id,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
        "VALUES (?, ?, 1, 'completed', 'ready', 'now', 'now')",
        (f"scan-{revision_id}", root_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at) "
        "VALUES (?, ?, ?, 'fixture', 'confirmed', 'now')",
        (revision_id, root_id, f"scan-{revision_id}"),
    )
    evidence_id = f"ev-{work_id}"
    conn.execute(
        "INSERT INTO source_evidence(evidence_id, scan_id, root_id, source_key, relative_path, entry_kind, "
        "provider, source_locator, fingerprint) VALUES (?, ?, ?, ?, ?, 'video', 'local', ?, ?)",
        (evidence_id, f"scan-{revision_id}", root_id, evidence_id, f"{evidence_id}.mkv", evidence_id, evidence_id),
    )
    conn.execute(
        "INSERT INTO revision_bindings(binding_id, revision_id, evidence_id, work_id) VALUES (?, ?, ?, ?)",
        (f"bind-{work_id}", revision_id, evidence_id, work_id),
    )
    # 显式设定：不要依赖列默认值，否则断言会受 schema 默认影响。
    conn.execute("UPDATE source_roots SET retired_at = '' WHERE root_id = ?", (root_id,))
    conn.execute("UPDATE works SET status = 'active' WHERE work_id = ?", (work_id,))


def test_shared_identity_is_normal_but_a_work_cannot_overwrite_its_own_slot(tmp_path):
    """不同 Work 可共享在线 ID；自动结果不能改写自身已有不同 ID。"""

    database = _database(tmp_path)
    with database.connect() as conn:
        _seed_work(conn, "work-a")
        _seed_work(conn, "work-b")
        assert _claim_provider_binding(
            conn, work_id="work-a", provider="tmdb", media_type="tv", provider_id="12345"
        )
        assert _claim_provider_binding(
            conn, work_id="work-b", provider="tmdb", media_type="tv", provider_id="12345"
        )
        assert not _claim_provider_binding(
            conn, work_id="work-b", provider="tmdb", media_type="tv", provider_id="54321"
        )
        assert [tuple(row) for row in conn.execute(
            "SELECT work_id, provider_id FROM provider_bindings ORDER BY work_id"
        )] == [("work-a", "12345"), ("work-b", "12345")]


def test_work_holds_live_slot_covers_the_three_stale_shapes(tmp_path):
    """“是否还占着媒体库位置”的唯一定义：三种陈旧形态都必须判为不占位。

    这是跨批次串接的地基——只有仍留在媒体库里的作品才算占着在线身份。
    """

    from app.media_v4.persistence.identity_lifecycle import work_holds_live_slot

    database = _database(tmp_path)
    with database.connect() as conn:
        # 每种形态用独立作品与来源：revision 状态的转换是**单向**的
        # （触发器禁止 superseded → confirmed），所以不能"改了再还原"。
        for index, work_id in enumerate(("live", "superseded-import", "retired-root", "retired-work"), start=1):
            _seed_work(conn, f"work-{work_id}")
            _make_active(
                conn, f"work-{work_id}", revision_id=f"rev-{index}", root_id=f"root-{index}"
            )

        # ① 正常活动
        assert work_holds_live_slot(conn, "work-live") is True

        # ② 导入被取代（按来源删除后重导的情形）
        conn.execute("UPDATE import_revisions SET status = 'superseded' WHERE revision_id = 'rev-2'")
        assert work_holds_live_slot(conn, "work-superseded-import") is False

        # ③ 来源退役
        conn.execute("UPDATE source_roots SET retired_at = 'now' WHERE root_id = 'root-3'")
        assert work_holds_live_slot(conn, "work-retired-root") is False

        # ④ 作品本身退出媒体库
        conn.execute("UPDATE works SET status = 'superseded' WHERE work_id = 'work-retired-work'")
        assert work_holds_live_slot(conn, "work-retired-work") is False


def test_retired_source_cleanup_does_not_use_shared_provider_identity(tmp_path):
    """共享在线 ID 不是同一作品证据，不能据此清除另一个来源的续接身份。"""

    from app.media_v4.persistence.identity_lifecycle import release_retired_source_identities

    database = _database(tmp_path)
    with database.connect() as conn:
        _seed_work(conn, "retired-shared")
        _make_active(
            conn,
            "retired-shared",
            revision_id="rev-retired-shared",
            root_id="root-retired-shared",
        )
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES ('retired-shared', 'tmdb', 'tv', '12345')"
        )
        conn.execute(
            "UPDATE source_roots SET retired_at = 'now' WHERE root_id = 'root-retired-shared'"
        )

        release_retired_source_identities(
            conn,
            "another-root",
            "later",
            work_keys={"series:another:tv"},
            titles={"another"},
        )

        work = conn.execute(
            "SELECT status, identity_key FROM works WHERE work_id = 'retired-shared'"
        ).fetchone()
        binding = conn.execute(
            "SELECT provider_id FROM provider_bindings WHERE work_id = 'retired-shared'"
        ).fetchone()
    assert tuple(work) == ("active", "title:retired-shared")
    assert str(binding["provider_id"]) == "12345"


def test_claiming_a_free_identity_succeeds_and_is_idempotent(tmp_path):
    database = _database(tmp_path)
    with database.connect() as conn:
        _seed_work(conn, "work-a")

        assert _claim_provider_binding(
            conn, work_id="work-a", provider="tmdb", media_type="tv", provider_id="777"
        )
        # 同一 work 再次声明同一身份：走 UNIQUE(work_id, provider, media_type) 的 upsert 分支
        assert _claim_provider_binding(
            conn, work_id="work-a", provider="tmdb", media_type="tv", provider_id="777"
        )

        rows = conn.execute("SELECT provider_id FROM provider_bindings").fetchall()
        assert [str(row["provider_id"]) for row in rows] == ["777"]


def test_movie_work_media_type_is_derived_from_work_type(tmp_path):
    """media_type 由 works.work_type 推导（series→tv，其余→movie）。"""

    database = _database(tmp_path)
    with database.connect() as conn:
        _seed_work(conn, "work-film", work_type="movie")

        assert _claim_provider_binding(
            conn, work_id="work-film", provider="tmdb", media_type="movie", provider_id="999"
        )

        row = conn.execute(
            "SELECT media_type FROM provider_bindings WHERE provider_id = '999'"
        ).fetchone()
        assert str(row["media_type"]) == "movie"
