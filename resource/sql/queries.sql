-- name: begin
BEGIN IMMEDIATE
-- name: commit
COMMIT
-- name: rollback
ROLLBACK
-- name: checkpoint
PRAGMA wal_checkpoint(TRUNCATE)
-- name: version
SELECT version FROM schema_version
-- name: ensure_group
INSERT INTO group_settings(scope,umo,updated_at,updated_by) VALUES(:scope,:umo,:now,:actor)
ON CONFLICT(scope) DO UPDATE SET umo=excluded.umo
-- name: settings
SELECT * FROM group_settings WHERE scope=:scope
-- name: groups
SELECT * FROM group_settings
-- name: set_enabled
UPDATE group_settings SET enabled=:value,revision=revision+1,updated_at=:now,updated_by=:actor WHERE scope=:scope
-- name: toggle_auto
UPDATE group_settings SET auto_approve=1-auto_approve,updated_at=:now,updated_by=:actor WHERE scope=:scope
-- name: toggle_skip
UPDATE group_settings SET skip_ban=1-skip_ban,updated_at=:now,updated_by=:actor WHERE scope=:scope
-- name: abandon_requests
UPDATE join_requests SET status='abandoned' WHERE scope=:scope AND status='pending'
-- name: cancel_challenges
UPDATE verification_challenges SET state='cancelled',updated_at=:now WHERE scope=:scope AND state IN ('delivery','waiting','sending')
-- name: invalidate_group_snapshots
UPDATE review_snapshots SET active=0 WHERE scope=:scope
-- name: cancel_group_sessions
UPDATE membership_sessions SET verification='cancelled' WHERE id IN
(SELECT session_id FROM verification_challenges WHERE scope=:scope AND state IN ('delivery','sending','waiting'))
-- name: flags_local
SELECT * FROM group_list_entries WHERE scope=:scope AND qq=:qq AND revoked_at IS NULL ORDER BY created_at,id
-- name: flags_public
SELECT * FROM public_blacklist_entries WHERE qq=:qq AND revoked_at IS NULL ORDER BY created_at,id
-- name: revoke_local
UPDATE group_list_entries SET revoked_at=:now,revoked_by=:actor WHERE scope=:scope AND qq=:qq AND kind=:kind AND revoked_at IS NULL
-- name: add_local
INSERT INTO group_list_entries(scope,qq,kind,note,actor,created_at) VALUES(:scope,:qq,:kind,:note,:actor,:now)
-- name: revoke_public
UPDATE public_blacklist_entries SET revoked_at=:now,revoked_by=:actor WHERE qq=:qq AND revoked_at IS NULL AND (:super=1 OR actor=:actor)
-- name: add_public
INSERT INTO public_blacklist_entries(qq,scope,note,actor,created_at) VALUES(:qq,:scope,:note,:actor,:now)
-- name: add_request
INSERT OR IGNORE INTO join_requests(scope,qq,inviter,flag,subtype,reason,happened_at,received_at,expires_at)
VALUES(:scope,:qq,:inviter,:flag,:subtype,:reason,:happened,:now,:expires)
-- name: request_by_flag
SELECT * FROM join_requests WHERE scope=:scope AND flag=:flag
-- name: request
SELECT * FROM join_requests WHERE scope=:scope AND id=:id
-- name: latest_request
SELECT * FROM join_requests WHERE scope=:scope AND qq=:qq ORDER BY received_at DESC,id DESC LIMIT 1
-- name: pending
SELECT * FROM join_requests WHERE scope=:scope AND status='pending' AND expires_at>:now ORDER BY received_at,id
-- name: expire_requests
UPDATE join_requests SET status='expired' WHERE status='pending' AND expires_at<=:now
-- name: claim_request
UPDATE join_requests SET status='processing',actor=:actor,decided_at=NULL
WHERE id=:id AND scope=:scope AND status='pending' AND expires_at>:now
AND EXISTS(SELECT 1 FROM group_settings WHERE scope=:scope AND enabled=1)
-- name: finish_request
UPDATE join_requests SET status=:status,
decided_at=CASE WHEN :status IN ('approved','rejected') THEN :now ELSE NULL END
WHERE id=:id AND scope=:scope AND status='processing'
-- name: recover_requests
UPDATE join_requests SET status='unknown',decided_at=NULL WHERE status='processing'
-- name: join_candidates
SELECT * FROM join_requests WHERE scope=:scope AND qq=:qq AND session_id IS NULL
AND status IN ('approved','pending','unknown') AND received_at>=:since AND received_at<=:now ORDER BY received_at DESC
-- name: link_request
UPDATE join_requests SET session_id=:session,status='joined' WHERE id=:id AND scope=:scope
-- name: invalidate_snapshots
UPDATE review_snapshots SET active=0 WHERE scope=:scope AND actor=:actor
-- name: create_snapshot
INSERT INTO review_snapshots(id,scope,actor,created_at,expires_at) VALUES(:id,:scope,:actor,:now,:expires)
-- name: add_snapshot_item
INSERT INTO review_snapshot_items(snapshot_id,number,request_id) VALUES(:snapshot,:number,:request)
-- name: activate_snapshot
UPDATE review_snapshots SET active=1 WHERE id=:id AND expires_at>:now
AND id=(SELECT id FROM review_snapshots WHERE scope=:scope AND actor=:actor ORDER BY created_at DESC,rowid DESC LIMIT 1)
AND EXISTS(SELECT 1 FROM group_settings WHERE scope=:scope AND enabled=1 AND revision=:revision)
-- name: snapshot_items
SELECT r.*,i.number FROM review_snapshots s JOIN review_snapshot_items i ON s.id=i.snapshot_id
JOIN join_requests r ON r.id=i.request_id WHERE s.scope=:scope AND s.actor=:actor
AND s.active=1 AND s.expires_at>:now AND r.status='pending' AND r.expires_at>:now
AND (:number=0 OR i.number=:number) ORDER BY i.number
-- name: snapshot
SELECT * FROM review_snapshots WHERE id=:id
-- name: latest_snapshot
SELECT * FROM review_snapshots WHERE scope=:scope AND actor=:actor ORDER BY created_at DESC,rowid DESC LIMIT 1
-- name: member
SELECT * FROM group_members WHERE scope=:scope AND qq=:qq
-- name: event
SELECT 1 FROM membership_events WHERE event_key=:event_key
-- name: add_event
INSERT INTO membership_events(event_key,scope,qq,kind,happened_at,received_at,operator,session_id)
VALUES(:event_key,:scope,:qq,:kind,:happened,:now,:operator,:session)
-- name: join_member
INSERT INTO group_members(scope,qq,presence,first_join,last_join,session_id,updated_at)
VALUES(:scope,:qq,'present',:happened,:happened,:session,:now)
ON CONFLICT(scope,qq) DO UPDATE SET presence='present',
first_join=MIN(COALESCE(first_join,excluded.first_join),excluded.first_join),
last_join=excluded.last_join,session_id=excluded.session_id,updated_at=excluded.updated_at,source='notice'
-- name: add_session
INSERT INTO membership_sessions(id,scope,qq,joined_at,method,reason,request_id)
VALUES(:session,:scope,:qq,:happened,:method,:reason,:request_id)
-- name: leave_member
INSERT INTO group_members(scope,qq,presence,last_leave,leave_count,updated_at)
VALUES(:scope,:qq,'absent',:happened,1,:now)
ON CONFLICT(scope,qq) DO UPDATE SET presence='absent',last_leave=:happened,leave_count=leave_count+1,updated_at=:now
-- name: leave_session
UPDATE membership_sessions SET left_at=:happened,exit_type=:kind WHERE id=:session AND left_at IS NULL
-- name: supersede_session
UPDATE membership_sessions SET exit_type='unobserved',
verification=CASE WHEN verification IN ('none','waiting') THEN 'superseded' ELSE verification END
WHERE id=:old_session AND left_at IS NULL
-- name: history_local
SELECT * FROM membership_sessions WHERE scope=:scope AND qq=:qq ORDER BY joined_at DESC LIMIT 20
-- name: history_public
SELECT * FROM membership_sessions WHERE qq=:qq AND scope<>:scope ORDER BY joined_at DESC LIMIT 30
-- name: set_nickname
UPDATE group_members SET nickname=:nickname WHERE scope=:scope AND qq=:qq
-- name: snapshot_member
INSERT INTO group_members(scope,qq,nickname,presence,first_join,last_join,source,updated_at)
VALUES(:scope,:qq,:nickname,'present',:joined,:joined,'snapshot',:now)
ON CONFLICT(scope,qq) DO UPDATE SET nickname=:nickname
-- name: add_challenge
INSERT INTO verification_challenges(id,scope,qq,session_id,expression,answer,created_at,updated_at)
VALUES(:id,:scope,:qq,:session,:expression,:answer,:now,:now)
-- name: challenge
SELECT * FROM verification_challenges WHERE scope=:scope AND qq=:qq AND state IN ('delivery','sending','waiting')
ORDER BY created_at DESC LIMIT 1
-- name: challenge_id
SELECT * FROM verification_challenges WHERE id=:id
-- name: active_challenges
SELECT * FROM verification_challenges WHERE state IN ('delivery','waiting') ORDER BY created_at
-- name: begin_delivery
UPDATE verification_challenges SET state='sending',delivery_attempts=delivery_attempts+1,updated_at=:now
WHERE id=:id AND state='delivery'
-- name: delivered
UPDATE verification_challenges SET state='waiting',sent_at=:now,deadline=:deadline,message_id=:message,updated_at=:now
WHERE id=:id AND state='sending'
-- name: delivery_failed
UPDATE verification_challenges SET state=CASE WHEN delivery_attempts>=3 THEN 'delivery_failed' ELSE 'delivery' END,
next_delivery=:retry,updated_at=:now WHERE id=:id AND state='sending'
-- name: challenge_state
UPDATE verification_challenges SET state=:state,updated_at=:now WHERE id=:id AND state IN ('waiting','delivery','sending','kicking')
-- name: session_verification
UPDATE membership_sessions SET verification=:state WHERE id=:session
-- name: cancel_member_challenge
UPDATE verification_challenges SET state=:state,updated_at=:now WHERE scope=:scope AND qq=:qq AND state IN ('waiting','delivery','sending')
-- name: attempt
UPDATE verification_challenges SET attempts=attempts+1 WHERE id=:id AND state='waiting'
-- name: recover_challenges
UPDATE verification_challenges SET state=CASE WHEN state='kicking' THEN 'unknown' ELSE 'delivery' END,
deadline=NULL,delivery_attempts=0,next_delivery=0,updated_at=:now
WHERE state IN ('sending','kicking') OR (state='waiting' AND deadline<=:now)
-- name: audit
INSERT INTO action_audit(action_key,scope,target,actor,action,result,happened_at)
VALUES(:key,:scope,:qq,:actor,:action,:result,:now)
