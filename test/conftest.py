"""Isolated test environment, transport fakes, and package loading."""

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
Controller = importlib.import_module("monitor_plugin.core.controller").Controller
Scope = importlib.import_module("monitor_plugin.models.domain").Scope


class Clock:
    def __init__(self):
        self.now = 1800000000.0

    def __call__(self):
        return self.now


class Gateway:
    def __init__(self):
        self.roles = {"10001": "owner", "10002": "admin", "99999": "admin"}
        self.sent = []
        self.approvals = []
        self.kicks = []
        self.members = {}
        self.fail_approval = False
        self.fail_send = False
        self.fail_role = False

    async def role(self, scope, qq):
        if self.fail_role:
            raise TimeoutError()
        return self.roles.get(qq, "member")

    async def approve(self, scope, request, approve):
        self.approvals.append((scope.key, request["qq"], approve))
        if self.fail_approval:
            raise TimeoutError()

    async def kick(self, scope, qq):
        self.kicks.append((scope.key, qq))

    async def send(self, scope, text="", image=None, mention=None):
        if self.fail_send:
            raise TimeoutError()
        self.sent.append((scope.key, text, image, mention))
        return {"message_id": len(self.sent)}

    async def call(self, scope, action, **params):
        if action == "get_group_member_list":
            return [
                {
                    "user_id": qq,
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
        self.fail_formula = False

    async def card(self, profile, qq, flags, member, history, request, title, now, umo):
        self.cards.append((qq, flags, history, title))
        return "https://example.test/card.png", title

    async def formula(self, expression, umo):
        if self.fail_formula:
            raise RuntimeError("Render failed")
        return "https://example.test/formula.png"

    async def help(self, now, umo):
        return "https://example.test/help.png", "指令清单"


@pytest_asyncio.fixture
async def env():
    isolated = Path(tempfile.mkdtemp(prefix="case-", dir=os.environ["ASTRBOT_ROOT"]))
    db = Repository(isolated / "test.sqlite3", ROOT / "resource/sql")
    await db.open()
    clock, api, renderer = Clock(), Gateway(), Renderer()
    c = Controller(
        db,
        api,
        renderer,
        lambda qq, umo: qq == "10000",
        logging.getLogger("monitor-test"),
        clock,
        enabled_groups={"12345"},
    )
    scope = Scope("onebot", "99999", "12345")
    await db.execute(
        "ensure_group",
        {
            "scope": scope.key,
            "umo": "onebot:GroupMessage:12345",
            "actor": "10001",
            "now": clock(),
        },
    )
    yield types.SimpleNamespace(
        c=c, db=db, clock=clock, api=api, renderer=renderer, scope=scope
    )
    await c.stop()
    await db.close()


@pytest.fixture
def domain():
    return importlib.import_module("monitor_plugin.models.domain")
