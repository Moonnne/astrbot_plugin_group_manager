"""Member aggregates and bounded verification lifetimes."""

import random

from ..models.action_card import ActionCard
from ..models.button import Button
from ..models.challenge import Challenge
from ..utils.errors import error_detail
from ..utils.scope import group_umo, scope_key


def arithmetic(rng=None) -> tuple[str, int]:
    """Generate a safe expression; never evaluate received text."""
    rng = rng or random.SystemRandom()
    a, b, c = (rng.randint(1, 9) for _ in range(3))
    kind = rng.randrange(4)
    if kind == 0:
        return f"{a} + {b}", a + b
    if kind == 1:
        a, b = max(a, b), min(a, b)
        return f"{a} - {b}", a - b
    if kind == 2:
        return rf"{a} \times {b}", a * b
    return rf"({a} + {b}) \times {c}", (a + b) * c


def answer_options(answer, rng=None):
    """Choose two distinct distractors without marking the correct option."""
    rng = rng or random.SystemRandom()
    pool = [
        value for value in range(max(0, answer - 9), answer + 10) if value != answer
    ]
    options = [answer, *rng.sample(pool, 2)]
    rng.shuffle(options)
    return tuple(options)


class Verification:
    MAX_CHALLENGES = 2048
    MAX_PER_GROUP = 128

    def __init__(self, controller):
        self.c = controller
        self.challenges = {}

    async def begin(self, scope, qq, session):
        settings = await self.c.settings.get(scope)
        if not settings["enabled"] or qq == scope.bot:
            return
        if await self.exempt(scope, qq) is True:
            await self.c.db.verification(scope, qq, session, "exempt", self.c.clock())
            return
        if (
            len(self.challenges) >= self.MAX_CHALLENGES
            or sum(c.scope == scope for c in self.challenges.values())
            >= self.MAX_PER_GROUP
        ):
            self.c.log.warning("Verification cache full: scope=%s", scope_key(scope))
            return
        expression, answer = arithmetic()
        challenge = Challenge(
            scope,
            qq,
            session,
            expression,
            answer,
            self.c.clock(),
            options=answer_options(answer),
        )
        self.challenges[(scope_key(scope), qq)] = challenge
        self.c.spawn(self.deliver(challenge))

    async def exempt(self, scope, qq):
        if (
            qq == scope.bot
            or self.c.host_admin(qq, group_umo(scope))
            or (await self.c.db.flags(scope, qq))["white"]
        ):
            return True
        try:
            role = await self.c.api.for_scope(scope).role(scope, qq)
            return role in {"owner", "admin"}
        except Exception as exc:
            self.c.log.warning(
                "verification.role_unknown scope=%s qq=%s error=%s",
                scope_key(scope),
                qq,
                error_detail(exc),
            )
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

    async def answer(self, scope, qq, value, context_id=""):
        row = self.challenges.get((scope_key(scope), qq))
        if not row or row.state != "waiting" or row.deadline <= self.c.clock():
            return False
        if context_id and row.session != context_id:
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
                # The deadline starts only after a successful question send.
                await self.c.api.for_scope(scope).send(
                    scope,
                    "请在本题发送后 10 分钟内使用 /ans [答案] 完成验证，否则将被移出群聊。",
                    image=image,
                    mention=row.qq,
                    card=ActionCard(
                        "请在本题发送后 10 分钟内点击正确答案，或使用 /ans [答案] 完成验证，否则将被移出群聊。",
                        tuple(
                            Button(str(value), f"/ans {value} v_{row.session}", row.qq)
                            for value in row.options
                        ),
                    ),
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
                error_detail(exc),
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
                members = [
                    member
                    async for member in self.c.api.for_scope(scope).iter_members(scope)
                ]
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
                if await self.c.api.for_scope(scope).role(scope, scope.bot) not in {
                    "owner",
                    "admin",
                }:
                    await self.finish(row, "no_permission")
                    return
            except Exception as exc:
                self.c.log.warning(
                    "verification.expiry_deferred scope=%s qq=%s error=%s",
                    scope_key(scope),
                    row.qq,
                    error_detail(exc),
                )
                return
            # Remove the timer before issuing a non-idempotent action.
            await self.finish(row, "unknown")
            state = "removed"
            try:
                await self.c.api.for_scope(scope).kick(scope, row.qq)
            except Exception as exc:
                state = "unknown"
                self.c.log.warning(
                    "Kick outcome uncertain: scope=%s error=%s",
                    scope_key(scope),
                    error_detail(exc),
                )
            await self.c.db.verification(
                scope, row.qq, row.session, state, self.c.clock()
            )
