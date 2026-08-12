"""后端数据流 V2（SQLite v3）核心表结构。

新架构固定使用 ``CURRENT_SCHEMA_VERSION = 3``，并在 ``app_meta`` 写入
``backend_data_epoch = 2``；这里的“后端 V2”是数据架构代次，不与旧 OpenList
schema v2 混用。

事实边界：
- Source Catalog（sources / source_roots / source_directories / source_nodes
  / source_stage_entries）只保存物理事实，不写作品/季度/TMDB 判断；
- Import Revision（import_revisions / import_revision_items）确认后不可变；
- jobs / job_attempts 是持久任务事实，不依赖内存队列；
- STRM/NFO/图片/LibraryIndex 均为可重建投影。

安全：本模块不保存密码、Token、Authorization、直链或 OpenList 内部存储路径。
"""

SCHEMA_V3_TABLES: tuple[str, ...] = (
    # ---- 元数据 ----
    """
    CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # ---- 来源域 ----
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,          -- local / txt / openlist / webdav
        provider_id TEXT NOT NULL DEFAULT '',
        ingest_method TEXT NOT NULL DEFAULT '',
        connection_key TEXT NOT NULL DEFAULT '',
        capabilities_json TEXT NOT NULL DEFAULT '{}',
        display_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_roots (
        root_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
        remote_locator TEXT NOT NULL DEFAULT '',
        normalized_locator TEXT NOT NULL DEFAULT '',
        local_locator TEXT NOT NULL DEFAULT '',
        import_family TEXT NOT NULL DEFAULT 'anime',
        import_scope TEXT NOT NULL DEFAULT '',
        scan_policy TEXT NOT NULL DEFAULT 'standard',
        active_generation INTEGER NOT NULL DEFAULT 0,
        last_successful_scan_at TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(source_id, normalized_locator)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_batches (
        batch_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'pending',
        mode TEXT NOT NULL DEFAULT 'auto_safe',
        import_family TEXT NOT NULL DEFAULT 'anime',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_batch_roots (
        batch_id TEXT NOT NULL REFERENCES import_batches(batch_id) ON DELETE CASCADE,
        root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        generation INTEGER NOT NULL DEFAULT 0,
        error_kind TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (batch_id, root_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_runs (
        run_id TEXT PRIMARY KEY,
        root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
        generation INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'full',
        status TEXT NOT NULL DEFAULT 'queued',
        started_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT '',
        error TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_directories (
        root_id TEXT NOT NULL,
        remote_path TEXT NOT NULL,
        parent_path TEXT NOT NULL DEFAULT '',
        depth INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'queued',
        accepted_generation INTEGER NOT NULL DEFAULT 0,
        entry_count INTEGER NOT NULL DEFAULT 0,
        member_hash TEXT NOT NULL DEFAULT '',
        last_verified_at TEXT DEFAULT '',
        next_verify_at TEXT DEFAULT '',
        retry_count INTEGER NOT NULL DEFAULT 0,
        last_error_kind TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (root_id, remote_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_nodes (
        root_id TEXT NOT NULL,
        remote_path TEXT NOT NULL,
        parent_path TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'file',   -- file / dir
        size INTEGER,
        mtime REAL,
        etag TEXT DEFAULT '',
        content_hash TEXT DEFAULT '',
        remote_id TEXT DEFAULT '',
        logical_locator TEXT DEFAULT '',
        provider_id TEXT DEFAULT '',
        route_id TEXT DEFAULT '',
        first_seen_generation INTEGER NOT NULL DEFAULT 0,
        last_seen_generation INTEGER NOT NULL DEFAULT 0,
        tombstone TEXT DEFAULT '',
        PRIMARY KEY (root_id, remote_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_stage_entries (
        run_id TEXT NOT NULL,
        directory_path TEXT NOT NULL,
        remote_path TEXT NOT NULL,
        page INTEGER NOT NULL DEFAULT 0,
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'file',
        size INTEGER,
        mtime REAL,
        logical_locator TEXT DEFAULT '',
        PRIMARY KEY (run_id, remote_path)
    )
    """,
    # ---- 作品与执行域 ----
    """
    CREATE TABLE IF NOT EXISTS media_units (
        unit_id TEXT PRIMARY KEY,
        batch_id TEXT DEFAULT '',
        root_id TEXT NOT NULL,
        discovery_scope TEXT NOT NULL DEFAULT '',
        boundary TEXT NOT NULL DEFAULT '',
        work_key TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'discovered',
        closure_generation INTEGER NOT NULL DEFAULT 0,
        current_revision_id TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_revisions (
        revision_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES media_units(unit_id) ON DELETE CASCADE,
        parent_revision_id TEXT DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        provider_id TEXT NOT NULL DEFAULT '',
        source_generation INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'draft',
        hash TEXT NOT NULL DEFAULT '',
        confirm_method TEXT NOT NULL DEFAULT '',
        confirmed_at TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_revision_items (
        revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
        item_id TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        provider_id TEXT NOT NULL DEFAULT '',
        relative_path TEXT NOT NULL DEFAULT '',
        real_path TEXT NOT NULL DEFAULT '',
        logical_locator TEXT NOT NULL DEFAULT '',
        resource_type TEXT NOT NULL DEFAULT 'other',
        action TEXT NOT NULL DEFAULT 'ignore',
        work_id TEXT NOT NULL DEFAULT '',
        work_title TEXT NOT NULL DEFAULT '',
        original_title TEXT NOT NULL DEFAULT '',
        year INTEGER,
        media_type TEXT NOT NULL DEFAULT '',
        show_type TEXT NOT NULL DEFAULT '',
        series_group TEXT NOT NULL DEFAULT '',
        card_type TEXT NOT NULL DEFAULT '',
        belongs_to_series TEXT NOT NULL DEFAULT '',
        relation_type TEXT NOT NULL DEFAULT '',
        group_type TEXT NOT NULL DEFAULT '',
        season_number INTEGER,
        episode_number INTEGER,
        special_number INTEGER,
        title TEXT NOT NULL DEFAULT '',
        target_dir TEXT NOT NULL DEFAULT '',
        target_strm_path TEXT NOT NULL DEFAULT '',
        confidence TEXT NOT NULL DEFAULT 'medium',
        needs_review INTEGER NOT NULL DEFAULT 0,
        override_json TEXT NOT NULL DEFAULT '{}',
        warnings_json TEXT NOT NULL DEFAULT '[]',
        reasons_json TEXT NOT NULL DEFAULT '[]',
        user_override_id TEXT NOT NULL DEFAULT '',
        availability TEXT NOT NULL DEFAULT 'available',
        PRIMARY KEY (revision_id, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_libraries (
        library_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        root_id TEXT NOT NULL,
        remote_locator TEXT NOT NULL DEFAULT '',
        import_family TEXT NOT NULL DEFAULT 'anime',
        import_scope TEXT NOT NULL DEFAULT '',
        current_revision_id TEXT DEFAULT '',
        lifecycle_status TEXT NOT NULL DEFAULT 'draft',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ---- 任务与刮削域 ----
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        job_type TEXT NOT NULL,
        resource_key TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'queued',
        priority INTEGER NOT NULL DEFAULT 0,
        parent_job_id TEXT DEFAULT '',
        attempt INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        not_before TEXT DEFAULT '',
        lease_owner TEXT DEFAULT '',
        lease_until TEXT DEFAULT '',
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        progress INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT '',
        error TEXT DEFAULT '',
        version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_attempts (
        attempt_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL DEFAULT 0,
        started_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT '',
        error_type TEXT DEFAULT '',
        retryable INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scrape_bindings (
        binding_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL,
        work_id TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        provider_id TEXT NOT NULL DEFAULT '',
        tmdb_id INTEGER,
        tmdb_type TEXT DEFAULT '',
        bangumi_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        nfo_path TEXT DEFAULT '',
        poster_path TEXT DEFAULT '',
        fanart_path TEXT DEFAULT '',
        clearlogo_path TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scrape_reviews (
        review_id TEXT PRIMARY KEY,
        binding_id TEXT NOT NULL,
        local_title TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        candidates_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending',
        added_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scrape_failures (
        failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
        binding_id TEXT DEFAULT '',
        error TEXT DEFAULT '',
        stage TEXT DEFAULT '',
        retryable INTEGER NOT NULL DEFAULT 0,
        timestamp TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_records (
        artifact_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,                 -- strm / nfo / poster / fanart / clearlogo
        path TEXT NOT NULL,
        revision_id TEXT DEFAULT '',
        work_id TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(kind, path)
    )
    """,
)

SCHEMA_V3_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_source_roots_source ON source_roots(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_roots_batch ON import_batch_roots(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_scan_runs_root ON scan_runs(root_id)",
    "CREATE INDEX IF NOT EXISTS idx_source_dirs_root_state ON source_directories(root_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_source_nodes_parent ON source_nodes(root_id, parent_path)",
    "CREATE INDEX IF NOT EXISTS idx_source_nodes_root_gen ON source_nodes(root_id, last_seen_generation)",
    "CREATE INDEX IF NOT EXISTS idx_stage_run ON source_stage_entries(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_units_root ON media_units(root_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_revisions_unit ON import_revisions(unit_id)",
    "CREATE INDEX IF NOT EXISTS idx_revision_items_rev ON import_revision_items(revision_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_resource ON jobs(resource_key, status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_job_id)",
    "CREATE INDEX IF NOT EXISTS idx_job_attempts_job ON job_attempts(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_scrape_bindings_rev ON scrape_bindings(revision_id)",
    "CREATE INDEX IF NOT EXISTS idx_scrape_bindings_work ON scrape_bindings(work_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_rev ON artifact_records(revision_id)",
)

#: 旧通用表（任务/播放/刮削队列/追更），在任务 2-5 迁移完成前继续供旧功能使用。
LEGACY_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        progress INTEGER DEFAULT 0,
        message TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        error TEXT,
        result TEXT DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playback_history (
        history_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        work_title TEXT DEFAULT '',
        episode_id TEXT DEFAULT '',
        episode_title TEXT DEFAULT '',
        source TEXT DEFAULT '',
        media_type TEXT DEFAULT '',
        group_type TEXT DEFAULT '',
        season_number INTEGER DEFAULT 0,
        episode_number INTEGER DEFAULT 0,
        strm_path TEXT DEFAULT '',
        poster_path TEXT DEFAULT '',
        played_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scrape_candidate_cache (
        cache_id TEXT PRIMARY KEY,
        scrape_target_id TEXT NOT NULL,
        provider TEXT DEFAULT 'tmdb',
        tmdb_id INTEGER NOT NULL,
        tmdb_type TEXT DEFAULT '',
        title TEXT DEFAULT '',
        original_title TEXT DEFAULT '',
        year INTEGER,
        overview TEXT DEFAULT '',
        poster_path TEXT DEFAULT '',
        popularity REAL DEFAULT 0,
        vote_average REAL DEFAULT 0,
        score REAL DEFAULT 0,
        reasons TEXT DEFAULT '[]',
        cached_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scrape_review_queue (
        scrape_target_id TEXT PRIMARY KEY,
        source TEXT DEFAULT '',
        series_group TEXT DEFAULT '',
        local_title TEXT DEFAULT '',
        scrape_title TEXT DEFAULT '',
        scrape_year INTEGER,
        scrape_type TEXT DEFAULT '',
        local_season_number INTEGER,
        reason TEXT DEFAULT '',
        candidates TEXT DEFAULT '[]',
        added_at TEXT NOT NULL,
        status TEXT DEFAULT 'pending'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS failed_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scrape_target_id TEXT DEFAULT '',
        tmdb_id INTEGER,
        tmdb_type TEXT DEFAULT '',
        error TEXT DEFAULT '',
        stage TEXT DEFAULT '',
        timestamp TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracking_bindings (
        binding_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        display_title TEXT DEFAULT '',
        logical_source TEXT NOT NULL DEFAULT 'local',
        root_path TEXT NOT NULL,
        import_family TEXT NOT NULL DEFAULT 'anime',
        season_number INTEGER,
        series_group TEXT DEFAULT '',
        tracking_state TEXT NOT NULL DEFAULT 'tracking',
        attention_state TEXT NOT NULL DEFAULT 'ready',
        last_snapshot_id TEXT DEFAULT '',
        baseline_plan_id TEXT DEFAULT '',
        last_scan_at TEXT DEFAULT '',
        last_successful_scan_at TEXT DEFAULT '',
        last_result TEXT DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(work_id, root_path, season_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracking_scan_runs (
        scan_id TEXT PRIMARY KEY,
        binding_id TEXT NOT NULL,
        work_id TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT DEFAULT '',
        result TEXT DEFAULT '{}',
        error TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_overrides (
        work_id TEXT PRIMARY KEY,
        poster_path TEXT DEFAULT '',
        fanart_path TEXT DEFAULT '',
        clearlogo_path TEXT DEFAULT '',
        metadata TEXT DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
)

LEGACY_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type)",
    "CREATE INDEX IF NOT EXISTS idx_history_work ON playback_history(work_id)",
    "CREATE INDEX IF NOT EXISTS idx_history_played ON playback_history(played_at)",
    "CREATE INDEX IF NOT EXISTS idx_cache_target ON scrape_candidate_cache(scrape_target_id)",
    "CREATE INDEX IF NOT EXISTS idx_review_status ON scrape_review_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_failed_timestamp ON failed_cases(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_tracking_state ON tracking_bindings(tracking_state)",
    "CREATE INDEX IF NOT EXISTS idx_tracking_scan_work ON tracking_scan_runs(work_id)",
)


def create_schema_v3(conn) -> None:
    """在既有连接上创建 V2 架构全部表与索引（幂等，须由调用方置于事务内）。"""
    for ddl in SCHEMA_V3_TABLES:
        conn.execute(ddl)
    for ddl in SCHEMA_V3_INDEXES:
        conn.execute(ddl)
    for ddl in LEGACY_TABLES:
        conn.execute(ddl)
    for ddl in LEGACY_INDEXES:
        conn.execute(ddl)
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('backend_data_epoch', '2')"
    )
    # 轻量扩展表（source_health 等）统一由 ensure_* 幂等补齐
    ensure_source_health_table(conn)


# 轻量幂等扩展表（v3 之后的补充表，不 bump user_version）
_EXTRA_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS source_health (
        source_id TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'healthy',
        reason_kind TEXT NOT NULL DEFAULT '',
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        cooldown_until REAL NOT NULL DEFAULT 0,
        last_failure_at REAL NOT NULL DEFAULT 0,
        last_success_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_health_state ON source_health(state)",
)


def ensure_source_health_table(conn) -> None:
    """幂等补齐 source_health 表（已有 v3 库的轻量迁移，不重置数据库）。"""
    for ddl in _EXTRA_TABLES:
        conn.execute(ddl)
