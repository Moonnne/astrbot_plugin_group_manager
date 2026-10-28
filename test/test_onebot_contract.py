"""Verify account routing and profile costs without live QQ requests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiocqhttp.api_impl import ResultStore, WebSocketReverseApi
from aiocqhttp.exceptions import ApiNotAvailable
from monitor_plugin.core.adapter.onebot import OneBot
from monitor_plugin.models.scope import Scope

pytestmark = pytest.mark.asyncio


def transport():
    calls = []

    async def action(name, **params):
        calls.append((name, params))
        if name == "get_stranger_info":
            return {
                "user_id": 20001,
                "nickname": "Example",
                "qqLevel": 64,
                "long_nick": "示例个性签名",
                "country": "中国",
                "province": "北京",
                "city": "北京",
                "preview_url": "https://qq.com/full-card.png",
            }
        return {}

    client = SimpleNamespace(call_action=action)
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: client
    )
    return (
        OneBot(SimpleNamespace(get_platform_inst=lambda name: platform)),
        calls,
        Scope("onebot", "99999", "12345"),
    )


async def test_actions_select_original_bot_and_preserve_request_flag():
    api, calls, scope = transport()
    await api.approve(scope, {"flag": "opaque/slreq:abc", "subtype": "add"}, False)
    assert calls[-1][1]["flag"] == "opaque/slreq:abc"
    assert calls[-1][1]["self_id"] == 99999
    await api.kick(scope, "20001")
    assert calls[-1] == (
        "set_group_kick",
        {
            "self_id": 99999,
            "group_id": 12345,
            "user_id": 20001,
            "reject_add_request": False,
        },
    )
    assert all(action != "get_login_info" for action, _ in calls)


@pytest.mark.parametrize(
    ("text", "image", "mention", "expected"),
    [
        (
            "验证提示",
            "https://example.test/formula.png",
            "20001",
            [
                {"type": "at", "data": {"qq": "20001"}},
                {"type": "text", "data": {"text": " 验证提示\n"}},
                {"type": "image", "data": {"file": "https://example.test/formula.png"}},
            ],
        ),
        (
            "操作完成。",
            None,
            "10001",
            [
                {"type": "at", "data": {"qq": "10001"}},
                {"type": "text", "data": {"text": " 操作完成。"}},
            ],
        ),
        (
            "",
            "https://example.test/card.png",
            "20001",
            [{"type": "image", "data": {"file": "https://example.test/card.png"}}],
        ),
        (
            "首行\n末行\n\n",
            "https://example.test/card.png",
            None,
            [
                {"type": "text", "data": {"text": "首行\n末行\n"}},
                {"type": "image", "data": {"file": "https://example.test/card.png"}},
            ],
        ),
    ],
)
async def test_message_order_and_image_line(text, image, mention, expected):
    api, calls, scope = transport()
    await api.send(scope, text, image=image, mention=mention)
    action, params = calls[-1]
    assert action == "send_group_msg"
    assert params["self_id"] == 99999 and params["group_id"] == 12345
    assert params["message"] == expected


async def test_actual_cqhttp_router_rejects_disconnected_original_account(monkeypatch):
    socket = SimpleNamespace(send=AsyncMock())
    router = WebSocketReverseApi({"88888": socket}, set(), 1)
    with pytest.raises(ApiNotAvailable):
        await router.call_action(
            "set_group_kick", self_id=99999, group_id=12345, user_id=20001
        )
    socket.send.assert_not_called()
    monkeypatch.setattr(
        ResultStore,
        "fetch",
        AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {}}),
    )
    await router.call_action("get_group_list", self_id=88888)
    socket.send.assert_awaited_once()


async def test_profile_uses_one_api_call_and_never_dress_preview_as_cover():
    api, calls, scope = transport()
    profile = await api.profile(scope, "20001")
    assert profile["level"] == "64" and profile["signature"] == "示例个性签名"
    assert profile["area"] == "中国 北京 北京" and not profile["cover"]
    assert len(calls) == 1
    assert await api.profile(scope, "20001") == profile and len(calls) == 1
    api.clear()
    assert not api.profiles
