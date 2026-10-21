"""Exercise real AstrBot conversion/wake processing and CQHttp dispatch."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiocqhttp import Event
from monitor_plugin.core.events import GroupEvents
from monitor_plugin.core.rendering import Renderer
from monitor_plugin.main import GroupManager, ScopeFilter

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)
from astrbot.core.star.star import star_map

pytestmark = pytest.mark.asyncio


def adapter():
    return AiocqhttpAdapter(
        {"id": "onebot", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 0},
        {},
        asyncio.Queue(),
    )


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


async def test_empty_notice_breaks_chat_wake_but_native_observer_still_verifies(env):
    platform = adapter()
    payload = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "sub_type": "approve",
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
    await env.settings.update(env.scope, "10001", env.clock(), enabled=True)
    await env.c.start()
    observer = GroupEvents(SimpleNamespace(), env.c, env.c.log)
    observer.bind(platform)
    observer.bind(platform)
    assert len(observer.bindings) == 3
    try:
        await platform.get_client()._handle_event(payload)
        await env.c.queue.join()
        await asyncio.gather(*env.c.background)
        assert any(
            image == "https://example.test/formula.png"
            for _, _, image, _ in env.api.sent
        )
        assert (await env.db.member(env.scope, "20001"))["verification"] == "waiting"
    finally:
        observer.stop()
    assert not observer.bindings
    await platform.get_client()._handle_event({**payload, "user_id": 20002})
    assert env.c.queue.empty()


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


async def test_removed_adapter_subscriptions_are_released(env):
    first, second = adapter(), adapter()
    platforms = [first]
    observer = GroupEvents(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=platforms)),
        env.c,
        env.c.log,
    )
    observer.bind_all()
    platforms[:] = [second]
    observer.bind_all()
    assert len(observer.bindings) == 3
    assert all(client is second.get_client() for client, _, _ in observer.bindings)
    await first.get_client()._handle_event(
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
    assert env.c.queue.empty()
    observer.stop()


@pytest.mark.parametrize(
    "groups", ["12345", [True], [1.5], ["0012345"], ["群号"], [{}]]
)
async def test_invalid_enabled_groups_fail_before_opening_data(groups):
    plugin = GroupManager(SimpleNamespace(), {"enabled_groups": groups})
    with pytest.raises(ValueError):
        await plugin.initialize()
    assert plugin.repository is None
