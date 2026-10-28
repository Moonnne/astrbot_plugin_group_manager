"""Isolated host environment and deterministic protocol/service doubles."""

import importlib
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="monitor-host-")
sys.path.insert(0, str(ROOT.parents[2]))
package = types.ModuleType("monitor_plugin")
package.__path__ = [str(ROOT)]
sys.modules["monitor_plugin"] = package

Repository = importlib.import_module("monitor_plugin.core.repository").Repository
Runtime = importlib.import_module("monitor_plugin.core.runtime").Runtime
Settings = importlib.import_module("monitor_plugin.core.settings").Settings
Scope = importlib.import_module("monitor_plugin.models.scope").Scope


scope_key = importlib.import_module("monitor_plugin.utils.scope").scope_key


class Clock:
    def __init__(self):
        self.now = 1800000000.0

    def __call__(self):
        return self.now


class Gateway:
    def for_scope(self, scope):
        return self

    @staticmethod
    def valid_user(value):
        return str(value).isdigit() and len(str(value)) >= 5

    async def iter_members(self, scope):
        for value in await self.call(scope, "get_group_member_list"):
            yield value

    def __init__(self):
        self.roles = {"10001": "owner", "10002": "admin", "99999": "admin"}
        self.sent, self.approvals, self.kicks = [], [], []
        self.members = {}
        self.fail_approval = self.fail_send = self.fail_role = False

    async def role(self, scope, qq):
        if self.fail_role:
            raise TimeoutError()
        return self.roles.get(qq, "member")

    async def approve(self, scope, request, approve):
        self.approvals.append((scope_key(scope), request["qq"], approve))
        if self.fail_approval:
            raise TimeoutError()

    async def kick(self, scope, qq):
        self.kicks.append((scope_key(scope), qq))

    async def send(self, scope, text="", image=None, mention=None, *, card=None):
        if self.fail_send:
            raise TimeoutError()
        self.sent.append((scope_key(scope), text, image, mention))
        return {"message_id": len(self.sent)}

    async def call(self, scope, action, **params):
        if action == "get_group_member_list":
            return [
                {
                    "user_id": qq,
                    "nickname": qq,
                    "role": self.roles.get(qq, "member"),
                    "join_time": joined,
                }
                for qq, joined in self.members.items()
            ]
        raise AssertionError(action)

    async def profile(self, scope, qq):
        return {
            "nickname": qq,
            "level": "12",
            "area": "未公开",
            "sex": "未公开",
            "age": "未公开",
            "avatar": "",
            "cover": "",
        }


class Renderer:
    def __init__(self):
        self.cards = []
        self.fail_formula = self.fail_card = False

    async def card(self, profile, qq, flags, member, history, request, title, now, umo):
        self.cards.append((qq, flags, history, title))
        return (None if self.fail_card else "https://example.test/card.png"), title

    async def formula(self, expression, umo):
        if self.fail_formula:
            raise RuntimeError("Render failed")
        return "https://example.test/formula.png"

    async def help(self, now, umo):
        return "https://example.test/help.png", "指令清单"


@pytest_asyncio.fixture
async def env():
    tmp_path = Path(tempfile.mkdtemp(prefix="case-", dir=os.environ["ASTRBOT_ROOT"]))
    db = Repository(tmp_path / "db/test.sqlite3", ROOT / "resource/sql")
    await db.open()
    clock, api, renderer = Clock(), Gateway(), Renderer()
    settings = Settings(tmp_path / "settings")
    c = Runtime(
        db,
        api,
        renderer,
        settings,
        lambda qq, umo: qq == "10000",
        logging.getLogger("monitor-test"),
        clock,
        enabled_groups={"12345"},
    )
    scope = Scope("onebot", "99999", "12345")
    yield types.SimpleNamespace(
        c=c,
        db=db,
        clock=clock,
        api=api,
        renderer=renderer,
        scope=scope,
        settings=settings,
        root=tmp_path,
    )
    await c.stop()
    await db.close()


@pytest.fixture
def domain():
    return types.SimpleNamespace(
        parse_command=importlib.import_module(
            "monitor_plugin.core.commands"
        ).parse_command,
        decision=importlib.import_module("monitor_plugin.core.approval").decision,
        arithmetic=importlib.import_module(
            "monitor_plugin.core.verification"
        ).arithmetic,
        display_time=importlib.import_module(
            "monitor_plugin.utils.formatting"
        ).display_time,
        light_theme=importlib.import_module(
            "monitor_plugin.utils.formatting"
        ).light_theme,
    )
