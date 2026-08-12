# -*- coding: utf-8 -*-
"""M05 导入预览与用户修正 单元测试"""

import json
import sys
import shutil
from pathlib import Path

# 确保可以导入 app 模块
sys.path.insert(0, str(Path(__file__).parent.parent))

# 测试数据目录
_TEST_DATA_DIR = Path(__file__).parent.parent.parent / "data"


def _cleanup_data():
    """清理测试数据目录"""
    if _TEST_DATA_DIR.exists():
        try:
            shutil.rmtree(_TEST_DATA_DIR)
        except OSError:
            shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


def _make_video_item(
    item_id: str,
    work_title: str = "测试作品",
    year: int = 2024,
    work_id: str = "",
    series_group: str = "",
    group_type: str = "season",
    season_number: int = 1,
    episode_number: int = 1,
    confidence: str = "high",
    needs_review: bool = False,
    action: str = "generate_strm",
):
    """构造视频 ImportPlanItem"""
    from app.import_plan.models import ImportPlanItem
    return ImportPlanItem(
        id=item_id,
        plan_id="plan-test",
        raw_file_id=f"raw-{item_id}",
        source="pan115",
        relative_path=f"动画/{work_title}.{year}/{item_id}.mkv",
        real_path=rf"H:\115open\动画\{work_title}.{year}\{item_id}.mkv",
        resource_type="video",
        action=action,
        work_id=work_id or f"{work_title}-{year}",
        work_title=work_title,
        year=year,
        media_type="tv",
        series_group=series_group,
        group_type=group_type,
        card_type="main_series",
        season_number=season_number,
        episode_number=episode_number,
        confidence=confidence,
        needs_review=needs_review,
    )


def _make_plan(items, import_scope=""):
    """构造 ImportPlan"""
    from app.import_plan.models import ImportPlan
    return ImportPlan(
        plan_id="plan-test",
        source="pan115",
        source_snapshot_id="snap-test",
        import_scope=import_scope,
        status="draft",
        items=items,
    )


# ============================================================
# build_preview 测试
# ============================================================

def test_preview_summary_counts():
    """预览统计 total/video/generate/ignore/needs_review"""
    from app.import_plan.service import build_preview

    items = [
        _make_video_item("v1"),
        _make_video_item("v2", season_number=1, episode_number=2),
        _make_video_item("v3", action="ignore", group_type=""),
        _make_video_item("v4", confidence="low"),
    ]
    plan = _make_plan(items)
    preview = build_preview(plan)

    assert preview.summary["total_items"] == 4
    assert preview.summary["video_count"] == 4
    assert preview.summary["generate_strm_count"] == 3
    assert preview.summary["ignored_count"] == 1
    assert preview.summary["low_confidence_count"] == 1


def test_recognize_import_plan_assigns_show_type_from_import_category():
    """首页分类由导入目录语义决定，不由刮削结果决定。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.recognition.plan_recognizer import recognize_import_plan_media

    cases = [
        ("pan115", "动画/测试番剧.2024/测试番剧.S01E01.mkv", "anime_series", "tv"),
        ("pan115", "动画电影/测试电影 (2024)/测试电影 (2024).mkv", "anime_movie", "movie"),
        ("pan115", "剧集/测试剧集.2024/测试剧集.S01E01.mkv", "live_series", "tv"),
        ("pan115", "电影/测试真人电影 (2024)/测试真人电影 (2024).mkv", "live_movie", "movie"),
        ("local", "Yuru Camp/Season 1/Yuru Camp - S01E01.mkv", "anime_series", "tv"),
        (
            "baidu",
            "古诺希亚 (2025) {tmdbid-278604} [4K]/Season 1/"
            "古诺希亚.2025.S01E01.2160p.WebRip.HEVC.AAC-NoxiaAI.mkv",
            "anime_series",
            "tv",
        ),
    ]

    for idx, (source, relative_path, show_type, media_type) in enumerate(cases):
        plan = ImportPlan(
            plan_id=f"plan-show-type-{idx}",
            source=source,
            status="draft",
            items=[ImportPlanItem(
                id=f"v{idx}",
                plan_id=f"plan-show-type-{idx}",
                raw_file_id=f"raw-v{idx}",
                source=source,
                relative_path=relative_path,
                real_path=f"H:\\test\\v{idx}.mkv",
                resource_type="video",
                action="generate_strm",
            )],
        )

        recognize_import_plan_media(plan)

        assert plan.items[0].show_type == show_type
        assert plan.items[0].media_type == media_type


def test_apply_movie_guess_fills_group_type():
    """media_type=movie 的识别结果不能留下空 group_type。"""
    from app.import_plan.models import ImportPlanItem
    from app.recognition.media import MediaGuess
    from app.recognition.plan_recognizer import _apply_guess_to_item

    item = ImportPlanItem(
        id="movie-empty-group",
        plan_id="plan-movie-empty-group",
        raw_file_id="raw-movie-empty-group",
        source="pan115",
        relative_path="动画/佐贺偶像是传奇 梦幻银河乐园/Zombie Land Saga Yumeginga Paradise.mkv",
        real_path="115://movie.mkv",
        resource_type="video",
        action="generate_strm",
    )
    guess = MediaGuess(
        work_title="佐贺偶像是传奇 梦幻银河乐园",
        work_id="zombie-land-saga-yumeginga-paradise",
        media_type="movie",
        group_type="",
        confidence="low",
    )

    _apply_guess_to_item(item, guess)

    assert item.group_type == "movie"
    assert item.card_type == "standalone"
    assert item.media_type == "movie"


def test_preview_low_confidence_issue():
    """低置信度 item 生成 low_confidence issue"""
    from app.import_plan.service import build_preview

    items = [_make_video_item("v1", confidence="low")]
    plan = _make_plan(items)
    preview = build_preview(plan)

    codes = [issue.code for issue in preview.issues]
    assert "low_confidence" in codes


def test_preview_needs_review_issue():
    """needs_review item 生成 needs_review issue"""
    from app.import_plan.service import build_preview

    items = [_make_video_item("v1", needs_review=True)]
    plan = _make_plan(items)
    preview = build_preview(plan)

    codes = [issue.code for issue in preview.issues]
    assert "needs_review" in codes


def test_preview_ungrouped_video_issue():
    """未分组视频生成 ungrouped_video issue"""
    from app.import_plan.service import build_preview

    items = [_make_video_item("v1", group_type="")]
    plan = _make_plan(items)
    preview = build_preview(plan)

    codes = [issue.code for issue in preview.issues]
    assert "ungrouped_video" in codes
    issue = next(i for i in preview.issues if i.code == "ungrouped_video")
    assert "group_type" not in issue.message
    assert "正片、特别篇还是电影" in issue.message


def test_preview_duplicate_episode_issue():
    """重复 season episode 生成 duplicate_episode issue"""
    from app.import_plan.service import build_preview

    items = [
        _make_video_item("v1", season_number=1, episode_number=1),
        _make_video_item("v2", season_number=1, episode_number=1),
    ]
    plan = _make_plan(items)
    preview = build_preview(plan)

    codes = [issue.code for issue in preview.issues]
    assert "duplicate_episode" in codes


def test_preview_does_not_duplicate_distinct_work_ids_with_same_series_group():
    """同一系列下的不同版本/年份不能因为 series_group 相同而互相判重。"""
    from app.import_plan.service import build_preview

    items = [
        _make_video_item(
            "rezero-2016-e01",
            work_title="Re：从零开始的异世界生活",
            year=2016,
            work_id="rezero-2016",
            series_group="Re：从零开始的异世界生活",
            season_number=1,
            episode_number=1,
        ),
        _make_video_item(
            "rezero-2020-e01",
            work_title="Re：从零开始的异世界生活",
            year=2020,
            work_id="rezero-2020-shin",
            series_group="Re：从零开始的异世界生活",
            season_number=1,
            episode_number=1,
        ),
    ]
    items[1].relative_path = (
        "动画/Re：从零开始的异世界生活.S1-S3（将更新）/"
        "1.Re：从零开始的异世界生活 新编撰版.[S1].2020/rezero-2020-e01.mkv"
    )
    plan = _make_plan(items)
    preview = build_preview(plan)

    codes = [issue.code for issue in preview.issues]
    assert "duplicate_episode" not in codes
    assert preview.summary["needs_review_count"] == 0
    assert preview.summary["duplicate_episode_count"] == 0


def test_preview_does_not_duplicate_spinoff_with_parent_same_episode_number():
    """外传和主系列同属一个系列组时，不能因相同集号被误判为重复。"""
    from app.import_plan.service import build_preview

    items = [
        _make_video_item(
            "heya-camp-e01",
            work_title="Heya Camp",
            year=2020,
            work_id="heya-camp",
            series_group="Yuru Camp",
            season_number=1,
            episode_number=1,
        ),
        _make_video_item(
            "yuru-camp-e01",
            work_title="Yuru Camp",
            year=2020,
            work_id="yuru-camp",
            series_group="Yuru Camp",
            season_number=1,
            episode_number=1,
        ),
    ]

    preview = build_preview(_make_plan(items))

    assert "duplicate_episode" not in [issue.code for issue in preview.issues]
    assert preview.summary["needs_review_count"] == 0
    assert preview.summary["duplicate_episode_count"] == 0


def test_preview_duplicate_episode_blocks_across_version_work_ids():
    """同系列不同版本即使 work_id 不同，也不能静默占用同一真实集号。"""
    from app.import_plan.service import build_preview

    first = _make_video_item("v1", season_number=1, episode_number=1)
    second = _make_video_item("v2", season_number=1, episode_number=1)
    first.work_id = "series-original"
    second.work_id = "series-remux"
    first.series_group = second.series_group = "同一作品"
    preview = build_preview(_make_plan([first, second]))

    assert preview.summary["duplicate_episode_count"] == 2
    assert preview.summary["needs_review_count"] == 2
    assert "duplicate_episode" in [issue.code for issue in preview.issues]
    dup_issue = next(i for i in preview.issues if i.code == "duplicate_episode")
    assert len(dup_issue.item_ids) == 2
    assert "work_id" not in dup_issue.message
    assert "《测试作品》第 1 季第 1 集" in dup_issue.message


def test_preview_groups():
    """预览分组正确"""
    from app.import_plan.service import build_preview

    items = [
        _make_video_item("v1", work_title="作品A", season_number=1, episode_number=1),
        _make_video_item("v2", work_title="作品A", season_number=1, episode_number=2),
        _make_video_item("v3", work_title="作品A", season_number=2, episode_number=1),
    ]
    plan = _make_plan(items)
    preview = build_preview(plan)

    assert len(preview.groups) == 2  # S1 和 S2 两个分组


# ============================================================
# patch 测试
# ============================================================

def test_patch_allow_whitelist():
    """patch 允许白名单字段"""
    from app.import_plan.service import patch_plan_item

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    item, preview, error = patch_plan_item(plan, "v1", {
        "work_title": "修正后的作品名",
        "season_number": 2,
        "needs_review": False,
    })
    assert error is None
    assert item.work_title == "修正后的作品名"
    assert item.season_number == 2


def test_patch_keeps_manual_season_correction_in_a_consistent_tv_shape():
    """用户把误识别条目改为正片后，后端必须同步修正关联字段。"""
    from app.import_plan.service import patch_plan_item

    item = _make_video_item("v1")
    item.media_type = "movie"
    item.show_type = "anime_movie"
    item.group_type = "movie"
    plan = _make_plan([item])
    plan.import_family = "anime"

    updated, _, error = patch_plan_item(plan, "v1", {
        "action": "generate_strm",
        "group_type": "season",
        "season_number": 2,
        "episode_number": 1,
        "needs_review": False,
    })

    assert error is None
    assert updated.media_type == "tv"
    assert updated.show_type == "anime_series"
    assert updated.card_type == "main_series"


def test_patch_reject_unknown_field():
    """patch 拒绝未知字段"""
    from app.import_plan.service import patch_plan_item

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _, _, error = patch_plan_item(plan, "v1", {"unknown_field": "value"})
    assert error is not None
    assert "未知" in error


def test_patch_reject_target_field():
    """patch 拒绝 target_* 字段"""
    from app.import_plan.service import patch_plan_item

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _, _, error = patch_plan_item(plan, "v1", {"target_dir": "/some/path"})
    assert error is not None
    assert "禁止" in error

    _, _, error = patch_plan_item(plan, "v1", {"target_filename": "test.mkv"})
    assert error is not None

    _, _, error = patch_plan_item(plan, "v1", {"target_strm_path": "/test.strm"})
    assert error is not None


def test_patch_reject_source_field():
    """patch 拒绝来源字段"""
    from app.import_plan.service import patch_plan_item

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _, _, error = patch_plan_item(plan, "v1", {"source": "baidu"})
    assert error is not None
    assert "禁止" in error

    _, _, error = patch_plan_item(plan, "v1", {"relative_path": "new/path"})
    assert error is not None


def test_patch_user_override_id():
    """patch 后 user_override_id 有值"""
    from app.import_plan.service import patch_plan_item

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    item, _, error = patch_plan_item(plan, "v1", {"work_title": "修正"})
    assert error is None
    assert item.user_override_id is not None
    assert item.user_override_id != ""


def test_patch_reasons_appended():
    """patch 后 reasons 追加用户修正原因"""
    from app.import_plan.service import patch_plan_item

    items = [_make_video_item("v1")]
    plan = _make_plan(items)
    original_reasons_len = len(items[0].reasons)

    item, _, error = patch_plan_item(plan, "v1", {"work_title": "修正"})
    assert error is None
    assert len(item.reasons) > original_reasons_len
    assert any("用户修正" in r for r in item.reasons)


def test_patch_validate_values():
    """patch 值约束校验"""
    from app.import_plan.service import patch_plan_item

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    # 非法 action
    _, _, error = patch_plan_item(plan, "v1", {"action": "invalid"})
    assert error is not None

    # 非法 confidence
    _, _, error = patch_plan_item(plan, "v1", {"confidence": "invalid"})
    assert error is not None

    # 非法 year
    _, _, error = patch_plan_item(plan, "v1", {"year": 1800})
    assert error is not None

    # 非法 season_number（负数）
    _, _, error = patch_plan_item(plan, "v1", {"season_number": -1})
    assert error is not None

    # 非法 season_number（0，不是正整数）
    _, _, error = patch_plan_item(plan, "v1", {"season_number": 0})
    assert error is not None

    # 非法 episode_number（0）
    _, _, error = patch_plan_item(plan, "v1", {"episode_number": 0})
    assert error is not None

    # 合法值
    _, _, error = patch_plan_item(plan, "v1", {"year": 2024})
    assert error is None


def test_preview_season_missing_numbers():
    """group_type=season 但缺 season_number 或 episode_number 生成 error issue"""
    from app.import_plan.service import build_preview

    items = [
        _make_video_item("v1", group_type="season", season_number=None, episode_number=1),
        _make_video_item("v2", group_type="season", season_number=1, episode_number=None),
    ]
    plan = _make_plan(items)
    preview = build_preview(plan)

    invalid_issues = [i for i in preview.issues if i.code == "season_incomplete" and i.level == "error"]
    assert len(invalid_issues) >= 1, f"应有 error 级别 season_incomplete issue: {preview.issues}"
    assert len(invalid_issues[0].item_ids) == 2, f"应有 2 个 item: {invalid_issues[0].item_ids}"


def test_confirm_reject_season_missing_numbers():
    """confirm 拒绝 group_type=season 但缺 season/episode 的计划"""
    from app.import_plan.service import confirm_plan

    items = [
        _make_video_item("v1", group_type="season", season_number=None, episode_number=1),
    ]
    plan = _make_plan(items)

    _, error = confirm_plan(plan)
    assert error is not None, "group_type=season 缺 season_number 应被拒绝"


def test_confirm_reject_missing_work_title():
    """confirm 拒绝缺 work_title 的计划"""
    from app.import_plan.service import confirm_plan, build_preview

    items = [
        _make_video_item("v1", work_title=""),
    ]
    plan = _make_plan(items)

    # 先确认 preview 生成了 error 级别的 missing_work_title
    preview = build_preview(plan)
    error_issues = [i for i in preview.issues if i.level == "error" and i.code == "missing_work_title"]
    assert len(error_issues) > 0, f"应有 error 级别 missing_work_title: {preview.issues}"

    # confirm 应被拒绝
    _, error = confirm_plan(plan)
    assert error is not None, "缺 work_title 应被拒绝"


def test_confirm_reject_missing_group_type():
    """confirm 拒绝缺 group_type 的计划"""
    from app.import_plan.service import confirm_plan, build_preview

    items = [
        _make_video_item("v1", group_type=""),
    ]
    plan = _make_plan(items)

    # 先确认 preview 生成了 error 级别的 missing_group_type
    preview = build_preview(plan)
    error_issues = [i for i in preview.issues if i.level == "error" and i.code == "missing_group_type"]
    assert len(error_issues) > 0, f"应有 error 级别 missing_group_type: {preview.issues}"

    # confirm 应被拒绝
    _, error = confirm_plan(plan)
    assert error is not None, "缺 group_type 应被拒绝"


# ============================================================
# confirm 测试
# ============================================================

def test_confirm_defers_needs_review():
    """低置信 needs_review 只进入后续处理队列，不阻塞计划确认。"""
    from app.import_plan.service import confirm_plan

    items = [_make_video_item("v1", episode_number=1, needs_review=True)]
    plan = _make_plan(items)

    confirmed, error = confirm_plan(plan)
    assert error is None
    assert confirmed is not None
    assert confirmed.status == "confirmed"


def test_confirm_reject_no_video():
    """confirm 拒绝没有 generate_strm 视频的计划"""
    from app.import_plan.service import confirm_plan

    items = [_make_video_item("v1", action="ignore", group_type="")]
    plan = _make_plan(items)

    _, error = confirm_plan(plan)
    assert error is not None
    assert "generate_strm" in error


def test_confirm_success():
    """confirm 成功后 status=confirmed"""
    from app.import_plan.service import confirm_plan

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    confirmed, error = confirm_plan(plan)
    assert error is None
    assert confirmed.status == "confirmed"


def test_confirm_reject_non_draft():
    """confirm 拒绝非 draft 状态"""
    from app.import_plan.service import confirm_plan

    items = [_make_video_item("v1")]
    plan = _make_plan(items)
    plan.status = "confirmed"

    _, error = confirm_plan(plan)
    assert error is not None
    assert "draft" in error


# ============================================================
# API 测试
# ============================================================

def test_api_preview():
    """API preview 可返回 summary 与无伪造时间的 parse_logs"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.import_plan.store import save_import_plan

    _cleanup_data()
    try:
        items = [_make_video_item("v1")]
        plan = _make_plan(items)
        save_import_plan(plan)

        client = TestClient(app)
        response = client.get("/api/imports/pan115/preview")
        assert response.status_code == 200
        data = response.json()
        assert "summary" in data
        assert data["summary"]["total_items"] == 1
        assert data["summary"]["work_count"] == 1
        assert data["import_scope"] == ""
        # parse_logs 必须存在、只含 kind/message、不得伪造 time
        assert "parse_logs" in data
        assert isinstance(data["parse_logs"], list)
        assert len(data["parse_logs"]) == 4
        for entry in data["parse_logs"]:
            assert set(entry.keys()) == {"kind", "message"}, f"parse_logs 条目不得含 time: {entry}"
            assert entry["kind"] in {"info", "done", "warn", "error"}
        assert data["parse_logs"][1]["message"] == "作品识别完成：1 部作品、1 个媒体分组"
        assert data["parse_logs"][-1]["message"] == "当前阶段只确认识别结果；确认后才创建镜像并补充资料。"
    finally:
        _cleanup_data()


def test_preview_work_count_deduplicates_seasons_of_the_same_work():
    """同一作品的多季只能算一部作品；work_count 不等于 groups.length。"""
    from app.import_plan.service import build_preview

    items = [
        _make_video_item("s1-e1", work_title="作品A", work_id="work-a", season_number=1, episode_number=1),
        _make_video_item("s1-e2", work_title="作品A", work_id="work-a", season_number=1, episode_number=2),
        _make_video_item("s2-e1", work_title="作品A", work_id="work-a", season_number=2, episode_number=1),
        _make_video_item("movie-1", work_title="作品A 剧场版", work_id="work-a-movie", group_type="movie",
                         season_number=None, episode_number=None),
    ]
    plan = _make_plan(items)
    preview = build_preview(plan)

    assert preview.summary["work_count"] == 2  # 作品A + 作品A剧场版
    assert len(preview.groups) == 3  # S1、S2、剧场版


def test_preview_work_count_excludes_op_ed_and_auxiliary_videos():
    """OP/ED 与 PV/CM 等辅助视频不能独立计为媒体库作品。"""
    from app.import_plan.models import ImportPlanItem
    from app.import_plan.service import build_preview

    items = [
        _make_video_item("e1", work_title="作品B", work_id="work-b", season_number=1, episode_number=1),
    ]
    op_ed = ImportPlanItem(
        id="oped-1", plan_id="plan-test", raw_file_id="raw-oped", source="pan115",
        relative_path="动画/作品B/作品B OP.mkv", real_path="H:\\作品B OP.mkv",
        resource_type="video", action="generate_strm",
        work_title="作品B", work_id="work-b", group_type="op_ed",
        card_type="main_series", media_type="tv",
    )
    auxiliary = ImportPlanItem(
        id="pv-1", plan_id="plan-test", raw_file_id="raw-pv", source="pan115",
        relative_path="动画/作品B/作品B PV.mkv", real_path="H:\\作品B PV.mkv",
        resource_type="video", action="ignore",
        work_title="作品B", work_id="work-b", group_type="auxiliary",
        card_type="main_series", media_type="tv",
    )
    items.append(op_ed)
    items.append(auxiliary)
    preview = build_preview(_make_plan(items))

    assert preview.summary["work_count"] == 1
    assert preview.summary["video_count"] == 3


def test_preview_work_count_falls_back_for_empty_work_ids():
    """空 work_id 使用 card_type + series_group/work_title + year 作为回退身份。"""
    from app.import_plan.service import build_preview

    first = _make_video_item("f1", work_title="作品C", year=2020, work_id="", season_number=1, episode_number=1)
    second = _make_video_item("f2", work_title="作品C", year=2021, work_id="", season_number=1, episode_number=1)
    # 模拟识别阶段没有产出稳定 work_id 的情况
    first.work_id = ""
    second.work_id = ""
    preview = build_preview(_make_plan([first, second]))

    assert preview.summary["work_count"] == 2  # 年份不同，不能合并为一部


def test_api_preview_returns_seasonal_import_scope():
    """确认计划前端必须能从 HTTP 预览响应读取追更范围。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.import_plan.store import save_import_plan

    _cleanup_data()
    try:
        plan = _make_plan([_make_video_item("v1")], import_scope="seasonal")
        save_import_plan(plan)

        response = TestClient(app).get("/api/imports/pan115/preview")

        assert response.status_code == 200
        assert response.json()["import_scope"] == "seasonal"
    finally:
        _cleanup_data()


def test_api_patch():
    """API patch 可更新 item"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.import_plan.store import save_import_plan

    _cleanup_data()
    try:
        items = [_make_video_item("v1")]
        plan = _make_plan(items)
        save_import_plan(plan)

        client = TestClient(app)
        response = client.patch(
            "/api/imports/pan115/items/v1",
            json={"plan_id": "plan-test", "patch": {"work_title": "API修正"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["item"]["work_title"] == "API修正"
    finally:
        _cleanup_data()


def test_api_confirm_no_mirror():
    """API confirm 不生成镜像、不返回 task_id"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.import_plan.store import save_import_plan

    _cleanup_data()
    try:
        items = [_make_video_item("v1")]
        plan = _make_plan(items)
        save_import_plan(plan)

        client = TestClient(app)
        response = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": "plan-test"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "confirmed"
        assert "task_id" not in data
        assert "mirror" not in data
    finally:
        _cleanup_data()


def test_api_patch_reject_target():
    """API patch 拒绝 target 字段"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.import_plan.store import save_import_plan

    _cleanup_data()
    try:
        items = [_make_video_item("v1")]
        plan = _make_plan(items)
        save_import_plan(plan)

        client = TestClient(app)
        response = client.patch(
            "/api/imports/pan115/items/v1",
            json={"plan_id": "plan-test", "patch": {"target_dir": "/evil"}},
        )
        assert response.status_code == 400
    finally:
        _cleanup_data()


# ============================================================
# store 测试
# ============================================================

def test_store_save_and_load():
    """保存和加载 ImportPlan"""
    from app.import_plan.store import save_import_plan, load_import_plan

    _cleanup_data()
    try:
        items = [_make_video_item("v1")]
        plan = _make_plan(items)
        save_import_plan(plan)

        loaded = load_import_plan(plan_id="plan-test")
        assert loaded is not None
        assert loaded.plan_id == "plan-test"
        assert len(loaded.items) == 1
        assert loaded.items[0].id == "v1"
    finally:
        _cleanup_data()


def test_store_load_latest():
    """加载 source_latest.json"""
    from app.import_plan.store import save_import_plan, load_import_plan

    _cleanup_data()
    try:
        items = [_make_video_item("v1")]
        plan = _make_plan(items)
        save_import_plan(plan)

        loaded = load_import_plan(source="pan115")
        assert loaded is not None
        assert loaded.plan_id == "plan-test"
    finally:
        _cleanup_data()


def test_store_user_overrides():
    """保存和加载 user_overrides"""
    from app.import_plan.store import save_user_override, load_user_overrides
    from app.import_plan.overrides import UserOverride

    _cleanup_data()
    try:
        override = UserOverride(
            override_id="ov-1",
            plan_id="plan-test",
            item_id="item-1",
            source="pan115",
            updated_at="2026-06-12T21:00:00+08:00",
            patch={"work_title": "修正"},
        )
        save_user_override(override)

        loaded = load_user_overrides()
        assert len(loaded.items) == 1
        assert loaded.items[0].item_id == "item-1"
    finally:
        _cleanup_data()


# ============================================================
# import_scope 预览测试
# ============================================================

def test_preview_import_scope_seasonal():
    """seasonal plan 的预览返回 import_scope=seasonal"""
    from app.import_plan.service import build_preview

    items = [_make_video_item("v1")]
    plan = _make_plan(items, import_scope="seasonal")
    preview = build_preview(plan)
    assert preview.import_scope == "seasonal"


def test_preview_import_scope_empty():
    """普通 plan 的预览返回 import_scope=空字符串"""
    from app.import_plan.service import build_preview

    items = [_make_video_item("v1")]
    plan = _make_plan(items, import_scope="")
    preview = build_preview(plan)
    assert preview.import_scope == ""


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        # build_preview
        test_preview_summary_counts,
        test_preview_low_confidence_issue,
        test_preview_needs_review_issue,
        test_preview_ungrouped_video_issue,
        test_preview_duplicate_episode_issue,
        test_preview_groups,
        # patch
        test_patch_allow_whitelist,
        test_patch_reject_unknown_field,
        test_patch_reject_target_field,
        test_patch_reject_source_field,
        test_patch_user_override_id,
        test_patch_reasons_appended,
        test_patch_validate_values,
        test_preview_season_missing_numbers,
        test_confirm_reject_season_missing_numbers,
        test_confirm_reject_missing_work_title,
        test_confirm_reject_missing_group_type,
        # import_scope
        test_preview_import_scope_seasonal,
        test_preview_import_scope_empty,
        # confirm
        test_confirm_reject_needs_review,
        test_confirm_reject_no_video,
        test_confirm_success,
        test_confirm_reject_non_draft,
        # API
        test_api_preview,
        test_api_patch,
        test_api_confirm_no_mirror,
        test_api_patch_reject_target,
        # store
        test_store_save_and_load,
        test_store_load_latest,
        test_store_user_overrides,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            _cleanup_data()
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        finally:
            _cleanup_data()
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
