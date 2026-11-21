"""Regression and safety tests for Active Booking Shutdown Protection."""

import unittest
from datetime import datetime, date, time, timedelta
from unittest.mock import MagicMock

from src.models import (
    Reservation,
    ReservationType,
    ScheduleDecision,
    SystemState,
    ACCommand,
    ACState,
    ACMode,
    ACFanSpeed
)
from src.safeguards import SafeguardEngine
from src.orchestrator import AutomationOrchestrator
from src.config import AutomationConfig, TIMEZONE_SOFIA


class TestActiveBookingShutdownProtection(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        self.safeguards = SafeguardEngine(config=self.config)
        self.dev_id = self.config.TCL_LIVING_ROOM_ID

    def test_shutdown_rejected_when_active_reservation_in_decision(self):
        """If a decision attempts Power=False while carrying an active confirmed guest, it must be rejected."""
        now = datetime(2026, 10, 22, 17, 0, tzinfo=TIMEZONE_SOFIA)
        res = Reservation(
            uid="res-oct22",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        bad_shutdown_decision = ScheduleDecision(
            timestamp=now,
            system_state=SystemState.OCCUPIED_EVENING,
            active_reservation=res,
            target_power=False,  # Unintended power-off attempt!
            target_temperature=20.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Erroneous shutdown attempt",
            commands_to_send={
                self.dev_id: ACCommand(
                    device_id=self.dev_id,
                    power=False,
                    target_temperature=20.0,
                    mode=ACMode.OFF,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )
        current_states = {self.dev_id: ACState(device_id=self.dev_id, power=True, target_temperature=23.0)}

        results = self.safeguards.filter_and_validate(bad_shutdown_decision, current_states, now=now)
        should_send, cmd, rationale = results[self.dev_id]

        self.assertFalse(should_send, "Power=False must be blocked during confirmed active reservation")
        self.assertIn("Automatic shutdown rejected", rationale)

    def test_shutdown_rejected_when_active_reservation_in_trustworthy_snapshot(self):
        """If decision has active_reservation=None (bug/empty) but trustworthy snapshot confirms active stay, reject shutdown."""
        now = datetime(2026, 11, 15, 14, 0, tzinfo=TIMEZONE_SOFIA)
        active_guest = Reservation(
            uid="res-longstay",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        # Decision mistakenly claims IDLE_UNOCCUPIED
        idle_decision = ScheduleDecision(
            timestamp=now,
            system_state=SystemState.IDLE_UNOCCUPIED,
            active_reservation=None,
            target_power=False,
            target_temperature=21.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=True,
            decision_reason="Faulty idle decision",
            commands_to_send={
                self.dev_id: ACCommand(
                    device_id=self.dev_id,
                    power=False,
                    target_temperature=21.0,
                    mode=ACMode.OFF,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )
        current_states = {self.dev_id: ACState(device_id=self.dev_id, power=True)}

        results = self.safeguards.filter_and_validate(
            idle_decision,
            current_states,
            now=now,
            known_reservations=[active_guest]
        )
        should_send, cmd, rationale = results[self.dev_id]

        self.assertFalse(should_send, "Shutdown must be rejected when trustworthy snapshot confirms active guest")
        self.assertIn("Trustworthy snapshot confirms active in-stay guest", rationale)

    def test_shutdown_rejected_during_stale_calendar_with_ongoing_guest(self):
        """When calendar feed is stale, ongoing guest stay must not be powered off."""
        now = datetime(2026, 10, 25, 10, 0, tzinfo=TIMEZONE_SOFIA)
        ongoing_guest = Reservation(
            uid="res-ongoing",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        stale_shutdown_decision = ScheduleDecision(
            timestamp=now,
            system_state=SystemState.IDLE_UNOCCUPIED,
            active_reservation=None,
            target_power=False,
            target_temperature=21.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=True,
            decision_reason="Stale fallback shutdown",
            commands_to_send={
                self.dev_id: ACCommand(
                    device_id=self.dev_id,
                    power=False,
                    target_temperature=21.0,
                    mode=ACMode.OFF,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )
        current_states = {self.dev_id: ACState(device_id=self.dev_id, power=True)}

        results = self.safeguards.filter_and_validate(
            stale_shutdown_decision,
            current_states,
            now=now,
            known_reservations=[ongoing_guest],
            calendar_stale=True
        )
        should_send, cmd, rationale = results[self.dev_id]

        self.assertFalse(should_send, "Shutdown must be rejected during stale calendar when ongoing guest exists")
        self.assertIn("Calendar data is stale during ongoing guest stay", rationale)

    def test_shutdown_allowed_on_legitimate_checkout(self):
        """On checkout day after 11:00, shutdown must be allowed and processed normally."""
        checkout_time = datetime(2026, 12, 22, 11, 5, tzinfo=TIMEZONE_SOFIA)
        ended_guest = Reservation(
            uid="res-ended",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        checkout_decision = ScheduleDecision(
            timestamp=checkout_time,
            system_state=SystemState.CHECKOUT_SHUTDOWN,
            active_reservation=None,
            target_power=False,
            target_temperature=21.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=True,
            decision_reason="Checkout departure at 11:00",
            commands_to_send={
                self.dev_id: ACCommand(
                    device_id=self.dev_id,
                    power=False,
                    target_temperature=21.0,
                    mode=ACMode.OFF,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )
        current_states = {self.dev_id: ACState(device_id=self.dev_id, power=True)}

        results = self.safeguards.filter_and_validate(
            checkout_decision,
            current_states,
            now=checkout_time,
            known_reservations=[ended_guest]
        )
        should_send, cmd, rationale = results[self.dev_id]

        self.assertTrue(should_send, "Legitimate checkout shutdown after 11:00 must be approved")
        self.assertIn("Validated and approved", rationale)

    def test_shutdown_allowed_during_true_vacancy(self):
        """Between reservations when no booking covers the day, shutdown is approved."""
        vacant_time = datetime(2026, 12, 25, 14, 0, tzinfo=TIMEZONE_SOFIA)
        past_res = Reservation(
            uid="res-past",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        vacant_decision = ScheduleDecision(
            timestamp=vacant_time,
            system_state=SystemState.IDLE_UNOCCUPIED,
            active_reservation=None,
            target_power=False,
            target_temperature=21.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=True,
            decision_reason="Unoccupied vacancy",
            commands_to_send={
                self.dev_id: ACCommand(
                    device_id=self.dev_id,
                    power=False,
                    target_temperature=21.0,
                    mode=ACMode.OFF,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )
        current_states = {self.dev_id: ACState(device_id=self.dev_id, power=True)}

        results = self.safeguards.filter_and_validate(
            vacant_decision,
            current_states,
            now=vacant_time,
            known_reservations=[past_res]
        )
        should_send, cmd, rationale = results[self.dev_id]

        self.assertTrue(should_send, "True vacancy shutdown must be approved")
        self.assertIn("Validated and approved", rationale)

    def test_orchestrator_pre_dispatch_intercepts_power_false_during_guest_stay(self):
        """Orchestrator pre-dispatch check prevents Power=False from reaching controller during active stay."""
        mock_controller = MagicMock()
        mock_controller.send_command.return_value = True
        mock_controller.get_all_states.return_value = {
            self.dev_id: ACState(device_id=self.dev_id, power=True, target_temperature=23.0)
        }

        orchestrator = AutomationOrchestrator(
            config=self.config,
            controller=mock_controller
        )

        active_guest = Reservation(
            uid="res-active-stay",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        orchestrator.known_reservations_cache = {active_guest.uid: active_guest}

        now = datetime(2026, 10, 22, 23, 15, tzinfo=TIMEZONE_SOFIA)
        self.assertTrue(orchestrator._is_in_active_confirmed_guest_stay(now))

        # Even if safeguard engine is mocked to allow Power=False
        orchestrator.safeguard_engine = MagicMock()
        orchestrator.safeguard_engine.filter_and_validate.return_value = {
            self.dev_id: (
                True,
                ACCommand(device_id=self.dev_id, power=False, target_temperature=20.0, mode=ACMode.OFF, fan_speed=ACFanSpeed.AUTO),
                "Erroneous bypass"
            )
        }

        # Mock schedule engine
        orchestrator.schedule_engine = MagicMock()
        orchestrator.schedule_engine.to_sofia_time.return_value = now
        orchestrator.schedule_engine.evaluate.return_value = ScheduleDecision(
            timestamp=now,
            system_state=SystemState.OCCUPIED_NIGHT,
            active_reservation=active_guest,
            target_power=False,
            target_temperature=20.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.QUIET,
            target_eco=False,
            decision_reason="Faulty decision",
            commands_to_send={self.dev_id: ACCommand(device_id=self.dev_id, power=False, target_temperature=20.0, mode=ACMode.OFF, fan_speed=ACFanSpeed.AUTO)}
        )

        res = orchestrator.run_cycle(current_dt=now, override_reservations=[active_guest])

        self.assertFalse(res.commands_sent[self.dev_id], "Pre-dispatch layer must block Power=False")
        mock_controller.send_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
