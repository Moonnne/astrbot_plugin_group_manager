"""Exercise the actual AstrBot adapter, waking stage, and plugin filters."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiocqhttp import Event
from monitor_plugin.core.rendering import Renderer
from monitor_plugin.main import GroupManager, GroupManagerFilter

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)
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
    image, _ = await renderer.help(1800000000, "onebot:GroupMessage:12345")
    assert image == "https://example.test/render.png"
    await renderer.formula(r"3 \times 4", "onebot:GroupMessage:12345")
    assert service.render_custom_template.await_count == 2
    for call in service.render_custom_template.await_args_list:
        assert call.kwargs["return_url"] is True
        assert call.kwargs["options"]["full_page"] is True
    assert sessions == ["onebot:GroupMessage:12345"] * 2
    assert (
        service.render_custom_template.await_args.kwargs["endpoint"]
        == "https://example.test/t2i"
    )


def adapter():
    return AiocqhttpAdapter(
        {"id": "onebot", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 0},
        {},
        asyncio.Queue(),
    )


async def test_real_request_and_notice_conversion_preserve_raw_fields():
    platform = adapter()
    for post, extra in [
        (
            "request",
            {
                "request_type": "group",
                "sub_type": "add",
                "flag": "opaque:1",
                "comment": "hello",
            },
        ),
        (
            "notice",
            {
                "notice_type": "group_increase",
                "sub_type": "approve",
                "operator_id": 10001,
            },
        ),
    ]:
        raw = Event(
            {
                "post_type": post,
                "self_id": 99999,
                "user_id": 20001,
                "group_id": 12345,
                "time": 1800000000,
                **extra,
            }
        )
        message = await platform.convert_message(raw)
        event = platform.create_event(message)
        assert event.get_group_id() == "12345"
        assert event.message_str == ""
        assert event.message_obj.raw_message is raw
        assert GroupManagerFilter(False).filter(event, {})


@pytest.mark.parametrize(
    "text", ["/g unknown", "/g y nonsense", "/g ban", "/ans wrong", "/g", "/g help"]
)
async def test_real_waking_stage_routes_help_and_silences_invalid_commands(text, env):
    platform = adapter()
    raw = Event(
        {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "self_id": 99999,
            "user_id": 20001,
            "group_id": 12345,
            "time": 1800000000,
            "message_id": 1,
            "sender": {"user_id": 20001, "nickname": "Member", "role": "member"},
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
        }
    )
    event = platform.create_event(await platform.convert_message(raw))
    event.send = AsyncMock()
    metadata = star_map["monitor_plugin.main"]
    metadata.name = "astrbot_plugin_group_manager"
    metadata.activated = True
    config = deepcopy(DEFAULT_CONFIG)
    config.update({"wake_prefix": ["/"], "admins_id": ["10000"]})
    stage = WakingCheckStage()
    await stage.initialize(
        SimpleNamespace(astrbot_config=config, db_helper=None, astrbot_config_id="test")
    )
    await stage.process(event)
    assert event.get_extra("activated_handlers")
    assert not event.is_admin()
    plugin = object.__new__(GroupManager)
    plugin.controller = env.c
    plugin.logger = env.c.log
    await plugin.receive(event)
    assert event.is_stopped() and event.call_llm is True
    event.send.assert_not_called()
    if text in {"/g", "/g help"}:
        assert env.api.sent == [
            (env.scope.key, "", "https://example.test/help.png", None)
        ]
    else:
        assert not env.api.sent


async def test_help_in_private_chat_stays_silent(env):
    event = SimpleNamespace(
        message_obj=SimpleNamespace(raw_message={"post_type": "message"}),
        message_str="/g help",
        should_call_llm=lambda value: None,
        stop_event=lambda: None,
        get_group_id=lambda: "",
    )
    plugin = object.__new__(GroupManager)
    plugin.controller = env.c
    await plugin.receive(event)
    assert not env.api.sent


async def test_filter_ignores_unrelated_text_and_accepts_literal_slash():
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        message_obj=SimpleNamespace(raw_message={"post_type": "message"}),
        message_str="g on",
        is_at_or_wake_command=False,
    )
    assert not GroupManagerFilter(False).filter(event, {})
    event.message_str = "/g on"
    assert GroupManagerFilter(False).filter(event, {})
    event.message_str = "/greetings"
    assert not GroupManagerFilter(False).filter(event, {})


async def test_real_plugin_initialization_and_shutdown_use_isolated_data():
    plugin = GroupManager(
        SimpleNamespace(get_config=lambda umo=None: {"admins_id": []})
    )
    await plugin.initialize()
    repository = plugin.repository
    timer = plugin.controller.timer
    assert repository.path.name == "group_manager.sqlite3"
    assert "monitor-host-" in str(repository.path)
    await plugin.terminate()
    assert repository.db is None
    assert timer.done() and not plugin.controller.background


async def test_enabled_groups_normalize_ids_and_default_to_none():
    context = SimpleNamespace(get_config=lambda umo=None: {"admins_id": []})
    for config, expected in (
        ({}, frozenset()),
        (
            {"enabled_groups": [" 12345 ", 12345, "", "54321"]},
            frozenset({"12345", "54321"}),
        ),
    ):
        plugin = GroupManager(context, config)
        await plugin.initialize()
        try:
            assert plugin.controller.enabled_groups == expected
            assert not plugin.controller.allows_group("")
            assert not plugin.controller.allows_group("99998")
        finally:
            await plugin.terminate()


@pytest.mark.parametrize(
    "groups", ["12345", [True], [1.5], ["0012345"], ["群号"], [{}]]
)
async def test_invalid_enabled_groups_fail_before_opening_data(groups):
    plugin = GroupManager(
        SimpleNamespace(get_config=lambda umo=None: {}), {"enabled_groups": groups}
    )
    with pytest.raises(ValueError):
        await plugin.initialize()
    assert plugin.repository is None


@pytest.mark.parametrize("post", ["message", "request", "notice"])
async def test_outside_group_does_not_intercept_or_create_records(env, post):
    event = SimpleNamespace(
        message_obj=SimpleNamespace(raw_message={"post_type": post}),
        message_str="/g on",
        get_group_id=lambda: "54321",
        should_call_llm=Mock(),
        stop_event=Mock(),
    )
    plugin = object.__new__(GroupManager)
    plugin.controller = env.c
    await plugin.receive(event)
    event.should_call_llm.assert_not_called()
    event.stop_event.assert_not_called()
    assert not env.api.sent
    assert len(await env.db.all("groups")) == 1
