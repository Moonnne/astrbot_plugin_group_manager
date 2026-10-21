"""Event orchestration, local authorization, and owned command handling."""

import asyncio
import time
from collections import defaultdict

from ..models.domain import Scope, display_time, parse_command
from .approval import Approval
from .verification import Verification


class Controller:
    def __init__(
        self,
        repository,
        gateway,
        renderer,
        host_admin,
        logger,
        clock=time.time,
        *,
        enabled_groups=frozenset(),
    ):
        self.db, self.api, self.renderer = repository, gateway, renderer
        self.host_admin, self.log, self.clock = host_admin, logger, clock
        self.enabled_groups = frozenset(enabled_groups)
        self.locks = defaultdict(asyncio.Lock)
        self.approval = Approval(repository, gateway, clock, logger)
        self.verification = Verification(self)
        self.stopping = False
        self.background = set()
        self.timer = None
        self.scheduled = set()

    def allows_group(self, group):
        return group in self.enabled_groups

    async def start(self):
        for row in await self.db.all("groups"):
            scope = Scope.parse(row["scope"])
            if not self.allows_group(scope.group):
                await self.disable(scope, scope.bot)
        await self.db.batch(
            [
                ("recover_requests", {}),
                ("recover_challenges", {"now": self.clock()}),
                ("expire_requests", {"now": self.clock()}),
            ]
        )
        self.timer = asyncio.create_task(self.run_timer())

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self._completed)
        return task

    def _completed(self, task):
        self.background.discard(task)
        if not task.cancelled() and task.exception():
            self.log.error(
                "Background task failed: %s", type(task.exception()).__name__
            )

    async def stop(self):
        self.stopping = True
        tasks = list(self.background)
        if self.timer:
            tasks.append(self.timer)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_timer(self):
        while not self.stopping:
            try:
                await self.db.execute("expire_requests", {"now": self.clock()})
                for row in await self.db.all("active_challenges"):
                    if not self.allows_group(Scope.parse(row["scope"]).group):
                        continue
                    if row["id"] in self.scheduled:
                        continue
                    if (
                        row["state"] == "delivery"
                        and row["next_delivery"] <= self.clock()
                    ):
                        action = self.verification.deliver
                    elif row["state"] == "waiting" and row["deadline"] <= self.clock():
                        action = self.verification.expire
                    else:
                        continue
                    self.scheduled.add(row["id"])
                    task = self.spawn(action(row))
                    task.add_done_callback(
                        lambda _, key=row["id"]: self.scheduled.discard(key)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("Verification scheduler failed: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def handle(self, scope, umo, raw, actor, text="", superuser=False):
        if self.stopping or not self.allows_group(scope.group):
            return
        p = {"scope": scope.key, "umo": umo, "now": self.clock(), "actor": actor}
        await self.db.execute("ensure_group", p)
        post = raw.get("post_type")
        if post == "message":
            cmd = parse_command(text)
            if cmd:
                await self.command(scope, actor, cmd, superuser)
            return
        result = None
        async with self.locks[scope.key]:
            if post == "request" and raw.get("request_type") == "group":
                result = await self.approval.receive(scope, raw)
            elif post == "notice" and raw.get("notice_type") in {
                "group_increase",
                "group_decrease",
            }:
                await self.verification.notice(scope, raw)
        if result:
            request_id, revision = result
            await self.show_request(scope, request_id, revision)

    async def disable(self, scope, actor):
        p = {"scope": scope.key, "actor": actor, "now": self.clock(), "value": 0}
        await self.db.batch(
            [
                ("set_enabled", p),
                ("abandon_requests", p),
                ("cancel_group_sessions", p),
                ("cancel_challenges", p),
                ("invalidate_group_snapshots", p),
            ]
        )

    async def command(self, scope, actor, cmd, superuser):
        """Apply an authorized command while allowing cancellation between actions.

        Args:
            scope: Original platform, bot, and group identity.
            actor: QQ account issuing the command.
            cmd: Parsed command with validated arguments.
            superuser: Host administrator status from the original event.
        """
        if not self.allows_group(scope.group):
            return
        if cmd.verb == "help":
            settings = await self.db.one("settings", {"scope": scope.key})
            image, plain = await self.renderer.help(self.clock(), settings["umo"])
            await self.api.send(scope, "" if image else plain, image=image)
            return
        review = None
        decisions = None
        show_info = None
        reply = ""
        async with self.locks[scope.key]:
            p = {
                "scope": scope.key,
                "actor": actor,
                "now": self.clock(),
                "qq": cmd.target,
            }
            settings = await self.db.one("settings", p)
            if cmd.verb == "ans":
                if settings["enabled"] and await self.verification.answer(
                    scope, actor, cmd.target
                ):
                    reply = "验证通过。"
            else:
                role = "owner" if superuser else await self.api.role(scope, actor)
                rank = {"owner": 2, "admin": 1}.get(role, 0)
                required = (
                    2
                    if cmd.verb in {"ban", "wl", "pb", "rmb", "rmwl", "skip_ban"}
                    else 1
                )
                if cmd.verb == "rmpb":
                    required = 0
                if rank < required:
                    return
                if cmd.verb in {"on", "off"}:
                    if settings["enabled"] == int(cmd.verb == "on"):
                        return
                    if cmd.verb == "off":
                        await self.disable(scope, actor)
                    else:
                        await self.db.execute("set_enabled", {**p, "value": 1})
                    reply = "审批与入群验证已" + (
                        "开启。" if cmd.verb == "on" else "关闭。"
                    )
                    if cmd.verb == "on":
                        self.spawn(self.sync_members(scope))
                elif cmd.verb in {"auto", "skip_ban"}:
                    await self.db.execute(
                        "toggle_auto" if cmd.verb == "auto" else "toggle_skip", p
                    )
                    field = "auto_approve" if cmd.verb == "auto" else "skip_ban"
                    reply = ("自动审批" if cmd.verb == "auto" else "黑名单自动拒绝") + (
                        "已关闭。" if settings[field] else "已开启。"
                    )
                elif cmd.verb in {"ban", "wl", "pb", "rmb", "rmwl", "rmpb"}:
                    changed = await self.edit_list(scope, actor, cmd, superuser)
                    if changed:
                        reply = "名单已更新。"
                elif cmd.verb == "info":
                    show_info = cmd.target
                elif cmd.verb in {"y", "n"} and settings["enabled"]:
                    rows, snapshot = await self.approval.select(
                        scope, actor, cmd.target
                    )
                    if snapshot:
                        review = (rows, snapshot, settings["revision"])
                    elif rows:
                        decisions = rows
                if reply:
                    await self.db.execute(
                        "audit",
                        {
                            **p,
                            "key": f"command:{scope.key}:{actor}:{self.clock()}",
                            "action": cmd.verb,
                            "result": "completed",
                        },
                    )
        if decisions:
            counts = {"approved": 0, "rejected": 0, "unknown": 0}
            for row in decisions:
                async with self.locks[scope.key]:
                    if not superuser and await self.api.role(scope, actor) not in {
                        "owner",
                        "admin",
                    }:
                        break
                    status = await self.approval.act(
                        scope, row["id"], actor, cmd.verb == "y"
                    )
                    if status:
                        counts[status] += 1
            if sum(counts.values()):
                reply = f"同意 {counts['approved']}，拒绝 {counts['rejected']}，结果待核实 {counts['unknown']}。"
        if reply:
            await self.api.send(scope, reply, mention=actor)
        if show_info:
            await self.show_info(scope, show_info)
        if review:
            rows, snapshot, revision = review
            for row in rows:
                if not await self.show_request(
                    scope,
                    row["id"],
                    revision,
                    f"审批列表 · 操作人 {actor} · #{row['number']}",
                    (snapshot, actor),
                ):
                    return
            p = {
                "id": snapshot,
                "scope": scope.key,
                "actor": actor,
                "now": self.clock(),
                "revision": revision,
            }
            async with self.locks[scope.key]:
                if await self.db.execute("activate_snapshot", p):
                    snapshot_row = await self.db.one("snapshot", p)
                    await self.api.send(
                        scope,
                        f"请使用 /g y [编号/all] 或 /g n [编号/all]。\n"
                        f"编号仅对你有效，截止 {display_time(snapshot_row['expires_at'])}"
                        "（每条申请仍以卡片上的接管截止时间为准）。",
                        mention=actor,
                    )

    async def edit_list(self, scope, actor, cmd, superuser):
        now = self.clock()
        p = {
            "scope": scope.key,
            "actor": actor,
            "qq": cmd.target,
            "now": now,
            "note": cmd.note,
            "super": int(superuser),
            "kind": "white" if cmd.verb in {"wl", "rmwl"} else "black",
        }
        jobs = []
        if cmd.verb in {"pb", "rmpb"}:
            if cmd.verb == "pb":
                jobs = [("revoke_public", {**p, "super": 0}), ("add_public", p)]
            else:
                jobs = [("revoke_public", p)]
        else:
            jobs = [("revoke_local", p)]
            if cmd.verb in {"ban", "wl"}:
                jobs.append(("add_local", p))
        if cmd.verb == "wl":
            jobs.append(("cancel_member_challenge", {**p, "state": "exempt"}))
            member = await self.db.one("member", p)
            if member and member["session_id"]:
                jobs.append(
                    (
                        "session_verification",
                        {"session": member["session_id"], "state": "exempt"},
                    )
                )
        results = await self.db.batch(jobs)
        return any(result[1] > 0 for result in results)

    async def show_request(
        self, scope, request_id, revision, title="入群申请", review_token=None
    ):
        p = {"scope": scope.key, "id": request_id}
        row = await self.db.one("request", p)
        settings = await self.db.one("settings", p)
        if not settings["enabled"] or settings["revision"] != revision:
            return False
        profile = await self.api.profile(scope, row["qq"])
        flags = await self.db.flags(scope, row["qq"])
        member = await self.db.one("member", {**p, "qq": row["qq"]})
        row = await self.db.one("request", p)
        image, plain = await self.renderer.card(
            profile,
            row["qq"],
            flags,
            member,
            [],
            row,
            title,
            self.clock(),
            settings["umo"],
        )
        async with self.locks[scope.key]:
            settings = await self.db.one("settings", p)
            if not settings["enabled"] or settings["revision"] != revision:
                return False
            if review_token:
                latest = await self.db.one(
                    "latest_snapshot",
                    {
                        "scope": scope.key,
                        "actor": review_token[1],
                    },
                )
                if (
                    not latest
                    or latest["id"] != review_token[0]
                    or latest["expires_at"] <= self.clock()
                ):
                    return False
            await self.api.send(
                scope, (title if review_token else "") if image else plain, image=image
            )
        return True

    async def show_info(self, scope, qq):
        p = {"scope": scope.key, "qq": qq}
        settings = await self.db.one("settings", p)
        profile = await self.api.profile(scope, qq)
        flags = await self.db.flags(scope, qq)
        history = await self.db.all("history_local", p)
        if flags["public"]:
            history.extend(await self.db.all("history_public", p))
        member = await self.db.one("member", p)
        request = await self.db.one("latest_request", p)
        # Keep each image bounded; send long history as ordered pages.
        chunks = [history[i : i + 5] for i in range(0, len(history), 5)] or [[]]
        for index, chunk in enumerate(chunks, 1):
            title = f"成员记录 · {index}/{len(chunks)}"
            image, plain = await self.renderer.card(
                profile,
                qq,
                flags,
                member,
                chunk,
                request,
                title,
                self.clock(),
                settings["umo"],
            )
            # Public revocation during rendering must not leak another group's history.
            current = await self.db.flags(scope, qq)
            if flags["public"] and not current["public"]:
                return
            await self.api.send(scope, title if image else plain, image=image)

    async def sync_members(self, scope):
        if not self.allows_group(scope.group):
            return
        try:
            members = await self.api.call(
                scope, "get_group_member_list", group_id=int(scope.group)
            )
            async with self.locks[scope.key]:
                jobs = []
                for member in members:
                    qq = str(member.get("user_id", ""))
                    if qq.isdigit():
                        jobs.append(
                            (
                                "snapshot_member",
                                {
                                    "scope": scope.key,
                                    "qq": qq,
                                    "nickname": str(member.get("nickname") or qq),
                                    "joined": member.get("join_time") or None,
                                    "now": self.clock(),
                                },
                            )
                        )
                if jobs:
                    await self.db.batch(jobs)
        except Exception as exc:
            self.log.warning(
                "Member snapshot failed: scope=%s error=%s",
                scope.key,
                type(exc).__name__,
            )
