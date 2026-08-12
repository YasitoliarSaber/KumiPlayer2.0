# -*- coding: utf-8 -*-
"""M06 镜像生成器 单元测试"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_TEST_MIRROR = Path(__file__).parent.parent / "data" / "test_mirror"


def _cleanup():
    if _TEST_MIRROR.exists():
        shutil.rmtree(_TEST_MIRROR)


def _make_video_item(
    item_id: str,
    work_title: str = "冰菓",
    year: int = 2012,
    group_type: str = "season",
    season_number: int = 1,
    episode_number: int = 1,
    special_number: int = None,
    title: str = "",
    action: str = "generate_strm",
    relative_path: str = "",
):
    from app.import_plan.models import ImportPlanItem
    return ImportPlanItem(
        id=item_id,
        plan_id="plan-test",
        raw_file_id=f"raw-{item_id}",
        source="pan115",
        relative_path=relative_path or f"动画/{work_title}.{year}/{item_id}.mkv",
        real_path=rf"H:\115open\动画\{work_title}.{year}\{item_id}.mkv",
        resource_type="video",
        action=action,
        work_title=work_title,
        year=year,
        media_type="tv",
        group_type=group_type,
        card_type="main_series",
        season_number=season_number,
        episode_number=episode_number,
        special_number=special_number,
        title=title,
        confidence="high",
    )


def _make_plan(items, status="confirmed"):
    from app.import_plan.models import ImportPlan
    return ImportPlan(
        plan_id="plan-test",
        source="pan115",
        source_snapshot_id="snap-test",
        status=status,
        items=items,
    )


# ============================================================
# 基础功能测试
# ============================================================

def test_reject_draft_plan():
    """拒绝 draft plan"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1")]
    plan = _make_plan(items, status="draft")

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "failed"
        assert any("draft" in e for e in result.errors)
    finally:
        _cleanup()


def test_generation_reports_batched_progress_and_live_counts(tmp_path):
    """长镜像任务必须持续上报处理数量，不能一直停在任务初始 5%。"""
    from app.mirror.generator import generate_mirror

    items = [
        _make_video_item(f"v{number}", episode_number=number)
        for number in range(1, 6)
    ]
    plan = _make_plan(items)
    updates = []

    result = generate_mirror(
        plan,
        str(tmp_path / "mirror"),
        progress_callback=lambda progress, message, patch: updates.append((progress, message, patch)),
    )

    assert result.status == "success"
    live_updates = [item for item in updates if item[1].startswith("正在生成镜像")]
    assert live_updates
    assert live_updates[-1][1] == "正在生成镜像 5/5"
    payload = live_updates[-1][2]
    assert payload["generated_count"] == 5
    assert payload["failed_count"] == 0
    assert payload["skipped_count"] == 0
    assert payload["items_count"] == 5
    assert payload["processed_count"] == 5
    # 生成器只报告准确的 current_target 与 log_kind，供 API 包装器消费
    assert payload["current_target"] == "冰菓 · 第 1 季 · 第 5 集"
    assert payload["log_kind"] == "info"
    assert all(20 <= progress <= 90 for progress, _, _ in live_updates)


def test_production_generation_stops_when_cloud_video_paths_are_invalid(tmp_path, monkeypatch):
    """正式生成前必须验证网盘视频样本，不能把错误路径写进 .strm。"""
    from app.mirror.generator import generate_mirror

    item = _make_video_item("missing")
    item.source = "baidu"
    item.real_path = str(tmp_path / "百度网盘" / "不存在" / "S01E01.mkv")
    plan = _make_plan([item])
    plan.source = "baidu"
    mirror_root = tmp_path / "mirror"
    monkeypatch.setattr("app.mirror.generator._get_mirror_root", lambda _root: mirror_root)

    result = generate_mirror(plan)

    assert result.status == "failed"
    assert any("路径验证" in error for error in result.errors)
    assert not list(mirror_root.rglob("*.strm"))


def test_openlist_generation_stops_when_mount_paths_invalid(tmp_path, monkeypatch):
    """OpenList 挂载失效时必须在写 .strm 前阻断，并给出可读提示。"""
    from app.mirror.generator import generate_mirror

    item = _make_video_item("openlist-missing")
    item.source = "openlist"
    item.real_path = str(tmp_path / "夸克挂载" / "动画" / "冰菓" / "S01E01.mkv")
    plan = _make_plan([item])
    plan.source = "openlist"
    mirror_root = tmp_path / "mirror"
    monkeypatch.setattr("app.mirror.generator._get_mirror_root", lambda _root: mirror_root)

    result = generate_mirror(plan)

    assert result.status == "failed"
    assert any("媒体路径验证失败" in error for error in result.errors)
    assert any("OpenList 本地挂载路径不可访问，请检查挂载后重试" in error for error in result.errors)
    assert not list(mirror_root.rglob("*.strm"))


def test_openlist_generation_continues_when_sample_paths_reachable(tmp_path, monkeypatch):
    """OpenList 抽样路径可访问时按既有规则继续生成镜像。"""
    from app.mirror.generator import generate_mirror

    video_dir = tmp_path / "夸克挂载" / "动画" / "冰菓"
    video_dir.mkdir(parents=True)
    real_file = video_dir / "S01E01.mkv"
    real_file.write_bytes(b"real")

    item = _make_video_item("openlist-ok")
    item.source = "openlist"
    item.real_path = str(real_file)
    plan = _make_plan([item])
    plan.source = "openlist"
    mirror_root = tmp_path / "mirror"
    monkeypatch.setattr("app.mirror.generator._get_mirror_root", lambda _root: mirror_root)

    result = generate_mirror(plan)

    assert result.status == "success"
    strms = list(mirror_root.rglob("*.strm"))
    assert len(strms) == 1
    assert strms[0].read_text(encoding="utf-8").strip() == str(real_file)


def test_openlist_dry_run_with_explicit_mirror_root_skips_validation(tmp_path):
    """显式 mirror_root 是测试/离线预演入口：不触发挂载验证，不误阻断。"""
    from app.mirror.generator import generate_mirror

    item = _make_video_item("openlist-dry")
    item.source = "openlist"
    item.real_path = str(tmp_path / "不存在的挂载" / "S01E01.mkv")
    plan = _make_plan([item])
    plan.source = "openlist"

    result = generate_mirror(plan, str(tmp_path / "mirror"))

    assert result.status == "success"
    assert not any("路径验证" in error for error in result.errors)


def test_only_process_video_generate_strm():
    """只处理 video + generate_strm"""
    from app.mirror.generator import generate_mirror
    from app.import_plan.models import ImportPlanItem

    items = [
        _make_video_item("v1"),
        _make_video_item("v2", action="ignore", group_type=""),
        ImportPlanItem(
            id="sub1", plan_id="plan-test", raw_file_id="raw-sub1",
            source="pan115", relative_path="test.ass", real_path="H:\\test.ass",
            resource_type="subtitle", action="attach_only",
        ),
    ]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.generated_count == 1
        assert result.skipped_count == 1
    finally:
        _cleanup()


def test_ignore_non_video():
    """忽略 subtitle / nfo / image"""
    from app.mirror.generator import generate_mirror
    from app.import_plan.models import ImportPlanItem

    items = [
        _make_video_item("v1"),
        ImportPlanItem(
            id="nfo1", plan_id="plan-test", raw_file_id="raw-nfo1",
            source="pan115", relative_path="test.nfo", real_path="H:\\test.nfo",
            resource_type="nfo", action="ignore",
        ),
    ]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.generated_count == 1
        assert len(result.items) == 1  # 只有 video item
    finally:
        _cleanup()


# ============================================================
# 分组目录和命名测试
# ============================================================

def test_season_directory_and_strm():
    """season 生成 Season 1 目录和 S01E01 strm"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1", season_number=1, episode_number=1)]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"
        assert result.generated_count == 1

        strm_path = Path(result.items[0].strm_path)
        assert "Season 1" in str(strm_path)
        assert "S01E01" in strm_path.name
        assert strm_path.exists()
    finally:
        _cleanup()


def test_sps_directory_and_strm():
    """sps 生成 SPs 目录，保留原始文件名"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1", group_type="special", season_number=None, episode_number=None, special_number=1,
                              relative_path="动画/test/[MAI] Test OVA 01 [BDRip].mkv")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"

        strm_path = Path(result.items[0].strm_path)
        assert "Season 0" in str(strm_path), f"应在 SPs 目录: {strm_path}"
        assert strm_path.name.endswith(".strm"), f"应以 .strm 结尾: {strm_path.name}"
        # 原始名称应保留（去掉技术标签后）
        assert "MAI" not in strm_path.name, f"字幕组标签应被清理: {strm_path.name}"
        assert "BDRip" not in strm_path.name, f"技术标签应被清理: {strm_path.name}"
    finally:
        _cleanup()


def test_op_ed_directory_and_strm():
    """OP/ED 已废弃：识别后跳过，不生成镜像"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item(
        "v1", group_type="ignored", season_number=None, episode_number=None,
        title="NCOP01",
        relative_path="动画/test/[MAI] Test NCOP01 [Ma10p_1080p].mkv",
    )]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"
        assert result.generated_count == 0
        assert result.skipped_count == 1
        assert result.items[0].status == "skipped"
        assert result.items[0].strm_path == ""
    finally:
        _cleanup()


def test_movie_strm():
    """movie 生成作品根目录下 movie strm"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item(
        "v1", group_type="movie", season_number=None, episode_number=None,
        year=2017,
    )]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"

        strm_path = Path(result.items[0].strm_path)
        # movie 不应有分组子目录
        assert "Season" not in str(strm_path)
        assert "Season 0" not in str(strm_path)
        assert "OP-ED" not in str(strm_path)
        assert "2017" in strm_path.name
    finally:
        _cleanup()


def test_standalone_movie_uses_specific_title_for_path():
    """系列电影镜像路径应使用具体电影标题，不只用系列名"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item(
        "v1", work_title="刀剑神域", group_type="movie",
        season_number=None, episode_number=None, year=2017,
        title="4.剧场版：序列之争.2017",
    )]
    items[0].card_type = "standalone"
    items[0].media_type = "movie"
    items[0].original_title = "4.剧场版：序列之争.2017"
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"
        strm_path = Path(result.items[0].strm_path)
        assert "剧场版：序列之争 (2017)" in str(strm_path)
        assert strm_path.name == "剧场版：序列之争 (2017).strm"
    finally:
        _cleanup()


# ============================================================
# .strm 内容测试
# ============================================================

def test_strm_content_equals_real_path():
    """.strm 内容等于 real_path"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        strm_path = Path(result.items[0].strm_path)
        content = strm_path.read_text(encoding="utf-8").strip()
        assert content == items[0].real_path
    finally:
        _cleanup()


# ============================================================
# target 字段回填测试
# ============================================================

def test_target_fields_filled():
    """成功后回填 target_dir / target_filename / target_strm_path"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        item = plan.items[0]
        assert item.target_dir != ""
        assert item.target_filename != ""
        assert item.target_strm_path != ""
        assert "S01E01" in item.target_filename
    finally:
        _cleanup()


# ============================================================
# 状态流转测试
# ============================================================

def test_status_remains_confirmed_on_success():
    """镜像成功后仍保留已确认计划，支持自动刮削和安全重试。"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"
        assert plan.status == "confirmed"
    finally:
        _cleanup()


def test_status_remains_confirmed_when_conflict_resolved():
    """同名冲突自动改名后，计划仍保持 confirmed。"""
    from app.mirror.generator import generate_mirror

    # 创建两个原本会冲突的 item（相同目标路径）
    items = [
        _make_video_item("v1", season_number=1, episode_number=1),
        _make_video_item("v2", season_number=1, episode_number=1),
    ]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"
        assert result.generated_count == 2
        assert result.failed_count == 0
        assert plan.status == "confirmed"
    finally:
        _cleanup()


# ============================================================
# 冲突处理测试
# ============================================================

def test_same_target_conflict_uses_unique_filenames():
    """同名目标路径冲突时保留全部条目并生成唯一文件名"""
    from app.mirror.generator import generate_mirror

    items = [
        _make_video_item("v1", season_number=1, episode_number=1),
        _make_video_item("v2", season_number=1, episode_number=1),
    ]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.failed_count == 0
        assert result.generated_count == 2
        paths = [it.strm_path for it in result.items if it.status == "generated"]
        assert len(paths) == 2
        assert len(set(paths)) == 2
        assert all(Path(p).exists() for p in paths)
    finally:
        _cleanup()


def test_existing_same_content_skipped():
    """已存在相同内容 strm 时跳过"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _cleanup()
    try:
        # 第一次生成
        result1 = generate_mirror(plan, str(_TEST_MIRROR))
        assert result1.generated_count == 1

        # 重新创建 plan（因为 status 已变为 executed）
        plan2 = _make_plan([_make_video_item("v1")])
        result2 = generate_mirror(plan2, str(_TEST_MIRROR))
        # 应该跳过（内容相同）
        assert result2.skipped_count == 1
    finally:
        _cleanup()


def test_existing_different_content_failed():
    """已存在不同内容 strm 时失败且不覆盖"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1")]
    plan = _make_plan(items)

    _cleanup()
    try:
        # 第一次生成
        result1 = generate_mirror(plan, str(_TEST_MIRROR))
        assert result1.generated_count == 1

        # 修改 real_path 后重新生成
        items2 = [_make_video_item("v1")]
        items2[0].real_path = "H:\\different\\path.mkv"
        plan2 = _make_plan(items2)
        result2 = generate_mirror(plan2, str(_TEST_MIRROR))
        assert result2.failed_count == 1
        assert any("内容不同" in e for e in result2.errors)
    finally:
        _cleanup()


def test_existing_broken_strm_is_repaired_when_new_target_is_reachable(tmp_path):
    """历史错误路径不可达而新路径已验证时，应修复生成文件而不是永久冲突。"""
    from app.mirror.generator import generate_mirror

    media_file = tmp_path / "百度网盘" / "01动画" / "新番" / "作品" / "S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")

    plan = _make_plan([_make_video_item("v1")])
    plan.items[0].real_path = str(media_file)

    _cleanup()
    try:
        result1 = generate_mirror(plan, str(_TEST_MIRROR))
        assert result1.generated_count == 1
        strm_path = Path(result1.items[0].strm_path)
        strm_path.write_text(str(tmp_path / "旧错误目录" / "不存在.mkv") + "\n", encoding="utf-8")

        repaired_plan = _make_plan([_make_video_item("v1")])
        repaired_plan.items[0].real_path = str(media_file)
        result2 = generate_mirror(repaired_plan, str(_TEST_MIRROR))

        assert result2.status == "success"
        assert result2.generated_count == 1
        assert result2.failed_count == 0
        assert "修复" in result2.items[0].message
        assert strm_path.read_text(encoding="utf-8").strip() == str(media_file)
    finally:
        _cleanup()


# ============================================================
# 路径安全测试
# ============================================================

def test_path_sanitized():
    """路径非法字符被清洗"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1", work_title="测试：作品*名")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.status == "success"
        strm_path = Path(result.items[0].strm_path)
        # 不应包含非法字符
        assert ":" not in strm_path.name
        assert "*" not in strm_path.name
    finally:
        _cleanup()


def test_path_traversal_sanitized():
    """路径遍历字符被 sanitize_filename 清洗"""
    from app.mirror.generator import build_target_for_item
    from app.import_plan.models import ImportPlanItem

    # sanitize_filename 会将 / 替换为 _，所以 ../evil 变成 .._evil
    item = ImportPlanItem(
        id="evil", plan_id="p", raw_file_id="r", source="pan115",
        relative_path="test.mkv", real_path="H:\\test.mkv",
        resource_type="video", action="generate_strm",
        work_title="../evil", group_type="movie",
    )
    root = _TEST_MIRROR
    target_path, _, _ = build_target_for_item(item, root, "115", {}, {})
    # 路径应在 mirror_root 下
    assert str(target_path).startswith(str(root)), f"路径越界: {target_path}"
    # 不应包含 .. 组件
    assert ".." not in target_path.parts, f"路径包含 ..: {target_path}"


def test_safe_join_rejects_traversal():
    """safe_join 直接拒绝路径遍历"""
    from app.core.paths import safe_join

    root = Path("/safe/mirror")
    try:
        safe_join(root, "..", "evil")
        assert False, "应抛出 ValueError"
    except ValueError:
        pass


def test_local_collection_spin_off_uses_own_work_directory():
    """本地合集里的独立子作品不能写进主系列 Season 1。"""
    from app.mirror.generator import build_target_for_item
    from app.import_plan.models import ImportPlanItem

    item = ImportPlanItem(
        id="heya-01",
        plan_id="p",
        raw_file_id="r",
        source="local",
        relative_path=(
            "[VCB-Studio] Yuru Camp/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [Ma10p_1080p]/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [01][Ma10p_1080p][x265_flac].mkv"
        ),
        real_path="D:\\media\\Heya Camp 01.mkv",
        resource_type="video",
        action="generate_strm",
        work_title="Heya Camp",
        series_group="Yuru Camp",
        card_type="standalone",
        belongs_to_series="Yuru Camp",
        relation_type="spin_off",
        group_type="season",
        season_number=1,
        episode_number=1,
        media_type="tv",
    )

    target_path, _, _ = build_target_for_item(item, _TEST_MIRROR, "local", {}, {})

    assert "Heya Camp" in target_path.parts
    assert target_path.parent.name == "Season 1"
    assert not (
        "Yuru Camp" in target_path.parts
        and target_path.parts[target_path.parts.index("Yuru Camp") + 1] == "Season 1"
    )


# ============================================================
# 稳定序号测试
# ============================================================

def test_fail_missing_work_title():
    """work_title 为空时 item failed，不生成 .strm"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1", work_title="")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.failed_count == 1
        assert result.generated_count == 0
        assert any("work_title" in e for e in result.errors)
    finally:
        _cleanup()


def test_fail_empty_group_type():
    """group_type 为空时 item failed"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1", group_type="")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.failed_count == 1
        assert any("group_type" in e for e in result.errors)
    finally:
        _cleanup()


def test_fail_invalid_group_type():
    """group_type 无效时 item failed"""
    from app.mirror.generator import generate_mirror

    items = [_make_video_item("v1", group_type="weird")]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.failed_count == 1
        assert any("group_type" in e for e in result.errors)
    finally:
        _cleanup()


def test_fail_season_missing_numbers():
    """season 缺 season_number 或 episode_number 时 item failed"""
    from app.mirror.generator import generate_mirror

    items = [
        _make_video_item("v1", season_number=None, episode_number=1),
        _make_video_item("v2", season_number=1, episode_number=None),
    ]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.failed_count == 2
        assert result.generated_count == 0
    finally:
        _cleanup()


def test_sps_stable_sequence():
    """SPs 保留原始文件名，不使用通用序号"""
    from app.mirror.generator import generate_mirror

    items = [
        _make_video_item("s1", group_type="special", season_number=None, episode_number=None,
                         special_number=None, relative_path="动画/test/[MAI] Test OVA 01.mkv"),
        _make_video_item("s2", group_type="special", season_number=None, episode_number=None,
                         special_number=None, relative_path="动画/test/[MAI] Test OVA 02.mkv"),
    ]
    plan = _make_plan(items)

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.generated_count == 2
        filenames = [Path(r.strm_path).name for r in result.items]
        # 应保留原始名称（清理后）
        assert any("OVA" in f for f in filenames), f"应保留 OVA: {filenames}"
    finally:
        _cleanup()


def test_movie_release_folder_title_is_cleaned_for_mirror_path():
    """standalone movie 镜像目录和文件名应使用干净电影标题。"""
    from app.mirror.generator import generate_mirror

    item = _make_video_item(
        "movie-yuru",
        work_title="Yuru Camp Movie",
        group_type="movie",
        season_number=None,
        episode_number=None,
        title="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]",
        relative_path=(
            "[VCB-Studio] Yuru Camp/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p][x265_flac].mkv"
        ),
    )
    item.work_id = "w-yuru-movie"
    item.card_type = "standalone"
    item.media_type = "movie"
    item.series_group = "Yuru Camp"
    item.year = None
    item.original_title = "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]"
    plan = _make_plan([item])

    _cleanup()
    try:
        result = generate_mirror(plan, str(_TEST_MIRROR))
        assert result.generated_count == 1
        assert Path(result.items[0].strm_path).parent.name == "Yuru Camp Movie"
        assert Path(result.items[0].strm_path).name == "Yuru Camp Movie.strm"
    finally:
        _cleanup()


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_reject_draft_plan,
        test_only_process_video_generate_strm,
        test_ignore_non_video,
        test_season_directory_and_strm,
        test_sps_directory_and_strm,
        test_op_ed_directory_and_strm,
        test_movie_strm,
        test_strm_content_equals_real_path,
        test_target_fields_filled,
        test_status_remains_confirmed_on_success,
        test_status_remains_confirmed_when_conflict_resolved,
        test_same_target_conflict_uses_unique_filenames,
        test_existing_same_content_skipped,
        test_existing_different_content_failed,
        test_path_sanitized,
        test_path_traversal_sanitized,
        test_safe_join_rejects_traversal,
        test_fail_missing_work_title,
        test_fail_empty_group_type,
        test_fail_invalid_group_type,
        test_fail_season_missing_numbers,
        test_sps_stable_sequence,
    ]
    passed = failed = 0
    for t in tests:
        try:
            _cleanup()
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        finally:
            _cleanup()
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
