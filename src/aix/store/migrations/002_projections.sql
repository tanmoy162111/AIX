BEGIN;

-- Projections are rebuildable from `events` (PLAYBOOK §8.2). `data` holds the domain model as JSON;
-- the other columns exist for querying. `seq` is the seq of the event that created the row.

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  graph_id TEXT,
  data TEXT NOT NULL
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT NOT NULL,
  seq INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX tasks_run ON tasks(run_id, seq);

CREATE TABLE attempts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT,
  number INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL,
  seq INTEGER NOT NULL,
  data TEXT NOT NULL,
  result TEXT,
  verification TEXT
);
CREATE INDEX attempts_task ON attempts(task_id, number);

CREATE TABLE checks (
  id TEXT PRIMARY KEY,
  attempt_id TEXT,
  run_id TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  seq INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX checks_attempt ON checks(attempt_id, seq);

CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  point TEXT NOT NULL,
  subject TEXT NOT NULL,
  provider TEXT NOT NULL,
  outcome TEXT NOT NULL,
  seq INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX decisions_run ON decisions(run_id, seq);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  subject TEXT NOT NULL,
  status TEXT NOT NULL,
  seq INTEGER NOT NULL,
  data TEXT NOT NULL
);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  type TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  seq INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX artifacts_run ON artifacts(run_id, seq);

-- Populated from M7.6 (§22.3).
CREATE TABLE agent_stats (
  agent_id TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  task_type TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  accepted INTEGER NOT NULL DEFAULT 0,
  verification_passed INTEGER NOT NULL DEFAULT 0,
  retries INTEGER NOT NULL DEFAULT 0,
  human_interventions INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms_total INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, model, task_type)
);

PRAGMA user_version = 2;
COMMIT;
