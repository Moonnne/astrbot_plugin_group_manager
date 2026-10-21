"""Membership lifetimes and verification delivery/timeout transitions."""

import hashlib
import json
import uuid

from ..models.domain import Scope, arithmetic, display_time


class Verification:
    def __init__(self, controller):
        self.c = controller
        self.db, self.api, self.clock, self.log = (
            controller.db,
            controller.api,
            controller.clock,
            controller.log,
        )

    async def notice(self, scope, raw):
        """Record an observed membership transition without inventing offline history.

        Args:
            scope: Original bot and group identity.
            raw: OneBot notice with event time and participant identifiers.
        """
        qq = str(raw.get("user_id", ""))
        if not qq.isdigit():
            return
        now = self.clock()
        happened = float(raw.get("time") or now)
        kind = raw["notice_type"]
        member = await self.db.one("member", {"scope": scope.key, "qq": qq})
        signature = [
            scope.key,
            kind,
            raw.get("sub_type"),
            qq,
            raw.get("operator_id"),
            raw.get("time"),
            raw.get("message_id"),
        ]
        event_key = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
        if await self.db.one("event", {"event_key": event_key}):
            return
        # Ignore delayed events from before the current membership lifetime.
        if member and member["last_join"] and happened < member["last_join"]:
            return
        if kind == "group_increase":
            if (
                member
                and member["session_id"]
                and member["last_join"]
                and happened <= member["last_join"]
            ):
                return
            session = uuid.uuid4().hex
            p = {"scope": scope.key, "qq": qq, "since": happened - 1800, "now": now}
            candidates = await self.db.all("join_candidates", p)
            request = candidates[0] if len(candidates) == 1 else None
            p.update(
                {
                    "session": session,
                    "happened": happened,
                    "event_key": event_key,
                    "operator": str(raw.get("operator_id") or ""),
                    "kind": "join",
                    "method": {"approve": "审核加入", "invite": "邀请加入"}.get(
                        raw.get("sub_type"), "未知"
                    ),
                    "reason": request["reason"] if request else "",
                    "request_id": request["id"] if request else None,
                }
            )
            jobs = [("join_member", p), ("add_session", p), ("add_event", p)]
            if member and member["session_id"]:
                jobs.insert(
                    0, ("supersede_session", {**p, "old_session": member["session_id"]})
                )
                jobs.insert(
                    0, ("cancel_member_challenge", {**p, "state": "superseded"})
                )
            if request:
                jobs.append(("link_request", {**p, "id": request["id"]}))
            await self.db.batch(jobs)
            settings = await self.db.one("settings", p)
            if not settings["enabled"] or qq == scope.bot:
                return
            exempt = await self.exempt(scope, qq, settings)
            if exempt is True:
                await self.db.execute("session_verification", {**p, "state": "exempt"})
                return
            expression, answer = arithmetic()
            await self.db.execute(
                "add_challenge",
                {
                    **p,
                    "id": uuid.uuid4().hex,
                    "expression": expression,
                    "answer": answer,
                },
            )
        elif kind == "group_decrease":
            if member and member["presence"] == "absent":
                return
            session = member["session_id"] if member else None
            p = {
                "scope": scope.key,
                "qq": qq,
                "now": now,
                "happened": happened,
                "event_key": event_key,
                "session": session,
                "operator": str(raw.get("operator_id") or ""),
                "kind": str(raw.get("sub_type") or "leave"),
                "state": "left",
            }
            await self.db.batch(
                [
                    ("leave_member", p),
                    ("leave_session", p),
                    ("add_event", p),
                    ("cancel_member_challenge", p),
                ]
            )
            if qq == scope.bot:
                await self.c.disable(scope, scope.bot)

    async def exempt(self, scope, qq, settings):
        if qq == scope.bot or self.c.host_admin(qq, settings["umo"]):
            return True
        if (await self.db.flags(scope, qq))["white"]:
            return True
        try:
            return await self.api.role(scope, qq) in {"owner", "admin"}
        except Exception:
            # Unknown role is retried, never treated as permission to punish.
            return None

    async def answer(self, scope, qq, value):
        p = {"scope": scope.key, "qq": qq}
        row = await self.db.one("challenge", p)
        if not row or row["state"] != "waiting" or row["deadline"] <= self.clock():
            return False
        member = await self.db.one("member", p)
        if (
            not member
            or member["presence"] != "present"
            or member["session_id"] != row["session_id"]
        ):
            return False
        p.update({"id": row["id"], "now": self.clock(), "session": row["session_id"]})
        await self.db.execute("attempt", p)
        if int(value) != row["answer"]:
            return False
        await self.db.batch(
            [
                ("challenge_state", {**p, "state": "passed"}),
                ("session_verification", {**p, "state": "passed"}),
            ]
        )
        return True

    async def deliver(self, initial):
        """Render outside the group lock, then arm the deadline after a valid send.

        Args:
            initial: Persisted challenge selected by the scheduler.
        """
        scope = Scope.parse(initial["scope"])
        async with self.c.locks[scope.key]:
            row = await self.db.one("challenge_id", {"id": initial["id"]})
            settings = await self.db.one("settings", {"scope": scope.key})
            if (
                not row
                or row["state"] != "delivery"
                or row["next_delivery"] > self.clock()
            ):
                return
            if not settings["enabled"]:
                return
            member = await self.db.one("member", {"scope": scope.key, "qq": row["qq"]})
            if (
                not member
                or member["presence"] != "present"
                or member["session_id"] != row["session_id"]
            ):
                await self.finish(row, "cancelled")
                return
            exempt = await self.exempt(scope, row["qq"], settings)
            if exempt is True:
                await self.finish(row, "exempt")
                return
            if exempt is None:
                return
            await self.db.execute(
                "begin_delivery", {"id": row["id"], "now": self.clock()}
            )
        try:
            image = await self.c.renderer.formula(row["expression"], settings["umo"])
            async with self.c.locks[scope.key]:
                current = await self.db.one("challenge_id", {"id": row["id"]})
                if current["state"] != "sending":
                    return
                result = await self.api.send(
                    scope,
                    " 请在本题发送后 10 分钟内使用 /ans [答案] 完成验证。否则您将被移出此群聊。",
                    image=image,
                    mention=row["qq"],
                )
                now = self.clock()
                await self.db.execute(
                    "delivered",
                    {
                        "id": row["id"],
                        "now": now,
                        "deadline": now + 600,
                        "message": str((result or {}).get("message_id", "")),
                    },
                )
                await self.db.execute(
                    "session_verification",
                    {
                        "session": row["session_id"],
                        "state": "waiting",
                    },
                )
            await self.api.send(
                scope, f"验证截止：{display_time(now + 600)}", mention=row["qq"]
            )
        except Exception as exc:
            await self.db.execute(
                "delivery_failed",
                {
                    "id": row["id"],
                    "now": self.clock(),
                    "retry": self.clock() + 30,
                },
            )
            self.log.warning(
                "Challenge delivery failed: id=%s error=%s",
                row["id"],
                type(exc).__name__,
            )

    async def finish(self, row, state):
        await self.db.batch(
            [
                (
                    "challenge_state",
                    {"id": row["id"], "state": state, "now": self.clock()},
                ),
                (
                    "session_verification",
                    {"session": row["session_id"], "state": state},
                ),
            ]
        )

    async def expire(self, initial):
        """Revalidate a membership lifetime before dispatching one kick.

        Args:
            initial: Persisted challenge selected by the scheduler.
        """
        scope = Scope.parse(initial["scope"])
        async with self.c.locks[scope.key]:
            row = await self.db.one("challenge_id", {"id": initial["id"]})
            settings = await self.db.one("settings", {"scope": scope.key})
            if (
                not settings["enabled"]
                or row["state"] != "waiting"
                or row["deadline"] > self.clock()
            ):
                return
            member = await self.db.one("member", {"scope": scope.key, "qq": row["qq"]})
            if (
                not member
                or member["presence"] != "present"
                or member["session_id"] != row["session_id"]
            ):
                await self.finish(row, "cancelled")
                return
            exempt = await self.exempt(scope, row["qq"], settings)
            if exempt is True:
                await self.finish(row, "exempt")
                return
            if exempt is None:
                return
            try:
                # A fresh full list avoids fallback member-info objects for departed users.
                members = await self.api.call(
                    scope, "get_group_member_list", group_id=int(scope.group)
                )
                target = next(
                    (m for m in members if str(m.get("user_id")) == row["qq"]), None
                )
                if not target:
                    await self.finish(row, "absent")
                    return
                if target.get("role") in {"owner", "admin"}:
                    await self.finish(row, "exempt")
                    return
                joined = target.get("join_time") or 0
                if float(joined) > member["last_join"] + 2:
                    await self.finish(row, "stale")
                    return
                if await self.api.role(scope, scope.bot) not in {"owner", "admin"}:
                    await self.finish(row, "no_permission")
                    return
            except Exception:
                return
            await self.finish(row, "kicking")
            p = {
                "key": f"challenge:{row['id']}",
                "scope": scope.key,
                "qq": row["qq"],
                "actor": scope.bot,
                "action": "kick",
                "result": "dispatched",
                "now": self.clock(),
            }
            await self.db.execute("audit", p)
            state = "removed"
            try:
                await self.api.kick(scope, row["qq"])
            except Exception as exc:
                state = "unknown"
                self.log.warning(
                    "Kick outcome uncertain: id=%s error=%s",
                    row["id"],
                    type(exc).__name__,
                )
            await self.finish(row, state)
            await self.db.execute("audit", {**p, "result": state, "now": self.clock()})
