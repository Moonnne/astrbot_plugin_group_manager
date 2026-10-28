"""AstrBot command registration and plugin lifecycle."""

import weakref
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.adapter.access import HostAccess
from .core.adapter.handlers import (
    GroupCommandFilter,
    GroupEventFilter,
    Handlers,
    OwnedMessageFilter,
    ScopeFilter,
)
from .core.adapter.registry import Adapters
from .core.group_selection import GroupSelector
from .core.rendering import Renderer
from .core.repository import Repository
from .core.runtime import Runtime
from .core.settings import Settings
from .utils.errors import error_detail
from .utils.scope import valid_selection


class GroupManager(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.controller = self.repository = self.events = None
        self.access = HostAccess(context)
        self.adapters = Adapters(
            context, int(self.config.get("api_timeout", 15)), logger=self.logger
        )
        self.group_selector = GroupSelector(
            context, self.config, self.logger, self.adapters
        )

    async def initialize(self):
        root = Path(__file__).parent
        groups = self.config.get("enabled_groups", [])
        if not isinstance(groups, list):
            raise ValueError("Enabled groups must be a list of QQ group IDs")
        enabled_groups = set()
        for value in groups:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError("Invalid enabled group ID")
            group = str(value).strip()
            if group:
                if not valid_selection(group):
                    raise ValueError("Invalid enabled group ID")
                enabled_groups.add(group)
        self.config["enabled_groups"] = sorted(enabled_groups)
        api_timeout = int(self.config.get("api_timeout", 15))
        render_timeout = int(self.config.get("render_timeout", 25))
        if not 3 <= api_timeout <= 60 or not 5 <= render_timeout <= 120:
            raise ValueError("Invalid timeout configuration")
        data = StarTools.get_data_dir("astrbot_plugin_group_manager")
        try:
            self.repository = Repository(
                data / "db/group_manager.sqlite3",
                root / "resource/sql",
                logger=self.logger,
            )
            await self.repository.open()
            self.controller = Runtime(
                self.repository,
                self.adapters,
                Renderer(
                    self.html_render,
                    root / "resource/templates",
                    render_timeout,
                    background_url=self.config.get("help_background_url", ""),
                    logger=self.logger,
                ),
                Settings(data / "settings", logger=self.logger),
                self.access.is_admin,
                self.logger,
                enabled_groups=enabled_groups,
                scope_guard=self.access.allows,
            )
            await self.controller.start()
            self.events = Handlers(self.controller, self.adapters, self.logger)
            ScopeFilter.runtime = weakref.ref(self.events)
            await self.group_selector.refresh()
        except BaseException as exc:
            self.logger.error("Plugin initialization failed: %s", error_detail(exc))
            await self.terminate()
            raise

    @filter.custom_filter(GroupCommandFilter, False)
    @filter.command_group("group", alias={"g"}, desc="Monitor 群管理")
    def group(self):
        pass

    @filter.custom_filter(ScopeFilter, False)
    @group.command("help", desc="显示指令清单")
    async def help_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("on", desc="开启审批与验证")
    async def on_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("off", desc="关闭审批与验证")
    async def off_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("y", desc="批准申请：[编号/all]")
    async def approve_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("n", desc="拒绝申请：[编号/all]")
    async def reject_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("auto", desc="切换自动审批；skip_ban 切换黑名单拒绝")
    async def auto_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("ban", desc="添加本群黑名单：[成员ID] [理由]")
    async def ban_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("rmb", desc="撤销本群黑名单：[成员ID]")
    async def rmb_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("wl", desc="添加本群白名单：[成员ID] [备注]")
    async def wl_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("rmwl", desc="撤销本群白名单：[成员ID]")
    async def rmwl_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("pb", desc="添加公共黑名单：[成员ID] [理由]")
    async def pb_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("rmpb", desc="撤销本人公共黑名单：[成员ID]")
    async def rmpb_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @group.command("info", desc="成员记录卡片：[成员ID]")
    async def info_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.custom_filter(ScopeFilter, False)
    @filter.command("ans", desc="入群验证：[整数答案]")
    async def answer_command(self, event: AstrMessageEvent):
        await self.events.message(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    @filter.custom_filter(OwnedMessageFilter, False)
    async def receive(self, event: AstrMessageEvent):
        # Also owns bare/invalid commands and literal /group or /g with other wake prefixes.
        await self.events.message(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    @filter.custom_filter(GroupEventFilter, False)
    async def event_monitoring(self, event: AstrMessageEvent):
        """Handle native group requests and membership notices through AstrBot."""
        if self.events:
            event.should_call_llm(True)
            self.events.notice(event)

    @filter.on_platform_loaded()
    async def platform_loaded(self):
        if self.controller and not self.controller.stopping:
            await self.group_selector.refresh()

    @filter.on_astrbot_loaded()
    async def loaded(self):
        if self.controller and not self.controller.stopping:
            await self.group_selector.refresh()

    async def terminate(self):
        if self.events:
            self.events.stop()
        try:
            if self.controller:
                await self.controller.stop()
        finally:
            if ScopeFilter.runtime and ScopeFilter.runtime() is self.events:
                ScopeFilter.runtime = None
            if self.repository:
                await self.repository.close()
