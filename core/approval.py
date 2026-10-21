"""Approval state transitions and per-operator review snapshots."""

import uuid

from ..models.domain import decision


class Approval:
    def __init__(self, repository, gateway, clock, logger):
        self.db, self.api, self.clock, self.log = repository, gateway, clock, logger

    async def receive(self, scope, raw):
        """Persist an identifiable human request and apply the arrival-time policy.

        Args:
            scope: Original bot and group identity.
            raw: OneBot group request with its opaque flag.

        Returns:
            The request ID and group revision, or None for ignored/duplicate events.
        """
        settings = await self.db.one("settings", {"scope": scope.key})
        if not settings["enabled"]:
            return None
        subtype = raw.get("sub_type")
        if subtype == "add":
            qq, inviter = str(raw.get("user_id", "")), ""
        elif subtype == "invite":
            qq, inviter = str(raw.get("invited_id", "")), str(raw.get("user_id", ""))
        else:
            return None
        flag = raw.get("flag")
        if not qq.isdigit() or qq == scope.bot or not isinstance(flag, str) or not flag:
            return None
        now = self.clock()
        p = {
            "scope": scope.key,
            "qq": qq,
            "inviter": inviter,
            "flag": flag,
            "subtype": subtype,
            "reason": str(raw.get("comment") or "")[:2000],
            "happened": float(raw.get("time") or now),
            "now": now,
            "expires": now + 1800,
        }
        if not await self.db.execute("add_request", p):
            return None
        row = await self.db.one("request_by_flag", p)
        action = decision(settings, await self.db.flags(scope, qq))
        if action in {"approve", "reject"}:
            await self.act(scope, row["id"], scope.bot, action == "approve")
        return row["id"], settings["revision"]

    async def act(self, scope, request_id, actor, approve):
        p = {"scope": scope.key, "id": request_id, "actor": actor, "now": self.clock()}
        if not await self.db.execute("claim_request", p):
            return False
        row = await self.db.one("request", p)
        status = "approved" if approve else "rejected"
        audit = {
            **p,
            "key": f"request:{request_id}",
            "qq": row["qq"],
            "action": status,
            "result": "dispatched",
        }
        await self.db.execute("audit", audit)
        try:
            await self.api.approve(scope, row, approve)
        except Exception as exc:
            status = "unknown"
            self.log.warning(
                "Approval outcome uncertain: scope=%s request=%s error=%s",
                scope.key,
                request_id,
                type(exc).__name__,
            )
        await self.db.batch(
            [
                ("finish_request", {**p, "status": status, "now": self.clock()}),
                ("audit", {**audit, "result": status, "now": self.clock()}),
            ]
        )
        return status

    async def select(self, scope, actor, target):
        p = {"scope": scope.key, "actor": actor, "now": self.clock()}
        if target:
            p["number"] = 0 if target == "all" else int(target)
            rows = await self.db.all("snapshot_items", p)
            return rows, None
        snapshot = uuid.uuid4().hex
        await self.db.batch(
            [
                ("invalidate_snapshots", p),
                ("create_snapshot", {**p, "id": snapshot, "expires": p["now"] + 1800}),
            ]
        )
        rows = await self.db.all("pending", p)
        if len(rows) <= 1:
            return rows, None
        jobs = []
        for number, row in enumerate(rows, 1):
            row["number"] = number
            jobs.append(
                (
                    "add_snapshot_item",
                    {
                        "snapshot": snapshot,
                        "number": number,
                        "request": row["id"],
                    },
                )
            )
        await self.db.batch(jobs)
        return rows, snapshot
