"""provider 身份抢占：并发刮削不得因唯一约束把 job 打失败。

provider_bindings 有 ``PRIMARY KEY(provider, media_type, provider_id)`` 与
``UNIQUE(work_id, provider, media_type)`` 两个约束。原实现只声明了后者的
ON CONFLICT，于是两个 work 并发抢同一 Provider 身份时，后提交者撞主键 →
IntegrityError → 整个 scrape_work 失败（实测线上连续出现 8 次）。
"""

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


def test_active_owner_still_blocks_and_stale_owner_is_taken_over(tmp_path):
    """活动占用者必须继续拦住；**陈旧占用者**（被取代导入的残留绑定）必须让位。

    跨批次回归：重新导入时若上一批（已被取代）的作品仍占着同一个在线身份，
    新批次会被判成"在线作品已关联到另一部作品"、反复退化为人工处理。
    """

    database = _database(tmp_path)
    with database.connect() as conn:
        _seed_work(conn, "work-a")
        _seed_work(conn, "work-b")
        _seed_work(conn, "work-c")
        # work-a 活动并占用身份；work-b 是"陈旧占用者"（没有任何活动 revision 绑定）
        _make_active(conn, "work-a", revision_id="rev-a", root_id="root-a")
        _make_active(conn, "work-b", revision_id="rev-b", root_id="root-b")
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES ('work-b', 'tmdb', 'tv', '12345')"
        )
        conn.execute("UPDATE import_revisions SET status = 'superseded' WHERE revision_id = 'rev-b'")

        # 活动占用者：拒绝
        assert (
            _claim_provider_binding(
                conn, work_id="work-a", provider="tmdb", media_type="tv", provider_id="12345"
            )
            is True
        ), "活动作品之间仍按先到先得"
        _claim_provider_binding(
            conn, work_id="work-c", provider="tmdb", media_type="tv", provider_id="12345"
        )
        owner = conn.execute(
            "SELECT work_id FROM provider_bindings WHERE provider = 'tmdb' "
            "AND media_type = 'tv' AND provider_id = '12345'"
        ).fetchone()
        assert str(owner["work_id"]) == "work-a", "活动占用者的身份不得被抢占"

        # 陈旧占用者：允许接管（把身份归还给活动 work）
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES ('work-b', 'tmdb', 'tv', '54321')"
        )
        assert _claim_provider_binding(
            conn, work_id="work-c", provider="tmdb", media_type="tv", provider_id="54321"
        ), "陈旧占用者必须让位，否则跨批次导入会一直被挡成人工处理"
        new_owner = conn.execute(
            "SELECT work_id FROM provider_bindings WHERE provider = 'tmdb' "
            "AND media_type = 'tv' AND provider_id = '54321'"
        ).fetchone()
        assert str(new_owner["work_id"]) == "work-c"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM provider_bindings WHERE work_id = 'work-b' AND provider_id = '54321'"
            ).fetchone()[0]
            == 0
        ), "陈旧绑定必须被释放"


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
