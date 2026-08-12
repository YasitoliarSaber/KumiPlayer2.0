# -*- coding: utf-8 -*-
"""M03 资源类型识别与导入计划模型 单元测试

覆盖 M03 设计说明要求的全部测试项。
"""

import sys
from pathlib import Path

# 确保可以导入 app 模块
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# 辅助函数
# ============================================================

def _make_raw_file(
    name: str = "",
    ext: str = "",
    resource_hint: str = "",
    relative_path: str = "",
    source: str = "pan115",
):
    """构造 RawFile 用于测试"""
    from app.raw.models import RawFile

    stem = name
    dot_idx = name.rfind(".")
    if dot_idx > 0:
        stem = name[:dot_idx]
        if not ext:
            ext = name[dot_idx:]

    return RawFile(
        id=f"id-{name}",
        snapshot_id="snap-1",
        source=source,
        source_root=r"H:\115open",
        virtual_root="动画",
        relative_path=relative_path or f"动画/test/{name}",
        real_path=rf"H:\115open\动画\test\{name}",
        name=name,
        stem=stem,
        ext=ext,
        depth=3,
        parent_path="动画/test",
        is_file=True,
        resource_hint=resource_hint,
    )


def _make_snapshot(files):
    """构造 RawSnapshot 用于测试"""
    from app.raw.models import RawSnapshot

    return RawSnapshot(
        snapshot_id="snap-test-1",
        source="pan115",
        source_root=r"H:\115open",
        created_at="2026-06-12T12:00:00+08:00",
        input_file="test.txt",
        file_count=len(files),
        video_count=sum(1 for f in files if f.resource_hint == "video"),
        files=files,
    )


# ============================================================
# 资源类型分类测试
# ============================================================

def test_classify_video():
    """classify video: .mkv / .MP4 → video"""
    from app.recognition.resource_type import classify_resource_type

    assert classify_resource_type(ext=".mkv") == "video"
    assert classify_resource_type(ext=".MP4") == "video"
    assert classify_resource_type(ext=".avi") == "video"
    assert classify_resource_type(ext=".ts") == "video"
    assert classify_resource_type(ext=".m2ts") == "video"
    assert classify_resource_type(ext=".wmv") == "video"
    assert classify_resource_type(ext=".flv") == "video"
    assert classify_resource_type(ext=".rmvb") == "video"
    assert classify_resource_type(ext=".mov") == "video"


def test_classify_subtitle():
    """classify subtitle: .ass / .SRT → subtitle"""
    from app.recognition.resource_type import classify_resource_type

    assert classify_resource_type(ext=".ass") == "subtitle"
    assert classify_resource_type(ext=".SRT") == "subtitle"
    assert classify_resource_type(ext=".ssa") == "subtitle"
    assert classify_resource_type(ext=".vtt") == "subtitle"


def test_classify_non_video():
    """classify non-video: .nfo / .jpg / .zip 等 → 对应资源类型"""
    from app.recognition.resource_type import classify_resource_type

    assert classify_resource_type(ext=".nfo") == "nfo"
    assert classify_resource_type(ext=".jpg") == "image"
    assert classify_resource_type(ext=".jpeg") == "image"
    assert classify_resource_type(ext=".png") == "image"
    assert classify_resource_type(ext=".gif") == "image"
    assert classify_resource_type(ext=".bmp") == "image"
    assert classify_resource_type(ext=".webp") == "image"
    assert classify_resource_type(ext=".ttf") == "font"
    assert classify_resource_type(ext=".ttc") == "font"
    assert classify_resource_type(ext=".otf") == "font"
    assert classify_resource_type(ext=".zip") == "archive"
    assert classify_resource_type(ext=".rar") == "archive"
    assert classify_resource_type(ext=".7z") == "archive"
    assert classify_resource_type(ext=".exe") == "archive"
    assert classify_resource_type(ext=".mp3") == "audio"
    assert classify_resource_type(ext=".txt") == "text"


def test_classify_unknown_ext():
    """unknown ext: .abc → other"""
    from app.recognition.resource_type import classify_resource_type

    assert classify_resource_type(ext=".abc") == "other"
    assert classify_resource_type(ext=".xyz") == "other"
    assert classify_resource_type(ext="") == "other"


def test_classify_hint_conflict():
    """hint conflict: ext=.mkv, hint=image → 以 ext 为准，输出 video"""
    from app.recognition.resource_type import classify_resource_type

    result = classify_resource_type(ext=".mkv", resource_hint="image")
    assert result == "video", f"应以 ext 为准，实际: {result}"

    result = classify_resource_type(ext=".jpg", resource_hint="video")
    assert result == "image", f"应以 ext 为准，实际: {result}"


def test_classify_from_name():
    """从 name 提取扩展名"""
    from app.recognition.resource_type import classify_resource_type

    assert classify_resource_type(name="视频.mkv") == "video"
    assert classify_resource_type(name="字幕.SRT") == "subtitle"
    assert classify_resource_type(name="图片.PNG") == "image"


def test_classify_hint_only():
    """ext 和 name 都无法识别时，参考 resource_hint"""
    from app.recognition.resource_type import classify_resource_type

    assert classify_resource_type(resource_hint="video") == "video"
    assert classify_resource_type(resource_hint="subtitle") == "subtitle"
    assert classify_resource_type(resource_hint="image") == "image"
    assert classify_resource_type(resource_hint="invalid") == "other"


def test_normalize_ext():
    """normalize_ext 归一化"""
    from app.recognition.resource_type import normalize_ext

    assert normalize_ext(".MKV") == ".mkv"
    assert normalize_ext("mkv") == ".mkv"
    assert normalize_ext(".mp4") == ".mp4"
    assert normalize_ext("") == ""
    assert normalize_ext("  .AVI  ") == ".avi"


# ============================================================
# action 决策测试
# ============================================================

def test_action_video():
    """action video → generate_strm"""
    from app.recognition.resource_type import decide_import_action

    assert decide_import_action("video") == "generate_strm"
    assert decide_import_action("video", "pan115") == "generate_strm"


def test_action_subtitle():
    """action subtitle → attach_only"""
    from app.recognition.resource_type import decide_import_action

    assert decide_import_action("subtitle") == "attach_only"


def test_action_pan_nfo_image():
    """action pan nfo/image → ignore"""
    from app.recognition.resource_type import decide_import_action

    assert decide_import_action("nfo", "pan115") == "ignore"
    assert decide_import_action("image", "pan115") == "ignore"
    assert decide_import_action("font", "pan115") == "ignore"
    assert decide_import_action("archive", "pan115") == "ignore"
    assert decide_import_action("audio", "pan115") == "ignore"
    assert decide_import_action("text", "pan115") == "ignore"
    assert decide_import_action("other", "pan115") == "ignore"


# ============================================================
# draft ImportPlan 生成测试
# ============================================================

def test_build_draft_plan_all_files_have_items():
    """build draft plan: 每个 RawFile 都有 ImportPlanItem"""
    from app.recognition.planner import build_draft_import_plan

    files = [
        _make_raw_file(name="视频.mkv"),
        _make_raw_file(name="字幕.ass"),
        _make_raw_file(name="信息.nfo"),
        _make_raw_file(name="图片.jpg"),
        _make_raw_file(name="字体.ttf"),
        _make_raw_file(name="压缩.zip"),
    ]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    assert len(plan.items) == len(files), f"item 数 {len(plan.items)} != 文件数 {len(files)}"


def test_build_draft_plan_video_action():
    """视频 action=generate_strm"""
    from app.recognition.planner import build_draft_import_plan

    files = [_make_raw_file(name="视频.mkv")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    assert len(plan.items) == 1
    assert plan.items[0].action == "generate_strm"
    assert plan.items[0].resource_type == "video"


def test_build_draft_plan_subtitle_action():
    """字幕 action=attach_only"""
    from app.recognition.planner import build_draft_import_plan

    files = [_make_raw_file(name="字幕.ass")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    assert plan.items[0].action == "attach_only"
    assert plan.items[0].resource_type == "subtitle"


def test_build_draft_plan_ignore_actions():
    """nfo/image/font/archive/audio/text → ignore"""
    from app.recognition.planner import build_draft_import_plan

    test_cases = [
        ("信息.nfo", "nfo"),
        ("图片.jpg", "image"),
        ("字体.ttf", "font"),
        ("压缩.zip", "archive"),
        ("音频.mp3", "audio"),
        ("说明.txt", "text"),
    ]
    for filename, expected_type in test_cases:
        files = [_make_raw_file(name=filename)]
        snap = _make_snapshot(files)
        plan = build_draft_import_plan(snap)
        item = plan.items[0]
        assert item.resource_type == expected_type, f"{filename}: 类型应为 {expected_type}，实际 {item.resource_type}"
        assert item.action == "ignore", f"{filename}: action 应为 ignore，实际 {item.action}"


def test_no_media_recognition():
    """不填 season_number / episode_number / group_type（即使文件名含 S01E01 / NCOP）"""
    from app.recognition.planner import build_draft_import_plan

    files = [
        _make_raw_file(name="冰菓.S01E01.重生.mkv", ext=".mkv"),
        _make_raw_file(name="NCOP01.mkv", ext=".mkv"),
        _make_raw_file(name="[BeanSub]special.mkv", ext=".mkv"),
    ]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    for item in plan.items:
        assert item.season_number is None, f"不应填 season_number: {item.relative_path}"
        assert item.episode_number is None, f"不应填 episode_number: {item.relative_path}"
        assert item.group_type == "", f"不应填 group_type: {item.relative_path}"
        assert item.work_title == "", f"不应填 work_title: {item.relative_path}"


def test_summary():
    """summary 统计正确"""
    from app.recognition.planner import build_draft_import_plan

    files = [
        _make_raw_file(name="v1.mkv"),
        _make_raw_file(name="v2.mp4"),
        _make_raw_file(name="s1.ass"),
        _make_raw_file(name="s2.srt"),
        _make_raw_file(name="img.jpg"),
        _make_raw_file(name="info.nfo"),
        _make_raw_file(name="unknown.abc"),
    ]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)
    summary = plan.summary

    assert summary["total_items"] == 7
    assert summary["video_count"] == 2
    assert summary["subtitle_count"] == 2
    assert summary["by_action"]["generate_strm"] == 2
    assert summary["by_action"]["attach_only"] == 2
    assert summary["by_action"]["ignore"] == 3
    assert summary["needs_review_count"] == 1  # unknown.abc


def test_stable_ids():
    """同一 snapshot 重复生成，plan_id / item_id 稳定"""
    from app.recognition.planner import build_draft_import_plan

    files = [
        _make_raw_file(name="视频.mkv"),
        _make_raw_file(name="字幕.ass"),
    ]
    snap = _make_snapshot(files)

    plan1 = build_draft_import_plan(snap)
    plan2 = build_draft_import_plan(snap)

    assert plan1.plan_id == plan2.plan_id, "plan_id 不稳定"
    for i1, i2 in zip(plan1.items, plan2.items):
        assert i1.id == i2.id, f"item_id 不稳定: {i1.relative_path}"


def test_plan_status_draft():
    """ImportPlan.status = draft"""
    from app.recognition.planner import build_draft_import_plan

    files = [_make_raw_file(name="视频.mkv")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    assert plan.status == "draft"
    assert plan.source == "pan115"
    assert plan.source_snapshot_id == "snap-test-1"


def test_item_fields_filled():
    """ImportPlanItem 必填字段都已填充"""
    from app.recognition.planner import build_draft_import_plan

    files = [_make_raw_file(name="视频.mkv", relative_path="动画/test/视频.mkv")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    item = plan.items[0]
    assert item.id != ""
    assert item.plan_id != ""
    assert item.raw_file_id != ""
    assert item.source == "pan115"
    assert item.relative_path == "动画/test/视频.mkv"
    assert item.real_path != ""
    assert item.resource_type == "video"
    assert item.action == "generate_strm"
    assert item.confidence == "high"
    assert item.needs_review is False
    assert len(item.reasons) > 0


def test_other_needs_review():
    """other 类型 needs_review=True"""
    from app.recognition.planner import build_draft_import_plan

    files = [_make_raw_file(name="未知.xyz")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    item = plan.items[0]
    assert item.resource_type == "other"
    assert item.needs_review is True
    assert item.confidence == "low"


def test_unknown_ext_with_valid_hint():
    """unknown ext + valid resource_hint: confidence=medium，reasons 说明来自 hint"""
    from app.recognition.planner import build_draft_import_plan

    # .abc 不是已知扩展名，但 resource_hint="video"
    files = [_make_raw_file(name="file.abc", ext=".abc", resource_hint="video")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    item = plan.items[0]
    assert item.resource_type == "video", f"应为 video，实际: {item.resource_type}"
    assert item.confidence == "medium", f"confidence 应为 medium（来自 hint），实际: {item.confidence}"
    assert item.action == "generate_strm", f"action 应为 generate_strm，实际: {item.action}"
    assert any("resource_hint" in r for r in item.reasons), f"reasons 应说明来自 resource_hint: {item.reasons}"
    assert any("弱提示" in r for r in item.reasons), f"reasons 应说明是弱提示: {item.reasons}"


def test_known_ext_high_confidence():
    """已知扩展名: confidence=high，reasons 说明来自扩展名"""
    from app.recognition.planner import build_draft_import_plan

    files = [_make_raw_file(name="视频.mkv", ext=".mkv")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    item = plan.items[0]
    assert item.resource_type == "video"
    assert item.confidence == "high", f"已知扩展名 confidence 应为 high，实际: {item.confidence}"
    assert any("识别为" in r for r in item.reasons), f"reasons 应说明扩展名识别: {item.reasons}"


def test_reasons_content():
    """reasons 内容包含扩展名和动作说明"""
    from app.recognition.planner import build_draft_import_plan

    files = [_make_raw_file(name="视频.mkv")]
    snap = _make_snapshot(files)
    plan = build_draft_import_plan(snap)

    reasons = plan.items[0].reasons
    assert any(".mkv" in r for r in reasons), f"reasons 应包含扩展名: {reasons}"
    assert any("strm" in r for r in reasons), f"reasons 应包含动作说明: {reasons}"


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        # 资源类型分类
        test_classify_video,
        test_classify_subtitle,
        test_classify_non_video,
        test_classify_unknown_ext,
        test_classify_hint_conflict,
        test_classify_from_name,
        test_classify_hint_only,
        test_normalize_ext,
        # action 决策
        test_action_video,
        test_action_subtitle,
        test_action_pan_nfo_image,
        # draft ImportPlan
        test_build_draft_plan_all_files_have_items,
        test_build_draft_plan_video_action,
        test_build_draft_plan_subtitle_action,
        test_build_draft_plan_ignore_actions,
        test_no_media_recognition,
        test_summary,
        test_stable_ids,
        test_plan_status_draft,
        test_item_fields_filled,
        test_other_needs_review,
        test_unknown_ext_with_valid_hint,
        test_known_ext_high_confidence,
        test_reasons_content,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
