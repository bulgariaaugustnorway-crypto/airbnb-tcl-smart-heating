"""Configuration management with self-contained relative path resolution."""

import os
import sys
import tempfile
from datetime import time, timedelta
from pathlib import Path
from typing import Dict, Any, Optional

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore

# Base directories - self-contained relative to this file
SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent

# In AWS Lambda environment (/var/task is read-only), use /tmp for runtime cache and logs
IS_LAMBDA = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("LAMBDA_TASK_ROOT"))

if IS_LAMBDA:
    BASE_WRITABLE = Path(tempfile.gettempdir())  # Private Lambda execution environment
    DATA_DIR = BASE_WRITABLE / "data"
    SAMPLE_CALENDARS_DIR = DATA_DIR / "sample_calendars"
    CACHE_DIR = DATA_DIR / "cache"
    LOGS_DIR = BASE_WRITABLE / "logs"
else:
    RUNTIME_ROOT = Path(os.getenv("HEATING_RUNTIME_DIR", str(PROJECT_ROOT)))
    DATA_DIR = RUNTIME_ROOT / "data"
    SAMPLE_CALENDARS_DIR = PROJECT_ROOT / "data" / "sample_calendars"
    CACHE_DIR = DATA_DIR / "cache"
    LOGS_DIR = RUNTIME_ROOT / "logs"

# Ensure runtime directories exist
try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass



def load_env_file(env_path: Optional[Path] = None) -> None:
    """Lightweight pure-python .env loader without third-party dependencies."""
    target = env_path or (PROJECT_ROOT / ".env")
    if not target.exists():
        return
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            # Don't overwrite existing system environment variables
            if k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


# Automatically load .env if present
if os.getenv("HEATING_LOAD_ENV", "true").lower() == "true":
    load_env_file()


# Timezone resolution with graceful fallback
def get_sofia_timezone():
    """Retrieve Europe/Sofia timezone object with graceful fallback."""
    try:
        return zoneinfo.ZoneInfo("Europe/Sofia")
    except Exception:
        # Fallback for systems without tzdata: Sofia is EET (UTC+2) in winter, EEST (UTC+3) in summer.
        from datetime import tzinfo, timedelta, datetime

        class SofiaTimezoneFallback(tzinfo):
            def utcoffset(self, dt):
                if dt is None:
                    return timedelta(hours=2)
                year = dt.year
                march_31 = datetime(year, 3, 31)
                last_sun_march = march_31 - timedelta(days=(march_31.weekday() + 1) % 7)
                oct_31 = datetime(year, 10, 31)
                last_sun_oct = oct_31 - timedelta(days=(oct_31.weekday() + 1) % 7)

                dst_start = datetime(year, 3, last_sun_march.day, 3, 0)
                dst_end = datetime(year, 10, last_sun_oct.day, 4, 0)

                dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
                if dst_start <= dt_naive < dst_end:
                    return timedelta(hours=3)
                return timedelta(hours=2)

            def dst(self, dt):
                offset = self.utcoffset(dt)
                return timedelta(hours=1) if offset == timedelta(hours=3) else timedelta(0)

            def tzname(self, dt):
                offset = self.utcoffset(dt)
                return "EEST" if offset == timedelta(hours=3) else "EET"

        return SofiaTimezoneFallback()


TIMEZONE_SOFIA = get_sofia_timezone()


class AutomationConfig:
    """Central configuration for Airbnb TCL AC automation."""

    # Property Times (Europe/Sofia)
    CHECKIN_TIME: time = time(15, 0)       # 15:00
    CHECKOUT_TIME: time = time(11, 0)      # 11:00
    PRE_ARRIVAL_HOURS: int = 2             # Pre-heat 2 hours before checkin (13:00 - 15:00)

    # Synthetic local aliases; physical IDs must be supplied explicitly.
    TCL_LIVING_ROOM_ID: str = os.getenv("TCL_LIVING_ROOM_ID", "demo_living_room")
    TCL_BEDROOM_ID: str = os.getenv("TCL_BEDROOM_ID", "demo_bedroom")
    LIVING_ROOM_ENABLED: bool = os.getenv("LIVING_ROOM_ENABLED", "true").lower() in ("true", "1", "yes")
    BEDROOM_ENABLED: bool = os.getenv("BEDROOM_ENABLED", "true").lower() in ("true", "1", "yes")
    DEVICE_IDS = [
        TCL_LIVING_ROOM_ID,
        TCL_BEDROOM_ID,
    ]
    BEDROOM_TEMP_OFFSET: float = float(os.getenv("BEDROOM_TEMP_OFFSET", "0.0"))
    LIVING_ROOM_TEMP_OFFSET: float = float(os.getenv("LIVING_ROOM_TEMP_OFFSET", "0.0"))
    DEFAULT_HEATING_PROFILE: str = os.getenv("DEFAULT_HEATING_PROFILE", "SKI_DAY").upper()
    RESERVATION_PROFILES: Dict[str, Any] = {}

    # Target Diurnal Schedules (Occupied Stay)
    # Winter heating profile:
    # 07:00–10:00: 23°C
    # 10:00–17:00: 21°C
    # 17:00–23:00: 23°C
    # 23:00–07:00: 20°C
    SCHEDULE_MORNING_START: time = time(7, 0)      # 07:00 - 10:00
    SCHEDULE_MORNING_TEMP: float = 23.0
    SCHEDULE_MORNING_FAN: str = "Mid"

    SCHEDULE_DAY_START: time = time(10, 0)         # 10:00 - 17:00
    SCHEDULE_DAY_TEMP: float = 21.0
    SCHEDULE_DAY_FAN: str = "Auto"

    SCHEDULE_EVENING_START: time = time(17, 0)     # 17:00 - 23:00
    SCHEDULE_EVENING_TEMP: float = 23.0
    SCHEDULE_EVENING_FAN: str = "Mid"

    SCHEDULE_NIGHT_START: time = time(23, 0)       # 23:00 - 07:00
    SCHEDULE_NIGHT_TEMP: float = 20.0
    SCHEDULE_NIGHT_FAN: str = "Quiet"

    # Independent per-room diurnal temperature setpoints (None = use schedule / profile preset)
    LIVING_ROOM_MORNING_TEMP: Optional[float] = None
    LIVING_ROOM_DAY_TEMP: Optional[float] = None
    LIVING_ROOM_EVENING_TEMP: Optional[float] = None
    LIVING_ROOM_NIGHT_TEMP: Optional[float] = None

    BEDROOM_MORNING_TEMP: Optional[float] = None
    BEDROOM_DAY_TEMP: Optional[float] = None
    BEDROOM_EVENING_TEMP: Optional[float] = None
    BEDROOM_NIGHT_TEMP: Optional[float] = None

    # Pre-arrival settings
    PRE_ARRIVAL_TEMP: float = 23.0                 # 23°C starting 2 hours before check-in
    PRE_ARRIVAL_FAN: str = "Auto"
    PRE_ARRIVAL_MODE: str = "Heat"

    # Unoccupied / Idle Settings
    IDLE_POWER_OFF: bool = True
    ANTI_FREEZE_ENABLED: bool = False              # If true, keeps 10°C when unoccupied in extreme cold
    ANTI_FREEZE_TEMP: float = 10.0

    # Safeguards & Throttling
    DRY_RUN: bool = os.getenv("TCL_DRY_RUN", "true").lower() in ("true", "1", "yes")
    MIN_CYCLE_INTERVAL_SECONDS: int = 600          # 10 minutes debounce / anti-cycling
    TEMP_MIN_SAFE: float = 16.0
    TEMP_MAX_SAFE: float = 30.0
    CALENDAR_MAX_CACHE_HOURS: int = 24
    CALENDAR_MAX_STALE_HOURS: int = int(os.getenv("CALENDAR_MAX_STALE_HOURS", "48"))
    CANCELLATION_GRACE_PERIOD_HOURS: int = int(os.getenv("CANCELLATION_GRACE_PERIOD_HOURS", "4"))
    MANUAL_OVERRIDE_PRESERVE: bool = os.getenv("MANUAL_OVERRIDE_PRESERVE", "true").lower() in ("true", "1", "yes")
    TREAT_IMPORTED_CHANNELS_AS_CONFIRMED: bool = os.getenv(
        "TREAT_IMPORTED_CHANNELS_AS_CONFIRMED", "true"
    ).lower() in ("true", "1", "yes")
    TREAT_MANUAL_BLOCKS_AS_CONFIRMED: bool = os.getenv(
        "TREAT_MANUAL_BLOCKS_AS_CONFIRMED", "false"
    ).lower() in ("true", "1", "yes")
    HEAT_DURING_BLOCKED_DATES: bool = os.getenv(
        "HEAT_DURING_BLOCKED_DATES", "false"
    ).lower() in ("true", "1", "yes")

    # Hardware LED Display and Audio Feedback Preferences
    # New Phase 5 Defaults:
    # 07:00–23:00: LED display ON (screen: 1) when AC is running for Airbnb guest clarity.
    # 23:00–07:00: LED display OFF (screen: 0) to avoid disturbing guests while sleeping.
    # All hours: Beep feedback OFF (beepSwitch: 0) for silent unattended operation.
    DEFAULT_LED_DISPLAY: bool = os.getenv("DEFAULT_LED_DISPLAY", "true").lower() in ("true", "1", "yes")
    NIGHT_LED_DISPLAY: bool = os.getenv("NIGHT_LED_DISPLAY", "false").lower() in ("true", "1", "yes")
    BEEP_FEEDBACK_ENABLED: bool = os.getenv("BEEP_FEEDBACK_ENABLED", "false").lower() in ("true", "1", "yes")

    # Per-Room Independent LED Display Preferences:
    # "SCHEDULE": Follow day/night schedule (07:00–23:00 ON, 23:00–07:00 OFF)
    # "ALWAYS_OFF": Keep unit's display permanently dark / OFF 24/7
    # "ALWAYS_ON": Keep unit's display permanently lit when AC is running
    LIVING_ROOM_LED_MODE: str = os.getenv("LIVING_ROOM_LED_MODE", "SCHEDULE").upper()
    BEDROOM_LED_MODE: str = os.getenv("BEDROOM_LED_MODE", "SCHEDULE").upper()

    # System Mode: AUTO (calendar-driven), MANUAL (hold temperature), PAUSED (all off)
    SYSTEM_MODE: str = os.getenv("SYSTEM_MODE", "AUTO")

    # Adaptive Guest Comfort & Energy Optimization Policy
    ADAPTIVE_COMFORT_ENABLED: bool = os.getenv("ADAPTIVE_COMFORT_ENABLED", "true").lower() in ("true", "1", "yes")
    NIGHT_SETBACK_DELTA: float = float(os.getenv("NIGHT_SETBACK_DELTA", "1.0"))
    NIGHT_SETBACK_FLOOR: float = float(os.getenv("NIGHT_SETBACK_FLOOR", "18.0"))
    ECONOMIC_MAX_DAY_TEMP: float = float(os.getenv("ECONOMIC_MAX_DAY_TEMP", "24.0"))
    ECONOMIC_ENFORCEMENT_ENABLED: bool = os.getenv("ECONOMIC_ENFORCEMENT_ENABLED", "false").lower() in ("true", "1", "yes")
    ECONOMIC_WINDOW_START: time = time(17, 0)
    ECONOMIC_WINDOW_END: time = time(18, 0)
    MORNING_WINDOW_START: time = time(7, 0)
    MORNING_WINDOW_END: time = time(7, 30)
    NIGHT_WINDOW_START: time = time(23, 0)
    NIGHT_WINDOW_END: time = time(23, 30)
    GUEST_PREFERENCE_PRESERVATION: bool = os.getenv("GUEST_PREFERENCE_PRESERVATION", "true").lower() in ("true", "1", "yes")

    # Adaptive Proposed Starting Defaults (when no guest preference recorded yet)
    ADAPTIVE_DEFAULT_DAY_TEMP_LIVING: float = float(os.getenv("ADAPTIVE_DEFAULT_DAY_TEMP_LIVING", "22.0"))
    ADAPTIVE_DEFAULT_DAY_TEMP_BEDROOM: float = float(os.getenv("ADAPTIVE_DEFAULT_DAY_TEMP_BEDROOM", "21.0"))

    # Multi-Tier Low Temperature & Frost Monitoring
    LOW_TEMP_WARNING_THRESHOLD: float = float(os.getenv("LOW_TEMP_WARNING_THRESHOLD", "16.0"))
    LOW_TEMP_ELEVATED_THRESHOLD: float = float(os.getenv("LOW_TEMP_ELEVATED_THRESHOLD", "12.0"))
    LOW_TEMP_CRITICAL_THRESHOLD: float = float(os.getenv("LOW_TEMP_CRITICAL_THRESHOLD", "8.0"))
    LOW_TEMP_ALERT_COOLDOWN_HOURS: float = float(os.getenv("LOW_TEMP_ALERT_COOLDOWN_HOURS", "4.0"))
    TEMP_FRESHNESS_MAX_AGE_MINUTES: float = float(os.getenv("TEMP_FRESHNESS_MAX_AGE_MINUTES", "60.0"))
    PROLONGED_STALE_ALERT_HOURS: float = float(os.getenv("PROLONGED_STALE_ALERT_HOURS", "3.0"))

    # Owner Dashboard & Web Security
    DASHBOARD_PASSWORD_HASH: Optional[str] = os.getenv("DASHBOARD_PASSWORD_HASH", None)
    SESSION_SECRET_KEY: str = os.getenv("SESSION_SECRET_KEY", "")

    # External URLs & Notification Channels (overridden via environment variables / SSM)
    AIRBNB_ICAL_URL: Optional[str] = os.getenv("AIRBNB_ICAL_URL", None)
    NOTIFICATION_WEBHOOK_URL: Optional[str] = os.getenv("NOTIFICATION_WEBHOOK_URL", None)
    TELEGRAM_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN", None)
    TELEGRAM_CHAT_ID: Optional[str] = os.getenv("TELEGRAM_CHAT_ID", None)
    TELEGRAM_MAX_RETRIES: int = int(os.getenv("TELEGRAM_MAX_RETRIES", "2"))
    TELEGRAM_RETRY_BACKOFF_SECONDS: float = float(os.getenv("TELEGRAM_RETRY_BACKOFF_SECONDS", "1.0"))
    TELEGRAM_TIMEOUT_SECONDS: float = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "5.0"))
    ALERT_DEDUPLICATION_WINDOW_MINUTES: float = float(os.getenv("ALERT_DEDUPLICATION_WINDOW_MINUTES", "30.0"))

    @property
    def is_telegram_configured(self) -> bool:
        """Return True if both bot token and chat ID are configured."""
        token = getattr(self, "TELEGRAM_BOT_TOKEN", None)
        chat_id = getattr(self, "TELEGRAM_CHAT_ID", None)
        return bool(token and chat_id)

    # Cloud Persistence (AWS S3)
    S3_BUCKET: Optional[str] = os.getenv("S3_BUCKET", None)
    S3_STATE_KEY: str = os.getenv("S3_STATE_KEY", "state/automation_state.json")
    S3_CALENDAR_KEY: str = os.getenv("S3_CALENDAR_KEY", "cache/airbnb_calendar_cache.ics")
    S3_CONFIG_KEY: str = os.getenv("S3_CONFIG_KEY", "config/dashboard_config.json")
    S3_OWNER_TEST_KEY: str = os.getenv("S3_OWNER_TEST_KEY", "state/owner_test_state.json")

    # TCL Cloud Configuration
    TCL_USERNAME: Optional[str] = os.getenv("TCL_USERNAME", None)
    TCL_PASSWORD: Optional[str] = os.getenv("TCL_PASSWORD", None)
    TCL_APP_LOGIN_URL: str = os.getenv(
        "TCL_APP_LOGIN_URL", "https://pa.account.tcl.com/account/login?clientId=54148614"
    )
    TCL_APP_CLOUD_URL: str = os.getenv(
        "TCL_APP_CLOUD_URL", "https://prod-center.aws.tcljd.com/v3/global/cloud_url_get"
    )
    TCL_APP_ID: str = os.getenv("TCL_APP_ID", "wx6e1af3fa84fbe523")
    TCL_READ_ONLY: bool = os.getenv("TCL_READ_ONLY", "true").lower() in ("true", "1", "yes")

    # Shelly Cloud Extensibility Configuration (Future Support)
    SHELLY_AUTH_KEY: Optional[str] = os.getenv("SHELLY_AUTH_KEY", None)
    SHELLY_SERVER_URL: Optional[str] = os.getenv("SHELLY_SERVER_URL", None)

    # 7-Day Read-Only Monitoring & Telemetry Collection
    MONITORING_ENABLED: bool = os.getenv("MONITORING_ENABLED", "false").lower() in ("true", "1", "yes")
    S3_OBSERVATIONS_PREFIX: str = os.getenv("S3_OBSERVATIONS_PREFIX", "history/observations/")
    S3_METER_READINGS_KEY: str = os.getenv("S3_METER_READINGS_KEY", "history/meter_readings.json")

    # Local Cache Files
    DATA_DIR: Path = DATA_DIR
    CACHE_DIR: Path = CACHE_DIR
    LOGS_DIR: Path = LOGS_DIR
    CALENDAR_CACHE_FILE: Path = CACHE_DIR / "airbnb_calendar_cache.ics"
    STATE_CACHE_FILE: Path = CACHE_DIR / "automation_state.json"
    AUDIT_LOG_FILE: Path = LOGS_DIR / "automation_audit.log"
    OBSERVATIONS_DIR: Path = DATA_DIR / "history" / "observations"
    METER_READINGS_FILE: Path = DATA_DIR / "history" / "meter_readings.json"
