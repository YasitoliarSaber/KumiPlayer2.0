"""V4 唯一物理数据库结构。

V4 不通过启动时逐列 ALTER 隐式补丁升级。结构变化必须提升 user_version；
同一代内旧库仍只允许一次性重置，只有明确的 v4→v5 增量迁移例外。
"""

from __future__ import annotations

import sqlite3

V4_SCHEMA_VERSION = 10


def create_schema_v4(conn: sqlite3.Connection) -> None:
    """在调用方事务中创建完整 V4 表结构。"""

    statements = (
        """
        CREATE TABLE v4_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE source_health (
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
        "CREATE INDEX idx_v4_source_health_state ON source_health(state)",
        """
        CREATE TABLE openlist_telemetry (
            conn_hash TEXT NOT NULL,
            day TEXT NOT NULL,
            operation TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (conn_hash, day, operation)
        )
        """,
        """
        CREATE TABLE source_roots (
            root_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            ingest_method TEXT NOT NULL,
            source_locator TEXT NOT NULL DEFAULT '',
            playback_locator TEXT NOT NULL DEFAULT '',
            route_id TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            source_mode TEXT NOT NULL DEFAULT '',
            last_scan_mode TEXT NOT NULL DEFAULT '',
            retired_at TEXT NOT NULL DEFAULT '',
            retired_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE source_scans (
            scan_id TEXT PRIMARY KEY,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            UNIQUE(root_id, generation)
        )
        """,
        """
        CREATE TABLE source_evidence (
            evidence_id TEXT PRIMARY KEY,
            scan_id TEXT NOT NULL REFERENCES source_scans(scan_id) ON DELETE CASCADE,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            provider TEXT NOT NULL DEFAULT '',
            source_key TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            entry_kind TEXT NOT NULL,
            size INTEGER,
            mtime REAL,
            fingerprint TEXT NOT NULL DEFAULT '',
            raw_file_id TEXT NOT NULL DEFAULT '',
            ingest_method TEXT NOT NULL DEFAULT '',
            source_route_id TEXT NOT NULL DEFAULT '',
            source_locator TEXT NOT NULL DEFAULT '',
            playback_locator TEXT NOT NULL DEFAULT '',
            tmdb_hint_id TEXT NOT NULL DEFAULT '',
            tmdb_hint_type TEXT NOT NULL DEFAULT '',
            import_family TEXT NOT NULL DEFAULT 'anime',
            target_filename TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL DEFAULT '',
            presence_state TEXT NOT NULL DEFAULT 'present',
            UNIQUE(scan_id, source_key)
        )
        """,
        """
        CREATE TABLE parsed_facts (
            parsed_fact_id TEXT PRIMARY KEY,
            evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id) ON DELETE CASCADE,
            parser_version TEXT NOT NULL,
            resource_type TEXT NOT NULL DEFAULT 'video',
            media_type TEXT NOT NULL DEFAULT '',
            group_type TEXT NOT NULL DEFAULT '',
            work_title TEXT NOT NULL DEFAULT '',
            original_title TEXT NOT NULL DEFAULT '',
            series_group TEXT NOT NULL DEFAULT '',
            card_type TEXT NOT NULL DEFAULT '',
            relation_type TEXT NOT NULL DEFAULT '',
            show_type TEXT NOT NULL DEFAULT '',
            title_candidates_json TEXT NOT NULL DEFAULT '[]',
            year_candidate INTEGER,
            season_token_raw TEXT NOT NULL DEFAULT '',
            episode_token_raw TEXT NOT NULL DEFAULT '',
            season_candidate INTEGER,
            episode_candidate INTEGER,
            absolute_episode_candidate INTEGER,
            special_candidate INTEGER NOT NULL DEFAULT 0,
            episode_range_json TEXT NOT NULL DEFAULT 'null',
            special_number INTEGER,
            tmdb_hint_id INTEGER,
            tmdb_hint_type TEXT NOT NULL DEFAULT '',
            release_group TEXT NOT NULL DEFAULT '',
            edition_tags_json TEXT NOT NULL DEFAULT '[]',
            quality_tags_json TEXT NOT NULL DEFAULT '[]',
            confidence TEXT NOT NULL DEFAULT 'medium',
            needs_review INTEGER NOT NULL DEFAULT 0,
            is_importable INTEGER NOT NULL DEFAULT 1,
            is_auxiliary INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]'
        )
        """,
        """
        CREATE TABLE works (
            work_id TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            work_type TEXT NOT NULL,
            preferred_title TEXT NOT NULL DEFAULT '',
            original_title TEXT NOT NULL DEFAULT '',
            year INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE work_overrides (
            work_id TEXT PRIMARY KEY,
            override_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE work_aliases (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            normalized_title TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT '',
            alias_type TEXT NOT NULL DEFAULT 'alternate',
            PRIMARY KEY(work_id, normalized_title, language)
        )
        """,
        """
        CREATE TABLE work_source_bindings (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            structural_key TEXT NOT NULL,
            confidence TEXT NOT NULL DEFAULT 'medium',
            binding_source TEXT NOT NULL DEFAULT 'resolver',
            PRIMARY KEY(work_id, root_id, structural_key)
        )
        """,
        """
        CREATE TABLE provider_bindings (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            media_type TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            PRIMARY KEY(provider, media_type, provider_id),
            UNIQUE(work_id, provider, media_type)
        )
        """,
        """
        CREATE TABLE seasons (
            season_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            local_season_number INTEGER NOT NULL,
            season_kind TEXT NOT NULL DEFAULT 'regular',
            title TEXT NOT NULL DEFAULT '',
            UNIQUE(work_id, local_season_number, season_kind)
        )
        """,
        """
        CREATE TABLE season_provider_mappings (
            season_id TEXT NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_season_number INTEGER,
            provider_season_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(season_id, provider)
        )
        """,
        """
        CREATE TABLE episodes (
            episode_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            season_id TEXT NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
            local_episode_number INTEGER,
            absolute_episode_number INTEGER,
            special_number INTEGER,
            episode_kind TEXT NOT NULL DEFAULT 'regular',
            display_title TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE episode_provider_mappings (
            episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_season_number INTEGER,
            provider_episode_number INTEGER,
            provider_episode_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(episode_id, provider)
        )
        """,
        """
        CREATE TABLE editions (
            edition_id TEXT PRIMARY KEY,
            episode_id TEXT REFERENCES episodes(episode_id) ON DELETE CASCADE,
            work_id TEXT REFERENCES works(work_id) ON DELETE CASCADE,
            edition_key TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            duration_hint INTEGER,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            CHECK ((episode_id IS NOT NULL) != (work_id IS NOT NULL)),
            UNIQUE(episode_id, edition_key),
            UNIQUE(work_id, edition_key)
        )
        """,
        """
        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY,
            evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id) ON DELETE CASCADE,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            source_locator TEXT NOT NULL DEFAULT '',
            playback_locator TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT '',
            size INTEGER,
            mtime REAL,
            resolution TEXT NOT NULL DEFAULT '',
            video_codec TEXT NOT NULL DEFAULT '',
            audio_codec TEXT NOT NULL DEFAULT '',
            release_group TEXT NOT NULL DEFAULT '',
            version_tags_json TEXT NOT NULL DEFAULT '[]',
            availability_state TEXT NOT NULL DEFAULT 'available',
            UNIQUE(evidence_id)
        )
        """,
        """
        CREATE TABLE episode_assets (
            episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
            edition_id TEXT REFERENCES editions(edition_id) ON DELETE SET NULL,
            asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'source',
            preference_rank INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(episode_id, asset_id)
        )
        """,
        """
        CREATE TABLE work_assets (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            edition_id TEXT REFERENCES editions(edition_id) ON DELETE SET NULL,
            asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'source',
            preference_rank INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(work_id, asset_id)
        )
        """,
        """
        CREATE TABLE import_revisions (
            revision_id TEXT PRIMARY KEY,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            scan_id TEXT NOT NULL REFERENCES source_scans(scan_id) ON DELETE CASCADE,
            resolver_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'confirmed', 'executing', 'completed', 'failed', 'superseded')),
            graph_digest TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            confirmed_at TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE revision_evidence (
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id) ON DELETE RESTRICT,
            parsed_fact_id TEXT NOT NULL REFERENCES parsed_facts(parsed_fact_id) ON DELETE RESTRICT,
            PRIMARY KEY(revision_id, evidence_id)
        )
        """,
        """
        CREATE TABLE revision_bindings (
            binding_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id) ON DELETE RESTRICT,
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE RESTRICT,
            season_id TEXT REFERENCES seasons(season_id) ON DELETE RESTRICT,
            episode_id TEXT REFERENCES episodes(episode_id) ON DELETE RESTRICT,
            edition_id TEXT REFERENCES editions(edition_id) ON DELETE RESTRICT,
            asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
            confidence TEXT NOT NULL DEFAULT 'medium',
            decision_source TEXT NOT NULL DEFAULT 'resolver',
            reasons_json TEXT NOT NULL DEFAULT '[]',
            override_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(revision_id, evidence_id, episode_id, edition_id, asset_id)
        )
        """,
        """
        CREATE TABLE revision_issues (
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            issue_id TEXT NOT NULL,
            code TEXT NOT NULL,
            evidence_id TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(revision_id, issue_id)
        )
        """,
        """
        CREATE TABLE revision_overrides (
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id) ON DELETE RESTRICT,
            overrides_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY(revision_id, evidence_id)
        )
        """,
        """
        CREATE TABLE scrape_bindings (
            binding_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(revision_id, work_id, provider)
        )
        """,
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            work_id TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            work_id TEXT NOT NULL DEFAULT '',
            artifact_type TEXT NOT NULL,
            target_path TEXT NOT NULL,
            digest TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'staged',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(revision_id, artifact_type, target_path)
        )
        """,
        """
        CREATE TABLE library_generations (
            generation_id TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'building',
            created_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE library_cards (
            generation_id TEXT NOT NULL REFERENCES library_generations(generation_id) ON DELETE CASCADE,
            work_id TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER,
            media_type TEXT NOT NULL,
            episode_count INTEGER NOT NULL DEFAULT 0,
            asset_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(generation_id, work_id)
        )
        """,
        """
        CREATE TABLE playback_progress (
            episode_id TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            work_id TEXT NOT NULL,
            position REAL NOT NULL DEFAULT 0,
            duration REAL NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(episode_id, asset_id)
        )
        """,
        """
        CREATE TABLE playback_history (
            event_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            episode_id TEXT NOT NULL DEFAULT '',
            asset_id TEXT NOT NULL DEFAULT '',
            played_at TEXT NOT NULL,
            title_snapshot TEXT NOT NULL DEFAULT '',
            season_snapshot TEXT NOT NULL DEFAULT '',
            episode_snapshot TEXT NOT NULL DEFAULT '',
            source_provider TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE tracking_states (
            work_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL DEFAULT '',
            last_watched_episode INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(work_id, provider)
        )
        """,
        """
        CREATE TRIGGER v4_source_evidence_immutable_update
        BEFORE UPDATE ON source_evidence
        BEGIN
            SELECT RAISE(ABORT, 'source_evidence is immutable');
        END
        """,
        """
        CREATE TRIGGER v4_source_evidence_immutable_delete
        BEFORE DELETE ON source_evidence
        BEGIN
            SELECT RAISE(ABORT, 'source_evidence is immutable');
        END
        """,
        """
        CREATE TRIGGER v4_parsed_facts_immutable_update
        BEFORE UPDATE ON parsed_facts
        BEGIN
            SELECT RAISE(ABORT, 'parsed_facts is immutable');
        END
        """,
        """
        CREATE TRIGGER v4_parsed_facts_immutable_delete
        BEFORE DELETE ON parsed_facts
        BEGIN
            SELECT RAISE(ABORT, 'parsed_facts is immutable');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_binding_update_guard
        BEFORE UPDATE ON revision_bindings
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_binding_delete_guard
        BEFORE DELETE ON revision_bindings
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_evidence_update_guard
        BEFORE UPDATE ON revision_evidence
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision evidence is immutable');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_evidence_delete_guard
        BEFORE DELETE ON revision_evidence
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision evidence is immutable');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_override_write_guard
        BEFORE INSERT ON revision_overrides
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = NEW.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'only draft revision accepts overrides');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_override_update_guard
        BEFORE UPDATE ON revision_overrides
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'only draft revision accepts overrides');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_override_delete_guard
        BEFORE DELETE ON revision_overrides
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'only draft revision accepts overrides');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_revision_snapshot_guard
        BEFORE UPDATE ON import_revisions
        WHEN OLD.status IN ('confirmed', 'superseded') AND (
            NEW.root_id != OLD.root_id OR NEW.scan_id != OLD.scan_id OR
            NEW.resolver_version != OLD.resolver_version OR NEW.graph_digest != OLD.graph_digest OR
            NEW.created_at != OLD.created_at OR NEW.confirmed_at != OLD.confirmed_at OR
            (OLD.status = 'confirmed' AND NEW.status NOT IN ('confirmed', 'superseded')) OR
            (OLD.status = 'superseded' AND NEW.status != 'superseded')
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision snapshot is immutable');
        END
        """,
        """
        CREATE TRIGGER v4_confirmed_revision_delete_guard
        BEFORE DELETE ON import_revisions
        WHEN OLD.status != 'draft'
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision snapshot is immutable');
        END
        """,
        "CREATE INDEX idx_v4_evidence_root_scan ON source_evidence(root_id, scan_id)",
        """
        CREATE TABLE tree_scan_validation (
            scan_id TEXT PRIMARY KEY REFERENCES source_scans(scan_id) ON DELETE CASCADE,
            root_id TEXT NOT NULL,
            effective_root TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            samples_json TEXT NOT NULL DEFAULT '[]',
            candidates_json TEXT NOT NULL DEFAULT '[]',
            validated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE work_relations (
            relation_id TEXT PRIMARY KEY,
            parent_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            child_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL DEFAULT 'related',
            UNIQUE(parent_work_id, child_work_id, relation_type)
        )
        """,
        """
        CREATE TABLE revision_work_candidates (
            candidate_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL,
            work_id TEXT NOT NULL,
            draft_work_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            original_title TEXT NOT NULL DEFAULT '',
            aliases_json TEXT NOT NULL DEFAULT '[]',
            year INTEGER,
            evidence TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE maintenance_operations (
            operation_id TEXT PRIMARY KEY,
            scope_provider TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            preview_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "ALTER TABLE source_roots ADD COLUMN root_container TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE works ADD COLUMN show_type TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE works ADD COLUMN card_type TEXT NOT NULL DEFAULT ''",
        "CREATE UNIQUE INDEX uq_v4_active_revision_per_root "
        "ON import_revisions(root_id) WHERE status = 'confirmed'",
        "CREATE INDEX idx_v4_facts_evidence ON parsed_facts(evidence_id)",
        "CREATE INDEX idx_v4_seasons_work ON seasons(work_id)",
        "CREATE INDEX idx_v4_source_bindings_lookup ON work_source_bindings(root_id, structural_key)",
        "CREATE INDEX idx_v4_episodes_season ON episodes(season_id)",
        """
        CREATE UNIQUE INDEX uq_v4_episode_local_identity ON episodes(
            work_id, season_id, COALESCE(local_episode_number, -1),
            COALESCE(special_number, -1), episode_kind
        )
        """,
        "CREATE INDEX idx_v4_bindings_revision ON revision_bindings(revision_id)",
        "CREATE INDEX idx_v4_jobs_status ON jobs(status, updated_at)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO v4_meta(key, value) VALUES ('schema', 'v4')")
    conn.execute("INSERT INTO v4_meta(key, value) VALUES ('backend_data_epoch', '4')")


def create_tree_scan_validation(conn: sqlite3.Connection) -> None:
    """创建目录树扫描验证表（v5 新增），供 v4→v5 迁移复用。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tree_scan_validation (
            scan_id TEXT PRIMARY KEY REFERENCES source_scans(scan_id) ON DELETE CASCADE,
            root_id TEXT NOT NULL,
            effective_root TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            samples_json TEXT NOT NULL DEFAULT '[]',
            candidates_json TEXT NOT NULL DEFAULT '[]',
            validated_at TEXT NOT NULL
        )
        """
    )


def create_v6_structures(conn: sqlite3.Connection) -> None:
    """创建 v6 新增结构：work_relations、revision_work_candidates，以及
    source_roots.root_container、works.show_type/card_type 列。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_relations (
            relation_id TEXT PRIMARY KEY,
            parent_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            child_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL DEFAULT 'related',
            UNIQUE(parent_work_id, child_work_id, relation_type)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS revision_work_candidates (
            candidate_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL,
            work_id TEXT NOT NULL,
            draft_work_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            original_title TEXT NOT NULL DEFAULT '',
            aliases_json TEXT NOT NULL DEFAULT '[]',
            year INTEGER,
            evidence TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _add_column_if_missing(conn, "source_roots", "root_container", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "works", "show_type", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "works", "card_type", "TEXT NOT NULL DEFAULT ''")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_schema_v5_to_v6(conn: sqlite3.Connection) -> None:
    """v5 → v6 增量迁移：新增关系/候选表与作品卡片字段，不改动既有数据。"""

    create_v6_structures(conn)


def migrate_schema_v6_to_v7(conn: sqlite3.Connection) -> None:
    """v6 → v7 增量迁移：revision_work_candidates 增加 original_title / aliases_json。"""

    _add_column_if_missing(conn, "revision_work_candidates", "original_title", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "revision_work_candidates", "aliases_json", "TEXT NOT NULL DEFAULT '[]'")


def create_v8_structures(conn: sqlite3.Connection) -> None:
    """v8 增量结构：source_roots 增加来源根级模式字段。

    source_mode 表达来源卡的建立/维护策略（local / tree_snapshot /
    tree_openlist / openlist_full），last_scan_mode 只记录最近一次扫描方式；
    两者都与文件级 SourceEvidence.ingest_method 分层，不再从排序后的
    第一条证据反推来源卡模式。
    """

    _add_column_if_missing(conn, "source_roots", "source_mode", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "source_roots", "last_scan_mode", "TEXT NOT NULL DEFAULT ''")


def create_v9_structures(conn: sqlite3.Connection) -> None:
    """v9 增量结构：来源退役字段与媒体库维护操作表。

    source_roots.retired_at / retired_reason 表达“活动来源退役”；已确认的
    revision / evidence / facts 作为审计事实保留，实时查询通过活动来源过滤排除。
    maintenance_operations 记录按来源清理的预览/确认结果，支持幂等重入。
    """

    _add_column_if_missing(conn, "source_roots", "retired_at", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "source_roots", "retired_reason", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_operations (
            operation_id TEXT PRIMARY KEY,
            scope_provider TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            preview_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def create_v10_structures(conn: sqlite3.Connection) -> None:
    """v10 增量结构：独立播放历史事件表与作品用户覆盖层。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_overrides (
            work_id TEXT PRIMARY KEY,
            override_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playback_history (
            event_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            episode_id TEXT NOT NULL DEFAULT '',
            asset_id TEXT NOT NULL DEFAULT '',
            played_at TEXT NOT NULL,
            title_snapshot TEXT NOT NULL DEFAULT '',
            season_snapshot TEXT NOT NULL DEFAULT '',
            episode_snapshot TEXT NOT NULL DEFAULT '',
            source_provider TEXT NOT NULL DEFAULT ''
        )
        """
    )


def migrate_schema_v9_to_v10(conn: sqlite3.Connection) -> None:
    """v9 → v10 增量迁移：独立播放历史事件表。"""

    create_v10_structures(conn)


def migrate_schema_v8_to_v9(conn: sqlite3.Connection) -> None:
    """v8 → v9 增量迁移：来源退役字段与维护操作表。"""

    create_v9_structures(conn)


def migrate_schema_v7_to_v8(conn: sqlite3.Connection) -> None:
    """v7 → v8 增量迁移：新增 source_mode / last_scan_mode 并做一次最小回填。

    回填只依据既有 source_roots 行推导一次，后续必须由 scan/preview 合同显式
    维护；不允许继续从文件证据反推模式。
    """

    create_v8_structures(conn)
    conn.execute("UPDATE source_roots SET source_mode = 'local' WHERE source_mode = '' AND ingest_method = 'local_scan'")
    conn.execute(
        "UPDATE source_roots SET source_mode = 'tree_openlist' "
        "WHERE source_mode = '' AND ingest_method = 'directory_tree' AND route_id != ''"
    )
    conn.execute(
        "UPDATE source_roots SET source_mode = 'tree_snapshot' "
        "WHERE source_mode = '' AND ingest_method = 'directory_tree'"
    )
    conn.execute(
        "UPDATE source_roots SET source_mode = 'openlist_full' "
        "WHERE source_mode = '' AND ingest_method = 'openlist_scan'"
    )


def migrate_schema_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5 增量迁移：只新增 tree_scan_validation 表，不改动任何既有表。

    用户已有 v4 媒体库通过本迁移保留全部已确认数据；迁移在调用方事务内执行。
    """

    create_tree_scan_validation(conn)
