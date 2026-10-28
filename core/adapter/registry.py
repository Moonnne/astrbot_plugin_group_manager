"""Resolve platform implementations without leaking protocol names into business code."""

from .onebot import OneBot
from .qqofficial import QQOfficial


class Adapters:
    def __init__(self, context, timeout=15, *, logger=None):
        self.context = context
        official = QQOfficial(context, timeout, logger=logger)
        self.protocols = {
            "aiocqhttp": OneBot(context, timeout, logger=logger),
            "qq_official": official,
            "qq_official_webhook": official,
        }

    def for_scope(self, scope):
        platform = self.context.get_platform_inst(scope.platform)
        if platform is None or platform.meta().name not in self.protocols:
            raise RuntimeError("Group management platform is unavailable")
        return self.protocols[platform.meta().name]

    def for_event(self, event):
        platform = self.context.get_platform_inst(event.get_platform_id())
        if platform is None or platform.meta().name != event.get_platform_name():
            return None
        return self.protocols.get(event.get_platform_name())

    def prune(self):
        for adapter in set(self.protocols.values()):
            adapter.prune()

    def clear(self):
        for adapter in set(self.protocols.values()):
            adapter.clear()
