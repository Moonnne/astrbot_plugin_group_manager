"""Translate AstrBot handlers into platform-neutral business method calls."""

import asyncio

from astrbot.api.event import filter

from ...utils.errors import error_detail
from ...utils.scope import scope_key
from ..commands import COMMAND


class ScopeFilter(filter.CustomFilter):
    runtime = None

    def filter(self, event, cfg):
        entry = self.runtime() if self.runtime else None
        return bool(entry and entry.accepts(event, message=True))


class GroupEventFilter(ScopeFilter):
    def filter(self, event, cfg):
        entry = self.runtime() if self.runtime else None
        return bool(entry and entry.accepts(event, message=False))


class GroupCommandFilter(ScopeFilter):
    def filter(self, event, cfg):
        return super().filter(event, cfg) and event.message_str.strip().removeprefix(
            "/"
        ) not in {"group", "g"}


class OwnedMessageFilter(ScopeFilter):
    def filter(self, event, cfg):
        if not super().filter(event, cfg):
            return False
        text = event.message_str.strip()
        if not COMMAND.match(text) or not (
            text.startswith("/") or event.is_at_or_wake_command
        ):
            return False
        parts = text.split()
        if event.is_at_or_wake_command and not text.startswith("/"):
            if parts[0] == "ans" or (
                len(parts) > 1
                and parts[1]
                in {
                    "help",
                    "on",
                    "off",
                    "y",
                    "n",
                    "auto",
                    "ban",
                    "rmb",
                    "wl",
                    "rmwl",
                    "pb",
                    "rmpb",
                    "info",
                }
            ):
                return False
        return True


class Handlers:
    def __init__(self, runtime, adapters, logger):
        self.c, self.adapters, self.log = runtime, adapters, logger
        self.stopped = False

    def accepts(self, event, *, message):
        if self.stopped or self.c.stopping or not event.get_group_id():
            return False
        adapter = self.adapters.for_event(event)
        if adapter is None:
            return False
        if not (adapter.is_message(event) if message else adapter.is_notice(event)):
            return False
        return self.c.allows_group(adapter.scope(event))

    async def message(self, event):
        if not self.accepts(event, message=True) or event.get_extra("monitor_handled"):
            return
        event.set_extra("monitor_handled", True)
        event.should_call_llm(True)
        event.stop_event()
        scope = self.adapters.for_event(event).scope(event)
        try:
            task = self.c.spawn(
                self.c.execute(
                    scope, event.get_sender_id(), event.message_str, event.is_admin()
                )
            )
            if task:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.error(
                "Group command failed: scope=%s error=%s",
                scope_key(scope),
                error_detail(exc),
            )

    def notice(self, event):
        if not self.accepts(event, message=False) or event.get_extra(
            "monitor_event_handled"
        ):
            return
        try:
            normalized = self.adapters.for_event(event).normalize(event)
            if normalized is not None:
                event.set_extra("monitor_event_handled", True)
                event.should_call_llm(True)
                self.c.enqueue(normalized)
        except (ValueError, TypeError, OverflowError) as exc:
            self.log.warning("Invalid group event: %s", error_detail(exc))

    def stop(self):
        self.stopped = True
