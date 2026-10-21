"""AstrBot entry point for scoped QQ group management."""

import asyncio
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.controller import Controller
from .core.onebot import OneBot
from .core.rendering import Renderer
from .core.repository import Repository
from .models.domain import COMMAND, Scope


class GroupManagerFilter(filter.CustomFilter):
    def filter(self, event, cfg):
        if event.get_platform_name() != "aiocqhttp":
            return False
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return False
        post = raw.get("post_type")
        if post == "message":
            text = event.message_str.strip()
            return bool(
                COMMAND.match(text)
                and (text.startswith("/") or event.is_at_or_wake_command)
            )
        return bool(
            raw.get("group_id")
            and (
                (post == "request" and raw.get("request_type") == "group")
                or (
                    post == "notice"
                    and raw.get("notice_type") in {"group_increase", "group_decrease"}
                )
            )
        )


class GroupManager(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.config = config or {}
        self.controller = None
        self.repository = None

    async def initialize(self):
        root = Path(__file__).parent
        api_timeout = int(self.config.get("api_timeout", 15))
        render_timeout = int(self.config.get("render_timeout", 25))
        if not 3 <= api_timeout <= 60 or not 5 <= render_timeout <= 120:
            raise ValueError("Invalid timeout configuration")
        self.repository = Repository(
            StarTools.get_data_dir("astrbot_plugin_group_manager")
            / "group_manager.sqlite3",
            root / "resource/sql",
        )
        await self.repository.open()
        try:
            self.controller = Controller(
                self.repository,
                OneBot(self.context, api_timeout),
                Renderer(self.html_render, root / "resource/templates", render_timeout),
                self.is_host_admin,
                self.logger,
            )
            await self.controller.start()
        except BaseException:
            await self.repository.close()
            raise

    def is_host_admin(self, qq, umo):
        return qq in {
            str(value) for value in self.context.get_config(umo).get("admins_id", [])
        }

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    @filter.custom_filter(GroupManagerFilter, False)
    async def receive(self, event: AstrMessageEvent):
        raw = event.message_obj.raw_message
        if raw.get("post_type") == "message":
            event.should_call_llm(True)
            event.stop_event()
        if not self.controller or not event.get_group_id():
            return
        scope = Scope(
            event.get_platform_id(), event.get_self_id(), event.get_group_id()
        )
        if not scope.group.isdigit() or not scope.bot.isdigit():
            return
        try:
            await self.controller.spawn(
                self.controller.handle(
                    scope,
                    event.unified_msg_origin,
                    dict(raw),
                    event.get_sender_id(),
                    event.message_str,
                    event.is_admin(),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(
                "Group management event failed: scope=%s error=%s",
                scope.key,
                type(exc).__name__,
            )

    @filter.on_astrbot_loaded()
    async def loaded(self):
        if self.controller:
            for row in await self.repository.all("groups"):
                self.controller.spawn(
                    self.controller.sync_members(Scope.parse(row["scope"]))
                )

    async def terminate(self):
        if self.controller:
            await self.controller.stop()
        if self.repository:
            await self.repository.close()
