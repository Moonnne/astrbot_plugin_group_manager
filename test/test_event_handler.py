"""Replay adapter events through the registered AstrBot group handler."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio
from host import adapter, run_handlers, settle
from monitor_plugin.main import GroupManager
from monitor_plugin.utils.scope import scope_key

from astrbot.api.star import StarTools
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def host_plugin(env, monkeypatch):
    platform = adapter()
    client = platform.get_client()
    client.subscribe = Mock(wraps=client.subscribe)
    client.unsubscribe = Mock(wraps=client.unsubscribe)
    context = SimpleNamespace(
        get_config=lambda umo=None: {
            "admins_id": [],
            "plugin_set": ["*"],
            "platform_settings": {},
        },
        platform_manager=SimpleNamespace(platform_insts=[platform]),
        get_platform_inst=lambda name: platform,
    )
    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: env.root)
    plugin = GroupManager(context, {"enabled_groups": ["12345"]})
    await plugin.initialize()
    plugin.controller.api, plugin.controller.renderer = env.api, env.renderer
    plugin.controller.clock = env.clock
    await plugin.controller.settings.update(
        env.scope, "10001", env.clock(), enabled=True
    )
    yield plugin, platform
    if not plugin.controller.stopping:
        await plugin.terminate()


def notice(clock, subtype="approve", **changes):
    return {
        "post_type": "notice",
        "notice_type": "group_increase",
        "sub_type": subtype,
        "self_id": 99999,
        "user_id": 20001,
        "group_id": 12345,
        "operator_id": 10001,
        "time": int(clock()),
        **changes,
    }


async def dispatch(plugin, platform, payload):
    await platform.get_client()._handle_event(payload)
    event = platform._event_queue.get_nowait()
    await run_handlers(plugin, event)
    await settle(plugin.controller)
    return event


async def test_request_card_and_manual_approval_use_registered_handler(
    host_plugin, env
):
    plugin, platform = host_plugin
    payload = {
        "post_type": "request",
        "request_type": "group",
        "sub_type": "add",
        "self_id": 99999,
        "user_id": 20001,
        "group_id": 12345,
        "time": int(env.clock()),
        "flag": "opaque-handle",
        "comment": "Synthetic request",
    }
    await platform.get_client()._handle_event(payload)
    await settle(plugin.controller)
    assert not env.renderer.cards, (
        "The removed client observer still processes requests"
    )
    event = platform._event_queue.get_nowait()
    await run_handlers(plugin, event)
    await settle(plugin.controller)
    assert env.renderer.cards and env.api.sent[-1][2]
    assert event.call_llm is True and not event.is_stopped()
    message = {
        **payload,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "user_id": 10001,
        "sender": {"user_id": 10001, "nickname": "Owner"},
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "/g y"}}],
        "raw_message": "/g y",
    }
    await dispatch(plugin, platform, message)
    assert env.api.approvals == [(scope_key(env.scope), "20001", True)]
    platform.get_client().subscribe.assert_not_called()


@pytest.mark.parametrize("subtype", ["approve", "invite"])
async def test_join_without_request_sends_one_formula_and_can_answer(
    host_plugin, env, subtype
):
    plugin, platform = host_plugin
    payload = notice(env.clock, subtype)
    await dispatch(plugin, platform, payload)
    await dispatch(plugin, platform, payload)
    assert not plugin.controller.approval.requests
    formulas = [
        entry
        for entry in env.api.sent
        if entry[2] == "https://example.test/formula.png"
    ]
    assert len(formulas) == 1 and formulas[0][3] == "20001"
    row = next(iter(plugin.controller.verification.challenges.values()))
    assert row.deadline == env.clock() + 600
    member = await plugin.repository.member(env.scope, "20001")
    assert member["method"] == ("QQ 10001 邀请" if subtype == "invite" else "申请入群")
    answer = {
        **payload,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "sender": {"user_id": 20001, "nickname": "Member"},
        "message_id": 2,
        "message": [{"type": "text", "data": {"text": f"/ans {row.answer}"}}],
        "raw_message": f"/ans {row.answer}",
    }
    await dispatch(plugin, platform, answer)
    assert not plugin.controller.verification.challenges
    assert (await plugin.repository.member(env.scope, "20001"))[
        "verification"
    ] == "passed"


async def test_disabled_group_still_records_join_and_leave(host_plugin, env):
    plugin, platform = host_plugin
    await plugin.controller.disable(env.scope, "10001")
    await dispatch(plugin, platform, notice(env.clock, "invite"))
    env.clock.now += 1
    await dispatch(
        plugin, platform, notice(env.clock, "leave", notice_type="group_decrease")
    )
    member = await plugin.repository.member(env.scope, "20001")
    assert member["presence"] == "absent" and member["leave_count"] == 1
    assert not env.api.sent and not plugin.controller.verification.challenges


async def test_unselected_group_and_normal_messages_do_not_activate_listener(
    host_plugin, env
):
    plugin, platform = host_plugin
    event = await dispatch(plugin, platform, notice(env.clock, group_id=54321))
    assert not event.get_extra("activated_handlers")
    payload = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "self_id": 99999,
        "user_id": 20001,
        "group_id": 12345,
        "time": int(env.clock()),
        "sender": {"user_id": 20001, "nickname": "Member"},
        "message_id": 3,
        "message": [{"type": "text", "data": {"text": "ordinary message"}}],
        "raw_message": "ordinary message",
    }
    event = await dispatch(plugin, platform, payload)
    assert not event.get_extra("activated_handlers")
    assert not env.api.sent and not plugin.controller.seen


async def test_lifecycle_does_not_manage_client_subscriptions(host_plugin):
    plugin, platform = host_plugin
    await plugin.platform_loaded()
    await plugin.loaded()
    await plugin.terminate()
    platform.get_client().subscribe.assert_not_called()
    platform.get_client().unsubscribe.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [{"user_id": 0}, {"self_id": 0}, {"group_id": 0}, {"flag": "x" * 4097}],
)
async def test_invalid_event_identity_and_oversized_handle_are_silent(
    host_plugin, env, changes
):
    plugin, platform = host_plugin
    await dispatch(plugin, platform, notice(env.clock, **changes))
    assert not env.api.sent and not plugin.controller.seen


async def test_host_plugin_selection_blocks_event_handler(host_plugin, env):
    plugin, platform = host_plugin
    await platform.get_client()._handle_event(notice(env.clock))
    event = platform._event_queue.get_nowait()
    config = deepcopy(DEFAULT_CONFIG)
    config["plugin_set"] = []
    await run_handlers(plugin, event, config)
    await settle(plugin.controller)
    assert not event.get_extra("activated_handlers")
    assert not env.api.sent and not plugin.controller.seen


@pytest.mark.parametrize("text", ["/g y", "/g n"])
async def test_approval_command_with_no_pending_requests_is_silent(
    host_plugin, env, text
):
    plugin, platform = host_plugin
    await dispatch(
        plugin,
        platform,
        {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "self_id": 99999,
            "user_id": 10001,
            "group_id": 12345,
            "time": int(env.clock()),
            "message_id": 4,
            "sender": {"user_id": 10001, "nickname": "Owner"},
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
        },
    )
    assert not env.api.sent and not env.api.approvals
    with plugin.file_log.sink.condition:
        output = "\n".join(plugin.file_log.sink.lines)
    assert "approval.selection" in output and "rows=0" in output
    assert "empty_wake_prefix=False" in output


@pytest.mark.parametrize("stops", [False, True])
async def test_event_match_is_visible_before_other_handler_can_stop_delivery(
    host_plugin, env, monkeypatch, stops
):
    plugin, platform = host_plugin

    async def intercept(event):
        if stops:
            event.stop_event()

    module = "isolated_interceptor"
    monkeypatch.setitem(star_map, module, StarMetadata(name=module, activated=True))
    interceptor = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"{module}.intercept",
        handler_name="intercept",
        handler_module_path=module,
        handler=intercept,
        event_filters=[EventMessageTypeFilter(EventMessageType.GROUP_MESSAGE)],
        extras_configs={"priority": 10000},
    )
    star_handlers_registry.append(interceptor)
    try:
        await dispatch(
            plugin,
            platform,
            {
                "self_id": 99999,
                "user_id": 20001,
                "group_id": 12345,
                "time": int(env.clock()),
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "flag": "private-handle",
                "comment": "private-reason",
            },
        )
        assert bool(env.renderer.cards) is not stops
        with plugin.file_log.sink.condition:
            output = "\n".join(plugin.file_log.sink.lines)
        assert "event.filter" in output and "matched=True" in output
        assert ("event.received" in output) is not stops
        assert "private-handle" not in output and "private-reason" not in output
    finally:
        star_handlers_registry.remove(interceptor)
