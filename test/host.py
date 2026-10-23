"""Real AstrBot adapter and handler dispatch for isolated integration tests."""

import asyncio
from copy import deepcopy
from types import MethodType, SimpleNamespace

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)
from astrbot.core.star.star import star_map


def adapter():
    return AiocqhttpAdapter(
        {"id": "onebot", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 0},
        {},
        asyncio.Queue(),
    )


async def run_handlers(plugin, event, config=None):
    metadata = star_map["monitor_plugin.main"]
    metadata.name, metadata.activated = "astrbot_plugin_group_manager", True
    config = deepcopy(DEFAULT_CONFIG) if config is None else config
    config.update(wake_prefix=["/"], admins_id=["10000"])
    stage = WakingCheckStage()
    await stage.initialize(
        SimpleNamespace(astrbot_config=config, db_helper=None, astrbot_config_id="test")
    )
    await stage.process(event)
    originals = []
    try:
        for handler in event.get_extra("activated_handlers", []):
            if handler.handler_module_path == "monitor_plugin.main":
                originals.append((handler, handler.handler))
                handler.handler = MethodType(handler.handler, plugin)
        request = StarRequestSubStage()
        await request.initialize(SimpleNamespace(astrbot_config=config))
        async for _ in request.process(event):
            pass
    finally:
        for handler, original in originals:
            handler.handler = original
    return event


async def settle(controller):
    await controller.queue.join()
    while controller.background:
        await asyncio.gather(*list(controller.background))
