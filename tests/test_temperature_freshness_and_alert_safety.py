"""Exhaustive test suite for Temperature Freshness Classification and Low-Temperature Alert Safety.

Covers the 15 required verification scenarios:
1. Fresh temperature above threshold -> No alert.
2. Fresh temperature below 16°C -> WARNING alert dispatched.
3. Fresh temperature below 12°C -> ELEVATED alert dispatched.
4. Fresh temperature below 8°C -> CRITICAL alert dispatched.
5. Stale reading above 16°C -> Retains active alert, does not clear it.
6. Stale reading below 16°C -> Triggers alert labeled as [STALE TELEMETRY].
7. Missing temperature timestamp -> Treated as UNAVAILABLE, labeled.
8. Offline device -> Treated as offline, not low temperature.
9. Manually powered-OFF device -> Treated as guest disabled, low-temperature monitoring remains active.
10. Repeated monitoring cycles -> Cooldown deduplication works as expected.
11. Lambda cold start -> Successfully hydrates alert state and freshness.
12. Notification cooldown -> Same-tier alerts suppressed within 4h.
13. Critical severity escalation -> Escalation from warning to critical bypasses cooldown.
14. Dashboard freshness display -> Returns proper status, age, and warnings.
15. Existing guest override behavior -> Preserved through cycles.
"""

import json
import unittest
from datetime import datetime, date, time as dtime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.dashboard import auth
from src.dashboard.config_store import DashboardConfigStore
from src.dashboard.handler import handle_dashboard_request, _build_status_data
from src.models import (
    ACState,
    ACMode,
    ACFanSpeed,
    Reservation,
    ReservationType,
    SystemState,
)
from src.orchestrator import AutomationOrchestrator
from src.state_store import StateStore


class TestTemperatureFreshnessAndAlertSafety(unittest.TestCase):
    """15-Scenario Regression Suite for Temperature Freshness & Alert Safety."""

    def setUp(self):
        self.config = AutomationConfig()
        self.config.DRY_RUN = True
        self.config.TCL_READ_ONLY = True
        self.config.TEMP_FRESHNESS_MAX_AGE_MINUTES = 60.0
        self.config.PROLONGED_STALE_ALERT_HOURS = 3.0
        self.config.LOW_TEMP_ALERT_COOLDOWN_HOURS = 4.0
        self.config.LOW_TEMP_WARNING_THRESHOLD = 16.0
        self.config.LOW_TEMP_ELEVATED_THRESHOLD = 12.0
        self.config.LOW_TEMP_CRITICAL_THRESHOLD = 8.0
        self.config.SESSION_SECRET_KEY = "test-session-secret-key-12345-synthetic-session-key-for-tests-only"

        self.lr_id = self.config.TCL_LIVING_ROOM_ID or "demo_living_room"
        self.br_id = self.config.TCL_BEDROOM_ID or "demo_bedroom"
        self.config.DEVICE_IDS = [self.lr_id, self.br_id]

        self.res = Reservation(
            uid="res-stay-oct22-dec22",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Confirmed Guest Stay",
            reservation_type=ReservationType.CONFIRMED,
        )

    # -------------------------------------------------------------------------
    # Scenario 1: Fresh temperature above threshold -> No alert
    # -------------------------------------------------------------------------
    def test_01_fresh_temperature_above_threshold_no_alert(self):
        """1. Fresh temperature (21.0°C) above warning threshold triggers no alert."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        dt = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=21.0, current_temperature=20.5, temp_measured_at=dt, is_online=True),
        })

        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 0, "No frost alerts should be dispatched for comfortable temperatures.")
        self.assertEqual(len(orch._last_frost_alerts), 0, "No frost alert records should be tracked.")

    # -------------------------------------------------------------------------
    # Scenario 2: Fresh temperature below 16°C -> WARNING alert dispatched
    # -------------------------------------------------------------------------
    def test_02_fresh_temperature_below_16c_warning_alert(self):
        """2. Fresh temperature (15.0°C) triggers WARNING alert."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        dt = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=15.0, temp_measured_at=dt, is_online=True),
        })

        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 1)
        evt = frost_calls[0]
        self.assertEqual(evt.severity, "WARNING")
        self.assertIn("15.0°C", evt.message)
        self.assertNotIn("[STALE TELEMETRY]", evt.title)
        self.assertIn(self.br_id, orch._last_frost_alerts)
        self.assertEqual(orch._last_frost_alerts[self.br_id]["tier"], "WARNING")
        self.assertFalse(orch._last_frost_alerts[self.br_id]["is_stale"])

    # -------------------------------------------------------------------------
    # Scenario 3: Fresh temperature below 12°C -> ELEVATED alert dispatched
    # -------------------------------------------------------------------------
    def test_03_fresh_temperature_below_12c_elevated_alert(self):
        """3. Fresh temperature (11.5°C) triggers ELEVATED alert."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        dt = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=11.5, temp_measured_at=dt, is_online=True),
        })

        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 1)
        self.assertEqual(orch._last_frost_alerts[self.br_id]["tier"], "ELEVATED")

    # -------------------------------------------------------------------------
    # Scenario 4: Fresh temperature below 8°C -> CRITICAL alert dispatched
    # -------------------------------------------------------------------------
    def test_04_fresh_temperature_below_8c_critical_alert(self):
        """4. Fresh temperature (7.0°C) triggers CRITICAL alert."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        dt = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=7.0, temp_measured_at=dt, is_online=True),
        })

        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 1)
        evt = frost_calls[0]
        self.assertEqual(evt.severity, "CRITICAL")
        self.assertEqual(orch._last_frost_alerts[self.br_id]["tier"], "CRITICAL")

    # -------------------------------------------------------------------------
    # Scenario 5: Stale reading above 16°C -> Retains active alert, does not clear it
    # -------------------------------------------------------------------------
    def test_05_stale_reading_above_16c_retains_active_alert(self):
        """5. A stale reading >= 16°C (e.g. 18.0°C from 4h ago) does NOT clear active frost alert."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        # Step 1: 14.0°C fresh -> triggers alert
        dt1 = datetime(2026, 10, 24, 8, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt1, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.0, temp_measured_at=dt1, is_online=True),
        })
        orch.run_cycle(current_dt=dt1, override_reservations=[self.res])
        self.assertIn(self.br_id, orch._last_frost_alerts)

        # Step 2: Reading shows 18.0°C, but measurement timestamp is dt1 (4 hours old at dt2)
        dt2 = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt2, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=18.0, temp_measured_at=dt1, is_online=True),
        })
        orch.run_cycle(current_dt=dt2, override_reservations=[self.res])

        # Active frost alert MUST remain because reading is stale!
        self.assertIn(
            self.br_id,
            orch._last_frost_alerts,
            "CRITICAL SAFETY: Stale reading >= 16°C must NOT clear active frost alert!"
        )

        # Step 3: Now fresh reading arrives at 18.0°C (measured at dt3)
        dt3 = datetime(2026, 10, 24, 12, 15, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt3, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=18.0, temp_measured_at=dt3, is_online=True),
        })
        orch.run_cycle(current_dt=dt3, override_reservations=[self.res])

        # Now that a confirmed fresh measurement >= 17°C arrived, alert is cleared
        self.assertNotIn(self.br_id, orch._last_frost_alerts, "Confirmed FRESH reading clears frost alert.")

    # -------------------------------------------------------------------------
    # Scenario 6: Stale reading below 16°C -> Triggers alert labeled as [STALE TELEMETRY]
    # -------------------------------------------------------------------------
    def test_06_stale_reading_below_16c_triggers_labeled_alert(self):
        """6. Stale reading below 16°C triggers alert explicitly labeled [STALE TELEMETRY]."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        eval_dt = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        stale_meas_dt = eval_dt - timedelta(hours=2)  # 2 hours old (> 60 min threshold)

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=eval_dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.5, temp_measured_at=stale_meas_dt, is_online=True),
        })

        orch.run_cycle(current_dt=eval_dt, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 1)
        evt = frost_calls[0]
        self.assertIn("[STALE TELEMETRY]", evt.title)
        self.assertIn("STALE", evt.message)
        self.assertTrue(orch._last_frost_alerts[self.br_id]["is_stale"])

    # -------------------------------------------------------------------------
    # Scenario 7: Missing temperature timestamp -> Treated as UNAVAILABLE, labeled
    # -------------------------------------------------------------------------
    def test_07_missing_temperature_timestamp_treated_as_unavailable(self):
        """7. Missing temperature timestamp is classified UNAVAILABLE and does not pass as fresh."""
        st = ACState(
            device_id=self.br_id,
            power=False,
            current_temperature=15.0,
            temp_measured_at=None,
            is_online=True
        )
        self.assertEqual(st.get_freshness_status(), "UNAVAILABLE")

        mock_state = {
            "version": 1,
            "system_state": "OCCUPIED_DAY",
            "active_profile": "SKI_DAY",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "device_states": {
                self.br_id: {
                    "device_id": self.br_id,
                    "power": False,
                    "current_temperature": 15.0,
                    "temp_measured_at": None,
                    "online": True
                }
            }
        }
        with patch("src.dashboard.handler.StateStore.load_state", return_value=mock_state):
            dash_data = _build_status_data(self.config, DashboardConfigStore(config=self.config))
            br = dash_data["devices"]["bedroom"]
            self.assertEqual(br["temp_freshness"], "UNAVAILABLE")
            self.assertTrue(br["is_unavailable"])
            self.assertIn("Temperature reading UNAVAILABLE", br["active_warnings"])

    # -------------------------------------------------------------------------
    # Scenario 8: Offline device -> Treated as offline, not low temperature
    # -------------------------------------------------------------------------
    def test_08_offline_device_treated_as_offline_not_low_temperature(self):
        """8. Offline device with cold cached temperature does NOT trigger a false frost alert."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        dt = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=5.0, temp_measured_at=dt, is_online=False),
        })

        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 0, "Offline device must not trigger a low-temperature alert.")
        self.assertNotIn(self.br_id, orch._last_frost_alerts)

    # -------------------------------------------------------------------------
    # Scenario 9: Manually powered-OFF device -> Guest disabled, low-temp active
    # -------------------------------------------------------------------------
    def test_09_manually_powered_off_device_monitored_as_guest_disabled(self):
        """9. Manually powered-OFF device is labeled as guest-disabled, with frost monitoring active."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        dt = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=13.0, temp_measured_at=dt, is_online=True),
        })

        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 1)
        evt = frost_calls[0]
        self.assertIn("guest power-OFF override", evt.message)

    # -------------------------------------------------------------------------
    # Scenario 10: Repeated monitoring cycles -> Cooldown deduplication works
    # -------------------------------------------------------------------------
    def test_10_repeated_monitoring_cycles_cooldown_deduplication(self):
        """10. Repeated 15-minute cycles suppress redundant alerts within cooldown window."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        dt1 = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt1, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.0, temp_measured_at=dt1, is_online=True),
        })
        orch.run_cycle(current_dt=dt1, override_reservations=[self.res])

        frost_calls_1 = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls_1), 1)

        # Next tick 15 minutes later, same temperature condition
        dt2 = dt1 + timedelta(minutes=15)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt2, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.0, temp_measured_at=dt2, is_online=True),
        })
        orch.run_cycle(current_dt=dt2, override_reservations=[self.res])

        frost_calls_2 = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls_2), 1, "Alert must be deduplicated across consecutive ticks.")

    # -------------------------------------------------------------------------
    # Scenario 11: Lambda cold start -> Hydrates alert state and freshness
    # -------------------------------------------------------------------------
    def test_11_lambda_cold_start_hydrates_alert_state(self):
        """11. State persisted to store is reloaded on cold start, preserving alert tracking."""
        store = StateStore(in_memory=True)
        orch1 = AutomationOrchestrator(config=self.config, state_store=store)
        orch1.notifier.dispatch = MagicMock()

        dt1 = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch1.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt1, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.0, temp_measured_at=dt1, is_online=True),
        })
        orch1.run_cycle(current_dt=dt1, override_reservations=[self.res])
        self.assertIn(self.br_id, orch1._last_frost_alerts)

        # Simulate cold start with new instance using same store
        orch2 = AutomationOrchestrator(config=self.config, state_store=store)
        orch2.notifier.dispatch = MagicMock()
        self.assertIn(self.br_id, orch2._last_frost_alerts, "Cold start must hydrate _last_frost_alerts from store.")

        # Cycle 15 minutes later on new container
        dt2 = dt1 + timedelta(minutes=15)
        orch2.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt2, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.0, temp_measured_at=dt2, is_online=True),
        })
        orch2.run_cycle(current_dt=dt2, override_reservations=[self.res])

        frost_calls_cold = [c[0][0] for c in orch2.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls_cold), 0, "Cold start must maintain deduplication via persisted store state.")

    # -------------------------------------------------------------------------
    # Scenario 12: Notification cooldown -> Same-tier alerts suppressed within 4h
    # -------------------------------------------------------------------------
    def test_12_notification_cooldown_same_tier_suppressed_for_4_hours(self):
        """12. Alert in same tier is suppressed until 4.0 hour cooldown elapses."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        # Alert 1 at 10:00
        t0 = datetime(2026, 10, 24, 10, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=t0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=15.0, temp_measured_at=t0, is_online=True),
        })
        orch.run_cycle(current_dt=t0, override_reservations=[self.res])
        self.assertEqual(len([c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]), 1)

        # Tick at 13:50 (3h 50m elapsed) -> Suppressed
        t1 = t0 + timedelta(hours=3, minutes=50)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=t1, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.5, temp_measured_at=t1, is_online=True),
        })
        orch.run_cycle(current_dt=t1, override_reservations=[self.res])
        self.assertEqual(len([c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]), 1)

        # Tick at 14:05 (4h 05m elapsed) -> Cooldown passed -> New notification
        t2 = t0 + timedelta(hours=4, minutes=5)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=t2, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=14.5, temp_measured_at=t2, is_online=True),
        })
        orch.run_cycle(current_dt=t2, override_reservations=[self.res])
        self.assertEqual(len([c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]), 2)

    # -------------------------------------------------------------------------
    # Scenario 13: Critical severity escalation -> Escalation bypasses cooldown
    # -------------------------------------------------------------------------
    def test_13_critical_severity_escalation_bypasses_cooldown(self):
        """13. Escalating from WARNING to CRITICAL immediately bypasses the 4h cooldown."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        # Step 1: WARNING alert (15.0°C) at 12:00
        t0 = datetime(2026, 10, 24, 12, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=t0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=15.0, temp_measured_at=t0, is_online=True),
        })
        orch.run_cycle(current_dt=t0, override_reservations=[self.res])

        # Step 2: 15 minutes later temperature drops to 7.5°C (CRITICAL)
        t1 = t0 + timedelta(minutes=15)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=t1, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=7.5, temp_measured_at=t1, is_online=True),
        })
        orch.run_cycle(current_dt=t1, override_reservations=[self.res])

        frost_calls = [c[0][0] for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "FROST_WARNING"]
        self.assertEqual(len(frost_calls), 2, "Escalation to CRITICAL must immediately bypass cooldown.")
        self.assertEqual(frost_calls[1].severity, "CRITICAL")

    # -------------------------------------------------------------------------
    # Scenario 14: Dashboard freshness display -> Status, age, and warnings
    # -------------------------------------------------------------------------
    def test_14_dashboard_freshness_display_formatting(self):
        """14. Dashboard handler formats fresh, stale, and warning indicators correctly."""
        now_sofia = datetime.now(TIMEZONE_SOFIA)

        # Fresh living room (measured 10m ago) & Stale bedroom (measured 3h 20m ago)
        fresh_meas = (now_sofia - timedelta(minutes=10)).astimezone(timezone.utc).isoformat()
        stale_meas = (now_sofia - timedelta(hours=3, minutes=20)).astimezone(timezone.utc).isoformat()

        mock_state = {
            "version": 1,
            "system_state": "OCCUPIED_DAY",
            "active_profile": "SKI_DAY",
            "updated_at": (now_sofia - timedelta(minutes=5)).astimezone(timezone.utc).isoformat(),
            "device_states": {
                self.lr_id: {
                    "device_id": self.lr_id,
                    "power": True,
                    "target_temperature": 21.0,
                    "current_temperature": 21.2,
                    "temp_measured_at": fresh_meas,
                    "online": True
                },
                self.br_id: {
                    "device_id": self.br_id,
                    "power": False,
                    "target_temperature": 21.0,
                    "current_temperature": 17.2,
                    "temp_measured_at": stale_meas,
                    "online": True
                }
            },
            "guest_manual_overrides": {
                self.br_id: {"power_off": True, "reservation_uid": self.res.uid}
            },
            "last_frost_alerts": {
                self.br_id: {"tier": "WARNING", "temperature": 15.5}
            }
        }

        with patch("src.dashboard.handler.StateStore.load_state", return_value=mock_state):
            data = _build_status_data(self.config, DashboardConfigStore(config=self.config))

        lr = data["devices"]["living_room"]
        br = data["devices"]["bedroom"]

        # Living Room: FRESH
        self.assertEqual(lr["temp_freshness"], "FRESH")
        self.assertTrue(lr["is_fresh"])
        self.assertEqual(lr["temp_age_str"], "10m ago")

        # Bedroom: STALE, Guest power-off, active warnings
        self.assertEqual(br["temp_freshness"], "STALE")
        self.assertTrue(br["is_stale"])
        self.assertEqual(br["temp_age_str"], "3h 20m ago")
        self.assertTrue(br["guest_power_off_override"])
        self.assertEqual(br["physical_power_label"], "OFF")

        warnings = br["active_warnings"]
        self.assertTrue(any("STALE" in w for w in warnings))
        self.assertTrue(any("Guest Power-OFF" in w for w in warnings))
        self.assertTrue(any("Low Temperature" in w for w in warnings))

    # -------------------------------------------------------------------------
    # Scenario 15: Existing guest override behavior -> Preserved through cycles
    # -------------------------------------------------------------------------
    def test_15_guest_override_behavior_preserved_through_cycles(self):
        """15. Existing guest power-OFF override persists and prevents scheduled power-ON."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        # Initialize guest power-OFF override for bedroom
        orch.safeguard_engine.set_guest_power_off_override(self.br_id, self.res.uid)

        # Run cycle during OCCUPIED_DAY (which normally wants bedroom ON)
        dt = datetime(2026, 10, 24, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=19.0, temp_measured_at=dt, is_online=True),
        })

        result = orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        # Bedroom command must NOT be sent (power=False / skipped)
        self.assertFalse(result.commands_sent.get(self.br_id, False), "Guest power-OFF override must prevent automatic power-ON.")
        self.assertTrue(result.commands_sent.get(self.lr_id, False), "Living room should proceed normally.")
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id))


if __name__ == "__main__":
    unittest.main()
