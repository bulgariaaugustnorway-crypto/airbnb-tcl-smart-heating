"""Data models and enums for TCL AC Airbnb Cloud Automation."""

from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Optional, Dict, Any


class ReservationType(str, Enum):
    """Classification of iCal calendar entry."""
    CONFIRMED = "CONFIRMED"          # e.g., SUMMARY:Reserved
    BLOCKED = "BLOCKED"              # e.g., SUMMARY:Airbnb (Not available)
    OWNER_TEST = "OWNER_TEST"        # Temporary owner/test occupancy
    OWNER_PRESENT = "OWNER_PRESENT"  # Temporary owner presence override
    CANCELLED = "CANCELLED"          # Was confirmed, but now removed or flagged cancelled
    UNKNOWN = "UNKNOWN"


class SystemState(str, Enum):
    """High-level automation system state for the property."""
    IDLE_UNOCCUPIED = "IDLE_UNOCCUPIED"          # No active reservation; ACs OFF or anti-freeze
    PRE_ARRIVAL_HEATING = "PRE_ARRIVAL_HEATING"  # 2-3 hours before checkin on arrival date
    OCCUPIED_MORNING = "OCCUPIED_MORNING"        # Guest active: 07:00 - 09:00
    OCCUPIED_DAY = "OCCUPIED_DAY"                # Guest active: 09:00 - 18:00
    OCCUPIED_EVENING = "OCCUPIED_EVENING"        # Guest active: 18:00 - 23:00
    OCCUPIED_NIGHT = "OCCUPIED_NIGHT"            # Guest active: 23:00 - 07:00
    CHECKOUT_SHUTDOWN = "CHECKOUT_SHUTDOWN"      # Check-out day at/after checkout time (11:00)
    OWNER_TEST_HOLD = "OWNER_TEST_HOLD"          # Temporary owner on-site test override
    PAUSED = "PAUSED"                            # Automation suspended; physical device states preserved


class ACMode(str, Enum):
    """Supported TCL AC operating modes."""
    HEAT = "Heat"
    COOL = "Cool"
    DRY = "Dry"
    FAN = "Fan"
    AUTO = "Auto"
    OFF = "Off"


class ACFanSpeed(str, Enum):
    """Supported fan speeds."""
    AUTO = "Auto"
    LOW = "Low"
    MID = "Mid"
    HIGH = "High"
    TURBO = "Turbo"
    QUIET = "Quiet"


class HeatingProfileType(str, Enum):
    """Named heating profiles for Airbnb guest comfort."""
    AT_HOME = "AT_HOME"      # Warm throughout the day (23°C day)
    SKI_DAY = "SKI_DAY"      # Eco daytime setback (21°C day)
    CUSTOM = "CUSTOM"        # User-customized diurnal setpoints


HEATING_PROFILE_PRESETS: Dict[str, Dict[str, Any]] = {
    HeatingProfileType.AT_HOME.value: {
        "morning_temp": 23.0,
        "day_temp": 23.0,
        "evening_temp": 23.0,
        "night_temp": 20.0,
        "morning_fan": "Mid",
        "day_fan": "Mid",
        "evening_fan": "Mid",
        "night_fan": "Quiet",
        "living_room": {
            "morning_temp": 23.0,
            "day_temp": 23.0,
            "evening_temp": 23.0,
            "night_temp": 20.0,
            "morning_fan": "Mid",
            "day_fan": "Mid",
            "evening_fan": "Mid",
            "night_fan": "Quiet",
        },
        "bedroom": {
            "morning_temp": 22.0,
            "day_temp": 21.0,
            "evening_temp": 22.0,
            "night_temp": 20.0,
            "morning_fan": "Mid",
            "day_fan": "Mid",
            "evening_fan": "Mid",
            "night_fan": "Quiet",
        },
    },
    HeatingProfileType.SKI_DAY.value: {
        "morning_temp": 23.0,
        "day_temp": 21.0,
        "evening_temp": 23.0,
        "night_temp": 20.0,
        "morning_fan": "Mid",
        "day_fan": "Auto",
        "evening_fan": "Mid",
        "night_fan": "Quiet",
        "living_room": {
            "morning_temp": 23.0,
            "day_temp": 21.0,
            "evening_temp": 23.0,
            "night_temp": 20.0,
            "morning_fan": "Mid",
            "day_fan": "Auto",
            "evening_fan": "Mid",
            "night_fan": "Quiet",
        },
        "bedroom": {
            "morning_temp": 22.0,
            "day_temp": 20.0,
            "evening_temp": 22.0,
            "night_temp": 20.0,
            "morning_fan": "Mid",
            "day_fan": "Auto",
            "evening_fan": "Mid",
            "night_fan": "Quiet",
        },
    }
}


def mask_uid(uid: Optional[str]) -> str:
    """Safely truncate or hash Airbnb reservation UID for privacy and security in logs."""
    if not uid:
        return "none"
    uid_str = str(uid).strip()
    if len(uid_str) <= 12:
        return uid_str
    return f"{uid_str[:6]}...{uid_str[-4:]}"


@dataclass
class Reservation:
    """Represents a parsed booking from the Airbnb iCal feed."""
    uid: str
    start_date: date            # Check-in day
    end_date: date              # Check-out day (guest leaves morning of end_date)
    summary: str
    reservation_type: ReservationType
    description: str = ""
    raw_status: str = "CONFIRMED"

    @property
    def masked_uid(self) -> str:
        """Truncated identifier for safe logging without exposing full Airbnb UID."""
        return mask_uid(self.uid)

    @property
    def is_confirmed(self) -> bool:
        return self.reservation_type == ReservationType.CONFIRMED

    @property
    def is_blocked(self) -> bool:
        return self.reservation_type == ReservationType.BLOCKED

    def is_occupancy_eligible(self, heat_during_blocked_dates: bool = True) -> bool:
        """Check if this reservation triggers HVAC comfort heating schedule."""
        if self.reservation_type == ReservationType.CONFIRMED:
            return True
        if self.reservation_type == ReservationType.BLOCKED and heat_during_blocked_dates:
            return True
        if self.reservation_type == ReservationType.OWNER_TEST:
            return True
        return False

    @property
    def nights_count(self) -> int:
        return (self.end_date - self.start_date).days

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "summary": self.summary,
            "type": self.reservation_type.value,
            "nights": self.nights_count,
            "raw_status": self.raw_status,
        }


@dataclass
class ACState:
    """Current or desired state of a single TCL AC unit."""
    device_id: str
    power: bool = False
    target_temperature: float = 21.0
    current_temperature: Optional[float] = None
    temp_measured_at: Optional[datetime] = None
    mode: ACMode = ACMode.HEAT
    fan_speed: ACFanSpeed = ACFanSpeed.AUTO
    eco_mode: bool = False
    display_led: bool = False
    beep_feedback: bool = False
    is_online: bool = True
    last_updated: Optional[datetime] = None

    @property
    def online(self) -> bool:
        return self.is_online

    def get_freshness_status(
        self,
        now: Optional[datetime] = None,
        max_fresh_minutes: float = 60.0
    ) -> str:
        """
        Determine temperature freshness classification:
        - UNAVAILABLE: temperature missing, device offline, or timestamp unknown.
        - FRESH: measurement age <= max_fresh_minutes.
        - STALE: measurement age > max_fresh_minutes.
        """
        if self.current_temperature is None or not self.is_online or self.temp_measured_at is None:
            return "UNAVAILABLE"

        from datetime import timezone
        eval_now = now or datetime.now(timezone.utc)
        eval_ts = eval_now
        meas_ts = self.temp_measured_at

        # Harmonize timezone awareness
        if eval_ts.tzinfo is None and meas_ts.tzinfo is not None:
            eval_ts = eval_ts.replace(tzinfo=timezone.utc)
        elif eval_ts.tzinfo is not None and meas_ts.tzinfo is None:
            eval_ts = eval_ts.astimezone(timezone.utc).replace(tzinfo=None)

        age_seconds = max(0.0, (eval_ts - meas_ts).total_seconds())
        if age_seconds <= (max_fresh_minutes * 60.0):
            return "FRESH"
        return "STALE"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "power": self.power,
            "target_temperature": self.target_temperature,
            "current_temperature": self.current_temperature,
            "temp_measured_at": self.temp_measured_at.isoformat() if self.temp_measured_at else None,
            "mode": self.mode.value,
            "fan_speed": self.fan_speed.value,
            "eco_mode": self.eco_mode,
            "display_led": self.display_led,
            "beep_feedback": self.beep_feedback,
            "is_online": self.is_online,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }


@dataclass
class ACCommand:
    """Target command payload to send to a TCL AC unit."""
    device_id: str
    power: bool
    target_temperature: float
    mode: ACMode
    fan_speed: ACFanSpeed
    eco_mode: bool = False
    display_led: bool = False
    beep_feedback: bool = False
    reason: str = ""

    def to_shadow_payload(self) -> Dict[str, Any]:
        """Convert to AWS IoT Device Shadow payload matching TCL Home protocol."""
        fan_map = {"Auto": 0, "Quiet": 1, "Low": 2, "Mid": 4, "High": 6, "Turbo": 7}
        mode_map = {"Auto": 0, "Cool": 1, "Dry": 2, "Fan": 3, "Heat": 4}
        return {
            "state": {
                "desired": {
                    "powerSwitch": 1 if self.power else 0,
                    "targetTemperature": float(self.target_temperature),
                    "workMode": mode_map.get(self.mode.value, 4) if self.mode else 4,
                    "windSpeed7Gear": fan_map.get(self.fan_speed.value, 0) if self.fan_speed else 0,
                    "Eco": 1 if self.eco_mode else 0,
                    "screen": 1 if self.display_led else 0,
                    "beepSwitch": 1 if self.beep_feedback else 0,
                    # Legacy/mock aliases
                    "Power": 1 if self.power else 0,
                    "SetTemp": int(round(self.target_temperature)),
                    "Mode": self.mode.value if self.mode else "Heat",
                    "WindSpeed": self.fan_speed.value if self.fan_speed else "Auto",
                }
            }
        }


@dataclass
class ScheduleDecision:
    """The result of a scheduler evaluation cycle."""
    timestamp: datetime
    system_state: SystemState
    active_reservation: Optional[Reservation]
    target_power: bool
    target_temperature: float
    target_mode: ACMode
    target_fan_speed: ACFanSpeed
    target_eco: bool
    decision_reason: str
    target_display_led: bool = False
    target_beep: bool = False
    active_profile: str = "SKI_DAY"
    commands_to_send: Dict[str, ACCommand] = field(default_factory=dict)


@dataclass
class OwnerTestSession:
    """Temporary owner on-site test mode with automatic expiration."""
    active: bool = False
    started_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    duration_hours: float = 2.0
    target_temperature: float = 22.0
    mode: ACMode = ACMode.HEAT
    fan_speed: ACFanSpeed = ACFanSpeed.AUTO
    display_led: bool = False
    living_room_power: bool = True
    bedroom_power: bool = True
    created_by: str = "owner"
    notes: str = ""
    session_type: str = "OWNER_TEST"  # "OWNER_TEST" (fixed setpoint hold) or "OWNER_PRESENT" (schedule-driven)

    def is_active(self, current_dt: Optional[datetime] = None) -> bool:
        if not self.active or not self.expires_at:
            return False
        now = current_dt or datetime.now()
        # Handle timezone-awareness comparison
        if self.expires_at.tzinfo and now.tzinfo is None:
            now = now.replace(tzinfo=self.expires_at.tzinfo)
        elif self.expires_at.tzinfo is None and now.tzinfo:
            now = now.replace(tzinfo=None)
        return now < self.expires_at

    def remaining_seconds(self, current_dt: Optional[datetime] = None) -> float:
        """Calculate remaining active seconds until expiration."""
        if not self.active or not self.expires_at:
            return 0.0
        now = current_dt or datetime.now()
        if self.expires_at.tzinfo and now.tzinfo is None:
            now = now.replace(tzinfo=self.expires_at.tzinfo)
        elif self.expires_at.tzinfo is None and now.tzinfo:
            now = now.replace(tzinfo=None)
        diff = (self.expires_at - now).total_seconds()
        return max(0.0, diff)

    def is_expiring_soon(self, threshold_minutes: int = 120, current_dt: Optional[datetime] = None) -> bool:
        """Return True if session is active and has less than threshold_minutes remaining."""
        rem = self.remaining_seconds(current_dt)
        return 0.0 < rem <= (threshold_minutes * 60)

    def to_dict(self, current_dt: Optional[datetime] = None) -> Dict[str, Any]:
        now = current_dt or (datetime.now(self.expires_at.tzinfo or None) if self.expires_at else datetime.now())
        rem_sec = self.remaining_seconds(now)
        rem_h = int(rem_sec // 3600)
        rem_m = int((rem_sec % 3600) // 60)
        if rem_h > 0:
            remaining_str = f"{rem_h}h {rem_m}m"
        elif rem_m > 0:
            remaining_str = f"{rem_m}m"
        else:
            remaining_str = "< 1m" if rem_sec > 0 else "Expired"

        expires_formatted = None
        if self.expires_at:
            expires_formatted = self.expires_at.strftime("%H:%M (%d %b) Sofia")

        return {
            "active": self.active,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "expires_at_formatted": expires_formatted,
            "duration_hours": self.duration_hours,
            "remaining_seconds": rem_sec,
            "remaining_str": remaining_str,
            "is_expiring_soon": self.is_expiring_soon(threshold_minutes=120, current_dt=now),
            "target_temperature": self.target_temperature,
            "mode": self.mode.value,
            "fan_speed": self.fan_speed.value,
            "display_led": self.display_led,
            "living_room_power": self.living_room_power,
            "bedroom_power": self.bedroom_power,
            "created_by": self.created_by,
            "notes": self.notes,
            "session_type": self.session_type,
        }
