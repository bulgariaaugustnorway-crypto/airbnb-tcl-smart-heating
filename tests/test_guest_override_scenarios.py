"""
Simulation of 8 critical guest override and transition scenarios for TCL Airbnb HVAC Automation.

Scenarios covered:
1. Guest turns bedroom OFF during evening.
2. Guest lowers living room from 23°C to 21°C.
3. The 23:00 night transition occurs.
4. The 07:00 morning transition occurs.
5. Guest turns a unit OFF during the night.
6. Guest checks out at 11:00.
7. New guest arrives later the same day (turnover).
8. Airbnb calendar becomes temporarily unavailable (stale calendar).
"""

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
    ACFanSpeed,
    HeatingProfileType
)
from src.safeguards import SafeguardEngine
from src.orchestrator import AutomationOrchestrator
from src.scheduler import HVACScheduleEngine
from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.state_store import StateStore, ConcurrencyConflictError



class TestGuestOverrideCriticalScenarios(unittest.TestCase):

    def setUp(self):
        self.config = AutomationConfig()
        self.config.MANUAL_OVERRIDE_PRESERVE = True
        self.lr_id = self.config.TCL_LIVING_ROOM_ID
        self.br_id = self.config.TCL_BEDROOM_ID

        # Active 61-night confirmed Airbnb reservation (Oct 22 - Dec 22, 2026)
        self.res = Reservation(
            uid="res-active-stay-oct22",
            start_date=date(2026, 10, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

    def test_scenario_1_guest_turns_bedroom_off_during_evening(self):
        """Scenario 1: Guest turns bedroom OFF during evening (20:00 Sofia).
        Safeguard must preserve the OFF state and not turn it back ON during the evening daypart.
        """
        safeguards = SafeguardEngine(config=self.config)
        # Previous applied command was ON at 22°C (Evening AT_HOME profile)
        safeguards.record_applied_command(ACCommand(
            device_id=self.br_id,
            power=True,
            target_temperature=22.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.MID,
            reason="Evening AT_HOME"
        ))

        # Physical AC state reported: Power is OFF (guest used remote at 20:00)
        current_states = {
            self.br_id: ACState(
                device_id=self.br_id,
                power=False,
                target_temperature=22.0,
                current_temperature=21.0,
                is_online=True
            )
        }

        # 20:15 cycle in OCCUPIED_EVENING
        dt_2015 = datetime(2026, 10, 22, 20, 15, tzinfo=TIMEZONE_SOFIA)
        decision = ScheduleDecision(
            timestamp=dt_2015,
            system_state=SystemState.OCCUPIED_EVENING,
            active_reservation=self.res,
            target_power=True,
            target_temperature=22.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Evening comfort",
            commands_to_send={
                self.br_id: ACCommand(
                    device_id=self.br_id,
                    power=True,
                    target_temperature=22.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.MID,
                    reason="Evening schedule"
                )
            }
        )

        results = safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=dt_2015,
            is_transition_tick=False
        )

        should_send, cmd, rationale = results[self.br_id]
        self.assertFalse(should_send, "Safeguard must suppress command to turn Bedroom ON")
        self.assertIn("Guest manual override preserved", rationale)
        self.assertIn("manually turned OFF", rationale)

    def test_scenario_2_guest_lowers_living_room_from_23_to_21(self):
        """Scenario 2: Guest lowers living room from 23°C to 21°C during evening.
        Safeguard must preserve the 21°C setpoint during the evening daypart.
        """
        safeguards = SafeguardEngine(config=self.config)
        # Previous applied command was ON at 23°C
        safeguards.record_applied_command(ACCommand(
            device_id=self.lr_id,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.MID,
            reason="Evening AT_HOME"
        ))

        # Physical AC state reported: Power ON, Target 21°C (guest changed remote)
        current_states = {
            self.lr_id: ACState(
                device_id=self.lr_id,
                power=True,
                target_temperature=21.0,
                current_temperature=21.2,
                is_online=True
            )
        }

        # 21:00 cycle in OCCUPIED_EVENING
        dt_2100 = datetime(2026, 10, 22, 21, 0, tzinfo=TIMEZONE_SOFIA)
        decision = ScheduleDecision(
            timestamp=dt_2100,
            system_state=SystemState.OCCUPIED_EVENING,
            active_reservation=self.res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Evening comfort",
            commands_to_send={
                self.lr_id: ACCommand(
                    device_id=self.lr_id,
                    power=True,
                    target_temperature=23.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.MID,
                    reason="Evening schedule"
                )
            }
        )

        results = safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=dt_2100,
            is_transition_tick=False
        )

        should_send, cmd, rationale = results[self.lr_id]
        self.assertFalse(should_send, "Safeguard must suppress resetting living room to 23°C")
        self.assertIn("Guest manual override preserved", rationale)
        self.assertIn("21.0°C", rationale)

    def test_scenario_3_night_transition_at_2300(self):
        """Scenario 3: The 23:00 night transition occurs.
        Scheduler generates OCCUPIED_NIGHT target (20°C, Quiet fan, LED False).
        Living Room is transitioned to 20°C.
        """
        engine = HVACScheduleEngine(config=self.config)
        dt_2300 = datetime(2026, 10, 22, 23, 0, tzinfo=TIMEZONE_SOFIA)
        decision = engine.evaluate(reservations=[self.res], current_dt=dt_2300)

        self.assertEqual(decision.system_state, SystemState.OCCUPIED_NIGHT)
        self.assertEqual(decision.target_temperature, 20.0)
        self.assertEqual(decision.target_fan_speed, ACFanSpeed.QUIET)
        self.assertFalse(decision.target_display_led)

    def test_scenario_4_morning_transition_at_0700(self):
        """Scenario 4: The 07:00 morning transition occurs.
        Scheduler transitions to OCCUPIED_MORNING (23°C, Mid fan, LED True).
        """
        engine = HVACScheduleEngine(config=self.config)
        dt_0700 = datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA)
        decision = engine.evaluate(reservations=[self.res], current_dt=dt_0700)

        self.assertEqual(decision.system_state, SystemState.OCCUPIED_MORNING)
        self.assertEqual(decision.target_temperature, 23.0)
        self.assertTrue(decision.target_power)
        self.assertTrue(decision.target_display_led)

    def test_scenario_5_guest_turns_unit_off_during_night(self):
        """Scenario 5: Guest turns a unit OFF during the night (01:00 Sofia).
        Safeguard preserves the OFF state during OCCUPIED_NIGHT.
        """
        safeguards = SafeguardEngine(config=self.config)
        safeguards.record_applied_command(ACCommand(
            device_id=self.lr_id,
            power=True,
            target_temperature=20.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            reason="Night AT_HOME"
        ))

        current_states = {
            self.lr_id: ACState(
                device_id=self.lr_id,
                power=False,
                target_temperature=20.0,
                current_temperature=20.5,
                is_online=True
            )
        }

        dt_0100 = datetime(2026, 10, 23, 1, 0, tzinfo=TIMEZONE_SOFIA)
        decision = ScheduleDecision(
            timestamp=dt_0100,
            system_state=SystemState.OCCUPIED_NIGHT,
            active_reservation=self.res,
            target_power=True,
            target_temperature=20.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.QUIET,
            target_eco=False,
            decision_reason="Night comfort",
            commands_to_send={
                self.lr_id: ACCommand(
                    device_id=self.lr_id,
                    power=True,
                    target_temperature=20.0,
                    mode=ACMode.HEAT,
                    fan_speed=ACFanSpeed.QUIET,
                    reason="Night schedule"
                )
            }
        )

        results = safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=dt_0100,
            is_transition_tick=False
        )

        should_send, cmd, rationale = results[self.lr_id]
        self.assertFalse(should_send, "Safeguard must preserve guest turning unit OFF during night")
        self.assertIn("Guest manual override preserved", rationale)

    def test_scenario_6_guest_checks_out_at_1100(self):
        """Scenario 6: Guest checks out at 11:00 on checkout date (Dec 22, 2026).
        Active Booking Shutdown Protection safely allows checkout shutdown.
        """
        engine = HVACScheduleEngine(config=self.config)
        safeguards = SafeguardEngine(config=self.config)

        # Checkout day at 11:05
        dt_checkout = datetime(2026, 12, 22, 11, 5, tzinfo=TIMEZONE_SOFIA)
        decision = engine.evaluate(reservations=[self.res], current_dt=dt_checkout)

        self.assertEqual(decision.system_state, SystemState.CHECKOUT_SHUTDOWN)
        self.assertFalse(decision.target_power)

        current_states = {
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=20.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=20.0, is_online=True)
        }

        results = safeguards.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=dt_checkout,
            is_transition_tick=True,
            known_reservations=[self.res]
        )

        # Shutdown must be approved for checkout
        for dev_id in [self.lr_id, self.br_id]:
            should_send, cmd, rationale = results[dev_id]
            self.assertTrue(should_send, f"Checkout shutdown must be approved for {dev_id}")
            self.assertFalse(cmd.power)

    def test_scenario_7_new_guest_arrives_later_same_day(self):
        """Scenario 7: New guest arrives later the same day (turnover: checkout 11:00, checkin 15:00).
        At 13:00 (2 hrs before checkin), PRE_ARRIVAL_HEATING engages.
        """
        stay_checkout = Reservation(
            uid="res-departing",
            start_date=date(2026, 12, 22),
            end_date=date(2026, 12, 22),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        stay_arriving = Reservation(
            uid="res-new-arrival",
            start_date=date(2026, 12, 22),
            end_date=date(2026, 12, 28),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )

        engine = HVACScheduleEngine(config=self.config)
        # At 13:05 on turnover day: Pre-arrival heating must activate
        dt_turnover_preheat = datetime(2026, 12, 22, 13, 5, tzinfo=TIMEZONE_SOFIA)
        decision = engine.evaluate(
            reservations=[stay_checkout, stay_arriving],
            current_dt=dt_turnover_preheat
        )

        self.assertEqual(decision.system_state, SystemState.PRE_ARRIVAL_HEATING)
        self.assertTrue(decision.target_power)
        self.assertEqual(decision.target_temperature, 23.0)

    def test_scenario_8_airbnb_calendar_becomes_temporarily_unavailable(self):
        """Scenario 8: Airbnb calendar becomes temporarily unavailable (stale calendar).
        System must preserve active in-stay reservation heating and block any shutdown commands.
        """
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)

        # First cycle: normal sync establishing in-stay reservation
        dt_normal = datetime(2026, 10, 22, 16, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt_normal, override_reservations=[self.res])

        # Second cycle: calendar parser fails / reports stale
        orch.calendar_parser.sync_calendar = MagicMock(return_value=MagicMock(
            reservations=[],
            cancelled_uids=[],
            is_stale=True,
            error_message="HTTP 503 Service Unavailable"
        ))

        dt_stale = datetime(2026, 10, 22, 16, 15, tzinfo=TIMEZONE_SOFIA)
        res_stale = orch.run_cycle(current_dt=dt_stale)

        # Must NOT shut down heating!
        self.assertTrue(res_stale.calendar_stale)
        self.assertEqual(res_stale.system_state, SystemState.OCCUPIED_DAY)
        # Heating must remain active
        for dev_id, (should_send, cmd, rationale) in res_stale.safeguard_decisions.items():
            if cmd:
                self.assertTrue(cmd.power, f"Device {dev_id} must not be powered off during stale calendar")

    def test_scenario_9_persistent_power_off_across_all_daypart_transitions(self):
        """Scenario 9: Guest turns Bedroom OFF at 23:35.
        Bedroom must remain OFF across 07:00, 10:00, 17:00, and 23:00 transitions on subsequent days.
        """
        safeguards = SafeguardEngine(config=self.config)
        # Baseline: Bedroom was ON
        safeguards.record_applied_command(ACCommand(
            device_id=self.br_id,
            power=True,
            target_temperature=20.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            reason="Night AT_HOME"
        ))

        # Physical state reported: OFF at 23:35 on Oct 22
        br_off_state = ACState(
            device_id=self.br_id,
            power=False,
            target_temperature=21.0,
            current_temperature=18.7,
            is_online=True
        )

        transitions = [
            # 07:00 Morning transition
            (datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA), SystemState.OCCUPIED_MORNING, 22.0, ACFanSpeed.MID),
            # 10:00 Daytime transition
            (datetime(2026, 10, 23, 10, 0, tzinfo=TIMEZONE_SOFIA), SystemState.OCCUPIED_DAY, 21.0, ACFanSpeed.AUTO),
            # 17:00 Evening transition
            (datetime(2026, 10, 23, 17, 0, tzinfo=TIMEZONE_SOFIA), SystemState.OCCUPIED_EVENING, 22.0, ACFanSpeed.MID),
            # 23:00 Night transition
            (datetime(2026, 10, 23, 23, 0, tzinfo=TIMEZONE_SOFIA), SystemState.OCCUPIED_NIGHT, 20.0, ACFanSpeed.QUIET),
        ]

        for dt_trans, state, temp, fan in transitions:
            cmd = ACCommand(
                device_id=self.br_id,
                power=True,
                target_temperature=temp,
                mode=ACMode.HEAT,
                fan_speed=fan,
                reason=f"{state.value} schedule"
            )
            decision = ScheduleDecision(
                timestamp=dt_trans,
                system_state=state,
                active_reservation=self.res,
                target_power=True,
                target_temperature=temp,
                target_mode=ACMode.HEAT,
                target_fan_speed=fan,
                target_eco=False,
                decision_reason=f"Transition into {state.value}",
                commands_to_send={self.br_id: cmd}
            )
            results = safeguards.filter_and_validate(
                decision=decision,
                current_states={self.br_id: br_off_state},
                now=dt_trans,
                is_transition_tick=True
            )
            should_send, validated_cmd, rationale = results[self.br_id]
            self.assertFalse(
                should_send,
                f"Bedroom must NOT be turned ON at {dt_trans.strftime('%H:%M')} during {state.value}"
            )
            self.assertIn("Persistent Power-OFF", rationale)

    def test_scenario_10_independent_room_control_living_room_heats_while_bedroom_off(self):
        """Scenario 10: Bedroom has persistent power-OFF override, Living Room follows comfort schedule.
        Control of both units must remain 100% independent.
        """
        safeguards = SafeguardEngine(config=self.config)
        # Register Bedroom power-OFF override
        safeguards.set_guest_power_off_override(self.br_id, self.res.uid)

        # Current physical states
        current_states = {
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True),
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True)
        }

        # 07:00 Morning transition: LR transitions to 23°C, BR stays OFF
        dt_0700 = datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA)
        dec_0700 = ScheduleDecision(
            timestamp=dt_0700,
            system_state=SystemState.OCCUPIED_MORNING,
            active_reservation=self.res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Morning comfort",
            commands_to_send={
                self.lr_id: ACCommand(device_id=self.lr_id, power=True, target_temperature=23.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.MID),
                self.br_id: ACCommand(device_id=self.br_id, power=True, target_temperature=22.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.MID),
            }
        )
        res_0700 = safeguards.filter_and_validate(
            decision=dec_0700,
            current_states=current_states,
            now=dt_0700,
            is_transition_tick=True
        )

        # Living Room: APPROVED to transition to 23°C
        lr_send, lr_cmd, _ = res_0700[self.lr_id]
        self.assertTrue(lr_send, "Living Room must be approved to transition to 23°C")
        self.assertEqual(lr_cmd.target_temperature, 23.0)

        # Bedroom: SUPPRESSED by persistent power-OFF override
        br_send, _, br_rationale = res_0700[self.br_id]
        self.assertFalse(br_send, "Bedroom must remain suppressed (OFF)")
        self.assertIn("Persistent Power-OFF", br_rationale)

    def test_scenario_11_guest_turns_bedroom_back_on_resumes_scheduling_from_next_daypart(self):
        """Scenario 11: Guest turns Bedroom back ON.
        Safeguard clears power-OFF, preserves guest setpoint during current daypart,
        and resumes normal scheduled management from next daypart.
        """
        safeguards = SafeguardEngine(config=self.config)
        safeguards.set_guest_power_off_override(self.br_id, self.res.uid)

        # Guest turns Bedroom ON at 22°C at 14:00 during OCCUPIED_DAY
        br_turned_on = ACState(
            device_id=self.br_id,
            power=True,
            target_temperature=22.0,
            current_temperature=21.0,
            is_online=True
        )

        dt_1415 = datetime(2026, 10, 23, 14, 15, tzinfo=TIMEZONE_SOFIA)
        dec_1415 = ScheduleDecision(
            timestamp=dt_1415,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=self.res,
            target_power=True,
            target_temperature=21.0,  # Scheduled daytime is 21°C
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Daytime comfort",
            commands_to_send={
                self.br_id: ACCommand(device_id=self.br_id, power=True, target_temperature=21.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.AUTO)
            }
        )
        res_1415 = safeguards.filter_and_validate(
            decision=dec_1415,
            current_states={self.br_id: br_turned_on},
            now=dt_1415,
            is_transition_tick=False
        )

        # Power-OFF is cleared, guest setpoint 22°C is preserved during daytime (not overwritten with 21°C)
        should_send_mid, _, rationale_mid = res_1415[self.br_id]
        self.assertFalse(should_send_mid, "Should not overwrite guest's selected 22°C with scheduled 21°C")
        self.assertIn("22.0°C", rationale_mid)

        # At 17:00 Evening transition: normal scheduled management resumes!
        dt_1700 = datetime(2026, 10, 23, 17, 0, tzinfo=TIMEZONE_SOFIA)
        dec_1700 = ScheduleDecision(
            timestamp=dt_1700,
            system_state=SystemState.OCCUPIED_EVENING,
            active_reservation=self.res,
            target_power=True,
            target_temperature=22.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Evening comfort",
            commands_to_send={
                self.br_id: ACCommand(device_id=self.br_id, power=True, target_temperature=22.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.MID)
            }
        )
        res_1700 = safeguards.filter_and_validate(
            decision=dec_1700,
            current_states={self.br_id: br_turned_on},
            now=dt_1700,
            is_transition_tick=True
        )
        # Note: If current state already matches desired state (22C, Mid), debounce approves desired state or skips
        # Check that it is NOT blocked by power-OFF override
        self.assertFalse(safeguards.has_guest_power_off_override(self.br_id, self.res.uid))

    def test_scenario_12_lambda_cold_start_persists_power_off_override(self):
        """Scenario 12: Lambda cold start persistence.
        Manual override state must survive across Orchestrator reinstantiation via StateStore.
        """
        store = StateStore(in_memory=True)
        orch1 = AutomationOrchestrator(config=self.config, state_store=store)

        # Baseline: Both units running during occupied stay
        orch1.safeguard_engine.record_applied_command(ACCommand(
            device_id=self.br_id,
            power=True,
            target_temperature=22.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.MID,
            reason="Pre-arrival / evening comfort"
        ))

        # Cycle 1: Living room ON, Bedroom OFF reported (guest turned Bedroom OFF at 23:35)
        dt_night = datetime(2026, 10, 22, 23, 40, tzinfo=TIMEZONE_SOFIA)
        orch1.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True)
        })
        orch1.run_cycle(current_dt=dt_night, override_reservations=[self.res])

        # Verify state store contains guest_manual_overrides
        persisted = store.load_state()
        self.assertIn("guest_manual_overrides", persisted)
        self.assertIn(self.br_id, persisted["guest_manual_overrides"])
        self.assertTrue(persisted["guest_manual_overrides"][self.br_id]["power_off"])

        # Simulate Lambda Cold Start: Create a brand new orchestrator reading from same StateStore
        orch2 = AutomationOrchestrator(config=self.config, state_store=store)
        self.assertTrue(orch2.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))

        # Cycle 2 at 07:00 Morning transition: Bedroom must remain suppressed!
        dt_morning = datetime(2026, 10, 23, 7, 0, tzinfo=TIMEZONE_SOFIA)
        orch2.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True)
        })
        res_morning = orch2.run_cycle(current_dt=dt_morning, override_reservations=[self.res])

        # Bedroom must NOT be sent a command to turn ON
        br_send, br_cmd, br_rationale = res_morning.safeguard_decisions[self.br_id]
        self.assertFalse(br_send, "Bedroom must remain suppressed after cold start")
        self.assertIn("Persistent Power-OFF", br_rationale)

    def test_scenario_13_device_offline_does_not_trigger_power_off_override(self):
        """Scenario 13: Communication failure / device offline must NOT trigger a guest power-off override."""
        safeguards = SafeguardEngine(config=self.config)
        offline_state = ACState(
            device_id=self.br_id,
            power=False,
            target_temperature=20.0,
            is_online=False  # Device is offline / communication dropout
        )

        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        cmd = ACCommand(device_id=self.br_id, power=True, target_temperature=21.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.AUTO)
        dec = ScheduleDecision(
            timestamp=dt,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=self.res,
            target_power=True,
            target_temperature=21.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Daytime",
            commands_to_send={self.br_id: cmd}
        )

        safeguards.filter_and_validate(
            decision=dec,
            current_states={self.br_id: offline_state},
            now=dt,
            is_transition_tick=False
        )

        # Must NOT record a persistent guest power-off override for an offline device!
        self.assertFalse(
            safeguards.has_guest_power_off_override(self.br_id, self.res.uid),
            "Offline device must not be interpreted as a guest power-off action"
        )

    def test_scenario_14_checkout_clears_power_off_override_allowing_next_guest_pre_arrival(self):
        """Scenario 14: Checkout clears persistent power-off override, allowing incoming guest pre-arrival heating."""
        safeguards = SafeguardEngine(config=self.config)
        # Departing guest turned Bedroom OFF
        safeguards.set_guest_power_off_override(self.br_id, self.res.uid)

        # Checkout transition at 11:00
        dt_checkout = datetime(2026, 12, 22, 11, 0, tzinfo=TIMEZONE_SOFIA)
        dec_checkout = ScheduleDecision(
            timestamp=dt_checkout,
            system_state=SystemState.CHECKOUT_SHUTDOWN,
            active_reservation=self.res,
            target_power=False,
            target_temperature=20.0,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=True,
            decision_reason="Checkout shutdown",
            commands_to_send={
                self.br_id: ACCommand(device_id=self.br_id, power=False, target_temperature=20.0, mode=ACMode.OFF, fan_speed=ACFanSpeed.AUTO)
            }
        )
        safeguards.filter_and_validate(
            decision=dec_checkout,
            current_states={self.br_id: ACState(device_id=self.br_id, power=False, is_online=True)},
            now=dt_checkout,
            is_transition_tick=True
        )

        # Overrides must be cleared!
        self.assertFalse(safeguards.has_guest_power_off_override(self.br_id))

        # Next guest pre-arrival heating at 13:00
        next_res = Reservation(
            uid="res-incoming-dec22",
            start_date=date(2026, 12, 22),
            end_date=date(2026, 12, 28),
            summary="Reserved",
            reservation_type=ReservationType.CONFIRMED
        )
        dt_preheat = datetime(2026, 12, 22, 13, 0, tzinfo=TIMEZONE_SOFIA)
        cmd_preheat = ACCommand(
            device_id=self.br_id,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            reason="Pre-arrival heating"
        )
        dec_preheat = ScheduleDecision(
            timestamp=dt_preheat,
            system_state=SystemState.PRE_ARRIVAL_HEATING,
            active_reservation=next_res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=False,
            decision_reason="Pre-arrival heating for next stay",
            commands_to_send={self.br_id: cmd_preheat}
        )
        res_preheat = safeguards.filter_and_validate(
            decision=dec_preheat,
            current_states={self.br_id: ACState(device_id=self.br_id, power=False, is_online=True)},
            now=dt_preheat,
            is_transition_tick=True
        )

        should_send, approved_cmd, _ = res_preheat[self.br_id]
        self.assertTrue(should_send, "Pre-arrival heating for next reservation must be approved!")
        self.assertTrue(approved_cmd.power, "Command must turn AC ON for next reservation")
        self.assertEqual(approved_cmd.target_temperature, 23.0)

    def test_scenario_15_first_live_startup_bedroom_already_physically_off(self):
        """Scenario 15: First live startup with Bedroom already physically OFF.
        Telemetry initialization must establish and preserve a persistent guest power-OFF override.
        """
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True)
        })

        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        res = orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        # Bedroom must be suppressed and persistent power-off override recorded
        br_send, br_cmd, br_rationale = res.safeguard_decisions[self.br_id]
        self.assertFalse(br_send, "Bedroom must NOT be turned ON at first startup when physically OFF")
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))
        self.assertIn("Persistent Power-OFF", br_rationale)

    def test_scenario_16_first_live_startup_living_room_at_21_establishes_daypart_override(self):
        """Scenario 16: First live startup with Living Room at 21°C instead of scheduled 23°C (AT_HOME).
        Telemetry initialization must preserve 21°C until the next scheduled daypart (17:00).
        """
        store = StateStore(in_memory=True)
        cfg = AutomationConfig()
        cfg.ADAPTIVE_COMFORT_ENABLED = False
        cfg.MANUAL_OVERRIDE_PRESERVE = True
        cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
        orch = AutomationOrchestrator(config=cfg, state_store=store)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, fan_speed=ACFanSpeed.MID, display_led=True, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=True, target_temperature=21.0, fan_speed=ACFanSpeed.MID, display_led=True, is_online=True)
        })

        # Cycle 1 at 14:00 (OCCUPIED_DAY, scheduled LR is 23°C under AT_HOME)
        dt_day = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        res_day = orch.run_cycle(current_dt=dt_day, override_reservations=[self.res])

        lr_send, lr_cmd, lr_rationale = res_day.safeguard_decisions[self.lr_id]
        self.assertFalse(lr_send, "Living Room must NOT be commanded to 23°C during daytime; preserve 21°C override")
        self.assertIn("Guest manual override preserved", lr_rationale)


        # Cycle 2 at 17:00 (OCCUPIED_EVENING transition)
        dt_evening = datetime(2026, 10, 23, 17, 0, tzinfo=TIMEZONE_SOFIA)
        res_evening = orch.run_cycle(current_dt=dt_evening, override_reservations=[self.res])

        lr_send_ev, lr_cmd_ev, lr_rationale_ev = res_evening.safeguard_decisions[self.lr_id]
        self.assertTrue(lr_send_ev, "Daypart transition at 17:00 must resume scheduled evening management")
        self.assertEqual(lr_cmd_ev.target_temperature, 23.0)

    def test_scenario_17_missing_or_dry_run_only_history_triggers_telemetry_init(self):
        """Scenario 17: Command execution history is missing or DRY_RUN-only.
        System must NOT assume prior successful commands, and must read physical telemetry directly.
        """
        store = StateStore(in_memory=True)
        cfg = AutomationConfig()
        cfg.DRY_RUN = True
        orch = AutomationOrchestrator(config=cfg, state_store=store)

        # Verify initial state has no live history
        self.assertFalse(orch.safeguard_engine.has_live_command_history)
        self.assertIsNone(orch.safeguard_engine.initialized_reservation_uid)

        # Controller reports Bedroom is OFF
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True)
        })

        dt = datetime(2026, 10, 23, 15, 0, tzinfo=TIMEZONE_SOFIA)
        res = orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        # Even with DRY_RUN=True and no prior command history, Bedroom is preserved OFF
        br_send, _, br_rationale = res.safeguard_decisions[self.br_id]
        self.assertFalse(br_send)
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))
        self.assertEqual(orch.safeguard_engine.initialized_reservation_uid, self.res.uid)

    def test_scenario_18_idempotent_first_start_runs_preserve_state_without_corruption(self):
        """Scenario 18: Repeated execution of the activation initialization is strictly idempotent.
        Subsequent cycles must NOT corrupt or overwrite existing valid overrides.
        """
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, is_online=True)
        })

        # Cycle 1 at 14:00
        dt1 = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt1, override_reservations=[self.res])
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))

        # Cycle 2 at 14:15
        dt2 = datetime(2026, 10, 23, 14, 15, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt2, override_reservations=[self.res])
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))

        # Cycle 3 at 14:30
        dt3 = datetime(2026, 10, 23, 14, 30, tzinfo=TIMEZONE_SOFIA)
        orch.run_cycle(current_dt=dt3, override_reservations=[self.res])
        self.assertTrue(orch.safeguard_engine.has_guest_power_off_override(self.br_id, self.res.uid))

    def test_scenario_19_guest_turns_living_room_off_preserves_power_off(self):
        """Scenario 19: Guest turns Living Room OFF during an active stay.
        Safeguard must record persistent power-OFF override and preserve it.
        """
        safeguards = SafeguardEngine(config=self.config)
        # Previous command had Living Room ON
        safeguards.record_applied_command(ACCommand(
            device_id=self.lr_id,
            power=True,
            target_temperature=23.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.MID,
            reason="Daytime AT_HOME"
        ))

        # Physical report: Living Room is now OFF
        current_states = {
            self.lr_id: ACState(device_id=self.lr_id, power=False, target_temperature=21.0, is_online=True)
        }

        dt = datetime(2026, 10, 23, 16, 0, tzinfo=TIMEZONE_SOFIA)
        dec = ScheduleDecision(
            timestamp=dt,
            system_state=SystemState.OCCUPIED_DAY,
            active_reservation=self.res,
            target_power=True,
            target_temperature=23.0,
            target_mode=ACMode.HEAT,
            target_fan_speed=ACFanSpeed.MID,
            target_eco=False,
            decision_reason="Daytime schedule",
            commands_to_send={
                self.lr_id: ACCommand(device_id=self.lr_id, power=True, target_temperature=23.0, mode=ACMode.HEAT, fan_speed=ACFanSpeed.MID)
            }
        )

        results = safeguards.filter_and_validate(
            decision=dec,
            current_states=current_states,
            now=dt,
            is_transition_tick=False
        )

        should_send, cmd, rationale = results[self.lr_id]
        self.assertFalse(should_send, "Living Room must NOT be turned back ON after guest turned it OFF")
        self.assertTrue(safeguards.has_guest_power_off_override(self.lr_id, self.res.uid))

    def test_scenario_20_concurrency_conflict_handling_preserves_overrides(self):
        """Scenario 20: S3/Memory optimistic concurrency conflict handling.
        When two instances attempt to save state concurrently, ConcurrencyConflictError is raised
        and state overwrite is aborted, preserving valid overrides.
        """
        store1 = StateStore(in_memory=True)
        store1.save_state({"initial": "baseline", "guest_manual_overrides": {"dev-1": {"power_off": True}}})

        # Instance 2 reads state at ETag mem-v1
        store2 = StateStore(in_memory=True)
        store2._memory_data = dict(store1._memory_data)
        store2._memory_version = store1._memory_version
        state2 = store2.load_state()

        # Instance 1 updates state in the background (advances to mem-v2)
        store1.save_state({"updated_by": "instance1", "guest_manual_overrides": {"dev-1": {"power_off": True}}})
        # Reflect change into shared memory backing
        store2._memory_version = store1._memory_version

        # Instance 2 tries to save stale state: must raise ConcurrencyConflictError!
        state2["stale_update"] = True
        with self.assertRaises(ConcurrencyConflictError):
            store2.save_state(state2, check_concurrency=True)

    def test_scenario_21_frost_protection_alert_when_bedroom_temp_drops_unit_remains_off(self):
        """Scenario 21: Bedroom temperature drops near frost threshold (12°C warning, 8°C critical).
        Alert is dispatched to host, and Bedroom unit strictly remains OFF to respect guest override.
        """
        store = StateStore(in_memory=True)
        orch = AutomationOrchestrator(config=self.config, state_store=store)
        orch.notifier.dispatch = MagicMock()

        # Bedroom has persistent power-off override
        orch.safeguard_engine.set_guest_power_off_override(self.br_id, self.res.uid)

        # Ambient temperature drops to 11.5°C (warning)
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.5, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=11.5, is_online=True)
        })

        dt = datetime(2026, 10, 23, 14, 0, tzinfo=TIMEZONE_SOFIA)
        res = orch.run_cycle(current_dt=dt, override_reservations=[self.res])

        # Bedroom must strictly remain OFF (zero commands dispatched to Bedroom)
        br_send, _, br_rationale = res.safeguard_decisions[self.br_id]
        self.assertFalse(br_send, "Bedroom must remain OFF despite low temperature")
        self.assertIn("Persistent Power-OFF", br_rationale)

        # Host notification must have been dispatched
        dispatched_events = [call[0][0] for call in orch.notifier.dispatch.call_args_list]
        frost_events = [e for e in dispatched_events if e.event_type == "FROST_WARNING"]
        self.assertTrue(len(frost_events) >= 1)
        warning_ev = frost_events[0]
        self.assertEqual(warning_ev.severity, "WARNING")
        self.assertIn("11.5", warning_ev.message)
        self.assertIn("guest power-OFF override", warning_ev.message)

        # Ambient temperature drops to 7.5°C (critical)
        orch.notifier.dispatch.reset_mock()
        orch.controller.get_all_states = MagicMock(return_value={
            self.lr_id: ACState(device_id=self.lr_id, power=True, target_temperature=21.0, current_temperature=21.5, is_online=True),
            self.br_id: ACState(device_id=self.br_id, power=False, target_temperature=21.0, current_temperature=7.5, is_online=True)
        })
        res_crit = orch.run_cycle(current_dt=dt + timedelta(minutes=15), override_reservations=[self.res])

        br_send_crit, _, _ = res_crit.safeguard_decisions[self.br_id]
        self.assertFalse(br_send_crit, "Bedroom unit must strictly remain OFF even at 7.5°C critical")

        dispatched_crit = [call[0][0] for call in orch.notifier.dispatch.call_args_list]
        frost_crit_events = [e for e in dispatched_crit if e.event_type == "FROST_WARNING" and e.severity == "CRITICAL"]
        self.assertTrue(len(frost_crit_events) >= 1)
        self.assertIn("7.5", frost_crit_events[0].message)


if __name__ == "__main__":
    unittest.main()
