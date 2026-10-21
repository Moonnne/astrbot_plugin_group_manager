"""Scoped orchestration, authorization, and bounded background work."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import asdict

from ..utils.plugin_log import error_detail, event_summary, flag_ref
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
        allowed = self.allows_group(scope.group) and (
            self.scope_guard is None or await self.scope_guard(scope)
        )
        self.log.debug(
            "scope.check scope=%s group_selected=%s allowed=%s",
            scope_key(scope),
            self.allows_group(scope.group),
            allowed,
        )
        return allowed

    async def start(self):
        self.stopping = False
        self.workers = [asyncio.create_task(self.work()) for _ in range(4)]
        self.timer = asyncio.create_task(self.run_timer())
        self.log.info(
            "runtime.started workers=%s queue_limit=%s task_limit=%s",
            len(self.workers),
            self.QUEUE_SIZE,
            self.MAX_TASKS,
        )

    def enqueue(self, scope, raw):
        if self.stopping or not self.allows_group(scope.group):
            self.log.debug(
                "queue.rejected scope=%s stopping=%s group_selected=%s",
                scope_key(scope),
                self.stopping,
                self.allows_group(scope.group),
            )
            return
        try:
            self.queue.put_nowait((scope, raw))
            self.log.debug(
                "queue.enqueued scope=%s depth=%s event=%s",
                scope_key(scope),
                self.queue.qsize(),
                event_summary(raw),
            )
        except asyncio.QueueFull:
            self.log.warning("Group event queue full: scope=%s", scope_key(scope))

    async def work(self):
        while True:
            scope, raw = await self.queue.get()
            self.log.debug(
                "queue.dequeued scope=%s remaining=%s",
                scope_key(scope),
                self.queue.qsize(),
            )
            try:
                await self.handle(scope, group_umo(scope), raw, "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(
                    "Group event failed: scope=%s error=%s",
                    scope_key(scope),
                    error_detail(exc),
                )
            finally:
                self.queue.task_done()
                self.log.debug("queue.event_complete scope=%s", scope_key(scope))

    def spawn(self, coroutine):
        if self.stopping or len(self.background) >= self.MAX_TASKS:
            coroutine.close()
            self.log.warning("Background work rejected: capacity=%s", self.MAX_TASKS)
            return None
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self._completed)
        self.log.debug(
            "task.spawned name=%s pending=%s", task.get_name(), len(self.background)
        )
        return task

    def _completed(self, task):
        self.background.discard(task)
        if not task.cancelled() and task.exception():
            self.log.error("Background task failed: %s", error_detail(task.exception()))
        self.log.debug(
            "task.completed name=%s cancelled=%s pending=%s",
            task.get_name(),
            task.cancelled(),
            len(self.background),
        )

    async def stop(self):
        self.log.info(
            "runtime.stop_begin tasks=%s queue=%s requests=%s snapshots=%s challenges=%s",
            len(self.background),
            self.queue.qsize(),
            len(self.approval.requests),
            len(self.approval.snapshots),
            len(self.verification.challenges),
        )
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
        self.log.info("runtime.stop_complete")

    async def run_timer(self):
        while not self.stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("Verification scheduler failed: %s", error_detail(exc))
            await asyncio.sleep(5)

    async def tick(self):
        now = self.clock()
        if hasattr(self.api, "prune"):
            self.api.prune()
        self.approval.prune()
        if self.approval.requests or self.verification.challenges:
            self.log.debug(
                "timer.tick requests=%s snapshots=%s challenges=%s scheduled=%s",
                len(self.approval.requests),
                len(self.approval.snapshots),
                len(self.verification.challenges),
                len(self.scheduled),
            )
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
        self.log.debug(
            "flow.begin scope=%s stopping=%s event=%s",
            scope_key(scope),
            self.stopping,
            event_summary(raw),
        )
        if self.stopping or not await self.allows_scope(scope):
            self.log.info(
                "flow.ignored scope=%s reason=stopping_or_scope_denied",
                scope_key(scope),
            )
            return
        if raw.get("post_type") == "message":
            cmd = parse_command(text)
            self.log.debug(
                "command.parsed scope=%s actor=%s valid=%s verb=%s",
                scope_key(scope),
                actor,
                cmd is not None,
                cmd.verb if cmd else None,
            )
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
                self.log.debug(
                    "flow.duplicate scope=%s event_ref=%s", scope_key(scope), key[:12]
                )
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
            self.log.debug(
                "flow.state_updated scope=%s new_request=%s seen_size=%s",
                scope_key(scope),
                bool(row),
                len(self.seen),
            )
        if row:
            self.spawn(self.show_request(scope, row))

    async def disable(self, scope, actor):
        self.log.info("group.disable_begin scope=%s actor=%s", scope_key(scope), actor)
        await self.settings.update(scope, actor, self.clock(), enabled=False)
        self.approval.clear_group(scope)
        for key, row in list(self.verification.challenges.items()):
            if row.scope == scope:
                await self.verification.finish(row, "cancelled")
        self.log.info("group.disable_complete scope=%s", scope_key(scope))

    async def command(self, scope, actor, cmd, superuser=False):
        self.log.debug(
            "command.begin scope=%s actor=%s verb=%s superuser=%s",
            scope_key(scope),
            actor,
            cmd.verb if cmd else None,
            superuser,
        )
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
                self.log.debug(
                    "command.permission scope=%s actor=%s role=%s rank=%s required=%s",
                    scope_key(scope),
                    actor,
                    role,
                    rank,
                    required,
                )
                if rank < required:
                    self.log.info(
                        "command.ignored scope=%s verb=%s reason=insufficient_permission",
                        scope_key(scope),
                        cmd.verb,
                    )
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
                        "已关闭" if settings[field] else "已开启"
                    )
                elif cmd.verb in {"ban", "wl", "pb", "rmb", "rmwl", "rmpb"}:
                    if await self.db.edit_list(
                        scope, actor, cmd, superuser, self.clock()
                    ):
                        reply = "操作完成，数据库已更新"
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
                    self.log.info(
                        "approval.selection scope=%s verb=%s target=%s rows=%s snapshot=%s",
                        scope_key(scope),
                        cmd.verb,
                        cmd.target,
                        len(rows),
                        bool(review),
                    )
                elif cmd.verb in {"y", "n"}:
                    self.log.info(
                        "approval.selection_ignored scope=%s reason=group_disabled",
                        scope_key(scope),
                    )
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
        self.log.debug(
            "card.request_begin scope=%s request=%s flag_ref=%s status=%s",
            scope_key(scope),
            row.id,
            flag_ref(row.flag),
            row.status,
        )
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
                self.log.info(
                    "card.request_discarded scope=%s request=%s reason=stale_disabled_or_denied",
                    scope_key(scope),
                    row.id,
                )
                return False
            if review and self.approval.snapshots.get(scope_key(scope)) is not review:
                self.log.info(
                    "card.request_discarded scope=%s request=%s reason=snapshot_replaced",
                    scope_key(scope),
                    row.id,
                )
                return False
            await self.api.send(scope, "" if image else plain, image=image)
            self.log.info(
                "card.request_sent scope=%s request=%s image=%s",
                scope_key(scope),
                row.id,
                bool(image),
            )
        return True

    async def show_info(self, scope, qq):
        self.log.debug("card.info_begin scope=%s qq=%s", scope_key(scope), qq)
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
            self.log.debug(
                "card.info_page_sent scope=%s qq=%s offset=%s",
                scope_key(scope),
                qq,
                offset,
            )
            if len(history) < 5:
                break
            offset += 5

    async def sync_members(self, scope):
        self.log.debug("members.sync_begin scope=%s", scope_key(scope))
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
                self.log.info(
                    "members.sync_complete scope=%s imported=%s",
                    scope_key(scope),
                    len(jobs),
                )
        except Exception as exc:
            self.log.warning(
                "Member snapshot failed: scope=%s error=%s",
                scope_key(scope),
                error_detail(exc),
            )
