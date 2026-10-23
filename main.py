"""AstrBot command registration and plugin lifecycle."""

import asyncio
import weakref
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.session_plugin_manager import SessionPluginManager

from .core.controller import Controller
from .core.events import GroupEvents
from .core.group_selection import GroupSelector
from .core.onebot import OneBot
from .core.rendering import Renderer
from .core.repository import Repository
from .core.rules import COMMAND, GROUP_ID
from .core.settings import Settings
from .models.scope import Scope
from .utils.errors import error_detail
from .utils.scope import group_umo, scope_key


class ScopeFilter(filter.CustomFilter):
    runtime = None

    def filter(self, event, cfg):
        controller = self.runtime() if self.runtime else None
        raw = event.message_obj.raw_message
        return bool(
            controller
            and not controller.stopping
            and event.get_platform_name() == "aiocqhttp"
            and controller.allows_group(event.get_group_id())
            and isinstance(raw, dict)
            and raw.get("post_type") == "message"
        )


class GroupEventFilter(filter.CustomFilter):
    def filter(self, event, cfg):
        controller = ScopeFilter.runtime() if ScopeFilter.runtime else None
        return bool(
            controller
            and not controller.stopping
            and event.get_platform_name() == "aiocqhttp"
            and controller.allows_group(event.get_group_id())
            and GroupEvents.matches(event.message_obj.raw_message)
        )


class GroupCommandFilter(ScopeFilter):
    def filter(self, event, cfg):
        # The root group's native bare-command error must stay silent.
        return (
            super().filter(event, cfg)
            and event.message_str.strip().removeprefix("/") != "g"
        )


class OwnedMessageFilter(ScopeFilter):
    def filter(self, event, cfg):
        if not super().filter(event, cfg):
            return False
        text = event.message_str.strip()
        if not COMMAND.match(text) or not (
            text.startswith("/") or event.is_at_or_wake_command
        ):
            return False
        # Recognized native commands execute their registered handlers.
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


class GroupManager(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.controller = self.repository = self.events = None
        self.group_selector = GroupSelector(context, self.config, self.logger)

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
                if not GROUP_ID.fullmatch(group):
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
            self.controller = Controller(
                self.repository,
                OneBot(self.context, api_timeout, logger=self.logger),
                Renderer(
                    self.html_render,
                    root / "resource/templates",
                    render_timeout,
                    background_url=self.config.get("help_background_url", ""),
                    logger=self.logger,
                ),
                Settings(data / "settings", logger=self.logger),
                self.is_host_admin,
                self.logger,
                enabled_groups=enabled_groups,
                scope_guard=self.allows_scope,
            )
            await self.controller.start()
            ScopeFilter.runtime = weakref.ref(self.controller)
            self.events = GroupEvents(self.controller, self.logger)
            await self.group_selector.refresh()
        except BaseException as exc:
            self.logger.error("Plugin initialization failed: %s", error_detail(exc))
            await self.terminate()
            raise

    def is_host_admin(self, qq, umo):
        return qq in {str(v) for v in self.context.get_config(umo).get("admins_id", [])}

    async def allows_scope(self, scope):
        umo = group_umo(scope)
        cfg = self.context.get_config(umo)
        plugins = cfg.get("plugin_set", ["*"])
        if plugins != ["*"] and "astrbot_plugin_group_manager" not in plugins:
            return False
        settings = cfg.get("platform_settings", {})
        whitelist = {
            str(v).strip() for v in settings.get("id_whitelist", []) if str(v).strip()
        }
        if (
            settings.get("enable_id_white_list")
            and whitelist
            and not ({scope.group, umo} & whitelist)
        ):
            return False
        allowed = await SessionPluginManager.is_plugin_enabled_for_session(
            umo, "astrbot_plugin_group_manager"
        )
        return allowed

    @filter.custom_filter(GroupCommandFilter, False)
    @filter.command_group("g", desc="Monitor 群管理")
    def g(self):
        pass

    @filter.custom_filter(ScopeFilter, False)
    @g.command("help", desc="显示指令清单")
    async def help_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("on", desc="开启审批与验证")
    async def on_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("off", desc="关闭审批与验证")
    async def off_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("y", desc="批准申请：[编号/all]")
    async def approve_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("n", desc="拒绝申请：[编号/all]")
    async def reject_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("auto", desc="切换自动审批；skip_ban 切换黑名单拒绝")
    async def auto_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("ban", desc="添加本群黑名单：[QQ号] [理由]")
    async def ban_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("rmb", desc="撤销本群黑名单：[QQ号]")
    async def rmb_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("wl", desc="添加本群白名单：[QQ号] [备注]")
    async def wl_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("rmwl", desc="撤销本群白名单：[QQ号]")
    async def rmwl_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("pb", desc="添加公共黑名单：[QQ号] [理由]")
    async def pb_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("rmpb", desc="撤销本人公共黑名单：[QQ号]")
    async def rmpb_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @g.command("info", desc="成员记录卡片：[QQ号]")
    async def info_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.custom_filter(ScopeFilter, False)
    @filter.command("ans", desc="入群验证：[整数答案]")
    async def answer_command(self, event: AstrMessageEvent):
        await self.dispatch(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    @filter.custom_filter(OwnedMessageFilter, False)
    async def receive(self, event: AstrMessageEvent):
        # Also owns bare/invalid commands and literal /g when a different wake prefix is configured.
        await self.dispatch(event)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    @filter.custom_filter(GroupEventFilter, False)
    async def event_monitoring(self, event: AstrMessageEvent):
        """Handle native group requests and membership notices through AstrBot."""
        if self.events:
            event.should_call_llm(True)
            self.events.receive(event)

    async def dispatch(self, event):
        if not self.controller or not self.controller.allows_group(
            event.get_group_id()
        ):
            return
        if event.get_extra("monitor_handled"):
            return
        event.set_extra("monitor_handled", True)
        event.should_call_llm(True)
        event.stop_event()
        scope = Scope(
            event.get_platform_id(), event.get_self_id(), event.get_group_id()
        )
        if not scope.group.isdigit() or not scope.bot.isdigit():
            return
        try:
            task = self.controller.spawn(
                self.controller.handle(
                    scope,
                    event.unified_msg_origin,
                    dict(event.message_obj.raw_message),
                    event.get_sender_id(),
                    event.message_str,
                    event.is_admin(),
                )
            )
            if task:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(
                "Group command failed: scope=%s error=%s",
                scope_key(scope),
                error_detail(exc),
            )

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
            if ScopeFilter.runtime and ScopeFilter.runtime() is self.controller:
                ScopeFilter.runtime = None
            if self.repository:
                await self.repository.close()
