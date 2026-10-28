"""Verify group discovery, searchable labels, and stable selection values."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from monitor_plugin.core.group_selection import GroupSelector

pytestmark = pytest.mark.asyncio


class Config(dict):
    def __init__(self, groups):
        super().__init__(enabled_groups=list(groups))
        self.schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )


def platform(platform_id, groups, name="aiocqhttp"):
    client = SimpleNamespace(call_action=AsyncMock(return_value=groups))
    return SimpleNamespace(
        meta=lambda: SimpleNamespace(id=platform_id, name=name),
        get_client=lambda: client,
    )


def context(platforms, aliases):
    return SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=platforms),
        get_db=lambda: SimpleNamespace(get_umo_aliases=AsyncMock(return_value=aliases)),
    )


def alias(umo, name, creator="20001"):
    return SimpleNamespace(
        umo=umo, auto_name=name, user_alias="", creator_sender_id=creator
    )


async def test_selector_uses_names_and_ids_and_only_discovers_onebot_groups():
    first = platform(
        "onebot",
        [
            {"group_id": 12345, "group_name": "研发群"},
            {"group_id": 54321, "group_name": "同名群"},
            {"group_id": 67890, "group_name": ""},
        ],
    )
    second = platform(
        "onebot2",
        [
            {"group_id": 33333, "group_name": "同名群"},
            {"group_id": 12345, "group_name": "研发群"},
        ],
    )
    unrelated = platform(
        "other", [{"group_id": 88888, "group_name": "Other"}], name="telegram"
    )
    config = Config(["99999", "12345"])
    host = context(
        [first, second, unrelated],
        [
            alias("onebot:GroupMessage:12345", "旧名字"),
            alias("onebot:GroupMessage:20001_77777", "历史讨论群"),
            alias("onebot:GroupMessage:67890", "本地群名"),
            alias("onebot:FriendMessage:22222", "私聊"),
            alias("other:GroupMessage:88888", "其他平台"),
            alias("malformed", "无效"),
        ],
    )
    await GroupSelector(host, config, logging.getLogger("monitor-test")).refresh()
    item = config.schema["enabled_groups"]
    choices = dict(zip(item["options"], item["labels"], strict=True))
    assert choices == {
        "12345": "研发群（12345）",
        "54321": "同名群（54321）",
        "33333": "同名群（33333）",
        "77777": "历史讨论群（77777）",
        "67890": "本地群名（67890）",
        "99999": "未获取群名（99999）",
    }
    assert config["enabled_groups"] == ["99999", "12345"]
    first.get_client().call_action.assert_awaited_once_with("get_group_list")
    second.get_client().call_action.assert_awaited_once_with("get_group_list")
    unrelated.get_client().call_action.assert_not_called()


async def test_offline_adapter_preserves_selected_ids_and_known_group_names():
    bot = platform("onebot", [])
    bot.get_client().call_action.side_effect = TimeoutError()
    config = Config(["12345", "99999"])
    host = context([bot], [alias("onebot:GroupMessage:12345", "缓存群名")])
    selector = GroupSelector(host, config, logging.getLogger("monitor-test"))
    await selector.refresh()
    item = config.schema["enabled_groups"]
    choices = dict(zip(item["options"], item["labels"], strict=True))
    assert choices["12345"] == "缓存群名（12345）"
    assert choices["99999"] == "未获取群名（99999）"


async def test_official_groups_use_scoped_umo_choices_without_onebot_actions():
    group = "A" * 32
    first = platform("official-ws", [], name="qq_official")
    second = platform("official-hook", [], name="qq_official_webhook")
    config = Config([])
    await GroupSelector(
        context(
            [first, second],
            [
                alias(f"official-ws:GroupMessage:{group}", "官方测试群"),
                alias(f"official-hook:GroupMessage:{group}", "Webhook 测试群"),
            ],
        ),
        config,
        logging.getLogger("monitor-test"),
    ).refresh()
    assert set(config.schema["enabled_groups"]["options"]) == {
        f"official-ws:GroupMessage:{group}",
        f"official-hook:GroupMessage:{group}",
    }
    first.get_client().call_action.assert_not_called()
    second.get_client().call_action.assert_not_called()


async def test_refresh_updates_renamed_groups_without_changing_scope():
    bot = platform("onebot", [])
    bot.get_client().call_action.side_effect = [
        [{"group_id": 12345, "group_name": "旧群名"}],
        [{"group_id": 12345, "group_name": "新群名"}],
    ]
    config = Config(["12345"])
    selector = GroupSelector(
        context([bot], []), config, logging.getLogger("monitor-test")
    )
    await selector.refresh()
    assert config.schema["enabled_groups"]["labels"] == ["旧群名（12345）"]
    await selector.refresh()
    assert config.schema["enabled_groups"]["options"] == ["12345"]
    assert config.schema["enabled_groups"]["labels"] == ["新群名（12345）"]
    assert config["enabled_groups"] == ["12345"]


async def test_unavailable_names_do_not_remove_selection_or_allow_all():
    config = Config(["12345"])
    host = context([], [])
    host.get_db = lambda: SimpleNamespace(
        get_umo_aliases=AsyncMock(side_effect=RuntimeError())
    )
    await GroupSelector(host, config, logging.getLogger("monitor-test")).refresh()
    assert config.schema["enabled_groups"]["options"] == ["12345"]
    assert config["enabled_groups"] == ["12345"]
    empty = Config([])
    await GroupSelector(
        context([], []), empty, logging.getLogger("monitor-test")
    ).refresh()
    assert empty.schema["enabled_groups"]["options"] == []
    assert empty["enabled_groups"] == []


async def test_host_configuration_response_exposes_dynamic_dropdown(monkeypatch):
    from astrbot.dashboard.services import config_service

    bot = platform("onebot", [{"group_id": 12345, "group_name": "可搜索的群聊"}])
    config = Config(["12345"])
    await GroupSelector(
        context([bot], []), config, logging.getLogger("monitor-test")
    ).refresh()
    plugin_name = "astrbot_plugin_group_manager"
    monkeypatch.setattr(
        config_service,
        "star_registry",
        [SimpleNamespace(name=plugin_name, config=config, i18n={})],
    )
    service = config_service.ConfigDisplayService(SimpleNamespace(astrbot_config={}))
    response = await service.get_configs(plugin_name)
    item = response["metadata"][plugin_name]["items"]["enabled_groups"]
    assert item["type"] == "list"
    assert item["options"] == ["12345"]
    assert item["labels"] == ["可搜索的群聊（12345）"]
    assert response["config"]["enabled_groups"] == ["12345"]
