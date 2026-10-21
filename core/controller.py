"""Scoped orchestration, authorization, and bounded background work."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import asdict

from ..utils.scope import group_umo, scope_key
from .approval import Approval
from .rules import display_time, parse_command
from .verification import Verification


class Controller:
    MAX_TASKS = 64
    QUEUE_SIZE = 512

    def __init__(
        self,
        repository,
        gateway,
        renderer,
        settings,
        host_admin,
        logger,
        clock=time.time,
        *,
        enabled_groups=frozenset(),
        scope_guard=None,
    ):
        self.db, self.api, self.renderer, self.settings = (
            repository,
            gateway,
            renderer,
            settings,
        )
        self.host_admin, self.log, self.clock = host_admin, logger, clock
        self.enabled_groups = frozenset(enabled_groups)
        self.scope_guard = scope_guard
        # Fixed stripes avoid retaining one lock per historic group.
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.approval, self.verification = Approval(self), Verification(self)
        self.background = set()
        self.scheduled = set()
        self.workers = []
        self.queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self.seen = OrderedDict()
        self.stopping = False
        self.timer = None

    def lock(self, scope):
        return self.locks[hash(scope_key(scope)) % len(self.locks)]

    def allows_group(self, group):
        return str(group) in self.enabled_groups

    async def allows_scope(self, scope):
        return self.allows_group(scope.group) and (
            self.scope_guard is None or await self.scope_guard(scope)
        )

    async def start(self):
        self.stopping = False
        self.workers = [asyncio.create_task(self.work()) for _ in range(4)]
        self.timer = asyncio.create_task(self.run_timer())

    def enqueue(self, scope, raw):
        if self.stopping or not self.allows_group(scope.group):
            return
        try:
            self.queue.put_nowait((scope, raw))
        except asyncio.QueueFull:
            self.log.warning("Group event queue full: scope=%s", scope_key(scope))

    async def work(self):
        while True:
            scope, raw = await self.queue.get()
            try:
                await self.handle(scope, group_umo(scope), raw, "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(
                    "Group event failed: scope=%s error=%s",
                    scope_key(scope),
                    type(exc).__name__,
                )
            finally:
                self.queue.task_done()

    def spawn(self, coroutine):
        if self.stopping or len(self.background) >= self.MAX_TASKS:
            coroutine.close()
            self.log.warning("Background work rejected: capacity=%s", self.MAX_TASKS)
            return None
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
        tasks = [*self.background, *self.workers]
        if self.timer:
            tasks.append(self.timer)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.background.clear()
        self.workers.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.approval.requests.clear()
        self.approval.snapshots.clear()
        jobs = [
            (
                "verification",
                {
                    "scope": scope_key(row.scope),
                    "qq": row.qq,
                    "session": row.session,
                    "state": "interrupted",
                    "now": self.clock(),
                },
            )
            for row in self.verification.challenges.values()
        ]
        if jobs:
            await self.db.batch(jobs)
        self.verification.challenges.clear()
        self.scheduled.clear()
        self.seen.clear()
        if hasattr(self.api, "clear"):
            self.api.clear()

    async def run_timer(self):
        while not self.stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("Verification scheduler failed: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def tick(self):
        now = self.clock()
        if hasattr(self.api, "prune"):
            self.api.prune()
        self.approval.prune()
        while self.seen and next(iter(self.seen.values())) <= now:
            self.seen.popitem(last=False)
        for key, row in list(self.verification.challenges.items()):
            if key in self.scheduled:
                continue
            if row.created_at + 1800 <= now:
                async with self.lock(row.scope):
                    await self.verification.finish(row, "expired")
                continue
            if row.next_attempt > now:
                continue
            action = (
                self.verification.deliver
                if row.state == "delivery"
                else (
                    self.verification.expire
                    if row.state == "waiting" and row.deadline <= now
                    else None
                )
            )
            if action:
                task = self.spawn(action(row))
                if task:
                    self.scheduled.add(key)
                    task.add_done_callback(lambda _, k=key: self.scheduled.discard(k))

    async def handle(self, scope, umo, raw, actor, text="", superuser=False):
        if self.stopping or not await self.allows_scope(scope):
            return
        if raw.get("post_type") == "message":
            cmd = parse_command(text)
            if cmd:
                await self.command(scope, actor, cmd, superuser)
            return
        row = None
        async with self.lock(scope):
            # Stable wire fields only; the adapter generates random message IDs for notices.
            fields = [
                scope_key(scope),
                *(
                    raw.get(k)
                    for k in (
                        "post_type",
                        "request_type",
                        "notice_type",
                        "sub_type",
                        "user_id",
                        "operator_id",
                        "flag",
                        "time",
                    )
                ),
            ]
            key = hashlib.sha256(json.dumps(fields).encode()).hexdigest()
            if self.seen.get(key, 0) > self.clock():
                return
            if raw.get("post_type") == "request" and raw.get("request_type") == "group":
                row = await self.approval.receive(scope, raw)
            elif raw.get("post_type") == "notice" and raw.get("notice_type") in {
                "group_increase",
                "group_decrease",
            }:
                await self.verification.notice(scope, raw)
            self.seen[key] = self.clock() + 3600
            self.seen.move_to_end(key)
            while len(self.seen) > 8192:
                self.seen.popitem(last=False)
        if row:
            self.spawn(self.show_request(scope, row))

    async def disable(self, scope, actor):
        await self.settings.update(scope, actor, self.clock(), enabled=False)
        self.approval.clear_group(scope)
        for key, row in list(self.verification.challenges.items()):
            if row.scope == scope:
                await self.verification.finish(row, "cancelled")

    async def command(self, scope, actor, cmd, superuser=False):
        if not cmd or self.stopping or not await self.allows_scope(scope):
            return
        if cmd.verb == "help":
            image, plain = await self.renderer.help(self.clock(), group_umo(scope))
            await self.api.send(scope, "" if image else plain, image=image)
            return
        reply, review, rows, info = "", None, [], False
        async with self.lock(scope):
            settings = await self.settings.get(scope)
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
                    enabled = cmd.verb == "on"
                    if settings["enabled"] == enabled:
                        return
                    if enabled:
                        await self.settings.update(
                            scope, actor, self.clock(), enabled=True
                        )
                        self.spawn(self.sync_members(scope))
                    else:
                        await self.disable(scope, actor)
                    reply = "审批与入群验证已" + ("开启。" if enabled else "关闭。")
                elif cmd.verb in {"auto", "skip_ban"}:
                    field = "auto_approve" if cmd.verb == "auto" else "skip_ban"
                    await self.settings.update(
                        scope, actor, self.clock(), **{field: not settings[field]}
                    )
                    reply = ("自动审批" if cmd.verb == "auto" else "黑名单自动拒绝") + (
                        "已关闭。" if settings[field] else "已开启。"
                    )
                elif cmd.verb in {"ban", "wl", "pb", "rmb", "rmwl", "rmpb"}:
                    if await self.db.edit_list(
                        scope, actor, cmd, superuser, self.clock()
                    ):
                        reply = "名单已更新。"
                    if cmd.verb == "wl":
                        challenge = self.verification.challenges.get(
                            (scope_key(scope), cmd.target)
                        )
                        if challenge:
                            await self.verification.finish(challenge, "exempt")
                elif cmd.verb == "info":
                    info = True
                elif cmd.verb in {"y", "n"} and settings["enabled"]:
                    rows, review = self.approval.select(scope, cmd.target)
        if rows and not review:
            counts = {"approved": 0, "rejected": 0, "unknown": 0}
            for row in rows:
                async with self.lock(scope):
                    if not superuser and await self.api.role(scope, actor) not in {
                        "owner",
                        "admin",
                    }:
                        break
                    status = await self.approval.act(scope, row, actor, cmd.verb == "y")
                    if status:
                        counts[status] += 1
            if sum(counts.values()):
                reply = f"同意 {counts['approved']}，拒绝 {counts['rejected']}，结果待核实 {counts['unknown']}。"
        if reply:
            await self.api.send(scope, reply, mention=actor)
        if info:
            await self.show_info(scope, cmd.target)
        if review:
            for number, row in enumerate(rows, 1):
                if not await self.show_request(
                    scope, row, f"审批列表 · #{number}", review
                ):
                    return
                async with self.lock(scope):
                    if self.approval.snapshots.get(scope_key(scope)) is not review:
                        return
                    review.items[number] = row.id
            async with self.lock(scope):
                if (
                    self.approval.snapshots.get(scope_key(scope)) is review
                    and review.expires_at > self.clock()
                ):
                    review.active = True
                    await self.api.send(
                        scope,
                        "请使用 /g y [编号/all] 或 /g n [编号/all]。\n"
                        f"编号全群共用，截止 {display_time(review.expires_at)}；申请仍以各自的半小时期限为准。",
                    )

    async def show_request(self, scope, row, title="入群申请", review=None):
        profile = await self.api.profile(scope, row.qq)
        flags = await self.db.flags(scope, row.qq)
        member = await self.db.member(scope, row.qq)
        image, plain = await self.renderer.card(
            profile,
            row.qq,
            flags,
            member,
            [],
            asdict(row),
            title,
            self.clock(),
            group_umo(scope),
        )
        async with self.lock(scope):
            if (
                self.approval.requests.get(row.id) is not row
                or row.expires_at <= self.clock()
                or not (await self.settings.get(scope))["enabled"]
                or not await self.allows_scope(scope)
            ):
                return False
            if review and self.approval.snapshots.get(scope_key(scope)) is not review:
                return False
            await self.api.send(scope, "" if image else plain, image=image)
        return True

    async def show_info(self, scope, qq):
        profile = await self.api.profile(scope, qq)
        flags = await self.db.flags(scope, qq)
        member = await self.db.member(scope, qq)
        request = self.approval.latest(scope, qq)
        offset = 0
        while True:
            history = (
                await self.db.all(
                    "history_public",
                    {
                        "scope": scope_key(scope),
                        "qq": qq,
                        "limit": 5,
                        "offset": offset,
                    },
                )
                if flags["public"]
                else []
            )
            image, _ = await self.renderer.card(
                profile,
                qq,
                flags,
                member,
                history,
                asdict(request) if request else None,
                "成员记录",
                self.clock(),
                group_umo(scope),
            )
            current = await self.db.flags(scope, qq)
            if flags["public"] and not current["public"]:
                return
            if not image:
                self.log.warning(
                    "Member info render failed: scope=%s", scope_key(scope)
                )
                return
            if not await self.allows_scope(scope):
                return
            await self.api.send(scope, image=image)
            if len(history) < 5:
                break
            offset += 5

    async def sync_members(self, scope):
        if not self.allows_group(scope.group):
            return
        try:
            members = await self.api.call(
                scope, "get_group_member_list", group_id=int(scope.group)
            )
            async with self.lock(scope):
                jobs = []
                for member in members:
                    qq = str(member.get("user_id", ""))
                    if qq.isdigit():
                        jobs.append(
                            (
                                "snapshot_member",
                                {
                                    "scope": scope_key(scope),
                                    "qq": qq,
                                    "nickname": str(member.get("nickname") or qq)[:160],
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
                scope_key(scope),
                type(exc).__name__,
            )
