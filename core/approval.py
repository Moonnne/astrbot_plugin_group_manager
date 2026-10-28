"""Expiring join requests and a single review snapshot per group."""

import uuid
from dataclasses import asdict

from ..models.join_request import JoinRequest
from ..models.review_snapshot import ReviewSnapshot
from ..utils.errors import error_detail
from ..utils.scope import scope_key


def decision(settings: dict, flags: dict) -> str:
    if not settings["enabled"]:
        return "ignore"
    if flags["white"]:
        return "approve"
    if flags["black"] or flags["public"]:
        return "reject" if settings["skip_ban"] else "manual"
    return "approve" if settings["auto_approve"] else "manual"


class Approval:
    MAX_REQUESTS = 2048
    MAX_PER_GROUP = 128

    def __init__(self, controller):
        self.c = controller
        self.requests = {}
        self.snapshots = {}

    def prune(self):
        now = self.c.clock()
        self.requests = {
            key: row for key, row in self.requests.items() if row.expires_at > now
        }
        self.snapshots = {
            key: row
            for key, row in self.snapshots.items()
            if row.expires_at > now
            and any(value in self.requests for value in row.items.values())
        }

    def clear_group(self, scope):
        self.requests = {
            key: row for key, row in self.requests.items() if row.scope != scope
        }
        self.snapshots.pop(scope_key(scope), None)

    def latest(self, scope, qq):
        return next(
            (
                r
                for r in reversed(list(self.requests.values()))
                if r.scope == scope and r.qq == qq
            ),
            None,
        )

    async def receive(self, event):
        scope = event.scope
        settings = await self.c.settings.get(scope)
        if not settings["enabled"]:
            return None
        qq, flag = event.member, event.request_id
        if (
            not self.c.api.for_scope(scope).valid_user(qq)
            or qq == scope.bot
            or not flag
        ):
            return None
        self.prune()
        if any(r.scope == scope and r.flag == flag for r in self.requests.values()):
            return None
        if (
            len(self.requests) >= self.MAX_REQUESTS
            or sum(r.scope == scope for r in self.requests.values())
            >= self.MAX_PER_GROUP
        ):
            self.c.log.warning("Request cache full: scope=%s", scope_key(scope))
            return None
        now = self.c.clock()
        row = JoinRequest(
            uuid.uuid4().hex,
            scope,
            qq,
            flag,
            event.subtype,
            event.reason,
            now,
            now + 1800,
            event.inviter,
            method=event.method,
        )
        self.requests[row.id] = row
        flags = await self.c.db.flags(scope, qq)
        action = decision(settings, flags)
        if event.approved:
            row.status, row.actor, row.decided_at = "approved", scope.bot, now
        elif action in {"approve", "reject"}:
            await self.act(scope, row, scope.bot, action == "approve")
        return row

    async def act(self, scope, row, actor, approve):
        if (
            self.requests.get(row.id) is not row
            or row.scope != scope
            or row.status != "pending"
            or row.expires_at <= self.c.clock()
        ):
            return None
        if (
            not await self.c.allows_scope(scope)
            or not (await self.c.settings.get(scope))["enabled"]
        ):
            return None
        row.status = "processing"
        row.actor = actor
        try:
            await self.c.api.for_scope(scope).approve(scope, asdict(row), approve)
            row.status = "approved" if approve else "rejected"
        except Exception as exc:
            row.status = "unknown"
            self.c.log.warning(
                "Approval outcome uncertain: scope=%s request=%s error=%s",
                scope_key(scope),
                row.id,
                error_detail(exc),
            )
        row.decided_at = self.c.clock() if row.status != "unknown" else None
        return row.status

    def select(self, scope, target):
        self.prune()
        if target:
            if target.startswith("r_"):
                row = self.requests.get(target[2:])
                return (
                    [row]
                    if row and row.scope == scope and row.status == "pending"
                    else [],
                    None,
                )
            snapshot = self.snapshots.get(scope_key(scope))
            if not snapshot or not snapshot.active:
                return [], None
            ids = (
                snapshot.items.values()
                if target == "all"
                else [snapshot.items.get(int(target))]
            )
            rows = [
                self.requests[key]
                for key in ids
                if key in self.requests and self.requests[key].status == "pending"
            ]
            return rows, None
        self.snapshots.pop(scope_key(scope), None)
        rows = sorted(
            (
                r
                for r in self.requests.values()
                if r.scope == scope and r.status == "pending"
            ),
            key=lambda r: r.received_at,
        )
        if len(rows) <= 1:
            return rows, None
        snapshot = ReviewSnapshot(uuid.uuid4().hex, self.c.clock() + 1800)
        self.snapshots[scope_key(scope)] = snapshot
        return rows, snapshot
