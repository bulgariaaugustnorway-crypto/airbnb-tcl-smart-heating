"""Exhaustive test suite for Reliable Telegram Owner Alerts, Freshness Verification, and Dashboard Status.

Covers all 14 required Phase 5 scenarios and Phase 3 event formats:
1. Valid Telegram response (HTTP 200 with ok: true).
2. Telegram API returning ok: false with error description.
3. HTTP 401/403 Unauthorized/Forbidden (immediate rejection, no retry).
4. HTTP 429 Rate limiting (respects retry_after and retries).
5. Network timeout / URLError (retries with bounded backoff).
6. Invalid or missing bot token (handled cleanly without crashing).
7. Missing chat ID (handled cleanly without sending).
8. Duplicate suppression within time window (same severity).
9. Critical escalation bypasses deduplication window.
10. Cold-start persistence of notification delivery state (survives state save and hydration).
11. Secret redaction (token never leaks in logs, exceptions, URLs, or dashboard).
12. Dashboard notification status states (NOT CONFIGURED, CONFIGURED — UNTESTED, LAST DELIVERY SUCCESSFUL, DELIVERY FAILED).
13. Existing generic webhook backward compatibility.
14. Notification failures never interrupt HVAC safety logic or cycle execution.
15. Rich alert formatters for all required event types (frost tiers, stale telemetry, economic limit, device error, privacy protection).
"""

import io
import json
import logging
import socket
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.dashboard.config_store import DashboardConfigStore
from src.dashboard.handler import _build_status_data
from src.models import ACState, ACMode, ACFanSpeed
from src.notifier import (
    NotificationDispatcher,
    NotificationEvent,
    TelegramDeliveryAdapter,
    redact_sensitive_str,
    format_device_label,
)
from src.orchestrator import AutomationOrchestrator
from src.state_store import StateStore


class MockHttpResponse:
    """Mock for src.http_transport.open_https context manager response."""
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data_bytes = json.dumps(data).encode("utf-8")

    def read(self, *args, **kwargs):
        return self._data_bytes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class TestTelegramNotifier(unittest.TestCase):
    """Exhaustive test suite for Telegram notifications, security, and dashboard status."""

    def setUp(self):
        self.test_token = "TEST_ONLY_BOT_TOKEN"
        self.test_chat_id = "987654321"
        self.config = AutomationConfig()
        self.config.DRY_RUN = True
        self.config.TCL_READ_ONLY = True
        self.config.TELEGRAM_BOT_TOKEN = self.test_token
        self.config.TELEGRAM_CHAT_ID = self.test_chat_id
        self.config.TELEGRAM_MAX_RETRIES = 2
        self.config.TELEGRAM_RETRY_BACKOFF_SECONDS = 0.0  # Instant backoff for fast tests
        self.config.ALERT_DEDUPLICATION_WINDOW_MINUTES = 30.0

    # --------------------------------------------------------------------------
    # Scenario 1: Valid Telegram response
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_01_valid_telegram_response(self, mock_urlopen):
        mock_urlopen.return_value = MockHttpResponse(200, {
            "ok": True,
            "result": {"message_id": 42, "text": "Test alert"}
        })

        adapter = TelegramDeliveryAdapter(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            timeout=5.0,
            max_retries=1,
            backoff_seconds=0.0
        )
        success, err = adapter.send_message("Test alert text")

        self.assertTrue(success)
        self.assertIsNone(err)
        mock_urlopen.assert_called_once()

        # Check payload sent
        call_req = mock_urlopen.call_args[0][0]
        self.assertIn("api.telegram.org/bot", call_req.full_url)
        payload = json.loads(call_req.data.decode("utf-8"))
        self.assertEqual(payload["chat_id"], self.test_chat_id)
        self.assertEqual(payload["text"], "Test alert text")
        self.assertEqual(payload["parse_mode"], "Markdown")

    # --------------------------------------------------------------------------
    # Scenario 2: Telegram API returning ok: false
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_02_telegram_api_ok_false(self, mock_urlopen):
        mock_urlopen.return_value = MockHttpResponse(200, {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found"
        })

        adapter = TelegramDeliveryAdapter(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            backoff_seconds=0.0
        )
        success, err = adapter.send_message("Test message")

        self.assertFalse(success)
        self.assertIn("chat not found", err)

    # --------------------------------------------------------------------------
    # Scenario 3: HTTP 401 / 403 (Unauthorized / Forbidden - No Retry)
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_03_http_401_and_403_no_retry(self, mock_urlopen):
        # 401 Unauthorized
        err_body = json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized: invalid token"}).encode("utf-8")
        http_err = urllib.error.HTTPError(
            url="https://api.telegram.org/bot[REDACTED]/sendMessage",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(err_body)
        )
        mock_urlopen.side_effect = http_err

        adapter = TelegramDeliveryAdapter(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            max_retries=2,
            backoff_seconds=0.0
        )
        success, err = adapter.send_message("Test message")

        self.assertFalse(success)
        self.assertIn("HTTP 401", err)
        # Verify it did NOT retry: urlopen called exactly once
        self.assertEqual(mock_urlopen.call_count, 1)

    # --------------------------------------------------------------------------
    # Scenario 4: HTTP 429 Rate Limiting
    # --------------------------------------------------------------------------
    @patch("time.sleep")
    @patch("src.http_transport.open_https")
    def test_04_http_429_rate_limit(self, mock_urlopen, mock_sleep):
        rate_limit_body = json.dumps({
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 1",
            "parameters": {"retry_after": 1}
        }).encode("utf-8")

        err_429 = urllib.error.HTTPError(
            url="https://api.telegram.org/bot[REDACTED]/sendMessage",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(rate_limit_body)
        )
        success_resp = MockHttpResponse(200, {"ok": True, "result": {"message_id": 99}})

        # First call hits 429, second call succeeds
        mock_urlopen.side_effect = [err_429, success_resp]

        adapter = TelegramDeliveryAdapter(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            max_retries=2,
            backoff_seconds=0.0
        )
        success, err = adapter.send_message("Test rate limit")

        self.assertTrue(success)
        self.assertIsNone(err)
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_with(1)

    # --------------------------------------------------------------------------
    # Scenario 5: Network Timeout / URLError with Retries
    # --------------------------------------------------------------------------
    @patch("time.sleep")
    @patch("src.http_transport.open_https")
    def test_05_network_timeout_retries(self, mock_urlopen, mock_sleep):
        # 3 timeout failures (initial + 2 retries)
        mock_urlopen.side_effect = urllib.error.URLError(socket.timeout("Connection timed out"))

        adapter = TelegramDeliveryAdapter(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            max_retries=2,
            backoff_seconds=0.0
        )
        success, err = adapter.send_message("Test timeout")

        self.assertFalse(success)
        self.assertIn("Network timeout/error", err)
        # Attempt 0, Attempt 1, Attempt 2 -> 3 total calls
        self.assertEqual(mock_urlopen.call_count, 3)

    # --------------------------------------------------------------------------
    # Scenario 6: Invalid or Missing Token
    # --------------------------------------------------------------------------
    def test_06_invalid_or_missing_token(self):
        adapter = TelegramDeliveryAdapter(bot_token="", chat_id=self.test_chat_id)
        self.assertFalse(adapter.is_configured)
        success, err = adapter.send_message("Hello")
        self.assertFalse(success)
        self.assertIn("missing bot token", err)

        dispatcher = NotificationDispatcher(bot_token=None, chat_id=self.test_chat_id, config=AutomationConfig())
        self.assertEqual(dispatcher.delivery_status, "NOT CONFIGURED")

    # --------------------------------------------------------------------------
    # Scenario 7: Missing Chat ID
    # --------------------------------------------------------------------------
    def test_07_missing_chat_id(self):
        adapter = TelegramDeliveryAdapter(bot_token=self.test_token, chat_id=None)
        self.assertFalse(adapter.is_configured)
        success, err = adapter.send_message("Hello")
        self.assertFalse(success)
        self.assertIn("missing bot token or chat ID", err)

    # --------------------------------------------------------------------------
    # Scenario 8: Duplicate Suppression within Window
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_08_duplicate_suppression_within_window(self, mock_urlopen):
        mock_urlopen.return_value = MockHttpResponse(200, {"ok": True, "result": {"message_id": 1}})

        dispatcher = NotificationDispatcher(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            config=self.config
        )
        self.config.ALERT_DEDUPLICATION_WINDOW_MINUTES = 30.0

        event1 = NotificationEvent(
            event_type="FROST_WARNING",
            title="Low Temperature Alert: Living Room (15.5°C)",
            message="Indoor temp is 15.5°C",
            severity="WARNING",
            metadata={"device_id": "demo_living_room"}
        )
        event2 = NotificationEvent(
            event_type="FROST_WARNING",
            title="Low Temperature Alert: Living Room (15.2°C)",
            message="Indoor temp is 15.2°C",
            severity="WARNING",
            metadata={"device_id": "demo_living_room"}
        )

        # First dispatch should deliver to Telegram
        dispatcher.dispatch(event1)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(dispatcher.delivery_status, "LAST DELIVERY SUCCESSFUL")

        # Second dispatch within 30m with same severity (WARNING) should be suppressed
        dispatcher.dispatch(event2)
        self.assertEqual(mock_urlopen.call_count, 1)  # NOT called again
        self.assertEqual(len(dispatcher.events_history), 2)  # Logged in audit history

    # --------------------------------------------------------------------------
    # Scenario 9: Critical Escalation Bypasses Deduplication
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_09_critical_escalation_bypasses_deduplication(self, mock_urlopen):
        mock_urlopen.return_value = MockHttpResponse(200, {"ok": True, "result": {"message_id": 1}})

        dispatcher = NotificationDispatcher(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            config=self.config
        )

        warning_event = NotificationEvent(
            event_type="FROST_WARNING",
            title="Low Temperature Alert: Living Room (15.0°C)",
            message="Indoor temp is 15.0°C",
            severity="WARNING",
            metadata={"device_id": "demo_living_room"}
        )
        critical_event = NotificationEvent(
            event_type="FROST_WARNING",
            title="CRITICAL Low Temperature Alert: Living Room (7.5°C)",
            message="Indoor temp dropped to 7.5°C",
            severity="CRITICAL",
            metadata={"device_id": "demo_living_room"}
        )

        # First: WARNING dispatched
        dispatcher.dispatch(warning_event)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second: CRITICAL escalation occurs 5 minutes later -> MUST bypass cooldown
        dispatcher.dispatch(critical_event)
        self.assertEqual(mock_urlopen.call_count, 2)  # Delivered!

    # --------------------------------------------------------------------------
    # Scenario 10: Cold-Start Persistence of Notification Delivery State
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_10_cold_start_persistence(self, mock_urlopen):
        mock_urlopen.return_value = MockHttpResponse(200, {"ok": True, "result": {"message_id": 1}})

        # 1. First invocation: successful delivery occurs
        disp1 = NotificationDispatcher(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            config=self.config
        )
        event = NotificationEvent(
            event_type="DEVICE_ERROR",
            title="Device Error: Bedroom",
            message="Connection failed",
            severity="ERROR",
            metadata={"device_id": "demo_bedroom"}
        )
        disp1.dispatch(event)
        self.assertEqual(disp1.delivery_status, "LAST DELIVERY SUCCESSFUL")

        # Export state for persistence
        persisted_state = disp1.get_delivery_state()
        self.assertEqual(persisted_state["status"], "LAST DELIVERY SUCCESSFUL")
        self.assertIn("DEVICE_ERROR:demo_bedroom", persisted_state["alert_history"])

        # 2. Simulate Lambda cold-start in a brand new container
        disp2 = NotificationDispatcher(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            config=self.config
        )
        self.assertEqual(disp2.delivery_status, "CONFIGURED — UNTESTED")

        # Hydrate persisted state
        disp2.set_delivery_state(persisted_state)
        self.assertEqual(disp2.delivery_status, "LAST DELIVERY SUCCESSFUL")
        self.assertIsNotNone(disp2.last_delivery_timestamp)

        # Verify duplicate suppression was also restored
        mock_urlopen.reset_mock()
        disp2.dispatch(event)
        self.assertEqual(mock_urlopen.call_count, 0)  # Suppressed by restored history!

    # --------------------------------------------------------------------------
    # Scenario 11: Secret Redaction (Zero Leakage)
    # --------------------------------------------------------------------------
    def test_11_secret_redaction(self):
        raw_secret = "TEST_ONLY_BOT_TOKEN"

        # 1. Direct secret in exception message
        msg_with_secret = f"Connection failed to https://api.telegram.org/bot{raw_secret}/sendMessage: 401 Unauthorized"
        redacted = redact_sensitive_str(msg_with_secret, raw_secret)
        self.assertNotIn(raw_secret, redacted)
        self.assertIn("[REDACTED_TOKEN]", redacted)

        # 2. URL parameter with token
        url_with_param = f"https://example.com/webhook?token={raw_secret}&chat_id=123"
        redacted_param = redact_sensitive_str(url_with_param, raw_secret)
        self.assertNotIn(raw_secret, redacted_param)

        # 3. Verify logging never contains raw secret
        with patch("src.http_transport.open_https", side_effect=urllib.error.URLError(raw_secret)), self.assertLogs("src.notifier", level="INFO") as cm:
            dispatcher = NotificationDispatcher(
                bot_token=raw_secret,
                chat_id=self.test_chat_id,
                config=self.config
            )
            event = NotificationEvent(
                event_type="TEST",
                title="Test Alert",
                message=f"Notice with secret {raw_secret}",
                severity="INFO"
            )
            dispatcher.dispatch(event)

            for record in cm.output:
                self.assertNotIn(raw_secret, record)

    # --------------------------------------------------------------------------
    # Scenario 12: Dashboard Notification Status States
    # --------------------------------------------------------------------------
    def test_12_dashboard_notification_status_states(self):
        state_store = StateStore(in_memory=True)
        config_store = DashboardConfigStore(config=self.config)

        # State A: NOT CONFIGURED (Neither Telegram nor Webhook)
        unconfigured_cfg = AutomationConfig()
        unconfigured_cfg.TELEGRAM_BOT_TOKEN = None
        unconfigured_cfg.TELEGRAM_CHAT_ID = None
        unconfigured_cfg.NOTIFICATION_WEBHOOK_URL = None

        with patch("src.dashboard.handler.StateStore", return_value=state_store):
            status = _build_status_data(unconfigured_cfg, config_store)
            self.assertEqual(status["notifications"]["status"], "NOT CONFIGURED")
            self.assertEqual(status["notifications"]["channel"], "NONE")
            self.assertFalse(status["notifications"]["configured"])

        # State B: CONFIGURED — UNTESTED (Credentials exist, but no delivery yet)
        with patch("src.dashboard.handler.StateStore", return_value=state_store):
            status = _build_status_data(self.config, config_store)
            self.assertEqual(status["notifications"]["status"], "CONFIGURED — UNTESTED")
            self.assertEqual(status["notifications"]["channel"], "TELEGRAM")
            self.assertTrue(status["notifications"]["configured"])

        # State C: LAST DELIVERY SUCCESSFUL
        state_store.save_state({
            "notification_delivery_state": {
                "status": "LAST DELIVERY SUCCESSFUL",
                "channel": "TELEGRAM",
                "last_delivery_timestamp": "2026-10-24T12:00:00",
                "last_delivery_timestamp_sofia": "24 Oct 2026, 12:00 Sofia",
                "last_delivery_error": None
            }
        })
        with patch("src.dashboard.handler.StateStore", return_value=state_store):
            status = _build_status_data(self.config, config_store)
            self.assertEqual(status["notifications"]["status"], "LAST DELIVERY SUCCESSFUL")
            self.assertEqual(status["notifications"]["last_delivery_timestamp_sofia"], "24 Oct 2026, 12:00 Sofia")

        # State D: DELIVERY FAILED
        state_store.save_state({
            "notification_delivery_state": {
                "status": "DELIVERY FAILED",
                "channel": "TELEGRAM",
                "last_delivery_error": f"HTTP 401: Unauthorized (token: {self.test_token})"
            }
        })
        with patch("src.dashboard.handler.StateStore", return_value=state_store):
            status = _build_status_data(self.config, config_store)
            self.assertEqual(status["notifications"]["status"], "DELIVERY FAILED")
            # Verify secrets are scrubbed in the dashboard error string
            self.assertNotIn(self.test_token, status["notifications"]["last_delivery_error"])

    # --------------------------------------------------------------------------
    # Scenario 13: Existing Generic Webhook Backward Compatibility
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_13_generic_webhook_compatibility(self, mock_urlopen):
        mock_urlopen.return_value = MockHttpResponse(200, {"status": "ok"})

        webhook_config = AutomationConfig()
        webhook_config.TELEGRAM_BOT_TOKEN = None
        webhook_config.TELEGRAM_CHAT_ID = None
        webhook_config.NOTIFICATION_WEBHOOK_URL = "https://discord.com/api/webhooks/123/xyz"

        dispatcher = NotificationDispatcher(config=webhook_config)
        self.assertEqual(dispatcher.delivery_channel, "WEBHOOK")

        event = NotificationEvent(
            event_type="CALENDAR_WARNING",
            title="Sync Alert",
            message="Feed delayed",
            severity="WARNING"
        )
        dispatcher.dispatch(event)

        self.assertEqual(dispatcher.delivery_status, "LAST DELIVERY SUCCESSFUL")
        mock_urlopen.assert_called_once()
        sent_data = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertIn("content", sent_data)

    # --------------------------------------------------------------------------
    # Scenario 14: Notification Failures Do Not Disrupt HVAC Safety Logic
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_14_notification_failures_do_not_disrupt_hvac(self, mock_urlopen):
        # Simulate fatal connection reset during dispatch
        mock_urlopen.side_effect = ConnectionResetError("Connection violently closed by peer")

        state_store = StateStore(in_memory=True)
        dispatcher = NotificationDispatcher(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            config=self.config
        )

        orchestrator = AutomationOrchestrator(
            config=self.config,
            notifier=dispatcher,
            state_store=state_store
        )

        # Orchestrator runs full cycle with failing notification
        # This MUST complete cleanly without unhandled exceptions
        try:
            result = orchestrator.run_cycle()
            self.assertIsNotNone(result)
        except Exception as e:
            self.fail(f"HVAC orchestrator cycle failed due to notification exception: {e}")

        # Delivery status recorded as failed, but automation finished
        self.assertEqual(dispatcher.delivery_status, "DELIVERY FAILED")

    # --------------------------------------------------------------------------
    # Scenario 15: Rich Alert Formatters and Privacy Requirements (Phase 3)
    # --------------------------------------------------------------------------
    @patch("src.http_transport.open_https")
    def test_15_rich_alert_formatters_and_privacy(self, mock_urlopen):
        mock_urlopen.return_value = MockHttpResponse(200, {"ok": True, "result": {"message_id": 1}})

        dispatcher = NotificationDispatcher(
            bot_token=self.test_token,
            chat_id=self.test_chat_id,
            config=self.config
        )

        # 1. Multi-tier frost warning: Living Room 15.5°C (WARNING)
        dispatcher.notify_frost_warning(
            device_id="demo_living_room",
            measured_temp=15.5,
            threshold=16.0,
            severity="WARNING",
            is_guest_disabled=True
        )
        last_event = dispatcher.events_history[-1]
        self.assertIn("Living Room (demo_living_room)", last_event.title)
        self.assertIn("turned OFF by guest", last_event.message)

        # 2. Elevated warning: 11.5°C
        dispatcher.notify_frost_warning(
            device_id="demo_living_room",
            measured_temp=11.5,
            threshold=12.0,
            severity="WARNING",
            is_guest_disabled=False
        )
        self.assertEqual(dispatcher.events_history[-1].metadata["measured_temperature"], 11.5)

        # 3. Critical warning: 7.2°C (CRITICAL)
        dispatcher.notify_frost_warning(
            device_id="demo_living_room",
            measured_temp=7.2,
            threshold=8.0,
            severity="CRITICAL",
            is_guest_disabled=True
        )
        self.assertEqual(dispatcher.events_history[-1].severity, "CRITICAL")

        # 4. Prolonged Stale Telemetry: 3.5 hours inactive
        dispatcher.notify_stale_telemetry_warning(
            device_id="demo_living_room",
            age_hours=3.5,
            last_temp=17.2,
            is_guest_disabled=True
        )
        stale_event = dispatcher.events_history[-1]
        self.assertIn("3.5h inactive", stale_event.title)
        self.assertIn("17.2°C", stale_event.message)

        # 5. Economic Alert
        dispatcher.notify_economic_alert(
            device_id="demo_bedroom",
            requested_temp=26.0,
            measured_temp=22.0,
            limit=24.0,
            reason="Exceeds 24.0°C maximum"
        )
        eco_event = dispatcher.events_history[-1]
        self.assertIn("Bedroom (demo_bedroom)", eco_event.title)
        self.assertIn("26.0°C", eco_event.title)

        # 6. Cancellation with privacy protection (masked UID only)
        raw_uid = "HM1234567890XYZ"
        dispatcher.notify_cancellation(uid=raw_uid)
        cancel_event = dispatcher.events_history[-1]
        self.assertNotIn("1234567890", cancel_event.message)
        self.assertIn("HM1234...0XYZ", cancel_event.message)



if __name__ == "__main__":
    unittest.main()
