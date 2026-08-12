"""catalog.closure 唯一收口判断（模块2 阶段B）验收。

is_boundary_complete 正向定义：boundary 下所有当前有效的 source_directories
checkpoint 必须全部 complete；任一 queued/scanning/failed 都阻塞；无相关
checkpoint 也为 False；boundary 之外的目录不影响判断。
"""


import pytest

from app.catalog import closure, store
from app.db.database import close_connection, get_connection, init_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "closure.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _insert(remote_path: str, state: str, root_id: str = "root-x") -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO source_directories (
            root_id, remote_path, parent_path, depth, state
        ) VALUES (?, ?, '', 0, ?)
        """,
        (root_id, remote_path, state),
    )
    conn.commit()


class TestIsBoundaryComplete:
    def test_all_complete_is_true(self):
        """boundary 自身与全部子目录 complete → True。"""
        for path in ("/动画/作品", "/动画/作品/Season 1", "/动画/作品/Season 2"):
            _insert(path, "complete")
        assert closure.is_boundary_complete("root-x", "/动画/作品") is True

    def test_boundary_itself_not_complete_is_false(self):
        """boundary 自身 queued → False（boundary 必须确认过才收口）。"""
        _insert("/动画/作品", "queued")
        _insert("/动画/作品/Season 1", "complete")
        assert closure.is_boundary_complete("root-x", "/动画/作品") is False

    def test_queued_blocks(self):
        _insert("/动画/作品", "complete")
        _insert("/动画/作品/Season 1", "complete")
        _insert("/动画/作品/Season 2", "queued")
        assert closure.is_boundary_complete("root-x", "/动画/作品") is False

    def test_scanning_blocks(self):
        _insert("/动画/作品", "complete")
        _insert("/动画/作品/Season 1", "scanning")
        assert closure.is_boundary_complete("root-x", "/动画/作品") is False

    def test_failed_blocks(self):
        """failed 同样阻塞收口：作品不完整不得生成可执行版本。"""
        _insert("/动画/作品", "complete")
        _insert("/动画/作品/Season 1", "complete")
        _insert("/动画/作品/OVA", "failed")
        assert closure.is_boundary_complete("root-x", "/动画/作品") is False

    def test_no_checkpoint_is_false(self):
        """boundary 下没有任何目录记录 → False（从未确认过的边界不是 complete）。"""
        assert closure.is_boundary_complete("root-x", "/动画/作品") is False

    def test_sibling_boundary_does_not_affect(self):
        """无关作品（另一 boundary）的状态不影响本 boundary。"""
        _insert("/动画/作品", "complete")
        _insert("/动画/作品/Season 1", "complete")
        _insert("/动画/别的作品", "failed")  # 同级另一 boundary，不在本边界相关集合内
        assert closure.is_boundary_complete("root-x", "/动画/作品") is True

    def test_prefix_matching_does_not_leak_sibling(self):
        """LIKE 前缀匹配不会把「/动画/作品集」这类同级目录误算进「/动画/作品」。"""
        _insert("/动画/作品", "complete")
        _insert("/动画/作品/Season 1", "complete")
        _insert("/动画/作品集", "failed")  # remote_path = '/动画/作品集' 不匹配 '/动画/作品/%'
        assert closure.is_boundary_complete("root-x", "/动画/作品") is True

    def test_other_root_does_not_affect(self):
        _insert("/动画/作品", "complete", root_id="root-x")
        _insert("/动画/作品", "failed", root_id="root-y")
        assert closure.is_boundary_complete("root-x", "/动画/作品") is True

    def test_missing_checkpoint_row_means_not_relevant(self):
        """「当前有效」= 行存在：消失目录的 checkpoint 行被删除后不再阻塞收口。"""
        _insert("/动画/作品", "complete")
        _insert("/动画/作品/Season 1", "complete")
        # Season 2 曾经 failed，随后目录消失被删除 checkpoint（M2-A 语义）：
        # 行已不存在 → 不阻塞
        assert closure.is_boundary_complete("root-x", "/动画/作品") is True
