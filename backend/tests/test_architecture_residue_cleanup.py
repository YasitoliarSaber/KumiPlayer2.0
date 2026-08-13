import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


DEAD_FRONTEND_MODULES = [
    "src/components/ui/animated-number.tsx",
    "src/components/ui/badge.tsx",
    "src/components/ui/button.tsx",
    "src/components/ui/card.tsx",
    "src/components/ui/dialog.tsx",
    "src/components/ui/dropdown-menu.tsx",
    "src/components/ui/gradient-heading.tsx",
    "src/components/ui/scroll-area.tsx",
    "src/components/ui/separator.tsx",
    "src/components/ui/skeleton.tsx",
    "src/components/ui/tabs.tsx",
    "src/components/ui/texture-button.tsx",
    "src/components/ui/tooltip.tsx",
    "src/design/ui-kit.ts",
    "src/lib/utils.ts",
    "src/stores/player.ts",
    "src/utils/genreTags.ts",
]


def test_unreachable_frontend_residue_is_removed():
    remaining = [path for path in DEAD_FRONTEND_MODULES if (ROOT / path).exists()]
    assert remaining == []


def test_package_manifest_only_keeps_reachable_runtime_dependencies():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    all_dependencies = set(package["dependencies"]) | set(package["devDependencies"])
    unused = {
        "@radix-ui/react-dialog",
        "@radix-ui/react-slot",
        "@tailwindcss/postcss",
        "class-variance-authority",
        "clsx",
        "motion",
        "oxlint",
        "radix-ui",
        "react-router-dom",
        "tailwind-merge",
    }
    assert all_dependencies.isdisjoint(unused)
    assert set(package["scripts"]) == {
        "build",
        "check:versions",
        "dev",
        "test",
        "test:components",
        "test:contracts",
        "tauri",
    }


def test_tauri_dev_entry_is_controlled_local_vite():
    """`npm run tauri dev` 必须启动受控本机 Vite 开发服务，而非开放浏览器运行模式。"""
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    tauri = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    vite = (ROOT / "vite.config.ts").read_text(encoding="utf-8")

    # package.json：存在固定端口的 dev 脚本，且不引入浏览器 preview 入口。
    assert "dev" in package["scripts"]
    assert re.search(r"--host\s+127\.0\.0\.1", package["scripts"]["dev"])
    assert re.search(r"--port\s+1420", package["scripts"]["dev"])
    assert "--strictPort" in package["scripts"]["dev"]
    assert "preview" not in package["scripts"]

    # tauri.conf.json：devUrl 仅给 tauri dev 使用；frontendDist 仍是生产资源来源。
    build = tauri["build"]
    assert build["frontendDist"] == "../dist"
    assert build["beforeBuildCommand"] == "npm run build"
    assert build["beforeDevCommand"] == "npm run dev"
    assert build["devUrl"] == "http://127.0.0.1:1420"

    # vite.config.ts：仅监听本机 1420，strictPort 占用即失败，不悄悄换端口。
    assert "TAURI_DEV_HOST" not in vite
    assert re.search(r"host:\s*[\"']127\.0\.0\.1[\"']", vite)
    assert re.search(r"port:\s*1420", vite)
    assert re.search(r"strictPort:\s*true", vite)

    # /api 走 HTTP 代理、/ws 走 WebSocket 代理，都透明转发到本机后端 37821。
    assert re.search(r"[\"']/api[\"']\s*:\s*\{", vite)
    assert "http://127.0.0.1:37821" in vite
    assert re.search(r"[\"']/ws[\"']\s*:\s*\{", vite)
    assert re.search(r"target:\s*[\"']ws://127\.0\.0\.1:37821[\"']", vite)
    assert re.search(r"ws:\s*true", vite)

    # 不开放移动端泛化配置，不残留已退役的旁路脚本。
    assert not (ROOT / "scripts/build/build_tauri.sh").exists()
    assert not (ROOT / "scripts/dev").exists()
    assert not (ROOT / "scripts/fix-codex-powershell.cmd").exists()
    assert not (ROOT / "scripts/fix-codex-powershell.ps1").exists()


def test_legacy_autoplay_helper_is_removed_but_current_queue_remains():
    service = (ROOT / "backend/app/playback/service.py").read_text(encoding="utf-8")
    assert not (ROOT / "backend/app/playback/autoplay.py").exists()
    assert not (ROOT / "backend/tests/test_playback_autoplay.py").exists()
    assert "def _build_playback_queue(" in service
    # 播放队列在启动前解析为真实媒体路径（PROJECT_MEMORY：MPV 只接收真实路径/URL）。
    assert "playlist_paths=media_targets" in service
    assert "_read_strm_target(queued_episode.strm_path)" in service


def test_cache_cleanup_covers_generated_residue_with_workspace_guards():
    cleanup = (ROOT / "scripts/cleanup_build_cache.ps1").read_text(encoding="utf-8")
    assert "Assert-WorkspacePath" in cleanup
    assert "__pycache__" in cleanup
    assert "src-tauri\\gen" in cleanup
    assert "*.tsbuildinfo" in cleanup
    assert "src-tauri\\icons\\android" in cleanup
    assert "src-tauri\\icons\\ios" in cleanup


def test_retired_preserve_seasonal_cleanup_chain_is_removed():
    paths = [
        "src/api/library.ts",
        "src/components/media/LibraryMaintenancePanel.tsx",
        "backend/app/api/library.py",
        "backend/app/library/delete.py",
        "backend/app/media_presets/store.py",
    ]
    residue = {
        path: token
        for path in paths
        for token in ("preserve_seasonal", "preserveSeasonal", "library_preserve_seasonal")
        if token in (ROOT / path).read_text(encoding="utf-8")
    }
    assert residue == {}


def test_library_api_only_keeps_current_delete_and_detail_routes():
    frontend = (ROOT / "src/api/library.ts").read_text(encoding="utf-8")
    backend = (ROOT / "backend/app/api/library.py").read_text(encoding="utf-8")

    assert "deletePreview:" not in frontend
    assert "deleteConfirm:" not in frontend
    assert re.search(r"^\s+rescan:\s", frontend, re.MULTILINE) is None
    assert re.search(r'@router\.get\("/works/\{work_id\}"\)', backend)
    assert '@router.get("/work/{work_id}")' not in backend
    assert '@router.get("/detail/{work_id}")' not in backend
    assert '@router.get("/{work_id}")' not in backend
    assert '@router.post("/delete/preview")' not in backend
    assert '@router.post("/delete/confirm")' not in backend


def test_large_library_surfaces_use_zustand_selectors():
    paths = [
        "src/App.tsx",
        "src/pages/HomePage.tsx",
        "src/pages/CategoryPage.tsx",
        "src/pages/SearchPage.tsx",
        "src/pages/RecentPage.tsx",
        "src/pages/FavoritesPage.tsx",
        "src/pages/WorkDetailPage.tsx",
        "src/pages/SettingsPage.tsx",
        "src/components/shell/Sidebar.tsx",
    ]
    offenders = [
        path
        for path in paths
        if re.search(r"useLibraryStore\(\s*\)", (ROOT / path).read_text(encoding="utf-8"))
    ]
    assert offenders == []


# ============================================================
# 模块4 C2：OpenList 旧递归导入主路径退役
# ============================================================

def test_openlist_legacy_recursive_import_chain_is_removed():
    """旧递归导入链（scan_openlist_preset / scan_remote_tree / legacy API 路由）已退役。"""
    # 旧链专用模块与测试已删除
    assert not (ROOT / "backend/app/integrations/openlist/scan.py").exists()
    assert not (ROOT / "backend/tests/test_openlist_scan.py").exists()

    # openlist normal API 不得引用旧递归链符号（scan / manifest 写路径）
    openlist_api = (ROOT / "backend/app/api/openlist.py").read_text(encoding="utf-8")
    # 排除保留的新链函数名 rescan_openlist_preset（其名字含子串 scan_openlist_preset）
    api_without_rescan = openlist_api.replace("rescan_openlist_preset", "")
    assert "scan_openlist_preset" not in api_without_rescan
    assert "scan_remote_tree" not in openlist_api
    assert "openlist.scan" not in openlist_api
    assert "import openlist.manifest" not in openlist_api
    # legacy /import 与 /batch-import 路由已删除（rescan 与 durable import-batch 保留）
    assert '@router.post("/import")' not in openlist_api
    assert '@router.post("/batch-import")' not in openlist_api
    assert '@router.post("/import-batch")' in openlist_api
    assert '@router.post("/presets/{preset_id}/rescan")' in openlist_api

    # media_presets.service 不再保留旧链实现
    service = (ROOT / "backend/app/media_presets/service.py").read_text(encoding="utf-8")
    assert "def scan_openlist_preset(" not in service
    assert "_discard_openlist_manifest" not in service
    assert "_openlist_manifest_size" not in service

    # manifest.py 只保留只读接口（历史清单仍需被 sources/openlist 读取）
    manifest = (ROOT / "backend/app/integrations/openlist/manifest.py").read_text(encoding="utf-8")
    assert "def read_manifest(" in manifest
    assert "def write_manifest(" not in manifest
    assert "canonical_sha256" not in manifest

    # 前端不再暴露旧导入入口
    frontend = (ROOT / "src/api/openlist.ts").read_text(encoding="utf-8")
    assert "importRemote" not in frontend
    assert "batchImport" not in frontend
    assert "'/api/openlist/import'" not in frontend


def test_openlist_shared_scanner_and_raw_snapshot_stay():
    """新链共用件与 RawSnapshot 必须保留（防误删护栏）。"""
    # Source Catalog 扫描器仍使用 OpenListDirectoryScanner（新链真实 runtime caller）
    catalog = (ROOT / "backend/app/catalog/scanner.py").read_text(encoding="utf-8")
    assert "OpenListDirectoryScanner" in catalog
    assert (ROOT / "backend/app/integrations/openlist/scanner.py").exists()

    # sources/openlist 仍需读取 legacy 清单
    sources = (ROOT / "backend/app/sources/openlist.py").read_text(encoding="utf-8")
    assert "read_manifest" in sources

    # RawSnapshot / raw store 是 115/百度/local 兼容路径的共用件，不允许删除
    assert (ROOT / "backend/app/raw/models.py").exists()
    assert (ROOT / "backend/app/raw/store.py").exists()


def test_v3_projection_dataflow_gate():
    """Module 5 数据流门禁：durable 执行不得回接 legacy JSON 事实源。"""
    handlers = (ROOT / "backend/app/pipeline/handlers.py").read_text(encoding="utf-8")
    library_handler = (ROOT / "backend/app/pipeline/library_handler.py").read_text(encoding="utf-8")
    generator = (ROOT / "backend/app/mirror/generator.py").read_text(encoding="utf-8")
    revision_store_src = (ROOT / "backend/app/import_plan/revision_store.py").read_text(encoding="utf-8")
    effective_store = (ROOT / "backend/app/scrape/effective_store.py").read_text(encoding="utf-8")
    library_store = (ROOT / "backend/app/library/store.py").read_text(encoding="utf-8")
    projection = (ROOT / "backend/app/library/projection.py").read_text(encoding="utf-8")

    # durable mirror handler：V3 不回写 legacy ImportPlan JSON
    assert "persist_plan=False" in handlers
    assert "persist_plan: bool = True" in generator
    assert "save_import_plan" not in handlers
    # durable library handler：不调用 load_latest_confirmed_import_plan、不读 JSON scrape_map
    assert "load_latest_confirmed_import_plan" not in library_handler
    assert "load_scrape_map(" not in library_handler  # 只读 JSON 的旧入口不得出现在 projection handler
    assert "list_current_revisions" in library_handler
    # current revision 权威查询基于 media_units.current_revision_id
    assert "u.current_revision_id" in revision_store_src
    # V3 scrape binding 稳定 identity（binding_id 严格 = scrape_target_id，不 fallback work_id）
    assert "binding_id = item.scrape_target_id" in effective_store
    assert "or item.work_id" not in effective_store
    assert "ON CONFLICT (binding_id) DO UPDATE" in effective_store
    # provider_id 从当前 import_revisions 事实填入
    assert "SELECT provider_id FROM import_revisions WHERE revision_id = ?" in effective_store
    # durable handler 对 stale/superseded job 的 current-revision execution fence
    assert "is_current_revision" in handlers
    # current revision 权威门禁查询
    assert "def is_current_revision(" in revision_store_src
    # artifact upsert 同 path 重写时 attribution 切到当前 revision
    artifacts_store = (ROOT / "backend/app/pipeline/artifacts.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (kind, path) DO UPDATE" in artifacts_store
    # LibraryIndex 保留为 projection（store 读写仍在）
    assert "def save_library_index" in library_store
    assert "def load_library_index" in library_store
