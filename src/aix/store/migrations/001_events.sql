BEGIN;

CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  run_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  type TEXT NOT NULL,
  ts TEXT NOT NULL,
  payload TEXT NOT NULL,
  schema_version INTEGER NOT NULL
);
CREATE INDEX events_run ON events(run_id, seq);
CREATE INDEX events_type ON events(type, seq);

-- Events are append-only (PLAYBOOK §8.2).
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

PRAGMA user_version = 1;
COMMIT;
