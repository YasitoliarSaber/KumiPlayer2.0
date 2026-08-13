"""Preflight 0 回归：每个测试必须拥有独立 SQLite 状态。

规划员指令：修 pytest SQLite 每测试隔离后，必须加一个回归——
test A 插入一行数据，test B 查询时必须看不到 test A 的数据。

两个用例故意按文件顺序摆放：若隔离失效（共享集合级 DB），后一个用例
会看到前一用例写入的行而失败，从而暴露顺序敏感回归。
"""


def test_seed_app_meta_row():
    """在 app_meta 写入隔离探针行，并自证写入成功。"""
    from app.db.database import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('isolation_probe', 'seeded')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'isolation_probe'"
    ).fetchone()
    assert row is not None and row["value"] == "seeded"


def test_other_test_must_not_see_seeded_row():
    """后一个测试必须看不到前一个测试写入的行（独立 SQLite）。"""
    from app.db.database import get_connection

    row = get_connection().execute(
        "SELECT value FROM app_meta WHERE key = 'isolation_probe'"
    ).fetchone()
    assert row is None, "测试之间共享了 SQLite 状态（隔离失效）：每个测试应有独立数据库"
