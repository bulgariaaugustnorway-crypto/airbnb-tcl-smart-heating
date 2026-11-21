"""Unit and integration tests for Phase 3B Safe Physical TCL Control Verification."""

import unittest
from unittest.mock import MagicMock, patch, call
from datetime import datetime

from src.models import ACState, ACMode, ACFanSpeed
from src.notifier import NotificationDispatcher, NotificationEvent
from src.tcl.controller import TCLCloudController, ReadOnlyModeError
from src.tcl.phase3b_verifier import (
    Phase3BPhysicalVerifier,
    Phase3BResult,
    LIVING_ROOM_DEFAULT_PHYSICAL_ID,
    BEDROOM_DEFAULT_PHYSICAL_ID,
)


class TestPhase3BControl(unittest.TestCase):

    def setUp(self):
        self.mock_controller = MagicMock(spec=TCLCloudController)
        self.mock_notifier = MagicMock(spec=NotificationDispatcher)

        self.mock_controller.ensure_valid_session.return_value = True
        self.mock_controller.resolve_physical_id.side_effect = lambda dev_id: dev_id

        # Default living room baseline: Power ON, 23.0°C, Heat
        self.living_room_state = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=23.0,
            current_temperature=22.5,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.LOW,
            is_online=True,
            last_updated=datetime.now(),
        )

        self.mock_controller.get_state.return_value = self.living_room_state

        self.verifier = Phase3BPhysicalVerifier(
            controller=self.mock_controller,
            notifier=self.mock_notifier,
            living_room_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            bedroom_id=BEDROOM_DEFAULT_PHYSICAL_ID,
            sync_wait_seconds=0.0  # Fast execution in tests
        )

    def test_successful_1deg_verification_cycle(self):
        """Full successful test: read baseline (23°C), set 24°C, verify, restore 23°C, verify."""
        state_baseline = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
        )
        state_tested = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=24.0,
            mode=ACMode.HEAT,
        )
        state_restored = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
        )

        # get_state calls: 1. baseline read, 2. test setpoint verify, 3. restore verify
        self.mock_controller.get_state.side_effect = [
            state_baseline,
            state_tested,
            state_restored,
        ]

        result = self.verifier.run_verification(
            dry_run=False,
            interactive_confirm_callback=lambda plan: True
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status_code, "SUCCESS")
        self.assertEqual(result.baseline_temperature, 23.0)
        self.assertEqual(result.test_temperature, 24.0)
        self.assertEqual(result.verified_test_temperature, 24.0)
        self.assertEqual(result.restored_temperature, 23.0)

        # Verify controller was called with exact setpoints: first 24.0°C, then 23.0°C
        self.mock_controller.set_target_temperature.assert_has_calls([
            call(LIVING_ROOM_DEFAULT_PHYSICAL_ID, 24.0, dry_run=False),
            call(LIVING_ROOM_DEFAULT_PHYSICAL_ID, 23.0, dry_run=False),
        ])

    def test_guard_refuses_to_turn_on_ac_when_off(self):
        """Safeguard: If living room AC is OFF, refuse to run test and never turn it ON."""
        off_state = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=False,
            target_temperature=23.0,
            mode=ACMode.HEAT,
        )
        self.mock_controller.get_state.return_value = off_state

        result = self.verifier.run_verification(
            dry_run=False,
            interactive_confirm_callback=lambda plan: True
        )

        self.assertFalse(result.success)
        self.assertEqual(result.status_code, "ABORTED_OFF")
        self.assertIn("powered OFF", result.message)

        # Confirm ZERO mutating calls were dispatched
        self.mock_controller.set_target_temperature.assert_not_called()
        self.mock_controller.send_command.assert_not_called()

    def test_guard_aborts_when_user_declines_confirmation(self):
        """Safeguard: If interactive confirmation is declined, abort with zero commands sent."""
        result = self.verifier.run_verification(
            dry_run=False,
            interactive_confirm_callback=lambda plan: False  # User declined
        )

        self.assertFalse(result.success)
        self.assertEqual(result.status_code, "ABORTED_CONFIRM")
        self.assertIn("aborted", result.message.lower())

        # Confirm ZERO mutating calls were dispatched
        self.mock_controller.set_target_temperature.assert_not_called()

    def test_guard_protects_bedroom_ac_from_being_targeted(self):
        """Safeguard: Verifier rejects execution if living room ID matches bedroom ID."""
        verifier_bad = Phase3BPhysicalVerifier(
            controller=self.mock_controller,
            notifier=self.mock_notifier,
            living_room_id=BEDROOM_DEFAULT_PHYSICAL_ID,
            bedroom_id=BEDROOM_DEFAULT_PHYSICAL_ID,
        )

        result = verifier_bad.run_verification(dry_run=False)

        self.assertFalse(result.success)
        self.assertEqual(result.status_code, "ERROR")
        self.assertIn("matches Bedroom ID", result.message)
        self.mock_controller.set_target_temperature.assert_not_called()

    def test_high_baseline_temperature_adjusts_downwards(self):
        """If baseline is 29°C, adjustment should be downwards (-1°C) to 28°C to stay within bounds."""
        state_baseline = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=29.0,
            mode=ACMode.HEAT,
        )
        state_tested = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=28.0,
            mode=ACMode.HEAT,
        )
        state_restored = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=29.0,
            mode=ACMode.HEAT,
        )

        self.mock_controller.get_state.side_effect = [
            state_baseline,
            state_tested,
            state_restored,
        ]

        result = self.verifier.run_verification(
            dry_run=False,
            interactive_confirm_callback=lambda plan: True
        )

        self.assertTrue(result.success)
        self.assertEqual(result.test_temperature, 28.0)
        self.assertEqual(result.restored_temperature, 29.0)

    def test_failed_test_verification_triggers_alert_and_rollback(self):
        """If fresh shadow read does not reflect the test setpoint, trigger alert and rollback."""
        state_baseline = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
        )
        # Device failed to change or returned wrong temp
        state_unresponsive = ACState(
            device_id=LIVING_ROOM_DEFAULT_PHYSICAL_ID,
            power=True,
            target_temperature=23.0,  # Unchanged
            mode=ACMode.HEAT,
        )

        self.mock_controller.get_state.side_effect = [
            state_baseline,
            state_unresponsive,
        ]

        result = self.verifier.run_verification(
            dry_run=False,
            interactive_confirm_callback=lambda plan: True
        )

        self.assertFalse(result.success)
        self.assertEqual(result.status_code, "FAILED_VERIFY")
        self.assertIn("Verification failed", result.message)

        # Verify emergency rollback to baseline was dispatched
        self.mock_controller.set_target_temperature.assert_has_calls([
            call(LIVING_ROOM_DEFAULT_PHYSICAL_ID, 24.0, dry_run=False),
            call(LIVING_ROOM_DEFAULT_PHYSICAL_ID, 23.0, dry_run=False),
        ])

        # Verify alert was dispatched
        self.mock_notifier.dispatch.assert_called()
        dispatched_event = self.mock_notifier.dispatch.call_args[0][0]
        self.assertEqual(dispatched_event.event_type, "PHASE_3B_ALERT")
        self.assertEqual(dispatched_event.severity, "CRITICAL")

    def test_dry_run_mode(self):
        """Dry-run simulation passes without calling real hardware commands."""
        result = self.verifier.run_verification(
            dry_run=True,
            interactive_confirm_callback=None
        )

        self.assertTrue(result.success)
        self.assertTrue(result.dry_run)
        self.mock_controller.set_target_temperature.assert_has_calls([
            call(LIVING_ROOM_DEFAULT_PHYSICAL_ID, 24.0, dry_run=True),
            call(LIVING_ROOM_DEFAULT_PHYSICAL_ID, 23.0, dry_run=True),
        ])


if __name__ == "__main__":
    unittest.main()
