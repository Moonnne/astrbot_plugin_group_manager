"""Retention, redaction, lifecycle, and flow diagnostics without real data."""

import asyncio
import logging
import tempfile
from pathlib import Path

import pytest
from monitor_plugin.utils.plugin_log import PluginLog, event_summary, redact


def test_log_retains_latest_1000_lines_independently_of_console_level():
    directory = Path(tempfile.mkdtemp(prefix="monitor-log-test-"))
    host = logging.getLogger("monitor-log-retention-test")
    host.setLevel(logging.CRITICAL)
    log = PluginLog(host, directory / "log/monitor.log")
    for i in range(1200):
        log.debug("flow step=%s", i)
    log.close()
    rows = (directory / "log/monitor.log").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1000 and rows[0].endswith("step=200")
    assert rows[-1].endswith("step=1199") and "UTC+8" in rows[-1]
    assert not log.sink.worker.is_alive()
    assert not host.handlers


def test_log_restores_tail_and_redacts_secrets():
    directory = Path(tempfile.mkdtemp(prefix="monitor-log-test-"))
    path = directory / "monitor.log"
    path.write_bytes("\r\n".join(f"old {i}" for i in range(1100)).encode())
    log = PluginLog(logging.getLogger("monitor-log-redaction-test"), path)
    log.info("access_token=secret-value\nAuthorization: Bearer private-token")
    log.close()
    raw = path.read_bytes()
    assert b"secret-value" not in raw and b"private-token" not in raw
    assert raw.count(b"<REDACTED>") == 2
    assert len(raw.splitlines()) == 1000
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert "'password': '<REDACTED>'" == redact("'password': 'hidden'")


def test_event_summary_excludes_flag_and_comment_bodies():
    value = event_summary(
        {
            "post_type": "request",
            "group_id": 12345,
            "flag": "private-opaque-flag",
            "comment": "private reason",
        }
    )
    assert "private" not in str(value)
    assert value["comment_chars"] == 14 and len(value["flag_ref"]) == 12


@pytest.mark.asyncio
async def test_plugin_log_initializes_and_flushes_at_unload():
    from types import SimpleNamespace

    from monitor_plugin.main import GroupManager

    plugin = GroupManager(
        SimpleNamespace(
            get_config=lambda umo=None: {"admins_id": []},
            platform_manager=SimpleNamespace(platform_insts=[]),
        )
    )
    await plugin.initialize()
    sink = plugin.file_log.sink
    plugin.logger.info("test lifecycle checkpoint")
    await plugin.terminate()
    assert sink.path.parent.name == "log" and not sink.worker.is_alive()
    assert "test lifecycle checkpoint" in sink.path.read_text(encoding="utf-8")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_native_request_to_approval_has_a_complete_log_trace():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from monitor_plugin.core.rules import parse_command
    from monitor_plugin.main import GroupManager
    from monitor_plugin.models.scope import Scope
    from test_host_contract import adapter

    platform = adapter()
    client = platform.get_client()
    calls = []

    async def action(name, **params):
        calls.append(name)
        if name == "get_group_member_info":
            return {
                "user_id": params["user_id"],
                "role": "owner",
                "nickname": "Operator",
            }
        if name == "get_group_member_list":
            return []
        if name == "get_stranger_info":
            return {"user_id": 20001, "nickname": "Applicant", "qqLevel": 64}
        if name == "send_group_msg":
            return {"message_id": 42}
        if name == "set_group_add_request":
            return None
        raise AssertionError(name)

    client.call_action = action
    context = SimpleNamespace(
        get_config=lambda umo=None: {
            "admins_id": [],
            "plugin_set": ["*"],
            "platform_settings": {},
        },
        platform_manager=SimpleNamespace(platform_insts=[platform]),
        get_platform_inst=lambda name: platform,
        html_renderer=SimpleNamespace(
            render_custom_template=AsyncMock(
                return_value="https://example.test/card.png"
            )
        ),
    )
    plugin = GroupManager(context, {"enabled_groups": ["12345"]})
    await plugin.initialize()
    sink = plugin.file_log.sink
    try:
        scope = Scope("onebot", "99999", "12345")
        await plugin.controller.command(scope, "10001", parse_command("/g on"))
        await client._handle_event(
            {
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "self_id": 99999,
                "user_id": 20001,
                "group_id": 12345,
                "time": 1800000000,
                "flag": "do-not-log-opaque-handle",
                "comment": "do-not-log-reason",
            }
        )
        await plugin.controller.queue.join()
        await asyncio.gather(*list(plugin.controller.background))
        await plugin.controller.command(scope, "10001", parse_command("/g y"))
    finally:
        await plugin.terminate()
    output = sink.path.read_text(encoding="utf-8")
    for marker in (
        "event.inventory",
        "event.subscribed",
        "event.received",
        "queue.enqueued",
        "queue.dequeued",
        "flow.begin",
        "scope.check",
        "settings.read_complete",
        "db.batch_complete",
        "approval.accepted",
        "render.begin",
        "render.complete",
        "card.request_sent",
        "approval.selection",
        "approval.action_complete",
        "api.complete",
        "lifecycle.terminate_complete",
    ):
        assert marker in output, marker
    assert (
        "do-not-log-opaque-handle" not in output and "do-not-log-reason" not in output
    )
    assert "get_group_system_msg" not in calls


@pytest.mark.asyncio
async def test_cancelled_initialization_closes_created_log_worker(monkeypatch):
    import threading
    from types import SimpleNamespace

    import monitor_plugin.main as entry

    started = asyncio.Event()
    release = threading.Event()
    created = []
    loop = asyncio.get_running_loop()

    def factory(host, path):
        log = PluginLog(host, path)
        created.append(log)
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=2)
        return log

    monkeypatch.setattr(entry, "PluginLog", factory)
    plugin = entry.GroupManager(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[]))
    )
    task = asyncio.create_task(plugin.initialize())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created and not created[0].sink.worker.is_alive()
