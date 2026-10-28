-- name: begin_write
BEGIN IMMEDIATE;
-- name: begin_read
BEGIN;
-- name: checkpoint
PRAGMA wal_checkpoint(TRUNCATE);
-- name: member
SELECT * FROM group_members WHERE scope=:scope AND qq=:qq;
-- name: history_public
SELECT * FROM group_members WHERE qq=:qq AND scope<>:scope
AND ((:domain='' AND json_extract(scope,'$[1]') NOT LIKE 'qqofficial:%')
 OR (:domain<>'' AND json_extract(scope,'$[1]')=:domain))
ORDER BY updated_at DESC LIMIT :limit OFFSET :offset;
-- name: flags_local
SELECT * FROM group_list_entries WHERE scope=:scope AND qq=:qq AND revoked_at IS NULL;
-- name: flags_public
SELECT * FROM public_blacklist_entries WHERE qq=:qq AND revoked_at IS NULL ORDER BY created_at;
-- name: join_member
INSERT INTO group_members(scope,qq,presence,first_join,last_join,session_id,method,reason,updated_at)
VALUES(:scope,:qq,'present',:happened,:happened,:session,:method,:reason,:now)
ON CONFLICT(scope,qq) DO UPDATE SET
 presence='present',first_join=COALESCE(group_members.first_join,excluded.first_join),
 last_join=excluded.last_join,session_id=excluded.session_id,method=excluded.method,
 reason=excluded.reason,verification='none',updated_at=excluded.updated_at;
-- name: leave_member
INSERT INTO group_members(scope,qq,presence,last_leave,leave_count,updated_at)
VALUES(:scope,:qq,'absent',:happened,1,:now)
ON CONFLICT(scope,qq) DO UPDATE SET
 presence='absent',last_leave=excluded.last_leave,
 leave_count=group_members.leave_count+1,session_id=NULL,
 verification='left',updated_at=excluded.updated_at;
-- name: verification
UPDATE group_members SET verification=:state,updated_at=:now
WHERE scope=:scope AND qq=:qq AND session_id=:session;
-- name: snapshot_member
INSERT INTO group_members(scope,qq,nickname,presence,first_join,last_join,updated_at)
VALUES(:scope,:qq,:nickname,'present',:joined,:joined,:now)
ON CONFLICT(scope,qq) DO UPDATE SET
 nickname=excluded.nickname,presence='present',
 first_join=CASE WHEN group_members.first_join IS NULL THEN excluded.first_join
  WHEN excluded.first_join IS NULL THEN group_members.first_join
  ELSE MIN(group_members.first_join,excluded.first_join) END,
 last_join=CASE WHEN group_members.last_join IS NULL THEN excluded.last_join
  WHEN excluded.last_join IS NULL THEN group_members.last_join
  ELSE MAX(group_members.last_join,excluded.last_join) END,
 session_id=CASE WHEN group_members.presence='absent'
  OR excluded.last_join > group_members.last_join+2 THEN NULL ELSE group_members.session_id END,
 updated_at=excluded.updated_at;
-- name: add_local
INSERT INTO group_list_entries(scope,qq,kind,note,actor,created_at)
VALUES(:scope,:qq,:kind,:note,:actor,:now)
ON CONFLICT(scope,qq,kind) DO UPDATE SET
 note=excluded.note,actor=excluded.actor,created_at=excluded.created_at,
 revoked_at=NULL,revoked_by=NULL;
-- name: revoke_local
UPDATE group_list_entries SET revoked_at=:now,revoked_by=:actor
WHERE scope=:scope AND qq=:qq AND kind=:kind AND revoked_at IS NULL;
-- name: add_public
INSERT INTO public_blacklist_entries(qq,actor,scope,note,created_at)
VALUES(:qq,:actor,:scope,:note,:now)
ON CONFLICT(qq,actor) DO UPDATE SET
 scope=excluded.scope,note=excluded.note,created_at=excluded.created_at,
 revoked_at=NULL,revoked_by=NULL;
-- name: revoke_public
UPDATE public_blacklist_entries SET revoked_at=:now,revoked_by=:actor
WHERE qq=:qq AND revoked_at IS NULL AND (actor=:actor OR :super=1);
