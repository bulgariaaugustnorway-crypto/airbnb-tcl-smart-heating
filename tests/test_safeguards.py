"""Unit tests for safety guardrails, compressor cycling protection, and debouncing."""

import unittest
from datetime import datetime, timedelta

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
from src.config import AutomationConfig, TIMEZONE_SOFIA


class TestSafeguards(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        self.safeguards = SafeguardEngine(config=self.config)
        self.eval_time = datetime(2026, 10, 20, 14, 0, tzinfo=TIMEZONE_SOFIA)

    def test_strict_guard_rejects_occupied_without_confirmed_booking(self):
        # Malicious or bugged decision trying to heat property without confirmed reservation
        bad_decision = ScheduleDecision(
            timestamp=self.eval_time,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=None,  # No reservation!
            target_power=True,
            target_temperature=22.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Faulty trigger",
            commands_to_send={
                "tcl_ac_living_room": ACCommand(
                    device_id="tcl_ac_living_room",
                    power=True,
                    target_temperature=22.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )

        current_states = {
            "tcl_ac_living_room": ACState(device_id="tcl_ac_living_room", power=False)
        }

        results = self.safeguards.filter_and_validate(bad_decision, current_states, self.eval_time)
        should_send, cmd, rationale = results["tcl_ac_living_room"]

        self.assertFalse(should_send, "Safeguard must reject occupied command without confirmed reservation")
        self.assertIn("BLOCKED BY SAFEGUARD", rationale)

    def test_debounce_skips_identical_command(self):
        # Current device state matches target command perfectly
        current_states = {
            "tcl_ac_living_room": ACState(
                device_id="tcl_ac_living_room",
                power=True,
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                eco_mode=False
            )
        }

        confirmed_res = Reservation(
            uid="res-valid",
            start_date=self.eval_time.date(),
            end_date=self.eval_time.date() + timedelta(days=2),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        valid_decision = ScheduleDecision(
            timestamp=self.eval_time,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=confirmed_res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Valid stay",
            commands_to_send={
                "tcl_ac_living_room": ACCommand(
                    device_id="tcl_ac_living_room",
                    power=True,
                    target_temperature=21.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.AUTO,
                    eco_mode=False
                )
            }
        )

        results = self.safeguards.filter_and_validate(valid_decision, current_states, self.eval_time)
        should_send, cmd, rationale = results["tcl_ac_living_room"]

        self.assertFalse(should_send, "Command should be skipped if device is already in desired state")
        self.assertIn("Debounce", rationale)

    def test_compressor_anti_cycling_dwell_time(self):
        # 1. First toggle: Turn unit ON at T=0
        t0 = datetime(2026, 10, 20, 14, 0, tzinfo=TIMEZONE_SOFIA)
        current_states = {
            "tcl_ac_living_room": ACState(device_id="tcl_ac_living_room", power=False)
        }
        res = Reservation(
            uid="res-valid",
            start_date=t0.date(),
            end_date=t0.date() + timedelta(days=2),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        dec_on = ScheduleDecision(
            timestamp=t0,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Turn on",
            commands_to_send={
                "tcl_ac_living_room": ACCommand(
                    device_id="tcl_ac_living_room",
                    power=True,
                    target_temperature=21.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )
        results = self.safeguards.filter_and_validate(dec_on, current_states, t0)
        self.assertTrue(results["tcl_ac_living_room"][0])

        # Update simulated state to ON
        current_states["tcl_ac_living_room"].power = True

        # 2. Rapid flip-flop attempt: Try turning unit OFF only 2 minutes (120s) later!
        t1 = t0 + timedelta(minutes=2)
        dec_off = ScheduleDecision(
            timestamp=t1,
            system_state=SystemState.CHECKOUT_SHUTDOWN,
            active_reservation=None,
            target_power=False,
            target_temperature=21.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=True,
            decision_reason="Rapid shutdown attempt",
            commands_to_send={
                "tcl_ac_living_room": ACCommand(
                    device_id="tcl_ac_living_room",
                    power=False,
                    target_temperature=21.0,
                    mode=ACMode.OFF,
                    fan_speed=ACFanSpeed.AUTO
                )
            }
        )
        results2 = self.safeguards.filter_and_validate(dec_off, current_states, t1)
        should_send, cmd, rationale = results2["tcl_ac_living_room"]

        self.assertFalse(should_send, "Rapid power toggling must be blocked to protect compressor")
        self.assertIn("Compressor protection", rationale)

    def test_temperature_clamping(self):
        # Extreme temperature command (e.g. 35°C or 10°C)
        res = Reservation(
            uid="res-valid",
            start_date=self.eval_time.date(),
            end_date=self.eval_time.date() + timedelta(days=2),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        cmd = ACCommand(
            device_id="tcl_ac_living_room",
            power=True,
            target_temperature=38.0,  # Dangerously high
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO
        )
        dec = ScheduleDecision(
            timestamp=self.eval_time,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=res,
            target_power=True,
            target_temperature=38.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Overheat test",
            commands_to_send={"tcl_ac_living_room": cmd}
        )

        current_states = {
            "tcl_ac_living_room": ACState(device_id="tcl_ac_living_room", power=False)
        }

        results = self.safeguards.filter_and_validate(dec, current_states, self.eval_time)
        should_send, validated_cmd, rationale = results["tcl_ac_living_room"]

    def test_debounce_ignores_beep_feedback_reset_by_physical_remote(self):
        """Physical remote buttons reset beepSwitch: 1. Debounce must ignore beep mismatch when climate matches."""
        # Hardware reports beep_feedback=True (from physical IR remote press), but climate matches desired command
        current_states = {
            "tcl_ac_living_room": ACState(
                device_id="tcl_ac_living_room",
                power=True,
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                eco_mode=False,
                display_led=True,
                beep_feedback=True,  # Hardware buzzer enabled by physical remote
                is_online=True
            )
        }

        confirmed_res = Reservation(
            uid="res-valid",
            start_date=self.eval_time.date(),
            end_date=self.eval_time.date() + timedelta(days=2),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        # Desired command specifies beep_feedback=False (silent operation)
        target_cmd = ACCommand(
            device_id="tcl_ac_living_room",
            power=True,
            target_temperature=21.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            eco_mode=False,
            display_led=True,
            beep_feedback=False
        )

        valid_decision = ScheduleDecision(
            timestamp=self.eval_time,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=confirmed_res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Valid stay",
            commands_to_send={"tcl_ac_living_room": target_cmd}
        )

        # Should be filtered out by Debounce — not sent!
        results = self.safeguards.filter_and_validate(valid_decision, current_states, self.eval_time)
        should_send, cmd, rationale = results["tcl_ac_living_room"]

        self.assertFalse(should_send, "Must not re-transmit command every 15m solely because beep_feedback is 1")
        self.assertIn("Debounce", rationale)

        # If climate DOES change (e.g. setpoint becomes 23.0°C), command IS sent with beep_feedback=False
        cmd_changed = ACCommand(
            device_id="tcl_ac_living_room",
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            eco_mode=False,
            display_led=True,
            beep_feedback=False
        )
        dec_changed = ScheduleDecision(
            timestamp=self.eval_time,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=confirmed_res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Transition to 23C",
            commands_to_send={"tcl_ac_living_room": cmd_changed}
        )
        results2 = self.safeguards.filter_and_validate(dec_changed, current_states, self.eval_time)
        should_send2, cmd2, _ = results2["tcl_ac_living_room"]
        self.assertTrue(should_send2, "Must send command when climate actually changes")
        self.assertFalse(cmd2.beep_feedback, "Sent command must bundle silent beep feedback (False)")


if __name__ == "__main__":
    unittest.main()
