-- Royells v20 PostgreSQL non-constraint indexes
-- Run after schema.sql. On a new empty database this file can run as-is.
-- During a live migration, regular-table indexes can be created with
-- CONCURRENTLY one at a time. PostgreSQL does not support CREATE INDEX
-- CONCURRENTLY directly on a partitioned parent; create the parent index on
-- ONLY, build each child index concurrently, and attach the child indexes.

BEGIN;

SET LOCAL search_path = royells, public;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '0';

-- Source configuration and scan progress.
CREATE INDEX IF NOT EXISTS idx_source_channels_active_priority
    ON source_channels (priority DESC, source_chat_id)
    WHERE enabled AND status = 'active';

-- Transactional PostgreSQL-to-Redis projection outbox.
CREATE INDEX IF NOT EXISTS idx_redis_outbox_publish
    ON redis_outbox (available_at, outbox_id)
    INCLUDE (
        event_type, aggregate_type, aggregate_id,
        aggregate_version, destination, attempt_count
    )
    WHERE outbox_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_redis_outbox_expired_lease
    ON redis_outbox (lease_expires_at, outbox_id)
    INCLUDE (lease_owner_instance_id, lease_token, attempt_count)
    WHERE outbox_status = 'publishing';

CREATE INDEX IF NOT EXISTS idx_redis_outbox_retention
    ON redis_outbox (published_at, outbox_id)
    WHERE outbox_status = 'published';

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_channels_active_username
    ON source_channels (lower(username))
    WHERE username IS NOT NULL
      AND status NOT IN ('removed', 'ignored_system');

CREATE INDEX IF NOT EXISTS idx_source_identifiers_source_active
    ON source_identifiers (source_chat_id, is_primary DESC)
    WHERE is_active;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_identifiers_primary_kind
    ON source_identifiers (source_chat_id, identifier_type)
    WHERE is_active AND is_primary;

CREATE INDEX IF NOT EXISTS idx_source_scan_cursors_incomplete
    ON source_scan_cursors (cursor_kind, updated_at, source_chat_id)
    INCLUDE (next_message_id, high_watermark_message_id)
    WHERE completed = false;

CREATE INDEX IF NOT EXISTS idx_scan_runs_active
    ON scan_runs (scan_type, started_at)
    WHERE status IN ('planned', 'running', 'paused');

CREATE INDEX IF NOT EXISTS idx_scan_run_sources_pending
    ON scan_run_sources (scan_run_id, status, updated_at, source_chat_id)
    WHERE status IN ('pending', 'running', 'paused', 'failed');

-- Subscription dashboard and expiry maintenance.
CREATE INDEX IF NOT EXISTS idx_subscriptions_status_expiry
    ON subscriptions (status, expire_at, user_id);

CREATE INDEX IF NOT EXISTS idx_subscriptions_username
    ON subscriptions (lower(username))
    WHERE username IS NOT NULL;

-- Media and source-message lookups.
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_objects_file_unique_id
    ON media_objects (telegram_file_unique_id)
    WHERE telegram_file_unique_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_objects_last_seen
    ON media_objects (last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_source_messages_media_uid
    ON source_messages (media_uid)
    WHERE media_uid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_source_messages_group
    ON source_messages (source_chat_id, media_group_id, source_message_id)
    WHERE media_group_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_source_messages_sent
    ON source_messages (source_chat_id, sent_at DESC, source_message_id DESC);

CREATE INDEX IF NOT EXISTS idx_media_groups_flush
    ON media_groups (flush_after, source_chat_id, media_group_id)
    WHERE assembly_status IN ('assembling', 'ready', 'expired');

-- Worker lifecycle.
CREATE INDEX IF NOT EXISTS idx_worker_instances_live
    ON worker_instances (instance_id, worker_type, status, heartbeat_at)
    WHERE status IN ('starting', 'idle', 'working', 'waiting', 'stopping');

-- Job lifecycle and recovery.
CREATE INDEX IF NOT EXISTS idx_media_jobs_runnable
    ON media_jobs (status, priority DESC, not_before, created_at)
    INCLUDE (job_type, source_chat_id, attempt_count)
    WHERE status IN (
        'admitted', 'queued', 'downloading', 'downloaded',
        'uploading', 'reconciling', 'retry_wait'
    );

CREATE INDEX IF NOT EXISTS idx_media_jobs_source_status
    ON media_jobs (source_chat_id, status, created_at DESC)
    WHERE source_chat_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_jobs_scan_run
    ON media_jobs (scan_run_id, status, created_at)
    WHERE scan_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_jobs_terminal_retention
    ON media_jobs (completed_at, status, job_id)
    WHERE status IN ('completed', 'failed', 'dead', 'cancelled');

CREATE INDEX IF NOT EXISTS idx_job_items_media_uid
    ON job_items (media_uid, item_status, job_id)
    WHERE media_uid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_job_items_status
    ON job_items (job_id, item_status, item_no);

CREATE INDEX IF NOT EXISTS idx_job_files_active
    ON job_files (file_state, updated_at, job_id)
    INCLUDE (storage_key, bytes_completed, actual_size_bytes)
    WHERE file_state IN (
        'expected', 'downloading', 'complete', 'validating',
        'ready', 'uploading', 'orphaned', 'invalid'
    );

CREATE INDEX IF NOT EXISTS idx_job_files_cleanup
    ON job_files (cleanup_after, job_file_id)
    WHERE cleanup_after IS NOT NULL
      AND file_state NOT IN ('deleted', 'uploading');

CREATE INDEX IF NOT EXISTS idx_job_transfer_active
    ON job_transfer_state (transfer_status, updated_at, job_id)
    WHERE transfer_status IN ('pending', 'running', 'paused', 'ambiguous', 'failed');

-- Durable fair queue. These indexes are intentionally narrow and partial.
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_queue_one_active_per_job
    ON job_queue_entries (job_id)
    WHERE state IN ('ready', 'delayed', 'leased');

CREATE UNIQUE INDEX IF NOT EXISTS uq_job_queue_one_active_per_worker
    ON job_queue_entries (lease_owner_worker_id)
    WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS idx_queue_fairness_next
    ON queue_fairness_state (
        queue_name,
        priority_class,
        last_virtual_finish,
        fairness_key
    );

CREATE INDEX IF NOT EXISTS idx_job_queue_claim
    ON job_queue_entries (
        queue_name,
        priority_class,
        priority DESC,
        virtual_finish ASC,
        queue_order ASC
    )
    INCLUDE (
        job_id, available_at, estimated_cost, virtual_start, row_version
    )
    WHERE state = 'ready';

CREATE INDEX IF NOT EXISTS idx_job_queue_delayed_due
    ON job_queue_entries (available_at, queue_name, priority DESC, queue_order)
    INCLUDE (job_id)
    WHERE state = 'delayed';

CREATE INDEX IF NOT EXISTS idx_job_queue_expired_lease
    ON job_queue_entries (lease_expires_at, queue_name, queue_entry_id)
    INCLUDE (job_id, lease_owner_worker_id, lease_token)
    WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS idx_job_queue_terminal_retention
    ON job_queue_entries (completed_at, state, queue_entry_id)
    WHERE state IN ('done', 'cancelled', 'dead');

-- Processing reservations and ownership recovery.
CREATE INDEX IF NOT EXISTS idx_processing_reservations_expiry
    ON processing_reservations (expires_at, reservation_key)
    INCLUDE (job_id, owner_instance_id, owner_worker_id, fencing_token)
    WHERE state = 'active';

CREATE INDEX IF NOT EXISTS idx_processing_reservations_media
    ON processing_reservations (media_uid, expires_at)
    WHERE state = 'active' AND media_uid IS NOT NULL;

-- Delivery journal, reconciliation, and target authority.
CREATE INDEX IF NOT EXISTS idx_delivery_intents_unresolved
    ON delivery_intents (status, reconcile_after, created_at)
    INCLUDE (job_id, target_chat_id, attempt_no)
    WHERE status IN ('prepared', 'submitting', 'accepted', 'ambiguous', 'confirmed');

CREATE INDEX IF NOT EXISTS idx_delivery_intents_job
    ON delivery_intents (job_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_items_target_message
    ON delivery_intent_items (target_chat_id, target_message_id)
    WHERE target_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_delivery_items_media_status
    ON delivery_intent_items (media_uid, item_status)
    WHERE media_uid IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_target_media_target_message
    ON target_media_registry (target_chat_id, target_message_id);

CREATE INDEX IF NOT EXISTS idx_target_media_status_updated
    ON target_media_registry (registry_status, updated_at, media_uid);

CREATE INDEX IF NOT EXISTS idx_target_media_source_message
    ON target_media_registry (source_chat_id, source_message_id)
    WHERE source_chat_id IS NOT NULL AND source_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_deduplication_media_active
    ON deduplication_keys (media_uid, key_kind)
    WHERE status = 'active' AND media_uid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_deduplication_source_message
    ON deduplication_keys (source_chat_id, source_message_id)
    WHERE status = 'active'
      AND source_chat_id IS NOT NULL
      AND source_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dead_media_source_status
    ON dead_media (source_chat_id, status, last_failed_at DESC)
    WHERE source_chat_id IS NOT NULL;

-- Telegram transport and shared runtime coordination.
CREATE INDEX IF NOT EXISTS idx_telegram_requests_unresolved
    ON telegram_requests (status, next_reconcile_at, created_at)
    INCLUDE (job_id, delivery_intent_id, operation, client_role)
    WHERE status IN ('prepared', 'inflight', 'accepted', 'ambiguous');

CREATE INDEX IF NOT EXISTS idx_telegram_requests_job
    ON telegram_requests (job_id, created_at DESC)
    WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_leases_expiry
    ON runtime_leases (expires_at, lease_kind, lease_name)
    INCLUDE (owner_instance_id, owner_worker_id, fencing_token)
    WHERE status = 'held';

CREATE INDEX IF NOT EXISTS idx_rate_limit_blocked
    ON rate_limit_state (blocked_until, scope_key)
    WHERE blocked_until IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_checkpoint_current
    ON runtime_checkpoints (checkpoint_kind)
    WHERE checkpoint_status = 'current';

CREATE INDEX IF NOT EXISTS idx_runtime_checkpoint_retention
    ON runtime_checkpoints (checkpoint_kind, created_at DESC, checkpoint_id);

CREATE INDEX IF NOT EXISTS idx_runtime_health_status
    ON runtime_health (health_status, last_seen_at);

CREATE INDEX IF NOT EXISTS idx_migration_rejects_unresolved
    ON migration_rejects (migration_run_id, source_name, created_at)
    WHERE resolved_at IS NULL;

-- Partitioned history indexes. PostgreSQL creates corresponding child indexes.
CREATE INDEX IF NOT EXISTS idx_job_events_job_time
    ON job_events (job_id, event_at DESC);

CREATE INDEX IF NOT EXISTS idx_job_events_queue_time
    ON job_events (queue_entry_id, event_at DESC)
    WHERE queue_entry_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_job_events_worker_time
    ON job_events (worker_id, event_at DESC)
    WHERE worker_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_job_events_time_brin
    ON job_events USING brin (event_at)
    WITH (pages_per_range = 64);

CREATE INDEX IF NOT EXISTS idx_delivery_history_media_time
    ON delivery_history (media_uid, delivered_at DESC)
    WHERE media_uid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_delivery_history_target
    ON delivery_history (target_chat_id, target_message_id, delivered_at DESC)
    WHERE target_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_delivery_history_job_time
    ON delivery_history (job_id, delivered_at DESC)
    WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_delivery_history_time_brin
    ON delivery_history USING brin (delivered_at)
    WITH (pages_per_range = 64);

CREATE INDEX IF NOT EXISTS idx_source_scan_events_source_time
    ON source_scan_events (source_chat_id, event_at DESC);

CREATE INDEX IF NOT EXISTS idx_source_scan_events_run_time
    ON source_scan_events (scan_run_id, event_at DESC)
    WHERE scan_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_source_scan_events_time_brin
    ON source_scan_events USING brin (event_at)
    WITH (pages_per_range = 64);

CREATE INDEX IF NOT EXISTS idx_audit_events_object_time
    ON audit_events (object_type, object_id, event_at DESC)
    WHERE object_type <> '';

CREATE INDEX IF NOT EXISTS idx_audit_events_action_time
    ON audit_events (action, event_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_time_brin
    ON audit_events USING brin (event_at)
    WITH (pages_per_range = 64);

COMMIT;
