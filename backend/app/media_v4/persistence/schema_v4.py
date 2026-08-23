"""V4 唯一物理数据库结构。

V4 不通过启动时逐列 ALTER 隐式补丁升级。结构变化必须提升 user_version，
而旧媒体库在 V4 入口只允许一次性重置。
"""

from __future__ import annotations

import sqlite3

V4_SCHEMA_VERSION = 4


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
        CREATE TABLE source_roots (
            root_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            ingest_method TEXT NOT NULL,
            source_locator TEXT NOT NULL DEFAULT '',
            playback_locator TEXT NOT NULL DEFAULT '',
            route_id TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
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
        CREATE TABLE work_aliases (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            normalized_title TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT '',
            alias_type TEXT NOT NULL DEFAULT 'alternate',
            PRIMARY KEY(work_id, normalized_title, language)
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
            CHECK ((episode_id IS NOT NULL) OR (work_id IS NOT NULL)),
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
        CREATE TABLE import_revisions (
            revision_id TEXT PRIMARY KEY,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            scan_id TEXT NOT NULL REFERENCES source_scans(scan_id) ON DELETE CASCADE,
            resolver_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            graph_digest TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            confirmed_at TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE revision_bindings (
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
            PRIMARY KEY(revision_id, evidence_id)
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
            status TEXT NOT NULL DEFAULT 'queued',
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
        "CREATE INDEX idx_v4_evidence_root_scan ON source_evidence(root_id, scan_id)",
        "CREATE INDEX idx_v4_facts_evidence ON parsed_facts(evidence_id)",
        "CREATE INDEX idx_v4_seasons_work ON seasons(work_id)",
        "CREATE INDEX idx_v4_episodes_season ON episodes(season_id)",
        "CREATE INDEX idx_v4_bindings_revision ON revision_bindings(revision_id)",
        "CREATE INDEX idx_v4_jobs_status ON jobs(status, updated_at)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO v4_meta(key, value) VALUES ('schema', 'v4')")
    conn.execute("INSERT INTO v4_meta(key, value) VALUES ('backend_data_epoch', '4')")
