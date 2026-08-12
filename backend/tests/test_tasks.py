# -*- coding: utf-8 -*-
"""M07 TaskManager 单元测试"""

import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_submit_returns_task():
    """submit 返回 task_id"""
    import time
    from app.tasks.manager import TaskManager

    mgr = TaskManager()
    record = mgr.submit("mirror_generate", "pan115", lambda: {"ok": True})
    assert record.task_id.startswith("task_")
    assert record.task_type == "mirror_generate"
    assert record.source == "pan115"
    time.sleep(0.5)
    # 任务可能已完成
    result = mgr.get_task(record.task_id)
    assert result.status in ("pending", "running", "succeeded")
    mgr.shutdown()


def test_task_succeeded_result():
    """任务成功后 result 有值"""
    import time
    from app.tasks.manager import TaskManager

    mgr = TaskManager()
    record = mgr.submit("mirror_generate", "pan115", lambda: {"count": 42})
    time.sleep(0.5)
    result = mgr.get_task(record.task_id)
    assert result.status == "succeeded"
    assert result.result["count"] == 42
    mgr.shutdown()


def test_task_succeeded():
    """任务执行成功后 status=succeeded progress=100"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager()
    record = mgr.submit(
        "mirror_generate", "pan115",
        lambda: {"generated_count": 10},
        message="测试",
    )
    # 等待完成
    time.sleep(0.5)
    result = mgr.get_task(record.task_id)
    assert result.status == "succeeded"
    assert result.progress == 100
    assert result.result["generated_count"] == 10
    mgr.shutdown()


def test_task_failed():
    """任务执行异常后 status=failed error 有值"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager()

    def _fail():
        raise RuntimeError("测试错误")

    record = mgr.submit("mirror_generate", "pan115", _fail)
    time.sleep(0.5)
    result = mgr.get_task(record.task_id)
    assert result.status == "failed"
    assert result.error == "测试错误"
    assert result.progress == 100
    mgr.shutdown()


def test_get_nonexistent_task():
    """查询不存在 task 返回 None"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager()
    result = mgr.get_task("nonexistent")
    assert result is None
    mgr.shutdown()


def test_reject_concurrent_same_type_source():
    """同 task_type + source 运行中任务会被拒绝"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager()

    def _slow():
        time.sleep(2)
        return {}

    mgr.submit("mirror_generate", "pan115", _slow)
    time.sleep(0.1)

    try:
        mgr.submit("mirror_generate", "pan115", _slow)
        assert False, "应抛出 ValueError"
    except ValueError as e:
        assert "运行中" in str(e)
    finally:
        mgr.shutdown()


def test_named_queue_runs_jobs_serially_and_starts_next_automatically():
    """同一命名队列只运行一个任务，前一个结束后自动启动下一个。"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager(max_workers=2)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    execution_order = []

    def _first():
        execution_order.append("first-start")
        first_started.set()
        release_first.wait(timeout=2)
        execution_order.append("first-end")
        return {"job": "first"}

    def _second():
        execution_order.append("second-start")
        second_started.set()
        return {"job": "second"}

    first = mgr.submit_queued(
        "scrape_auto", "pan115", _first,
        queue_name="scrape", initial_result={"plan_id": "plan-1"},
    )
    second = mgr.submit_queued(
        "scrape_select", "baidu", _second,
        queue_name="scrape", initial_result={"plan_id": "plan-2"},
    )

    assert first_started.wait(timeout=1)
    assert mgr.get_task(second.task_id).status == "pending"
    assert mgr.get_task(second.task_id).result["queue_position"] == 1
    assert not second_started.is_set()

    release_first.set()
    assert second_started.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline and mgr.get_task(second.task_id).status != "succeeded":
        time.sleep(0.02)

    assert mgr.get_task(first.task_id).result["plan_id"] == "plan-1"
    assert mgr.get_task(second.task_id).result["plan_id"] == "plan-2"
    assert execution_order == ["first-start", "first-end", "second-start"]
    mgr.shutdown()


def test_cancelled_task_keeps_running_lane_until_worker_exits():
    """停止是协作式的，旧线程退出前不能启动同来源替代任务。"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager(max_workers=2)
    started = threading.Event()
    release = threading.Event()

    def _blocking():
        started.set()
        release.wait(timeout=2)
        return {}

    record = mgr.submit("scrape_auto", "cancel-lane-test", _blocking)
    assert started.wait(timeout=1)
    assert mgr.cancel_task(record.task_id) is True

    try:
        mgr.submit("scrape_auto", "cancel-lane-test", lambda: {})
        assert False, "旧工作线程退出前不应释放刮削通道"
    except ValueError as exc:
        assert "运行中" in str(exc)
    finally:
        release.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            replacement = mgr.submit("scrape_auto", "cancel-lane-test", lambda: {"ok": True})
            break
        except ValueError:
            time.sleep(0.02)
    else:
        assert False, "旧工作线程退出后应释放刮削通道"

    deadline = time.time() + 2
    while time.time() < deadline and mgr.get_task(replacement.task_id).status != "succeeded":
        time.sleep(0.02)
    assert mgr.get_task(replacement.task_id).status == "succeeded"
    mgr.shutdown()


def test_should_cancel_is_injected_and_works():
    """submit 自动注入 should_cancel 闭包，任务函数可用它提前退出。"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager()

    def _check_cancel(should_cancel=None, progress_callback=None):
        assert callable(should_cancel), "should_cancel 未被注入"
        assert should_cancel() is False
        return {"checked": True}

    record = mgr.submit("scrape_auto", "inject-test", _check_cancel)
    time.sleep(0.5)
    result = mgr.get_task(record.task_id)
    assert result.status == "succeeded"
    assert result.result["checked"] is True
    mgr.shutdown()


def test_running_progress_is_persisted_with_throttle_but_final_state_is_forced():
    """高频日志实时留在内存，SQLite 只做节流快照并强制保存终态。"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager()
    persisted = []
    finished = threading.Event()

    def capture(record, *, throttle=False):
        persisted.append((record.status, record.progress, throttle))

    mgr._persist_task = capture

    def _chatty(progress_callback=None):
        for index in range(80):
            progress_callback(index, f"日志 {index}", {"logs": [{"message": f"日志 {index}"}]})
        finished.set()
        return {"count": 80}

    record = mgr.submit("scrape_auto", "persist-throttle", _chatty)
    assert finished.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline and mgr.get_task(record.task_id).status != "succeeded":
        time.sleep(0.01)

    result = mgr.get_task(record.task_id)
    assert result.status == "succeeded"
    assert result.result["count"] == 80
    assert len(persisted) <= 5
    assert persisted[-1][:2] == ("succeeded", 100)
    assert persisted[-1][2] is False
    mgr.shutdown()


def test_cancelled_task_has_its_own_terminal_status():
    """用户主动取消是独立终态，不能被误报为失败或覆盖为成功。"""
    import threading
    from app.tasks.manager import TaskManager

    mgr = TaskManager()
    started = threading.Event()

    def _slow(should_cancel=None, progress_callback=None):
        started.set()
        # 等待取消，然后模拟返回（应被 worker 检测到 cancelled 并标记为已停止）
        for _ in range(100):
            if should_cancel and should_cancel():
                return {"should_not_appear": True}
            time.sleep(0.01)
        return {"completed": True}

    record = mgr.submit("scrape_auto", "cancel-status-test", _slow)
    assert started.wait(timeout=2)
    time.sleep(0.05)
    mgr.cancel_task(record.task_id)
    # 等待 worker 退出
    time.sleep(0.5)
    result = mgr.get_task(record.task_id)
    assert result.status == "cancelled", f"期望 cancelled, 实际 {result.status}"
    assert result.message == "已停止", f"期望 '已停止', 实际 {result.message}"
    assert not result.error
    mgr.shutdown()


def test_library_clear_can_cancel_tracking_tasks_without_stopping_other_work():
    """清理媒体库时只停止追更扫描，避免已删除绑定被后台任务恢复。"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager(max_workers=2)
    release = threading.Event()
    tracking_started = threading.Event()
    mirror_started = threading.Event()

    def _wait(started, should_cancel=None):
        started.set()
        while not release.wait(timeout=0.01):
            if should_cancel and should_cancel():
                return {"stopped": True}
        return {"released": True}

    try:
        tracking = mgr.submit("tracking_scan", "series-a", _wait, tracking_started)
        mirror = mgr.submit("mirror_generate", "local", _wait, mirror_started)
        assert tracking_started.wait(timeout=1)
        assert mirror_started.wait(timeout=1)

        assert mgr.cancel_running_tracking_tasks() == 1
        # 协作式停止可能在读取状态前已经完成；两者都证明停止请求只命中了追更任务。
        assert mgr.get_task(tracking.task_id).message in {"正在停止", "已停止"}
        assert mgr.get_task(mirror.task_id).message != "正在停止"
    finally:
        release.set()
        mgr.shutdown()


def test_source_library_clear_only_cancels_matching_tracking_tasks():
    """按来源清理只能停止同来源追更，不能干扰其他盘的扫描。"""
    from app.tasks.manager import TaskManager

    mgr = TaskManager(max_workers=2)
    release = threading.Event()
    pan_started = threading.Event()
    baidu_started = threading.Event()

    def _wait(started, should_cancel=None):
        started.set()
        while not release.wait(timeout=0.01):
            if should_cancel and should_cancel():
                return {"stopped": True}
        return {"released": True}

    try:
        pan = mgr.submit("tracking_scan", "pan115", _wait, pan_started)
        baidu = mgr.submit("tracking_scan", "baidu", _wait, baidu_started)
        assert pan_started.wait(timeout=1)
        assert baidu_started.wait(timeout=1)

        assert mgr.cancel_running_tracking_tasks("pan115") == 1
        # 工作线程可能已经响应取消并进入最终状态，两种文案都表示命中了目标任务。
        assert mgr.get_task(pan.task_id).message in {"正在停止", "已停止"}
        assert mgr.get_task(baidu.task_id).message != "正在停止"
    finally:
        release.set()
        mgr.shutdown()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_submit_returns_task,
        test_task_succeeded,
        test_task_succeeded_result,
        test_task_failed,
        test_get_nonexistent_task,
        test_reject_concurrent_same_type_source,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
