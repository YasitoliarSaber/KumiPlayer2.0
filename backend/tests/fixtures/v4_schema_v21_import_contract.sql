-- Frozen from unmodified v21 at cc52dddee107bf0141246554049864ad40bc3ef7.
BEGIN TRANSACTION;
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
        );
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
        );
INSERT INTO "assets" VALUES('a1','ev1','root','','','',NULL,NULL,'','','','','[]','available');
INSERT INTO "assets" VALUES('a2','ev2','root','','','',NULL,NULL,'','','','','[]','available');
CREATE TABLE bangumi_episode_sync (
            episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
            subject_id INTEGER NOT NULL,
            bangumi_episode_id INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'succeeded',
            synced_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(episode_id, subject_id)
        );
CREATE TABLE bangumi_matches (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            season_number INTEGER NOT NULL DEFAULT 0,
            subject_id INTEGER NOT NULL,
            subject_name TEXT NOT NULL DEFAULT '',
            subject_name_cn TEXT NOT NULL DEFAULT '',
            episode_map_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(work_id, season_number)
        );
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
        );
CREATE TABLE episode_assets (
            episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
            edition_id TEXT REFERENCES editions(edition_id) ON DELETE SET NULL,
            asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'source',
            preference_rank INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(episode_id, asset_id)
        );
CREATE TABLE episode_provider_mappings (
            episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_season_number INTEGER,
            provider_episode_number INTEGER,
            provider_episode_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(episode_id, provider)
        );
CREATE TABLE episodes (
            episode_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            season_id TEXT NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
            local_episode_number INTEGER,
            absolute_episode_number INTEGER,
            special_number INTEGER,
            episode_kind TEXT NOT NULL DEFAULT 'regular',
            display_title TEXT NOT NULL DEFAULT ''
        );
INSERT INTO "episodes" VALUES('e1','w1','s1',NULL,NULL,NULL,'regular','');
INSERT INTO "episodes" VALUES('e2','w2','s2',NULL,NULL,NULL,'regular','');
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
        );
INSERT INTO "import_revisions" VALUES('r1','root','scan1','v21','superseded','','then','');
INSERT INTO "import_revisions" VALUES('r2','root','scan2','v21','confirmed','','then','');
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
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            heartbeat_at TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
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
        );
CREATE TABLE library_generations (
            generation_id TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'building',
            created_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE maintenance_operation_items (
            item_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL REFERENCES maintenance_operations(operation_id) ON DELETE CASCADE,
            artifact_id TEXT NOT NULL,
            root_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            target_path TEXT NOT NULL,
            plan_status TEXT NOT NULL DEFAULT 'planned',
            result_status TEXT NOT NULL DEFAULT 'pending',
            result_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
CREATE TABLE maintenance_operations (
            operation_id TEXT PRIMARY KEY,
            scope_provider TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            preview_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            digest TEXT NOT NULL DEFAULT '',
            root_ids_json TEXT NOT NULL DEFAULT '[]',
            mirror_root_identity TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE openlist_telemetry (
            conn_hash TEXT NOT NULL,
            day TEXT NOT NULL,
            operation TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (conn_hash, day, operation)
        );
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
            episode_title TEXT NOT NULL DEFAULT '',
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
        );
INSERT INTO "parsed_facts" VALUES('fact1','ev1','v21','video','','','Show','','','','','','[]',NULL,'','','',NULL,NULL,NULL,0,'null',NULL,NULL,'','','[]','[]','medium',0,1,0,'[]','[]');
INSERT INTO "parsed_facts" VALUES('fact2','ev2','v21','video','','','Show','','','','','','[]',NULL,'','','',NULL,NULL,NULL,0,'null',NULL,NULL,'','','[]','[]','medium',0,1,0,'[]','[]');
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
        );
INSERT INTO "playback_history" VALUES('h1','w1','e1','a1','then','Original title','','','');
CREATE TABLE playback_progress (
            episode_id TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            work_id TEXT NOT NULL,
            position REAL NOT NULL DEFAULT 0,
            duration REAL NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(episode_id, asset_id)
        );
INSERT INTO "playback_progress" VALUES('e1','a1','w1',123.0,1440.0,0,'then');
CREATE TABLE provider_bindings (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            media_type TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            PRIMARY KEY(work_id, provider, media_type)
        );
INSERT INTO "provider_bindings" VALUES('w1','tmdb','tv','123');
INSERT INTO "provider_bindings" VALUES('w2','tmdb','tv','123');
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
        );
INSERT INTO "revision_bindings" VALUES('b1','r1','ev1','w1','s1','e1',NULL,'a1','medium','resolver','[]','{}');
INSERT INTO "revision_bindings" VALUES('b2','r2','ev2','w2','s2','e2',NULL,'a2','medium','resolver','[]','{}');
CREATE TABLE revision_evidence (
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id) ON DELETE RESTRICT,
            parsed_fact_id TEXT NOT NULL REFERENCES parsed_facts(parsed_fact_id) ON DELETE RESTRICT,
            PRIMARY KEY(revision_id, evidence_id)
        );
INSERT INTO "revision_evidence" VALUES('r1','ev1','fact1');
INSERT INTO "revision_evidence" VALUES('r2','ev2','fact2');
CREATE TABLE revision_issues (
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            issue_id TEXT NOT NULL,
            code TEXT NOT NULL,
            evidence_id TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(revision_id, issue_id)
        );
CREATE TABLE revision_overrides (
            revision_id TEXT NOT NULL REFERENCES import_revisions(revision_id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id) ON DELETE RESTRICT,
            overrides_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY(revision_id, evidence_id)
        );
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
        , score REAL, reasons_json TEXT NOT NULL DEFAULT '[]', popularity REAL, recommended INTEGER NOT NULL DEFAULT 0);
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
        );
CREATE TABLE season_provider_mappings (
            season_id TEXT NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_season_number INTEGER,
            provider_season_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(season_id, provider)
        );
CREATE TABLE seasons (
            season_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            local_season_number INTEGER NOT NULL,
            season_kind TEXT NOT NULL DEFAULT 'regular',
            title TEXT NOT NULL DEFAULT '',
            UNIQUE(work_id, local_season_number, season_kind)
        );
INSERT INTO "seasons" VALUES('s1','w1',0,'regular','');
INSERT INTO "seasons" VALUES('s2','w2',0,'regular','');
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
        );
INSERT INTO "source_evidence" VALUES('ev1','scan1','root','','key1','Show/E01.mkv','video',NULL,NULL,'','','','','','','','','anime','','','present');
INSERT INTO "source_evidence" VALUES('ev2','scan2','root','','key2','Show/E01.mkv','video',NULL,NULL,'','','','','','','','','anime','','','present');
CREATE TABLE source_health (
            source_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'healthy',
            reason_kind TEXT NOT NULL DEFAULT '',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            cooldown_until REAL NOT NULL DEFAULT 0,
            last_failure_at REAL NOT NULL DEFAULT 0,
            last_success_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
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
        , root_container TEXT NOT NULL DEFAULT '');
INSERT INTO "source_roots" VALUES('root','local','directory_tree','','','','',1,'','','','','then','then','');
CREATE TABLE source_scan_directories (
            scan_id TEXT NOT NULL,
            remote_path TEXT NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            next_page INTEGER NOT NULL DEFAULT 1,
            discovered_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (scan_id, remote_path)
        );
CREATE TABLE source_scan_requests (
            scan_id TEXT PRIMARY KEY REFERENCES source_scans(scan_id) ON DELETE CASCADE,
            scan_kind TEXT NOT NULL DEFAULT 'full',
            source_mode TEXT NOT NULL DEFAULT '',
            request_json TEXT NOT NULL DEFAULT '{}',
            input_archive_path TEXT NOT NULL DEFAULT '',
            input_sha256 TEXT NOT NULL DEFAULT '',
            original_filename TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_of TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE source_scans (
            scan_id TEXT PRIMARY KEY,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            status TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'queued',
            processed_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            heartbeat_at TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            UNIQUE(root_id, generation)
        );
INSERT INTO "source_scans" VALUES('scan1','root',1,'completed','queued',0,0,'',0,'','','');
INSERT INTO "source_scans" VALUES('scan2','root',2,'completed','queued',0,0,'',0,'','','');
CREATE TABLE tracking_states (
            work_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL DEFAULT '',
            last_watched_episode INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(work_id, provider)
        );
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
        );
CREATE TABLE v4_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
INSERT INTO "v4_meta" VALUES('schema','v4');
INSERT INTO "v4_meta" VALUES('backend_data_epoch','4');
CREATE TABLE work_aliases (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            normalized_title TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT '',
            alias_type TEXT NOT NULL DEFAULT 'alternate',
            PRIMARY KEY(work_id, normalized_title, language)
        );
CREATE TABLE work_assets (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            edition_id TEXT REFERENCES editions(edition_id) ON DELETE SET NULL,
            asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'source',
            preference_rank INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(work_id, asset_id)
        );
CREATE TABLE work_overrides (
            work_id TEXT PRIMARY KEY,
            override_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
INSERT INTO "work_overrides" VALUES('w1','{"favorite":true,"title":"Manual"}','then','then');
CREATE TABLE work_relations (
            relation_id TEXT PRIMARY KEY,
            parent_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            child_work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL DEFAULT 'related',
            UNIQUE(parent_work_id, child_work_id, relation_type)
        );
CREATE TABLE work_source_bindings (
            work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
            root_id TEXT NOT NULL REFERENCES source_roots(root_id) ON DELETE CASCADE,
            structural_key TEXT NOT NULL,
            confidence TEXT NOT NULL DEFAULT 'medium',
            binding_source TEXT NOT NULL DEFAULT 'resolver',
            PRIMARY KEY(work_id, root_id, structural_key)
        );
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
        , show_type TEXT NOT NULL DEFAULT '', card_type TEXT NOT NULL DEFAULT '');
INSERT INTO "works" VALUES('w1','local:w1','movie','Show','',NULL,'active','then','then','','');
INSERT INTO "works" VALUES('w2','local:w2','movie','Show','',NULL,'active','then','then','','');
CREATE INDEX idx_v4_source_health_state ON source_health(state);
CREATE INDEX idx_v4_provider_bindings_identity ON provider_bindings(provider, media_type, provider_id);
CREATE TRIGGER v4_source_evidence_immutable_update
        BEFORE UPDATE ON source_evidence
        BEGIN
            SELECT RAISE(ABORT, 'source_evidence is immutable');
        END;
CREATE TRIGGER v4_source_evidence_immutable_delete
        BEFORE DELETE ON source_evidence
        BEGIN
            SELECT RAISE(ABORT, 'source_evidence is immutable');
        END;
CREATE TRIGGER v4_parsed_facts_immutable_update
        BEFORE UPDATE ON parsed_facts
        BEGIN
            SELECT RAISE(ABORT, 'parsed_facts is immutable');
        END;
CREATE TRIGGER v4_parsed_facts_immutable_delete
        BEFORE DELETE ON parsed_facts
        BEGIN
            SELECT RAISE(ABORT, 'parsed_facts is immutable');
        END;
CREATE TRIGGER v4_confirmed_binding_update_guard
        BEFORE UPDATE ON revision_bindings
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision bindings are immutable');
        END;
CREATE TRIGGER v4_confirmed_binding_delete_guard
        BEFORE DELETE ON revision_bindings
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision bindings are immutable');
        END;
CREATE TRIGGER v4_confirmed_evidence_update_guard
        BEFORE UPDATE ON revision_evidence
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision evidence is immutable');
        END;
CREATE TRIGGER v4_confirmed_evidence_delete_guard
        BEFORE DELETE ON revision_evidence
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision evidence is immutable');
        END;
CREATE TRIGGER v4_confirmed_override_write_guard
        BEFORE INSERT ON revision_overrides
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = NEW.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'only draft revision accepts overrides');
        END;
CREATE TRIGGER v4_confirmed_override_update_guard
        BEFORE UPDATE ON revision_overrides
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'only draft revision accepts overrides');
        END;
CREATE TRIGGER v4_confirmed_override_delete_guard
        BEFORE DELETE ON revision_overrides
        WHEN EXISTS (
            SELECT 1 FROM import_revisions
            WHERE revision_id = OLD.revision_id AND status != 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'only draft revision accepts overrides');
        END;
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
        END;
CREATE TRIGGER v4_confirmed_revision_delete_guard
        BEFORE DELETE ON import_revisions
        WHEN OLD.status != 'draft'
        BEGIN
            SELECT RAISE(ABORT, 'confirmed revision snapshot is immutable');
        END;
CREATE INDEX idx_v4_evidence_root_scan ON source_evidence(root_id, scan_id);
CREATE UNIQUE INDEX uq_v4_active_revision_per_root ON import_revisions(root_id) WHERE status = 'confirmed';
CREATE INDEX idx_v4_facts_evidence ON parsed_facts(evidence_id);
CREATE INDEX idx_v4_seasons_work ON seasons(work_id);
CREATE INDEX idx_v4_source_bindings_lookup ON work_source_bindings(root_id, structural_key);
CREATE INDEX idx_v4_episodes_season ON episodes(season_id);
CREATE UNIQUE INDEX uq_v4_episode_local_identity ON episodes(
            work_id, season_id, COALESCE(local_episode_number, -1),
            COALESCE(special_number, -1), episode_kind
        )
        ;
CREATE INDEX idx_v4_bindings_revision ON revision_bindings(revision_id);
CREATE INDEX idx_v4_jobs_status ON jobs(status, updated_at);
CREATE UNIQUE INDEX ux_maintenance_op_artifact
        ON maintenance_operation_items(operation_id, artifact_id)
        ;
CREATE INDEX idx_bangumi_matches_subject ON bangumi_matches(subject_id);
CREATE INDEX idx_source_scan_directories_pending ON source_scan_directories (scan_id, status, depth, remote_path);
CREATE INDEX idx_v4_bindings_work ON revision_bindings(work_id, revision_id);
CREATE INDEX idx_v4_artifacts_work ON artifacts(revision_id, work_id, artifact_type);
CREATE INDEX idx_v4_jobs_revision ON jobs(revision_id, job_type, job_id);
COMMIT;
PRAGMA user_version = 21;
