"""Data models for HVAC behavior and electricity consumption monitoring."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, Dict, Any, List


@dataclass
class DeviceObservation:
    """A single 15-minute observation of a physical TCL AC unit."""
    observation_id: str                      # Format: {device_id}_{iso_minute}
    timestamp_utc: str                       # ISO 8601 UTC
    timestamp_sofia: str                     # ISO 8601 Europe/Sofia
    device_id: str                           # e.g. "demo_living_room"
    room: str                                # "living_room" or "bedroom"
    power: bool                              # True = ON, False = OFF
    mode: str                                # "Heat", "Cool", "Dry", "Fan", "Auto"
    target_temperature: float                # Selected setpoint in °C
    indoor_temperature: Optional[float] = None  # Measured indoor ambient in °C
    temp_measured_at: Optional[str] = None      # Physical sensor timestamp from shadow metadata (ISO)
    temp_freshness: str = "FRESH"              # "FRESH", "STALE", "UNAVAILABLE"
    temp_age_minutes: Optional[float] = None    # Age in minutes since sensor update
    fan_speed: str = "Auto"                     # "Auto", "Quiet", "Low", "Mid", "High", "Turbo"
    display_led: bool = False                   # True = display on, False = dark
    beep_feedback: bool = False                 # Audible feedback buzzer
    online: bool = True                         # Connectivity status
    shadow_version: Optional[int] = None     # AWS IoT Shadow version counter
    guest_override_active: bool = False      # Whether an active guest override is recorded
    power_off_override: bool = False         # Guest turned unit off during stay
    temp_override: Optional[float] = None    # Guest-specified day temperature
    external_change_detected: bool = False   # Physical state differs from automated schedule
    active_reservation_uid: Optional[str] = None  # Privacy-masked reservation UID
    system_state: str = "UNKNOWN"            # High-level scheduler state
    command_sent: bool = False               # Whether any physical command was sent this cycle
    dry_run: bool = True                     # DRY_RUN safety flag status
    read_only: bool = True                   # READ_ONLY safety flag status
    actual_metered_kwh: Optional[float] = None  # None: TCL Cloud does NOT support energy metering
    metadata_power_timestamp: Optional[str] = None # Exact shadow timestamp for powerSwitch change
    metadata_temp_timestamp: Optional[str] = None  # Exact shadow timestamp for targetTemperature change

    def to_dict(self) -> Dict[str, Any]:
        """Convert observation to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceObservation":
        """Instantiate from dictionary."""
        # Filter keys to match dataclass fields
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class OperationEvent:
    """A detected discrete state-change or operation event."""
    event_id: str                            # Unique deterministic ID
    timestamp_utc: str                       # ISO 8601 UTC
    timestamp_sofia: str                     # ISO 8601 Europe/Sofia
    device_id: str                           # Physical device ID
    room: str                                # "living_room" or "bedroom"
    event_type: str                          # "POWER_ON", "POWER_OFF", "TARGET_TEMP_CHANGE", etc.
    previous_value: Any                      # Value before change
    new_value: Any                           # Value after change
    detection_method: str                    # "METADATA_TIMESTAMP" or "POLL_INTERVAL"
    exact_change_time_sofia: Optional[str] = None  # If metadata provided exact second
    description: str = ""                    # Human-readable explanation
    reservation_uid_masked: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OperationEvent":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class WholePropertyMeterReading:
    """Manual or external meter reading for the whole apartment electricity utility meter."""
    reading_id: str                          # Unique ID
    timestamp_utc: str                       # Time reading was recorded (UTC)
    timestamp_sofia: str                     # Time reading was recorded (Europe/Sofia)
    reading_date: str                        # Date of reading YYYY-MM-DD
    meter_kwh: float                         # Cumulative meter reading in kWh
    daily_consumption_kwh: Optional[float] = None  # Computed kWh used since previous reading
    period_hours: Optional[float] = None     # Hours since previous reading
    recorded_by: str = "owner"               # "owner", "host", or "technician"
    notes: str = ""
    scope: str = "WHOLE_APARTMENT"           # Must be explicitly WHOLE_APARTMENT

    @property
    def total_kwh(self) -> float:
        """Alias for meter_kwh for dashboard and report compatibility."""
        return self.meter_kwh

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["total_kwh"] = self.meter_kwh
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WholePropertyMeterReading":
        d = dict(data)
        if "total_kwh" in d and "meter_kwh" not in d:
            d["meter_kwh"] = d.pop("total_kwh")
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class OutdoorWeatherReading:
    """Outdoor weather observation for the Bansko / Razlog property area."""
    timestamp_utc: str
    timestamp_sofia: str
    temperature_c: float
    humidity_pct: Optional[float] = None
    weather_code: Optional[int] = None
    weather_description: Optional[str] = None
    location: str = "Bansko, Bulgaria (41.8383°N, 23.4885°E)"
    source: str = "Open-Meteo"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutdoorWeatherReading":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class DailyDeviceSummary:
    """Aggregated daily metrics for one AC unit."""
    date: str                                # YYYY-MM-DD
    device_id: str
    room: str
    total_samples: int = 0
    on_samples: int = 0
    approx_on_hours: float = 0.0             # on_samples * (sample_interval_minutes / 60)
    power_on_events: int = 0
    power_off_events: int = 0
    temp_adjustments: int = 0
    min_indoor_temp: Optional[float] = None
    max_indoor_temp: Optional[float] = None
    avg_indoor_temp: Optional[float] = None
    stale_temp_samples: int = 0
    offline_samples: int = 0
    setpoint_distribution: Dict[str, int] = field(default_factory=dict)
    sample_interval_minutes: float = 15.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
