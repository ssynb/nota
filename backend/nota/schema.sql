CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY,
  plan text NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro')),
  timezone text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recordings (
  id uuid PRIMARY KEY,
  user_id text NOT NULL REFERENCES users,
  kind text NOT NULL CHECK (kind IN ('audio', 'text')),
  text text,
  started_at timestamptz NOT NULL,
  total_chunks int,
  status text NOT NULL,
  ai text NOT NULL CHECK (ai IN ('enabled', 'paused_quota')),
  -- which ledger paused it: the day's recording count (at start) or speech minutes (at processing)
  pause_reason text CHECK (pause_reason IN ('recordings_today', 'minutes')),
  vad_segments jsonb,                -- VAD checkpoint: [[start, end], ...] seconds
  speech_seconds double precision,   -- what the minutes ledger is charged
  segments jsonb,                    -- ASR + speaker checkpoint
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recordings_paused ON recordings (user_id) WHERE ai = 'paused_quota';

CREATE TABLE IF NOT EXISTS chunks (
  recording_id uuid NOT NULL REFERENCES recordings ON DELETE CASCADE,
  seq int NOT NULL CHECK (seq >= 0),
  audio bytea NOT NULL,
  PRIMARY KEY (recording_id, seq)
);

-- Ledgers. A recording is charged at most once per ledger: recording_charges is keyed by recording.
CREATE TABLE IF NOT EXISTS daily_usage (
  user_id text NOT NULL, day date NOT NULL, used int NOT NULL,
  PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS monthly_usage (
  user_id text NOT NULL, period text NOT NULL,
  speech_seconds double precision NOT NULL DEFAULT 0,
  asks int NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, period)
);
CREATE TABLE IF NOT EXISTS recording_charges (
  recording_id uuid PRIMARY KEY REFERENCES recordings,
  user_id text NOT NULL,
  day date,                         -- set once the day's recording slot is taken
  period text,                      -- set once speech minutes are charged
  speech_seconds double precision,
  charged_at timestamptz,
  status text NOT NULL CHECK (status IN ('reserved', 'charged', 'refunded'))
);
CREATE INDEX IF NOT EXISTS recording_charges_window ON recording_charges (user_id, charged_at);

CREATE TABLE IF NOT EXISTS jobs (
  recording_id uuid PRIMARY KEY REFERENCES recordings,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
  attempts int NOT NULL DEFAULT 0,
  run_after timestamptz NOT NULL DEFAULT now(),
  owner uuid,
  lease_until timestamptz,
  last_error text
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (run_after) WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS conversations (
  id uuid PRIMARY KEY,
  recording_id uuid NOT NULL REFERENCES recordings,
  idx int NOT NULL,
  user_id text NOT NULL,
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL,
  speakers jsonb NOT NULL,
  lines jsonb NOT NULL,
  title text,
  overview text,
  summarized boolean NOT NULL DEFAULT false,
  agent_done boolean NOT NULL DEFAULT false,
  memory_done boolean NOT NULL DEFAULT false,
  UNIQUE (recording_id, idx)
);
CREATE INDEX IF NOT EXISTS conversations_user_time ON conversations (user_id, started_at);

CREATE TABLE IF NOT EXISTS facts (
  id bigserial PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES conversations ON DELETE CASCADE,
  text text NOT NULL,
  speaker_label text,
  offset_seconds double precision NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS todos (
  id uuid PRIMARY KEY,
  user_id text NOT NULL,
  conversation_id uuid REFERENCES conversations,
  text text NOT NULL,
  due_at timestamptz,
  done boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS actions (
  id uuid PRIMARY KEY,
  user_id text NOT NULL,
  conversation_id uuid REFERENCES conversations,
  idempotency_key text NOT NULL UNIQUE,
  type text NOT NULL CHECK (type IN ('create_calendar_event', 'delete_calendar_event', 'share_message')),
  args jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('pending_confirmation', 'pending', 'done', 'failed', 'rejected')),
  needs_confirmation boolean NOT NULL,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS actions_user_status ON actions (user_id, status);

CREATE TABLE IF NOT EXISTS profile_items (
  id uuid PRIMARY KEY,
  user_id text NOT NULL,
  category text NOT NULL,
  content text NOT NULL CHECK (char_length(content) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS voiceprints (
  user_id text PRIMARY KEY REFERENCES users,
  embedding real[] NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS asks (
  user_id text NOT NULL,
  idem_key text NOT NULL,
  period text NOT NULL,
  status text NOT NULL CHECK (status IN ('running', 'done', 'failed')),
  owner uuid,
  lease_until timestamptz,
  result jsonb,
  PRIMARY KEY (user_id, idem_key)
);

CREATE TABLE IF NOT EXISTS events (
  id bigserial PRIMARY KEY,
  user_id text NOT NULL,
  type text NOT NULL,
  data jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
