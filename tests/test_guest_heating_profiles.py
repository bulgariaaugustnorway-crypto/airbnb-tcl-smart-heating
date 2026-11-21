"""Comprehensive tests for Guest Comfort Heating Profiles (AT_HOME, SKI_DAY, CUSTOM),
room-specific temperature offsets, reservation profile isolation, automatic expiration,
and guest remote adjustment safeguards.
"""

import json
from datetime import datetime, date, time, timedelta
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
    HeatingProfileType,
    HEATING_PROFILE_PRESETS,
    Reservation,
    ReservationType,
    ScheduleDecision,
    SystemState,
)
from src.orchestrator import AutomationOrchestrator
from src.safeguards import SafeguardEngine
from src.scheduler import HVACScheduleEngine
from src.state_store import StateStore


@pytest.fixture(autouse=True)
def clean_config_and_cache(tmp_path, monkeypatch):
    """Ensure tests run with clean config and local cache directory."""
    monkeypatch.setattr("src.dashboard.config_store.CACHE_DIR", tmp_path)
    monkeypatch.setattr("src.config.AutomationConfig.STATE_CACHE_FILE", tmp_path / "automation_state.json")


class TestHeatingProfiles:
    """Verifies diurnal setpoints for AT_HOME, SKI_DAY, and CUSTOM heating profiles."""

    def test_at_home_profile_schedule_evaluation(self):
        """AT_HOME profile keeps warm 23°C throughout morning, day, and evening, and 20°C at night."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="stay-at-home-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Family)",
            reservation_type=ReservationType.CONFIRMED
        )

        # 1. Morning: 08:30 Sofia -> 23°C, Mid fan
        dt_morning = datetime(2026, 10, 22, 8, 30, tzinfo=TIMEZONE_SOFIA)
        dec_m = engine.evaluate([res], current_dt=dt_morning)
        assert dec_m.target_power is True
        assert dec_m.target_temperature == 23.0
        assert dec_m.target_fan_speed == ACFanSpeed.MID
        assert dec_m.active_profile == "AT_HOME"

        # 2. Daytime: 14:00 Sofia -> 23°C, Mid fan (Unlike SKI_DAY which is 21°C)
        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_d = engine.evaluate([res], current_dt=dt_day)
        assert dec_d.target_power is True
        assert dec_d.target_temperature == 23.0
        assert dec_d.target_fan_speed == ACFanSpeed.MID
        assert dec_d.active_profile == "AT_HOME"

        # 3. Evening: 20:00 Sofia -> 23°C, Mid fan
        dt_evening = datetime(2026, 10, 22, 20, 0, tzinfo=TIMEZONE_SOFIA)
        dec_e = engine.evaluate([res], current_dt=dt_evening)
        assert dec_e.target_power is True
        assert dec_e.target_temperature == 23.0
        assert dec_e.target_fan_speed == ACFanSpeed.MID
        assert dec_e.active_profile == "AT_HOME"

        # 4. Night: 23:30 Sofia -> 20°C, Quiet fan
        dt_night = datetime(2026, 10, 22, 23, 30, tzinfo=TIMEZONE_SOFIA)
        dec_n = engine.evaluate([res], current_dt=dt_night)
        assert dec_n.target_power is True
        assert dec_n.target_temperature == 20.0
        assert dec_n.target_fan_speed == ACFanSpeed.QUIET
        assert dec_n.target_display_led is False
        assert dec_n.active_profile == "AT_HOME"

    def test_ski_day_profile_schedule_evaluation(self):
        """SKI_DAY profile provides eco daytime setback to 21°C between 10:00 and 17:00."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "SKI_DAY"
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="stay-skiday-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Skiers)",
            reservation_type=ReservationType.CONFIRMED
        )

        # 1. Morning: 08:30 Sofia -> 23°C, Mid fan
        dt_morning = datetime(2026, 10, 22, 8, 30, tzinfo=TIMEZONE_SOFIA)
        dec_m = engine.evaluate([res], current_dt=dt_morning)
        assert dec_m.target_temperature == 23.0
        assert dec_m.target_fan_speed == ACFanSpeed.MID
        assert dec_m.active_profile == "SKI_DAY"

        # 2. Daytime: 14:00 Sofia -> Eco setback: 21°C, Auto fan
        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_d = engine.evaluate([res], current_dt=dt_day)
        assert dec_d.target_temperature == 21.0
        assert dec_d.target_fan_speed == ACFanSpeed.AUTO
        assert dec_d.active_profile == "SKI_DAY"

        # 3. Evening: 19:30 Sofia -> Warm return: 23°C, Mid fan
        dt_evening = datetime(2026, 10, 22, 19, 30, tzinfo=TIMEZONE_SOFIA)
        dec_e = engine.evaluate([res], current_dt=dt_evening)
        assert dec_e.target_temperature == 23.0
        assert dec_e.target_fan_speed == ACFanSpeed.MID
        assert dec_e.active_profile == "SKI_DAY"

        # 4. Night: 02:00 Sofia -> Silent night: 20°C, Quiet fan
        dt_night = datetime(2026, 10, 23, 2, 0, tzinfo=TIMEZONE_SOFIA)
        dec_n = engine.evaluate([res], current_dt=dt_night)
        assert dec_n.target_temperature == 20.0
        assert dec_n.target_fan_speed == ACFanSpeed.QUIET
        assert dec_n.target_display_led is False
        assert dec_n.active_profile == "SKI_DAY"

    def test_custom_profile_schedule_evaluation(self):
        """CUSTOM profile evaluates to owner-specified setpoints without code changes."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "CUSTOM"
        cfg.SCHEDULE_MORNING_TEMP = 22.5
        cfg.SCHEDULE_DAY_TEMP = 20.5
        cfg.SCHEDULE_EVENING_TEMP = 22.0
        cfg.SCHEDULE_NIGHT_TEMP = 19.5
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="stay-custom-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Custom)",
            reservation_type=ReservationType.CONFIRMED
        )

        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_d = engine.evaluate([res], current_dt=dt_day)
        assert dec_d.target_temperature == 20.5
        assert dec_d.active_profile == "CUSTOM"

        dt_night = datetime(2026, 10, 22, 23, 30, tzinfo=TIMEZONE_SOFIA)
        dec_n = engine.evaluate([res], current_dt=dt_night)
        assert dec_n.target_temperature == 19.5
        assert dec_n.active_profile == "CUSTOM"


class TestRoomTemperatureOffsets:
    """Verifies independent offsets for Living Room and Bedroom."""

    def test_independent_living_room_and_bedroom_offsets(self):
        """Living room and bedroom commands apply distinct room temperature offsets."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "SKI_DAY"
        cfg.LIVING_ROOM_TEMP_OFFSET = 1.0     # Living Room +1.0°C warmer
        cfg.BEDROOM_TEMP_OFFSET = -1.5        # Bedroom -1.5°C cooler
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="stay-offsets-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Offsets Test)",
            reservation_type=ReservationType.CONFIRMED
        )

        # Daytime: base temp is 21.0°C
        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec = engine.evaluate([res], current_dt=dt_day)

        assert dec.target_temperature == 21.0

        lr_cmd = dec.commands_to_send[cfg.TCL_LIVING_ROOM_ID]
        br_cmd = dec.commands_to_send[cfg.TCL_BEDROOM_ID]

        # Living Room: 21.0 + 1.0 = 22.0°C
        assert lr_cmd.target_temperature == 22.0
        # Bedroom: 21.0 - 1.5 = 19.5°C
        assert br_cmd.target_temperature == 19.5

    def test_per_reservation_offset_overrides(self):
        """Offsets assigned specifically to a reservation override property defaults."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "SKI_DAY"
        cfg.LIVING_ROOM_TEMP_OFFSET = 0.0
        cfg.BEDROOM_TEMP_OFFSET = 0.0
        cfg.RESERVATION_PROFILES = {
            "stay-override-1": {
                "profile": "AT_HOME",
                "living_room_offset": -1.0,
                "bedroom_offset": 2.0
            }
        }
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="stay-override-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        # AT_HOME Daytime: base temp is 23.0°C
        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec = engine.evaluate([res], current_dt=dt_day)

        assert dec.active_profile == "AT_HOME"
        assert dec.target_temperature == 23.0

        lr_cmd = dec.commands_to_send[cfg.TCL_LIVING_ROOM_ID]
        br_cmd = dec.commands_to_send[cfg.TCL_BEDROOM_ID]

        # Living Room: 23.0 - 1.0 = 22.0°C
        assert lr_cmd.target_temperature == 22.0
        # Bedroom: 23.0 + 2.0 = 25.0°C
        assert br_cmd.target_temperature == 25.0


class TestReservationProfileIsolationAndDecoupling:
    """Verifies that reservation profile assignment affects only that stay and preserves calendar decoupling."""

    def test_reservation_specific_profile_does_not_affect_future_bookings(self):
        """Assigning AT_HOME to Stay 1 leaves Stay 2 on property default SKI_DAY."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "SKI_DAY"
        cfg.RESERVATION_PROFILES = {
            "stay-1-oct": {
                "profile": "AT_HOME"
            }
        }
        engine = HVACScheduleEngine(config=cfg)

        res1 = Reservation(
            uid="stay-1-oct",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Stay 1 (Assigned AT_HOME)",
            reservation_type=ReservationType.CONFIRMED
        )
        res2 = Reservation(
            uid="stay-2-nov",
            start_date=date(2026, 11, 5),
            end_date=date(2026, 11, 10),
            summary="Stay 2 (Unassigned -> Default SKI_DAY)",
            reservation_type=ReservationType.CONFIRMED
        )

        # Oct 21 at 14:00 (Stay 1 active) -> Evaluates to AT_HOME (23°C)
        dt_stay1 = datetime(2026, 10, 21, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec1 = engine.evaluate([res1, res2], current_dt=dt_stay1)
        assert dec1.active_profile == "AT_HOME"
        assert dec1.target_temperature == 23.0

        # Nov 6 at 14:00 (Stay 2 active) -> Evaluates to SKI_DAY (21°C)
        dt_stay2 = datetime(2026, 11, 6, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec2 = engine.evaluate([res1, res2], current_dt=dt_stay2)
        assert dec2.active_profile == "SKI_DAY"
        assert dec2.target_temperature == 21.0

    def test_reservation_profiles_support_both_confirmed_and_blocked(self):
        """Host blocked stays and confirmed guest stays both support profile customization."""
        cfg = AutomationConfig()
        cfg.HEAT_DURING_BLOCKED_DATES = True
        cfg.RESERVATION_PROFILES = {
            "block-host-oct": {
                "profile": "AT_HOME"
            }
        }
        engine = HVACScheduleEngine(config=cfg)

        block_res = Reservation(
            uid="block-host-oct",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Airbnb (Not available)",
            reservation_type=ReservationType.BLOCKED
        )

        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec = engine.evaluate([block_res], current_dt=dt_day)
        assert dec.active_profile == "AT_HOME"
        assert dec.target_temperature == 23.0
        assert dec.active_reservation.is_blocked is True


class TestAutomaticProfileExpiration:
    """Verifies that reservation profiles automatically expire after checkout time."""

    def test_cleanup_expired_profiles_purges_after_checkout(self):
        """Profile remains active before 11:00 on checkout date and is purged after 11:00."""
        store = DashboardConfigStore()
        uid = "stay-checkout-exp-1"
        end_date = date(2026, 10, 23)

        store.set_reservation_profile(
            uid=uid,
            profile_type="AT_HOME",
            end_date=end_date.isoformat()
        )

        res = Reservation(
            uid=uid,
            start_date=date(2026, 10, 20),
            end_date=end_date,
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        # 1. Oct 23 at 10:30 (Before checkout 11:00) -> Still active, 0 purged
        dt_before = datetime(2026, 10, 23, 10, 30, tzinfo=TIMEZONE_SOFIA)
        purged = store.cleanup_expired_profiles(dt_before, [res])
        assert purged == 0
        assert store.get_reservation_profile(uid) is not None

        # 2. Oct 23 at 11:15 (After checkout 11:00) -> Expired and purged!
        dt_after = datetime(2026, 10, 23, 11, 15, tzinfo=TIMEZONE_SOFIA)
        purged = store.cleanup_expired_profiles(dt_after, [res])
        assert purged == 1
        assert store.get_reservation_profile(uid) is None

    def test_orchestrator_run_cycle_cleans_up_expired_profiles(self):
        """AutomationOrchestrator.run_cycle triggers profile cleanup on tick."""
        cfg = AutomationConfig()
        cfg.DRY_RUN = True
        store = DashboardConfigStore(config=cfg)
        uid = "stay-orch-exp-1"
        end_date = date(2026, 10, 23)

        store.set_reservation_profile(uid=uid, profile_type="AT_HOME", end_date=end_date.isoformat())
        assert store.get_reservation_profile(uid) is not None

        res = Reservation(
            uid=uid,
            start_date=date(2026, 10, 20),
            end_date=end_date,
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        orch = AutomationOrchestrator(config=cfg)

        # Tick at 12:00 on checkout date
        dt_after = datetime(2026, 10, 23, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt_after, override_reservations=[res])

        # Verify profile was purged from store
        assert store.get_reservation_profile(uid) is None


class TestGuestRemoteAdjustmentSafeguards:
    """Verifies that physical remote control adjustments are respected and reported."""

    def test_guest_manual_temp_override_preserved_within_daypart(self):
        """Guest sets remote to 24°C; safeguard preserves preference without overriding every 15 min."""
        cfg = AutomationConfig()
        cfg.MANUAL_OVERRIDE_PRESERVE = True
        safeguards = SafeguardEngine(config=cfg)

        dev_id = cfg.TCL_LIVING_ROOM_ID

        # Step 1: Normal automated target is 21°C
        cmd = ACCommand(
            device_id=dev_id,
            power=True,
            target_temperature=21.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            reason="Daytime schedule"
        )
        res = Reservation(
            uid="guest-stay-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        decision = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Daytime",
            commands_to_send={dev_id: cmd}
        )

        # Initial command applied
        safeguards.record_applied_command(cmd)

        # Step 2: Guest physically changed temperature to 24°C on remote
        reported_state = ACState(
            device_id=dev_id,
            power=True,
            target_temperature=24.0,  # +3°C drift from 21.0°C
            current_temperature=21.5,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            is_online=True
        )

        # Next 15-minute cycle: automation still requests 21°C
        eval_dt = datetime(2026, 10, 22, 14, 15, tzinfo=TIMEZONE_SOFIA)
        results = safeguards.filter_and_validate(
            decision,
            {dev_id: reported_state},
            eval_dt
        )
        passed, out_cmd, reason = results[dev_id]

        # Safeguard blocks sending command to respect guest preference!
        assert passed is False
        assert "Guest manual override preserved" in reason
        assert "24.0°C" in reason

    def test_dashboard_status_exposes_guest_override_active(self):
        """Dashboard status API flags guest_override_active when physical state drifts from target."""
        cfg = AutomationConfig()
        cfg.TCL_READ_ONLY = True
        store = DashboardConfigStore(config=cfg)

        state_store = StateStore(s3_bucket=cfg.S3_BUCKET, s3_key=cfg.S3_STATE_KEY)
        lr_id = cfg.TCL_LIVING_ROOM_ID

        state_data = {
            "last_system_state": "OCCUPIED_DAY",
            "device_states": {
                lr_id: {
                    "power": True,
                    "target_temperature": 24.0,  # Physical unit reports 24°C
                    "current_temperature": 21.0,
                    "mode": "Heat",
                    "fan_speed": "Auto",
                    "display_led": True,
                    "beep_feedback": False,
                    "online": True
                }
            },
            "last_applied_commands": {
                lr_id: {
                    "device_id": lr_id,
                    "power": True,
                    "target_temperature": 21.0,  # Automation commanded 21°C
                    "mode": "Heat",
                    "fan_speed": "Auto",
                    "eco_mode": False,
                    "display_led": True,
                    "beep_feedback": False,
                    "reason": "Scheduled Daytime"
                }
            }
        }
        state_store.save_state(state_data)

        secret_key = "test-secret-synthetic-session-key-for-tests-only-salt-2026-synthetic-session-key-for-tests-only"
        cfg.SESSION_SECRET_KEY = secret_key
        token = auth.create_session_token(secret_key)

        event = {
            "httpMethod": "GET",
            "path": "/api/status",
            "headers": {"cookie": f"session_token={token}"}
        }

        resp = handle_dashboard_request(event, None, config=cfg)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])

        lr_dev = body["devices"]["living_room"]
        assert lr_dev["guest_override_active"] is True
        assert "Manual setpoint: 24.0°C" in lr_dev["override_details"]


class TestDashboardProfileAPI:
    """Verifies REST API endpoints for heating profile configuration and reservation assignment."""

    def test_api_save_and_clear_reservation_profile(self):
        """POST /api/reservation-profile assigns and clears profiles with CSRF validation."""
        cfg = AutomationConfig()
        secret_key = "test-secret-synthetic-session-key-for-tests-only-salt-2026-synthetic-session-key-for-tests-only"
        cfg.SESSION_SECRET_KEY = secret_key
        cfg.DASHBOARD_PASSWORD_HASH = auth.hash_password("OwnerPassword123")
        store = DashboardConfigStore(config=cfg)

        token = auth.create_session_token(secret_key)
        csrf = auth.generate_csrf_token(token, secret_key)

        uid = "api-stay-oct-20"

        # 1. Assign AT_HOME profile
        event_save = {
            "httpMethod": "POST",
            "path": "/api/reservation-profile",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": csrf
            },
            "body": json.dumps({
                "csrf_token": csrf,
                "uid": uid,
                "profile": "AT_HOME",
                "living_room_offset": 1.0,
                "bedroom_offset": -0.5
            })
        }

        resp = handle_dashboard_request(event_save, None, config=cfg)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "success"
        assert body["profile"] == "AT_HOME"

        # Verify persisted in store
        prof = store.get_reservation_profile(uid)
        assert prof is not None
        assert prof["profile"] == "AT_HOME"
        assert prof["living_room_offset"] == 1.0
        assert prof["bedroom_offset"] == -0.5

        # 2. Clear profile
        event_clear = {
            "httpMethod": "POST",
            "path": "/api/reservation-profile",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": csrf
            },
            "body": json.dumps({
                "csrf_token": csrf,
                "uid": uid,
                "action": "CLEAR"
            })
        }
        resp_clear = handle_dashboard_request(event_clear, None, config=cfg)
        assert resp_clear["statusCode"] == 200
        assert store.get_reservation_profile(uid) is None

    def test_api_reservation_profile_csrf_protection(self):
        """POST /api/reservation-profile rejects requests with invalid CSRF token."""
        cfg = AutomationConfig()
        secret_key = "test-secret-synthetic-session-key-for-tests-only-salt-2026-synthetic-session-key-for-tests-only"
        cfg.SESSION_SECRET_KEY = secret_key
        token = auth.create_session_token(secret_key)

        event = {
            "httpMethod": "POST",
            "path": "/api/reservation-profile",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": "invalid-csrf-token-attacker"
            },
            "body": json.dumps({
                "uid": "stay-123",
                "profile": "AT_HOME"
            })
        }

        resp = handle_dashboard_request(event, None, config=cfg)
        assert resp["statusCode"] == 403
        body = json.loads(resp["body"])
        assert "CSRF" in body["error"]

    def test_api_config_saves_default_profile_and_offsets(self):
        """POST /api/config allows updating default heating profile and room offsets."""
        cfg = AutomationConfig()
        secret_key = "test-secret-synthetic-session-key-for-tests-only-salt-2026-synthetic-session-key-for-tests-only"
        cfg.SESSION_SECRET_KEY = secret_key
        token = auth.create_session_token(secret_key)
        csrf = auth.generate_csrf_token(token, secret_key)
        store = DashboardConfigStore(config=cfg)

        event = {
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": csrf
            },
            "body": json.dumps({
                "csrf_token": csrf,
                "default_heating_profile": "AT_HOME",
                "living_room_temp_offset": 0.5,
                "bedroom_temp_offset": -1.0
            })
        }

        resp = handle_dashboard_request(event, None, config=cfg)
        assert resp["statusCode"] == 200

        loaded = store.load_dashboard_config()
        assert loaded["default_heating_profile"] == "AT_HOME"
        assert loaded["living_room_temp_offset"] == 0.5
        assert loaded["bedroom_temp_offset"] == -1.0


class TestConfigurationConsistencyAndFallbacks:
    """Verifies single source of truth, persistent config retention, and file naming compatibility."""

    def test_config_store_loads_and_saves_consistently_without_dropping_keys(self, tmp_path):
        """Verifies partial updates to dashboard_config.json preserve all other existing keys."""
        cfg = AutomationConfig()
        store = DashboardConfigStore(config=cfg)

        # Baseline config with custom temperatures
        initial = store.load_dashboard_config()
        assert "schedule_day_temp" in initial
        assert "default_heating_profile" in initial

        # Save partial update (e.g. only living_room_temp_offset)
        store.save_dashboard_config({"living_room_temp_offset": 1.5})

        loaded_1 = store.load_dashboard_config()
        assert loaded_1["living_room_temp_offset"] == 1.5
        assert loaded_1["schedule_day_temp"] == initial["schedule_day_temp"]
        assert loaded_1["default_heating_profile"] == initial["default_heating_profile"]

        # Save another partial update (e.g. default_heating_profile)
        store.save_dashboard_config({"default_heating_profile": "AT_HOME"})

        loaded_2 = store.load_dashboard_config()
        assert loaded_2["living_room_temp_offset"] == 1.5
        assert loaded_2["default_heating_profile"] == "AT_HOME"
        assert loaded_2["schedule_day_temp"] == initial["schedule_day_temp"]

    def test_dashboard_settings_json_backward_compatibility_fallback(self, tmp_path):
        """If a dashboard_settings.json file exists in cache, config_store reads it seamlessly as fallback."""
        cfg = AutomationConfig()
        store = DashboardConfigStore(config=cfg)

        # Write legacy settings file
        settings_file = tmp_path / "dashboard_settings.json"
        settings_file.write_text(json.dumps({
            "default_heating_profile": "AT_HOME",
            "living_room_temp_offset": 2.0,
            "bedroom_temp_offset": -1.0,
            "schedule_day_temp": 22.5
        }), encoding="utf-8")

        # When dashboard_config.json does not exist, store falls back to dashboard_settings.json
        loaded = store.load_dashboard_config()
        assert loaded["default_heating_profile"] == "AT_HOME"
        assert loaded["living_room_temp_offset"] == 2.0
        assert loaded["bedroom_temp_offset"] == -1.0
        assert loaded["schedule_day_temp"] == 22.5

    def test_orchestrator_run_cycle_picks_up_saved_dashboard_settings_dynamically(self, tmp_path):
        """Orchestrator run_cycle automatically picks up updated dashboard settings without restart."""
        cfg = AutomationConfig()
        cfg.DRY_RUN = True
        store = DashboardConfigStore(config=cfg)
        orch = AutomationOrchestrator(config=cfg)

        # 1. Default: system_mode is AUTO
        assert orch.config.SYSTEM_MODE == "AUTO"

        # 2. Host changes system mode to PAUSED via dashboard
        store.save_dashboard_config({"system_mode": "PAUSED"})

        # Run cycle -> Orchestrator automatically re-hydrates config
        res = Reservation(
            uid="stay-pause-dyn-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        dt = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        result = orch.run_cycle(current_dt=dt, override_reservations=[res], reload_config=True)

        assert orch.config.SYSTEM_MODE == "PAUSED"
        assert result.system_state == SystemState.PAUSED
        assert len(result.commands_sent) == 0


class TestUpcomingGuestComfortAndOffsets:
    """Verifies that upcoming stay can be assigned AT_HOME while default SKI_DAY is retained for others,
    with diurnal setpoints and physical AC offset application.
    """

    def test_upcoming_confirmed_guest_at_home_assignment_with_ski_day_default_for_others(self):
        """Assign AT_HOME to upcoming stay; other stays stay on SKI_DAY; verify all 4 diurnal slots on both physical units."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "SKI_DAY"
        cfg.LIVING_ROOM_TEMP_OFFSET = 1.0   # LR +1.0°C warmer
        cfg.BEDROOM_TEMP_OFFSET = -1.5      # BR -1.5°C cooler
        store = DashboardConfigStore(config=cfg)

        upcoming_stay = Reservation(
            uid="res-upcoming-comfort-1",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 10, 26),
            summary="Upcoming VIP Guest",
            reservation_type=ReservationType.CONFIRMED
        )
        future_stay = Reservation(
            uid="res-future-default-2",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 5),
            summary="Future Standard Guest",
            reservation_type=ReservationType.CONFIRMED
        )

        # Host assigns AT_HOME specifically to upcoming_stay
        store.set_reservation_profile(
            uid=upcoming_stay.uid,
            profile_type="AT_HOME",
            end_date=upcoming_stay.end_date.isoformat()
        )
        cfg = store.apply_to_config(cfg)
        engine = HVACScheduleEngine(config=cfg)

        lr_id = cfg.TCL_LIVING_ROOM_ID  # demo_living_room
        br_id = cfg.TCL_BEDROOM_ID      # demo_bedroom
        assert lr_id == "demo_living_room"
        assert br_id == "demo_bedroom"

        # --- Test 1: Morning (07:00–10:00) at 08:30 ---
        # Base: 23°C. LR (+1.0) = 24.0°C, BR (-1.5) = 21.5°C, Fan: Mid
        dt_morning = datetime(2026, 10, 23, 8, 30, tzinfo=TIMEZONE_SOFIA)
        dec_m = engine.evaluate([upcoming_stay, future_stay], current_dt=dt_morning)
        assert dec_m.active_profile == "AT_HOME"
        assert dec_m.target_temperature == 23.0
        assert dec_m.commands_to_send[lr_id].target_temperature == 24.0
        assert dec_m.commands_to_send[br_id].target_temperature == 21.5
        assert dec_m.commands_to_send[lr_id].fan_speed == ACFanSpeed.MID
        assert dec_m.commands_to_send[br_id].fan_speed == ACFanSpeed.MID

        # --- Test 2: Daytime (10:00–17:00) at 14:00 ---
        # AT_HOME stays at 23°C (no setback!). LR (+1.0) = 24.0°C, BR (-1.5) = 21.5°C, Fan: Mid
        dt_day = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_d = engine.evaluate([upcoming_stay, future_stay], current_dt=dt_day)
        assert dec_d.active_profile == "AT_HOME"
        assert dec_d.target_temperature == 23.0
        assert dec_d.commands_to_send[lr_id].target_temperature == 24.0
        assert dec_d.commands_to_send[br_id].target_temperature == 21.5
        assert dec_d.commands_to_send[lr_id].fan_speed == ACFanSpeed.MID

        # --- Test 3: Evening (17:00–23:00) at 20:00 ---
        # Base: 23°C. LR (+1.0) = 24.0°C, BR (-1.5) = 21.5°C, Fan: Mid
        dt_evening = datetime(2026, 10, 23, 20, 0, tzinfo=TIMEZONE_SOFIA)
        dec_e = engine.evaluate([upcoming_stay, future_stay], current_dt=dt_evening)
        assert dec_e.active_profile == "AT_HOME"
        assert dec_e.target_temperature == 23.0
        assert dec_e.commands_to_send[lr_id].target_temperature == 24.0
        assert dec_e.commands_to_send[br_id].target_temperature == 21.5

        # --- Test 4: Night (23:00–07:00) at 23:30 ---
        # Base: 20°C. LR (+1.0) = 21.0°C, BR (-1.5) = 18.5°C, Fan: Quiet, LED: False
        dt_night = datetime(2026, 10, 23, 23, 30, tzinfo=TIMEZONE_SOFIA)
        dec_n = engine.evaluate([upcoming_stay, future_stay], current_dt=dt_night)
        assert dec_n.active_profile == "AT_HOME"
        assert dec_n.target_temperature == 20.0
        assert dec_n.target_display_led is False
        assert dec_n.target_fan_speed == ACFanSpeed.QUIET
        assert dec_n.commands_to_send[lr_id].target_temperature == 21.0
        assert dec_n.commands_to_send[br_id].target_temperature == 18.5
        assert dec_n.commands_to_send[lr_id].display_led is False
        assert dec_n.commands_to_send[br_id].display_led is False
        assert dec_n.commands_to_send[lr_id].fan_speed == ACFanSpeed.QUIET
        assert dec_n.commands_to_send[br_id].fan_speed == ACFanSpeed.QUIET

        # --- Test 5: Verify Future Stay evaluates to default SKI_DAY with daytime setback ---
        dt_future_day = datetime(2026, 11, 2, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_future = engine.evaluate([upcoming_stay, future_stay], current_dt=dt_future_day)
        assert dec_future.active_profile == "SKI_DAY"
        assert dec_future.target_temperature == 21.0  # SKI_DAY daytime setback
        assert dec_future.commands_to_send[lr_id].target_temperature == 22.0  # 21.0 + 1.0
        assert dec_future.commands_to_send[br_id].target_temperature == 19.5  # 21.0 - 1.5


class TestMultiScenarioProfileExpiration:
    """Verifies profile expiration and isolation across consecutive stays, date modifications,
    cancellations, and blocked stays.
    """

    def test_consecutive_reservations_turnover_expiration(self):
        """Stay A checks out at 11:00 on Oct 23 (assigned AT_HOME).
        Stay B checks in at 15:00 on Oct 23 (unassigned -> default SKI_DAY).
        Verifies AT_HOME expires at 11:00 and zero leakage to Stay B pre-arrival or stay.
        """
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "SKI_DAY"
        cfg.DRY_RUN = True
        store = DashboardConfigStore(config=cfg)

        stay_a = Reservation(
            uid="consec-stay-A",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Stay A (Departing)",
            reservation_type=ReservationType.CONFIRMED
        )
        stay_b = Reservation(
            uid="consec-stay-B",
            start_date=date(2026, 10, 23),
            end_date=date(2026, 10, 26),
            summary="Stay B (Arriving)",
            reservation_type=ReservationType.CONFIRMED
        )

        # Assign AT_HOME to Stay A only
        store.set_reservation_profile(
            uid=stay_a.uid,
            profile_type="AT_HOME",
            end_date=stay_a.end_date.isoformat()
        )
        assert store.get_reservation_profile(stay_a.uid) is not None
        assert store.get_reservation_profile(stay_b.uid) is None

        orch = AutomationOrchestrator(config=cfg)

        # 1. Oct 23 at 10:15 (Stay A still in room before 11:00 checkout)
        # Evaluates Stay A with AT_HOME (23°C daytime)
        dt_morning = datetime(2026, 10, 23, 10, 15, tzinfo=TIMEZONE_SOFIA)
        res_m = orch.run_cycle(current_dt=dt_morning, override_reservations=[stay_a, stay_b])
        assert res_m.system_state == SystemState.OCCUPIED_DAY
        assert res_m.active_reservation.uid == stay_a.uid
        # Profile still exists
        assert store.get_reservation_profile(stay_a.uid) is not None

        # 2. Oct 23 at 11:05 (Checkout has elapsed; Stay A departs)
        dt_checkout = datetime(2026, 10, 23, 11, 5, tzinfo=TIMEZONE_SOFIA)
        res_c = orch.run_cycle(current_dt=dt_checkout, override_reservations=[stay_a, stay_b])
        # Profile for Stay A must be automatically purged!
        assert store.get_reservation_profile(stay_a.uid) is None

        # 3. Oct 23 at 13:30 (Pre-arrival window for Stay B, 13:00-15:00)
        dt_pre = datetime(2026, 10, 23, 13, 30, tzinfo=TIMEZONE_SOFIA)
        res_p = orch.run_cycle(current_dt=dt_pre, override_reservations=[stay_b])
        assert res_p.system_state == SystemState.PRE_ARRIVAL_HEATING
        assert res_p.active_reservation.uid == stay_b.uid

        # 4. Oct 23 at 16:00 (Stay B in-stay after 15:00 checkin)
        # Stay B must evaluate to property default SKI_DAY (21°C daytime setback)!
        dt_in_stay = datetime(2026, 10, 23, 16, 0, tzinfo=TIMEZONE_SOFIA)
        dec_b = orch.schedule_engine.evaluate([stay_b], current_dt=dt_in_stay)
        assert dec_b.active_profile == "SKI_DAY"
        assert dec_b.target_temperature == 21.0
        assert store.get_reservation_profile(stay_a.uid) is None

    def test_modified_airbnb_dates_extension_and_shortening(self):
        """Calendar extension delays profile expiration; shortening accelerates expiration."""
        store = DashboardConfigStore()

        # Case 1: Extension (Originally Oct 20-24, extended to Oct 26)
        uid_ext = "stay-ext-1"
        store.set_reservation_profile(uid=uid_ext, profile_type="AT_HOME", end_date="2026-10-24")

        # Calendar feed returns updated end_date Oct 26
        extended_res = Reservation(
            uid=uid_ext,
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 26),
            summary="Extended Stay",
            reservation_type=ReservationType.CONFIRMED
        )

        # On Oct 24 at 11:30 (original checkout date), calendar shows stay is extended to Oct 26
        dt_orig_checkout = datetime(2026, 10, 24, 11, 30, tzinfo=TIMEZONE_SOFIA)
        purged = store.cleanup_expired_profiles(dt_orig_checkout, [extended_res])
        assert purged == 0
        assert store.get_reservation_profile(uid_ext) is not None

        # On Oct 26 at 11:30 (actual extended checkout date) -> Purged!
        dt_new_checkout = datetime(2026, 10, 26, 11, 30, tzinfo=TIMEZONE_SOFIA)
        purged = store.cleanup_expired_profiles(dt_new_checkout, [extended_res])
        assert purged == 1
        assert store.get_reservation_profile(uid_ext) is None

        # Case 2: Shortening (Originally Oct 20-28, shortened to Oct 25)
        uid_short = "stay-short-2"
        store.set_reservation_profile(uid=uid_short, profile_type="AT_HOME", end_date="2026-10-28")

        shortened_res = Reservation(
            uid=uid_short,
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Shortened Stay",
            reservation_type=ReservationType.CONFIRMED
        )

        # On Oct 25 at 11:30 -> Purged because calendar reports end_date Oct 25
        dt_short_checkout = datetime(2026, 10, 25, 11, 30, tzinfo=TIMEZONE_SOFIA)
        purged = store.cleanup_expired_profiles(dt_short_checkout, [shortened_res])
        assert purged == 1
        assert store.get_reservation_profile(uid_short) is None

    def test_deleted_or_cancelled_reservation_profile_cleanup(self):
        """If a reservation is deleted from calendar feed, profile expires at stored end_date or can be cleared."""
        store = DashboardConfigStore()
        uid = "stay-deleted-1"
        store.set_reservation_profile(uid=uid, profile_type="AT_HOME", end_date="2026-10-23")

        # Calendar returns empty (reservation cancelled/deleted)
        empty_calendar = []

        # Before stored checkout time: retains profile for safety
        dt_before = datetime(2026, 10, 23, 10, 0, tzinfo=TIMEZONE_SOFIA)
        assert store.cleanup_expired_profiles(dt_before, empty_calendar) == 0

        # After stored checkout time: purges cleanly
        dt_after = datetime(2026, 10, 23, 11, 30, tzinfo=TIMEZONE_SOFIA)
        assert store.cleanup_expired_profiles(dt_after, empty_calendar) == 1
        assert store.get_reservation_profile(uid) is None

    def test_owner_blocked_dates_custom_profile_and_expiration(self):
        """Owner blocked stay has customized AT_HOME profile and expires after blocked checkout."""
        cfg = AutomationConfig()
        cfg.HEAT_DURING_BLOCKED_DATES = True
        store = DashboardConfigStore(config=cfg)
        uid = "blocked-family-oct"

        store.set_reservation_profile(uid=uid, profile_type="AT_HOME", end_date="2026-10-25")
        blocked_res = Reservation(
            uid=uid,
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Family Stay",
            reservation_type=ReservationType.BLOCKED
        )

        engine = HVACScheduleEngine(config=cfg)
        dt_stay = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec = engine.evaluate([blocked_res], current_dt=dt_stay)
        assert dec.active_profile == "AT_HOME"
        assert dec.target_temperature == 23.0

        # Expires on checkout date past 11:00
        dt_exp = datetime(2026, 10, 25, 11, 15, tzinfo=TIMEZONE_SOFIA)
        assert store.cleanup_expired_profiles(dt_exp, [blocked_res]) == 1
        assert store.get_reservation_profile(uid) is None


class TestGuestRemoteOverrideSafeguardTimeline:
    """Verifies that manual physical remote adjustments are preserved within dayparts without
    15-minute repeated overwrites, and that scheduled daypart transitions reset the schedule.
    """

    def test_guest_remote_change_preserved_and_resets_at_evening_transition(self):
        """Guest sets remote to 24°C at 14:10 during SKI_DAY (21°C).
        - 14:15 cycle: Safeguard detects drift, preserves 24°C, sends 0 commands.
        - 16:45 cycle: Safeguard preserves 24°C, sends 0 commands.
        - 17:00 cycle: Evening transition triggers (is_transition_tick=True).
          Automation dispatches scheduled evening command (23°C).
        """
        cfg = AutomationConfig()
        cfg.MANUAL_OVERRIDE_PRESERVE = True
        cfg.DRY_RUN = True
        dev_id = cfg.TCL_LIVING_ROOM_ID

        safeguards = SafeguardEngine(config=cfg)

        res = Reservation(
            uid="stay-remote-timeline-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        # Step 1: 14:00 (Daytime target 21°C) - Initial command dispatched
        cmd_1400 = ACCommand(
            device_id=dev_id,
            power=True,
            target_temperature=21.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            reason="Daytime schedule"
        )
        safeguards.record_applied_command(cmd_1400)

        # Step 2: Guest physically changes Living Room AC to 24°C on remote at 14:10
        guest_physical_state = ACState(
            device_id=dev_id,
            power=True,
            target_temperature=24.0,  # +3°C drift from 21.0°C
            current_temperature=21.2,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            is_online=True
        )

        # Step 3: 14:15 Tick (Automation would normally send 21°C)
        dec_1415 = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 14, 15, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Daytime",
            commands_to_send={dev_id: cmd_1400}
        )
        res_1415 = safeguards.filter_and_validate(
            decision=dec_1415,
            current_states={dev_id: guest_physical_state},
            now=dec_1415.timestamp,
            is_transition_tick=False
        )
        passed_1415, _, reason_1415 = res_1415[dev_id]
        # Safeguard MUST block command and preserve guest preference!
        assert passed_1415 is False
        assert "Guest manual override preserved" in reason_1415
        assert "24.0°C" in reason_1415

        # Step 4: 16:45 Tick (Near end of daytime, still in OCCUPIED_DAY)
        dec_1645 = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 16, 45, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Daytime",
            commands_to_send={dev_id: cmd_1400}
        )
        res_1645 = safeguards.filter_and_validate(
            decision=dec_1645,
            current_states={dev_id: guest_physical_state},
            now=dec_1645.timestamp,
            is_transition_tick=False
        )
        passed_1645, _, reason_1645 = res_1645[dev_id]
        # Safeguard STILL preserves guest preference!
        assert passed_1645 is False
        assert "Guest manual override preserved" in reason_1645

        # Step 5: 17:00 Tick - Evening Transition! (OCCUPIED_DAY -> OCCUPIED_EVENING)
        # Target temperature transitions to 23.0°C, is_transition_tick=True
        cmd_1700 = ACCommand(
            device_id=dev_id,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.MID,
            reason="Evening schedule"
        )
        dec_1700 = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 17, 0, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_EVENING,
            active_reservation=res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Evening comfort",
            commands_to_send={dev_id: cmd_1700}
        )
        res_1700 = safeguards.filter_and_validate(
            decision=dec_1700,
            current_states={dev_id: guest_physical_state},
            now=dec_1700.timestamp,
            is_transition_tick=True  # State transition tick!
        )
        passed_1700, out_cmd_1700, reason_1700 = res_1700[dev_id]
        # Scheduled transition is PERMITTED and resets AC to evening target!
        assert passed_1700 is True
        assert out_cmd_1700.target_temperature == 23.0
        assert out_cmd_1700.fan_speed == ACFanSpeed.MID

    def test_guest_power_off_remote_override_resets_at_transition(self):
        """Guest physically powers off unit via remote; safeguard preserves power off persistently across
        daypart transitions (e.g. 17:00) until the guest physically turns unit back ON.
        """
        cfg = AutomationConfig()
        cfg.MANUAL_OVERRIDE_PRESERVE = True
        dev_id = cfg.TCL_LIVING_ROOM_ID
        safeguards = SafeguardEngine(config=cfg)

        res = Reservation(
            uid="stay-power-remote-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        # Baseline: AC was powered ON
        last_cmd = ACCommand(
            device_id=dev_id,
            power=True,
            target_temperature=21.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            reason="Daytime schedule"
        )
        safeguards.record_applied_command(last_cmd)

        # Guest turns AC OFF on remote
        off_state = ACState(
            device_id=dev_id,
            power=False,
            target_temperature=21.0,
            current_temperature=21.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            is_online=True
        )

        # 14:15 Tick within daypart: Preserved OFF!
        dec_day = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 14, 15, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Daytime",
            commands_to_send={dev_id: last_cmd}
        )
        res_day = safeguards.filter_and_validate(
            decision=dec_day,
            current_states={dev_id: off_state},
            now=dec_day.timestamp,
            is_transition_tick=False
        )
        passed_day, _, reason_day = res_day[dev_id]
        assert passed_day is False
        assert "manually turned OFF" in reason_day

        # 17:00 Evening transition: Persistent power-OFF policy KEEPS unit OFF!
        cmd_eve = ACCommand(
            device_id=dev_id,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.MID,
            reason="Evening schedule"
        )
        dec_eve = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 17, 0, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_EVENING,
            active_reservation=res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Evening comfort",
            commands_to_send={dev_id: cmd_eve}
        )
        res_eve = safeguards.filter_and_validate(
            decision=dec_eve,
            current_states={dev_id: off_state},
            now=dec_eve.timestamp,
            is_transition_tick=True
        )
        passed_eve, _, reason_eve = res_eve[dev_id]
        assert passed_eve is False
        assert "Persistent Power-OFF" in reason_eve

        # Guest later turns AC back ON on remote at 22°C (at 18:00)
        on_state = ACState(
            device_id=dev_id,
            power=True,
            target_temperature=22.0,
            current_temperature=21.5,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.MID,
            is_online=True
        )
        dec_eve_turnon = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 18, 0, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_EVENING,
            active_reservation=res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Evening comfort",
            commands_to_send={dev_id: cmd_eve}
        )
        res_eve_turnon = safeguards.filter_and_validate(
            decision=dec_eve_turnon,
            current_states={dev_id: on_state},
            now=dec_eve_turnon.timestamp,
            is_transition_tick=False
        )
        # Power-OFF is cleared, guest setpoint 22°C preserved during current daypart
        passed_turnon, _, reason_turnon = res_eve_turnon[dev_id]
        assert passed_turnon is False
        assert "22.0°C" in reason_turnon

        # Next daypart transition (23:00 Night): normal scheduling resumes!
        cmd_night = ACCommand(
            device_id=dev_id,
            power=True,
            target_temperature=20.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            reason="Night schedule"
        )
        dec_night = ScheduleDecision(
            timestamp=datetime(2026, 10, 22, 23, 0, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_NIGHT,
            active_reservation=res,
            target_power=True,
            target_temperature=20.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.QUIET,
            target_eco=False,
            decision_reason="Night comfort",
            commands_to_send={dev_id: cmd_night}
        )
        res_night = safeguards.filter_and_validate(
            decision=dec_night,
            current_states={dev_id: on_state},
            now=dec_night.timestamp,
            is_transition_tick=True
        )
        passed_night, out_night, _ = res_night[dev_id]
        assert passed_night is True
        assert out_night.target_temperature == 20.0


class TestMobileDashboardControlsAndSecurity:
    """Verifies owner dashboard controls: profile selection, temperature editing, room offsets,
    and CSRF/authentication security barriers.
    """

    def test_mobile_dashboard_unauthenticated_requests_blocked(self):
        """Unauthenticated requests to protected endpoints return 401 Unauthorized."""
        cfg = AutomationConfig()
        cfg.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only-salt-2026-synthetic-session-key-for-tests-only"

        for path in ("/api/config", "/api/reservation-profile", "/api/owner-test"):
            event = {
                "httpMethod": "POST",
                "path": path,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"test": 123})
            }
            resp = handle_dashboard_request(event, None, config=cfg)
            assert resp["statusCode"] == 401
            body = json.loads(resp["body"])
            assert body["error"] == "Unauthorized"

    def test_mobile_dashboard_missing_or_invalid_csrf_blocked(self):
        """Authenticated requests with missing or invalid CSRF token return 403 Forbidden."""
        cfg = AutomationConfig()
        secret = "test-secret-synthetic-session-key-for-tests-only-salt-2026-synthetic-session-key-for-tests-only"
        cfg.SESSION_SECRET_KEY = secret
        token = auth.create_session_token(secret)

        # Missing CSRF
        event_missing = {
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json"
            },
            "body": json.dumps({"default_heating_profile": "AT_HOME"})
        }
        resp_missing = handle_dashboard_request(event_missing, None, config=cfg)
        assert resp_missing["statusCode"] == 403

        # Invalid CSRF
        event_invalid = {
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": "attacker-tampered-token"
            },
            "body": json.dumps({"default_heating_profile": "AT_HOME"})
        }
        resp_invalid = handle_dashboard_request(event_invalid, None, config=cfg)
        assert resp_invalid["statusCode"] == 403

    def test_mobile_dashboard_full_flow_profile_temperatures_and_offsets(self):
        """End-to-end flow: Login -> change diurnal temps & offsets -> assign stay profile -> verify in status."""
        cfg = AutomationConfig()
        secret = "test-secret-synthetic-session-key-for-tests-only-salt-2026-synthetic-session-key-for-tests-only"
        cfg.SESSION_SECRET_KEY = secret
        cfg.DASHBOARD_PASSWORD_HASH = auth.hash_password("OwnerPassword123")
        store = DashboardConfigStore(config=cfg)

        # 1. Login
        login_event = {
            "httpMethod": "POST",
            "path": "/api/login",
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"password": "OwnerPassword123"})
        }
        login_resp = handle_dashboard_request(login_event, None, config=cfg)
        assert login_resp["statusCode"] == 200
        cookie_header = login_resp["headers"]["Set-Cookie"]
        assert "session_token=" in cookie_header
        token = cookie_header.split("session_token=")[1].split(";")[0]

        csrf = auth.generate_csrf_token(token, secret)

        # 2. Update config: Change diurnal setpoints, default profile, and room offsets
        config_update_event = {
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": csrf
            },
            "body": json.dumps({
                "csrf_token": csrf,
                "default_heating_profile": "AT_HOME",
                "schedule_morning_temp": 23.5,
                "schedule_day_temp": 23.5,
                "schedule_evening_temp": 23.5,
                "schedule_night_temp": 20.0,
                "living_room_temp_offset": 1.0,
                "bedroom_temp_offset": -1.0
            })
        }
        cfg_resp = handle_dashboard_request(config_update_event, None, config=cfg)
        assert cfg_resp["statusCode"] == 200

        # Verify saved in config store
        loaded = store.load_dashboard_config()
        assert loaded["default_heating_profile"] == "AT_HOME"
        assert loaded["schedule_morning_temp"] == 23.5
        assert loaded["living_room_temp_offset"] == 1.0
        assert loaded["bedroom_temp_offset"] == -1.0

        # 3. Assign custom profile to upcoming stay
        res_profile_event = {
            "httpMethod": "POST",
            "path": "/api/reservation-profile",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": csrf
            },
            "body": json.dumps({
                "csrf_token": csrf,
                "uid": "stay-fullflow-1",
                "profile": "CUSTOM",
                "custom_temperatures": {
                    "morning_temp": 24.0,
                    "day_temp": 22.0,
                    "evening_temp": 24.0,
                    "night_temp": 20.0
                },
                "living_room_offset": 0.5,
                "bedroom_offset": -0.5
            })
        }
        res_prof_resp = handle_dashboard_request(res_profile_event, None, config=cfg)
        assert res_prof_resp["statusCode"] == 200

        assigned = store.get_reservation_profile("stay-fullflow-1")
        assert assigned is not None
        assert assigned["profile"] == "CUSTOM"
        assert assigned["custom_temperatures"]["morning_temp"] == 24.0
        assert assigned["living_room_offset"] == 0.5
        assert assigned["bedroom_offset"] == -0.5


class TestIndependentPerRoomHeatingProfiles:
    """Verifies independent diurnal room setpoints, exact boundary transitions,
    safe nighttime floor preservation (20°C), dashboard persistence, and backward compatibility.
    """

    def test_at_home_independent_room_temperatures_all_diurnal_slots(self):
        """AT_HOME delivers independent temperatures:
        Living Room: 23°C / 23°C / 23°C / 20°C
        Bedroom:     22°C / 21°C / 22°C / 20°C
        Nighttime 20°C floor is strictly preserved without dropping below 20°C.
        """
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        cfg.BEDROOM_TEMP_OFFSET = 0.0
        cfg.LIVING_ROOM_TEMP_OFFSET = 0.0
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="res-at-home-indep-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Family)",
            reservation_type=ReservationType.CONFIRMED,
        )

        lr_id = cfg.TCL_LIVING_ROOM_ID  # demo_living_room
        br_id = cfg.TCL_BEDROOM_ID       # demo_bedroom

        slots = [
            # (time, state, lr_expected_temp, br_expected_temp, expected_fan, expected_led, expected_gear)
            (time(8, 30), SystemState.OCCUPIED_MORNING, 23.0, 22.0, ACFanSpeed.MID, True, 4),
            (time(14, 0), SystemState.OCCUPIED_DAY, 23.0, 21.0, ACFanSpeed.MID, True, 4),
            (time(20, 0), SystemState.OCCUPIED_EVENING, 23.0, 22.0, ACFanSpeed.MID, True, 4),
            (time(2, 0), SystemState.OCCUPIED_NIGHT, 20.0, 20.0, ACFanSpeed.QUIET, False, 1),
        ]

        for t, expected_state, lr_temp, br_temp, exp_fan, exp_led, exp_gear in slots:
            dt = datetime.combine(date(2026, 10, 22), t, tzinfo=TIMEZONE_SOFIA)
            dec = engine.evaluate([res], current_dt=dt)

            assert dec.system_state == expected_state, f"Expected {expected_state} at {t}, got {dec.system_state}"
            assert dec.target_power is True
            assert dec.active_profile == "AT_HOME"

            lr_cmd = dec.commands_to_send[lr_id]
            br_cmd = dec.commands_to_send[br_id]

            # Verify Living Room
            assert lr_cmd.power is True
            assert lr_cmd.target_temperature == lr_temp, f"LR temp mismatch at {t}: expected {lr_temp}, got {lr_cmd.target_temperature}"
            assert lr_cmd.fan_speed == exp_fan
            assert lr_cmd.display_led == exp_led
            assert lr_cmd.beep_feedback is False

            lr_payload = lr_cmd.to_shadow_payload()["state"]["desired"]
            assert lr_payload["targetTemperature"] == lr_temp
            assert lr_payload["windSpeed7Gear"] == exp_gear
            assert lr_payload["screen"] == (1 if exp_led else 0)
            assert lr_payload["beepSwitch"] == 0

            # Verify Bedroom
            assert br_cmd.power is True
            assert br_cmd.target_temperature == br_temp, f"BR temp mismatch at {t}: expected {br_temp}, got {br_cmd.target_temperature}"
            assert br_cmd.fan_speed == exp_fan
            assert br_cmd.display_led == exp_led
            assert br_cmd.beep_feedback is False

            br_payload = br_cmd.to_shadow_payload()["state"]["desired"]
            assert br_payload["targetTemperature"] == br_temp
            assert br_payload["windSpeed7Gear"] == exp_gear
            assert br_payload["screen"] == (1 if exp_led else 0)
            assert br_payload["beepSwitch"] == 0

            # Guaranteed night floor
            if expected_state == SystemState.OCCUPIED_NIGHT:
                assert br_cmd.target_temperature >= 20.0, "Nighttime Bedroom temp must never drop below 20.0°C"
                assert lr_cmd.target_temperature >= 20.0, "Nighttime Living Room temp must never drop below 20.0°C"

    def test_at_home_exact_boundary_transitions_all_dayparts(self):
        """Verify seamless transitions at 07:00, 10:00, 17:00, and 23:00 boundary seconds."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        cfg.BEDROOM_TEMP_OFFSET = 0.0
        cfg.LIVING_ROOM_TEMP_OFFSET = 0.0
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="res-boundary-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED,
        )

        lr_id = cfg.TCL_LIVING_ROOM_ID
        br_id = cfg.TCL_BEDROOM_ID

        # Boundary pairs: (pre_dt, expected_pre_state, post_dt, expected_post_state, expected_lr_pre, expected_lr_post, expected_br_pre, expected_br_post)
        boundaries = [
            # 1. 07:00 transition (Night -> Morning)
            (
                datetime(2026, 10, 22, 6, 59, 59, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_NIGHT,
                datetime(2026, 10, 22, 7, 0, 0, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_MORNING,
                20.0, 23.0,  # LR
                20.0, 22.0,  # BR
            ),
            # 2. 10:00 transition (Morning -> Day)
            (
                datetime(2026, 10, 22, 9, 59, 59, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_MORNING,
                datetime(2026, 10, 22, 10, 0, 0, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_DAY,
                23.0, 23.0,  # LR remains 23°C
                22.0, 21.0,  # BR drops to 21°C
            ),
            # 3. 17:00 transition (Day -> Evening)
            (
                datetime(2026, 10, 22, 16, 59, 59, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_DAY,
                datetime(2026, 10, 22, 17, 0, 0, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_EVENING,
                23.0, 23.0,  # LR remains 23°C
                21.0, 22.0,  # BR warms back to 22°C
            ),
            # 4. 23:00 transition (Evening -> Night)
            (
                datetime(2026, 10, 22, 22, 59, 59, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_EVENING,
                datetime(2026, 10, 22, 23, 0, 0, tzinfo=TIMEZONE_SOFIA),
                SystemState.OCCUPIED_NIGHT,
                23.0, 20.0,  # LR drops to 20°C
                22.0, 20.0,  # BR drops to 20°C
            ),
        ]

        for pre_dt, pre_state, post_dt, post_state, lr_pre, lr_post, br_pre, br_post in boundaries:
            dec_pre = engine.evaluate([res], current_dt=pre_dt)
            dec_post = engine.evaluate([res], current_dt=post_dt)

            assert dec_pre.system_state == pre_state, f"Failed pre-transition at {pre_dt}"
            assert dec_post.system_state == post_state, f"Failed post-transition at {post_dt}"

            # Check LR
            assert dec_pre.commands_to_send[lr_id].target_temperature == lr_pre
            assert dec_post.commands_to_send[lr_id].target_temperature == lr_post

            # Check BR
            assert dec_pre.commands_to_send[br_id].target_temperature == br_pre
            assert dec_post.commands_to_send[br_id].target_temperature == br_post

            # Check Beep feedback is consistently False across all transitions
            assert dec_pre.commands_to_send[lr_id].beep_feedback is False
            assert dec_post.commands_to_send[lr_id].beep_feedback is False
            assert dec_pre.commands_to_send[br_id].beep_feedback is False
            assert dec_post.commands_to_send[br_id].beep_feedback is False

    def test_dashboard_persists_and_hydrates_independent_room_temperatures(self):
        """Verify that dashboard /api/config persists 8 room-specific diurnal parameters,
        and applies them to AutomationConfig and HVACScheduleEngine."""
        cfg = AutomationConfig()
        secret = "synthetic-test-session-secret-at-least-32-characters"
        cfg.SESSION_SECRET_KEY = secret
        cfg.DASHBOARD_PASSWORD_HASH = auth.hash_password("OwnerPassword123")
        store = DashboardConfigStore(config=cfg)

        token = auth.create_session_token(secret)
        csrf = auth.generate_csrf_token(token, secret)

        # 1. Update independent room temperatures via API
        event = {
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {
                "cookie": f"session_token={token}",
                "content-type": "application/json",
                "x-csrf-token": csrf
            },
            "body": json.dumps({
                "csrf_token": csrf,
                "default_heating_profile": "AT_HOME",
                "living_room_morning_temp": 23.5,
                "living_room_day_temp": 23.5,
                "living_room_evening_temp": 23.5,
                "living_room_night_temp": 20.0,
                "bedroom_morning_temp": 21.5,
                "bedroom_day_temp": 20.5,
                "bedroom_evening_temp": 21.5,
                "bedroom_night_temp": 20.0,
            })
        }

        resp = handle_dashboard_request(event, None, config=cfg)
        assert resp["statusCode"] == 200

        # 2. Verify stored in S3/cache
        loaded = store.load_dashboard_config()
        assert loaded["living_room_morning_temp"] == 23.5
        assert loaded["living_room_day_temp"] == 23.5
        assert loaded["living_room_evening_temp"] == 23.5
        assert loaded["living_room_night_temp"] == 20.0
        assert loaded["bedroom_morning_temp"] == 21.5
        assert loaded["bedroom_day_temp"] == 20.5
        assert loaded["bedroom_evening_temp"] == 21.5
        assert loaded["bedroom_night_temp"] == 20.0

        # 3. Apply to fresh AutomationConfig
        fresh_cfg = AutomationConfig()
        fresh_cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        fresh_cfg.BEDROOM_TEMP_OFFSET = 0.0
        store.apply_to_config(fresh_cfg)

        assert fresh_cfg.LIVING_ROOM_MORNING_TEMP == 23.5
        assert fresh_cfg.BEDROOM_DAY_TEMP == 20.5

        # 4. Schedule evaluation using hydrated config
        engine = HVACScheduleEngine(config=fresh_cfg)
        res = Reservation(
            uid="res-custom-temps-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED,
        )

        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_day = engine.evaluate([res], current_dt=dt_day)

        assert dec_day.commands_to_send[fresh_cfg.TCL_LIVING_ROOM_ID].target_temperature == 23.5
        assert dec_day.commands_to_send[fresh_cfg.TCL_BEDROOM_ID].target_temperature == 20.5

    def test_legacy_offset_preserved_when_bedroom_temp_offset_nonzero(self):
        """When bedroom_temp_offset is explicitly non-zero (legacy test configuration),
        it applies mathematically relative to decision target temperature."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        cfg.BEDROOM_TEMP_OFFSET = -1.5
        engine = HVACScheduleEngine(config=cfg)

        res = Reservation(
            uid="res-legacy-offset-1",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED,
        )

        dt_morning = datetime(2026, 10, 22, 8, 30, tzinfo=TIMEZONE_SOFIA)
        dec = engine.evaluate([res], current_dt=dt_morning)

        # Base decision is 23.0°C. With offset -1.5, Bedroom becomes 21.5°C
        assert dec.commands_to_send[cfg.TCL_LIVING_ROOM_ID].target_temperature == 23.0
        assert dec.commands_to_send[cfg.TCL_BEDROOM_ID].target_temperature == 21.5

    def test_guest_physical_remote_override_preserved_with_independent_rooms(self):
        """Guest adjusting Bedroom AC with physical remote is preserved across 15m ticks,
        without affecting Living Room or being overwritten until next scheduled transition."""
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        cfg.BEDROOM_TEMP_OFFSET = 0.0
        scheduler = HVACScheduleEngine(config=cfg)
        safeguards = SafeguardEngine(config=cfg)

        res = Reservation(
            uid="res-remote-guest-stay",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED,
        )

        lr_id = cfg.TCL_LIVING_ROOM_ID
        br_id = cfg.TCL_BEDROOM_ID

        # 14:00 daytime cycle: LR 23°C, BR 21°C
        t_1400 = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_1400 = scheduler.evaluate([res], current_dt=t_1400)

        # Record applied commands
        safeguards.record_applied_command(dec_1400.commands_to_send[lr_id])
        safeguards.record_applied_command(dec_1400.commands_to_send[br_id])

        # Guest adjusts Bedroom AC to 23.5°C via IR remote (which also resets beep_feedback=True)
        current_states = {
            lr_id: ACState(
                device_id=lr_id,
                power=True,
                target_temperature=23.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.MID,
                display_led=True,
                beep_feedback=False,
                is_online=True
            ),
            br_id: ACState(
                device_id=br_id,
                power=True,
                target_temperature=23.5,  # Guest increased Bedroom to 23.5°C
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.MID,
                display_led=True,
                beep_feedback=True,        # Hardware buzzer toggled by physical remote
                is_online=True
            )
        }

        # 14:15 Tick (non-transition)
        t_1415 = datetime(2026, 10, 22, 14, 15, tzinfo=TIMEZONE_SOFIA)
        results = safeguards.filter_and_validate(
            decision=dec_1400,
            current_states=current_states,
            now=t_1415,
            is_transition_tick=False
        )

        # LR is Debounced (already 23°C)
        should_send_lr, _, rationale_lr = results[lr_id]
        assert should_send_lr is False
        assert "Debounce" in rationale_lr

        # BR is Preserved (guest manual override)
        should_send_br, _, rationale_br = results[br_id]
        assert should_send_br is False
        assert "Guest manual override preserved" in rationale_br

        # 17:00 Evening transition tick -> Resets to scheduled evening setpoints (LR 23°C, BR 22°C)
        t_1700 = datetime(2026, 10, 22, 17, 0, tzinfo=TIMEZONE_SOFIA)
        dec_1700 = scheduler.evaluate([res], current_dt=t_1700)

        results_1700 = safeguards.filter_and_validate(
            decision=dec_1700,
            current_states=current_states,
            now=t_1700,
            is_transition_tick=True
        )

        should_send_br_trans, cmd_br_trans, _ = results_1700[br_id]
        assert should_send_br_trans is True
        assert cmd_br_trans.target_temperature == 22.0
        assert cmd_br_trans.beep_feedback is False

    def test_dashboard_rejects_invalid_room_temperatures(self):
        """Dashboard POST /api/config must reject out-of-range (<16°C or >30°C) and malformed values."""
        cfg = AutomationConfig()
        secret = "synthetic-test-session-secret-at-least-32-characters"
        cfg.SESSION_SECRET_KEY = secret
        token = auth.create_session_token(secret)
        csrf = auth.generate_csrf_token(token, secret)

        # 1. Out of range high (> 30°C)
        resp_high = handle_dashboard_request({
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {"cookie": f"session_token={token}", "content-type": "application/json", "x-csrf-token": csrf},
            "body": json.dumps({"csrf_token": csrf, "bedroom_day_temp": 35.0})
        }, None, config=cfg)
        assert resp_high["statusCode"] == 400
        assert "out of range" in json.loads(resp_high["body"])["error"]

        # 2. Out of range low (< 16°C)
        resp_low = handle_dashboard_request({
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {"cookie": f"session_token={token}", "content-type": "application/json", "x-csrf-token": csrf},
            "body": json.dumps({"csrf_token": csrf, "living_room_morning_temp": 14.5})
        }, None, config=cfg)
        assert resp_low["statusCode"] == 400
        assert "out of range" in json.loads(resp_low["body"])["error"]

        # 3. Non-numeric value
        resp_str = handle_dashboard_request({
            "httpMethod": "POST",
            "path": "/api/config",
            "headers": {"cookie": f"session_token={token}", "content-type": "application/json", "x-csrf-token": csrf},
            "body": json.dumps({"csrf_token": csrf, "living_room_day_temp": "tropical"})
        }, None, config=cfg)
        assert resp_str["statusCode"] == 400
        assert "Invalid number" in json.loads(resp_str["body"])["error"]

    def test_sofia_timezone_and_daylight_saving_time(self):
        """Europe/Sofia timezone correctly applies UTC+3 in summer (EEST) and UTC+2 in winter (EET),
        and the diurnal schedule evaluates strictly against Sofia local wall-clock hours.
        """
        cfg = AutomationConfig()
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        engine = HVACScheduleEngine(config=cfg)

        # Summer: July 15 (EEST, UTC+3)
        dt_summer_noon = datetime(2026, 7, 15, 12, 0, tzinfo=TIMEZONE_SOFIA)
        offset_summer = dt_summer_noon.utcoffset()
        assert offset_summer == timedelta(hours=3), f"Summer in Sofia must be UTC+3, got {offset_summer}"

        # Winter: January 15 (EET, UTC+2)
        dt_winter_noon = datetime(2026, 1, 15, 12, 0, tzinfo=TIMEZONE_SOFIA)
        offset_winter = dt_winter_noon.utcoffset()
        assert offset_winter == timedelta(hours=2), f"Winter in Sofia must be UTC+2, got {offset_winter}"

        # Schedule evaluation in winter: 08:30 Sofia -> Morning state (LR 23°C, BR 22°C)
        winter_res = Reservation(
            uid="winter-booking",
            start_date=date(2026, 1, 14),
            end_date=date(2026, 1, 20),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED,
        )
        dt_winter_morn = datetime(2026, 1, 15, 8, 30, tzinfo=TIMEZONE_SOFIA)
        dec_winter = engine.evaluate([winter_res], current_dt=dt_winter_morn)
        assert dec_winter.system_state == SystemState.OCCUPIED_MORNING
        assert dec_winter.commands_to_send[cfg.TCL_LIVING_ROOM_ID].target_temperature == 23.0
        assert dec_winter.commands_to_send[cfg.TCL_BEDROOM_ID].target_temperature == 22.0

        # Schedule evaluation in summer: 02:00 Sofia -> Night state (LR 20°C, BR 20°C)
        summer_res = Reservation(
            uid="summer-booking",
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 20),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED,
        )
        dt_summer_night = datetime(2026, 7, 15, 2, 0, tzinfo=TIMEZONE_SOFIA)
        dec_summer = engine.evaluate([summer_res], current_dt=dt_summer_night)
        assert dec_summer.system_state == SystemState.OCCUPIED_NIGHT
        assert dec_summer.commands_to_send[cfg.TCL_LIVING_ROOM_ID].target_temperature == 20.0
        assert dec_summer.commands_to_send[cfg.TCL_BEDROOM_ID].target_temperature == 20.0

    def test_reservation_specific_at_home_profile_isolated_from_calendar(self):
        """Applying AT_HOME to a specific reservation UID isolates the profile to that stay only,
        without modifying the iCal feed, and unassigned reservations default to SKI_DAY.
        """
        cfg = AutomationConfig()
        secret = "synthetic-test-session-secret-at-least-32-characters"
        cfg.SESSION_SECRET_KEY = secret
        store = DashboardConfigStore(config=cfg)

        token = auth.create_session_token(secret)
        csrf = auth.generate_csrf_token(token, secret)

        # Stay A: Confirmed reservation assigned AT_HOME via dashboard
        stay_a_uid = "stay-confirmed-family-123"
        resp_assign = handle_dashboard_request({
            "httpMethod": "POST",
            "path": "/api/reservation-profile",
            "headers": {"cookie": f"session_token={token}", "content-type": "application/json", "x-csrf-token": csrf},
            "body": json.dumps({"csrf_token": csrf, "uid": stay_a_uid, "profile": "AT_HOME"})
        }, None, config=cfg)
        assert resp_assign["statusCode"] == 200

        # Verify saved in config store
        prof_a = store.get_reservation_profile(stay_a_uid)
        assert prof_a is not None
        assert prof_a["profile"] == "AT_HOME"

        # Stay B: Unassigned stay defaults to system default (SKI_DAY)
        stay_b_uid = "stay-unassigned-skiers-456"
        prof_b = store.get_reservation_profile(stay_b_uid)
        assert prof_b is None  # No explicit assignment

        # Run scheduler for Stay A during daytime (14:00) -> Evaluates to AT_HOME (LR 23°C, BR 21°C)
        res_a = Reservation(
            uid=stay_a_uid,
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Family)",
            reservation_type=ReservationType.CONFIRMED,
        )
        store.apply_to_config(cfg)
        engine = HVACScheduleEngine(config=cfg)
        dt_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_a = engine.evaluate([res_a], current_dt=dt_day)
        assert dec_a.active_profile == "AT_HOME"
        assert dec_a.commands_to_send[cfg.TCL_LIVING_ROOM_ID].target_temperature == 23.0
        assert dec_a.commands_to_send[cfg.TCL_BEDROOM_ID].target_temperature == 21.0

        # Run scheduler for Stay B during daytime (14:00) -> Evaluates to SKI_DAY (LR 21°C, BR 20°C)
        res_b = Reservation(
            uid=stay_b_uid,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 5),
            summary="Reserved (Skiers)",
            reservation_type=ReservationType.CONFIRMED,
        )
        dt_day_b = datetime(2026, 11, 3, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec_b = engine.evaluate([res_b], current_dt=dt_day_b)
        assert dec_b.active_profile == "SKI_DAY"
        assert dec_b.commands_to_send[cfg.TCL_LIVING_ROOM_ID].target_temperature == 21.0
        assert dec_b.commands_to_send[cfg.TCL_BEDROOM_ID].target_temperature == 21.0
