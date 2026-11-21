"""Offline test boundary installed before application imports."""
import copy
import os
import sys
import tempfile
from pathlib import Path
import pytest

for name in list(os.environ):
    if name.startswith(("AWS_", "TCL_", "S3_", "SSM_", "TELEGRAM_", "DASHBOARD_", "SHELLY_")) or name in {
        "AIRBNB_ICAL_URL", "NOTIFICATION_WEBHOOK_URL", "SESSION_SECRET_KEY", "LAMBDA_TASK_ROOT",
        "MONITORING_ENABLED", "HEAT_DURING_BLOCKED_DATES", "HEATING_RUNTIME_DIR", "RATE_LIMIT_BACKEND",
        "WEATHER_LATITUDE", "WEATHER_LONGITUDE", "WEATHER_ELEVATION_M",
    }:
        os.environ.pop(name, None)
_runtime = tempfile.TemporaryDirectory(prefix="smart-heating-tests-")
os.environ.update(HEATING_LOAD_ENV="false", HEATING_RUNTIME_DIR=_runtime.name, AWS_EC2_METADATA_DISABLED="true")
_blocked = []


def _guard(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        _blocked.append(event)
        raise AssertionError("Unmocked network or subprocess access is forbidden in tests")


sys.addaudithook(_guard)


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch, tmp_path):
    from src.config import AutomationConfig
    import boto3
    _blocked.clear()
    values = {k: copy.deepcopy(v) for k, v in vars(AutomationConfig).items()
              if not k.startswith("__") and not isinstance(v, property)}
    # Real boto3 factories cannot read credentials or create a live client.
    def reject_client(*args, **kwargs):
        _blocked.append("boto3 factory")
        raise AssertionError("Tests must inject a mocked AWS client")
    monkeypatch.setattr(boto3, "client", reject_client)
    monkeypatch.setattr(boto3, "resource", reject_client)
    monkeypatch.setattr(boto3.session.Session, "client", reject_client)
    monkeypatch.setattr(boto3.session.Session, "resource", reject_client)
    # Existing behavior tests deliberately exercise telemetry and blocked stays.
    # Safer release defaults are checked separately in test_public_release.py.
    AutomationConfig.MONITORING_ENABLED = True
    AutomationConfig.HEAT_DURING_BLOCKED_DATES = True
    AutomationConfig.CALENDAR_CACHE_FILE = tmp_path / "calendar.ics"
    AutomationConfig.STATE_CACHE_FILE = tmp_path / "state.json"
    AutomationConfig.AUDIT_LOG_FILE = tmp_path / "audit.log"
    yield
    for key, value in values.items():
        setattr(AutomationConfig, key, value)
    assert not _blocked, "Blocked external access attempted: " + ", ".join(_blocked)


def pytest_sessionfinish(session, exitstatus):
    _runtime.cleanup()
