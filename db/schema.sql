-- RagLeap Core database schema
-- Requires the pgvector extension (bundled in the pgvector/pgvector Docker image)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_name TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER,
    embedding vector(3072),
    detected_language TEXT,
    language_confidence REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- pgvector's HNSW index has a hard 2000-dimension limit for standard `vector`,
-- but Gemini's gemini-embedding-001 produces 3072-dim vectors. We cast to
-- halfvec(3072) (half-precision, pgvector >= 0.7.0) to build the index within
-- that limit, matching the same workaround used in RagLeap's production schema.
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);

-- Full-text search support for hybrid (dense + sparse) retrieval.
-- Generated column stays in sync automatically on insert/update.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS text_search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;

CREATE INDEX IF NOT EXISTS chunks_text_search_idx
    ON chunks USING GIN (text_search_vector);

-- Integrations: external data sources (databases, CRMs, SaaS APIs) that can
-- sync per-user context into RAG responses. Single-tenant — no workspace scoping.
CREATE TABLE IF NOT EXISTS data_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,

    -- Connection details (encrypted at the application layer via Fernet
    -- before being written here — never store plaintext credentials)
    connection_string TEXT,
    api_endpoint TEXT,
    api_key TEXT,
    api_headers JSONB DEFAULT '{}',

    -- Query configuration
    query_template TEXT,
    field_mappings JSONB DEFAULT '{}',
    user_identifier_field TEXT NOT NULL DEFAULT 'user_id',
    documents_table_name TEXT NOT NULL DEFAULT 'documents',

    -- Sync configuration
    sync_interval_minutes INTEGER NOT NULL DEFAULT 360,
    last_sync_at TIMESTAMPTZ,
    last_sync_status TEXT NOT NULL DEFAULT 'pending',
    last_sync_error TEXT,
    last_sync_record_count INTEGER NOT NULL DEFAULT 0,

    is_active BOOLEAN NOT NULL DEFAULT true,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Customer/user context synced from external data sources, used to
-- personalize RAG responses.
CREATE TABLE IF NOT EXISTS synced_context_data (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    data_source_id UUID NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    user_identifier TEXT NOT NULL,
    context_data JSONB NOT NULL DEFAULT '{}',
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_updated_at TIMESTAMPTZ,
    UNIQUE (data_source_id, user_identifier)
);

CREATE INDEX IF NOT EXISTS synced_context_data_lookup_idx
    ON synced_context_data (data_source_id, user_identifier);


CREATE TABLE IF NOT EXISTS business_profile (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name         TEXT NOT NULL DEFAULT '',
    industry              TEXT NOT NULL DEFAULT '',
    description           TEXT NOT NULL DEFAULT '',
    products_services     TEXT NOT NULL DEFAULT '',
    target_customers      TEXT NOT NULL DEFAULT '',
    working_hours         TEXT NOT NULL DEFAULT '',
    location              TEXT NOT NULL DEFAULT '',
    tone_preference       TEXT NOT NULL DEFAULT 'friendly',
    primary_language      TEXT NOT NULL DEFAULT 'en',
    additional_languages  JSONB NOT NULL DEFAULT '[]',
    owner_instructions    TEXT NOT NULL DEFAULT '',
    auto_learned_profile  TEXT NOT NULL DEFAULT '',
    skill_version         INTEGER NOT NULL DEFAULT 0,
    is_profile_complete   BOOLEAN NOT NULL DEFAULT false,
    last_learned_at       TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employee_roles (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role             TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL DEFAULT '',
    is_active        BOOLEAN NOT NULL DEFAULT true,
    channels         JSONB NOT NULL DEFAULT '[]',
    skill_tags       JSONB NOT NULL DEFAULT '[]',
    personality      TEXT NOT NULL DEFAULT '',
    skills_summary   TEXT NOT NULL DEFAULT '',
    last_learned_at  TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employee_memory (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    text_content       TEXT NOT NULL,
    summary            TEXT NOT NULL DEFAULT '',
    tags               JSONB NOT NULL DEFAULT '[]',
    importance         REAL NOT NULL DEFAULT 0.7,
    source             TEXT NOT NULL DEFAULT 'interaction',
    permanent          BOOLEAN NOT NULL DEFAULT false,
    review_after_days  INTEGER NOT NULL DEFAULT 180,
    content_hash       TEXT NOT NULL,
    embedding          vector(3072),
    token_count        INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS employee_memory_embedding_idx
    ON employee_memory USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
CREATE INDEX IF NOT EXISTS employee_memory_tags_idx
    ON employee_memory USING GIN (tags);
CREATE INDEX IF NOT EXISTS employee_memory_hash_idx
    ON employee_memory (content_hash, source);
-- UNIQUE index required by write_learned_skill()'s
-- INSERT ... ON CONFLICT (content_hash, source) DO NOTHING clause.
-- This existed live on the VPS but was missing here (schema drift,
-- same failure mode PR #267's SENSITIVE_DOMAIN_ROLES had) - discovered
-- when seed_default_memory_seeds() failed on a fresh CI database with
-- "no unique or exclusion constraint matching the ON CONFLICT
-- specification" despite working fine against the live (already-
-- patched) VPS database.
CREATE UNIQUE INDEX IF NOT EXISTS employee_memory_hash_source_uidx
    ON employee_memory (content_hash, source);

-- n8n workflow automation. Single-tenant — no workspace scoping.
-- Fires a webhook after the AI replies on a matching channel; caller
-- (channel adapter) supplies channel/message/ai_reply, this module never
-- raises — a broken workflow config should never break the main chat flow.
CREATE TABLE IF NOT EXISTS n8n_workflows (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    webhook_url TEXT NOT NULL,
    channels    JSONB NOT NULL DEFAULT '[]',
    is_active   BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS n8n_workflows_active_idx
    ON n8n_workflows (is_active);

-- Autonomous Loop settings — single-tenant port of production's
-- api/autonomy_engine.py. One row (singleton, no workspace FK).
CREATE TABLE IF NOT EXISTS autonomy_settings (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mode              TEXT NOT NULL DEFAULT 'off',
    channels          JSONB NOT NULL DEFAULT '[]',
    actions           JSONB NOT NULL DEFAULT '[]',
    approval_channel  TEXT NOT NULL DEFAULT 'telegram',
    approval_target   TEXT NOT NULL DEFAULT '',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pending actions awaiting owner approval in "semi" mode.
CREATE TABLE IF NOT EXISTS autonomy_pending (
    action_id     TEXT PRIMARY KEY,
    action_type   TEXT NOT NULL,
    channel       TEXT NOT NULL,
    target        TEXT NOT NULL,
    content       TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    role          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bounded audit log of every autonomous action (executed, rejected, or errored).
CREATE TABLE IF NOT EXISTS autonomy_log (
    id           BIGSERIAL PRIMARY KEY,
    action_type  TEXT NOT NULL,
    channel      TEXT NOT NULL,
    target       TEXT NOT NULL,
    content      TEXT NOT NULL,
    result       TEXT NOT NULL,
    approved     BOOLEAN NOT NULL DEFAULT true,
    role         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS autonomy_log_created_idx
    ON autonomy_log (created_at DESC);

-- Tracks the most recent role-based reply per (channel, chat_id), so
-- an owner can send a quick feedback command (thumbs up/down,
-- "helpful"/"not helpful") that gets attributed to the correct
-- memory entries via core.employees.learning.record_role_memory_outcome().
-- Without this table there is no real success/failure signal available
-- in the channels -- see core/employees/feedback.py.
CREATE TABLE IF NOT EXISTS role_reply_log (
    channel          TEXT NOT NULL,
    chat_id          TEXT NOT NULL,
    role_memory_ids  JSONB NOT NULL DEFAULT '[]',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, chat_id)
);

-- Owner-configured default AI Employee role per channel (e.g. telegram ->
-- manager, whatsapp -> support). Falls back to 'support' when unset. See
-- core/employees/channel_roles.py for the routing logic that uses this.
CREATE TABLE IF NOT EXISTS channel_role_config (
    channel     TEXT PRIMARY KEY,
    role        TEXT NOT NULL DEFAULT 'support',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- CSV connector content storage — no persistent disk volume exists in
-- docker-compose.yml, so CSV content lives in Postgres like everything
-- else in Core, rather than on the container filesystem.
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS csv_content TEXT;
ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS csv_filename TEXT;

-- Item #2 of the 9-pattern agentic-architecture build: a real per-request
-- trace record for the blocking chat pipeline (core.chat.ask()). Scoped
-- narrow on purpose, same as the Tool Registry's first entry -- ask_stream()
-- is NOT traced here (see core/observability.py's module docstring for
-- why), and this is a flat record per request, not a formal span/tree
-- layer. Also strengthens the audit story for the still-open compliance
-- questions on the 7 sensitive-domain roles (see ROADMAP.md).
CREATE TABLE IF NOT EXISTS agent_traces (
    id                  BIGSERIAL PRIMARY KEY,
    role                TEXT,
    query               TEXT NOT NULL,
    detected_language   TEXT,
    chunks_retrieved    INTEGER,
    chunks_sent         INTEGER,
    provider_used       TEXT,
    fallback_used       BOOLEAN NOT NULL DEFAULT false,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER,
    latency_ms          INTEGER,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_traces_created_idx
    ON agent_traces (created_at DESC);

-- Item #3 of the 9-pattern agentic-architecture build (self-correction/
-- reflection): flags when a sensitive-domain role's answer was checked
-- against retrieved context and found to contain an unsupported claim.
-- NULL means either the check wasn't run (role not in
-- SENSITIVE_DOMAIN_ROLES) or the answer was found grounded. See
-- core/generation.py's GenerationService.check_grounding().
ALTER TABLE agent_traces ADD COLUMN IF NOT EXISTS reflection_concern TEXT;
ALTER TABLE agent_traces ADD COLUMN IF NOT EXISTS reasoning TEXT;

-- Usage ledger for per-role / global budgets (core/budget.py). One row per LLM call.
-- estimated = true when the provider reported no token counts (e.g. Ollama) and the
-- numbers are ~4 characters per token from the prompt and reply text.
CREATE TABLE IF NOT EXISTS llm_usage (
    id                BIGSERIAL PRIMARY KEY,
    role              TEXT,
    provider          TEXT,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    estimated         BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_usage_created_idx ON llm_usage (created_at DESC);
CREATE INDEX IF NOT EXISTS llm_usage_role_created_idx ON llm_usage (role, created_at DESC);

-- Task tickets: a lightweight work-tracking system AI employees (via the
-- action planner) and the owner (via the API) can create, assign, and
-- update. Mirrors n8n_workflows' single-tenant, plain-SQL style.
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'blocked', 'done', 'cancelled')),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    assigned_role TEXT,
    creator TEXT NOT NULL DEFAULT 'owner',
    parent_task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    due_date TIMESTAMPTZ,
    result TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks (status);
CREATE INDEX IF NOT EXISTS tasks_assigned_role_idx ON tasks (assigned_role);

-- Proactive triggers: scheduled check-ins where an AI Employee runs a
-- prompt on an interval and the result is delivered to the owner via the
-- same approval gate as every other action (off/semi/full all apply).
CREATE TABLE IF NOT EXISTS proactive_triggers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_minutes INTEGER NOT NULL CHECK (schedule_minutes >= 5),
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS proactive_triggers_due_idx
    ON proactive_triggers (next_run_at) WHERE is_active;

-- Act-observe agent loop runs (opt-in: AGENT_LOOP_ENABLED). A run that needs owner
-- approval is saved here and resumed when the pending action is approved or rejected.
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role TEXT,
    query TEXT NOT NULL,
    context_answer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'waiting_approval', 'done', 'failed', 'rejected', 'budget')),
    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    tainted BOOLEAN NOT NULL DEFAULT false,
    pending_action_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runs_pending_idx ON agent_runs (pending_action_id) WHERE pending_action_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS agent_runs_created_idx ON agent_runs (created_at DESC);

-- Cron-style schedules for proactive triggers: a trigger has EITHER schedule_minutes (every N
-- minutes) OR cron (5-field crontab evaluated in `timezone`), never both.
ALTER TABLE proactive_triggers ADD COLUMN IF NOT EXISTS cron TEXT;
ALTER TABLE proactive_triggers ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE proactive_triggers ALTER COLUMN schedule_minutes DROP NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'proactive_triggers_one_schedule') THEN
        ALTER TABLE proactive_triggers ADD CONSTRAINT proactive_triggers_one_schedule
            CHECK ((schedule_minutes IS NOT NULL) <> (cron IS NOT NULL));
    END IF;
END
$$;

-- Dashboard-editable settings (a dashboard value wins over .env). Secret values are Fernet-encrypted.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    is_secret BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Folder ingest (core/folder_ingest.py): documents that came from a folder the owner granted for knowledge, and the
-- owner's approval of the first sync of each grant. Existing installs apply this file again (it is idempotent).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_grant TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_path TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_hash TEXT;
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source_grant, source_path);
CREATE TABLE IF NOT EXISTS folder_ingest_approvals (
    grant_name TEXT PRIMARY KEY,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_files INTEGER
);

-- People and personal keys (core/users.py, core/permissions.py). The shared RAGLEAP_API_KEY still works and counts as
-- the owner. A key is shown once; only its sha256 hash is stored. org_id is empty today (reserved for multi-tenant).
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'admin', 'owner')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS users_name_lower_idx ON users (lower(name));
CREATE TABLE IF NOT EXISTS user_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
