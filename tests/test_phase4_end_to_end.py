"""Phase 4 End-to-End Integration Tests.

Validates:
1. Cancellation grace period lifecycle (disappearance -> grace protection -> restoration vs expiry).
2. Stale calendar safe fallback (>48h staleness prevents erroneous heating).
3. Guest manual override preservation during active stay dayparts.
4. Independent bedroom and living room controls and temperature offsets.
5. Persistent StateStore serialization and recovery.
6. AWS Lambda handler execution and response format.
"""

import json
import unittest
from datetime import datetime, date, timedelta, time
from typing import Dict, Any, List

from src.config import AutomationConfig, TIMEZONE_SOFIA, SAMPLE_CALENDARS_DIR
from src.models import (
    Reservation,
    ReservationType,
    SystemState,
    ACCommand,
    ACState,
    ACMode,
    ACFanSpeed,
    ScheduleDecision,
)
from src.orchestrator import AutomationOrchestrator, CycleResult
from src.device_controller import MockACController
from unittest.mock import patch, MagicMock
from src.notifier import NotificationDispatcher
from src.state_store import StateStore
from src.safeguards import SafeguardEngine
from src.scheduler import HVACScheduleEngine
from lambda_handler import lambda_handler, load_ssm_parameters


class TestPhase4EndToEnd(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        self.config.DRY_RUN = False
        self.controller = MockACController(device_ids=self.config.DEVICE_IDS)
        self.notifier = NotificationDispatcher()
        self.state_store = StateStore(in_memory=True)
        self.orchestrator = AutomationOrchestrator(
            config=self.config,
            controller=self.controller,
            notifier=self.notifier,
            state_store=self.state_store
        )

    # -------------------------------------------------------------------------
    # 1. Cancellation Grace Period Lifecycle Tests
    # -------------------------------------------------------------------------
    def test_grace_period_activation_and_restoration(self):
        """When an in-progress stay disappears, grace period protects it; if it reappears, grace clears."""
        cal_v1 = SAMPLE_CALENDARS_DIR / "cancellation_v1.ics"
        cal_v2 = SAMPLE_CALENDARS_DIR / "cancellation_v2.ics"

        t_now = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)

        # 1. Active stay running normally
        self.controller.states[self.config.TCL_LIVING_ROOM_ID].power = True
        self.orchestrator.safeguard_engine.has_live_command_history = True
        res1 = self.orchestrator.run_cycle(current_dt=t_now, calendar_file=cal_v1)
        self.assertEqual(res1.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)

        # 2. Reservation disappears from feed (Airbnb API glitch)
        res2 = self.orchestrator.run_cycle(current_dt=t_now, calendar_file=cal_v2)
        self.assertEqual(res2.system_state, SystemState.OCCUPIED_DAY)
        self.assertIn("synthetic-a6ed1b4af6@example.invalid", res2.in_grace_period_uids)
        self.assertTrue(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)
        self.assertIn("synthetic-a6ed1b4af6@example.invalid", self.orchestrator.active_grace_periods)

        # 3. Next cycle: Airbnb feed recovers and reservation reappears
        res3 = self.orchestrator.run_cycle(current_dt=t_now, calendar_file=cal_v1)
        self.assertEqual(res3.system_state, SystemState.OCCUPIED_DAY)
        self.assertNotIn("synthetic-a6ed1b4af6@example.invalid", self.orchestrator.active_grace_periods)

    def test_grace_period_expiry_leads_to_safe_shutdown(self):
        """When an in-progress stay disappears and does not return, shutdown occurs after 4 hours."""
        self.config.HEAT_DURING_BLOCKED_DATES = False
        cal_v1 = SAMPLE_CALENDARS_DIR / "cancellation_v1.ics"
        cal_v2 = SAMPLE_CALENDARS_DIR / "cancellation_v2.ics"

        t_now = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)

        # 1. Establish active stay
        self.controller.states[self.config.TCL_LIVING_ROOM_ID].power = True
        self.orchestrator.safeguard_engine.has_live_command_history = True
        self.orchestrator.run_cycle(current_dt=t_now, calendar_file=cal_v1)


        # 2. Stay disappears -> 4h grace period begins (until 18:00)
        res2 = self.orchestrator.run_cycle(current_dt=t_now, calendar_file=cal_v2)
        self.assertEqual(res2.system_state, SystemState.OCCUPIED_DAY)

        # 3. 3 hours later (17:00) -> Still within grace period, heating maintained
        t_inside_grace = t_now + timedelta(hours=3)
        res3 = self.orchestrator.run_cycle(current_dt=t_inside_grace, calendar_file=cal_v2)
        self.assertEqual(res3.system_state, SystemState.OCCUPIED_EVENING)
        self.assertTrue(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)

        # 4. 5 hours later (19:00) -> Grace period expired, shut down safely
        t_after_grace = t_now + timedelta(hours=5)
        res4 = self.orchestrator.run_cycle(current_dt=t_after_grace, calendar_file=cal_v2)
        self.assertEqual(res4.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertFalse(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)
        self.assertFalse(self.controller.get_state(self.config.TCL_BEDROOM_ID).power)

    # -------------------------------------------------------------------------
    # 2. Stale Calendar Safe Fallback Tests
    # -------------------------------------------------------------------------
    def test_stale_calendar_blocks_unverified_future_arrival(self):
        """A calendar older than 48 hours prevents unverified future arrivals from triggering heating."""
        from src.calendar_parser import CalendarSyncResult

        # Future reservation starting tomorrow
        stale_sync_result = CalendarSyncResult(
            reservations=[
                Reservation(
                    uid="future-res-1",
                    start_date=date(2026, 10, 23),
                    end_date=date(2026, 10, 27),
                    summary="Future Reservation",
                    reservation_type=ReservationType.CONFIRMED
                )
            ],
            source="mock_stale_cache",
            cancelled_uids=[],
            is_stale=True,
            error_message="Calendar file last modified 52 hours ago (exceeds 48h limit)"
        )

        class MockStaleParser:
            def sync_calendar(self, *args, **kwargs):
                return stale_sync_result

        orchestrator = AutomationOrchestrator(
            config=self.config,
            controller=self.controller,
            notifier=self.notifier,
            calendar_parser=MockStaleParser(),  # type: ignore
            state_store=StateStore(in_memory=True)
        )

        # Evaluating today (2026-10-22) -> Future arrival is suppressed to protect against heating cancelled booking
        t_now = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        result = orchestrator.run_cycle(current_dt=t_now)

        self.assertEqual(result.system_state, SystemState.IDLE_UNOCCUPIED)
        self.assertTrue(result.calendar_stale)
        self.assertFalse(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)

        # Verify critical alert was logged
        stale_events = [e for e in self.notifier.events_history if e.event_type == "CALENDAR_STALE_ALERT"]
        self.assertTrue(len(stale_events) >= 1)

    def test_stale_calendar_preserves_active_in_stay_guest(self):
        """A calendar older than 48 hours does NOT shut down heating for an active in-stay guest."""
        from src.calendar_parser import CalendarSyncResult

        # Established in-stay reservation
        stale_sync_result = CalendarSyncResult(
            reservations=[
                Reservation(
                    uid="active-in-stay-guest",
                    start_date=date(2026, 10, 20),
                    end_date=date(2026, 10, 25),
                    summary="Active In-Stay Guest",
                    reservation_type=ReservationType.CONFIRMED
                )
            ],
            source="mock_stale_cache",
            cancelled_uids=[],
            is_stale=True,
            error_message="Calendar file last modified 52 hours ago (exceeds 48h limit)"
        )

        class MockStaleParser:
            def sync_calendar(self, *args, **kwargs):
                return stale_sync_result

        orchestrator = AutomationOrchestrator(
            config=self.config,
            controller=self.controller,
            notifier=self.notifier,
            calendar_parser=MockStaleParser(),  # type: ignore
            state_store=StateStore(in_memory=True)
        )

        t_now = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        self.controller.states[self.config.TCL_LIVING_ROOM_ID].power = True
        orchestrator.safeguard_engine.has_live_command_history = True
        result = orchestrator.run_cycle(current_dt=t_now)

        # Must maintain OCCUPIED_DAY because guest is physically in-residence!
        self.assertEqual(result.system_state, SystemState.OCCUPIED_DAY)
        self.assertTrue(result.calendar_stale)
        self.assertTrue(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).power)
        self.assertEqual(self.controller.get_state(self.config.TCL_LIVING_ROOM_ID).target_temperature, 21.0)


    def test_calendar_recovery_notification(self):
        """When calendar was stale and recovers, notify_calendar_recovered is emitted."""
        from src.calendar_parser import CalendarSyncResult

        stale_res = CalendarSyncResult(
            reservations=[],
            source="cache",
            is_stale=True,
            error_message="Stale calendar"
        )
        fresh_res = CalendarSyncResult(
            reservations=[],
            source="network",
            is_stale=False,
            error_message=None
        )

        class SwitchingParser:
            def __init__(self):
                self.is_stale_mode = True
            def sync_calendar(self, *args, **kwargs):
                return stale_res if self.is_stale_mode else fresh_res

        parser = SwitchingParser()
        orchestrator = AutomationOrchestrator(
            config=self.config,
            controller=self.controller,
            notifier=self.notifier,
            calendar_parser=parser,  # type: ignore
            state_store=StateStore(in_memory=True)
        )

        t_now = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        # Cycle 1: Stale
        orchestrator.run_cycle(current_dt=t_now)
        self.assertTrue(orchestrator.was_calendar_stale)

        # Cycle 2: Recovered!
        parser.is_stale_mode = False
        orchestrator.run_cycle(current_dt=t_now)
        self.assertFalse(orchestrator.was_calendar_stale)

        recovered_events = [e for e in self.notifier.events_history if e.event_type == "CALENDAR_RECOVERED"]
        self.assertTrue(len(recovered_events) >= 1)

    # -------------------------------------------------------------------------
    # 3. Guest Manual Override Preservation Tests
    # -------------------------------------------------------------------------
    def test_guest_manual_override_preserved_in_daypart(self):
        """If a guest manually sets AC to 25°C during daytime, automation preserves it until evening."""
        safeguard = SafeguardEngine(config=self.config)
        dev_id = self.config.TCL_LIVING_ROOM_ID
        now = datetime(2026, 10, 22, 12, 0, tzinfo=TIMEZONE_SOFIA)

        active_res = Reservation(
            uid="guest-res-override",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Guest)",
            reservation_type=ReservationType.CONFIRMED
        )

        # Scheduled target: 21°C during OCCUPIED_DAY
        decision = ScheduleDecision(
            timestamp=now,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=active_res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Occupied Day",
            commands_to_send={
                dev_id: ACCommand(
                    device_id=dev_id,
                    power=True,
                    target_temperature=21.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.AUTO,
                    eco_mode=False,
                    reason="Scheduled Day: 21.0°C"
                )
            }
        )

        # Record prior command applied by automation at start of daypart (21.0°C)
        safeguard.record_applied_command(decision.commands_to_send[dev_id])

        # Hardware reports guest changed temperature to 25.0°C via remote
        current_states = {
            dev_id: ACState(
                device_id=dev_id,
                power=True,
                target_temperature=25.0,  # Manual guest override!
                current_temperature=22.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                eco_mode=False,
                is_online=True,
                last_updated=datetime.now()
            )
        }

        # Cycle 1: Non-transition tick -> Safeguard preserves guest override!
        results = safeguard.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=now,
            is_transition_tick=False
        )

        should_send, cmd, rationale = results[dev_id]
        self.assertFalse(should_send)
        self.assertIn("Guest manual override preserved", rationale)

        # Cycle 2: Daypart transition to OCCUPIED_EVENING (17:00) -> Override resets to schedule!
        now_evening = datetime(2026, 10, 22, 17, 0, tzinfo=TIMEZONE_SOFIA)
        decision.timestamp = now_evening
        decision.system_state = SystemState.OCCUPIED_EVENING
        decision.commands_to_send[dev_id].target_temperature = 23.0

        results_transition = safeguard.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=now_evening,
            is_transition_tick=True
        )

        should_send_trans, cmd_trans, rationale_trans = results_transition[dev_id]
        self.assertTrue(should_send_trans)
        self.assertEqual(cmd_trans.target_temperature, 23.0)

    def test_guest_manual_override_unsafe_temp_clamped(self):
        """If a guest manually sets temperature outside [16, 30] (e.g. 35°C), automation overrides with safe schedule."""
        safeguard = SafeguardEngine(config=self.config)
        dev_id = self.config.TCL_LIVING_ROOM_ID
        now = datetime(2026, 10, 22, 12, 0, tzinfo=TIMEZONE_SOFIA)

        active_res = Reservation(
            uid="guest-res-extreme",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Guest)",
            reservation_type=ReservationType.CONFIRMED
        )

        decision = ScheduleDecision(
            timestamp=now,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=active_res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Occupied Day",
            commands_to_send={
                dev_id: ACCommand(
                    device_id=dev_id,
                    power=True,
                    target_temperature=21.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.AUTO,
                    eco_mode=False,
                    reason="Scheduled Day: 21.0°C"
                )
            }
        )
        safeguard.record_applied_command(decision.commands_to_send[dev_id])

        # Hardware reports guest changed temperature to unsafe 35.0°C
        current_states = {
            dev_id: ACState(
                device_id=dev_id,
                power=True,
                target_temperature=35.0,  # Unsafe extreme!
                current_temperature=22.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                eco_mode=False,
                is_online=True,
                last_updated=datetime.now()
            )
        }

        results = safeguard.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=now,
            is_transition_tick=False
        )

        should_send, cmd, rationale = results[dev_id]
        # Should NOT preserve unsafe temperature! Automation must send corrective command
        self.assertTrue(should_send)
        self.assertEqual(cmd.target_temperature, 21.0)

    # -------------------------------------------------------------------------
    # 4. Bedroom Temperature Offset and Enablement Tests
    # -------------------------------------------------------------------------
    def test_bedroom_offset_and_enablement(self):
        """Validate bedroom temperature offset (-1.0°C) and independent enablement toggle."""
        engine = HVACScheduleEngine(config=self.config)
        self.config.BEDROOM_TEMP_OFFSET = -1.0
        self.config.LIVING_ROOM_ENABLED = True
        self.config.BEDROOM_ENABLED = True

        res = Reservation(
            uid="res-offset-test",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 23),
            summary="Reservation",
            reservation_type=ReservationType.CONFIRMED
        )

        # 08:00 -> Morning schedule (Base = 23.0°C)
        t_morning = datetime(2026, 10, 21, 8, 0, tzinfo=TIMEZONE_SOFIA)
        decision = engine.evaluate([res], t_morning)

        lr_cmd = decision.commands_to_send[self.config.TCL_LIVING_ROOM_ID]
        br_cmd = decision.commands_to_send[self.config.TCL_BEDROOM_ID]

        self.assertEqual(lr_cmd.target_temperature, 23.0)
        self.assertEqual(br_cmd.target_temperature, 22.0)  # 23.0 - 1.0 offset
        self.assertTrue(lr_cmd.power)
        self.assertTrue(br_cmd.power)

        # Now disable bedroom
        self.config.BEDROOM_ENABLED = False
        decision2 = engine.evaluate([res], t_morning)
        self.assertTrue(decision2.commands_to_send[self.config.TCL_LIVING_ROOM_ID].power)
        self.assertFalse(decision2.commands_to_send[self.config.TCL_BEDROOM_ID].power)
        self.assertEqual(decision2.commands_to_send[self.config.TCL_BEDROOM_ID].mode, ACMode.OFF)

        # Restore
        self.config.BEDROOM_ENABLED = True
        self.config.BEDROOM_TEMP_OFFSET = 0.0

    # -------------------------------------------------------------------------
    # 5. Persistent StateStore Tests
    # -------------------------------------------------------------------------
    def test_state_store_serialization_and_recovery(self):
        """Validate that StateStore preserves and rehydrates state cleanly."""
        store = StateStore(in_memory=True)
        test_state = {
            "last_system_state": "OCCUPIED_DAY",
            "known_confirmed_uids": ["res-1", "res-2"],
            "active_grace_periods": {
                "res-1": {
                    "disappeared_at": "2026-10-23T14:00:00+02:00",
                    "grace_until": "2026-10-23T18:00:00+02:00",
                    "reservation": {
                        "uid": "res-1",
                        "start_date": "2026-10-20",
                        "end_date": "2026-10-23",
                        "summary": "Guest 1",
                        "raw_status": "CONFIRMED"
                    }
                }
            },
            "last_power_toggle": {
                "demo_living_room": "2026-10-23T14:00:00+02:00"
            }
        }

        saved = store.save_state(test_state)
        self.assertTrue(saved)

        loaded = store.load_state()
        self.assertEqual(loaded["last_system_state"], "OCCUPIED_DAY")
        self.assertEqual(loaded["known_confirmed_uids"], ["res-1", "res-2"])
        self.assertIn("res-1", loaded["active_grace_periods"])

    # -------------------------------------------------------------------------
    # 6. AWS Lambda Handler Tests
    # -------------------------------------------------------------------------
    def test_lambda_handler_execution(self):
        """Validate that lambda_handler runs a cycle and returns HTTP 200 with JSON summary."""
        # Run with mock event simulating 15-minute EventBridge trigger
        event = {
            "source": "aws.events",
            "current_dt": "2026-10-21T08:30:00+03:00"  # In Sofia morning window
        }

        response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)

        body = response["body"]
        self.assertEqual(body["status"], "success")
        self.assertIn("system_state", body)
        self.assertIn("commands_sent", body)
        self.assertIn("execution_duration_ms", body)
        self.assertTrue(body["execution_duration_ms"] > 0)

    # -------------------------------------------------------------------------
    # 7. Multi-Channel OTA Classification Tests
    # -------------------------------------------------------------------------
    def test_multi_channel_imported_ota_classification(self):
        """Validate classifying Booking.com, VRBO, and Airbnb reservations vs manual blocks."""
        from src.calendar_parser import AirbnbCalendarParser

        parser = AirbnbCalendarParser()
        # Airbnb native
        self.assertEqual(parser._classify_reservation("Reserved - Guest Name"), ReservationType.CONFIRMED)
        # Booking.com imported into Airbnb
        self.assertEqual(parser._classify_reservation("Booking.com - 3491294821"), ReservationType.CONFIRMED)
        # Booking.com native
        self.assertEqual(parser._classify_reservation("CLOSED - John Smith"), ReservationType.CONFIRMED)
        # VRBO
        self.assertEqual(parser._classify_reservation("VRBO - HA-192849"), ReservationType.CONFIRMED)
        # Manual host block
        self.assertEqual(parser._classify_reservation("Not available"), ReservationType.BLOCKED)
        self.assertEqual(parser._classify_reservation("Blocked by host"), ReservationType.BLOCKED)

    # -------------------------------------------------------------------------
    # 8. AWS SSM Parameter Store & Packaging Security Tests
    # -------------------------------------------------------------------------
    @patch("boto3.client")
    def test_ssm_parameter_store_hydration(self, mock_boto_client):
        """Test retrieving secrets from SSM Parameter Store with KMS decryption."""
        mock_ssm = MagicMock()
        mock_boto_client.return_value = mock_ssm

        mock_ssm.get_parameters_by_path.return_value = {
            "Parameters": [
                {"Name": "/smart-heating/example/tcl_username", "Value": "test-host@example.com"},
                {"Name": "/smart-heating/example/tcl_password", "Value": "mock-tcl-password-123"},
                {"Name": "/smart-heating/example/airbnb_ical_url", "Value": "https://calendar.example.invalid/ical/mock.ics?s=secret"},
                {"Name": "/smart-heating/example/notification_webhook_url", "Value": "https://webhook.mock/alert"}
            ]
        }

        params = load_ssm_parameters("/smart-heating/example")

        self.assertEqual(params["tcl_username"], "test-host@example.com")
        self.assertEqual(params["tcl_password"], "mock-tcl-password-123")
        self.assertEqual(params["airbnb_ical_url"], "https://calendar.example.invalid/ical/mock.ics?s=secret")
        self.assertEqual(params["notification_webhook_url"], "https://webhook.mock/alert")

        # Verify WithDecryption=True was requested for KMS SecureString
        mock_ssm.get_parameters_by_path.assert_called_once_with(
            Path="/smart-heating/example",
            Recursive=True,
            WithDecryption=True
        )

    @patch("boto3.client")
    def test_ssm_parameter_store_error_fallback(self, mock_boto_client):
        """Test graceful error fallback when SSM service throws an exception."""
        mock_ssm = MagicMock()
        mock_boto_client.return_value = mock_ssm
        mock_ssm.get_parameters_by_path.side_effect = RuntimeError("SSM service unavailable")

        # Must not raise exception, but return empty dictionary
        params = load_ssm_parameters("/smart-heating/example")
        self.assertEqual(params, {})

    def test_samignore_excludes_sensitive_files(self):
        """Verify that .samignore explicitly filters out .env, credentials, and cache files from packaging."""
        import fnmatch
        from src.config import PROJECT_ROOT

        samignore_path = PROJECT_ROOT / ".samignore"
        self.assertTrue(samignore_path.exists(), ".samignore must exist in project root")

        rules = [
            line.strip() for line in samignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        def is_ignored(rel_path: str) -> bool:
            norm = rel_path.replace("\\", "/")
            return any(
                fnmatch.fnmatch(norm, r) or fnmatch.fnmatch(norm.split("/")[-1], r) or norm.startswith(r.rstrip("/*"))
                for r in rules
            )

        # Critical sensitive files that must NEVER be packaged into deployment artifact
        self.assertTrue(is_ignored(".env"))
        self.assertTrue(is_ignored(".env.local"))
        self.assertTrue(is_ignored(".env.prod"))
        self.assertTrue(is_ignored("credentials.json"))
        self.assertTrue(is_ignored(".venv/bin/activate"))
        self.assertTrue(is_ignored("data/cache/automation_state.json"))
        self.assertTrue(is_ignored("logs/automation_audit.log"))
        self.assertTrue(is_ignored("tests/test_safeguards.py"))
        self.assertTrue(is_ignored("verify_tcl_connection.py"))


if __name__ == "__main__":
    unittest.main()
