"""Comprehensive test suite for Phase 5 Owner Dashboard, Silent Night Mode, and Hardware Controls."""

import json
import time
from datetime import datetime, date, time as dtime, timedelta
from typing import Dict, Any

import pytest

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.dashboard import auth
from src.dashboard.config_store import DashboardConfigStore
from src.dashboard.handler import handle_dashboard_request
from src.device_controller import MockACController
from src.models import (
    ACCommand,
    ACMode,
    ACFanSpeed,
    ACState,
    Reservation,
    ReservationType,
    ScheduleDecision,
    SystemState,
    OwnerTestSession
)
from src.orchestrator import AutomationOrchestrator
from src.safeguards import SafeguardEngine
from src.scheduler import HVACScheduleEngine
from src.state_store import StateStore
from src.tcl.phase5_verifier import Phase5OnsiteVerifier
from lambda_handler import lambda_handler, dashboard_handler


class TestDashboardAuthenticationAndSecurity:
    """Tests for PBKDF2 hashing, HMAC session tokens, brute-force lockout, and CSRF protection."""

    def test_password_hashing_and_verification(self):
        secret = "OwnerSuperSecret2026!"
        hashed = auth.hash_password(secret)
        assert "$" in hashed

        # Verify correct password
        assert auth.verify_password(secret, hashed) is True

        # Verify incorrect password
        assert auth.verify_password("WrongPassword!", hashed) is False
        assert auth.verify_password("", hashed) is False
        assert auth.verify_password(secret, "") is False

    def test_direct_plaintext_password_comparison(self):
        secret = "PlaintextSecretKey123"
        assert auth.verify_password(secret, secret) is False
        assert auth.verify_password("Wrong", secret) is False

    def test_rate_limiting_and_lockout(self):
        ip = "192.168.1.100"
        auth.clear_failed_attempts(ip)

        limited, rem = auth.is_rate_limited(ip)
        assert limited is False

        # Record 4 failed attempts - not yet locked
        for _ in range(4):
            lockout = auth.record_failed_attempt(ip)
            assert lockout == 0

        limited, rem = auth.is_rate_limited(ip)
        assert limited is False

        # 5th failed attempt triggers 15-minute lockout
        lockout = auth.record_failed_attempt(ip)
        assert lockout == auth.LOCKOUT_DURATION_SECONDS

        limited, rem = auth.is_rate_limited(ip)
        assert limited is True
        assert rem > 0

        # Success clears lockout
        auth.clear_failed_attempts(ip)
        limited, _ = auth.is_rate_limited(ip)
        assert limited is False

    def test_session_token_lifecycle(self):
        secret_key = "test-secret-synthetic-session-key-for-tests-only-hmac-key"
        token = auth.create_session_token(secret_key, duration_seconds=3600)
        assert auth.verify_session_token(token, secret_key) is True

        # Tampered token
        tampered = token[:-4] + "abcd"
        assert auth.verify_session_token(tampered, secret_key) is False

        # Wrong key
        assert auth.verify_session_token(token, "different-key") is False

        # Expired token
        expired_token = auth.create_session_token(secret_key, duration_seconds=-10)
        assert auth.verify_session_token(expired_token, secret_key) is False

    def test_csrf_token_validation(self):
        secret_key = "test-secret-synthetic-session-key-for-tests-only-hmac-key"
        session_token = auth.create_session_token(secret_key, duration_seconds=3600)
        csrf = auth.generate_csrf_token(session_token, secret_key)

        assert auth.verify_csrf_token(csrf, session_token, secret_key) is True
        assert auth.verify_csrf_token("invalid_csrf", session_token, secret_key) is False
        assert auth.verify_csrf_token(None, session_token, secret_key) is False

        other_session = auth.create_session_token(secret_key, duration_seconds=3600)
        assert auth.verify_csrf_token(csrf, other_session, secret_key) is False


class TestDashboardConfigStore:
    """Tests for user configuration persistence and hydration."""

    @pytest.fixture(autouse=True)
    def isolate_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.dashboard.config_store.CACHE_DIR", tmp_path)

    def test_default_config_values(self):
        cfg = AutomationConfig()
        store = DashboardConfigStore(config=cfg)
        loaded = store.load_dashboard_config()

        assert loaded["schedule_night_temp"] == 20.0
        assert loaded["schedule_night_fan"] == "Quiet"
        assert loaded["default_led_display"] is True
        assert loaded["night_led_display"] is False
        assert loaded["beep_feedback_enabled"] is False
        assert loaded["heat_during_blocked_dates"] is True
        assert loaded["living_room_led_mode"] == "SCHEDULE"
        assert loaded["bedroom_led_mode"] == "SCHEDULE"

    def test_save_and_apply_to_config(self):
        cfg = AutomationConfig()
        store = DashboardConfigStore(config=cfg)

        new_settings = {
            "schedule_night_temp": 19.5,
            "schedule_night_fan": "Quiet",
            "default_led_display": True,
            "beep_feedback_enabled": False,
            "bedroom_temp_offset": -1.0,
            "living_room_led_mode": "ALWAYS_OFF",
            "bedroom_led_mode": "ALWAYS_ON"
        }

        # Mock S3 save or use in-memory state
        target_cfg = AutomationConfig()
        # Mock load returning new_settings
        store.load_dashboard_config = lambda: dict(new_settings)

        hydrated = store.apply_to_config(target_cfg)
        assert hydrated.SCHEDULE_NIGHT_TEMP == 19.5
        assert hydrated.DEFAULT_LED_DISPLAY is True
        assert hydrated.BEDROOM_TEMP_OFFSET == -1.0
        assert hydrated.LIVING_ROOM_LED_MODE == "ALWAYS_OFF"
        assert hydrated.BEDROOM_LED_MODE == "ALWAYS_ON"


class TestSilentNightModeSchedule:
    """Tests for 20°C, Quiet fan speed, LED OFF, and Beep OFF between 23:00 and 07:00."""

    def test_night_schedule_evaluation(self):
        cfg = AutomationConfig()
        cfg.DEFAULT_LED_DISPLAY = False
        cfg.NIGHT_LED_DISPLAY = False
        cfg.BEEP_FEEDBACK_ENABLED = False

        engine = HVACScheduleEngine(config=cfg)

        # 02:00 night time
        night_dt = datetime(2026, 10, 20, 2, 0, tzinfo=TIMEZONE_SOFIA)
        res = Reservation(
            uid="NIGHT_GUEST_1",
            start_date=date(2026, 10, 19),
            end_date=date(2026, 10, 22),
            summary="Night Guest",
            reservation_type=ReservationType.CONFIRMED,
            raw_status="CONFIRMED"
        )

        decision: ScheduleDecision = engine.evaluate(reservations=[res], current_dt=night_dt)

        assert decision.system_state == SystemState.OCCUPIED_NIGHT
        assert decision.target_power is True
        assert decision.target_temperature == 20.0
        assert decision.target_fan_speed == ACFanSpeed.QUIET
        assert decision.target_display_led is False
        assert decision.target_beep is False

        for dev_id, cmd in decision.commands_to_send.items():
            assert cmd.power is True
            assert cmd.fan_speed == ACFanSpeed.QUIET
            assert cmd.display_led is False
            assert cmd.beep_feedback is False

            shadow = cmd.to_shadow_payload()["state"]["desired"]
            assert shadow["beepSwitch"] == 0
            assert shadow["screen"] == 0
            assert shadow["windSpeed7Gear"] == 1


class TestOwnerTestMode:
    """Tests for temporary owner on-site test sessions."""

    def test_owner_test_session_lifecycle(self):
        now = datetime.now(TIMEZONE_SOFIA)
        session = OwnerTestSession(
            active=True,
            started_at=now,
            expires_at=now + timedelta(hours=2),
            duration_hours=2.0,
            target_temperature=24.5,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            display_led=False,
            living_room_power=True,
            bedroom_power=True,
            created_by="owner_test"
        )

        assert session.is_active(now + timedelta(hours=1)) is True
        assert session.is_active(now + timedelta(hours=3)) is False

    def test_owner_test_overrides_unoccupied_schedule(self):
        cfg = AutomationConfig()
        engine = HVACScheduleEngine(config=cfg)

        now = datetime(2026, 10, 20, 12, 0, tzinfo=TIMEZONE_SOFIA)

        # No guest reservations (normally STANDBY_OFF)
        session = OwnerTestSession(
            active=True,
            started_at=now,
            expires_at=now + timedelta(hours=2),
            duration_hours=2.0,
            target_temperature=24.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.LOW,
            display_led=False,
            living_room_power=True,
            bedroom_power=False
        )

        decision = engine.evaluate(reservations=[], current_dt=now, owner_test=session)

        assert decision.system_state == SystemState.OWNER_TEST_HOLD
        assert decision.target_power is True
        assert decision.target_temperature == 24.0
        assert decision.target_fan_speed == ACFanSpeed.LOW

        # Living room should be ON at 24°C, Bedroom should be OFF
        lr_cmd = decision.commands_to_send.get(cfg.TCL_LIVING_ROOM_ID)
        br_cmd = decision.commands_to_send.get(cfg.TCL_BEDROOM_ID)

        assert lr_cmd is not None
        assert lr_cmd.power is True
        assert lr_cmd.target_temperature == 24.0

        assert br_cmd is not None
        assert br_cmd.power is False


class TestOwnerDashboardHttpRouter:
    """Tests for Lambda Function URL routing, login, status, config, and owner test APIs."""

    @pytest.fixture(autouse=True)
    def isolate_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.dashboard.config_store.CACHE_DIR", tmp_path)

    @pytest.fixture
    def test_config(self):
        cfg = AutomationConfig()
        cfg.DASHBOARD_PASSWORD_HASH = auth.hash_password("Sofia2026Owner!")
        cfg.SESSION_SECRET_KEY = "test-session-secret-key-12345-synthetic-session-key-for-tests-only"
        cfg.DRY_RUN = True
        cfg.TCL_READ_ONLY = True
        return cfg

    def test_get_root_unauthenticated_serves_login(self, test_config):
        event = {
            "version": "2.0",
            "rawPath": "/",
            "requestContext": {"http": {"method": "GET", "sourceIp": "10.0.0.1"}},
            "headers": {}
        }
        resp = handle_dashboard_request(event, None, config=test_config)
        assert resp["statusCode"] == 200
        assert "Owner Access Password" in resp["body"]
        assert "Content-Security-Policy" in resp["headers"]

    def test_login_success_and_logout(self, test_config):
        # 1. Login
        login_event = {
            "version": "2.0",
            "rawPath": "/api/login",
            "requestContext": {"http": {"method": "POST", "sourceIp": "10.0.0.1"}},
            "headers": {"content-type": "application/x-www-form-urlencoded"},
            "body": "password=Sofia2026Owner!"
        }
        login_resp = handle_dashboard_request(login_event, None, config=test_config)
        assert login_resp["statusCode"] == 302
        cookie_header = login_resp["headers"].get("Set-Cookie", "")
        assert "session_token=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=Strict" in cookie_header

        # Extract session token
        token_val = cookie_header.split("session_token=")[1].split(";")[0]

        # 2. Access dashboard with valid session
        dash_event = {
            "version": "2.0",
            "rawPath": "/",
            "requestContext": {"http": {"method": "GET", "sourceIp": "10.0.0.1"}},
            "headers": {"cookie": f"session_token={token_val}"}
        }
        dash_resp = handle_dashboard_request(dash_event, None, config=test_config)
        assert dash_resp["statusCode"] == 200
        assert "TCL Climate Control - Owner Dashboard" in dash_resp["body"]

        # 3. Logout
        logout_event = {
            "version": "2.0",
            "rawPath": "/api/logout",
            "requestContext": {"http": {"method": "POST", "sourceIp": "10.0.0.1"}},
            "headers": {"cookie": f"session_token={token_val}"}
        }
        logout_resp = handle_dashboard_request(logout_event, None, config=test_config)
        assert logout_resp["statusCode"] == 302
        assert "Max-Age=0" in logout_resp["headers"]["Set-Cookie"]

    def test_api_status_requires_auth(self, test_config):
        event = {
            "version": "2.0",
            "rawPath": "/api/status",
            "requestContext": {"http": {"method": "GET", "sourceIp": "10.0.0.1"}},
            "headers": {}
        }
        resp = handle_dashboard_request(event, None, config=test_config)
        assert resp["statusCode"] == 401

    def test_api_config_csrf_protection(self, test_config):
        token = auth.create_session_token(test_config.SESSION_SECRET_KEY)
        csrf = auth.generate_csrf_token(token, test_config.SESSION_SECRET_KEY)

        # Missing CSRF
        bad_event = {
            "version": "2.0",
            "rawPath": "/api/config",
            "requestContext": {"http": {"method": "POST", "sourceIp": "10.0.0.1"}},
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json"
            },
            "body": json.dumps({"schedule_night_temp": 19.0})
        }
        bad_resp = handle_dashboard_request(bad_event, None, config=test_config)
        assert bad_resp["statusCode"] == 403

        # Valid CSRF
        good_event = {
            "version": "2.0",
            "rawPath": "/api/config",
            "requestContext": {"http": {"method": "POST", "sourceIp": "10.0.0.1"}},
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": csrf
            },
            "body": json.dumps({
                "schedule_night_temp": 19.5,
                "schedule_night_fan": "Quiet",
                "default_led_display": False,
                "night_led_display": False,
                "beep_feedback_enabled": False
            })
        }
        good_resp = handle_dashboard_request(good_event, None, config=test_config)
        assert good_resp["statusCode"] == 200
        data = json.loads(good_resp["body"])
        assert data["status"] == "success"
        assert data["config"]["schedule_night_temp"] == 19.5

    def test_lambda_handler_dual_dispatch(self, test_config, monkeypatch):
        # 1. HTTP request to Lambda entrypoint
        http_event = {
            "version": "2.0",
            "rawPath": "/",
            "requestContext": {"http": {"method": "GET", "sourceIp": "10.0.0.1"}},
            "headers": {}
        }
        monkeypatch.setattr(AutomationConfig, "SESSION_SECRET_KEY", test_config.SESSION_SECRET_KEY)
        monkeypatch.setattr(AutomationConfig, "DASHBOARD_PASSWORD_HASH", test_config.DASHBOARD_PASSWORD_HASH)
        resp = lambda_handler(http_event, None)
        assert resp["statusCode"] == 200
        assert "Owner Access Password" in resp["body"]

        # 2. EventBridge scheduled cron request to Lambda entrypoint
        cron_event = {
            "source": "aws.events",
            "detail-type": "Scheduled Event",
            "time": "2026-10-20T12:00:00Z"
        }
        cron_resp = lambda_handler(cron_event, None)
        assert cron_resp["statusCode"] == 200
        assert cron_resp["body"]["status"] == "success"


class TestPhase5OnsiteVerifier:
    """Tests for on-site silent night and safeguard verifications."""

    def test_verifier_all_tests_pass_in_mock(self):
        controller = MockACController(device_ids=["tcl_ac_living_room", "tcl_ac_bedroom"])
        verifier = Phase5OnsiteVerifier(controller=controller)

        result = verifier.run_all_tests(target_device_id="tcl_ac_living_room", dry_run=True)
        assert result.success is True
        assert result.status_code == "SUCCESS"
        assert len(result.test_results) == 4

        assert result.test_results["compressor_anti_cycling"]["success"] is True
        assert result.test_results["beep_suppression"]["success"] is True
        assert result.test_results["led_off_safety"]["success"] is True
        assert result.test_results["running_only_fan"]["success"] is True

    def test_test_led_display_granular(self):
        controller = MockACController(device_ids=["tcl_ac_living_room"])
        verifier = Phase5OnsiteVerifier(controller=controller, sync_wait_seconds=0.01)

        # Baseline: display_led is False
        dry_res = verifier.test_led_display("tcl_ac_living_room", dry_run=True)
        assert dry_res["success"] is True
        assert dry_res["dry_run"] is True
        assert dry_res["tested_value"] == 1

        # Live test
        live_res = verifier.test_led_display("tcl_ac_living_room", target_screen=1, dry_run=False)
        assert live_res["success"] is True
        assert live_res["verified_value"] == 1
        assert controller.get_state("tcl_ac_living_room").display_led is True

        # Restore
        rest_res = verifier.restore_attribute("tcl_ac_living_room", "screen", 0, dry_run=False)
        assert rest_res["success"] is True
        assert controller.get_state("tcl_ac_living_room").display_led is False

    def test_test_beep_switch_granular(self):
        controller = MockACController(device_ids=["tcl_ac_living_room"])
        verifier = Phase5OnsiteVerifier(controller=controller, sync_wait_seconds=0.01)

        # Live test: set beepSwitch to 0 (Mute)
        res = verifier.test_beep_switch("tcl_ac_living_room", target_beep=0, dry_run=False)
        assert res["success"] is True
        assert res["verified_value"] == 0
        assert controller.get_state("tcl_ac_living_room").beep_feedback is False

    def test_test_quiet_fan_safeguards(self):
        controller = MockACController(device_ids=["tcl_ac_living_room"])
        verifier = Phase5OnsiteVerifier(controller=controller, sync_wait_seconds=0.01)

        # Unit is OFF -> Fan speed test MUST abort and not turn on AC
        off_res = verifier.test_quiet_fan("tcl_ac_living_room", dry_run=False)
        assert off_res["success"] is False
        assert off_res.get("aborted") is True
        assert controller.get_state("tcl_ac_living_room").power is False

        # Turn unit ON, now fan test succeeds
        controller.states["tcl_ac_living_room"].power = True
        on_res = verifier.test_quiet_fan("tcl_ac_living_room", dry_run=False)
        assert on_res["success"] is True
        assert on_res["verified_value"] == "Quiet"
        assert controller.get_state("tcl_ac_living_room").fan_speed == ACFanSpeed.QUIET

    def test_test_power_control_anti_cycling(self):
        controller = MockACController(device_ids=["tcl_ac_living_room"])
        verifier = Phase5OnsiteVerifier(controller=controller, sync_wait_seconds=0.01)

        # Initial power test toggles from 0 to 1
        res1 = verifier.test_power_control("tcl_ac_living_room", target_power=1, dry_run=False)
        assert res1["success"] is True
        assert res1["verified_value"] == 1
        assert controller.get_state("tcl_ac_living_room").power is True

        # Immediate second toggle attempt MUST be blocked by 10-minute anti-cycling lock
        res2 = verifier.test_power_control("tcl_ac_living_room", target_power=0, dry_run=False)
        assert res2["success"] is False
        assert res2.get("locked") is True
        assert controller.get_state("tcl_ac_living_room").power is True

    def test_system_mode_manual_schedule(self):
        """Verify SYSTEM_MODE='MANUAL' keeps ACs active with diurnal comfort temps without calendar."""
        cfg = AutomationConfig()
        cfg.SYSTEM_MODE = "MANUAL"
        cfg.SCHEDULE_DAY_TEMP = 21.5
        cfg.SCHEDULE_NIGHT_TEMP = 20.0
        engine = HVACScheduleEngine(config=cfg)

        # Daytime: 14:00 Sofia
        dt_day = datetime(2026, 10, 20, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_day = engine.evaluate(reservations=[], current_dt=dt_day)
        assert dec_day.target_power is True
        assert dec_day.target_temperature == 21.5
        assert dec_day.system_state == SystemState.OCCUPIED_DAY
        assert "manual override" in dec_day.decision_reason.lower()

        # Nighttime: 23:30 Sofia
        dt_night = datetime(2026, 10, 20, 23, 30, tzinfo=TIMEZONE_SOFIA)
        dec_night = engine.evaluate(reservations=[], current_dt=dt_night)
        assert dec_night.target_power is True
        assert dec_night.target_temperature == 20.0
        assert dec_night.target_fan_speed == ACFanSpeed.QUIET
        assert dec_night.target_display_led is False
        assert dec_night.system_state == SystemState.OCCUPIED_NIGHT

    def test_status_endpoint_occupancy_and_safety_banner(self, monkeypatch, tmp_path):
        """Verify /api/status exposes occupancy details, safety mode banner, and verified hardware IDs."""
        cfg = AutomationConfig()
        cfg.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only-key-321-synthetic-session-key-for-tests-only"
        cfg.DASHBOARD_PASSWORD_HASH = auth.hash_password("SuperSecret2026!")
        cfg.TCL_LIVING_ROOM_ID = "demo_living_room"
        cfg.TCL_BEDROOM_ID = "demo_bedroom"
        cfg.DRY_RUN = True
        cfg.TCL_READ_ONLY = True

        store = DashboardConfigStore(config=cfg)
        secret_key = auth.create_session_token(cfg.SESSION_SECRET_KEY)

        event = {
            "rawPath": "/api/status",
            "requestContext": {"http": {"method": "GET", "sourceIp": "127.0.0.1"}},
            "cookies": [f"session_token={secret_key}"]
        }

        resp = handle_dashboard_request(event, context=None, config=cfg)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])

        # Check safety banner fields
        assert data["automation_mode"] == "READ_ONLY"
        assert "Read-Only" in data["automation_label"]
        assert data["dry_run"] is True
        assert data["read_only"] is True

        # Check verified hardware IDs
        assert data["verified_hardware"]["living_room_id"] == "demo_living_room"
        assert data["verified_hardware"]["bedroom_id"] == "demo_bedroom"

        # Check occupancy data
        assert "occupancy" in data
        assert "status" in data["occupancy"]
        assert "counts" in data["occupancy"]
        assert "calendar_stale" in data["occupancy"]

    def test_persistent_rate_limiting_across_reload(self, tmp_path):
        """Verify failed login attempts survive in-memory cache clearing via file persistence."""
        ip = "192.168.10.99"
        auth.clear_failed_attempts(ip)

        # Record 5 failed attempts to lock out IP
        for _ in range(5):
            auth.record_failed_attempt(ip)

        limited, rem = auth.is_rate_limited(ip)
        assert limited is True
        assert rem > 0

        # Simulate Lambda container cold start / Python module restart: wipe in-memory dict
        auth._FAILED_ATTEMPTS.clear()

        # is_rate_limited should reload from persistent file store and STILL be locked out
        limited_after_reload, rem_after_reload = auth.is_rate_limited(ip)
        assert limited_after_reload is True
        assert rem_after_reload > 0

        # Clean up
        auth.clear_failed_attempts(ip)
        limited_cleared, _ = auth.is_rate_limited(ip)
        assert limited_cleared is False


class TestPhase5ProductionReadinessRegressions:
    """Specific regression tests for Phase 5 safety corrections and production audit."""

    def test_paused_mode_preserves_device_state(self):
        """
        PAUSED mode must suspend all automatic HVAC commands without changing current physical states.
        - Does NOT turn off units.
        - Does NOT change setpoints, fan speeds, or displays.
        - Dispatches zero commands.
        - Resumes normal scheduling upon returning to AUTO.
        """
        cfg = AutomationConfig()
        cfg.DRY_RUN = False
        cfg.SYSTEM_MODE = "PAUSED"

        controller = MockACController(device_ids=cfg.DEVICE_IDS)
        # Set initial physical device states: Living room and Bedroom are running at 20°C, Quiet fan, LED off
        lr_state = controller.states[cfg.TCL_LIVING_ROOM_ID]
        lr_state.power = True
        lr_state.target_temperature = 20.0
        lr_state.fan_speed = ACFanSpeed.QUIET
        lr_state.display_led = False
        lr_state.beep_feedback = False

        br_state = controller.states[cfg.TCL_BEDROOM_ID]
        br_state.power = True
        br_state.target_temperature = 20.0
        br_state.fan_speed = ACFanSpeed.QUIET
        br_state.display_led = False
        br_state.beep_feedback = False

        state_store = StateStore(in_memory=True)
        orchestrator = AutomationOrchestrator(
            config=cfg,
            controller=controller,
            state_store=state_store
        )

        dt = datetime(2026, 10, 20, 14, 0, tzinfo=TIMEZONE_SOFIA)
        # Run cycle while PAUSED
        result = orchestrator.run_cycle(current_dt=dt)

        assert result.system_state == SystemState.PAUSED
        assert len(result.commands_sent) == 0

        # Verify physical states remained completely untouched
        after_lr = controller.get_state(cfg.TCL_LIVING_ROOM_ID)
        assert after_lr.power is True
        assert after_lr.target_temperature == 20.0
        assert after_lr.fan_speed == ACFanSpeed.QUIET
        assert after_lr.display_led is False

        after_br = controller.get_state(cfg.TCL_BEDROOM_ID)
        assert after_br.power is True
        assert after_br.target_temperature == 20.0
        assert after_br.fan_speed == ACFanSpeed.QUIET
        assert after_br.display_led is False

        # Switch back to AUTO: resumes normal scheduling (unoccupied daytime turns off or adjusts)
        cfg.SYSTEM_MODE = "AUTO"
        result_auto = orchestrator.run_cycle(current_dt=dt)
        assert result_auto.system_state != SystemState.PAUSED

    def test_dynamodb_rate_limiter_mocked(self):
        """Verify DynamoDBRateLimiter atomic tracking, lockout at 5 attempts, and clearing."""
        import threading

        class MockDynamoTable:
            def __init__(self):
                self.items = {}
                self._lock = threading.Lock()

            def get_item(self, Key):
                with self._lock:
                    ip = Key.get("client_ip")
                    item = self.items.get(ip)
                    return {"Item": dict(item)} if item else {}

            def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
                with self._lock:
                    ip = Item["client_ip"]
                    if ConditionExpression and ip in self.items:
                        existing = self.items[ip]
                        now = ExpressionAttributeValues.get(":now", int(time.time()))
                        cutoff = ExpressionAttributeValues.get(":cutoff", now - 900)
                        first_att = existing.get("first_attempt", now)
                        lock_until = existing.get("lockout_until", 0)
                        if not (first_att < cutoff and lock_until <= now):
                            class ConditionalCheckFailedException(Exception):
                                pass
                            raise ConditionalCheckFailedException("ConditionalCheckFailedException: Condition check failed")
                    self.items[ip] = dict(Item)

            def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None, ReturnValues=None):
                with self._lock:
                    ip = Key.get("client_ip")
                    item = self.items.setdefault(ip, {"client_ip": ip, "attempt_count": 0})
                    if "ADD attempt_count" in UpdateExpression:
                        inc = ExpressionAttributeValues.get(":one", 1)
                        item["attempt_count"] = item.get("attempt_count", 0) + inc
                    if ":lock" in ExpressionAttributeValues:
                        item["lockout_until"] = ExpressionAttributeValues[":lock"]
                    if ":now" in ExpressionAttributeValues:
                        item["last_attempt"] = ExpressionAttributeValues[":now"]
                    if ":ttl" in ExpressionAttributeValues:
                        item["ttl"] = ExpressionAttributeValues[":ttl"]
                    return {"Attributes": dict(item)}

            def delete_item(self, Key):
                with self._lock:
                    self.items.pop(Key.get("client_ip"), None)

        class MockDynamoResource:
            def __init__(self, table):
                self.table = table

            def Table(self, name):
                return self.table

        mock_table = MockDynamoTable()
        mock_resource = MockDynamoResource(mock_table)
        limiter = auth.DynamoDBRateLimiter(dynamodb_resource=mock_resource)
        auth.set_rate_limiter(limiter)

        try:
            test_ip = "203.0.113.42"
            # Initially not rate limited
            is_limited, rem = auth.is_rate_limited(test_ip)
            assert is_limited is False
            assert rem == 0

            # 4 failed attempts: not locked out yet
            for _ in range(4):
                lockout = auth.record_failed_attempt(test_ip)
                assert lockout == 0
                is_limited, _ = auth.is_rate_limited(test_ip)
                assert is_limited is False

            # 5th failed attempt: triggers lockout
            lockout = auth.record_failed_attempt(test_ip)
            assert lockout == auth.LOCKOUT_DURATION_SECONDS
            is_limited, rem = auth.is_rate_limited(test_ip)
            assert is_limited is True
            assert rem > 0

            # Check table contents
            item = mock_table.items.get(test_ip)
            assert item is not None
            assert item["attempt_count"] == 5
            assert item["lockout_until"] > 0

            # Clear attempts on successful auth
            auth.clear_failed_attempts(test_ip)
            is_limited, rem = auth.is_rate_limited(test_ip)
            assert is_limited is False
            assert rem == 0
            assert test_ip not in mock_table.items
        finally:
            auth.set_rate_limiter(None)

    def test_dynamodb_rate_limiter_concurrent_attempts(self):
        """Verify simultaneous failed login attempts increment atomically and lock out without bypass."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        class ThreadSafeDynamoTable:
            def __init__(self):
                self.items = {}
                self._lock = threading.Lock()

            def get_item(self, Key):
                with self._lock:
                    ip = Key.get("client_ip")
                    item = self.items.get(ip)
                    return {"Item": dict(item)} if item else {}

            def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
                with self._lock:
                    ip = Item["client_ip"]
                    if ConditionExpression and ip in self.items:
                        class ConditionalCheckFailedException(Exception):
                            pass
                        raise ConditionalCheckFailedException("Condition check failed")
                    self.items[ip] = dict(Item)

            def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None, ReturnValues=None):
                with self._lock:
                    ip = Key.get("client_ip")
                    item = self.items.setdefault(ip, {"client_ip": ip, "attempt_count": 0})
                    if "ADD attempt_count" in UpdateExpression:
                        item["attempt_count"] = item.get("attempt_count", 0) + ExpressionAttributeValues.get(":one", 1)
                    if ":lock" in ExpressionAttributeValues:
                        item["lockout_until"] = ExpressionAttributeValues[":lock"]
                    return {"Attributes": dict(item)}

            def delete_item(self, Key):
                with self._lock:
                    self.items.pop(Key.get("client_ip"), None)

        mock_table = ThreadSafeDynamoTable()
        mock_resource = type("Res", (), {"Table": lambda s, n: mock_table})()
        limiter = auth.DynamoDBRateLimiter(dynamodb_resource=mock_resource)
        auth.set_rate_limiter(limiter)

        test_ip = "198.51.100.77"
        try:
            # 10 concurrent requests arrive simultaneously
            def record_attempt():
                return auth.record_failed_attempt(test_ip)

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(record_attempt) for _ in range(10)]
                results = [f.result() for f in futures]

            # All 10 executed atomically
            assert len(results) == 10
            # At least the later attempts returned lockout duration
            assert any(r == auth.LOCKOUT_DURATION_SECONDS for r in results)

            # In the table, attempt_count must be exactly 10 (no dropped increments due to race condition)
            stored_item = mock_table.items[test_ip]
            assert stored_item["attempt_count"] == 10
            assert stored_item["lockout_until"] > 0

            # Subsequent check must be locked out
            is_limited, rem = auth.is_rate_limited(test_ip)
            assert is_limited is True
            assert rem > 0
        finally:
            auth.set_rate_limiter(None)

    def test_dynamodb_rate_limiter_fail_secure_when_unavailable(self):
        """Verify that when DynamoDB is down, the system fails secure (raises error / returns 503) and does NOT fall back to local JSON."""
        class FailingDynamoResource:
            def Table(self, name):
                raise Exception("DynamoDB unreachable: Connection timed out")

        limiter = auth.DynamoDBRateLimiter(dynamodb_resource=FailingDynamoResource())
        auth.set_rate_limiter(limiter)

        try:
            # Direct auth call MUST raise RateLimiterUnavailableError (fails closed, not open)
            with pytest.raises(auth.RateLimiterUnavailableError):
                auth.is_rate_limited("192.0.2.1")

            with pytest.raises(auth.RateLimiterUnavailableError):
                auth.record_failed_attempt("192.0.2.1")

            # In handler, HTTP login request must return 503 Service Unavailable, NEVER 200 or 401 unthrottled
            cfg = AutomationConfig()
            cfg.DASHBOARD_PASSWORD_HASH = auth.hash_password("AnyPassword123")
            cfg.SESSION_SECRET_KEY = "synthetic-test-rate-limiter-session-key"
            event = {
                "rawPath": "/api/login",
                "requestContext": {"http": {"method": "POST", "sourceIp": "192.0.2.1"}},
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"password": "WrongPassword"})
            }
            resp = handle_dashboard_request(event, context=None, config=cfg)
            assert resp["statusCode"] == 503
            body = json.loads(resp["body"])
            assert body["status"] == "error"
            assert "unavailable" in body["error"].lower()
        finally:
            auth.set_rate_limiter(None)

    def test_paused_mode_survives_restart_and_config_reload(self, tmp_path, monkeypatch):
        """Verify PAUSED mode persists across container restarts/reloads and requires explicit action to resume AUTO."""
        monkeypatch.setattr("src.dashboard.config_store.CACHE_DIR", tmp_path)
        cfg = AutomationConfig()
        cfg.DRY_RUN = False
        cfg.S3_BUCKET = None

        store = DashboardConfigStore(config=cfg)

        # 1. Host sets system_mode to PAUSED
        saved = store.save_dashboard_config({"system_mode": "PAUSED"})
        assert saved is True

        # 2. Simulate Lambda container cold start / process restart: create fresh config instance
        fresh_cfg = AutomationConfig()
        fresh_cfg.DRY_RUN = False
        fresh_cfg.S3_BUCKET = None

        fresh_store = DashboardConfigStore(config=fresh_cfg)
        fresh_cfg = fresh_store.apply_to_config(fresh_cfg)

        # Confirm persisted mode is PAUSED
        assert fresh_cfg.SYSTEM_MODE == "PAUSED"

        # 3. Initialize controller and orchestrator with running units
        controller = MockACController(device_ids=fresh_cfg.DEVICE_IDS)
        controller.states[fresh_cfg.TCL_LIVING_ROOM_ID].power = True
        controller.states[fresh_cfg.TCL_LIVING_ROOM_ID].target_temperature = 20.0
        controller.states[fresh_cfg.TCL_LIVING_ROOM_ID].fan_speed = ACFanSpeed.QUIET
        controller.states[fresh_cfg.TCL_LIVING_ROOM_ID].display_led = False

        controller.states[fresh_cfg.TCL_BEDROOM_ID].power = True
        controller.states[fresh_cfg.TCL_BEDROOM_ID].target_temperature = 20.0
        controller.states[fresh_cfg.TCL_BEDROOM_ID].fan_speed = ACFanSpeed.QUIET
        controller.states[fresh_cfg.TCL_BEDROOM_ID].display_led = False

        state_store = StateStore(in_memory=True)
        orchestrator = AutomationOrchestrator(
            config=fresh_cfg,
            controller=controller,
            state_store=state_store
        )

        dt = datetime(2026, 10, 20, 15, 0, tzinfo=TIMEZONE_SOFIA)
        res1 = orchestrator.run_cycle(current_dt=dt)
        assert res1.system_state == SystemState.PAUSED
        assert len(res1.commands_sent) == 0

        # Physical states untouched
        assert controller.get_state(fresh_cfg.TCL_LIVING_ROOM_ID).power is True
        assert controller.get_state(fresh_cfg.TCL_LIVING_ROOM_ID).target_temperature == 20.0
        assert controller.get_state(fresh_cfg.TCL_BEDROOM_ID).power is True
        assert controller.get_state(fresh_cfg.TCL_BEDROOM_ID).target_temperature == 20.0

        # 4. Another cycle at night (23:30): STILL PAUSED, zero commands sent
        dt_night = datetime(2026, 10, 20, 23, 30, tzinfo=TIMEZONE_SOFIA)
        res2 = orchestrator.run_cycle(current_dt=dt_night)
        assert res2.system_state == SystemState.PAUSED
        assert len(res2.commands_sent) == 0

        # 5. Only explicit action returns to AUTO
        store.save_dashboard_config({"system_mode": "AUTO"})
        auto_cfg = store.apply_to_config(AutomationConfig())
        assert auto_cfg.SYSTEM_MODE == "AUTO"


    def test_config_store_rejects_unauthorized_safety_keys(self, tmp_path, monkeypatch):
        """Verify save_dashboard_config ignores dangerous safety and auth keys."""
        cfg = AutomationConfig()
        cfg.DRY_RUN = True
        cfg.TCL_READ_ONLY = True
        cfg.S3_BUCKET = None
        monkeypatch.setattr("src.dashboard.config_store.CACHE_DIR", tmp_path)

        store = DashboardConfigStore(config=cfg)

        malicious_payload = {
            "schedule_day_temp": 22.5,
            "dry_run": False,
            "DRY_RUN": False,
            "read_only": False,
            "TCL_READ_ONLY": False,
            "is_admin": True,
            "tcl_password": "HackedPassword!",
            "random_injection": "dangerous_val"
        }

        success = store.save_dashboard_config(malicious_payload)
        assert success is True

        loaded = store.load_dashboard_config()
        # Allowed key was updated
        assert loaded["schedule_day_temp"] == 22.5

        # Unauthorized keys were rejected and never persisted
        assert "dry_run" not in loaded
        assert "DRY_RUN" not in loaded
        assert "read_only" not in loaded
        assert "TCL_READ_ONLY" not in loaded
        assert "is_admin" not in loaded
        assert "tcl_password" not in loaded
        assert "random_injection" not in loaded

        # Server-side safety flags remain intact
        assert cfg.DRY_RUN is True
        assert cfg.TCL_READ_ONLY is True

    def test_all_sensitive_endpoints_require_auth_and_csrf(self):
        """Verify all /api/* endpoints reject unauthenticated access and mutating endpoints enforce CSRF."""
        cfg = AutomationConfig()
        cfg.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only-key-phase5-synthetic-session-key-for-tests-only"
        secret_key = cfg.SESSION_SECRET_KEY
        valid_session = auth.create_session_token(secret_key)
        valid_csrf = auth.generate_csrf_token(valid_session, secret_key)

        api_endpoints = [
            ("GET", "/api/status"),
            ("GET", "/api/config"),
            ("POST", "/api/config"),
            ("POST", "/api/owner-test"),
        ]

        # 1. Unauthenticated calls MUST return 401
        for method, path in api_endpoints:
            event = {
                "rawPath": path,
                "requestContext": {"http": {"method": method, "sourceIp": "192.168.1.50"}},
                "headers": {},
                "cookies": []
            }
            resp = handle_dashboard_request(event, context=None, config=cfg)
            assert resp["statusCode"] == 401, f"{method} {path} should return 401 when unauthenticated"
            body = json.loads(resp["body"])
            assert body["status"] == "error"
            assert "unauthorized" in body["error"].lower()

        # 2. Mutating endpoints without CSRF token MUST return 403
        mutating_endpoints = [
            ("/api/config", json.dumps({"schedule_day_temp": 21.0})),
            ("/api/owner-test", json.dumps({"action": "STOP"}))
        ]

        for path, body_data in mutating_endpoints:
            # Authenticated but missing CSRF
            event_no_csrf = {
                "rawPath": path,
                "requestContext": {"http": {"method": "POST", "sourceIp": "192.168.1.50"}},
                "headers": {"content-type": "application/json"},
                "cookies": [f"session_token={valid_session}"],
                "body": body_data
            }
            resp_no_csrf = handle_dashboard_request(event_no_csrf, context=None, config=cfg)
            assert resp_no_csrf["statusCode"] == 403, f"POST {path} without CSRF should return 403"
            res_body = json.loads(resp_no_csrf["body"])
            assert "csrf" in res_body["error"].lower()

            # Authenticated with INVALID CSRF
            event_bad_csrf = {
                "rawPath": path,
                "requestContext": {"http": {"method": "POST", "sourceIp": "192.168.1.50"}},
                "headers": {
                    "content-type": "application/json",
                    "x-csrf-token": "invalid_forged_csrf_token"
                },
                "cookies": [f"session_token={valid_session}"],
                "body": body_data
            }
            resp_bad_csrf = handle_dashboard_request(event_bad_csrf, context=None, config=cfg)
            assert resp_bad_csrf["statusCode"] == 403, f"POST {path} with bad CSRF should return 403"

            # Authenticated with VALID CSRF succeeds (returns 200)
            event_valid = {
                "rawPath": path,
                "requestContext": {"http": {"method": "POST", "sourceIp": "192.168.1.50"}},
                "headers": {
                    "content-type": "application/json",
                    "x-csrf-token": valid_csrf
                },
                "cookies": [f"session_token={valid_session}"],
                "body": body_data
            }
            resp_valid = handle_dashboard_request(event_valid, context=None, config=cfg)
            assert resp_valid["statusCode"] == 200, f"POST {path} with valid CSRF should succeed (200)"

    def test_dedicated_dashboard_handler_entrypoint(self, monkeypatch):
        """Verify dedicated dashboard_handler router processes requests independently of scheduler."""
        cfg = AutomationConfig()
        cfg.DASHBOARD_PASSWORD_HASH = __import__("src.dashboard.auth", fromlist=["hash_password"]).hash_password('TestPass123')
        cfg.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only-key-123-synthetic-session-key-for-tests-only"

        event = {
            "rawPath": "/login",
            "requestContext": {"http": {"method": "GET", "sourceIp": "192.168.1.99"}},
            "headers": {}
        }
        monkeypatch.setattr(AutomationConfig, "SESSION_SECRET_KEY", cfg.SESSION_SECRET_KEY)
        monkeypatch.setattr(AutomationConfig, "DASHBOARD_PASSWORD_HASH", cfg.DASHBOARD_PASSWORD_HASH)
        resp = dashboard_handler(event, context=None)
        assert resp["statusCode"] == 200
        assert "text/html" in resp["headers"]["Content-Type"]
        assert "Owner Access Password" in resp["body"]
