"""Unit and integration tests for blocked dates occupancy, merging, precedence, and safety."""

import unittest
from datetime import datetime, date, time, timedelta
from typing import List

from src.models import (
    Reservation,
    ReservationType,
    SystemState,
    ACMode,
    ACFanSpeed,
    ACState,
    ScheduleDecision,
)
from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.calendar_parser import AirbnbCalendarParser
from src.scheduler import HVACScheduleEngine
from src.safeguards import SafeguardEngine
from src.orchestrator import AutomationOrchestrator
from src.device_controller import MockACController
from src.notifier import NotificationDispatcher
from src.state_store import StateStore


class TestBlockedDatesOccupancy(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        self.config.DRY_RUN = False
        self.config.HEAT_DURING_BLOCKED_DATES = True
        self.engine = HVACScheduleEngine(config=self.config)

    def test_single_blocked_stay_lifecycle(self):
        """Test full lifecycle of a blocked stay (arrival, preheat, mid-stay, departure)."""
        blocked_res = Reservation(
            uid="block-oct20-23",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Airbnb (Not available)",
            reservation_type=ReservationType.BLOCKED
        )
        reservations = [blocked_res]

        # 1. Arrival Day Morning (10:00) -> IDLE_UNOCCUPIED
        t1 = datetime(2026, 10, 20, 10, 0, tzinfo=TIMEZONE_SOFIA)
        d1 = self.engine.evaluate(reservations, t1)
        self.assertEqual(d1.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(d1.target_power)

        # 2. Arrival Day Preheat (13:30) -> PRE_ARRIVAL_HEATING (23°C)
        t2 = datetime(2026, 10, 20, 13, 30, tzinfo=TIMEZONE_SOFIA)
        d2 = self.engine.evaluate(reservations, t2)
        self.assertEqual(d2.system_state, SystemState.PRE_ARRIVAL_HEATING)
        self.assertTrue(d2.target_power)
        self.assertEqual(d2.target_temperature, 23.0)
        self.assertEqual(d2.active_reservation.reservation_type, ReservationType.BLOCKED)
        self.assertIn("Blocked date stay", d2.decision_reason)

        # 3. Arrival Day Afternoon (16:00) -> OCCUPIED_DAY (21°C)
        t3 = datetime(2026, 10, 20, 16, 0, tzinfo=TIMEZONE_SOFIA)
        d3 = self.engine.evaluate(reservations, t3)
        self.assertEqual(d3.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(d3.target_power)
        self.assertEqual(d3.target_temperature, 21.0)

        # 4. Mid-stay Day Diurnal Windows (Oct 21)
        # Morning (08:00) -> 23°C, Mid fan
        t4_morning = datetime(2026, 10, 21, 8, 0, tzinfo=TIMEZONE_SOFIA)
        d4_morning = self.engine.evaluate(reservations, t4_morning)
        self.assertEqual(d4_morning.system_state, SystemState.OCCUPIED_MORNING)
        self.assertEqual(d4_morning.target_temperature, 23.0)
        self.assertEqual(d4_morning.target_fan_speed, ACFanSpeed.MID)

        # Day (14:00) -> 21°C, Auto fan
        t4_day = datetime(2026, 10, 21, 14, 0, tzinfo=TIMEZONE_SOFIA)
        d4_day = self.engine.evaluate(reservations, t4_day)
        self.assertEqual(d4_day.system_state, SystemState.OCCUPIED_DAY)
        self.assertEqual(d4_day.target_temperature, 21.0)

        # Evening (19:00) -> 23°C, Mid fan
        t4_eve = datetime(2026, 10, 21, 19, 0, tzinfo=TIMEZONE_SOFIA)
        d4_eve = self.engine.evaluate(reservations, t4_eve)
        self.assertEqual(d4_eve.system_state, SystemState.OCCUPIED_EVENING)
        self.assertEqual(d4_eve.target_temperature, 23.0)

        # Night (01:00) -> 20°C, Quiet fan
        t4_night = datetime(2026, 10, 22, 1, 0, tzinfo=TIMEZONE_SOFIA)
        d4_night = self.engine.evaluate(reservations, t4_night)
        self.assertEqual(d4_night.system_state, SystemState.OCCUPIED_NIGHT)
        self.assertEqual(d4_night.target_temperature, 20.0)
        self.assertEqual(d4_night.target_fan_speed, ACFanSpeed.QUIET)

        # 5. Departure Day Morning (10:00) -> OCCUPIED_DAY before 11:00
        t5_dept_morning = datetime(2026, 10, 23, 10, 0, tzinfo=TIMEZONE_SOFIA)
        d5_dept_morning = self.engine.evaluate(reservations, t5_dept_morning)
        self.assertEqual(d5_dept_morning.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(d5_dept_morning.target_power)

        # 6. Departure Day at 11:05 -> CHECKOUT_SHUTDOWN (Power OFF)
        t5_checkout = datetime(2026, 10, 23, 11, 5, tzinfo=TIMEZONE_SOFIA)
        d5_checkout = self.engine.evaluate(reservations, t5_checkout)
        self.assertEqual(d5_checkout.system_state, SystemState.CHECKOUT_SHUTDOWN)
        self.assertFalse(d5_checkout.target_power)
        self.assertEqual(d5_checkout.target_mode, ACMode.OFF)

    def test_merge_adjacent_blocked_dates(self):
        """Test merging consecutive 1-day blocks into continuous stay."""
        raw_ics = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20261016\n"
            "DTEND;VALUE=DATE:20261017\n"
            "UID:block-day-1\n"
            "SUMMARY:Airbnb (Not available)\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20261017\n"
            "DTEND;VALUE=DATE:20261018\n"
            "UID:block-day-2\n"
            "SUMMARY:Airbnb (Not available)\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20261018\n"
            "DTEND;VALUE=DATE:20261019\n"
            "UID:block-day-3\n"
            "SUMMARY:Airbnb (Not available)\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        parser = AirbnbCalendarParser()
        merged = parser.parse_ics_content(raw_ics)

        self.assertEqual(len(merged), 1, "Three contiguous 1-day blocks must merge into 1 continuous stay")
        res = merged[0]
        self.assertEqual(res.start_date, date(2026, 10, 16))
        self.assertEqual(res.end_date, date(2026, 10, 19))
        self.assertEqual(res.nights_count, 3)
        self.assertEqual(res.reservation_type, ReservationType.BLOCKED)
        self.assertEqual(res.raw_status, "BLOCKED_MERGED")

        # Crucial check: On middle day (Oct 17) at 12:00, stay must be OCCUPIED_DAY, NOT turnover shutdown!
        t_mid = datetime(2026, 10, 17, 12, 0, tzinfo=TIMEZONE_SOFIA)
        d_mid = self.engine.evaluate(merged, t_mid)
        self.assertEqual(d_mid.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(d_mid.target_power)

    def test_confirmed_booking_takes_precedence_over_overlapping_block(self):
        """Confirmed guest booking must completely override conflicting blocked date on same day."""
        guest_res = Reservation(
            uid="confirmed-guest-001",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Reserved - Alice",
            reservation_type=ReservationType.CONFIRMED
        )
        overlapping_block = Reservation(
            uid="host-block-overlapping",
            start_date=date(2026, 10, 19),
            end_date=date(2026, 10, 25),
            summary="Airbnb (Not available)",
            reservation_type=ReservationType.BLOCKED
        )
        reservations = [guest_res, overlapping_block]

        # On Oct 21 (guest mid-stay): guest booking must be active
        t_active = datetime(2026, 10, 21, 14, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(reservations, t_active)
        self.assertEqual(decision.active_reservation.uid, "confirmed-guest-001")
        self.assertEqual(decision.active_reservation.reservation_type, ReservationType.CONFIRMED)
        self.assertIn("Guest reservation", decision.decision_reason)

    def test_consecutive_turnover_between_guest_and_owner(self):
        """Seamless turnover when guest departs at 11:00 and owner blocked stay starts at 15:00."""
        guest_departing = Reservation(
            uid="guest-departing-oct20",
            start_date=date(2026, 10, 17),
            end_date=date(2026, 10, 20),
            summary="Reserved - Bob",
            reservation_type=ReservationType.CONFIRMED
        )
        owner_arriving = Reservation(
            uid="owner-arriving-oct20",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 24),
            summary="Airbnb (Not available)",
            reservation_type=ReservationType.BLOCKED
        )
        reservations = [guest_departing, owner_arriving]

        # 1. 10:00 -> Guest still here
        t_morning = datetime(2026, 10, 20, 10, 0, tzinfo=TIMEZONE_SOFIA)
        d_morning = self.engine.evaluate(reservations, t_morning)
        self.assertEqual(d_morning.active_reservation.uid, "guest-departing-oct20")
        self.assertEqual(d_morning.system_state, SystemState.OCCUPIED_DAY)

        # 2. 12:00 -> Turnover shutdown (cleaners window)
        t_turnover = datetime(2026, 10, 20, 12, 0, tzinfo=TIMEZONE_SOFIA)
        d_turnover = self.engine.evaluate(reservations, t_turnover)
        self.assertEqual(d_turnover.system_state, SystemState.CHECKOUT_SHUTDOWN)
        self.assertFalse(d_turnover.target_power)

        # 3. 13:30 -> Pre-arrival heating for incoming owner stay!
        t_preheat = datetime(2026, 10, 20, 13, 30, tzinfo=TIMEZONE_SOFIA)
        d_preheat = self.engine.evaluate(reservations, t_preheat)
        self.assertEqual(d_preheat.system_state, SystemState.PRE_ARRIVAL_HEATING)
        self.assertEqual(d_preheat.active_reservation.uid, "owner-arriving-oct20")
        self.assertTrue(d_preheat.target_power)
        self.assertEqual(d_preheat.target_temperature, 23.0)

        # 4. 15:30 -> Owner checked in
        t_in = datetime(2026, 10, 20, 15, 30, tzinfo=TIMEZONE_SOFIA)
        d_in = self.engine.evaluate(reservations, t_in)
        self.assertEqual(d_in.system_state, SystemState.OCCUPIED_DAY)
        self.assertEqual(d_in.active_reservation.uid, "owner-arriving-oct20")
        self.assertTrue(d_in.target_power)

    def test_disabled_setting_leaves_blocked_dates_idle(self):
        """When HEAT_DURING_BLOCKED_DATES=False, blocked dates must remain IDLE_UNOCCUPIED."""
        config_disabled = AutomationConfig()
        config_disabled.HEAT_DURING_BLOCKED_DATES = False
        engine_disabled = HVACScheduleEngine(config=config_disabled)

        blocked_res = Reservation(
            uid="block-oct20-23",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Airbnb (Not available)",
            reservation_type=ReservationType.BLOCKED
        )

        t = datetime(2026, 10, 21, 14, 0, tzinfo=TIMEZONE_SOFIA)
        decision = engine_disabled.evaluate([blocked_res], t)
        self.assertEqual(decision.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(decision.target_power)
        self.assertIn("HEAT_DURING_BLOCKED_DATES is disabled", decision.decision_reason)

    def test_safeguard_engine_validates_blocked_dates(self):
        """Safeguards must approve commands for eligible blocked stays when enabled, and reject when disabled."""
        safeguards_enabled = SafeguardEngine(config=self.config)

        blocked_res = Reservation(
            uid="block-valid",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Airbnb (Not available)",
            reservation_type=ReservationType.BLOCKED
        )
        t = datetime(2026, 10, 21, 14, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate([blocked_res], t)

        current_states = {
            self.config.TCL_LIVING_ROOM_ID: ACState(device_id=self.config.TCL_LIVING_ROOM_ID, power=False),
            self.config.TCL_BEDROOM_ID: ACState(device_id=self.config.TCL_BEDROOM_ID, power=False),
        }

        # 1. When enabled: commands are approved
        results = safeguards_enabled.filter_and_validate(decision, current_states, t)
        for dev_id in self.config.DEVICE_IDS:
            should_send, cmd, rationale = results[dev_id]
            self.assertTrue(should_send)
            self.assertEqual(cmd.target_temperature, 21.0)
            self.assertTrue(cmd.power)

        # 2. When disabled: commands are rejected by critical safeguard
        config_disabled = AutomationConfig()
        config_disabled.HEAT_DURING_BLOCKED_DATES = False
        safeguards_disabled = SafeguardEngine(config=config_disabled)

        results_rejected = safeguards_disabled.filter_and_validate(decision, current_states, t)
        for dev_id in self.config.DEVICE_IDS:
            should_send, cmd, rationale = results_rejected[dev_id]
            self.assertFalse(should_send)
            self.assertIn("BLOCKED BY SAFEGUARD", rationale)

    def test_stale_calendar_fallback_protects_active_blocked_stay(self):
        """During a stale calendar outage, in-stay blocked reservation must be protected until checkout."""
        controller = MockACController(device_ids=self.config.DEVICE_IDS)
        orchestrator = AutomationOrchestrator(
            config=self.config,
            controller=controller,
            state_store=StateStore(in_memory=True)
        )

        blocked_stay = Reservation(
            uid="active-blocked-stay",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Airbnb (Not available)",
            reservation_type=ReservationType.BLOCKED
        )

        # Mock sync result returning stale feed
        class MockStaleParser(AirbnbCalendarParser):
            def sync_calendar(self, *args, **kwargs):
                from src.calendar_parser import CalendarSyncResult
                return CalendarSyncResult(
                    reservations=[blocked_stay],
                    source="stale_cache",
                    is_from_cache=True,
                    cache_age_seconds=100000,
                    is_stale=True,
                    error_message="Calendar feed unreachable"
                )

        orchestrator.calendar_parser = MockStaleParser()

        # Run cycle on Oct 21 at 14:00 (mid-stay)
        t_stale = datetime(2026, 10, 21, 14, 0, tzinfo=TIMEZONE_SOFIA)
        res = orchestrator.run_cycle(current_dt=t_stale)

        # The active blocked stay MUST be preserved and heated despite stale calendar!
        self.assertEqual(res.system_state, SystemState.OCCUPIED_DAY)
        self.assertEqual(res.active_reservation.uid, "active-blocked-stay")
        self.assertTrue(controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)


if __name__ == "__main__":
    unittest.main()
