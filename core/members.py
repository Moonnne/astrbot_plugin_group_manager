"""Member aggregates and explicit platform membership changes."""

import uuid

from ..utils.errors import error_detail
from ..utils.scope import scope_key


class Members:
    def __init__(self, runtime):
        self.c = runtime

    async def sync(self, scope):
        if not self.c.allows_group(scope):
            return
        try:
            members = [
                member
                async for member in self.c.api.for_scope(scope).iter_members(scope)
            ]
            async with self.c.lock(scope):
                jobs = []
                for member in members:
                    qq = str(member.get("user_id", ""))
                    if self.c.api.for_scope(scope).valid_user(qq):
                        jobs.append(
                            (
                                "snapshot_member",
                                {
                                    "scope": scope_key(scope),
                                    "qq": qq,
                                    "nickname": str(member.get("nickname") or qq)[:160],
                                    "joined": member.get("join_time") or None,
                                    "now": self.c.clock(),
                                },
                            )
                        )
                if jobs:
                    await self.c.db.batch(jobs)
        except Exception as exc:
            self.c.log.warning(
                "Member snapshot failed: scope=%s error=%s",
                scope_key(scope),
                error_detail(exc),
            )

    async def receive(self, event):
        scope, qq = event.scope, event.member
        now = self.c.clock()
        happened = event.happened
        member = await self.c.db.member(scope, qq)
        if (
            member
            and max(member["last_join"] or 0, member["last_leave"] or 0) > happened
        ):
            return
        key = (scope_key(scope), qq)
        if event.kind == "join":
            if (
                member
                and member["session_id"]
                and happened <= (member["last_join"] or 0)
            ):
                return
            self.c.verification.challenges.pop(key, None)
            session = uuid.uuid4().hex
            candidates = [
                r
                for r in self.c.approval.requests.values()
                if r.scope == scope
                and r.qq == qq
                and r.expires_at > now
                and r.status in {"pending", "approved", "unknown"}
            ]
            request = candidates[0] if len(candidates) == 1 else None
            await self.c.db.execute(
                "join_member",
                {
                    "scope": scope_key(scope),
                    "qq": qq,
                    "happened": happened,
                    "session": session,
                    "method": request.method if request else event.method,
                    "reason": request.reason if request else "",
                    "now": now,
                },
            )
            if request:
                request.status = "joined"
            await self.c.verification.begin(scope, qq, session)
        else:
            if member and member["presence"] == "absent":
                return
            self.c.verification.challenges.pop(key, None)
            await self.c.db.execute(
                "leave_member",
                {
                    "scope": scope_key(scope),
                    "qq": qq,
                    "happened": happened,
                    "now": now,
                },
            )
            if qq == scope.bot:
                await self.c.commands.disable(scope, scope.bot)
