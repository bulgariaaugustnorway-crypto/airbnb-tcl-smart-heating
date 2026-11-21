"""Safeguard rules, state-drift debounce, and compressor protection engine."""

import logging
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional, List, Any

from src.models import ACCommand, ACState, ScheduleDecision, SystemState, Reservation
from src.config import AutomationConfig

_LOGGER = logging.getLogger(__name__)


class SafeguardEngine:
    """Enforces safety constraints, rate limits, and debouncing before sending commands."""

    def __init__(self, config: Optional[AutomationConfig] = None):
        self.config = config or AutomationConfig()
        self._last_power_toggle: Dict[str, datetime] = {}
        self._last_applied_command: Dict[str, ACCommand] = {}
        self._manual_overrides: Dict[str, Dict[str, Any]] = {}
        self._preferred_day_temperatures: Dict[str, Dict[str, Any]] = {}
        self.initialized_reservation_uid: Optional[str] = None
        self.has_live_command_history: bool = False

    def filter_and_validate(
        self,
        decision: ScheduleDecision,
        current_states: Dict[str, ACState],
        now: Optional[datetime] = None,
        is_transition_tick: bool = False,
        known_reservations: Optional[List[Reservation]] = None,
        calendar_stale: bool = False
    ) -> Dict[str, Tuple[bool, Optional[ACCommand], str]]:
        """
        Validate decision and determine which commands should actually be dispatched.
        Returns mapping: device_id -> (should_send, command_or_none, rationale)
        """
        eval_time = now or decision.timestamp
        results = {}

        # 0. System Paused Check: If system is PAUSED, strictly suppress all commands to preserve physical state
        if decision.system_state == SystemState.PAUSED:
            _LOGGER.info("System is in PAUSED mode: Suppressing all automatic HVAC commands to preserve physical state.")
            for dev_id in decision.commands_to_send:
                results[dev_id] = (
                    False,
                    None,
                    "Automation suspended by host (PAUSED mode); zero commands dispatched"
                )
            return results

        # 1. Critical Booking Guardrail: Never activate occupied profile without confirmed or eligible reservation
        is_occupied_state = decision.system_state in (
            SystemState.PRE_ARRIVAL_HEATING,
            SystemState.OCCUPIED_MORNING,
            SystemState.OCCUPIED_DAY,
            SystemState.OCCUPIED_EVENING,
            SystemState.OCCUPIED_NIGHT,
            SystemState.OWNER_TEST_HOLD,
        )
        if is_occupied_state and (
            not decision.active_reservation
            or not decision.active_reservation.is_occupancy_eligible(self.config.HEAT_DURING_BLOCKED_DATES)
        ):
            _LOGGER.critical(
                "VIOLATION DETECTED: Attempted to activate occupied schedule without confirmed or eligible reservation! Enforcing immediate shutdown."
            )
            for dev_id, cmd in decision.commands_to_send.items():
                results[dev_id] = (
                    False,
                    None,
                    "BLOCKED BY SAFEGUARD: No confirmed or eligible reservation exists for occupied schedule"
                )
            return results

        # 2. Per-device evaluation
        today = eval_time.date()
        curr_time = eval_time.time()
        pre_arrival_start = (
            datetime.combine(today, self.config.CHECKIN_TIME)
            - timedelta(hours=self.config.PRE_ARRIVAL_HOURS)
        ).time()

        active_res = decision.active_reservation
        active_uid = active_res.uid if (active_res and active_res.is_confirmed) else None

        # Clean up overrides if stay ended, system checked out, or vacancy
        if decision.system_state in (SystemState.CHECKOUT_SHUTDOWN, SystemState.IDLE_UNOCCUPIED) or not is_occupied_state or not active_uid:
            if self._manual_overrides or self._preferred_day_temperatures:
                _LOGGER.info("Clearing guest manual overrides and adaptive preferences due to checkout or vacancy transition.")
                self._manual_overrides.clear()
                self._preferred_day_temperatures.clear()
        else:
            # Clear overrides and preferences from previous reservations
            stale_devs = [
                d for d, ovr in self._manual_overrides.items()
                if ovr.get("reservation_uid") and ovr.get("reservation_uid") != active_uid
            ]
            for d in stale_devs:
                _LOGGER.info(
                    f"Clearing guest manual override for {d} (belonged to past reservation {self._manual_overrides[d].get('reservation_uid')})."
                )
                del self._manual_overrides[d]
            stale_prefs = [
                d for d, pref in self._preferred_day_temperatures.items()
                if pref.get("reservation_uid") and pref.get("reservation_uid") != active_uid
            ]
            for d in stale_prefs:
                _LOGGER.info(
                    f"Clearing guest preferred day temp for {d} (belonged to past reservation {self._preferred_day_temperatures[d].get('reservation_uid')})."
                )
                del self._preferred_day_temperatures[d]

        for dev_id, cmd in decision.commands_to_send.items():
            # 2-A. Active Booking Shutdown Protection Guardrail:
            # Under NO circumstances can an automatic Power=False command shut down heating during an active confirmed guest stay.
            if not cmd.power:
                # Check 1: Active decision explicitly carries a confirmed guest reservation (and not legitimate checkout shutdown)
                if (
                    decision.active_reservation
                    and decision.active_reservation.is_confirmed
                    and decision.system_state != SystemState.CHECKOUT_SHUTDOWN
                ):
                    _LOGGER.critical(
                        f"SAFEGUARD REJECTED SHUTDOWN: Attempted Power=False on {dev_id} during confirmed active reservation "
                        f"({decision.active_reservation.masked_uid}). Preserving guest heating."
                    )
                    results[dev_id] = (
                        False,
                        None,
                        f"BLOCKED BY SAFEGUARD: Automatic shutdown rejected during active confirmed guest reservation ({decision.active_reservation.masked_uid})"
                    )
                    continue

                # Check 2: Stale or unreliable calendar during ongoing confirmed guest stay
                if calendar_stale and known_reservations:
                    ongoing_stay = None
                    for r in known_reservations:
                        if r.is_confirmed and r.start_date <= today:
                            if r.end_date > today or (r.end_date == today and curr_time < self.config.CHECKOUT_TIME):
                                ongoing_stay = r
                                break
                    if ongoing_stay:
                        _LOGGER.critical(
                            f"SAFEGUARD REJECTED SHUTDOWN: Attempted Power=False on {dev_id} while calendar feed is stale "
                            f"during ongoing guest stay ({ongoing_stay.masked_uid}). Preserving guest heating."
                        )
                        results[dev_id] = (
                            False,
                            None,
                            f"BLOCKED BY SAFEGUARD: Automatic shutdown rejected. Calendar data is stale during ongoing guest stay ({ongoing_stay.masked_uid})"
                        )
                        continue

                # Check 3: Trustworthy reservation snapshot contains an active in-stay confirmed guest
                active_guest_in_snapshot = None
                if known_reservations:
                    for r in known_reservations:
                        if r.is_confirmed:
                            in_stay = (
                                (r.start_date < today < r.end_date)
                                or (r.start_date == today and curr_time >= pre_arrival_start)
                                or (r.end_date == today and curr_time < self.config.CHECKOUT_TIME)
                            )
                            if in_stay:
                                active_guest_in_snapshot = r
                                break

                if active_guest_in_snapshot:
                    _LOGGER.critical(
                        f"SAFEGUARD REJECTED SHUTDOWN: Attempted Power=False on {dev_id} while trustworthy snapshot confirms "
                        f"active in-stay guest ({active_guest_in_snapshot.masked_uid}). Preserving guest heating."
                    )
                    results[dev_id] = (
                        False,
                        None,
                        f"BLOCKED BY SAFEGUARD: Automatic shutdown rejected. Trustworthy snapshot confirms active in-stay guest ({active_guest_in_snapshot.masked_uid})"
                    )
                    continue

            curr = current_states.get(dev_id)
            if not curr:
                # If no current state known, allow command
                results[dev_id] = (True, cmd, "No prior state known; applying command")
                continue

            # 2a. Temperature Sanity Clamping
            if cmd.target_temperature < self.config.TEMP_MIN_SAFE:
                _LOGGER.warning(
                    f"Clamping {dev_id} temperature from {cmd.target_temperature}°C to safe minimum {self.config.TEMP_MIN_SAFE}°C"
                )
                cmd.target_temperature = self.config.TEMP_MIN_SAFE
            elif cmd.target_temperature > self.config.TEMP_MAX_SAFE:
                _LOGGER.warning(
                    f"Clamping {dev_id} temperature from {cmd.target_temperature}°C to safe maximum {self.config.TEMP_MAX_SAFE}°C"
                )
                cmd.target_temperature = self.config.TEMP_MAX_SAFE

            # 2b. Guest Manual Override Detection & Enforcement
            if (
                self.config.MANUAL_OVERRIDE_PRESERVE
                and is_occupied_state
                and active_uid
                and curr.is_online
            ):
                override = self._manual_overrides.get(dev_id, {})
                last_cmd = self._last_applied_command.get(dev_id)

                # Daypart transition: expire temporary temperature overrides from previous daypart
                if is_transition_tick:
                    if override.get("temp_override") is not None and override.get("temp_daypart") != decision.system_state.value:
                        _LOGGER.info(
                            f"Daypart transition into {decision.system_state.value}: Expiring temporary temperature override "
                            f"({override.get('temp_override')}°C) on {dev_id}. Resuming scheduled management."
                        )
                        override["temp_override"] = None
                        override["temp_daypart"] = None
                        self._manual_overrides[dev_id] = override

                # Detection of External Remote/Manual Changes:
                # In-stay states (excluding pre-arrival heating where guests have not arrived yet)
                in_stay_state = decision.system_state != SystemState.PRE_ARRIVAL_HEATING

                # Case 1: Guest turns unit OFF
                # Detected when unit was commanded ON (last_cmd.power is True), but physical state is now reported OFF
                is_new_power_off = (not curr.power and in_stay_state and last_cmd is not None and last_cmd.power)
                is_existing_power_off = (not curr.power and override.get("power_off") and override.get("reservation_uid") == active_uid)
                if is_new_power_off:
                    _LOGGER.info(
                        f"Detected guest manual power-OFF on {dev_id} during active reservation {active_uid}. "
                        f"Recording persistent power-OFF override for the remainder of the reservation."
                    )
                    self._manual_overrides[dev_id] = {
                        "reservation_uid": active_uid,
                        "power_off": True,
                        "temp_override": None,
                        "temp_daypart": None,
                        "detected_at": eval_time.isoformat()
                    }
                    override = self._manual_overrides[dev_id]
                elif is_existing_power_off:
                    pass

                # Case 2: Guest turns unit back ON after a power-OFF override
                elif curr.power and override.get("power_off"):
                    curr_temp = curr.target_temperature if curr.target_temperature is not None else cmd.target_temperature
                    _LOGGER.info(
                        f"Detected guest manually turned {dev_id} back ON at {curr_temp}°C. "
                        f"Clearing persistent power-OFF override. Preserving guest setpoint until next scheduled daypart."
                    )
                    is_daytime = decision.system_state in (
                        SystemState.OCCUPIED_MORNING,
                        SystemState.OCCUPIED_DAY,
                        SystemState.OCCUPIED_EVENING,
                    )
                    if is_daytime and curr_temp is not None:
                        self.set_preferred_day_temperature(dev_id, curr_temp, active_uid, source="manual_power_on", updated_at=eval_time)
                    self._manual_overrides[dev_id] = {
                        "reservation_uid": active_uid,
                        "power_off": False,
                        "temp_override": curr_temp,
                        "temp_daypart": decision.system_state.value,
                        "preferred_day_temperature": self.get_preferred_day_temperature(dev_id),
                        "detected_at": eval_time.isoformat()
                    }
                    override = self._manual_overrides[dev_id]

                # Case 3: Unit is ON, check if guest manually changed the target temperature
                elif curr.power and not is_transition_tick:
                    curr_temp = curr.target_temperature if curr.target_temperature is not None else cmd.target_temperature
                    baseline_temp = last_cmd.target_temperature if (last_cmd and last_cmd.target_temperature is not None) else None
                    if baseline_temp is not None and abs(curr_temp - baseline_temp) >= 0.5:
                        if override.get("temp_override") != curr_temp:
                            _LOGGER.info(
                                f"Detected guest manual temperature change on {dev_id} to {curr_temp}°C (baseline {baseline_temp}°C). "
                                f"Preserving setpoint during {decision.system_state.value} until next scheduled daypart."
                            )
                        is_daytime = decision.system_state in (
                            SystemState.OCCUPIED_MORNING,
                            SystemState.OCCUPIED_DAY,
                            SystemState.OCCUPIED_EVENING,
                        )
                        if is_daytime and curr_temp is not None:
                            self.set_preferred_day_temperature(dev_id, curr_temp, active_uid, source="manual_remote", updated_at=eval_time)

                        self._manual_overrides[dev_id] = {
                            "reservation_uid": active_uid,
                            "power_off": False,
                            "temp_override": curr_temp,
                            "temp_daypart": decision.system_state.value,
                            "preferred_day_temperature": self.get_preferred_day_temperature(dev_id),
                            "detected_at": eval_time.isoformat()
                        }
                        override = self._manual_overrides[dev_id]

                # Enforcement:
                # 1) Persistent Power-OFF Override: preserve OFF state across ALL dayparts & transitions
                if override.get("power_off") and override.get("reservation_uid") == active_uid:
                    if cmd.power:
                        results[dev_id] = (
                            False,
                            cmd,
                            f"Guest manual override preserved (Persistent Power-OFF): Guest manually turned OFF {dev_id}. "
                            f"Preserving unpowered state for active reservation ({active_uid}) across transitions."
                        )
                        continue

                # 2) Temporary Temperature Override: preserve until next scheduled daypart
                temp_override = override.get("temp_override")
                temp_daypart = override.get("temp_daypart")
                if (
                    temp_override is not None
                    and curr.power
                    and temp_daypart == decision.system_state.value
                ):

                    if temp_override < self.config.TEMP_MIN_SAFE or temp_override > self.config.TEMP_MAX_SAFE:
                        _LOGGER.warning(
                            f"Guest override on {dev_id} ({temp_override}°C) is outside safe limits "
                            f"[{self.config.TEMP_MIN_SAFE}°C-{self.config.TEMP_MAX_SAFE}°C]. Overriding with safe automation target {cmd.target_temperature}°C."
                        )
                    else:
                        results[dev_id] = (
                            False,
                            cmd,
                            f"Guest manual override preserved: Guest set temperature to {temp_override}°C on {dev_id}. "
                            f"Preserving guest preference during {decision.system_state.value}."
                        )
                        continue

            # 2c. Unpowered Device LED Guard: Never power on an unpowered AC solely to change its LED display
            if not curr.power and cmd.power:
                is_led_test_or_change = (
                    "led" in cmd.reason.lower() or "screen" in cmd.reason.lower()
                )
                if is_led_test_or_change:
                    results[dev_id] = (
                        False,
                        cmd,
                        f"Safeguard blocked: Will not power ON unpowered AC ({dev_id}) solely to change LED display."
                    )
                    continue

            # 2d. State Drift / Debounce Check (Skip identical commands)
            # Physical infrared remote commands cause the AC to beep and reset beepSwitch to 1.
            # We do NOT treat beep_feedback mismatch alone as a reason to dispatch a command,
            # which would cause redundant dispatches every 15 minutes and trigger unwanted audible chimes.
            # Instead, beepSwitch: 0 is bundled into every necessary state transition or setpoint command.
            if (
                curr.power == cmd.power
                and (not cmd.power or abs(curr.target_temperature - cmd.target_temperature) < 0.1)
                and (not cmd.power or curr.mode == cmd.mode)
                and (not cmd.power or curr.fan_speed == cmd.fan_speed)
                and (not cmd.power or curr.eco_mode == cmd.eco_mode)
                and (not cmd.power or curr.display_led == cmd.display_led)
            ):
                results[dev_id] = (
                    False,
                    cmd,
                    f"Debounce: {dev_id} already in desired state (Power={curr.power}, Temp={curr.target_temperature}°C, Mode={curr.mode.value}, LED={curr.display_led}, Beep={curr.beep_feedback})"
                )
                continue

            # 2d. Compressor Anti-Cycling / Rate-Limiting Protection
            power_toggled = curr.power != cmd.power
            if power_toggled:
                last_toggle = self._last_power_toggle.get(dev_id)
                if last_toggle:
                    elapsed = (eval_time - last_toggle).total_seconds()
                    if elapsed < self.config.MIN_CYCLE_INTERVAL_SECONDS:
                        remaining = int(self.config.MIN_CYCLE_INTERVAL_SECONDS - elapsed)
                        results[dev_id] = (
                            False,
                            cmd,
                            f"Compressor protection: {dev_id} power toggled {int(elapsed)}s ago. Must wait {remaining}s."
                        )
                        continue

                # Record new toggle time
                self._last_power_toggle[dev_id] = eval_time

            # Passed all checks
            if not self.config.DRY_RUN:
                self._last_applied_command[dev_id] = cmd
            results[dev_id] = (True, cmd, "Validated and approved for dispatch")

        return results

    def record_applied_command(self, cmd: ACCommand) -> None:
        """Record command dispatched to device for drift and debounce tracking."""
        self._last_applied_command[cmd.device_id] = cmd

    def record_power_toggle(self, device_id: str, toggle_time: Optional[datetime] = None) -> None:
        """Record power state transition timestamp for anti-cycling compressor protection."""
        self._last_power_toggle[device_id] = toggle_time or datetime.now()

    def is_compressor_cycle_locked(self, device_id: str, check_time: Optional[datetime] = None) -> bool:
        """Check whether device is locked by the 10-minute (600s) anti-cycling safeguard."""
        last_toggle = self._last_power_toggle.get(device_id)
        if not last_toggle:
            return False
        eval_time = check_time or datetime.now()
        elapsed = (eval_time - last_toggle).total_seconds()
        return elapsed < self.config.MIN_CYCLE_INTERVAL_SECONDS

    def get_manual_overrides(self) -> Dict[str, Dict[str, Any]]:
        """Return dictionary of current manual overrides for persistence."""
        return dict(self._manual_overrides)

    def set_manual_overrides(self, overrides: Dict[str, Dict[str, Any]]) -> None:
        """Hydrate manual overrides from persistent store."""
        if isinstance(overrides, dict):
            self._manual_overrides = {k: dict(v) for k, v in overrides.items() if isinstance(v, dict)}

    def clear_manual_overrides(self, device_id: Optional[str] = None) -> None:
        """Clear manual overrides for a device, or all devices if None."""
        if device_id:
            self._manual_overrides.pop(device_id, None)
        else:
            self._manual_overrides.clear()

    def set_guest_power_off_override(self, device_id: str, reservation_uid: str, timestamp: Optional[datetime] = None) -> None:
        """Manually register or force a persistent guest power-off override."""
        ts = (timestamp or datetime.now()).isoformat()
        self._manual_overrides[device_id] = {
            "reservation_uid": reservation_uid,
            "power_off": True,
            "temp_override": None,
            "temp_daypart": None,
            "detected_at": ts
        }

    def clear_guest_power_off_override(self, device_id: str) -> None:
        """Clear guest power-off override for a device."""
        if device_id in self._manual_overrides:
            self._manual_overrides[device_id]["power_off"] = False

    def has_guest_power_off_override(self, device_id: str, reservation_uid: Optional[str] = None) -> bool:
        """Check whether a persistent guest power-off override is active for a device."""
        ovr = self._manual_overrides.get(device_id)
        if not ovr or not ovr.get("power_off"):
            return False
        if reservation_uid and ovr.get("reservation_uid") != reservation_uid:
            return False
        return True

    def get_preferred_day_temperature(self, device_id: str) -> Optional[float]:
        """Get guest preferred daytime temperature for a device, if set."""
        pdata = self._preferred_day_temperatures.get(device_id)
        if pdata and "temperature" in pdata:
            return float(pdata["temperature"])
        ovr = self._manual_overrides.get(device_id)
        if ovr and ovr.get("preferred_day_temperature") is not None:
            return float(ovr["preferred_day_temperature"])
        return None

    def set_preferred_day_temperature(
        self,
        device_id: str,
        temp: float,
        reservation_uid: str,
        source: str = "manual_override",
        updated_at: Optional[datetime] = None
    ) -> None:
        """Record or update guest preferred daytime temperature for a device."""
        clamped = max(self.config.TEMP_MIN_SAFE, min(self.config.TEMP_MAX_SAFE, float(temp)))
        ts = (updated_at or datetime.now()).isoformat()
        self._preferred_day_temperatures[device_id] = {
            "temperature": clamped,
            "reservation_uid": reservation_uid,
            "source": source,
            "updated_at": ts
        }
        if device_id in self._manual_overrides:
            self._manual_overrides[device_id]["preferred_day_temperature"] = clamped

    def get_preferred_day_temperatures(self) -> Dict[str, Dict[str, Any]]:
        """Return all recorded preferred daytime temperatures for persistence."""
        return {k: dict(v) for k, v in self._preferred_day_temperatures.items()}

    def set_preferred_day_temperatures(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Hydrate preferred daytime temperatures from persistent storage."""
        if isinstance(data, dict):
            self._preferred_day_temperatures = {k: dict(v) for k, v in data.items() if isinstance(v, dict)}

    def clear_preferred_day_temperatures(self, device_id: Optional[str] = None) -> None:
        """Clear recorded preferred daytime temperatures."""
        if device_id:
            self._preferred_day_temperatures.pop(device_id, None)
            if device_id in self._manual_overrides:
                self._manual_overrides[device_id].pop("preferred_day_temperature", None)
        else:
            self._preferred_day_temperatures.clear()
            for ovr in self._manual_overrides.values():
                ovr.pop("preferred_day_temperature", None)

    def get_adaptive_night_target(self, device_id: str, default_day_temp: Optional[float] = None) -> float:
        """
        Calculate adaptive night setback target temperature from daytime preference.
        Formula:
          target = round(preferred_day_temp - NIGHT_SETBACK_DELTA, 1)
          if preferred_day_temp >= NIGHT_SETBACK_FLOOR:
              target = max(target, NIGHT_SETBACK_FLOOR)
          else:
              target = min(target, preferred_day_temp)
        """
        pref = self.get_preferred_day_temperature(device_id)
        if pref is None:
            if default_day_temp is not None:
                pref = default_day_temp
            elif device_id == self.config.TCL_LIVING_ROOM_ID:
                pref = getattr(self.config, "ADAPTIVE_DEFAULT_DAY_TEMP_LIVING", 22.0)
            else:
                pref = getattr(self.config, "ADAPTIVE_DEFAULT_DAY_TEMP_BEDROOM", 21.0)

        delta = getattr(self.config, "NIGHT_SETBACK_DELTA", 1.0)
        floor = getattr(self.config, "NIGHT_SETBACK_FLOOR", 18.0)
        target = round(pref - delta, 1)

        if pref >= floor:
            target = max(target, floor)
        else:
            # Guest prefers cooler setting during day (< 18.0°C), do not raise temperature at night!
            target = min(target, pref)

        return max(self.config.TEMP_MIN_SAFE, min(self.config.TEMP_MAX_SAFE, target))

    def mark_live_command_executed(self) -> None:
        """Record that at least one verified live physical command has been executed."""
        self.has_live_command_history = True

    def initialize_from_physical_telemetry(
        self,
        current_states: Dict[str, ACState],
        active_reservation: Optional[Reservation],
        system_state: SystemState,
        eval_time: Optional[datetime] = None,
        scheduled_commands: Optional[Dict[str, ACCommand]] = None,
    ) -> Dict[str, str]:
        """
        Idempotent first-start activation initialization from fresh physical telemetry.
        Requirements:
        1. Read fresh physical device telemetry.
        2. Verify devices are online.
        3. If an air conditioner is already physically OFF (like Bedroom), initialize and preserve
           this state as a persistent reservation-specific guest power-OFF override.
        4. If an air conditioner is already physically set to a different temperature than scheduled
           (like Living Room at 21°C instead of 23°C), initialize and preserve this state as a temporary
           daypart temperature override.
        5. If command execution history is missing or contains only DRY_RUN attempts, do not assume prior
           successful commands. Read physical telemetry directly.
        6. Only trust verified successful physical command executions.
        7. Ensure the initial activation is idempotent. Running it repeatedly must not overwrite or clear valid overrides.
        """
        now = eval_time or datetime.now()
        results: Dict[str, str] = {}

        if not active_reservation or not active_reservation.is_confirmed:
            return results

        # In-stay check: pre-arrival, checkout, or vacancy do not initialize in-stay guest overrides
        in_stay = system_state not in (
            SystemState.CHECKOUT_SHUTDOWN,
            SystemState.IDLE_UNOCCUPIED,
            SystemState.PRE_ARRIVAL_HEATING,
            SystemState.PAUSED,
        )
        if not in_stay:
            return results

        res_uid = active_reservation.uid

        # Idempotency check: If this exact reservation was already initialized at first start,
        # preserve all active overrides and do not overwrite guest modifications made post-startup.
        if self.initialized_reservation_uid == res_uid:
            return {
                dev_id: "First-start initialization already completed for reservation (idempotent no-op)"
                for dev_id in current_states
            }

        sched_cmds = scheduled_commands or {}

        for dev_id, curr in current_states.items():
            if not curr.is_online:
                results[dev_id] = f"Device {dev_id} is offline; skipping telemetry initialization"
                continue

            existing_override = self._manual_overrides.get(dev_id, {})
            existing_for_res = existing_override.get("reservation_uid") == res_uid

            # Case 1: Device is physically OFF
            if not curr.power:
                if existing_for_res and existing_override.get("power_off"):
                    results[dev_id] = f"Preserved existing persistent guest power-OFF override for {dev_id}"
                else:
                    self.set_guest_power_off_override(dev_id, res_uid, timestamp=now)
                    msg = (
                        f"Initialized persistent guest power-OFF override for {dev_id} from physical telemetry "
                        f"(unit is physically OFF during active stay {active_reservation.masked_uid})."
                    )
                    _LOGGER.info(msg)
                    results[dev_id] = msg

            # Case 2: Device is physically ON
            else:
                sched_cmd = sched_cmds.get(dev_id)
                scheduled_temp = sched_cmd.target_temperature if sched_cmd else None
                curr_temp = curr.target_temperature

                is_daytime = system_state in (
                    SystemState.OCCUPIED_MORNING,
                    SystemState.OCCUPIED_DAY,
                    SystemState.OCCUPIED_EVENING,
                )
                if curr_temp is not None and is_daytime:
                    # Record physical temperature as initial guest preferred day temperature
                    self.set_preferred_day_temperature(
                        dev_id, curr_temp, res_uid, source="first_start_telemetry", updated_at=now
                    )

                # If physical temperature differs from scheduled setpoint by >= 0.5°C
                if (
                    scheduled_temp is not None
                    and curr_temp is not None
                    and abs(curr_temp - scheduled_temp) >= 0.5
                ):
                    if (
                        existing_for_res
                        and existing_override.get("temp_override") == curr_temp
                        and existing_override.get("temp_daypart") == system_state.value
                    ):
                        results[dev_id] = f"Preserved existing temporary temperature override for {dev_id}"
                    elif not existing_for_res or existing_override.get("power_off") or existing_override.get("temp_override") is None:
                        # Register temporary daypart temperature override
                        self._manual_overrides[dev_id] = {
                            "reservation_uid": res_uid,
                            "power_off": False,
                            "temp_override": curr_temp,
                            "temp_daypart": system_state.value,
                            "preferred_day_temperature": self.get_preferred_day_temperature(dev_id),
                            "detected_at": now.isoformat()
                        }
                        msg = (
                            f"Initialized temporary daypart temperature override for {dev_id} from physical telemetry "
                            f"({curr_temp}°C vs scheduled {scheduled_temp}°C) during {system_state.value}."
                        )
                        _LOGGER.info(msg)
                        results[dev_id] = msg
                    else:
                        results[dev_id] = f"Active temperature override already in place for {dev_id}"
                else:
                    results[dev_id] = f"Device {dev_id} physically in sync with schedule ({curr_temp}°C)"

        self.initialized_reservation_uid = res_uid
        return results
