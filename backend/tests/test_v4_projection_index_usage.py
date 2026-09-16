"""O5：投影/来源卡/刮削的查询形状必须与索引形状匹配。

只断言"索引存在"是不够的——索引存在但优化器不用，等于没加。这里用
`EXPLAIN QUERY PLAN` 直接锁定三条真实查询的访问路径：

- `revision_bindings` 必须按 `work_id` 定位（原先按 revision_id 驱动，于是投影的每个
  相关子查询都要遍历当前所有 confirmed revision 的全部绑定行，O(作品数 × 绑定数)）；
- `jobs` 必须按 `revision_id` 定位（原先 SCAN jobs：来源卡每 1.5 秒一次全表扫 + 排序）；
- `artifacts` 必须按 `(revision_id, work_id, artifact_type)` 定位（原先自动索引只覆盖
  `(revision_id, artifact_type)`，work_id 只是后置过滤）。
"""

from __future__ import annotations


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "index-usage.db")
    database.initialize()
    return database


def _plan(conn, sql: str) -> str:
    return " | ".join(str(row["detail"]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}"))


def test_binding_count_subquery_searches_by_work_id(tmp_path):
    database = _database(tmp_path)
    with database.connect() as conn:
        plan = _plan(conn, """
            SELECT COUNT(DISTINCT rb.episode_id)
            FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
            WHERE rb.work_id = 'w-1' AND rb.episode_id IS NOT NULL AND ir.status = 'confirmed'
        """)

    assert "idx_v4_bindings_work" in plan, plan


def test_source_card_job_lookup_searches_by_revision_id(tmp_path):
    database = _database(tmp_path)
    with database.connect() as conn:
        plan = _plan(
            conn,
            "SELECT work_id, job_id FROM jobs WHERE revision_id IN ('r1','r2')",
        )

    assert "idx_v4_jobs_revision" in plan, plan
    assert "SCAN jobs" not in plan, plan


def test_artifact_completeness_lookup_uses_work_scoped_index(tmp_path):
    database = _database(tmp_path)
    with database.connect() as conn:
        plan = _plan(conn, """
            SELECT artifact_id FROM artifacts
            WHERE revision_id = 'r1' AND work_id = 'w1'
              AND artifact_type = 'episode_nfo' AND status = 'published'
        """)

    assert "idx_v4_artifacts_work" in plan, plan


def test_fresh_database_creates_the_query_indices(tmp_path):
    """空库创建路径也必须建出这三个索引（否则新装库悄悄缺索引）。"""

    database = _database(tmp_path)
    with database.connect() as conn:
        names = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }

    assert {"idx_v4_bindings_work", "idx_v4_artifacts_work", "idx_v4_jobs_revision"} <= names
