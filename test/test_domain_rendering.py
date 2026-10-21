"""Pure policy, parser, and template tests."""

import random
from datetime import datetime
from pathlib import Path

import pytest
from jinja2 import Environment, StrictUndefined
from monitor_plugin.core.onebot import safe_image
from monitor_plugin.core.rendering import Renderer
from monitor_plugin.models.domain import (
    SHANGHAI,
    arithmetic,
    decision,
    display_time,
    light_theme,
    parse_command,
)

RESOURCES = Path(__file__).resolve().parents[1] / "resource/templates"


@pytest.mark.parametrize(
    "text",
    [
        "/g help extra",
        "/g y 0",
        "/g y -1",
        "/g y 1 2",
        "/g ban 0 x",
        "/g wl 12345",
        "/g auto skip",
        "/ans",
        "/ans 1.0",
        "/answer 42",
        "/gban 12345 x",
        "/g unknown",
        "/ans __import__('os')",
    ],
)
def test_invalid_commands_are_not_exceptions(text):
    assert parse_command(text) is None


@pytest.mark.parametrize("text", ["/g", "/g help", "g", "g help", " /g  "])
def test_help_command_forms(text):
    assert parse_command(text).verb == "help"


@pytest.mark.asyncio
async def test_help_uses_host_template_service_and_falls_back_on_failure():
    captured = []

    async def render(template, data, *, umo=None, **kwargs):
        output = (
            Environment(undefined=StrictUndefined).from_string(template).render(data)
        )
        captured.append((output, {**kwargs, "umo": umo}))
        return "https://example.test/help.png"

    renderer = Renderer(render, RESOURCES)
    now = datetime(2026, 10, 21, 12, tzinfo=SHANGHAI).timestamp()
    image, plain = await renderer.help(now, "original:GroupMessage:12345")
    assert image == "https://example.test/help.png"
    output, options = captured[0]
    for command in ("/g help", "/g off", "/g n", "/g auto skip_ban", "/g rmpb", "/ans"):
        assert command in output and command in plain
    assert options["umo"] == "original:GroupMessage:12345"
    assert options["return_url"] is True
    assert options["options"]["viewport_height"] == 1
    assert "<h1>指令清单</h1>" in output and "群管理 · 指令清单" not in output
    assert "data:image/png;base64," in output
    assert 'class="notes"' not in output
    assert "AstrBot 管理员拥有全部命令权限" not in plain
    assert "Create Time: 2026-10-21 12:00:00 UTC+8" in output

    async def unavailable(*args, **kwargs):
        raise TimeoutError()

    renderer.render = unavailable
    assert await renderer.help(now, "umo") == (None, plain)


def test_all_policy_combinations():
    import itertools

    for enabled, auto, skip, white, black, public in itertools.product(
        (False, True), repeat=6
    ):
        result = decision(
            {"enabled": enabled, "auto_approve": auto, "skip_ban": skip},
            {"white": white, "black": black, "public": public},
        )
        expected = (
            "ignore"
            if not enabled
            else "approve"
            if white
            else ("reject" if skip else "manual")
            if black or public
            else "approve"
            if auto
            else "manual"
        )
        assert result == expected


def test_beijing_theme_boundaries_and_format():
    for hour, minute, expected in [
        (5, 59, False),
        (6, 0, True),
        (17, 59, True),
        (18, 0, False),
    ]:
        stamp = datetime(2026, 10, 21, hour, minute, tzinfo=SHANGHAI).timestamp()
        assert light_theme(stamp) is expected
        assert display_time(stamp) == f"2026-10-21 {hour:02}:{minute:02}:00 UTC+8"


@pytest.mark.asyncio
async def test_formula_subset_white_background_and_no_external_script():
    captured = []

    async def render(template, data, **kwargs):
        output = (
            Environment(undefined=StrictUndefined).from_string(template).render(data)
        )
        captured.append((output, kwargs))
        return "image"

    renderer = Renderer(render, RESOURCES)
    for seed in range(50):
        expression, answer = arithmetic(random.Random(seed))
        assert 0 <= answer <= 162
        await renderer.formula(expression, "umo")
    output, options = captured[-1]
    assert "background:#fff" in output and "color:#000" in output
    assert "<math" in output and "<script" not in output
    assert options["options"]["omit_background"] is False
    assert options["options"]["viewport_width"] == 1
    with pytest.raises(ValueError):
        await renderer.formula("<script>alert(1)</script>", "umo")


@pytest.mark.asyncio
async def test_card_escapes_untrusted_text_and_uses_numeric_level():
    captured = []

    async def render(template, data, **kwargs):
        output = (
            Environment(undefined=StrictUndefined).from_string(template).render(data)
        )
        captured.append(output)
        return "image"

    renderer = Renderer(render, RESOURCES)
    profile = {
        "nickname": "<script>bad()</script>",
        "level": "64",
        "age": "20",
        "sex": "男",
        "area": "未公开",
        "avatar": "",
        "cover": "",
        "signature": "<script>signature()</script>",
    }
    flags = {
        "white": False,
        "black": False,
        "public": False,
        "entries": [],
        "public_entries": [],
    }
    await renderer.card(
        profile, "12345", flags, None, [], None, "title", 1800000000, "umo"
    )
    assert "<script>bad()" not in captured[0]
    assert "&lt;script&gt;" in captured[0] and "Lv. 64" in captured[0]
    assert "MONITOR" in captured[0] and "margin:0;padding:0" in captured[0]
    assert "&lt;script&gt;signature()&lt;/script&gt;" in captured[0]
    assert 'class="title"' not in captured[0] and 'class="tags"' not in captured[0]
    assert '<div class="key">QQ 号</div>' not in captured[0]


@pytest.mark.asyncio
async def test_list_cards_keep_operators_times_and_reasons_separate():
    captured = []

    async def render(template, data, **kwargs):
        captured.append(
            Environment(undefined=StrictUndefined).from_string(template).render(data)
        )
        return "image"

    now = datetime(2026, 10, 21, 12, tzinfo=SHANGHAI).timestamp()
    profile = {
        "nickname": "Example",
        "level": "64",
        "sex": "男",
        "age": "20",
        "area": "未公开",
        "avatar": "",
        "cover": "",
    }
    flags = {
        "entries": [
            {"kind": kind, "actor": actor, "note": f"<{kind}>", "created_at": now}
            for kind, actor in (("white", "10001"), ("black", "10002"))
        ],
        "public_entries": [{"actor": "10003", "note": "<public>", "created_at": now}],
    }
    renderer = Renderer(render, RESOURCES)
    _, plain = await renderer.card(
        profile, "20001", flags, None, [], None, "removed heading", now, "umo"
    )
    output = captured[0]
    for kind, title, actor in (
        ("white", "白名单", "10001"),
        ("black", "黑名单", "10002"),
        ("public", "公共黑名单", "10003"),
    ):
        assert f'class="list-card {kind}"' in output
        assert title in output and actor in output
        assert f"&lt;{kind}&gt;" in output
        assert f"<{kind}>" in plain
    assert output.count("2026-10-21 12:00:00 UTC+8") == 4
    assert "removed heading" not in output
    assert "暂无个性签名" in output


@pytest.mark.asyncio
async def test_activity_hides_missing_data_and_uses_completed_operation_time():
    captured = []

    async def render(template, data, **kwargs):
        captured.append(
            (
                data,
                Environment(undefined=StrictUndefined)
                .from_string(template)
                .render(data),
            )
        )
        return "image"

    now = datetime(2026, 10, 21, 12, tzinfo=SHANGHAI).timestamp()
    profile = {
        "nickname": "Example",
        "level": "64",
        "sex": "男",
        "age": "21",
        "area": "天津",
        "avatar": "",
        "cover": "",
    }
    flags = {"entries": [], "public_entries": []}
    renderer = Renderer(render, RESOURCES)
    await renderer.card(
        profile,
        "20001",
        flags,
        {"first_join": None, "last_join": None, "last_leave": None, "leave_count": 0},
        [],
        None,
        "title",
        now,
        "umo",
    )
    assert captured[-1][0]["activity"] is None
    assert "section-card activity" not in captured[-1][1]
    request = {
        "subtype": "invite",
        "inviter": "10003",
        "reason": "<hello>",
        "status": "pending",
        "decided_at": now - 60,
        "actor": "10001",
        "expires_at": now + 1800,
    }
    await renderer.card(profile, "20001", flags, None, [], request, "title", now, "umo")
    assert captured[-1][0]["activity"]["operation_time"] == ""
    assert "接管截止" not in captured[-1][1]
    assert "QQ 10003 邀请" in captured[-1][1] and "&lt;hello&gt;" in captured[-1][1]
    member = {
        "first_join": now - 86400,
        "last_join": None,
        "last_leave": now - 3600,
        "leave_count": 2,
    }
    await renderer.card(
        profile,
        "20001",
        flags,
        member,
        [],
        {**request, "status": "approved"},
        "title",
        now,
        "umo",
    )
    data, output = captured[-1]
    assert data["activity"]["operation_time"] == "2026-10-21 11:59:00 UTC+8"
    assert data["activity"]["stats"] == [
        ("最早入群", "2026-10-20 12:00:00 UTC+8"),
        ("最近退群", "2026-10-21 11:00:00 UTC+8"),
        ("退群次数", "2"),
    ]
    assert output.index("badge level") < output.index("badge qq")
    assert output.index("地区 天津") < output.index("section-card activity")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/a",
        "https://qlogo.cn.evil.test/a",
        "https://user:pass@qq.com/a",
        "https://qq.com:9999/a",
    ],
)
def test_unsafe_image_origins_are_rejected(url):
    assert safe_image(url) == ""
