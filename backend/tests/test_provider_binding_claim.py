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


def test_claiming_an_identity_owned_by_another_work_returns_false(tmp_path):
    database = _database(tmp_path)
    with database.connect() as conn:
        _seed_work(conn, "work-a")
        _seed_work(conn, "work-b")
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES ('work-a', 'tmdb', 'tv', '12345')"
        )

        claimed = _claim_provider_binding(
            conn, work_id="work-b", provider="tmdb", media_type="tv", provider_id="12345"
        )

        assert claimed is False, "身份已被他人占用时必须返回 False，而不是抛 IntegrityError"
        owner = conn.execute(
            "SELECT work_id FROM provider_bindings WHERE provider = 'tmdb' "
            "AND media_type = 'tv' AND provider_id = '12345'"
        ).fetchone()
        assert str(owner["work_id"]) == "work-a", "已占用的身份不得被覆盖"


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
