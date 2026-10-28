"""QQ Official contracts using real AstrBot message classes and isolated transports."""

import __future__

import ast
import importlib
import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from host import run_handlers, settle
from monitor_plugin.core.adapter.qqofficial import QQOfficial
from monitor_plugin.core.commands import parse_command
from monitor_plugin.main import GroupManager
from monitor_plugin.models.scope import Scope
from monitor_plugin.utils.scope import group_umo

from astrbot.api.message_components import Image
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.star import StarTools

GROUP = "A" * 32
USER = "B" * 32
OWNER = "C" * 32


def official_platform(kind):
    saved = logging.root.handlers[:]
    try:
        websocket = importlib.import_module(
            "astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter"
        )
        webhook = importlib.import_module(
            "astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_adapter"
        )
    finally:
        for handler in saved:
            if handler not in logging.root.handlers:
                logging.root.addHandler(handler)
    cls = (
        websocket.QQOfficialPlatformAdapter
        if kind == "qq_official"
        else webhook.QQOfficialWebhookPlatformAdapter
    )
    platform = object.__new__(cls)
    platform.appid, platform.config = "test-app", {"id": "official"}
    platform.client = SimpleNamespace(http=SimpleNamespace(request=AsyncMock()))
    platform.send_by_session = AsyncMock()
    return platform, websocket


@pytest_asyncio.fixture(params=["qq_official", "qq_official_webhook"])
async def official(env, monkeypatch, request):
    platform, source = official_platform(request.param)
    calls = []

    async def http(route, **params):
        calls.append((route.method, route.url, params))
        if route.path.endswith("/members"):
            return {"members": [], "next_cursor": ""}
        if "/members/" in route.path:
            user = route.path.rsplit("/", 1)[1]
            return {
                "member_openid": user,
                "member_role": "owner" if user == OWNER else "member",
                "username": "Synthetic member",
            }
        if route.path.endswith("/bot_state"):
            return {"member_openid": "D" * 32, "member_role": "admin"}
        if route.path.endswith("/batch_remove_members"):
            return {"remove_members_result": "success"}
        if route.path.endswith("/messages"):
            return {"id": "native-card-id"}
        return {}

    platform.client.http.request.side_effect = http
    context = SimpleNamespace(
        get_platform_inst=lambda name: platform if name == "official" else None,
        get_config=lambda umo=None: {
            "admins_id": [],
            "plugin_set": ["*"],
            "platform_settings": {},
        },
        platform_manager=SimpleNamespace(platform_insts=[platform]),
    )
    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: env.root / "official")
    scope = Scope("official", "qqofficial:test-app", GROUP)
    plugin = GroupManager(context, {"enabled_groups": [group_umo(scope)]})
    await plugin.initialize()
    plugin.controller.renderer, plugin.controller.clock = env.renderer, env.clock
    await plugin.controller.settings.update(scope, OWNER, env.clock(), enabled=True)
    yield SimpleNamespace(
        plugin=plugin,
        platform=platform,
        source=source,
        scope=scope,
        calls=calls,
        env=env,
    )
    await plugin.terminate()


async def message(case, text, actor=OWNER):
    raw = case.source.PatchedGroupMessage(
        None,
        "event-message",
        {
            "id": "message-id",
            "content": text,
            "group_openid": GROUP,
            "author": {"member_openid": actor},
        },
    )
    abm = await case.source.QQOfficialPlatformAdapter._parse_from_qqofficial(
        raw, MessageType.GROUP_MESSAGE, True
    )
    abm.group_id, abm.session_id = GROUP, GROUP
    event = case.platform.create_event(abm)
    await run_handlers(case.plugin, event)
    await settle(case.plugin.controller)
    return event


async def notice(case, kind, **data):
    abm = AstrBotMessage()
    abm.type, abm.self_id, abm.group_id, abm.session_id = (
        MessageType.GROUP_MESSAGE,
        "qq_official",
        GROUP,
        GROUP,
    )
    abm.message_id, abm.timestamp, abm.message_str, abm.message = (
        "native-event",
        int(case.env.clock()),
        "",
        [],
    )
    abm.sender = MessageMember(user_id=USER, nickname="Synthetic")
    abm.raw_message = {
        "t": kind,
        "d": {
            "group_openid": GROUP,
            "member_openid": USER,
            "timestamp": int(case.env.clock()),
            **data,
        },
    }
    event = case.platform.create_event(abm)
    await run_handlers(case.plugin, event)
    await settle(case.plugin.controller)
    return event


async def test_official_request_card_and_approval_use_managed_transport(official):
    case = official
    await notice(
        case,
        "GROUP_JOIN_REQUEST",
        join_request_id="opaque-join-handle",
        username="Applicant",
        verify_info={"method": "verify_message", "verify_message": "Synthetic reason"},
    )
    assert len(case.plugin.controller.approval.requests) == 1
    payload = next(row[2]["json"] for row in case.calls if row[1].endswith("/messages"))
    assert payload["msg_type"] == 2
    content = payload["markdown"]["content"]
    assert content.endswith("\n![MONITOR](https://example.test/card.png)")
    buttons = payload["keyboard"]["content"]["rows"][0]["buttons"]
    assert [item["render_data"]["label"] for item in buttons] == ["同意", "拒绝"]
    assert all(
        item["action"]["type"] == 2 and item["action"]["enter"] for item in buttons
    )
    await message(case, buttons[0]["action"]["data"], USER)
    assert not [row for row in case.calls if "/approval_join_request/" in row[1]]
    await message(case, buttons[0]["action"]["data"])
    approvals = [row for row in case.calls if "/approval_join_request/" in row[1]]
    assert len(approvals) == 1
    assert approvals[0][0] == "POST" and approvals[0][1].endswith(
        f"/{GROUP}/approval_join_request/{USER}"
    )
    assert approvals[0][2]["json"] == {
        "op": "approve",
        "join_request_id": "opaque-join-handle",
    }
    await message(case, buttons[1]["action"]["data"])
    assert len([row for row in case.calls if "/approval_join_request/" in row[1]]) == 1


async def test_official_member_event_verification_and_answer(official):
    case = official
    await notice(case, "GROUP_MEMBER_ADD")
    row = next(iter(case.plugin.controller.verification.challenges.values()))
    assert row.deadline == case.env.clock() + 600
    payload = next(
        call[2]["json"] for call in case.calls if call[1].endswith("/messages")
    )
    content = payload["markdown"]["content"]
    assert content.startswith(f'<qqbot-at-user id="{USER}" /> ')
    assert content.endswith("\n![MONITOR](https://example.test/formula.png)")
    buttons = payload["keyboard"]["content"]["rows"][0]["buttons"]
    labels = [int(item["render_data"]["label"]) for item in buttons]
    assert len(labels) == len(set(labels)) == 3 and labels.count(row.answer) == 1
    assert all(
        item["action"]["permission"] == {"type": 0, "specify_user_ids": [USER]}
        for item in buttons
    )
    correct = next(
        item["action"]["data"]
        for item in buttons
        if int(item["render_data"]["label"]) == row.answer
    )
    wrong = next(
        item["action"]["data"]
        for item in buttons
        if int(item["render_data"]["label"]) != row.answer
    )
    await message(case, correct, OWNER)
    assert case.plugin.controller.verification.challenges
    await message(case, wrong, USER)
    assert row.deadline == case.env.clock() + 600
    await message(case, correct, USER)
    assert not case.plugin.controller.verification.challenges
    assert (await case.plugin.repository.member(case.scope, USER))[
        "verification"
    ] == "passed"


async def test_old_answer_button_cannot_pass_a_new_membership(official):
    await notice(official, "GROUP_MEMBER_ADD")
    row = next(iter(official.plugin.controller.verification.challenges.values()))
    old_command = f"/ans {row.answer} v_{row.session}"
    official.env.clock.now += 1
    await notice(official, "GROUP_MEMBER_REMOVE")
    official.env.clock.now += 1
    await notice(official, "GROUP_MEMBER_ADD")
    current = next(iter(official.plugin.controller.verification.challenges.values()))
    current.answer = row.answer
    await message(official, old_command, USER)
    assert (
        next(iter(official.plugin.controller.verification.challenges.values()))
        is current
    )
    official.env.clock.now = current.deadline
    await message(official, f"/ans {current.answer} v_{current.session}", USER)
    assert official.plugin.controller.verification.challenges


async def test_approval_buttons_stay_bound_after_new_requests_and_expiry(official):
    await notice(official, "GROUP_JOIN_REQUEST", join_request_id="first")
    original = next(iter(official.plugin.controller.approval.requests.values()))
    await notice(official, "GROUP_JOIN_REQUEST", join_request_id="second")
    await message(official, "/group y")
    await message(official, f"/group n r_{original.id}")
    approvals = [
        call for call in official.calls if "/approval_join_request/" in call[1]
    ]
    assert len(approvals) == 1 and approvals[0][2]["json"]["join_request_id"] == "first"
    second = next(
        row
        for row in official.plugin.controller.approval.requests.values()
        if row.flag == "second"
    )
    assert not official.plugin.controller.approval.select(
        Scope("official", "qqofficial:test-app", "E" * 32), f"r_{second.id}"
    )[0]
    official.env.clock.now = second.expires_at
    await message(official, f"/group y r_{second.id}")
    assert (
        len([call for call in official.calls if "/approval_join_request/" in call[1]])
        == 1
    )


async def test_card_send_failure_does_not_start_verification_deadline(official):
    http = official.platform.client.http.request
    original = http.side_effect

    async def fail_card(route, **params):
        return (
            {} if route.path.endswith("/messages") else await original(route, **params)
        )

    http.side_effect = fail_card
    await notice(official, "GROUP_MEMBER_ADD")
    row = next(iter(official.plugin.controller.verification.challenges.values()))
    assert row.state == "delivery" and row.deadline == 0


async def test_host_admin_can_use_approval_button_without_group_admin_role(official):
    await notice(official, "GROUP_JOIN_REQUEST", join_request_id="host-admin-request")
    row = next(iter(official.plugin.controller.approval.requests.values()))
    # Exercise the business entry with AstrBot's admin result and a member-only role.
    await official.plugin.controller.execute(
        official.scope, USER, f"/group y r_{row.id}", superuser=True
    )
    assert row.status == "approved" and row.actor == USER


async def test_native_card_fallback_escapes_applicant_markdown(official):
    official.env.renderer.card = AsyncMock(
        return_value=(
            None,
            '<qqbot-at-user id="injected" /> [untrusted](https://example.test)',
        )
    )
    await notice(official, "GROUP_JOIN_REQUEST", join_request_id="fallback-request")
    payload = next(
        call[2]["json"] for call in official.calls if call[1].endswith("/messages")
    )
    assert "markdown" in payload and "keyboard" in payload
    content = payload["markdown"]["content"]
    assert content.startswith(f'<qqbot-at-user id="{USER}" /> ')
    assert "&lt;" in content and content.count("<qqbot-at-user") == 1
    assert r"\[untrusted\]\(" in content and "![MONITOR]" not in content


@pytest.mark.parametrize("text", ["/group", "/group help", "/g", "/g help"])
async def test_both_command_group_names_show_image_help(official, text):
    await message(official, text, USER)
    chain = official.platform.send_by_session.await_args.args[1].chain
    assert len(chain) == 1 and isinstance(chain[0], Image)


async def test_official_help_is_image_and_numeric_qq_is_not_an_openid(official):
    await message(official, "/g help", USER)
    chain = official.platform.send_by_session.await_args.args[1].chain
    assert len(chain) == 1 and isinstance(chain[0], Image)
    assert parse_command("/g ban 12345 reason", QQOfficial.valid_user) is None


async def test_official_already_approved_request_is_not_approved_again(official):
    await notice(
        official,
        "GROUP_JOIN_REQUEST",
        join_request_id="already-done",
        auto_approved={"strategy_id": "strategy"},
    )
    row = next(iter(official.plugin.controller.approval.requests.values()))
    assert row.status == "approved"
    await message(official, "/g y")
    assert not [call for call in official.calls if "/approval_join_request/" in call[1]]


async def test_official_openid_namespaces_are_isolated_between_apps(env):
    a = Scope("official-a", "qqofficial:app-a", GROUP)
    b = Scope("official-b", "qqofficial:app-b", GROUP)
    command = parse_command(f"/g pb {USER} reason", QQOfficial.valid_user)
    await env.db.edit_list(a, OWNER, command, False, env.clock())
    assert (await env.db.flags(a, USER))["public"]
    assert not (await env.db.flags(b, USER))["public"]
    assert not (await env.db.flags(env.scope, USER))["public"]
    rows = (await env.db.flags(a, USER))["public_entries"]
    assert rows[0]["actor"] == OWNER


async def test_official_member_pagination_and_unsafe_removal_outcome(official):
    api = official.plugin.adapters.for_scope(official.scope)
    responses = [
        {
            "members": [{"member_openid": USER, "member_role": "member"}],
            "next_cursor": "next",
        },
        {
            "members": [{"member_openid": OWNER, "member_role": "owner"}],
            "next_cursor": "",
        },
    ]
    official.platform.client.http.request.side_effect = responses
    assert [item["user_id"] async for item in api.iter_members(official.scope)] == [
        USER,
        OWNER,
    ]
    official.platform.client.http.request.side_effect = None
    official.platform.client.http.request.return_value = {}
    with pytest.raises(RuntimeError, match="uncertain"):
        await api.kick(official.scope, USER)
    with pytest.raises(RuntimeError, match="AppID"):
        await api.role(Scope("official", "qqofficial:foreign-app", GROUP), USER)


def patched_host_sources(directory):
    root = Path(__file__).resolve().parents[1]
    paths = [
        Path("astrbot/core/platform/sources") / folder / file
        for folder, file in (
            ("qqofficial", "qqofficial_platform_adapter.py"),
            ("qqofficial_webhook", "qo_webhook_adapter.py"),
        )
    ]
    originals = {}
    for path in paths:
        originals[path] = (root.parents[2] / path).read_bytes()
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(originals[path])
    patch = root / "resource/astrbot/qqofficial-group-events.patch"
    for args in (["--check"], []):
        result = subprocess.run(
            ["git", "apply", *args, str(patch)],
            cwd=directory,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    for path, data in originals.items():
        assert (root.parents[2] / path).read_bytes() == data
    return [(directory / path).read_text(encoding="utf-8") for path in paths]


async def test_host_patch_delivers_real_sdk_group_request_to_plugin(official, env):
    from botpy.connection import ConnectionState

    sources = patched_host_sources(env.root / "host-copy")
    namespace = {
        **vars(official.source),
        "time": time,
        "ConnectionState": type("ExtendedState", (ConnectionState,), {}),
    }
    tree = ast.parse(sources[0])
    names = {
        "PatchedGroupManagementEvent",
        "_parse_group_management_event",
        "_ensure_group_message_create_parser",
    }
    nodes = [node for node in tree.body if getattr(node, "name", "") in names]
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]),
            "isolated-host-events",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    namespace["_ensure_group_message_create_parser"]()
    dispatched = []
    state = namespace["ConnectionState"](
        lambda name, value: dispatched.append((name, value)), None
    )
    state.parsers["group_join_request"](
        {
            "id": "native-request",
            "d": {
                "group_openid": GROUP,
                "member_openid": USER,
                "username": "Applicant",
                "join_request_id": "sdk-request-handle",
                "apply_at": "2026-10-28T12:00:00+08:00",
            },
        }
    )
    name, payload = dispatched[0]
    index = 0 if official.platform.meta().name == "qq_official" else 1
    klass = next(
        node
        for node in ast.parse(sources[index]).body
        if isinstance(node, ast.ClassDef) and node.name == "botClient"
    )
    methods = [
        node
        for node in klass.body
        if getattr(node, "name", "") in {"_commit_group_event", "on_group_join_request"}
    ]
    exec(
        compile(
            ast.Module(body=methods, type_ignores=[]),
            "isolated-host-callbacks",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    client = type(
        "ExtendedClient",
        (),
        {
            key: namespace[key]
            for key in ("_commit_group_event", "on_group_join_request")
        },
    )()
    client.platform = official.platform
    official.platform._session_scene = {}
    official.platform.commit_event = Mock()
    await getattr(client, "on_" + name)(payload)
    event = official.platform.commit_event.call_args.args[0]
    assert event.message_obj.raw_message["t"] == "GROUP_JOIN_REQUEST"
    await run_handlers(official.plugin, event)
    await settle(official.plugin.controller)
    row = next(iter(official.plugin.controller.approval.requests.values()))
    assert row.flag == "sdk-request-handle" and row.qq == USER


pytestmark = pytest.mark.asyncio
