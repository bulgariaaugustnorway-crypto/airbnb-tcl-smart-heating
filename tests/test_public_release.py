"""Regression tests for public-release defaults and security boundaries."""
import io
import json
import logging
from pathlib import Path
import urllib.error
from unittest.mock import patch
import zipfile

import pytest
from src.config import AutomationConfig, PROJECT_ROOT
from src.calendar_parser import AirbnbCalendarParser
from src.dashboard import auth
from src.dashboard.handler import handle_dashboard_request
from src.tcl.client import TCLCloudClient, mask_secret
from infra.build_artifact import build_lambda_artifact, inspect_archive
from tools.scan_public import scan_tree, scan_text


def test_public_configuration_contains_no_credentials():
    cfg = AutomationConfig()
    assert cfg.DRY_RUN and cfg.TCL_READ_ONLY
    assert cfg.DEVICE_IDS == ["demo_living_room", "demo_bedroom"]
    assert not any([cfg.TCL_USERNAME, cfg.TCL_PASSWORD, cfg.AIRBNB_ICAL_URL, cfg.TELEGRAM_BOT_TOKEN,
                    cfg.TELEGRAM_CHAT_ID, cfg.S3_BUCKET, cfg.SESSION_SECRET_KEY, cfg.DASHBOARD_PASSWORD_HASH])
    text = (PROJECT_ROOT / "src/config.py").read_text(encoding="utf-8")
    assert 'MONITORING_ENABLED", "false"' in text
    assert 'HEAT_DURING_BLOCKED_DATES", "false"' in text


@pytest.mark.parametrize("secret", ["", "short"])
def test_dashboard_fails_closed_without_strong_session_key(secret):
    cfg = AutomationConfig()
    cfg.SESSION_SECRET_KEY = secret
    cfg.TCL_PASSWORD = "test-only-password-must-never-be-a-signing-key"
    result = handle_dashboard_request({"rawPath": "/"}, None, cfg)
    assert result["statusCode"] == 503


def test_dashboard_rejects_plaintext_and_malformed_hashes():
    password = "synthetic-test-password"
    assert not auth.verify_password(password, password)
    assert not auth.verify_password(password, "pbkdf2:sha256:" + "9" * 15 + "$abc$def")
    assert not auth.verify_password(password, "pbkdf2:sha256:not-an-int$abc$def")
    assert auth.verify_password(password, auth.hash_password(password))


def test_calendar_masks_entire_secret_url_and_exception(caplog, tmp_path):
    raw = "https://calendar.example.invalid/private-path-secret.ics?token=query-secret"
    assert AirbnbCalendarParser.mask_url(raw) == "[REDACTED_CALENDAR_URL]"
    parser = AirbnbCalendarParser(cache_file=tmp_path / "missing.ics")
    with patch("src.http_transport.open_https", side_effect=urllib.error.URLError(raw)):
        with caplog.at_level(logging.WARNING):
            result = parser.sync_calendar(url=raw)
    assert "private-path-secret" not in caplog.text + str(result.error_message)
    assert "query-secret" not in caplog.text + str(result.error_message)


def test_tcl_http_errors_omit_body_url_and_exception(caplog):
    raw = "https://tcl.example.invalid/private-token"
    error = urllib.error.HTTPError(raw, 401, "Unauthorized", {}, io.BytesIO(b"sensitive-body-secret"))
    with patch("src.http_transport.open_https", side_effect=error):
        with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as caught:
            TCLCloudClient()._http_post(raw, {}, {}, "Test")
    combined = caplog.text + str(caught.value)
    assert "sensitive-body-secret" not in combined
    assert "private-token" not in combined
    assert mask_secret("test-only-long-sensitive-value") == "***REDACTED***"


def test_public_tree_is_safe():
    assert not scan_tree()["findings"]


def test_scanner_rejects_injected_sensitive_material(tmp_path):
    (tmp_path / ".env").write_text("TCL_PASSWORD=synthetic-injected-secret")
    (tmp_path / "data/history").mkdir(parents=True)
    (tmp_path / "data/history/private.jsonl").write_text("{}")
    categories = {finding["category"] for finding in scan_tree(tmp_path)["findings"]}
    assert "private-configuration" in categories
    assert "runtime-or-private-artifact" in categories
    # Construct obvious synthetic detector probes without shipping token-shaped literals.
    assert scan_text("probe", "AKIA" + "Z" * 16)
    assert scan_text("probe", "123456789:" + "Z" * 35)


def test_allowlisted_artifact_excludes_private_and_development_files(tmp_path):
    archive = build_lambda_artifact(tmp_path / "runtime.zip")
    names = inspect_archive(archive)
    assert "lambda_handler.py" in names
    assert "src/orchestrator.py" in names
    assert all(name.endswith(".py") for name in names)
    assert all(not name.startswith(("data/", "tests/", ".env", ".git")) for name in names)


def test_archive_inspection_rejects_private_file(tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(".env", "TCL_PASSWORD=synthetic-test-only")
    with pytest.raises(ValueError):
        inspect_archive(path)


@pytest.mark.parametrize("url", ["http://example.invalid", "file:///private", "https://user:password@example.invalid"])
def test_transport_rejects_insecure_schemes_and_userinfo(url):
    from src.http_transport import validate_https
    with pytest.raises(ValueError):
        validate_https(url)


def test_transport_rejects_https_to_http_redirect():
    from src.http_transport import HTTPSOnlyRedirectHandler
    handler = HTTPSOnlyRedirectHandler()
    with pytest.raises(ValueError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://example.invalid")


def test_transport_rejects_cross_origin_redirect():
    from src.http_transport import HTTPSOnlyRedirectHandler
    from urllib.request import Request
    req = Request("https://first.example.invalid", headers={"Authorization": "test-only"})
    with pytest.raises(ValueError):
        HTTPSOnlyRedirectHandler().redirect_request(req, None, 302, "Found", {}, "https://second.example.invalid")



def test_dashboard_json_cannot_terminate_script_or_inject_markup():
    from src.dashboard.app import _script_json, render_dashboard_html
    payload = "</script><script>alert('synthetic')</script>"
    serialized = _script_json({"summary": payload})
    assert "</script>" not in serialized
    assert json.loads(serialized)["summary"] == payload
    rendered = render_dashboard_html("safe-csrf-token", {"summary": payload}, {})
    assert payload not in rendered
    assert "warnText.innerHTML" not in rendered
    assert "evBox.innerHTML" not in rendered
    assert "meterElem.innerHTML" not in rendered


def test_webhook_error_masks_whole_url_and_transport_details(caplog):
    from src.notifier import NotificationDispatcher, NotificationEvent
    secret_url = "https://webhook.example.invalid/private-path-secret?key=query-secret"
    cfg = AutomationConfig()
    cfg.NOTIFICATION_WEBHOOK_URL = secret_url
    dispatcher = NotificationDispatcher(config=cfg, webhook_url=secret_url)
    with patch("src.http_transport.open_https", side_effect=urllib.error.URLError(secret_url)):
        with caplog.at_level(logging.WARNING):
            success, error = dispatcher._send_webhook(NotificationEvent("TEST", "Test", "Synthetic"))
    assert not success
    assert "private-path-secret" not in caplog.text + str(error)
    assert "query-secret" not in caplog.text + str(error)
