"""Regressions at the host registry, wire dispatcher, and output boundaries."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from monitor_plugin.core.rendering import Renderer
from monitor_plugin.main import GroupManager
from test_host_contract import adapter

from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry

pytestmark = pytest.mark.asyncio


async def test_group_is_registered_in_host_command_metadata():
    groups = [
        f.group_name
        for handler in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=False
        )
        if handler.handler_module_path == "monitor_plugin.main"
        for f in handler.event_filters
        if isinstance(f, CommandGroupFilter)
    ]
    assert "g" in groups


async def test_native_dispatch_reaches_group_request_without_message_pipeline(env):
    platform = adapter()
    context = SimpleNamespace(
        get_config=lambda umo=None: {"admins_id": []},
        platform_manager=SimpleNamespace(platform_insts=[platform]),
        get_platform_inst=lambda name: platform,
    )
    plugin = GroupManager(context, {"enabled_groups": ["12345"]})
    await plugin.initialize()
    own_controller = plugin.controller
    try:
        await env.settings.update(env.scope, "10001", env.clock(), enabled=True)
        await env.c.start()
        plugin.events.controller = env.c
        await platform.get_client()._handle_event(
            {
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "self_id": 99999,
                "user_id": 20001,
                "group_id": 12345,
                "time": int(env.clock()),
                "flag": "wire-request",
                "comment": "hello",
            }
        )
        await env.c.queue.join()
        await asyncio.gather(*env.c.background)
        assert env.renderer.cards, (
            "The raw group request never reached the card workflow"
        )
    finally:
        plugin.controller = own_controller
        await plugin.terminate()


async def test_info_sends_only_an_image(env):
    await env.c.show_info(env.scope, "20001")
    assert env.api.sent
    assert all(text == "" and image for _, text, image, _ in env.api.sent)


async def test_help_payload_does_not_inline_megabyte_background():
    captured = []

    async def render(template, data, **kwargs):
        captured.append((template, data))
        return "https://example.test/image.png"

    renderer = Renderer(
        render, Path(__file__).resolve().parents[1] / "resource/templates"
    )
    await renderer.help(1800000000, "onebot:GroupMessage:12345")
    template, data = captured[0]
    size = len(template.encode()) + len(str(data).encode())
    assert size < 100_000, f"Help render request carries {size:,} bytes"
