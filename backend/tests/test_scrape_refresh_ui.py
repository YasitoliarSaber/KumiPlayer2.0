from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_auto_scrape_refreshes_library_after_workflow_task_succeeds():
    page = (ROOT / 'src/pages/MediaManagementPage.tsx').read_text(encoding='utf-8')

    assert 'useLibraryStore' in page
    assert 'isScrapeTask(task)' in page
    assert 'loadLibrary({ force: true })' in page


def test_app_globally_refreshes_library_when_background_scrape_finishes():
    app = (ROOT / 'src/App.tsx').read_text(encoding='utf-8')

    assert "tasksApi.list({ type_prefix: 'scrape_'" in app
    assert 'completedScrapeTaskIdsRef' in app
    assert "task.status === 'succeeded'" in app
    assert 'loadLibrary({ force: true })' in app


def test_app_refreshes_library_while_scrape_publishes_incremental_results():
    app = (ROOT / 'src/App.tsx').read_text(encoding='utf-8')

    assert 'scrapeLibraryRevisionByTaskRef' in app
    assert 'library_refresh_revision' in app
    assert 'publishedDuringScrape' in app


def test_scrape_execution_stage_displays_elapsed_time():
    workbench = (ROOT / 'src/components/media/MediaTaskWorkbench.tsx').read_text(encoding='utf-8')

    assert 'formatTaskElapsed' in workbench
    assert 'media-task-duration' in workbench
    assert '本次耗时' in workbench


def test_manual_detail_scrape_waits_then_refreshes_current_work():
    page = (ROOT / 'src/pages/WorkDetailPage.tsx').read_text(encoding='utf-8')

    assert 'await waitForManagementTask(task.task_id)' in page
    assert 'await reloadCurrentWork()' in page
    assert '刮削完成，作品信息已自动刷新' in page


def test_manual_detail_scrape_defaults_to_whole_work_and_keeps_season_scope():
    page = (ROOT / 'src/pages/WorkDetailPage.tsx').read_text(encoding='utf-8')
    api = (ROOT / 'src/api/scrape.ts').read_text(encoding='utf-8')

    assert "useState<'work' | 'season'>('work')" in page
    assert '整部作品' in page
    assert '当前季度' in page
    assert 'scrapeScope' in page
    assert "scope: 'work' | 'season'" in api
