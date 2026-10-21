"""Regressions at the host command registry and output boundaries."""

import importlib
from pathlib import Path

import pytest
from monitor_plugin.core.rendering import Renderer

from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry

pytestmark = pytest.mark.asyncio


async def test_group_is_registered_in_host_command_metadata():
    importlib.import_module("monitor_plugin.main")
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
