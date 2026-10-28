"""Scoped orchestration, authorization, and bounded background work."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict

from ..models.scope import Scope
from ..utils.errors import error_detail
from ..utils.scope import group_umo, scope_key
from .approval import Approval
from .cards import Cards
from .commands import Commands, parse_command
from .members import Members
from .verification import Verification


class Runtime:
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
        self.commands, self.cards, self.members = (
            Commands(self),
            Cards(self),
            Members(self),
        )
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
        return (
            (
                group.group in self.enabled_groups
                or group_umo(group) in self.enabled_groups
            )
            if isinstance(group, Scope)
            else str(group) in self.enabled_groups
        )

    async def allows_scope(self, scope):
        return self.allows_group(scope) and (
            self.scope_guard is None or await self.scope_guard(scope)
        )

    async def start(self):
        self.stopping = False
        self.workers = [asyncio.create_task(self.work()) for _ in range(4)]
        self.timer = asyncio.create_task(self.run_timer())

    def enqueue(self, event):
        scope = event.scope
        if self.stopping or not self.allows_group(scope):
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.log.warning("Group event queue full: scope=%s", scope_key(scope))

    async def work(self):
        while True:
            event = await self.queue.get()
            try:
                await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(
                    "Group event failed: scope=%s error=%s",
                    scope_key(event.scope),
                    error_detail(exc),
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
            self.log.error("Background task failed: %s", error_detail(task.exception()))

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
                self.log.error("Verification scheduler failed: %s", error_detail(exc))
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

    async def execute(self, scope, actor, text, superuser=False):
        if self.stopping or not await self.allows_scope(scope):
            return
        cmd = parse_command(text, self.api.for_scope(scope).valid_user)
        if cmd:
            await self.commands.execute(scope, actor, cmd, superuser)

    async def handle(self, event):
        scope = event.scope
        if self.stopping or not await self.allows_scope(scope):
            return
        row = None
        async with self.lock(scope):
            key = hashlib.sha256(
                json.dumps(
                    [
                        scope_key(scope),
                        event.kind,
                        event.member,
                        event.happened,
                        event.request_id,
                        event.inviter,
                    ]
                ).encode()
            ).hexdigest()
            if self.seen.get(key, 0) > self.clock():
                return
            if event.kind == "request":
                row = await self.approval.receive(event)
            elif event.kind in {"join", "leave"}:
                await self.members.receive(event)
            self.seen[key] = self.clock() + 3600
            self.seen.move_to_end(key)
            while len(self.seen) > 8192:
                self.seen.popitem(last=False)
        if row:
            self.spawn(self.cards.request(scope, row))
