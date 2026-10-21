PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS group_settings(
 scope TEXT PRIMARY KEY, umo TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
 auto_approve INTEGER NOT NULL DEFAULT 0, skip_ban INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members(
 scope TEXT NOT NULL, qq TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '',
 presence TEXT NOT NULL DEFAULT 'unknown', first_join REAL, last_join REAL,
 last_leave REAL, leave_count INTEGER NOT NULL DEFAULT 0, session_id TEXT,
 source TEXT NOT NULL DEFAULT 'notice', updated_at REAL NOT NULL,
 PRIMARY KEY(scope,qq)
);
CREATE TABLE IF NOT EXISTS membership_sessions(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, qq TEXT NOT NULL,
 joined_at REAL NOT NULL, left_at REAL, method TEXT NOT NULL,
 reason TEXT NOT NULL, request_id INTEGER, exit_type TEXT,
 verification TEXT NOT NULL DEFAULT 'none'
);
CREATE TABLE IF NOT EXISTS membership_events(
 event_key TEXT PRIMARY KEY, scope TEXT NOT NULL, qq TEXT NOT NULL,
 kind TEXT NOT NULL, happened_at REAL NOT NULL, received_at REAL NOT NULL,
 operator TEXT NOT NULL, session_id TEXT
);
CREATE TABLE IF NOT EXISTS join_requests(
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, qq TEXT NOT NULL, inviter TEXT,
 flag TEXT NOT NULL, subtype TEXT NOT NULL, reason TEXT NOT NULL,
 happened_at REAL NOT NULL, received_at REAL NOT NULL, expires_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', actor TEXT, decided_at REAL,
 session_id TEXT, UNIQUE(scope,flag)
);
CREATE INDEX IF NOT EXISTS requests_pending ON join_requests(scope,status,expires_at);
CREATE TABLE IF NOT EXISTS review_snapshots(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, actor TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS snapshots_owner ON review_snapshots(scope,actor,created_at);
CREATE TABLE IF NOT EXISTS review_snapshot_items(
 snapshot_id TEXT NOT NULL REFERENCES review_snapshots(id), number INTEGER NOT NULL,
 request_id INTEGER NOT NULL REFERENCES join_requests(id),
 PRIMARY KEY(snapshot_id,number)
);
CREATE TABLE IF NOT EXISTS group_list_entries(
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, qq TEXT NOT NULL,
 kind TEXT NOT NULL, note TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL,
 revoked_at REAL, revoked_by TEXT
);
CREATE INDEX IF NOT EXISTS local_lists ON group_list_entries(scope,qq,kind,revoked_at);
CREATE TABLE IF NOT EXISTS public_blacklist_entries(
 id INTEGER PRIMARY KEY, qq TEXT NOT NULL, scope TEXT NOT NULL,
 note TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL,
 revoked_at REAL, revoked_by TEXT
);
CREATE INDEX IF NOT EXISTS public_lists ON public_blacklist_entries(qq,actor,revoked_at);
CREATE TABLE IF NOT EXISTS verification_challenges(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, qq TEXT NOT NULL,
 session_id TEXT NOT NULL UNIQUE, expression TEXT NOT NULL, answer INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'delivery', created_at REAL NOT NULL,
 sent_at REAL, deadline REAL, attempts INTEGER NOT NULL DEFAULT 0,
 delivery_attempts INTEGER NOT NULL DEFAULT 0, next_delivery REAL NOT NULL DEFAULT 0,
 message_id TEXT, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS challenge_due ON verification_challenges(state,deadline);
CREATE TABLE IF NOT EXISTS action_audit(
 id INTEGER PRIMARY KEY, action_key TEXT NOT NULL, scope TEXT NOT NULL,
 target TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 result TEXT NOT NULL, happened_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_key ON action_audit(action_key);
CREATE INDEX IF NOT EXISTS sessions_history ON membership_sessions(qq,scope,joined_at);
