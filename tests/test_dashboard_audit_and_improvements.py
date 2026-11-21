"""Regression and verification tests for Dashboard Telemetry, Occupancy Overrides, and Safe Concurrency."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.dashboard import auth
from src.dashboard.config_store import DashboardConfigStore, ConcurrencyConflictError
from src.dashboard.handler import _build_status_data, handle_dashboard_request
from src.models import OwnerTestSession, ACMode, ACFanSpeed, ReservationType
from src.state_store import StateStore


class TestDashboardTelemetryAndOverrides:
    """Verify Phase 1 & 2 requirements for telemetry freshness and override classification."""

    def test_fresh_telemetry_timestamp_and_status(self, tmp_path):
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)

        state_store = StateStore(s3_bucket="")
        now_utc = datetime.now(timezone.utc)
        test_state = {
            "updated_at": now_utc.isoformat(),
            "device_states": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": False,
                    "target_temperature": 22.0,
                    "current_temperature": 20.5,
                    "mode": "Heat",
                    "online": True
                }
            },
            "last_applied_commands": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": False,
                    "target_temperature": 21.0
                }
            }
        }
        state_store.save_state(test_state)

        status = _build_status_data(cfg, cfg_store)

        assert status["telemetry_is_stale"] is False
        assert status["telemetry_age_seconds"] < 10
        assert "Sofia" in status["telemetry_timestamp"]
        lr = status["devices"]["living_room"]
        assert lr["online"] is True
        # Unit is OFF: setpoint difference (22 vs 21) must NOT trigger an override!
        assert lr["override_active"] is False
        assert lr["override_type"] is None

    def test_stale_telemetry_detection(self, tmp_path):
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)

        state_store = StateStore(s3_bucket="")
        # 30 minutes ago (> 20 min stale threshold)
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        test_state = {
            "updated_at": stale_time.isoformat(),
            "device_states": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": False,
                    "target_temperature": 22.0,
                    "current_temperature": 20.5,
                    "online": True
                }
            }
        }
        state_store.save_state(test_state)

        status = _build_status_data(cfg, cfg_store)

        assert status["telemetry_is_stale"] is True
        assert status["telemetry_age_seconds"] >= 1800
        assert "STALE" in status["telemetry_age_str"]
        # Must NOT assert device is online when telemetry is stale!
        lr = status["devices"]["living_room"]
        assert lr["online"] is False
        assert lr["raw_online"] is True

    def test_override_classification_unoccupied(self, tmp_path):
        """When property is unoccupied, manual adjustments are labeled MANUAL DEVICE ADJUSTMENT."""
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)
        cfg_store.clear_owner_test_session()

        state_store = StateStore(s3_bucket="")
        now_utc = datetime.now(timezone.utc)
        # AC was turned ON manually while automation wanted it OFF
        test_state = {
            "updated_at": now_utc.isoformat(),
            "known_reservations": {},
            "device_states": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": True,
                    "target_temperature": 24.0,
                    "current_temperature": 20.5,
                    "online": True
                }
            },
            "last_applied_commands": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": False,
                    "target_temperature": 21.0
                }
            }
        }
        state_store.save_state(test_state)

        status = _build_status_data(cfg, cfg_store)
        lr = status["devices"]["living_room"]
        assert lr["override_active"] is True
        assert lr["override_type"] == "MANUAL DEVICE ADJUSTMENT"
        assert "turned ON manually" in lr["override_details"]

    def test_override_classification_guest_stay(self, tmp_path):
        """During active confirmed guest stay, adjustments are labeled GUEST OVERRIDE."""
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)
        cfg_store.clear_owner_test_session()

        now_sofia = datetime.now(TIMEZONE_SOFIA)
        today_str = now_sofia.date().isoformat()
        tomorrow_str = (now_sofia.date() + timedelta(days=2)).isoformat()

        state_store = StateStore(s3_bucket="")
        now_utc = datetime.now(timezone.utc)
        test_state = {
            "updated_at": now_utc.isoformat(),
            "known_reservations": {
                "guest-123": {
                    "uid": "guest-123",
                    "type": "CONFIRMED",
                    "start_date": today_str,
                    "end_date": tomorrow_str,
                    "summary": "Reserved"
                }
            },
            "device_states": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": True,
                    "target_temperature": 25.0,
                    "current_temperature": 22.0,
                    "online": True
                }
            },
            "last_applied_commands": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": True,
                    "target_temperature": 22.0
                }
            }
        }
        state_store.save_state(test_state)

        status = _build_status_data(cfg, cfg_store)
        lr = status["devices"]["living_room"]
        assert lr["override_active"] is True
        assert lr["override_type"] == "GUEST OVERRIDE"
        assert "Manual setpoint: 25.0°C" in lr["override_details"]

    def test_override_classification_owner_present(self, tmp_path):
        """During active OWNER_PRESENT session, adjustments are labeled OWNER OVERRIDE."""
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)

        now_sofia = datetime.now(TIMEZONE_SOFIA)
        owner_session = OwnerTestSession(
            active=True,
            started_at=now_sofia - timedelta(hours=1),
            expires_at=now_sofia + timedelta(hours=12),
            duration_hours=13.0,
            session_type="OWNER_PRESENT"
        )
        cfg_store.save_owner_test_session(owner_session)

        state_store = StateStore(s3_bucket="")
        now_utc = datetime.now(timezone.utc)
        test_state = {
            "updated_at": now_utc.isoformat(),
            "known_reservations": {},
            "device_states": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": True,
                    "target_temperature": 24.5,
                    "current_temperature": 22.0,
                    "online": True
                }
            },
            "last_applied_commands": {
                cfg.TCL_LIVING_ROOM_ID: {
                    "power": True,
                    "target_temperature": 23.0
                }
            }
        }
        state_store.save_state(test_state)

        status = _build_status_data(cfg, cfg_store)
        lr = status["devices"]["living_room"]
        assert lr["override_active"] is True
        assert lr["override_type"] == "OWNER OVERRIDE"


class TestSafeConfigurationSavingAndConcurrency:
    """Verify Phase 3 requirements for partial form saving, concurrency, and safety locks."""

    def test_optimistic_concurrency_conflict_detected(self):
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)

        # Baseline save
        cfg_store.save_dashboard_config({"schedule_day_temp": 21.0})
        saved = cfg_store.load_dashboard_config()
        first_version = saved.get("version", 1)
        first_updated_at = saved.get("updated_at")

        # Save an update from another concurrent session
        cfg_store.save_dashboard_config({"schedule_day_temp": 22.0})

        # Now attempt to save with stale version metadata
        with pytest.raises(ConcurrencyConflictError):
            cfg_store.save_dashboard_config(
                {"schedule_day_temp": 23.0},
                expected_version=first_version
            )

        with pytest.raises(ConcurrencyConflictError):
            cfg_store.save_dashboard_config(
                {"schedule_day_temp": 23.0},
                expected_updated_at=first_updated_at
            )

    def test_post_config_returns_409_on_conflict(self):
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only"
        secret_key = cfg.SESSION_SECRET_KEY
        token = auth.create_session_token(secret_key)
        csrf = auth.generate_csrf_token(token, secret_key)

        cfg_store = DashboardConfigStore(config=cfg)
        cfg_store.save_dashboard_config({"schedule_day_temp": 21.0})
        saved = cfg_store.load_dashboard_config()
        stale_version = saved.get("version", 1)

        # Intervening update
        cfg_store.save_dashboard_config({"schedule_day_temp": 22.5})

        event = {
            "rawPath": "/api/config",
            "requestContext": {"http": {"method": "POST", "sourceIp": "127.0.0.1"}},
            "cookies": [f"session_token={token}"],
            "headers": {"content-type": "application/json", "x-csrf-token": csrf},
            "body": json.dumps({
                "schedule_day_temp": 23.0,
                "base_version": stale_version
            })
        }

        resp = handle_dashboard_request(event, None, config=cfg)
        assert resp["statusCode"] == 409
        body = json.loads(resp["body"])
        assert "conflict" in body["error"].lower()

    def test_safety_locks_cannot_be_overridden_via_config(self):
        """Dashboard config POST must never alter TCL_DRY_RUN or TCL_READ_ONLY."""
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only"
        token = auth.create_session_token(cfg.SESSION_SECRET_KEY)
        csrf = auth.generate_csrf_token(token, cfg.SESSION_SECRET_KEY)

        event = {
            "rawPath": "/api/config",
            "requestContext": {"http": {"method": "POST", "sourceIp": "127.0.0.1"}},
            "cookies": [f"session_token={token}"],
            "headers": {"content-type": "application/json", "x-csrf-token": csrf},
            "body": json.dumps({
                "tcl_dry_run": False,
                "dry_run": False,
                "tcl_read_only": False,
                "read_only": False,
                "schedule_day_temp": 22.0
            })
        }

        resp = handle_dashboard_request(event, None, config=cfg)
        assert resp["statusCode"] == 200
        saved_cfg = DashboardConfigStore(config=cfg).load_dashboard_config()
        assert "tcl_dry_run" not in saved_cfg
        assert "dry_run" not in saved_cfg
        assert "tcl_read_only" not in saved_cfg
        assert "read_only" not in saved_cfg
        assert cfg.DRY_RUN is True
        assert cfg.TCL_READ_ONLY is True

    def test_simultaneous_concurrent_updates_detected(self):
        """Simultaneous concurrent update attempts from two sessions on the same version must raise ConcurrencyConflictError on the second writer."""
        from concurrent.futures import ThreadPoolExecutor
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)

        # Baseline version
        cfg_store.save_dashboard_config({"schedule_day_temp": 21.0})
        initial = cfg_store.load_dashboard_config()
        base_ver = initial.get("version", 1)
        base_upd = initial.get("updated_at")

        results = []
        errors = []

        def attempt_update(temp_val):
            try:
                ok = cfg_store.save_dashboard_config(
                    {"schedule_day_temp": temp_val},
                    expected_version=base_ver,
                    expected_updated_at=base_upd
                )
                results.append((temp_val, ok))
            except ConcurrencyConflictError as ce:
                errors.append(ce)

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(attempt_update, 22.0)
            f2 = executor.submit(attempt_update, 23.5)
            f1.result()
            f2.result()

        # Exactly one thread succeeds and the other receives ConcurrencyConflictError
        assert len(results) == 1, f"Expected exactly 1 success, got {len(results)}"
        assert len(errors) == 1, f"Expected exactly 1 ConcurrencyConflictError, got {len(errors)}"
        assert "conflict" in str(errors[0]).lower()

        # Final version must be incremented by exactly 1
        final_cfg = cfg_store.load_dashboard_config()
        assert final_cfg["version"] == base_ver + 1

    def test_s3_conditional_write_precondition_failed_translation(self, monkeypatch):
        """When AWS S3 returns PreconditionFailed (HTTP 412) on IfMatch, save_dashboard_config must raise ConcurrencyConflictError."""
        from botocore.exceptions import ClientError
        cfg = AutomationConfig()
        cfg.S3_BUCKET = "test-bucket"
        cfg_store = DashboardConfigStore(config=cfg)

        # Mock S3 get_object
        class MockS3Client:
            def get_object(self, Bucket, Key):
                body = json.dumps({"version": 2, "updated_at": "2026-10-18T20:00:00+03:00", "schedule_day_temp": 21.0}).encode("utf-8")
                class MockBody:
                    def read(self):
                        return body
                return {"Body": MockBody(), "ETag": '"etag-v2"'}

            def put_object(self, **kwargs):
                # Simulate S3 conditional check failure because someone modified the object in between
                err_resp = {"Error": {"Code": "PreconditionFailed", "Message": "At least one of the pre-conditions you specified did not hold"}}
                raise ClientError(err_resp, "PutObject")

        import boto3
        monkeypatch.setattr(boto3, "client", lambda service: MockS3Client())

        with pytest.raises(ConcurrencyConflictError) as exc_info:
            cfg_store.save_dashboard_config(
                {"schedule_day_temp": 22.0},
                expected_version=2,
                expected_etag='"etag-v2"'
            )

        assert "precondition" in str(exc_info.value).lower() or "conflict" in str(exc_info.value).lower()

    def test_unrelated_configuration_and_reservation_profiles_preserved(self):
        """Saving partial form fields must preserve unrelated settings and custom reservation profiles."""
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)

        custom_profiles = {
            "synthetic-guest-profile": {
                "uid": "synthetic-guest-profile",
                "profile": "AT_HOME",
                "custom_temperatures": {"day": 24.0, "night": 21.0}
            }
        }

        # Initialize config with custom profile and offsets
        cfg_store.save_dashboard_config({
            "reservation_profiles": custom_profiles,
            "bedroom_temp_offset": 1.5,
            "checkin_time": "16:00",
            "checkout_time": "10:30"
        })

        initial = cfg_store.load_dashboard_config()
        curr_ver = initial["version"]

        # Now save ONLY a change to schedule_day_temp
        cfg_store.save_dashboard_config(
            {"schedule_day_temp": 22.5},
            expected_version=curr_ver
        )

        updated = cfg_store.load_dashboard_config()
        assert updated["schedule_day_temp"] == 22.5
        # Verify unrelated fields and profiles were 100% preserved
        assert "synthetic-guest-profile" in updated["reservation_profiles"]
        assert updated["reservation_profiles"]["synthetic-guest-profile"]["profile"] == "AT_HOME"
        assert updated["bedroom_temp_offset"] == 1.5
        assert updated["checkin_time"] == "16:00"
        assert updated["checkout_time"] == "10:30"

    def test_telemetry_source_distinguishes_cached_s3_from_live(self):
        """Dashboard status must explicitly identify S3 cached state and not claim direct live telemetry."""
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg_store = DashboardConfigStore(config=cfg)

        status = _build_status_data(cfg, cfg_store)
        assert "telemetry_source" in status
        assert "S3" in status["telemetry_source"]
        assert status["telemetry_is_live_direct"] is False
