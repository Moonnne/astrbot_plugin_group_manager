"""Member aggregates and bounded verification lifetimes."""

import uuid

from ..models.challenge import Challenge
from ..utils.scope import group_umo, scope_key
from .rules import arithmetic


class Verification:
    MAX_CHALLENGES = 2048
    MAX_PER_GROUP = 128

    def __init__(self, controller):
        self.c = controller
        self.challenges = {}

    async def notice(self, scope, raw):
        qq = str(raw.get("user_id", ""))
        if not qq.isdigit():
            return
        now = self.c.clock()
        happened = float(raw.get("time") or now)
        member = await self.c.db.member(scope, qq)
        if (
            member
            and max(member["last_join"] or 0, member["last_leave"] or 0) > happened
        ):
            return
        key = (scope_key(scope), qq)
        if raw["notice_type"] == "group_increase":
            if (
                member
                and member["session_id"]
                and happened <= (member["last_join"] or 0)
            ):
                return
            self.challenges.pop(key, None)
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
            method = "申请入群"
            if raw.get("sub_type") == "invite":
                inviter = str(raw.get("operator_id") or "")
                method = f"QQ {inviter} 邀请" if inviter else "邀请入群"
            await self.c.db.execute(
                "join_member",
                {
                    "scope": scope_key(scope),
                    "qq": qq,
                    "happened": happened,
                    "session": session,
                    "method": method,
                    "reason": request.reason if request else "",
                    "now": now,
                },
            )
            if request:
                request.status = "joined"
            settings = await self.c.settings.get(scope)
            if not settings["enabled"] or qq == scope.bot:
                return
            if await self.exempt(scope, qq) is True:
                await self.c.db.verification(scope, qq, session, "exempt", now)
                return
            if (
                len(self.challenges) >= self.MAX_CHALLENGES
                or sum(c.scope == scope for c in self.challenges.values())
                >= self.MAX_PER_GROUP
            ):
                self.c.log.warning(
                    "Verification cache full: scope=%s", scope_key(scope)
                )
                return
            expression, answer = arithmetic()
            challenge = Challenge(scope, qq, session, expression, answer, now)
            self.challenges[key] = challenge
            self.c.spawn(self.deliver(challenge))
        else:
            if member and member["presence"] == "absent":
                return
            self.challenges.pop(key, None)
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
                await self.c.disable(scope, scope.bot)

    async def exempt(self, scope, qq):
        if (
            qq == scope.bot
            or self.c.host_admin(qq, group_umo(scope))
            or (await self.c.db.flags(scope, qq))["white"]
        ):
            return True
        try:
            return await self.c.api.role(scope, qq) in {"owner", "admin"}
        except Exception:
            return None

    async def finish(self, row, state):
        key = (scope_key(row.scope), row.qq)
        if self.challenges.get(key) is row:
            self.challenges.pop(key)
        await self.c.db.verification(
            row.scope, row.qq, row.session, state, self.c.clock()
        )

    async def current(self, row):
        if self.challenges.get((scope_key(row.scope), row.qq)) is not row:
            return False
        if (
            not await self.c.allows_scope(row.scope)
            or not (await self.c.settings.get(row.scope))["enabled"]
        ):
            await self.finish(row, "cancelled")
            return False
        member = await self.c.db.member(row.scope, row.qq)
        if (
            not member
            or member["session_id"] != row.session
            or member["presence"] != "present"
        ):
            await self.finish(row, "cancelled")
            return False
        return True

    async def answer(self, scope, qq, value):
        row = self.challenges.get((scope_key(scope), qq))
        if not row or row.state != "waiting" or row.deadline <= self.c.clock():
            return False
        if not await self.current(row) or int(value) != row.answer:
            return False
        await self.finish(row, "passed")
        return True

    async def deliver(self, row):
        scope = row.scope
        async with self.c.lock(scope):
            if row.state != "delivery" or not await self.current(row):
                return
            exempt = await self.exempt(scope, row.qq)
            if exempt is True:
                await self.finish(row, "exempt")
                return
            row.next_attempt = self.c.clock() + 30
            if exempt is None:
                return
            row.state = "rendering"
            row.attempts += 1
        try:
            image = await self.c.renderer.formula(row.expression, group_umo(scope))
            if not image:
                raise RuntimeError("Formula renderer returned no image")
            async with self.c.lock(scope):
                if not await self.current(row):
                    return
                exempt = await self.exempt(scope, row.qq)
                if exempt is True:
                    await self.finish(row, "exempt")
                    return
                if exempt is None:
                    row.state = "delivery"
                    return
                # The deadline starts only after a successful image send.
                await self.c.api.send(
                    scope,
                    " 请在本题发送后 10 分钟内使用 /ans [答案] 完成验证，否则将被移出群聊。",
                    image=image,
                    mention=row.qq,
                )
                row.state = "waiting"
                row.deadline = self.c.clock() + 600
                await self.c.db.verification(
                    scope, row.qq, row.session, "waiting", self.c.clock()
                )
        except Exception as exc:
            async with self.c.lock(scope):
                if self.challenges.get((scope_key(scope), row.qq)) is row:
                    if row.attempts >= 3:
                        await self.finish(row, "delivery_failed")
                    else:
                        row.state = "delivery"
                        row.next_attempt = self.c.clock() + 30
            self.c.log.warning(
                "Challenge delivery failed: scope=%s error=%s",
                scope_key(scope),
                type(exc).__name__,
            )

    async def expire(self, row):
        scope = row.scope
        async with self.c.lock(scope):
            if (
                row.state != "waiting"
                or row.deadline > self.c.clock()
                or not await self.current(row)
            ):
                return
            row.next_attempt = self.c.clock() + 30
            exempt = await self.exempt(scope, row.qq)
            if exempt is True:
                await self.finish(row, "exempt")
                return
            if exempt is None:
                return
            try:
                members = await self.c.api.call(
                    scope, "get_group_member_list", group_id=int(scope.group)
                )
                target = next(
                    (m for m in members if str(m.get("user_id")) == row.qq), None
                )
                if target is None:
                    await self.finish(row, "absent")
                    return
                if target.get("role") in {"owner", "admin"}:
                    await self.finish(row, "exempt")
                    return
                member = await self.c.db.member(scope, row.qq)
                if float(target.get("join_time") or 0) > member["last_join"] + 2:
                    await self.finish(row, "stale")
                    return
                if await self.c.api.role(scope, scope.bot) not in {"owner", "admin"}:
                    await self.finish(row, "no_permission")
                    return
            except Exception:
                return
            # Remove the timer before issuing a non-idempotent action.
            await self.finish(row, "unknown")
            state = "removed"
            try:
                await self.c.api.kick(scope, row.qq)
            except Exception as exc:
                state = "unknown"
                self.c.log.warning(
                    "Kick outcome uncertain: scope=%s error=%s",
                    scope_key(scope),
                    type(exc).__name__,
                )
            await self.c.db.verification(
                scope, row.qq, row.session, state, self.c.clock()
            )
