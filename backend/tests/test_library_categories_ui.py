# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_seasonal_category_uses_tracking_state_before_import_scope():
    """显式追更状态优先，无状态时才回退到导入范围。"""
    source = (ROOT / "src" / "utils" / "libraryCategories.ts").read_text(encoding="utf-8")

    assert "trackingState === 'completed' || trackingState === 'archived'" in source
    assert "trackingState === 'tracking' || trackingState === 'paused'" in source
    assert "return work.import_scope === 'seasonal';" in source


def test_completed_cards_ignore_stale_tracking_badge():
    """非新番卡片即使收到旧 tracking 字段，也不能显示“追更中”。"""
    source = (ROOT / "src" / "components" / "library" / "PosterCard.tsx").read_text(encoding="utf-8")

    assert "const isSeasonalImport = work.import_scope === 'seasonal';" in source
    assert "isSeasonalImport\n            ? `追更中 · ${latestEpisodeLabel(work)}`" in source
    assert "work.tracking" not in source


def test_seasonal_toolbar_has_one_automatic_update_action_without_repair_detours():
    source = (ROOT / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")

    assert "全部状态" not in source
    assert "扫描更新当前" in source
    assert "保守扫描" not in source
    assert "重新自动处理" not in source
    assert "打开作品" not in source
    assert "查看处理详情" not in source
    assert "pickFolder" in source


def test_seasonal_scan_scope_is_intersection_of_visible_cards_and_bindings():
    source = (ROOT / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")

    assert "visibleSeasonalWorkIds" in source
    assert "visibleSeasonalWorkIds.has(binding.work_id)" in source
    assert "trackingApi.importRoot" in source


def test_seasonal_scan_notice_lists_updates_and_can_be_dismissed():
    source = (ROOT / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "formatTrackingScanNotice" in source
    assert 'aria-label="关闭扫描结果"' in source
    assert "onClick={() => setNotice('')}" in source
    assert ".seasonal-notice-close" in styles


def test_seasonal_source_picker_uses_distinct_choice_cards_and_contextual_path_copy():
    """来源必须是清晰的选项组，不能再表现成字段标题下的一行普通文本。"""
    source = (ROOT / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert 'className="seasonal-source-options"' in source
    assert 'role="radiogroup"' in source
    assert 'role="radio"' in source
    assert "本地文件夹" in source
    assert "115 挂载" in source
    assert "百度挂载" in source
    assert "sourcePathCopy" in source
    assert ".seasonal-source-option" in styles
    assert ".seasonal-source-option.is-selected" in styles


def test_seasonal_page_recovers_running_tracking_task_after_navigation():
    source = (ROOT / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")

    assert "recoverActiveTrackingTask" in source
    assert "type_prefix: 'tracking_'" in source
    assert "recoveredScanTaskIdRef" in source


def test_seasonal_add_dialog_can_close_and_reopen_with_persisted_task_progress():
    """批量导入必须在后台继续，弹窗关闭后可从后端任务记录恢复进度和日志。"""
    source = (ROOT / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    dialog = source.split("{addOpen &&", 1)[1].split("{categoryWorks.length", 1)[0]
    add_handler = source.split("const addTracking", 1)[1].split("const chooseTrackingFolder", 1)[0]

    assert "latestAddTask" in source
    assert "setLatestAddTask(active)" in source
    assert "tasksApi.get(task.task_id)" in add_handler
    assert "await waitForTask(task.task_id)" not in add_handler
    assert "!busy && setAddOpen(false)" not in dialog
    assert "disabled={busy}" not in dialog.split("</header>", 1)[0]
    assert "关闭到后台" in dialog
    assert "查看进度" in source
    assert "seasonal-task-panel ${addTaskVisualStatus}" in dialog
    assert 'className="seasonal-task-progress"' in dialog
    assert 'className="seasonal-task-log"' in dialog
    assert ".seasonal-dialog-layout" in styles
    assert ".seasonal-task-panel" in styles
    assert "width: min(1040px, 100%)" in styles


def test_seasonal_status_is_simple_and_detail_menu_has_no_state_management():
    poster = (ROOT / "src" / "components" / "library" / "PosterCard.tsx").read_text(encoding="utf-8")
    detail = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "追更中 · ${latestEpisodeLabel(work)}" in poster
    assert "trackingStateLabel" not in poster
    assert "updateTrackingState" not in detail
    assert "暂停追更" not in detail
    assert "恢复追更" not in detail
    assert "标记完结" not in detail


def test_detail_page_registers_native_video_drop_for_episode_preview():
    detail = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")
    drop_helper = (ROOT / "src" / "platform" / "fileDrop.ts")

    assert drop_helper.exists()
    helper_source = drop_helper.read_text(encoding="utf-8")
    assert "onDragDropEvent" in helper_source
    assert "listenForVideoFileDrop" in detail
    assert "previewDroppedEpisodes" in detail


def test_detail_append_status_stays_inside_append_dialog():
    """追加剧集的识别、失败和完成提示不能覆盖在详情页背景上。"""
    detail = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    preview_body = detail.split("const previewDroppedEpisodes", 1)[1].split("useEffect(() =>", 1)[0]
    commit_body = detail.split("const commitAppendEpisodes", 1)[1].split("const handlePlay", 1)[0]

    assert "appendNotice" in detail
    assert "setAppendNotice" in preview_body
    assert "setAppendNotice" in commit_body
    assert "setNotice(" not in preview_body
    assert "setNotice(" not in commit_body
    assert "const finishedTask = await waitForManagementTask" in commit_body
    assert "(message) => setAppendNotice" in commit_body
    assert "metadata_status === 'degraded'" in commit_body
    assert 'className={`append-dialog-status' in detail
    assert ".append-dialog-status" in styles


def test_episode_context_menu_is_dark_and_kept_inside_viewport():
    """详情页右键菜单应使用深色浮层，并在窗口边缘自适应向上展开。"""
    detail = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    handler = detail.split("const handleEpisodeContextMenu", 1)[1].split("const markEpisodeCompleted", 1)[0]
    menu_css = styles.split(".episode-context-menu {", 1)[1].split("@keyframes fadeIn", 1)[0]

    assert "anchor.bottom + menuGap + menuHeight" in handler
    assert "anchor.top - menuHeight" in handler
    assert "anchor.bottom + menuGap" in handler
    assert "Math.max(viewportGap" in handler
    assert "activeElement.blur()" in handler
    assert "episodeContextMenuRef.current?.querySelector" in detail
    assert "focus({ preventScroll: true })" in detail
    assert "z-index: 10050" in menu_css
    assert "width: 168px" in menu_css
    assert "background: rgba(17, 23, 31" in menu_css
    assert "color: #f4f7fb" in menu_css
    assert "color-mix(in srgb, var(--accent) 72%, white)" in menu_css


def test_detail_page_shows_all_actual_source_tags():
    detail = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "const workSources = normalizedWorkSources(work)" in detail
    assert "workSources.map((source)" in detail
    assert "workSourceLabel(source)" in detail
    assert "work.sources || []" in detail
    assert "work.source" in detail
    assert "pan115: '115 网盘'" in detail
    assert "baidu: '百度网盘'" in detail
    assert "local: '本地'" in detail
