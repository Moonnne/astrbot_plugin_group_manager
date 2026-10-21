PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA user_version=1;
CREATE TABLE IF NOT EXISTS group_members(
 scope TEXT NOT NULL, qq TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '',
 presence TEXT NOT NULL DEFAULT 'unknown', first_join REAL, last_join REAL,
 last_leave REAL, leave_count INTEGER NOT NULL DEFAULT 0,
 session_id TEXT, method TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
 verification TEXT NOT NULL DEFAULT 'none', updated_at REAL NOT NULL,
 PRIMARY KEY(scope,qq)
);
CREATE INDEX IF NOT EXISTS members_account ON group_members(qq);
CREATE TABLE IF NOT EXISTS group_list_entries(
 scope TEXT NOT NULL, qq TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('black','white')),
 note TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL,
 revoked_at REAL, revoked_by TEXT,
 PRIMARY KEY(scope,qq,kind)
);
CREATE TABLE IF NOT EXISTS public_blacklist_entries(
 qq TEXT NOT NULL, actor TEXT NOT NULL, scope TEXT NOT NULL,
 note TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL, revoked_by TEXT,
 PRIMARY KEY(qq,actor)
);
