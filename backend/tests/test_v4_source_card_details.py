"""来源卡路径与导入完成时间只消费已保存证据。"""
import json

from .test_v4_source_card_scope_counts import _draft_card, _pair


def test_source_card_uses_original_tree_selection_and_job_finish_time(tmp_path):
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.revisions.service import V4RevisionService

    database, _ = _draft_card(tmp_path, [_pair("one", "Show.S01E01.mkv")])
    V4RevisionService(database).confirm("rev-scope")
    path = r"K:\百度网盘\01动画\01动画.txt"
    stamp = "2026-09-30T04:34:00Z"
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_scan_requests(scan_id, request_json, created_at, updated_at) "
            "VALUES ('scan-scope', ?, 'now', 'now')",
            (json.dumps({"tree_file_path": path}),),
        )
        conn.execute("UPDATE jobs SET status = 'succeeded', finished_at = ?", (stamp,))
        conn.execute("UPDATE source_roots SET updated_at = '2099-01-01', display_name = '重命名'")
    card = list_source_cards(database)[0]
    assert card["tree_file_path"] == path
    assert card["import_completed_at"] == stamp
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE job_type = 'scrape_work'")
    assert list_source_cards(database)[0]["import_completed_at"] == ""


def test_old_source_card_does_not_invent_txt_path_or_completion_time(tmp_path):
    from app.media_v4.projection.source_libraries import list_source_cards

    database, _ = _draft_card(tmp_path, [_pair("one", "Show.S01E01.mkv")])
    card = list_source_cards(database)[0]
    assert card["tree_file_path"] == ""
    assert card["import_completed_at"] == ""
