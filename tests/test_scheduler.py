"""Unit tests for the Europe/Sofia HVAC schedule state machine."""

import unittest
from datetime import datetime, date
from typing import List

from src.models import Reservation, ReservationType, SystemState, ACMode, ACFanSpeed
from src.scheduler import HVACScheduleEngine
from src.config import TIMEZONE_SOFIA, AutomationConfig


class TestHVACScheduleEngine(unittest.TestCase):

    def setUp(self):
        self.engine = HVACScheduleEngine()
        self.confirmed_reservation = Reservation(
            uid="test-res-oct20-23",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Reserved (Guest)",
            reservation_type=ReservationType.CONFIRMED
        )
        self.reservations: List[Reservation] = [self.confirmed_reservation]

    def test_idle_when_unoccupied(self):
        # Oct 19: One day before arrival
        dt = datetime(2026, 10, 19, 14, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(decision.target_power)
        self.assertEqual(decision.target_mode, ACMode.OFF)

    def test_idle_on_arrival_morning(self):
        # Oct 20 at 10:00: Arrival day, but before 13:00 pre-arrival
        dt = datetime(2026, 10, 20, 10, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(decision.target_power)

    def test_pre_arrival_heating_window(self):
        # Oct 20 at 13:30: Within the 13:00 - 15:00 pre-arrival window
        dt = datetime(2026, 10, 20, 13, 30, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.PRE_ARRIVAL_HEATING)
        self.assertTrue(decision.target_power)
        self.assertEqual(decision.target_temperature, 23.0)
        self.assertEqual(decision.target_mode, ACMode.HEAT)

    def test_guest_checked_in_afternoon(self):
        # Oct 20 at 16:00: Guest checked in (15:00+)
        dt = datetime(2026, 10, 20, 16, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(decision.target_power)
        self.assertEqual(decision.target_temperature, 21.0)

    def test_diurnal_morning_window(self):
        # Oct 21 at 08:00 (Morning: 07:00 - 10:00)
        dt = datetime(2026, 10, 21, 8, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.OCCUPIED_MORNING)
        self.assertTrue(decision.target_power)
        self.assertEqual(decision.target_temperature, 23.0)
        self.assertEqual(decision.target_fan_speed, ACFanSpeed.MID)

    def test_diurnal_evening_window(self):
        # Oct 21 at 20:00 (Evening: 17:00 - 23:00)
        dt = datetime(2026, 10, 21, 20, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.OCCUPIED_EVENING)
        self.assertTrue(decision.target_power)
        self.assertEqual(decision.target_temperature, 23.0)

    def test_diurnal_night_window(self):
        # Oct 22 at 01:30 (Night: 23:00 - 07:00)
        dt = datetime(2026, 10, 22, 1, 30, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.OCCUPIED_NIGHT)
        self.assertTrue(decision.target_power)
        self.assertEqual(decision.target_temperature, 20.0)
        self.assertEqual(decision.target_fan_speed, ACFanSpeed.QUIET)

    def test_departure_day_before_checkout(self):
        # Oct 23 at 10:00: Departure day, but before 11:00 checkout
        dt = datetime(2026, 10, 23, 10, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(decision.target_power)

    def test_departure_day_at_checkout(self):
        # Oct 23 at 11:05: Departure day after 11:00 checkout
        dt = datetime(2026, 10, 23, 11, 5, tzinfo=TIMEZONE_SOFIA)
        decision = self.engine.evaluate(self.reservations, dt)

        self.assertEqual(decision.system_state, SystemState.CHECKOUT_SHUTDOWN)
        self.assertFalse(decision.target_power)
        self.assertEqual(decision.target_mode, ACMode.OFF)


if __name__ == "__main__":
    unittest.main()
