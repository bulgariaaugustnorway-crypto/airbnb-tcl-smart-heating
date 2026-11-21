"""Phase 5 LED Display & Night Transition Automated Test Suite.

Validates:
1. 07:00-23:00 Daytime LED ON (screen: 1) when running.
2. 23:00-07:00 Nighttime LED OFF (screen: 0), Quiet fan speed (gear 1), 20°C heating target.
3. All hours: Beep feedback OFF (beepSwitch: 0).
4. Independent room controls (Living Room demo_living_room and Bedroom demo_bedroom).
5. Unpowered device LED guard: never turn on an AC solely to change its LED display.
6. Guest manual power-off and temperature override preservation.
7. Command deduplication / debounce avoiding repeating commands every 15 minutes.
"""

import unittest
from datetime import datetime, date, time, timedelta

from src.models import (
    Reservation,
    ReservationType,
    ScheduleDecision,
    SystemState,
    ACCommand,
    ACState,
    ACMode,
    ACFanSpeed,
)
from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.scheduler import HVACScheduleEngine
from src.safeguards import SafeguardEngine
from src.device_controller import MockACController
from src.orchestrator import AutomationOrchestrator
from src.state_store import StateStore
from src.notifier import NotificationDispatcher


class TestPhase5LedAndTransitions(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        # Ensure default Phase 5 configuration
        self.config.DEFAULT_LED_DISPLAY = True
        self.config.NIGHT_LED_DISPLAY = False
        self.config.BEEP_FEEDBACK_ENABLED = False
        self.config.LIVING_ROOM_LED_MODE = "SCHEDULE"
        self.config.BEDROOM_LED_MODE = "SCHEDULE"
        self.config.DRY_RUN = True
        self.config.TCL_READ_ONLY = True

        self.lr_id = self.config.TCL_LIVING_ROOM_ID  # demo_living_room
        self.br_id = self.config.TCL_BEDROOM_ID       # demo_bedroom

        self.scheduler = HVACScheduleEngine(config=self.config)
        self.safeguards = SafeguardEngine(config=self.config)

        # Standard active confirmed reservation covering today
        self.active_res = Reservation(
            uid="phase5-guest-stay",
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 25),
            summary="Reserved (Guest)",
            reservation_type=ReservationType.CONFIRMED,
            raw_status="CONFIRMED"
        )

    # -------------------------------------------------------------------------
    # 1. Day / Night Schedule & Transition Tests
    # -------------------------------------------------------------------------

    def test_daytime_schedule_evaluates_led_on_and_beep_off(self):
        """Between 07:00 and 23:00, running ACs must have LED ON (screen: 1) and Beep OFF (beepSwitch: 0)."""
        day_times = [
            time(7, 0),    # Morning start
            time(8, 30),   # Mid-morning
            time(10, 0),   # Day start
            time(14, 0),   # Mid-day
            time(17, 0),   # Evening start
            time(22, 59),  # 1 minute before night
        ]

        for t in day_times:
            dt = datetime.combine(date(2026, 10, 22), t, tzinfo=TIMEZONE_SOFIA)
            decision = self.scheduler.evaluate([self.active_res], current_dt=dt)

            self.assertTrue(decision.target_power, f"Power should be True at {t}")
            self.assertTrue(decision.target_display_led, f"Daytime LED should be True at {t}")
            self.assertFalse(decision.target_beep, f"Beep should be False at {t}")

            for dev_id in (self.lr_id, self.br_id):
                cmd = decision.commands_to_send[dev_id]
                self.assertTrue(cmd.power)
                self.assertTrue(cmd.display_led)
                self.assertFalse(cmd.beep_feedback)

                payload = cmd.to_shadow_payload()["state"]["desired"]
                self.assertEqual(payload["screen"], 1, f"Shadow screen must be 1 at {t}")
                self.assertEqual(payload["beepSwitch"], 0, f"Shadow beepSwitch must be 0 at {t}")

    def test_night_schedule_evaluates_led_off_quiet_fan_and_20c(self):
        """Between 23:00 and 07:00, running ACs must have 20°C, Quiet fan, LED OFF (screen: 0), Beep OFF."""
        night_times = [
            time(23, 0),   # Night start transition
            time(23, 15),  # 15 minutes in
            time(1, 0),    # Deep night
            time(3, 30),   # Mid night
            time(6, 59),   # 1 minute before morning
        ]

        for t in night_times:
            dt = datetime.combine(date(2026, 10, 22), t, tzinfo=TIMEZONE_SOFIA)
            decision = self.scheduler.evaluate([self.active_res], current_dt=dt)

            self.assertEqual(decision.system_state, SystemState.OCCUPIED_NIGHT, f"State at {t}")
            self.assertTrue(decision.target_power)
            self.assertEqual(decision.target_temperature, 20.0, f"Night temp at {t}")
            self.assertEqual(decision.target_fan_speed, ACFanSpeed.QUIET, f"Night fan at {t}")
            self.assertFalse(decision.target_display_led, f"Night LED at {t}")
            self.assertFalse(decision.target_beep, f"Night beep at {t}")

            for dev_id in (self.lr_id, self.br_id):
                cmd = decision.commands_to_send[dev_id]
                self.assertTrue(cmd.power)
                self.assertEqual(cmd.target_temperature, 20.0)
                self.assertEqual(cmd.fan_speed, ACFanSpeed.QUIET)
                self.assertFalse(cmd.display_led)
                self.assertFalse(cmd.beep_feedback)

                payload = cmd.to_shadow_payload()["state"]["desired"]
                self.assertEqual(payload["screen"], 0)
                self.assertEqual(payload["windSpeed7Gear"], 1)  # Quiet = 1
                self.assertEqual(payload["beepSwitch"], 0)
                self.assertEqual(payload["targetTemperature"], 20.0)

    def test_transition_at_2300_from_evening_to_night(self):
        """At 23:00 transition: Evening (23°C, Mid fan, LED ON) -> Night (20°C, Quiet fan, LED OFF)."""
        t_pre = datetime(2026, 10, 22, 22, 59, tzinfo=TIMEZONE_SOFIA)
        t_post = datetime(2026, 10, 22, 23, 0, tzinfo=TIMEZONE_SOFIA)

        dec_pre = self.scheduler.evaluate([self.active_res], current_dt=t_pre)
        dec_post = self.scheduler.evaluate([self.active_res], current_dt=t_post)

        self.assertEqual(dec_pre.system_state, SystemState.OCCUPIED_EVENING)
        self.assertEqual(dec_pre.target_temperature, 23.0)
        self.assertEqual(dec_pre.target_fan_speed, ACFanSpeed.MID)
        self.assertTrue(dec_pre.target_display_led)

        self.assertEqual(dec_post.system_state, SystemState.OCCUPIED_NIGHT)
        self.assertEqual(dec_post.target_temperature, 20.0)
        self.assertEqual(dec_post.target_fan_speed, ACFanSpeed.QUIET)
        self.assertFalse(dec_post.target_display_led)
        self.assertFalse(dec_post.target_beep)

    def test_transition_at_0700_from_night_to_morning(self):
        """At 07:00 transition: Night (20°C, Quiet, LED OFF) -> Morning (23°C, Mid fan, LED ON)."""
        t_pre = datetime(2026, 10, 23, 6, 59, tzinfo=TIMEZONE_SOFIA)
        t_post = datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA)

        dec_pre = self.scheduler.evaluate([self.active_res], current_dt=t_pre)
        dec_post = self.scheduler.evaluate([self.active_res], current_dt=t_post)

        self.assertEqual(dec_pre.system_state, SystemState.OCCUPIED_NIGHT)
        self.assertEqual(dec_pre.target_temperature, 20.0)
        self.assertEqual(dec_pre.target_fan_speed, ACFanSpeed.QUIET)
        self.assertFalse(dec_pre.target_display_led)

        self.assertEqual(dec_post.system_state, SystemState.OCCUPIED_MORNING)
        self.assertEqual(dec_post.target_temperature, 23.0)
        self.assertEqual(dec_post.target_fan_speed, ACFanSpeed.MID)
        self.assertTrue(dec_post.target_display_led)
        self.assertFalse(dec_post.target_beep)

    # -------------------------------------------------------------------------
    # 2. Unpowered Device LED Guard
    # -------------------------------------------------------------------------

    def test_unpowered_device_never_turned_on_solely_for_led(self):
        """An unpowered AC (power=False) must NOT be powered on merely because LED status changed."""
        t_now = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)

        # Unpowered AC
        current_states = {
            self.lr_id: ACState(
                device_id=self.lr_id,
                power=False,
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                display_led=False,
                is_online=True
            )
        }

        # Attempt to send a command with power=True solely to change LED
        led_cmd = ACCommand(
            device_id=self.lr_id,
            power=True,
            target_temperature=21.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            display_led=True,
            reason="LED screen update test"
        )
        dec = ScheduleDecision(
            timestamp=t_now,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=self.active_res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="LED test",
            commands_to_send={self.lr_id: led_cmd}
        )

        results = self.safeguards.filter_and_validate(
            decision=dec,
            current_states=current_states,
            now=t_now,
            is_transition_tick=False
        )

        should_send_lr, cmd_lr, rationale_lr = results[self.lr_id]
        self.assertFalse(should_send_lr)
        self.assertIn("Will not power ON unpowered AC", rationale_lr)

    def test_unoccupied_or_shutdown_devices_keep_led_off(self):
        """During CHECKOUT_SHUTDOWN and IDLE_UNOCCUPIED, power is False and display_led is False."""
        t_checkout = datetime(2026, 10, 22, 11, 30, tzinfo=TIMEZONE_SOFIA)
        dec_shutdown = self.scheduler._build_shutdown_decision(t_checkout, self.active_res, "Checkout")

        self.assertFalse(dec_shutdown.target_power)
        self.assertFalse(dec_shutdown.target_display_led)
        for cmd in dec_shutdown.commands_to_send.values():
            self.assertFalse(cmd.power)
            self.assertFalse(cmd.display_led)

        t_idle = datetime(2026, 10, 22, 12, 0, tzinfo=TIMEZONE_SOFIA)
        dec_idle = self.scheduler._build_idle_decision(t_idle, "Idle unoccupied")
        self.assertFalse(dec_idle.target_power)
        self.assertFalse(dec_idle.target_display_led)
        for cmd in dec_idle.commands_to_send.values():
            self.assertFalse(cmd.power)
            self.assertFalse(cmd.display_led)

    # -------------------------------------------------------------------------
    # 3. Guest Manual Overrides Preservation
    # -------------------------------------------------------------------------

    def test_guest_manual_power_off_preserved_across_ticks(self):
        """If a guest manually powers OFF an AC, it must NOT be turned back on every 15 minutes."""
        now = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.scheduler.evaluate([self.active_res], current_dt=now)

        # Record prior command (automation had turned on the unit at 21°C)
        prior_cmd = decision.commands_to_send[self.lr_id]
        self.safeguards.record_applied_command(prior_cmd)

        # Guest manually switched off Living Room AC via remote
        current_states = {
            self.lr_id: ACState(
                device_id=self.lr_id,
                power=False,  # Manually OFF
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                display_led=False,
                is_online=True
            ),
            self.br_id: ACState(
                device_id=self.br_id,
                power=True,
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                display_led=True,
                is_online=True
            )
        }

        # Cycle at 14:15 (non-transition tick)
        t_1415 = now + timedelta(minutes=15)
        results = self.safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=t_1415,
            is_transition_tick=False
        )

        should_send, cmd, rationale = results[self.lr_id]
        self.assertFalse(should_send)
        self.assertIn("Guest manually turned OFF", rationale)

        # Cycle at 14:30
        t_1430 = now + timedelta(minutes=30)
        results2 = self.safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=t_1430,
            is_transition_tick=False
        )
        self.assertFalse(results2[self.lr_id][0])
        self.assertIn("Guest manually turned OFF", results2[self.lr_id][2])

    def test_guest_manual_temp_preserved_within_daypart_resets_at_transition(self):
        """Guest manual temp (e.g. 24°C during OCCUPIED_DAY) is preserved until daypart transition."""
        now_day = datetime(2026, 10, 22, 12, 0, tzinfo=TIMEZONE_SOFIA)
        dec_day = self.scheduler.evaluate([self.active_res], current_dt=now_day)

        # Prior command was 21°C
        prior_cmd = dec_day.commands_to_send[self.lr_id]
        self.safeguards.record_applied_command(prior_cmd)

        # Guest set temp to 24.5°C
        current_states = {
            self.lr_id: ACState(
                device_id=self.lr_id,
                power=True,
                target_temperature=24.5,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                display_led=True,
                is_online=True
            )
        }

        # 12:15 tick (non-transition) -> Guest temp preserved!
        results = self.safeguards.filter_and_validate(
            decision=dec_day,
            current_states=current_states,
            now=now_day + timedelta(minutes=15),
            is_transition_tick=False
        )
        self.assertFalse(results[self.lr_id][0])
        self.assertIn("Guest manual override preserved", results[self.lr_id][2])

        # 17:00 evening transition tick -> resets to scheduled 23°C!
        now_evening = datetime(2026, 10, 22, 17, 0, tzinfo=TIMEZONE_SOFIA)
        dec_evening = self.scheduler.evaluate([self.active_res], current_dt=now_evening)

        results_trans = self.safeguards.filter_and_validate(
            decision=dec_evening,
            current_states=current_states,
            now=now_evening,
            is_transition_tick=True
        )
        should_send, cmd_trans, _ = results_trans[self.lr_id]
        self.assertTrue(should_send)
        self.assertEqual(cmd_trans.target_temperature, 23.0)

    # -------------------------------------------------------------------------
    # 4. Independent Per-Room LED Modes
    # -------------------------------------------------------------------------

    def test_independent_per_room_led_mode_always_off(self):
        """Living Room set to ALWAYS_OFF must remain dark during the day while Bedroom follows SCHEDULE."""
        self.config.LIVING_ROOM_LED_MODE = "ALWAYS_OFF"
        self.config.BEDROOM_LED_MODE = "SCHEDULE"
        custom_scheduler = HVACScheduleEngine(config=self.config)

        # Midday at 14:00 (Daytime)
        t_day = datetime(2026, 10, 22, 14, 0, tzinfo=TIMEZONE_SOFIA)
        dec = custom_scheduler.evaluate([self.active_res], current_dt=t_day)

        lr_cmd = dec.commands_to_send[self.lr_id]
        br_cmd = dec.commands_to_send[self.br_id]

        # Living Room must be dark (ALWAYS_OFF)
        self.assertFalse(lr_cmd.display_led, "Living Room must have display_led=False when ALWAYS_OFF")
        self.assertEqual(lr_cmd.to_shadow_payload()["state"]["desired"]["screen"], 0)

        # Bedroom must be lit (SCHEDULE during daytime)
        self.assertTrue(br_cmd.display_led, "Bedroom must have display_led=True during daytime schedule")
        self.assertEqual(br_cmd.to_shadow_payload()["state"]["desired"]["screen"], 1)

    def test_independent_per_room_led_mode_always_on(self):
        """Bedroom set to ALWAYS_ON must remain lit at night while Living Room follows SCHEDULE (dark at night)."""
        self.config.LIVING_ROOM_LED_MODE = "SCHEDULE"
        self.config.BEDROOM_LED_MODE = "ALWAYS_ON"
        custom_scheduler = HVACScheduleEngine(config=self.config)

        # Night at 02:00
        t_night = datetime(2026, 10, 22, 2, 0, tzinfo=TIMEZONE_SOFIA)
        dec = custom_scheduler.evaluate([self.active_res], current_dt=t_night)

        lr_cmd = dec.commands_to_send[self.lr_id]
        br_cmd = dec.commands_to_send[self.br_id]

        # Living Room follows SCHEDULE: dark at night
        self.assertFalse(lr_cmd.display_led)
        self.assertEqual(lr_cmd.to_shadow_payload()["state"]["desired"]["screen"], 0)

        # Bedroom is ALWAYS_ON
        self.assertTrue(br_cmd.display_led)
        self.assertEqual(br_cmd.to_shadow_payload()["state"]["desired"]["screen"], 1)

    # -------------------------------------------------------------------------
    # 5. Debounce & Command Deduplication (15-minute safety)
    # -------------------------------------------------------------------------

    def test_debounce_avoids_repeating_commands_when_already_active(self):
        """When hardware is already in target state (power, temp, mode, fan, LED, beep), debounce skips dispatch."""
        now = datetime(2026, 10, 22, 11, 0, tzinfo=TIMEZONE_SOFIA)
        decision = self.scheduler.evaluate([self.active_res], current_dt=now)

        # Hardware reports matching desired state
        current_states = {
            self.lr_id: ACState(
                device_id=self.lr_id,
                power=True,
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                display_led=True,
                beep_feedback=False,
                is_online=True
            ),
            self.br_id: ACState(
                device_id=self.br_id,
                power=True,
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                display_led=True,
                beep_feedback=False,
                is_online=True
            )
        }

        # Non-transition tick at 11:15
        results = self.safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=now + timedelta(minutes=15),
            is_transition_tick=False
        )

        for dev_id in (self.lr_id, self.br_id):
            should_send, _, rationale = results[dev_id]
            self.assertFalse(should_send, f"{dev_id} must be skipped by debounce")
            self.assertIn("Debounce", rationale)

    def test_nighttime_debounce_avoids_repeating_led_off_commands(self):
        """During the night, once LEDs are OFF, debounce skips subsequent ticks every 15 minutes."""
        now_night = datetime(2026, 10, 22, 23, 15, tzinfo=TIMEZONE_SOFIA)
        decision = self.scheduler.evaluate([self.active_res], current_dt=now_night)

        current_states = {
            self.lr_id: ACState(
                device_id=self.lr_id,
                power=True,
                target_temperature=20.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.QUIET,
                display_led=False,
                beep_feedback=False,
                is_online=True
            ),
            self.br_id: ACState(
                device_id=self.br_id,
                power=True,
                target_temperature=20.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.QUIET,
                display_led=False,
                beep_feedback=False,
                is_online=True
            )
        }

        results = self.safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=now_night,
            is_transition_tick=False
        )

        for dev_id in (self.lr_id, self.br_id):
            should_send, _, rationale = results[dev_id]
            self.assertFalse(should_send)
            self.assertIn("Debounce", rationale)

    # -------------------------------------------------------------------------
    # 6. Physical Hardware Identity Verification
    # -------------------------------------------------------------------------

    def test_physical_device_mapping_strictly_locked(self):
        """Verify that Living Room resolves to demo_living_room and Bedroom to demo_bedroom."""
        self.assertEqual(self.config.TCL_LIVING_ROOM_ID, "demo_living_room")
        self.assertEqual(self.config.TCL_BEDROOM_ID, "demo_bedroom")
        self.assertIn("demo_living_room", self.config.DEVICE_IDS)
        self.assertIn("demo_bedroom", self.config.DEVICE_IDS)


if __name__ == "__main__":
    unittest.main()
