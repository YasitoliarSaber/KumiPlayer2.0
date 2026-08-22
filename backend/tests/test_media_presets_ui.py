from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_media_management_exposes_preset_cards_and_native_file_picker():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    assert "media-preset-grid" in page
    assert "导入新版并安全比对" in page
    assert "选择目录树 TXT" in page
    assert "新建媒体库" not in page
    assert "pendingTreeActionRef" in page
    assert 'type="file"' in page
    assert "mediaPresetsApi.update" in page
    assert "mediaPresetsApi.deletePreview" in page
    assert "mediaPresetsApi.deleteConfirm" in page
    assert "删除导入卡片" in page
    assert "确认删除卡片" in page
    assert "镜像、NFO、图片、刮削结果、媒体库、追更和观看状态均会保留" in page
    assert "如需清理这些数据，请前往“媒体库维护”按来源操作" in page
    assert "彻底删除媒体库" not in page
    assert "镜像与元数据文件" not in page
    assert "播放与观看状态" not in page
    assert "按文件名前缀定位配置根下的媒体目录" not in page


def test_media_management_has_one_clear_cloud_import_entry():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    assert page.count("选择目录树 TXT") >= 2  # 115 与百度各自一个 TXT 导入主按钮
    assert "选择目录树</Button>" not in page
    assert "首次导入" in page
    assert "待处理导入" in page
    assert "已建立媒体库" in page
    assert "刮削完成后才会进入正式媒体库" in page


def test_media_preset_client_uses_multipart_upload():
    client = (ROOT / "src/api/mediaPresets.ts").read_text(encoding="utf-8")
    assert "new FormData()" in client
    assert "api.form" in client


def test_media_management_cards_expose_queue_progress_and_manual_review_entry():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    assert "media-preset-progress-ring" in page
    assert "排队等待" in page
    assert "处理刮削匹配" in page
    assert "scrapeApi.getReviewQueue" in page
    assert "scrapeApi.selectCandidate" in page
    assert "scrapeApi.skipReviewItem" in page


def test_running_scrape_remains_reachable_after_leaving_the_scrape_step():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")

    assert "isScrapeTask(task)" in page
    assert "preview?.status !== 'confirmed' && preview?.status !== 'executed'" in page
    assert "step === 'workbench'" in page
    assert "(preview || isScrapeTask(task))" in page
    assert "<MediaTaskWorkbench" in page
    assert "onCancel={activeTask ? () => void cancelTask() : undefined}" in page
    task_recovery = "if (target === 'workbench' && isScrapeTask(task)) return true;"
    assert task_recovery in page
    assert page.index(task_recovery) < page.index("if (!activeEntry?.planId) return false;")


def test_cancelled_scrape_is_presented_as_stopped_instead_of_failed():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    workbench = (ROOT / "src/components/media/MediaTaskWorkbench.tsx").read_text(encoding="utf-8")
    task_types = (ROOT / "src/api/types.ts").read_text(encoding="utf-8")
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert "'cancelled'" in task_types
    assert "刮削已停止" in page
    assert "上次刮削已停止" in page
    assert "media-flow-alert stopped" in workbench
    assert "task?.status === 'failed' && !isStopped" in workbench
    assert ".media-flow-alert.stopped" in css


def test_media_management_exposes_resolved_media_path_and_validation_state():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    assert "实际视频根目录" in page
    assert "路径验证失败" in page
    assert "pathValidation" in page


def test_baidu_seasonal_txt_import_uses_inferred_root_and_keeps_repair_fallback():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    client = (ROOT / "src/api/mediaPresets.ts").read_text(encoding="utf-8")

    assert "KumiPlayer 不会扫描百度挂载根目录来猜测路径" not in page
    # 新根路径合同：TXT 所在文件夹即实际媒体根，不再按文件名前缀猜测
    assert "TXT 导入会按文件名前缀从百度配置根定位" not in page
    assert "TXT 所在文件夹将作为实际媒体根目录" in page
    assert "setStep('confirm')" in page
    assert "选择实际文件夹并重新验证" in page
    assert "selectedCloudRoot" in page
    assert "mediaPresetsApi.rebindRoot" in page
    assert "sourceRoot: selectedCloudRoot" in page
    assert "/source-root" in client


def test_baidu_txt_import_passes_expected_source_and_uses_native_picker():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    client = (ROOT / "src/api/mediaPresets.ts").read_text(encoding="utf-8")
    picker = (ROOT / "src/platform/folderPicker.ts").read_text(encoding="utf-8")

    assert "expected_source" in client
    assert "createFromPath" in page
    assert "updateFromPath" in client
    assert "pickDirectoryTreeFile" in page
    assert "pickDirectoryTreeFile" in picker
    assert "打开百度网盘官网" in page
    assert "选择目录树 TXT" in page


def test_failed_preset_continue_revalidates_current_root_before_opening_plan():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    client = (ROOT / "src/api/mediaPresets.ts").read_text(encoding="utf-8")

    assert "mediaPresetsApi.revalidate" in page
    assert "重新验证并继续" in page
    assert "/revalidate" in client


def test_media_preset_cards_use_source_and_ordinal_pills_instead_of_redundant_titles():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert "getPresetOrdinalLabel" in page
    assert "media-preset-ordinal" in page
    assert "百度动画库" not in page
    assert "115动画库" not in page
    assert "media-preset-card-title > small { overflow: hidden; color: var(--text-secondary); font-size: 13px;" in css
    assert "media-preset-path > strong { overflow: hidden; font-size: 13px;" in css


def test_media_preset_cards_use_polished_command_card_visuals():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert "media-preset-action primary" in page
    assert "media-preset-action secondary" in page
    assert "media-preset-action repair" in page
    # 外层分区透明，不再包一层实体大卡；网格在宽屏自适应填满内容轴
    assert ".media-preset-section { display: grid; gap: 14px; margin-bottom: 22px; }" in css
    assert "grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr))" in css
    assert "minmax(min(100%, 368px), 400px)" not in css
    # 单张卡片才是唯一离散 surface：去装饰条、去 16px blur 与重投影
    card_rule = next(
        (line for line in css.splitlines() if line.startswith(".media-preset-card {")),
        "",
    )
    assert card_rule
    assert "backdrop-filter" not in card_rule
    assert "linear-gradient(180deg" not in card_rule
    assert ".media-preset-card::before" not in css
    assert ".media-preset-stats { display: flex; flex-wrap: wrap; gap: 7px;" in css
    assert ".media-preset-card-actions { display: grid;" in css
    assert "grid-template-columns: minmax(118px, .82fr) minmax(176px, 1.18fr)" in css
    assert ".media-preset-card-actions .media-preset-action.repair { grid-column: 1 / -1;" in css
    assert ".media-preset-card-actions > .fui-Button { width: 100%;" in css
    assert "white-space: nowrap;" in css
    assert "border-radius: 999px; font-weight: 700;" in css


def test_media_management_action_groups_keep_buttons_aligned_at_narrow_widths():
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert ".media-flow-page .fui-Button {" in css
    assert "display: inline-flex;" in css
    assert "align-items: center;" in css
    assert "justify-content: center;" in css
    assert "flex-wrap: nowrap;" in css
    assert "white-space: nowrap;" in css
    assert ".media-flow-page .fui-Button > .fui-Button__icon," in css
    assert ".media-flow-page .fui-Button > .fui-Spinner { flex: 0 0 auto; }" in css
    assert ".maintenance-card-actions," in css
    assert ".maintenance-danger-actions { display: flex; align-items: center; justify-content: flex-end;" in css
    assert ".maintenance-danger-actions { justify-content: stretch; }" in css
    assert ".maintenance-danger-actions .fui-Button," in css
    assert (
        ".media-edit-dialog footer { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap;"
        in css
    )
    assert ".media-edit-dialog footer .fui-Button { flex: 0 0 auto;" in css
    assert ".media-entry-actions { display: flex; flex-wrap: nowrap;" in css
    assert ".media-flow-footer { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap;" in css
    assert (
        ".media-stage-actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap;"
        in css
    )
    assert "@container media-flow (max-width: 420px)" in css
    assert ".media-preset-card-actions { grid-template-columns: 1fr; }" in css
    assert ".media-preset-card-actions .media-preset-action { grid-column: 1; }" in css
    assert "@media (max-width: 520px)" in css
    assert ".media-edit-dialog footer { align-items: stretch; flex-direction: column; }" in css
    assert ".media-edit-dialog footer .fui-Button { width: 100%; }" in css


def test_baidu_seasonal_import_offers_txt_and_metadata_only_folder_scan():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    client = (ROOT / "src/api/mediaPresets.ts").read_text(encoding="utf-8")

    assert "目录树 TXT 批量导入" in page
    assert "扫描新番真实文件夹" in page
    assert "TXT 所在文件夹将作为实际媒体根目录" in page
    assert "sourcesApi.createLocalImportBatch" in page
    assert "/scan-folder" in client
    assert "只读取名称、大小和修改时间" in page



def test_import_home_hides_preset_cards_while_import_setup_is_active():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")

    assert "importModeActive" in page
    assert "添加媒体目录" in page
    assert "返回媒体库" in page
    assert "!importModeActive" in page

def test_local_import_prefills_saved_local_media_root_without_overwriting_user_input():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")

    assert "configApi.getConfig" in page
    assert "config.local_root.trim()" in page
    assert "item.path.trim()" in page
    assert "path: savedLocalRoot" in page


def test_media_management_removes_scrape_precheck_and_keeps_maintenance_outside_import_flow():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    workbench = (ROOT / "src/components/media/MediaTaskWorkbench.tsx").read_text(encoding="utf-8")
    workflow_store = (ROOT / "src/stores/mediaWorkflow.ts").read_text(encoding="utf-8")
    scrape_client = (ROOT / "src/api/scrape.ts").read_text(encoding="utf-8")
    settings_page = (ROOT / "src/pages/SettingsPage.tsx").read_text(encoding="utf-8")

    assert "generated_count" in workbench
    assert "failed_count" in workbench
    assert "skipped_count" in workbench
    assert "items_count" in workbench
    assert "镜像已就绪" in workbench
    assert "isMirrorTaskReady" in page
    assert "刮削预检" not in page
    assert "'precheck'" not in page
    assert "media-flow-utilities" in (ROOT / "src/index.css").read_text(encoding="utf-8")
    assert "maintenance-nav-command" in page
    assert "'precheck'" not in workflow_store
    assert "dryRun" not in scrape_client
    assert "刮削预检" not in settings_page


def test_library_maintenance_reports_directory_tree_archives_removed_with_clear():
    panel = (ROOT / "src/components/media/LibraryMaintenancePanel.tsx").read_text(encoding="utf-8")
    client = (ROOT / "src/api/library.ts").read_text(encoding="utf-8")

    assert "media_preset_count" in panel
    assert "deleted_preset_ids" in panel
    assert "目录树导入档案" in panel
    assert "media_preset_count" in client
    assert "deleted_preset_ids" in client
    assert "onCleared" in panel
    assert "loadPresets(true)" in (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")


def test_both_netdisk_sources_show_official_entry_and_txt_picker_only():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")
    picker = (ROOT / "src/platform/folderPicker.ts").read_text(encoding="utf-8")

    # 115 与百度来源都有官网入口 + 选择目录树 TXT 的同一工作流
    assert "前往 115 官网生成目录树" in page
    assert "打开百度网盘官网" in page
    assert "选择目录树 TXT" in page

    # 不再出现 API 扫描 / 按文件名前缀猜根等过时文案
    assert "API 扫描" not in page
    assert "按文件名前缀定位配置根" not in page

    # 原生选择器仅显示 txt，与绝对路径后端合同一致
    assert "extensions: ['txt']" in picker
    assert "'txt', 'tree', 'log'" not in picker


def test_update_action_uses_target_preset_source_and_root():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")

    # 更新选择器使用目标预设自身的 source/source_root，而不是页面全局来源或配置根
    assert "presetSource: preset.source" in page
    assert "presetSourceRoot: preset.source_root" in page
    assert "updateFromPath(action.presetId, treePath, action.presetSource)" in page
    # 默认打开预设 source_root，而不是回退到另一网盘的配置根
    assert "if (action.kind === 'update') return action.presetSourceRoot;" in page


def test_preset_update_does_not_short_circuit_on_global_local_source():
    page = (ROOT / "src/pages/MediaManagementPage.tsx").read_text(encoding="utf-8")

    # 更新导入不能依赖页面全局 source；只有“创建 + 全局 local”才应短路。
    assert "if (action.kind === 'create' && source === 'local') return;" in page
    # 更新仍必须按目标预设 source 调用 updateFromPath
    assert "updateFromPath(action.presetId, treePath, action.presetSource)" in page
    # 全局 source=local 时不应整体 return（否则无法更新已导入的 115/百度预设）
    assert "action.kind === 'create' && source === 'local'" in page
    assert "if (source === 'local') return;" not in page
    # 更新动作仍传目标预设根目录给选择器
    assert "presetSourceRoot: preset.source_root" in page
