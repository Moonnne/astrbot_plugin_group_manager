"""Exercise real AstrBot conversion/wake processing and CQHttp dispatch."""

import logging
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiocqhttp import Event
from host import adapter
from monitor_plugin.core.events import GroupEvents
from monitor_plugin.core.rendering import Renderer
from monitor_plugin.main import GroupManager, ScopeFilter

from astrbot.api.platform import MessageType
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.star.star import star_map

pytestmark = pytest.mark.asyncio


async def test_real_star_html_render_contract():
    service = SimpleNamespace(
        render_custom_template=AsyncMock(return_value="https://example.test/render.png")
    )
    sessions = []

    def config(umo=None):
        sessions.append(umo)
        return {"t2i_endpoint": "https://example.test/t2i"}

    plugin = object.__new__(GroupManager)
    plugin.context = SimpleNamespace(get_config=config, html_renderer=service)
    renderer = Renderer(
        plugin.html_render, Path(__file__).resolve().parents[1] / "resource/templates"
    )
    await renderer.help(1800000000, "onebot:GroupMessage:12345")
    await renderer.formula(r"3 \times 4", "onebot:GroupMessage:12345")
    assert sessions == ["onebot:GroupMessage:12345"] * 2
    assert (
        service.render_custom_template.await_args.kwargs["endpoint"]
        == "https://example.test/t2i"
    )
    assert (
        service.render_custom_template.await_args.kwargs["options"]["omit_background"]
        is False
    )


@pytest.mark.parametrize(
    "text",
    [
        "/g",
        "/g help",
        "/g unknown",
        "/g ban",
        "/g info wrong",
        "/ans wrong",
        "/g auto nope",
    ],
)
async def test_real_wake_and_registered_handlers_keep_invalid_commands_silent(
    env, text
):
    import weakref

    platform = adapter()
    payload = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "self_id": 99999,
        "user_id": 20001,
        "group_id": 12345,
        "time": int(env.clock()),
        "message_id": 1,
        "sender": {"user_id": 20001, "nickname": "Member", "role": "member"},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }
    event = platform.create_event(await platform.convert_message(Event(payload)))
    event.send = AsyncMock()
    metadata = star_map["monitor_plugin.main"]
    metadata.name, metadata.activated = "astrbot_plugin_group_manager", True
    config = deepcopy(DEFAULT_CONFIG)
    config.update(wake_prefix=["/"], admins_id=["10000"])
    stage = WakingCheckStage()
    await stage.initialize(
        SimpleNamespace(astrbot_config=config, db_helper=None, astrbot_config_id="test")
    )
    ScopeFilter.runtime = weakref.ref(env.c)
    plugin = object.__new__(GroupManager)
    plugin.controller, plugin.logger = env.c, env.c.log
    try:
        await stage.process(event)
        handlers = event.get_extra("activated_handlers")
        assert handlers
        for handler in handlers:
            if not event.is_stopped():
                await handler.handler(plugin, event)
        assert event.is_stopped() and event.call_llm is True
        event.send.assert_not_called()
        assert bool(env.api.sent) == (text in {"/g", "/g help"})
    finally:
        ScopeFilter.runtime = None


@pytest.mark.parametrize("subtype", ["approve", "invite"])
async def test_host_empty_wake_prefix_rejects_notice_before_group_handlers(
    env, subtype
):
    platform = adapter()
    payload = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "sub_type": subtype,
        "self_id": 99999,
        "user_id": 20001,
        "group_id": 12345,
        "operator_id": 10001,
        "time": int(env.clock()),
    }
    event = platform.create_event(await platform.convert_message(Event(payload)))
    assert not event.message_str and not event.get_messages()
    config = deepcopy(DEFAULT_CONFIG)
    config["wake_prefix"] = [""]
    stage = WakingCheckStage()
    await stage.initialize(
        SimpleNamespace(astrbot_config=config, db_helper=None, astrbot_config_id="test")
    )
    with pytest.raises(IndexError):
        await stage.process(event)


@pytest.mark.parametrize(
    "fields",
    [
        {"post_type": "request", "request_type": "group", "sub_type": "add"},
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "invite",
            "invited_id": 20002,
        },
        {
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "approve",
        },
        {
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "invite",
        },
        {
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "leave",
        },
    ],
)
async def test_group_handler_preserves_native_wire_fields(fields):
    platform = adapter()
    received = []
    events = GroupEvents(
        SimpleNamespace(
            stopping=False,
            allows_group=lambda group: group == "12345",
            enqueue=lambda scope, raw: received.append((scope, raw)),
        ),
        logging.getLogger("monitor-contract"),
    )
    payload = {
        "self_id": 99999,
        "user_id": 20001,
        "group_id": 12345,
        "operator_id": 10001,
        "time": 1800000000,
        "flag": "opaque-request-handle",
        "comment": "Synthetic applicant message",
        **fields,
    }
    await platform.get_client()._handle_event(payload)
    host_event = platform._event_queue.get_nowait()
    events.receive(host_event)
    assert host_event.message_obj.type == MessageType.GROUP_MESSAGE
    assert not host_event.message_str and not host_event.get_messages()
    assert len(received) == 1
    scope, raw = received[0]
    assert (scope.platform, scope.bot, scope.group) == (
        "onebot",
        "99999",
        "12345",
    )
    assert raw == host_event.message_obj.raw_message == payload
    assert platform._event_queue.empty()


async def test_plugin_data_layout_and_shutdown_are_isolated():
    plugin = GroupManager(
        SimpleNamespace(
            get_config=lambda umo=None: {"admins_id": []},
            platform_manager=SimpleNamespace(platform_insts=[]),
        )
    )
    await plugin.initialize()
    repository, controller = plugin.repository, plugin.controller
    assert repository.path.parent.name == "db"
    assert "monitor-host-" in str(repository.path)
    await plugin.terminate()
    assert repository.db is None
    assert (
        controller.timer.done() and not controller.workers and not controller.background
    )
    assert not ScopeFilter.runtime


async def test_stopped_group_handler_rejects_new_events(env):
    platform = adapter()
    events = GroupEvents(env.c, env.c.log)
    events.stop()
    event = platform.create_event(
        await platform.convert_message(
            Event(
                {
                    "post_type": "notice",
                    "notice_type": "group_increase",
                    "sub_type": "approve",
                    "self_id": 99999,
                    "group_id": 12345,
                    "user_id": 20001,
                    "time": int(env.clock()),
                }
            )
        )
    )
    events.receive(event)
    assert env.c.queue.empty()


@pytest.mark.parametrize(
    "groups", ["12345", [True], [1.5], ["0012345"], ["群号"], [{}]]
)
async def test_invalid_enabled_groups_fail_before_opening_data(groups):
    plugin = GroupManager(SimpleNamespace(), {"enabled_groups": groups})
    with pytest.raises(ValueError):
        await plugin.initialize()
    assert plugin.repository is None
