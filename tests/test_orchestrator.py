"""End-to-end integration tests for the AutomationOrchestrator."""

import unittest
from datetime import datetime, date
from pathlib import Path

from src.orchestrator import AutomationOrchestrator
from src.device_controller import MockACController
from src.notifier import NotificationDispatcher
from src.config import AutomationConfig, SAMPLE_CALENDARS_DIR, TIMEZONE_SOFIA
from src.models import SystemState
from src.state_store import StateStore
from datetime import timedelta


class TestAutomationOrchestrator(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        self.config.DRY_RUN = False  # Enable mock state mutations for assertions
        self.controller = MockACController(device_ids=self.config.DEVICE_IDS)
        self.notifier = NotificationDispatcher()
        self.state_store = StateStore(in_memory=True)
        self.orchestrator = AutomationOrchestrator(
            config=self.config,
            controller=self.controller,
            notifier=self.notifier,
            state_store=self.state_store
        )

    def test_full_cycle_unoccupied_to_pre_arrival(self):
        cal_file = SAMPLE_CALENDARS_DIR / "normal_stay.ics"

        # 1. T = Oct 20, 10:00 (Arrival morning, before 13:00 preheat)
        t_morning = datetime(2026, 10, 20, 10, 0, tzinfo=TIMEZONE_SOFIA)
        res_morning = self.orchestrator.run_cycle(current_dt=t_morning, calendar_file=cal_file)

        self.assertEqual(res_morning.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)

        # 2. T = Oct 20, 13:30 (Pre-arrival heating window)
        t_preheat = datetime(2026, 10, 20, 13, 30, tzinfo=TIMEZONE_SOFIA)
        res_preheat = self.orchestrator.run_cycle(current_dt=t_preheat, calendar_file=cal_file)

        self.assertEqual(res_preheat.system_state, SystemState.PRE_ARRIVAL_HEATING)
        living_room = self.controller.get_state(self.config.TCL_LIVING_ROOM_ID)
        bedroom = self.controller.get_state(self.config.TCL_BEDROOM_ID)

        self.assertTrue(living_room.power)
        self.assertEqual(living_room.target_temperature, 23.0)
        self.assertTrue(bedroom.power)
        self.assertEqual(bedroom.target_temperature, 23.0)

    def test_cancellation_triggers_shutdown_and_notification(self):
        # Disable HEAT_DURING_BLOCKED_DATES since cancellation_v2.ics contains a blocked date
        self.orchestrator.config.HEAT_DURING_BLOCKED_DATES = False
        # Step 1: Run with v1 (reservation active, inside stay Oct 20-23 at 14:00)
        cal_v1 = SAMPLE_CALENDARS_DIR / "cancellation_v1.ics"
        t_active = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        self.controller.states[self.config.TCL_LIVING_ROOM_ID].power = True
        self.orchestrator.safeguard_engine.has_live_command_history = True
        res1 = self.orchestrator.run_cycle(current_dt=t_active, calendar_file=cal_v1)
        self.assertEqual(res1.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)


        # Step 2: Next cycle with v2 (in-progress booking disappears from Airbnb)
        # Phase 4 safeguard: Activates 4h Grace Period instead of immediate shutdown!
        cal_v2 = SAMPLE_CALENDARS_DIR / "cancellation_v2.ics"
        res2 = self.orchestrator.run_cycle(current_dt=t_active, calendar_file=cal_v2)

        self.assertEqual(res2.system_state, SystemState.OCCUPIED_DAY)
        self.assertIn("synthetic-a6ed1b4af6@example.invalid", res2.in_grace_period_uids)
        self.assertTrue(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)

        # Verify grace period alert dispatched
        grace_events = [
            e for e in self.notifier.events_history if e.event_type == "CALENDAR_GRACE_PERIOD"
        ]
        self.assertTrue(len(grace_events) >= 1)

        # Step 3: Advance clock past 4-hour grace period (14:00 + 5h = 19:00)
        t_expired = t_active + timedelta(hours=5)
        res3 = self.orchestrator.run_cycle(current_dt=t_expired, calendar_file=cal_v2)

        self.assertEqual(res3.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)

        # Verify final cancellation event dispatched after grace period expiry
        cancellation_events = [
            e for e in self.notifier.events_history if e.event_type == "RESERVATION_CANCELLED"
        ]
        self.assertTrue(len(cancellation_events) >= 1)

    def test_blocked_dates_keeps_system_idle(self):
        # When HEAT_DURING_BLOCKED_DATES is False, system stays idle
        self.orchestrator.config.HEAT_DURING_BLOCKED_DATES = False
        cal_blocked = SAMPLE_CALENDARS_DIR / "blocked_dates.ics"
        t_blocked = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        res = self.orchestrator.run_cycle(current_dt=t_blocked, calendar_file=cal_blocked)

        self.assertEqual(res.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)
        self.assertFalse(self.controller.get_state(self.config.TCL_BEDROOM_ID).power)

    def test_blocked_dates_heats_when_enabled(self):
        # By default, HEAT_DURING_BLOCKED_DATES is True: blocked dates trigger comfort heating
        self.orchestrator.config.HEAT_DURING_BLOCKED_DATES = True
        cal_blocked = SAMPLE_CALENDARS_DIR / "blocked_dates.ics"
        t_blocked = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        res = self.orchestrator.run_cycle(current_dt=t_blocked, calendar_file=cal_blocked)

        self.assertEqual(res.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)
        self.assertEqual(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).target_temperature, 21.0)
        self.assertTrue(self.controller.get_state(self.config.TCL_BEDROOM_ID).power)
        self.assertEqual(self.controller.get_state(self.config.TCL_BEDROOM_ID).target_temperature, 21.0)


if __name__ == "__main__":
    unittest.main()
