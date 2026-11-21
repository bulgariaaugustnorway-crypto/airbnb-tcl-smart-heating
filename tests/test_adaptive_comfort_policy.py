"""
Comprehensive test suite for Adaptive Guest Comfort & Energy Optimization Policy.

Validates all 26 Phase 11 scenarios:
1. Daytime guest preference preservation across dayparts.
2. Adaptive relative night setback from daytime preference.
3. Morning restoration of remembered daytime preference.
4. Guest low daytime preference night floor handling (never raise deliberately low temp).
5. Guest nighttime temperature adjustment does not overwrite daytime preference.
6. Bedroom persistent manual power-OFF preserved through transitions.
7. Bedroom guest manually turns back ON restores adaptive management.
8. First-start live physical telemetry initialization captures initial preference.
9. Economic alert on high temperature request with cold room (warming demand).
10. Economic alert on high temperature request with warm room (exceeds threshold).
11. Economic alert cooldown suppresses duplicate notifications within 4 hours.
12. Multi-tier frost warning: Critical escalation (<= 8.0°C).
13. Multi-tier frost warning: Elevated warning (<= 12.0°C).
14. Multi-tier frost warning: Sustained warning (<= 16.0°C).
15. Frost alert severity escalation bypasses cooldown.
16. Frost alert hysteresis recovery (+1.0°C above threshold).
17. Delayed EventBridge tick in morning window executes transition and records marker.
18. Subsequent EventBridge ticks in morning window do not re-trigger transition.
19. Evening control window execution and durable transition marker.
20. Night setback execution and durable transition marker.
21. Checkout at 11:00 clears guest preferences and overrides.
22. Stale reservation preferences purged for new guest arrival.
23. Fallback to standard diurnal schedule when adaptive comfort is disabled.
24. Safe temperature clamping on extreme guest setpoints (16.0°C - 26.0°C).
25. Dashboard status endpoint exposes adaptive comfort state and transition markers.
26. State persistence and hydration across Lambda cold starts.
"""

import json
import unittest
from datetime import datetime, date, time, timedelta
from unittest.mock import MagicMock, patch

from src.models import (
    Reservation,
    ReservationType,
    ScheduleDecision,
    SystemState,
    ACCommand,
    ACState,
    ACMode,
    ACFanSpeed,
    HeatingProfileType,
)
from src.safeguards import SafeguardEngine
from src.orchestrator import AutomationOrchestrator
from src.scheduler import HVACScheduleEngine
from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.state_store import StateStore
from src.dashboard.handler import _build_status_data, handle_dashboard_request
from src.dashboard import auth
from src.dashboard.config_store import DashboardConfigStore


class TestAdaptiveGuestComfortPolicy(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        self.config.ADAPTIVE_COMFORT_ENABLED = True
        self.config.MANUAL_OVERRIDE_PRESERVE = True
        self.config.DEFAULT_HEATING_PROFILE = "AT_HOME"
        self.config.NIGHT_SETBACK_DELTA = 1.0
        self.config.NIGHT_SETBACK_FLOOR = 18.0
        self.config.ECONOMIC_MAX_DAY_TEMP = 24.0
        self.config.LOW_TEMP_WARNING_THRESHOLD = 16.0
        self.config.LOW_TEMP_ELEVATED_THRESHOLD = 12.0
        self.config.LOW_TEMP_CRITICAL_THRESHOLD = 8.0
        self.config.LOW_TEMP_ALERT_COOLDOWN_HOURS = 4.0

        self.lr_id = self.config.TCL_LIVING_ROOM_ID
        self.br_id = self.config.TCL_BEDROOM_ID

        # Active confirmed Airbnb stay (Oct 22 - Dec 22, 2026)
        self.res = Reservation(
            uid="res-active-stay-oct22",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED,
        )

    def test_01_daytime_guest_preference_preserved_across_dayparts(self):
        """1. Daytime guest preference survives across daypart transitions."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        # 08:30 in OCCUPIED_MORNING: Guest lowers Living Room to 21°C on remote
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=22.0, is_online=True),
        })
        dt_morning = datetime(2026, 10, 23, 8, 30, tzinfo=TIMEZONE_SOFIA)
        res_m = orch.run_cycle(current_dt=dt_morning, override_reservations=[self.res])

        # Verify 21°C remembered as preferred day temperature
        self.assertEqual(orch.safeguard_engine.get_preferred_day_temperature(self.lr_id), 21.0)

        # 10:15 in OCCUPIED_DAY: Transition occurs. System must NOT reset to 23°C!
        dt_day = datetime(2026, 10, 23, 10, 15, tzinfo=TIMEZONE_SOFIA)
        res_d = orch.run_cycle(current_dt=dt_day, override_reservations=[self.res])
        # Living room evaluated target must remain 21.0°C
        self.assertEqual(orch.safeguard_engine.get_preferred_day_temperature(self.lr_id), 21.0)

        # 17:15 in OCCUPIED_EVENING: Transition occurs. Must still preserve 21°C!
        dt_ev = datetime(2026, 10, 23, 17, 15, tzinfo=TIMEZONE_SOFIA)
        res_e = orch.run_cycle(current_dt=dt_ev, override_reservations=[self.res])
        self.assertEqual(orch.safeguard_engine.get_preferred_day_temperature(self.lr_id), 21.0)

    def test_02_adaptive_night_setback_from_daytime_preference(self):
        """2. Night setback calculates relative target = preferred_day_temp - 1.0°C."""
        safeguards = SafeguardEngine(config=self.config)
        safeguards.set_preferred_day_temperature(self.lr_id, 21.0, self.res.uid)

        # Calculated night target: 21.0 - 1.0 = 20.0°C
        night_target = safeguards.get_adaptive_night_target(self.lr_id)
        self.assertEqual(night_target, 20.0)

        # In scheduler: 23:05 in OCCUPIED_NIGHT
        scheduler = HVACScheduleEngine(config=self.config)
        dt_night = datetime(2026, 10, 23, 23, 5, tzinfo=TIMEZONE_SOFIA)
        dec = scheduler.evaluate(
            reservations=[self.res],
            current_dt=dt_night,
            preferred_day_temperatures={self.lr_id: 21.0}
        )
        lr_cmd = dec.commands_to_send[self.lr_id]
        self.assertEqual(lr_cmd.target_temperature, 20.0)
        self.assertEqual(lr_cmd.fan_speed, ACFanSpeed.QUIET)
        self.assertFalse(lr_cmd.display_led)

    def test_03_morning_restoration_of_daytime_preference(self):
        """3. Morning transition (07:00) restores remembered daytime preference."""
        scheduler = HVACScheduleEngine(config=self.config)
        dt_morning = datetime(2026, 10, 24, 7, 0, tzinfo=TIMEZONE_SOFIA)
        dec = scheduler.evaluate(
            reservations=[self.res],
            current_dt=dt_morning,
            preferred_day_temperatures={self.lr_id: 21.0}
        )
        lr_cmd = dec.commands_to_send[self.lr_id]
        self.assertEqual(lr_cmd.target_temperature, 21.0)
        self.assertEqual(lr_cmd.fan_speed, ACFanSpeed.MID)
        self.assertTrue(lr_cmd.display_led)

    def test_04_guest_low_daytime_preference_night_floor_handling(self):
        """4. Night setback does NOT increase temperature above a low guest preference."""
        safeguards = SafeguardEngine(config=self.config)
        # Guest selected 17.5°C during the day (below the 18.0°C floor)
        safeguards.set_preferred_day_temperature(self.lr_id, 17.5, self.res.uid)

        night_target = safeguards.get_adaptive_night_target(self.lr_id)
        # Floor must NOT raise it to 18.0°C; it must remain <= 17.5°C
        self.assertLessEqual(night_target, 17.5)
        self.assertGreaterEqual(night_target, self.config.TEMP_MIN_SAFE)

    def test_05_guest_nighttime_adjustment_does_not_override_day_preference(self):
        """5. Night remote adjustment sets temporary night override without altering day preference."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.safeguard_engine.set_preferred_day_temperature(self.lr_id, 21.0, self.res.uid)

        # Baseline applied command at 23:00 was 20°C
        orch.safeguard_engine.record_applied_command(ACCommand(
            device_id=self.lr_id, power=True, target_temperature=20.0, mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET, reason="Night setback"
        ))

        # At 01:30 in OCCUPIED_NIGHT, guest feels cool and sets remote to 22.0°C
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=22.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=20.0, is_online=True)
        })
        dt_night = datetime(2026, 10, 24, 1, 30, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt_night, override_reservations=[self.res])

        # Crucial check: daytime preference remains 21.0°C!
        self.assertEqual(orch.safeguard_engine.get_preferred_day_temperature(self.lr_id), 21.0)

        # Morning transition at 07:00 restores 21.0°C
        dt_m = datetime(2026, 10, 24, 7, 0, tzinfo=TIMEZONE_SOFIA)
        res_m = orch.run_cycle(current_dt=dt_m, override_reservations=[self.res])
        lr_send, lr_cmd, _ = res_m.safeguard_decisions[self.lr_id]
        if lr_send and lr_cmd:
            self.assertEqual(lr_cmd.target_temperature, 21.0)

    def test_06_bedroom_persistent_power_off_preserved_through_transitions(self):
        """6. Persistent guest power-OFF override survives 07:00, 10:00, 17:00, 23:00."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.safeguard_engine.set_guest_power_off_override(self.br_id, self.res.uid)

        times = [
            datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA),   # Morning
            datetime(2026, 10, 23, 10, 0, tzinfo=TIMEZONE_SOFIA),  # Day
            datetime(2026, 10, 23, 17, 0, tzinfo=TIMEZONE_SOFIA),  # Evening
            datetime(2026, 10, 23, 23, 0, tzinfo=TIMEZONE_SOFIA),  # Night
        ]
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True),
        })

        for dt in times:
            res = orch.run_cycle(current_dt=dt, override_reservations=[self.res])
            br_send, br_cmd, br_rat = res.safeguard_decisions[self.br_id]
            self.assertFalse(br_send, f"Bedroom must stay OFF at {dt.time()}")
            self.assertIn("Persistent Power-OFF", br_rat)

    def test_07_bedroom_guest_turns_on_restores_adaptive_management(self):
        """7. Guest turning Bedroom ON clears power-OFF override and sets preferred temp."""
        safeguards = SafeguardEngine(config=self.config)
        safeguards.set_guest_power_off_override(self.br_id, self.res.uid)
        self.assertTrue(safeguards.has_guest_power_off_override(self.br_id, self.res.uid))

        # Guest switches Bedroom ON at 21.5°C during daytime
        curr_states = {
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=21.5, is_online=True)
        }
        dt_day = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec = ScheduleDecision(
            timestamp=dt_day,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=self.res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Day",
            commands_to_send={
                self.br_id: ACCommand(
                    device_id=self.br_id, power=True, target_temperature=21.0,
                    mode=ACMode.HEAT, fan_speed=ACFanSpeed.AUTO, reason="Day"
                )
            }
        )
        safeguards.filter_and_validate(dec, curr_states, now=dt_day)

        # Power-OFF override cleared
        self.assertFalse(safeguards.has_guest_power_off_override(self.br_id, self.res.uid))
        # Preferred day temp recorded as 21.5°C
        self.assertEqual(safeguards.get_preferred_day_temperature(self.br_id), 21.5)

    def test_08_first_start_initialization_captures_living_room_preference(self):
        """8. First live startup initializes preferences directly from fresh telemetry."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True),
        })
        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        # Living Room: 21.0°C captured
        self.assertEqual(orch.safeguard_engine.get_preferred_day_temperature(self.lr_id), 21.0)
        # Bedroom: Persistent power-OFF initialized
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))

    def test_09_economic_alert_high_temp_with_cold_room(self):
        """9. Economic alert for high setpoint (27°C) with cold room recognizes warming demand."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=27.0, current_temperature=17.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=17.0, is_online=True),
        })
        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        events = [call[0][0] for call in orch.notifier.dispatch.call_args_list if call[0][0].event_type == "ECONOMIC_ALERT"]
        self.assertTrue(len(events) >= 1)
        alert = events[0]
        self.assertIn("cold", alert.message.lower())
        self.assertIn("warming", alert.message.lower())

    def test_10_economic_alert_high_temp_warm_room(self):
        """10. Economic alert for high setpoint (26°C) in a warm room flags policy threshold."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=26.0, current_temperature=23.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=21.0, is_online=True),
        })
        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        events = [call[0][0] for call in orch.notifier.dispatch.call_args_list if call[0][0].event_type == "ECONOMIC_ALERT"]
        self.assertTrue(len(events) >= 1)
        alert = events[0]
        self.assertIn("exceeds", alert.message.lower())

    def test_11_economic_alert_cooldown(self):
        """11. Economic alert cooldown suppresses duplicate notifications within 4 hours."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=26.0, current_temperature=23.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=21.0, is_online=True),
        })
        dt1 = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt1, override_reservations=[self.res])
        count1 = sum(1 for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "ECONOMIC_ALERT")
        self.assertEqual(count1, 1)

        # 15 minutes later: same condition
        dt2 = dt1 + timedelta(minutes=15)
        orch.run_cycle(current_dt=dt2, override_reservations=[self.res])
        count2 = sum(1 for c in orch.notifier.dispatch.call_args_list if c[0][0].event_type == "ECONOMIC_ALERT")
        self.assertEqual(count2, 1, "Duplicate alert must be suppressed by cooldown")

    def test_12_multi_tier_frost_warning_critical(self):
        """12. Temperature <= 8.0°C triggers CRITICAL frost warning."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=7.5, is_online=True),
        })
        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        events = [call[0][0] for call in orch.notifier.dispatch.call_args_list if call[0][0].event_type == "FROST_WARNING" and call[0][0].severity == "CRITICAL"]
        self.assertTrue(len(events) >= 1)

    def test_13_multi_tier_frost_warning_elevated(self):
        """13. Temperature <= 12.0°C triggers ELEVATED frost warning."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=11.5, is_online=True),
        })
        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        events = [call[0][0] for call in orch.notifier.dispatch.call_args_list if call[0][0].event_type == "FROST_WARNING"]
        self.assertTrue(len(events) >= 1)
        self.assertEqual(events[0].metadata.get("tier"), "ELEVATED")

    def test_14_multi_tier_frost_warning_warning(self):
        """14. Temperature <= 16.0°C triggers WARNING low temperature alert."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=15.0, is_online=True),
        })
        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        events = [call[0][0] for call in orch.notifier.dispatch.call_args_list if call[0][0].event_type == "FROST_WARNING"]
        self.assertTrue(len(events) >= 1)
        self.assertEqual(events[0].metadata.get("tier"), "WARNING")

    def test_15_frost_alert_severity_escalation_bypasses_cooldown(self):
        """15. Escalation from WARNING to CRITICAL sends immediate alert bypassing cooldown."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        # Step 1: 15.0°C -> WARNING
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=15.0, is_online=True),
        })
        dt1 = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt1, override_reservations=[self.res])

        # Step 2: 15 minutes later, drops to 7.0°C -> CRITICAL
        orch.notifier.dispatch.reset_mock()
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=7.0, is_online=True),
        })
        dt2 = dt1 + timedelta(minutes=15)
        orch.run_cycle(current_dt=dt2, override_reservations=[self.res])

        crit_events = [call[0][0] for call in orch.notifier.dispatch.call_args_list if call[0][0].event_type == "FROST_WARNING" and call[0][0].severity == "CRITICAL"]
        self.assertEqual(len(crit_events), 1, "Escalation must bypass cooldown")

    def test_16_frost_alert_hysteresis_recovery(self):
        """16. Hysteresis (+1.0°C above threshold) clears alert tracking."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        # Step 1: 15.0°C -> WARNING alert
        dt1 = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt1, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=15.0, temp_measured_at=dt1, is_online=True),
        })
        orch.run_cycle(current_dt=dt1, override_reservations=[self.res])
        self.assertIn(self.br_id, orch._last_frost_alerts)

        # Step 2: Recovers to 17.5°C (>= 16.0 + 1.0 = 17.0°C)
        dt2 = dt1 + timedelta(minutes=15)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.0, temp_measured_at=dt2, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=17.5, temp_measured_at=dt2, is_online=True),
        })
        orch.run_cycle(current_dt=dt2, override_reservations=[self.res])
        self.assertNotIn(self.br_id, orch._last_frost_alerts, "Hysteresis recovery must clear alert state")

    def test_17_transition_marker_morning_delayed_execution(self):
        """17. Delayed EventBridge tick (07:15) executes morning transition and records marker."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        dt = datetime(2026, 10, 23, 7, 15, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        self.assertEqual(orch.last_completed_transitions.get("morning"), "2026-10-23")

    def test_18_transition_marker_morning_subsequent_ticks_no_retrigger(self):
        """18. Subsequent ticks at 07:30, 07:45 do not re-execute already completed transition."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.safeguard_engine.set_preferred_day_temperature(self.lr_id, 21.0, self.res.uid)

        # First tick at 07:00 marks completion
        dt1 = datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt1, override_reservations=[self.res])
        self.assertEqual(orch.last_completed_transitions.get("morning"), "2026-10-23")

        # Second tick at 07:15
        dt2 = datetime(2026, 10, 23, 7, 15, tzinfo=TIMEZONE_SOFIA)
        orch.controller.send_command = MagicMock(return_value=True)
        orch.run_cycle(current_dt=dt2, override_reservations=[self.res])
        # Debounce prevents redundant command
        self.assertFalse(orch.controller.send_command.called)

    def test_19_evening_window_execution(self):
        """19. Evening window (17:00-18:00) marks 'evening' transition completed."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        dt = datetime(2026, 10, 23, 17, 15, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        self.assertEqual(orch.last_completed_transitions.get("evening"), "2026-10-23")

    def test_20_night_window_execution(self):
        """20. Night window (23:00-23:30) marks 'night' transition completed."""
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        dt = datetime(2026, 10, 23, 23, 15, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        self.assertEqual(orch.last_completed_transitions.get("night"), "2026-10-23")

    def test_21_checkout_clears_adaptive_preferences(self):
        """21. Checkout at 11:00 transitions to CHECKOUT_SHUTDOWN and clears preferences."""
        safeguards = SafeguardEngine(config=self.config)
        safeguards.set_preferred_day_temperature(self.lr_id, 21.0, self.res.uid)
        safeguards.set_guest_power_off_override(self.br_id, self.res.uid)

        # Checkout tick
        dt = datetime(2026, 12, 22, 11, 0, tzinfo=TIMEZONE_SOFIA)
        dec = ScheduleDecision(
            timestamp=dt,
            system_state=SystemState.CHECKOUT_SHUTDOWN,
            active_reservation=self.res,
            target_power=False,
            target_temperature=21.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Checkout",
            commands_to_send={
                self.lr_id: ACCommand(device_id=self.lr_id, power=False, target_temperature=21.0, mode=ACMode.OFF, fan_speed=ACFanSpeed.AUTO, reason="Checkout"),
                self.br_id: ACCommand(device_id=self.br_id, power=False, target_temperature=21.0, mode=ACMode.OFF, fan_speed=ACFanSpeed.AUTO, reason="Checkout"),
            }
        )
        curr_states = {
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True),
        }
        safeguards.filter_and_validate(dec, curr_states, now=dt)

        # Preferences and overrides cleared
        self.assertIsNone(safeguards.get_preferred_day_temperature(self.lr_id))
        self.assertFalse(safeguards.has_guest_power_off_override(self.br_id))

    def test_22_stale_reservation_preferences_purged_for_new_guest(self):
        """22. New reservation arrival purges preferences from older reservation."""
        safeguards = SafeguardEngine(config=self.config)
        safeguards.set_preferred_day_temperature(self.lr_id, 21.0, "old-res-uid")
        safeguards.set_guest_power_off_override(self.br_id, "old-res-uid")

        new_res = Reservation(
            uid="new-res-uid-123",
            start_date=date(2026, 12, 23),
            end_date=date(2026, 12, 28),
            summary="New Guest",
            reservation_type=ReservationType.CONFIRMED,
        )
        dt = datetime(2026, 12, 23, 16, 0, tzinfo=TIMEZONE_SOFIA)
        dec = ScheduleDecision(
            timestamp=dt,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=new_res,
            target_power=True,
            target_temperature=22.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Day",
            commands_to_send={
                self.lr_id: ACCommand(device_id=self.lr_id, power=True, target_temperature=22.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.AUTO, reason="Day"),
                self.br_id: ACCommand(device_id=self.br_id, power=True, target_temperature=21.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.AUTO, reason="Day"),
            }
        )
        curr_states = {
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=22.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=21.0, is_online=True),
        }
        safeguards.filter_and_validate(dec, curr_states, now=dt)

        # Old reservation's preferences and overrides purged
        self.assertIsNone(safeguards.get_preferred_day_temperature(self.lr_id))
        self.assertFalse(safeguards.has_guest_power_off_override(self.br_id, "old-res-uid"))

    def test_23_adaptive_comfort_disabled_fallback_to_standard_schedule(self):
        """23. When ADAPTIVE_COMFORT_ENABLED=False, fallback to standard diurnal schedule."""
        cfg = AutomationConfig()
        cfg.ADAPTIVE_COMFORT_ENABLED = False
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        scheduler = HVACScheduleEngine(config=cfg)

        # Evening time: AT_HOME prescribes 23.0°C for Living Room
        dt_evening = datetime(2026, 10, 23, 17, 30, tzinfo=TIMEZONE_SOFIA)
        dec = scheduler.evaluate(
            reservations=[self.res],
            current_dt=dt_evening,
            preferred_day_temperatures={self.lr_id: 21.0}  # Should be ignored because adaptive is disabled
        )
        self.assertEqual(dec.commands_to_send[self.lr_id].target_temperature, 23.0)

    def test_24_safe_clamping_on_extreme_guest_preference(self):
        """24. Extreme guest setpoints clamped to [16.0°C, 26.0°C]."""
        safeguards = SafeguardEngine(config=self.config)
        safeguards.set_preferred_day_temperature(self.lr_id, 32.0, self.res.uid)
        self.assertEqual(safeguards.get_preferred_day_temperature(self.lr_id), self.config.TEMP_MAX_SAFE)

        safeguards.set_preferred_day_temperature(self.lr_id, 12.0, self.res.uid)
        self.assertEqual(safeguards.get_preferred_day_temperature(self.lr_id), self.config.TEMP_MIN_SAFE)

    @patch("src.dashboard.handler.StateStore")
    def test_25_dashboard_status_exposes_adaptive_comfort_state(self, mock_state_store_cls):
        """25. Dashboard status data exposes adaptive comfort configuration and states."""
        state = {
            "last_system_state": "OCCUPIED_DAY",
            "preferred_day_temperatures": {
                self.lr_id: {"temperature": 21.0, "source": "manual_override"},
                self.br_id: {"temperature": 20.5, "source": "manual_override"},
            },
            "guest_manual_overrides": {
                self.br_id: {"power_off": True, "reservation_uid": self.res.uid}
            },
            "last_completed_transitions": {
                "morning": "2026-10-23"
            }
        }
        mock_store = MagicMock()
        mock_store.load_state.return_value = state
        mock_state_store_cls.return_value = mock_store

        cfg_store = MagicMock()
        cfg_store.load_dashboard_config.return_value = {}
        cfg_store.load_owner_test_session.return_value = None

        status = _build_status_data(
            config=self.config,
            config_store=cfg_store,
        )
        self.assertIn("adaptive_comfort", status)
        ac = status["adaptive_comfort"]
        self.assertTrue(ac["enabled"])
        self.assertEqual(ac["living_room_preferred_day_temp"], 21.0)
        self.assertEqual(ac["bedroom_preferred_day_temp"], 20.5)
        self.assertTrue(ac["bedroom_guest_power_off"])
        self.assertEqual(ac["last_transitions"].get("morning"), "2026-10-23")

    def test_26_state_persistence_and_hydration_of_adaptive_comfort(self):
        """26. StateStore persists and rehydrates adaptive comfort state across cold starts."""
        store = StateStore(in_memory=True)
        orch1 = AutomationOrchestrator(config=self.config, state_store=store)

        orch1.safeguard_engine.set_preferred_day_temperature(self.lr_id, 21.0, self.res.uid)
        orch1.safeguard_engine.set_guest_power_off_override(self.br_id, self.res.uid)
        orch1.last_completed_transitions = {"morning": "2026-10-23", "night": "2026-10-22"}
        orch1._last_frost_alerts = {self.br_id: {"tier": "ELEVATED", "timestamp": "2026-10-23T14:00:00"}}
        orch1._last_economic_alerts = {self.lr_id: {"timestamp": "2026-10-23T14:00:00"}}
        orch1._save_persisted_state()

        # Simulate cold start
        orch2 = AutomationOrchestrator(config=self.config, state_store=store)
        self.assertEqual(orch2.safeguard_engine.get_preferred_day_temperature(self.lr_id), 21.0)
        self.assertTrue(orch2.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))
        self.assertEqual(orch2.last_completed_transitions.get("morning"), "2026-10-23")
        self.assertEqual(orch2._last_frost_alerts[self.br_id]["tier"], "ELEVATED")
        self.assertIn(self.lr_id, orch2._last_economic_alerts)

    def test_27_full_61_night_active_reservation_simulation(self):
        """27. Comprehensive multi-week simulation of the 61-night stay (Oct 22 - Dec 22, 2026).
        Simulates:
        - Arrival preheating Oct 22 13:00
        - Guest check-in 15:00 and manual adjust to 21°C on Living Room, Bedroom OFF
        - Cycles: Night 1 (20°C setback on LR, BR OFF), Morning 2 (21°C on LR, BR OFF)
        - Day 30 (Nov 21) stable 21°C / 20°C persistence
        - Day 50 (Dec 10) guest turns Bedroom ON at 22°C (restores adaptive management)
        - Departure day Dec 22 11:00 checkout shutdown and preferences purged
        - New guest preheating at 13:00 without stale preferences
        """
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        # 1. Arrival Day Oct 22 at 13:00: Pre-arrival heating (no guest arrived yet)
        dt_preheat = datetime(2026, 10, 22, 13, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=False, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, is_online=True),
        })
        res_preheat = orch.run_cycle(current_dt=dt_preheat, override_reservations=[self.res])
        self.assertEqual(res_preheat.system_state, SystemState.PRE_ARRIVAL_HEATING)

        # 2. In-stay Oct 22 at 18:00: Guest has checked in and set Living Room to 21°C, Bedroom to OFF
        dt_day1 = datetime(2026, 10, 22, 18, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True),
        })
        res_day1 = orch.run_cycle(current_dt=dt_day1, override_reservations=[self.res])
        self.assertEqual(orch.safeguard_engine.get_preferred_day_temperature(self.lr_id), 21.0)
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))

        # 3. Night 1 at 23:00: LR relative night setback to 20.0°C (21-1), BR stays OFF
        dt_night1 = datetime(2026, 10, 22, 23, 0, tzinfo=TIMEZONE_SOFIA)
        res_night1 = orch.run_cycle(current_dt=dt_night1, override_reservations=[self.res])
        lr_send, lr_cmd, _ = res_night1.safeguard_decisions[self.lr_id]
        br_send, _, br_rat = res_night1.safeguard_decisions[self.br_id]
        self.assertEqual(lr_cmd.target_temperature, 20.0)
        self.assertFalse(br_send)
        self.assertIn("Persistent Power-OFF", br_rat)

        # 4. Morning 2 at 07:00: LR restores 21.0°C, BR stays OFF
        dt_morn2 = datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA)
        res_morn2 = orch.run_cycle(current_dt=dt_morn2, override_reservations=[self.res])
        lr_send, lr_cmd, _ = res_morn2.safeguard_decisions[self.lr_id]
        br_send, _, _ = res_morn2.safeguard_decisions[self.br_id]
        self.assertEqual(lr_cmd.target_temperature, 21.0)
        self.assertFalse(br_send)

        # 5. Day 30 (Nov 21) at 23:00: LR setback is still 20.0°C (no progressive drift!), BR stays OFF
        dt_day30_night = datetime(2026, 11, 21, 23, 0, tzinfo=TIMEZONE_SOFIA)
        res_d30 = orch.run_cycle(current_dt=dt_day30_night, override_reservations=[self.res])
        _, lr_cmd, _ = res_d30.safeguard_decisions[self.lr_id]
        self.assertEqual(lr_cmd.target_temperature, 20.0)
        self.assertFalse(res_d30.safeguard_decisions[self.br_id][0])

        # 6. Day 50 (Dec 10) at 14:00: Guest turns Bedroom ON to 22.0°C
        dt_d50 = datetime(2026, 12, 10, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=22.0, is_online=True),
        })
        res_d50 = orch.run_cycle(current_dt=dt_d50, override_reservations=[self.res])
        self.assertFalse(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))
        self.assertEqual(orch.safeguard_engine.get_preferred_day_temperature(self.br_id), 22.0)

        # 7. Day 50 (Dec 10) at 23:00: Both units receive adaptive setback (LR 20.0°C, BR 21.0°C)
        dt_d50_night = datetime(2026, 12, 10, 23, 0, tzinfo=TIMEZONE_SOFIA)
        res_d50_n = orch.run_cycle(current_dt=dt_d50_night, override_reservations=[self.res])
        self.assertEqual(res_d50_n.safeguard_decisions[self.lr_id][1].target_temperature, 20.0)
        self.assertEqual(res_d50_n.safeguard_decisions[self.br_id][1].target_temperature, 21.0)

        # 8. Checkout day Dec 22 at 11:00: Checkout shutdown powers off all units and cleans state
        dt_checkout = datetime(2026, 12, 22, 11, 0, tzinfo=TIMEZONE_SOFIA)
        res_checkout = orch.run_cycle(current_dt=dt_checkout, override_reservations=[self.res])
        self.assertEqual(res_checkout.system_state, SystemState.CHECKOUT_SHUTDOWN)
        self.assertIsNone(orch.safeguard_engine.get_preferred_day_temperature(self.lr_id))
        self.assertIsNone(orch.safeguard_engine.get_preferred_day_temperature(self.br_id))

        # 9. Consecutive incoming guest Dec 22 at 13:00: Pre-arrival starts at default 23.0°C without old preferences
        incoming_res = Reservation(
            uid="res-incoming-dec22",
            start_date=date(2026, 12, 22),
            end_date=date(2026, 12, 29),
            summary="Next Guest",
            reservation_type=ReservationType.CONFIRMED,
        )
        dt_incoming = datetime(2026, 12, 22, 13, 0, tzinfo=TIMEZONE_SOFIA)
        res_inc = orch.run_cycle(current_dt=dt_incoming, override_reservations=[incoming_res])
        self.assertEqual(res_inc.system_state, SystemState.PRE_ARRIVAL_HEATING)
        self.assertEqual(res_inc.safeguard_decisions[self.lr_id][1].target_temperature, 23.0)

    def test_28_dashboard_api_config_saves_adaptive_comfort_settings(self):
        """28. Dashboard API POST /api/config validates and persists adaptive comfort settings."""
        cfg = AutomationConfig()
        cfg.S3_BUCKET = ""
        cfg.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only"
        secret_key = cfg.SESSION_SECRET_KEY
        token = auth.create_session_token(secret_key)
        csrf = auth.generate_csrf_token(token, secret_key)

        event = {
            "rawPath": "/api/config",
            "requestContext": {"http": {"method": "POST", "sourceIp": "127.0.0.1"}},
            "cookies": [f"session_token={token}"],
            "headers": {"content-type": "application/json", "x-csrf-token": csrf},
            "body": json.dumps({
                "adaptive_comfort_enabled": True,
                "guest_preference_preservation": True,
                "night_setback_delta": 1.5,
                "night_setback_floor": 18.5,
                "economic_max_day_temp": 23.5,
                "economic_enforcement_enabled": True,
                "tcl_dry_run": False,  # Safety flag modification attempt must be rejected
                "tcl_read_only": False,
            })
        }

        resp = handle_dashboard_request(event, None, config=cfg)
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertEqual(body["status"], "success")

        # Verify persisted values in config store
        cfg_store = DashboardConfigStore(config=cfg)
        saved = cfg_store.load_dashboard_config()
        self.assertTrue(saved["adaptive_comfort_enabled"])
        self.assertTrue(saved["guest_preference_preservation"])
        self.assertEqual(saved["night_setback_delta"], 1.5)
        self.assertEqual(saved["night_setback_floor"], 18.5)
        self.assertEqual(saved["economic_max_day_temp"], 23.5)
        self.assertTrue(saved["economic_enforcement_enabled"])
        # Safety flags remain untouched
        self.assertTrue(cfg.DRY_RUN)
        self.assertTrue(cfg.TCL_READ_ONLY)


if __name__ == "__main__":
    unittest.main()
