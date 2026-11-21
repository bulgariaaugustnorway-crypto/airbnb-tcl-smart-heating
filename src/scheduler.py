"""State machine and schedule engine for Europe/Sofia timezone."""

import logging
from datetime import datetime, date, time, timedelta
from typing import List, Optional, Tuple, Dict, Any

from src.models import (
    Reservation,
    ReservationType,
    SystemState,
    ACMode,
    ACFanSpeed,
    ACCommand,
    ScheduleDecision,
    OwnerTestSession,
    HeatingProfileType,
    HEATING_PROFILE_PRESETS,
)
from src.config import AutomationConfig, TIMEZONE_SOFIA

_LOGGER = logging.getLogger(__name__)


class HVACScheduleEngine:
    """Evaluates property booking state and generates device commands."""

    def __init__(self, config: Optional[AutomationConfig] = None):
        self.config = config or AutomationConfig()

    def to_sofia_time(self, dt: Optional[datetime] = None) -> datetime:
        """Ensure the datetime is localized to Europe/Sofia."""
        if dt is None:
            return datetime.now(TIMEZONE_SOFIA)
        if dt.tzinfo is None:
            # Assume local/Sofia time if naive
            return dt.replace(tzinfo=TIMEZONE_SOFIA)
        return dt.astimezone(TIMEZONE_SOFIA)

    @staticmethod
    def _res_desc(res: Reservation) -> str:
        """Format human-readable description preserving true reservation classification with masked UID."""
        if res.reservation_type == ReservationType.CONFIRMED:
            return f"Guest reservation {res.masked_uid}"
        elif res.reservation_type == ReservationType.BLOCKED:
            return f"Blocked date stay {res.masked_uid}"
        elif res.reservation_type == ReservationType.OWNER_TEST:
            return f"Owner test stay {res.masked_uid}"
        return f"{res.reservation_type.value} {res.masked_uid}"

    def evaluate(
        self,
        reservations: List[Reservation],
        current_dt: Optional[datetime] = None,
        owner_test: Optional[OwnerTestSession] = None,
        preferred_day_temperatures: Optional[Dict[str, float]] = None,
    ) -> ScheduleDecision:
        """
        Evaluate current booking status and determine target HVAC state.
        Supports confirmed guest bookings, eligible blocked/owner stays, and active Owner Test sessions.
        Confirmed reservations always take precedence over overlapping blocked dates.
        """
        sofia_now = self.to_sofia_time(current_dt)
        today = sofia_now.date()
        current_time = sofia_now.time()

        # --- SYSTEM PAUSED OVERRIDE (Suspend all automation without altering physical states) ---
        if getattr(self.config, "SYSTEM_MODE", "AUTO").upper() == "PAUSED":
            return ScheduleDecision(
                timestamp=sofia_now,
                system_state=SystemState.PAUSED,
                active_reservation=None,
                target_power=False,
                target_temperature=self.config.SCHEDULE_DAY_TEMP,
                target_mode=ACMode.HEAT,
                target_fan_speed=ACFanSpeed.AUTO,
                target_eco=False,
                target_display_led=False,
                target_beep=False,
                decision_reason="Automation suspended (PAUSED mode); zero commands dispatched, preserving current physical states",
                commands_to_send={}
            )

        # --- OWNER TEST / OWNER PRESENT MODE (Temporary on-site override with auto-expiration) ---
        if owner_test and owner_test.is_active(sofia_now):
            # Confirmed guest reservations strictly take precedence over owner presence
            pre_arrival_start = (
                datetime.combine(today, self.config.CHECKIN_TIME)
                - timedelta(hours=self.config.PRE_ARRIVAL_HOURS)
            ).time()
            active_guest = any(
                r.is_confirmed and (
                    (r.start_date < today < r.end_date) or
                    (r.start_date == today and current_time >= pre_arrival_start) or
                    (r.end_date == today and current_time < self.config.CHECKOUT_TIME)
                )
                for r in reservations
            )
            if not active_guest:
                expiry_str = owner_test.expires_at.strftime('%H:%M') if owner_test.expires_at else 'unknown'
                session_type = getattr(owner_test, "session_type", "OWNER_TEST")
                if session_type == "OWNER_PRESENT":
                    # Full diurnal per-room heating schedule during owner stay
                    owner_res = Reservation(
                        uid=f"owner-present-{owner_test.started_at.strftime('%Y%m%d%H%M') if owner_test.started_at else 'active'}",
                        start_date=today,
                        end_date=today + timedelta(days=1),
                        summary="Owner Present Mode",
                        reservation_type=ReservationType.OWNER_PRESENT,
                        raw_status="OWNER_PRESENT_ACTIVE"
                    )
                    return self._build_occupied_decision(
                        sofia_now,
                        owner_res,
                        f"Owner presence active (expires at {expiry_str} Sofia time)"
                    )
                else:
                    # Fixed setpoint test hold
                    test_res = Reservation(
                        uid=f"owner-test-{owner_test.started_at.strftime('%Y%m%d%H%M') if owner_test.started_at else 'active'}",
                        start_date=today,
                        end_date=today + timedelta(days=1),
                        summary="Owner Test Mode",
                        reservation_type=ReservationType.OWNER_TEST,
                        raw_status="OWNER_TEST_ACTIVE"
                    )
                    decision = ScheduleDecision(
                        timestamp=sofia_now,
                        system_state=SystemState.OWNER_TEST_HOLD,
                        active_reservation=test_res,
                        target_power=True,
                        target_temperature=owner_test.target_temperature,
                        target_mode=owner_test.mode,
                        target_fan_speed=owner_test.fan_speed,
                        target_eco=False,
                        target_display_led=owner_test.display_led,
                        target_beep=self.config.BEEP_FEEDBACK_ENABLED,
                        decision_reason=f"Owner Test Mode active (expires at {expiry_str} Sofia time)"
                    )
                    # Custom per-room power from owner test config
                    self._populate_commands(
                        decision,
                        living_override=owner_test.living_room_power,
                        bedroom_override=owner_test.bedroom_power
                    )
                    return decision

        # --- SYSTEM MANUAL OVERRIDE (Continuous Comfort Heating) ---
        if getattr(self.config, "SYSTEM_MODE", "AUTO").upper() == "MANUAL":
            manual_res = Reservation(
                uid="manual-override-active",
                start_date=today,
                end_date=today + timedelta(days=1),
                summary="Manual Override (Continuous)",
                reservation_type=ReservationType.CONFIRMED,
                raw_status="MANUAL_OVERRIDE"
            )
            return self._build_occupied_decision(
                sofia_now,
                manual_res,
                "Manual override active; continuous comfort heating independent of Airbnb calendar"
            )

        # Filter for eligible reservations (confirmed, or blocked if heat_during_blocked_dates is True)
        eligible = [
            r for r in reservations
            if r.is_occupancy_eligible(heat_during_blocked_dates=self.config.HEAT_DURING_BLOCKED_DATES)
        ]

        # 1. Identify relevant reservations for today with confirmed precedence
        departing_res: Optional[Reservation] = None
        arriving_res: Optional[Reservation] = None
        mid_stay_res: Optional[Reservation] = None

        # Pass A: Prioritize confirmed reservations touching today
        for res in [r for r in eligible if r.is_confirmed]:
            if res.start_date < today < res.end_date:
                mid_stay_res = res
            if res.end_date == today:
                departing_res = res
            if res.start_date == today:
                arriving_res = res

        # Pass B: Fill unfilled roles with other eligible stays (blocked / owner stays)
        # Note: If a confirmed reservation is in mid-stay today, it completely supersedes other stays.
        if mid_stay_res is None:
            non_confirmed = [r for r in eligible if not r.is_confirmed]
            for res in non_confirmed:
                if departing_res is None and res.end_date == today:
                    departing_res = res
                if arriving_res is None and res.start_date == today:
                    arriving_res = res
            # Only consider mid-stay for non-confirmed if neither departing nor arriving takes precedence
            if mid_stay_res is None and departing_res is None and arriving_res is None:
                for res in non_confirmed:
                    if res.start_date < today < res.end_date:
                        mid_stay_res = res
                        break

        pre_arrival_start_time = (
            datetime.combine(today, self.config.CHECKIN_TIME)
            - timedelta(hours=self.config.PRE_ARRIVAL_HOURS)
        ).time()

        # --- SCENARIO 1: Mid-stay Day (Guest or owner is in residence all day) ---
        if mid_stay_res:
            return self._build_occupied_decision(
                sofia_now,
                mid_stay_res,
                f"Mid-stay day for {self._res_desc(mid_stay_res)} (night of {today})",
                preferred_day_temperatures=preferred_day_temperatures
            )

        # --- SCENARIO 2: Consecutive Turnover Day (Stay A leaves, Stay B arrives today) ---
        if departing_res and arriving_res:
            # 2a. Before 11:00 Checkout -> Stay A still here
            if current_time < self.config.CHECKOUT_TIME:
                return self._build_occupied_decision(
                    sofia_now,
                    departing_res,
                    f"Consecutive checkout morning: {self._res_desc(departing_res)} departing at 11:00",
                    preferred_day_temperatures=preferred_day_temperatures
                )
            # 2b. 11:00 - 13:00 -> Turnover window (cleaners/empty property, ACs shut down)
            elif self.config.CHECKOUT_TIME <= current_time < pre_arrival_start_time:
                return self._build_shutdown_decision(
                    sofia_now,
                    departing_res,
                    f"Turnover window between {self._res_desc(departing_res)} checkout (11:00) and {self._res_desc(arriving_res)} pre-heat ({pre_arrival_start_time.strftime('%H:%M')})"
                )
            # 2c. 13:00 - 15:00 -> Pre-arrival heating for Stay B
            elif pre_arrival_start_time <= current_time < self.config.CHECKIN_TIME:
                return self._build_pre_arrival_decision(
                    sofia_now,
                    arriving_res,
                    f"Pre-arrival heating for consecutive incoming {self._res_desc(arriving_res)} (check-in at 15:00)"
                )
            # 2d. 15:00+ -> Stay B checked in
            else:
                return self._build_occupied_decision(
                    sofia_now,
                    arriving_res,
                    f"{self._res_desc(arriving_res)} checked in (consecutive turnover arrival)",
                    preferred_day_temperatures=preferred_day_temperatures
                )

        # --- SCENARIO 3: Departure Day Only (Stay leaves today, no new arrival) ---
        if departing_res:
            if current_time < self.config.CHECKOUT_TIME:
                return self._build_occupied_decision(
                    sofia_now,
                    departing_res,
                    f"Final morning for {self._res_desc(departing_res)} (checkout at 11:00)",
                    preferred_day_temperatures=preferred_day_temperatures
                )
            else:
                return self._build_shutdown_decision(
                    sofia_now,
                    departing_res,
                    f"Checkout complete for {self._res_desc(departing_res)} (departed at 11:00)"
                )

        # --- SCENARIO 4: Arrival Day Only (New stay arrives today) ---
        if arriving_res:
            if current_time < pre_arrival_start_time:
                return self._build_idle_decision(
                    sofia_now,
                    f"Arrival day for {self._res_desc(arriving_res)}, awaiting pre-arrival window at {pre_arrival_start_time.strftime('%H:%M')}"
                )
            elif pre_arrival_start_time <= current_time < self.config.CHECKIN_TIME:
                return self._build_pre_arrival_decision(
                    sofia_now,
                    arriving_res,
                    f"Pre-arrival heating active for incoming {self._res_desc(arriving_res)} (check-in at 15:00)"
                )
            else:
                return self._build_occupied_decision(
                    sofia_now,
                    arriving_res,
                    f"{self._res_desc(arriving_res)} checked in",
                    preferred_day_temperatures=preferred_day_temperatures
                )

        # --- SCENARIO 5: No Confirmed or Eligible Reservation Today (Idle / Unoccupied) ---
        has_blocks = any(r.is_blocked for r in reservations)
        if has_blocks and not self.config.HEAT_DURING_BLOCKED_DATES:
            reason = "No eligible reservation covering today (HEAT_DURING_BLOCKED_DATES is disabled); property unoccupied"
        else:
            reason = "No confirmed or eligible reservation covering today; property unoccupied"

        return self._build_idle_decision(
            sofia_now,
            reason
        )

    def _resolve_profile_and_setpoints(
        self,
        res: Optional[Reservation]
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Resolve active heating profile and diurnal setpoints for a given reservation or property default.
        Priority:
        1. Specific profile assigned to this reservation UID in RESERVATION_PROFILES.
        2. Property default profile (DEFAULT_HEATING_PROFILE, default: SKI_DAY).
        """
        res_profiles = getattr(self.config, "RESERVATION_PROFILES", {}) or {}
        assigned_config = None
        if res and res.uid in res_profiles:
            assigned_config = res_profiles[res.uid]

        if assigned_config:
            profile_name = str(assigned_config.get("profile", "SKI_DAY")).upper()
            custom_temps = assigned_config.get("custom_temperatures", {})
            lr_offset = assigned_config.get("living_room_offset")
            br_offset = assigned_config.get("bedroom_offset")
        else:
            profile_name = getattr(self.config, "DEFAULT_HEATING_PROFILE", "SKI_DAY").upper()
            custom_temps = {}
            lr_offset = None
            br_offset = None

        if lr_offset is None:
            lr_offset = getattr(self.config, "LIVING_ROOM_TEMP_OFFSET", 0.0)
        if br_offset is None:
            br_offset = getattr(self.config, "BEDROOM_TEMP_OFFSET", 0.0)

        # Base preset setpoints
        if profile_name == HeatingProfileType.AT_HOME.value:
            lr_sp = {
                "morning_temp": float(self.config.LIVING_ROOM_MORNING_TEMP if getattr(self.config, "LIVING_ROOM_MORNING_TEMP", None) is not None else 23.0),
                "morning_fan": "Mid",
                "day_temp": float(self.config.LIVING_ROOM_DAY_TEMP if getattr(self.config, "LIVING_ROOM_DAY_TEMP", None) is not None else 23.0),
                "day_fan": "Mid",
                "evening_temp": float(self.config.LIVING_ROOM_EVENING_TEMP if getattr(self.config, "LIVING_ROOM_EVENING_TEMP", None) is not None else 23.0),
                "evening_fan": "Mid",
                "night_temp": float(self.config.LIVING_ROOM_NIGHT_TEMP if getattr(self.config, "LIVING_ROOM_NIGHT_TEMP", None) is not None else 20.0),
                "night_fan": "Quiet",
            }
            br_sp = {
                "morning_temp": float(self.config.BEDROOM_MORNING_TEMP if getattr(self.config, "BEDROOM_MORNING_TEMP", None) is not None else 22.0),
                "morning_fan": "Mid",
                "day_temp": float(self.config.BEDROOM_DAY_TEMP if getattr(self.config, "BEDROOM_DAY_TEMP", None) is not None else 21.0),
                "day_fan": "Mid",
                "evening_temp": float(self.config.BEDROOM_EVENING_TEMP if getattr(self.config, "BEDROOM_EVENING_TEMP", None) is not None else 22.0),
                "evening_fan": "Mid",
                "night_temp": float(self.config.BEDROOM_NIGHT_TEMP if getattr(self.config, "BEDROOM_NIGHT_TEMP", None) is not None else 20.0),
                "night_fan": "Quiet",
            }
            setpoints = {
                "morning_temp": lr_sp["morning_temp"],
                "morning_fan": lr_sp["morning_fan"],
                "day_temp": lr_sp["day_temp"],
                "day_fan": lr_sp["day_fan"],
                "evening_temp": lr_sp["evening_temp"],
                "evening_fan": lr_sp["evening_fan"],
                "night_temp": lr_sp["night_temp"],
                "night_fan": lr_sp["night_fan"],
                "living_room": lr_sp,
                "bedroom": br_sp,
            }
        elif profile_name == HeatingProfileType.CUSTOM.value:
            c_lr = custom_temps.get("living_room") if isinstance(custom_temps.get("living_room"), dict) else {}
            c_br = custom_temps.get("bedroom") if isinstance(custom_temps.get("bedroom"), dict) else {}

            m_temp = float(custom_temps.get("morning_temp", self.config.SCHEDULE_MORNING_TEMP))
            d_temp = float(custom_temps.get("day_temp", self.config.SCHEDULE_DAY_TEMP))
            e_temp = float(custom_temps.get("evening_temp", self.config.SCHEDULE_EVENING_TEMP))
            n_temp = float(custom_temps.get("night_temp", self.config.SCHEDULE_NIGHT_TEMP))

            lr_sp = {
                "morning_temp": float(c_lr.get("morning_temp", custom_temps.get("living_room_morning_temp", getattr(self.config, "LIVING_ROOM_MORNING_TEMP", None) or m_temp))),
                "morning_fan": str(c_lr.get("morning_fan", self.config.SCHEDULE_MORNING_FAN)),
                "day_temp": float(c_lr.get("day_temp", custom_temps.get("living_room_day_temp", getattr(self.config, "LIVING_ROOM_DAY_TEMP", None) or d_temp))),
                "day_fan": str(c_lr.get("day_fan", self.config.SCHEDULE_DAY_FAN)),
                "evening_temp": float(c_lr.get("evening_temp", custom_temps.get("living_room_evening_temp", getattr(self.config, "LIVING_ROOM_EVENING_TEMP", None) or e_temp))),
                "evening_fan": str(c_lr.get("evening_fan", self.config.SCHEDULE_EVENING_FAN)),
                "night_temp": float(c_lr.get("night_temp", custom_temps.get("living_room_night_temp", getattr(self.config, "LIVING_ROOM_NIGHT_TEMP", None) or n_temp))),
                "night_fan": str(c_lr.get("night_fan", self.config.SCHEDULE_NIGHT_FAN)),
            }
            br_sp = {
                "morning_temp": float(c_br.get("morning_temp", custom_temps.get("bedroom_morning_temp", getattr(self.config, "BEDROOM_MORNING_TEMP", None) or m_temp))),
                "morning_fan": str(c_br.get("morning_fan", self.config.SCHEDULE_MORNING_FAN)),
                "day_temp": float(c_br.get("day_temp", custom_temps.get("bedroom_day_temp", getattr(self.config, "BEDROOM_DAY_TEMP", None) or d_temp))),
                "day_fan": str(c_br.get("day_fan", self.config.SCHEDULE_DAY_FAN)),
                "evening_temp": float(c_br.get("evening_temp", custom_temps.get("bedroom_evening_temp", getattr(self.config, "BEDROOM_EVENING_TEMP", None) or e_temp))),
                "evening_fan": str(c_br.get("evening_fan", self.config.SCHEDULE_EVENING_FAN)),
                "night_temp": float(c_br.get("night_temp", custom_temps.get("bedroom_night_temp", getattr(self.config, "BEDROOM_NIGHT_TEMP", None) or n_temp))),
                "night_fan": str(c_br.get("night_fan", self.config.SCHEDULE_NIGHT_FAN)),
            }
            setpoints = {
                "morning_temp": m_temp,
                "morning_fan": str(self.config.SCHEDULE_MORNING_FAN),
                "day_temp": d_temp,
                "day_fan": str(self.config.SCHEDULE_DAY_FAN),
                "evening_temp": e_temp,
                "evening_fan": str(self.config.SCHEDULE_EVENING_FAN),
                "night_temp": n_temp,
                "night_fan": str(self.config.SCHEDULE_NIGHT_FAN),
                "living_room": lr_sp,
                "bedroom": br_sp,
            }
        else:
            # Default: SKI_DAY (Daytime setback to 21°C), unless config schedule was customized
            has_custom_schedule = (
                self.config.SCHEDULE_DAY_TEMP != 21.0
                or self.config.SCHEDULE_MORNING_TEMP != 23.0
                or self.config.SCHEDULE_EVENING_TEMP != 23.0
                or self.config.SCHEDULE_NIGHT_TEMP != 20.0
                or getattr(self.config, "LIVING_ROOM_MORNING_TEMP", None) is not None
                or getattr(self.config, "BEDROOM_MORNING_TEMP", None) is not None
            )
            if has_custom_schedule:
                profile_name = HeatingProfileType.CUSTOM.value
                m_temp = float(self.config.SCHEDULE_MORNING_TEMP)
                d_temp = float(self.config.SCHEDULE_DAY_TEMP)
                e_temp = float(self.config.SCHEDULE_EVENING_TEMP)
                n_temp = float(self.config.SCHEDULE_NIGHT_TEMP)
                lr_sp = {
                    "morning_temp": float(getattr(self.config, "LIVING_ROOM_MORNING_TEMP", None) or m_temp),
                    "morning_fan": str(self.config.SCHEDULE_MORNING_FAN),
                    "day_temp": float(getattr(self.config, "LIVING_ROOM_DAY_TEMP", None) or d_temp),
                    "day_fan": str(self.config.SCHEDULE_DAY_FAN),
                    "evening_temp": float(getattr(self.config, "LIVING_ROOM_EVENING_TEMP", None) or e_temp),
                    "evening_fan": str(self.config.SCHEDULE_EVENING_FAN),
                    "night_temp": float(getattr(self.config, "LIVING_ROOM_NIGHT_TEMP", None) or n_temp),
                    "night_fan": str(self.config.SCHEDULE_NIGHT_FAN),
                }
                br_sp = {
                    "morning_temp": float(getattr(self.config, "BEDROOM_MORNING_TEMP", None) or m_temp),
                    "morning_fan": str(self.config.SCHEDULE_MORNING_FAN),
                    "day_temp": float(getattr(self.config, "BEDROOM_DAY_TEMP", None) or d_temp),
                    "day_fan": str(self.config.SCHEDULE_DAY_FAN),
                    "evening_temp": float(getattr(self.config, "BEDROOM_EVENING_TEMP", None) or e_temp),
                    "evening_fan": str(self.config.SCHEDULE_EVENING_FAN),
                    "night_temp": float(getattr(self.config, "BEDROOM_NIGHT_TEMP", None) or n_temp),
                    "night_fan": str(self.config.SCHEDULE_NIGHT_FAN),
                }
                setpoints = {
                    "morning_temp": m_temp,
                    "morning_fan": str(self.config.SCHEDULE_MORNING_FAN),
                    "day_temp": d_temp,
                    "day_fan": str(self.config.SCHEDULE_DAY_FAN),
                    "evening_temp": e_temp,
                    "evening_fan": str(self.config.SCHEDULE_EVENING_FAN),
                    "night_temp": n_temp,
                    "night_fan": str(self.config.SCHEDULE_NIGHT_FAN),
                    "living_room": lr_sp,
                    "bedroom": br_sp,
                }
            else:
                profile_name = HeatingProfileType.SKI_DAY.value
                lr_sp = {
                    "morning_temp": 23.0,
                    "morning_fan": "Mid",
                    "day_temp": 21.0,
                    "day_fan": "Auto",
                    "evening_temp": 23.0,
                    "evening_fan": "Mid",
                    "night_temp": 20.0,
                    "night_fan": "Quiet",
                }
                br_sp = {
                    "morning_temp": 23.0,
                    "morning_fan": "Mid",
                    "day_temp": 21.0,
                    "day_fan": "Auto",
                    "evening_temp": 23.0,
                    "evening_fan": "Mid",
                    "night_temp": 20.0,
                    "night_fan": "Quiet",
                }
                setpoints = {
                    "morning_temp": 23.0,
                    "morning_fan": "Mid",
                    "day_temp": 21.0,
                    "day_fan": "Auto",
                    "evening_temp": 23.0,
                    "evening_fan": "Mid",
                    "night_temp": 20.0,
                    "night_fan": "Quiet",
                    "living_room": lr_sp,
                    "bedroom": br_sp,
                }

        setpoints["living_room_offset"] = lr_offset
        setpoints["bedroom_offset"] = br_offset
        return profile_name, setpoints

    def _get_diurnal_period(
        self,
        current_time: time,
        setpoints: Optional[Dict[str, Any]] = None
    ) -> Tuple[SystemState, float, str]:
        """Determine diurnal period, target temperature, and fan speed."""
        sp = setpoints or {
            "morning_temp": self.config.SCHEDULE_MORNING_TEMP,
            "morning_fan": self.config.SCHEDULE_MORNING_FAN,
            "day_temp": self.config.SCHEDULE_DAY_TEMP,
            "day_fan": self.config.SCHEDULE_DAY_FAN,
            "evening_temp": self.config.SCHEDULE_EVENING_TEMP,
            "evening_fan": self.config.SCHEDULE_EVENING_FAN,
            "night_temp": self.config.SCHEDULE_NIGHT_TEMP,
            "night_fan": self.config.SCHEDULE_NIGHT_FAN,
        }

        if self.config.SCHEDULE_MORNING_START <= current_time < self.config.SCHEDULE_DAY_START:
            return (
                SystemState.OCCUPIED_MORNING,
                sp["morning_temp"],
                sp["morning_fan"]
            )
        elif self.config.SCHEDULE_DAY_START <= current_time < self.config.SCHEDULE_EVENING_START:
            return (
                SystemState.OCCUPIED_DAY,
                sp["day_temp"],
                sp["day_fan"]
            )
        elif self.config.SCHEDULE_EVENING_START <= current_time < self.config.SCHEDULE_NIGHT_START:
            return (
                SystemState.OCCUPIED_EVENING,
                sp["evening_temp"],
                sp["evening_fan"]
            )
        else:
            # Night profile: 23:00 - 07:00 (20°C, Quiet fan)
            return (
                SystemState.OCCUPIED_NIGHT,
                sp["night_temp"],
                sp["night_fan"]
            )

    def _build_occupied_decision(
        self,
        dt: datetime,
        res: Reservation,
        reason: str,
        preferred_day_temperatures: Optional[Dict[str, float]] = None
    ) -> ScheduleDecision:
        """Construct decision during guest occupancy."""
        profile_name, setpoints = self._resolve_profile_and_setpoints(res)
        system_state, target_temp, fan_speed_str = self._get_diurnal_period(dt.time(), setpoints)
        fan_speed = ACFanSpeed(fan_speed_str)

        # LED Display: At night (23:00–07:00), follow NIGHT_LED_DISPLAY (default False); otherwise DEFAULT_LED_DISPLAY (default True)
        is_night = (system_state == SystemState.OCCUPIED_NIGHT)
        led_target = self.config.NIGHT_LED_DISPLAY if is_night else self.config.DEFAULT_LED_DISPLAY
        beep_target = self.config.BEEP_FEEDBACK_ENABLED  # Always False by default

        formatted_reason = f"{reason} (Profile: {profile_name})"

        decision = ScheduleDecision(
            timestamp=dt,
            system_state=system_state,
            active_reservation=res,
            target_power=True,
            target_temperature=target_temp,
            target_mode=ACMode.HEAT,
            target_fan_speed=fan_speed,
            target_eco=False,
            target_display_led=led_target,
            target_beep=beep_target,
            decision_reason=formatted_reason,
            active_profile=profile_name
        )
        self._populate_commands(
            decision,
            lr_offset=setpoints.get("living_room_offset"),
            br_offset=setpoints.get("bedroom_offset"),
            setpoints=setpoints,
            preferred_day_temperatures=preferred_day_temperatures
        )
        return decision

    def _build_pre_arrival_decision(
        self,
        dt: datetime,
        res: Reservation,
        reason: str
    ) -> ScheduleDecision:
        """Construct decision for pre-arrival warm-up."""
        # Determine day vs night for pre-arrival warm-up
        current_time = dt.time()
        is_night = not (self.config.SCHEDULE_MORNING_START <= current_time < self.config.SCHEDULE_NIGHT_START)
        led_target = self.config.NIGHT_LED_DISPLAY if is_night else self.config.DEFAULT_LED_DISPLAY

        decision = ScheduleDecision(
            timestamp=dt,
            system_state=SystemState.PRE_ARRIVAL_HEATING,
            active_reservation=res,
            target_power=True,
            target_temperature=self.config.PRE_ARRIVAL_TEMP,
            target_mode=ACMode(self.config.PRE_ARRIVAL_MODE),
            target_fan_speed=ACFanSpeed(self.config.PRE_ARRIVAL_FAN),
            target_eco=False,
            target_display_led=led_target,
            target_beep=self.config.BEEP_FEEDBACK_ENABLED,
            decision_reason=reason
        )
        self._populate_commands(decision)
        return decision

    def get_room_led_target(self, dev_id: str, is_night: bool) -> bool:
        """
        Determine target LED display for a specific device.
        Supports independent room modes: SCHEDULE, ALWAYS_OFF, ALWAYS_ON.
        """
        is_bedroom = (dev_id == self.config.TCL_BEDROOM_ID)
        is_living = (dev_id == self.config.TCL_LIVING_ROOM_ID)

        mode = "SCHEDULE"
        if is_bedroom:
            mode = getattr(self.config, "BEDROOM_LED_MODE", "SCHEDULE").upper()
        elif is_living:
            mode = getattr(self.config, "LIVING_ROOM_LED_MODE", "SCHEDULE").upper()

        if mode == "ALWAYS_OFF":
            return False
        elif mode == "ALWAYS_ON":
            return True
        else:  # SCHEDULE
            return self.config.NIGHT_LED_DISPLAY if is_night else self.config.DEFAULT_LED_DISPLAY

    def _build_shutdown_decision(
        self,
        dt: datetime,
        res: Optional[Reservation],
        reason: str
    ) -> ScheduleDecision:
        """Construct decision for checkout shutdown."""
        decision = ScheduleDecision(
            timestamp=dt,
            system_state=SystemState.CHECKOUT_SHUTDOWN,
            active_reservation=res,
            target_power=False,
            target_temperature=self.config.SCHEDULE_DAY_TEMP,
            target_mode=ACMode.OFF,
            target_fan_speed=ACFanSpeed.AUTO,
            target_eco=True,
            target_display_led=False,
            target_beep=False,
            decision_reason=reason
        )
        self._populate_commands(decision)
        return decision

    def _build_idle_decision(
        self,
        dt: datetime,
        reason: str
    ) -> ScheduleDecision:
        """Construct decision for empty property."""
        if self.config.ANTI_FREEZE_ENABLED:
            power = True
            temp = self.config.ANTI_FREEZE_TEMP
            mode = ACMode.HEAT
            fan = ACFanSpeed.LOW
        else:
            power = False
            temp = self.config.SCHEDULE_DAY_TEMP
            mode = ACMode.OFF
            fan = ACFanSpeed.AUTO

        decision = ScheduleDecision(
            timestamp=dt,
            system_state=SystemState.IDLE_UNOCCUPIED,
            active_reservation=None,
            target_power=power,
            target_temperature=temp,
            target_mode=mode,
            target_fan_speed=fan,
            target_eco=True,
            target_display_led=False,
            target_beep=False,
            decision_reason=reason
        )
        self._populate_commands(decision)
        return decision

    def _populate_commands(
        self,
        decision: ScheduleDecision,
        living_override: Optional[bool] = None,
        bedroom_override: Optional[bool] = None,
        lr_offset: Optional[float] = None,
        br_offset: Optional[float] = None,
        setpoints: Optional[Dict[str, Any]] = None,
        preferred_day_temperatures: Optional[Dict[str, float]] = None,
    ) -> None:
        """Generate ACCommand objects for all configured TCL devices with room-specific configs."""
        commands = {}
        is_night = (decision.system_state == SystemState.OCCUPIED_NIGHT)

        # Offsets: use parameter if provided, otherwise config
        living_room_offset = lr_offset if lr_offset is not None else getattr(self.config, "LIVING_ROOM_TEMP_OFFSET", 0.0)
        bedroom_offset = br_offset if br_offset is not None else getattr(self.config, "BEDROOM_TEMP_OFFSET", 0.0)

        for dev_id in self.config.DEVICE_IDS:
            is_bedroom = (dev_id == self.config.TCL_BEDROOM_ID)
            is_living = (dev_id == self.config.TCL_LIVING_ROOM_ID)

            # Check per-room enablement and explicit overrides
            if is_bedroom and (bedroom_override is False or not self.config.BEDROOM_ENABLED):
                target_power = False
            elif is_living and (living_override is False or not self.config.LIVING_ROOM_ENABLED):
                target_power = False
            else:
                target_power = decision.target_power

            # Determine room-specific base target temperature and fan speed from setpoints
            room_sp = None
            if setpoints:
                if is_living and "living_room" in setpoints:
                    room_sp = setpoints["living_room"]
                elif is_bedroom and "bedroom" in setpoints:
                    room_sp = setpoints["bedroom"]

            if room_sp:
                if decision.system_state == SystemState.OCCUPIED_MORNING:
                    room_base_temp = float(room_sp.get("morning_temp", decision.target_temperature))
                    room_fan_str = str(room_sp.get("morning_fan", decision.target_fan_speed.value))
                elif decision.system_state == SystemState.OCCUPIED_DAY:
                    room_base_temp = float(room_sp.get("day_temp", decision.target_temperature))
                    room_fan_str = str(room_sp.get("day_fan", decision.target_fan_speed.value))
                elif decision.system_state == SystemState.OCCUPIED_EVENING:
                    room_base_temp = float(room_sp.get("evening_temp", decision.target_temperature))
                    room_fan_str = str(room_sp.get("evening_fan", decision.target_fan_speed.value))
                elif decision.system_state == SystemState.OCCUPIED_NIGHT:
                    room_base_temp = float(room_sp.get("night_temp", decision.target_temperature))
                    room_fan_str = str(room_sp.get("night_fan", decision.target_fan_speed.value))
                else:
                    room_base_temp = decision.target_temperature
                    room_fan_str = decision.target_fan_speed.value
            else:
                room_base_temp = decision.target_temperature
                room_fan_str = decision.target_fan_speed.value

            # Adaptive Guest Comfort Policy:
            is_occupied_diurnal = decision.system_state in (
                SystemState.OCCUPIED_MORNING,
                SystemState.OCCUPIED_DAY,
                SystemState.OCCUPIED_EVENING,
                SystemState.OCCUPIED_NIGHT,
            )
            is_adaptive = getattr(self.config, "ADAPTIVE_COMFORT_ENABLED", True) and is_occupied_diurnal
            pref_temp = (preferred_day_temperatures or {}).get(dev_id)

            if is_adaptive and pref_temp is not None:
                if is_night:
                    # Adaptive Night Setback:
                    delta = getattr(self.config, "NIGHT_SETBACK_DELTA", 1.0)
                    floor = getattr(self.config, "NIGHT_SETBACK_FLOOR", 18.0)
                    t = round(pref_temp - delta, 1)
                    if pref_temp >= floor:
                        t = max(t, floor)
                    else:
                        t = min(t, pref_temp)
                    target_temp = max(self.config.TEMP_MIN_SAFE, min(self.config.TEMP_MAX_SAFE, t))
                else:
                    # Daytime preference preservation (07:00–23:00)
                    target_temp = pref_temp
            else:
                # Apply standard room temperature and offsets
                if is_bedroom:
                    if bedroom_offset != 0.0:
                        target_temp = round(decision.target_temperature + bedroom_offset, 1)
                    else:
                        target_temp = room_base_temp
                elif is_living:
                    if living_room_offset != 0.0:
                        target_temp = round(decision.target_temperature + living_room_offset, 1)
                    else:
                        target_temp = room_base_temp
                else:
                    target_temp = decision.target_temperature

            # Determine fan speed: Night mode enforces QUIET fan
            if is_night:
                room_fan = ACFanSpeed.QUIET
            else:
                try:
                    room_fan = ACFanSpeed(room_fan_str)
                except (ValueError, KeyError):
                    room_fan = decision.target_fan_speed

            # Determine room-specific LED target
            if decision.system_state == SystemState.OWNER_TEST_HOLD:
                room_led = decision.target_display_led if target_power else False
            elif target_power:
                room_led = self.get_room_led_target(dev_id, is_night)
            else:
                room_led = False

            cmd = ACCommand(
                device_id=dev_id,
                power=target_power,
                target_temperature=target_temp,
                mode=decision.target_mode if target_power else ACMode.OFF,
                fan_speed=room_fan,
                eco_mode=decision.target_eco,
                display_led=room_led,
                beep_feedback=decision.target_beep,
                reason=decision.decision_reason
            )
            commands[dev_id] = cmd
        decision.commands_to_send = commands
