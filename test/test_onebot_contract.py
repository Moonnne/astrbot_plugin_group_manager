"""Verify real transport construction without sending any QQ requests."""

from types import SimpleNamespace

import pytest
from monitor_plugin.core.onebot import OneBot
from monitor_plugin.models.domain import Scope

pytestmark = pytest.mark.asyncio


class Client:
    def __init__(self):
        self.bot = 99999
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_login_info":
            return {"user_id": self.bot}
        if action == "get_stranger_info":
            return {
                "user_id": 20001,
                "nickname": "Example",
                "qqLevel": 64,
                "long_nick": "示例个性签名",
                "country": "中国",
                "province": "北京",
                "city": "北京",
            }
        if action == "_get_friend_dress":
            raise RuntimeError("Unsupported extension")
        return {}


def transport():
    client = Client()
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: client
    )
    context = SimpleNamespace(
        get_platform_inst=lambda platform_id: (
            platform if platform_id == "onebot" else None
        )
    )
    return OneBot(context), client, Scope("onebot", "99999", "12345")


async def test_flags_preserved_and_kick_does_not_block_rejoining():
    api, client, scope = transport()
    await api.approve(scope, {"flag": "opaque/slreq:abc", "subtype": "add"}, False)
    action, params = client.calls[-1]
    assert action == "set_group_add_request"
    assert params["flag"] == "opaque/slreq:abc" and params["approve"] is False
    await api.kick(scope, "20001")
    assert client.calls[-1] == (
        "set_group_kick",
        {
            "group_id": 12345,
            "user_id": 20001,
            "reject_add_request": False,
        },
    )


async def test_reconnect_account_change_prevents_stale_actions():
    api, client, scope = transport()
    await api.send(scope, "first")
    client.bot = 88888
    with pytest.raises(RuntimeError, match="identity mismatch"):
        await api.kick(scope, "20001")
    assert all(name != "set_group_kick" for name, _ in client.calls)


async def test_profile_optional_extensions_fail_without_losing_level():
    api, _, scope = transport()
    profile = await api.profile(scope, "20001")
    assert profile["level"] == "64" and profile["area"] == "中国 北京 北京"
    assert profile["cover"] == "" and profile["avatar"].startswith("https://")
    assert profile["signature"] == "示例个性签名"
