MIGRATION_VERSION = 4

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL CHECK (mime_type = 'image/png'),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    cat_name TEXT,
    cat_traits TEXT,
    source_manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_assets (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    role TEXT NOT NULL,
    sort_index INTEGER NOT NULL,
    PRIMARY KEY (project_id, role, sort_index)
);

CREATE TABLE IF NOT EXISTS search_runs (
    search_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'waiting_for_human', 'accepted', 'failed', 'cancelled')
    ),
    source_manifest_hash TEXT NOT NULL,
    placement_json TEXT NOT NULL,
    user_intent TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    max_rounds INTEGER NOT NULL,
    budget_usd REAL,
    review_each_round INTEGER NOT NULL,
    round_index INTEGER NOT NULL DEFAULT 0,
    global_winner_id TEXT,
    active_directives_json TEXT NOT NULL DEFAULT '[]',
    stop_reason TEXT,
    error_json TEXT,
    state_summary_json TEXT,
    idempotency_key TEXT,
    idempotency_fingerprint TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_runs_queue
ON search_runs(status, lease_until, created_at);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES search_runs(search_id),
    round_index INTEGER NOT NULL,
    variant_index INTEGER NOT NULL,
    request_key TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(search_id, round_index, variant_index),
    UNIQUE(search_id, request_key, variant_index)
);

CREATE INDEX IF NOT EXISTS idx_candidates_search
ON candidates(search_id, round_index, variant_index);

CREATE TABLE IF NOT EXISTS search_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id TEXT NOT NULL REFERENCES search_runs(search_id),
    event_key TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(search_id, event_key)
);

CREATE INDEX IF NOT EXISTS idx_search_events_stream
ON search_events(search_id, id);

CREATE TABLE IF NOT EXISTS provider_calls (
    request_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    search_id TEXT NOT NULL REFERENCES search_runs(search_id),
    status TEXT NOT NULL CHECK (
        status IN ('reserved', 'running', 'completed', 'failed_retryable', 'failed_terminal')
    ),
    request_json TEXT NOT NULL,
    response_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_json TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES search_runs(search_id),
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    round_index INTEGER NOT NULL,
    rubric_version TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    score REAL CHECK (score IS NULL OR (score >= 0 AND score <= 100)),
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, rubric_version)
);

CREATE INDEX IF NOT EXISTS idx_candidate_evaluations_search
ON candidate_evaluations(search_id, round_index, candidate_id);
"""
