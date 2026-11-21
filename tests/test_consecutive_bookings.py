"""Unit tests for consecutive (same-day back-to-back) turnover bookings."""

import unittest
from datetime import datetime, date
from typing import List

from src.models import Reservation, ReservationType, SystemState, ACMode
from src.scheduler import HVACScheduleEngine
from src.config import TIMEZONE_SOFIA


class TestConsecutiveBookings(unittest.TestCase):

    def setUp(self):
        self.engine = HVACScheduleEngine()
        self.guest_a = Reservation(
            uid="res-guest-a",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Reserved (Guest A)",
            reservation_type=ReservationType.CONFIRMED
        )
        self.guest_b = Reservation(
            uid="res-guest-b",
            start_date=date(2026, 10, 23),
            end_date=date(2026, 10, 26),
            summary="Reserved (Guest B)",
            reservation_type=ReservationType.CONFIRMED
        )
        self.reservations: List[Reservation] = [self.guest_a, self.guest_b]

    def test_consecutive_turnover_day_progression(self):
        # 1. 08:00 (Oct 23): Guest A still in residence -> Occupied Morning
        t_morning = datetime(2026, 10, 23, 8, 0, tzinfo=TIMEZONE_SOFIA)
        dec_morning = self.engine.evaluate(self.reservations, t_morning)
        self.assertEqual(dec_morning.system_state, SystemState.OCCUPIED_MORNING)
        self.assertTrue(dec_morning.target_power)
        self.assertEqual(dec_morning.active_reservation.uid, "res-guest-a")

        # 2. 10:30 (Oct 23): Guest A finishing packing -> Occupied Day
        t_packing = datetime(2026, 10, 23, 10, 30, tzinfo=TIMEZONE_SOFIA)
        dec_packing = self.engine.evaluate(self.reservations, t_packing)
        self.assertEqual(dec_packing.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(dec_packing.target_power)
        self.assertEqual(dec_packing.active_reservation.uid, "res-guest-a")

        # 3. 11:30 (Oct 23): Turnover window! Guest A has checked out (11:00), Guest B pre-heat starts at 13:00.
        # ACs MUST be OFF during turnover/cleaning to save energy.
        t_turnover = datetime(2026, 10, 23, 11, 30, tzinfo=TIMEZONE_SOFIA)
        dec_turnover = self.engine.evaluate(self.reservations, t_turnover)
        self.assertEqual(dec_turnover.system_state, SystemState.CHECKOUT_SHUTDOWN)
        self.assertFalse(dec_turnover.target_power, "ACs must be OFF during turnover window")
        self.assertEqual(dec_turnover.target_mode, ACMode.OFF)

        # 4. 13:30 (Oct 23): Pre-arrival heating kicks in for Guest B! (13:00 - 15:00)
        t_preheat = datetime(2026, 10, 23, 13, 30, tzinfo=TIMEZONE_SOFIA)
        dec_preheat = self.engine.evaluate(self.reservations, t_preheat)
        self.assertEqual(dec_preheat.system_state, SystemState.PRE_ARRIVAL_HEATING)
        self.assertTrue(dec_preheat.target_power)
        self.assertEqual(dec_preheat.target_temperature, 23.0)
        self.assertEqual(dec_preheat.target_mode, ACMode.HEAT)
        self.assertEqual(dec_preheat.active_reservation.uid, "res-guest-b")

        # 5. 16:00 (Oct 23): Guest B checked in (15:00+) -> Occupied Day
        t_checked_in = datetime(2026, 10, 23, 16, 0, tzinfo=TIMEZONE_SOFIA)
        dec_checked_in = self.engine.evaluate(self.reservations, t_checked_in)
        self.assertEqual(dec_checked_in.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(dec_checked_in.target_power)
        self.assertEqual(dec_checked_in.active_reservation.uid, "res-guest-b")


if __name__ == "__main__":
    unittest.main()
