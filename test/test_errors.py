"""Retain concise host errors without a diagnostic file writer."""

from types import SimpleNamespace

import pytest
from monitor_plugin.main import GroupManager
from monitor_plugin.utils.errors import error_detail, redact

from astrbot.api.star import StarTools


@pytest.mark.parametrize(
    "value",
    [
        "token=synthetic-secret",
        "Authorization: Bearer synthetic-secret",
        '"password": "synthetic-secret"',
    ],
)
def test_error_redaction(value):
    assert "synthetic-secret" not in redact(value)
    assert "<REDACTED>" in redact(value)


def test_error_details_are_bounded_and_include_exception_type():
    detail = error_detail(RuntimeError("token=synthetic-secret " + "x" * 1000))
    assert detail.startswith("RuntimeError: ")
    assert "synthetic-secret" not in detail and len(detail) <= 514


@pytest.mark.asyncio
async def test_lifecycle_uses_host_logger_without_diagnostic_files(monkeypatch, env):
    directory = env.root / "plugin"
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[]),
        get_config=lambda umo=None: {"admins_id": [], "plugin_set": ["*"]},
    )
    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: directory)
    plugin = GroupManager(context)
    host_logger = plugin.logger
    try:
        await plugin.initialize()
        assert plugin.logger is host_logger
        assert not (directory / "log").exists()
    finally:
        await plugin.terminate()
    assert not plugin.controller.workers and plugin.repository.db is None
