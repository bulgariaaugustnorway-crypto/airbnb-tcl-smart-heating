"""Configuration store managing owner dashboard settings and persistence."""

import hashlib
import json
import logging
import os
import threading
import time as py_time
from datetime import datetime, date, time
from typing import Dict, Any, Optional, List
from pathlib import Path

from src.config import AutomationConfig, TIMEZONE_SOFIA, CACHE_DIR
from src.models import OwnerTestSession, ACMode, ACFanSpeed

_LOGGER = logging.getLogger(__name__)
_CONFIG_MUTEX = threading.Lock()


class ConcurrencyConflictError(Exception):
    """Raised when an optimistic concurrency check fails during configuration update."""
    pass


class DashboardConfigStore:
    """Manages persistent dashboard settings and Owner Test sessions in AWS S3 or local cache."""

    def __init__(self, config: Optional[AutomationConfig] = None):
        self.config = config or AutomationConfig()

    def load_dashboard_config(self) -> Dict[str, Any]:
        """Load user-configurable settings from S3 or fallback to environment defaults."""
        defaults = {
            # Schedules
            "schedule_morning_temp": self.config.SCHEDULE_MORNING_TEMP,
            "schedule_morning_fan": self.config.SCHEDULE_MORNING_FAN,
            "schedule_day_temp": self.config.SCHEDULE_DAY_TEMP,
            "schedule_day_fan": self.config.SCHEDULE_DAY_FAN,
            "schedule_evening_temp": self.config.SCHEDULE_EVENING_TEMP,
            "schedule_evening_fan": self.config.SCHEDULE_EVENING_FAN,
            "schedule_night_temp": self.config.SCHEDULE_NIGHT_TEMP,
            "schedule_night_fan": self.config.SCHEDULE_NIGHT_FAN,

            # Check-in / Checkout
            "checkin_time": self.config.CHECKIN_TIME.strftime("%H:%M"),
            "checkout_time": self.config.CHECKOUT_TIME.strftime("%H:%M"),
            "pre_arrival_hours": self.config.PRE_ARRIVAL_HOURS,
            "pre_arrival_temp": self.config.PRE_ARRIVAL_TEMP,

            # Rooms
            "living_room_enabled": self.config.LIVING_ROOM_ENABLED,
            "bedroom_enabled": self.config.BEDROOM_ENABLED,
            "living_room_temp_offset": getattr(self.config, "LIVING_ROOM_TEMP_OFFSET", 0.0),
            "bedroom_temp_offset": self.config.BEDROOM_TEMP_OFFSET,

            # Heating Profiles & Guest Customization
            "default_heating_profile": getattr(self.config, "DEFAULT_HEATING_PROFILE", "SKI_DAY"),
            "reservation_profiles": dict(getattr(self.config, "RESERVATION_PROFILES", {})),

            # Hardware Preferences
            "default_led_display": self.config.DEFAULT_LED_DISPLAY,
            "night_led_display": self.config.NIGHT_LED_DISPLAY,
            "beep_feedback_enabled": self.config.BEEP_FEEDBACK_ENABLED,
            "living_room_led_mode": getattr(self.config, "LIVING_ROOM_LED_MODE", "SCHEDULE"),
            "bedroom_led_mode": getattr(self.config, "BEDROOM_LED_MODE", "SCHEDULE"),

            # Occupancy & System Mode
            "heat_during_blocked_dates": self.config.HEAT_DURING_BLOCKED_DATES,
            "system_mode": getattr(self.config, "SYSTEM_MODE", "AUTO"),
            "manual_override_preserve": self.config.MANUAL_OVERRIDE_PRESERVE,

            # Adaptive Guest Comfort & Energy Optimization
            "adaptive_comfort_enabled": getattr(self.config, "ADAPTIVE_COMFORT_ENABLED", True),
            "night_setback_delta": getattr(self.config, "NIGHT_SETBACK_DELTA", 1.0),
            "night_setback_floor": getattr(self.config, "NIGHT_SETBACK_FLOOR", 18.0),
            "economic_max_day_temp": getattr(self.config, "ECONOMIC_MAX_DAY_TEMP", 24.0),
            "economic_enforcement_enabled": getattr(self.config, "ECONOMIC_ENFORCEMENT_ENABLED", False),
            "economic_window_start": getattr(self.config, "ECONOMIC_WINDOW_START", time(17, 0)).strftime("%H:%M") if hasattr(getattr(self.config, "ECONOMIC_WINDOW_START", None), "strftime") else "17:00",
            "economic_window_end": getattr(self.config, "ECONOMIC_WINDOW_END", time(18, 0)).strftime("%H:%M") if hasattr(getattr(self.config, "ECONOMIC_WINDOW_END", None), "strftime") else "18:00",
            "guest_preference_preservation": getattr(self.config, "GUEST_PREFERENCE_PRESERVATION", True),

            # Concurrency & Version Metadata
            "version": 1,
            "updated_at": None,
            "_etag": None,
        }

        if not self.config.S3_BUCKET:
            key_filename = Path(self.config.S3_CONFIG_KEY).name or "dashboard_config.json"
            local_cache = CACHE_DIR / key_filename
            if not local_cache.exists():
                alt_name = "dashboard_settings.json" if key_filename == "dashboard_config.json" else "dashboard_config.json"
                alt_path = CACHE_DIR / alt_name
                if alt_path.exists():
                    local_cache = alt_path
            if local_cache.exists():
                try:
                    raw_text = local_cache.read_text(encoding="utf-8")
                    loaded = json.loads(raw_text)
                    loaded["_etag"] = f"W/{hashlib.sha256(raw_text.encode('utf-8')).hexdigest()}"
                    defaults.update(loaded)
                except Exception:
                    pass
            return defaults

        try:
            import boto3
            s3 = boto3.client("s3")
            resp = s3.get_object(Bucket=self.config.S3_BUCKET, Key=self.config.S3_CONFIG_KEY)
            raw_text = resp["Body"].read().decode("utf-8")
            loaded = json.loads(raw_text)
            loaded["_etag"] = resp.get("ETag")
            defaults.update(loaded)
            return defaults
        except Exception as e:
            _LOGGER.debug(f"Could not load dashboard config from S3 (using defaults): {e}")
            return defaults

    ALLOWED_CONFIG_KEYS = {
        "schedule_morning_temp", "schedule_morning_fan",
        "schedule_day_temp", "schedule_day_fan",
        "schedule_evening_temp", "schedule_evening_fan",
        "schedule_night_temp", "schedule_night_fan",
        "living_room_morning_temp", "living_room_day_temp",
        "living_room_evening_temp", "living_room_night_temp",
        "bedroom_morning_temp", "bedroom_day_temp",
        "bedroom_evening_temp", "bedroom_night_temp",
        "checkin_time", "checkout_time",
        "pre_arrival_hours", "pre_arrival_temp",
        "living_room_enabled", "bedroom_enabled",
        "living_room_temp_offset", "bedroom_temp_offset",
        "default_heating_profile", "reservation_profiles",
        "default_led_display", "night_led_display", "beep_feedback_enabled",
        "living_room_led_mode", "bedroom_led_mode",
        "heat_during_blocked_dates", "system_mode", "manual_override_preserve",
        "adaptive_comfort_enabled", "night_setback_delta", "night_setback_floor",
        "economic_max_day_temp", "economic_enforcement_enabled",
        "economic_window_start", "economic_window_end", "guest_preference_preservation"
    }

    def save_dashboard_config(
        self,
        new_settings: Dict[str, Any],
        expected_updated_at: Optional[str] = None,
        expected_version: Optional[int] = None,
        expected_etag: Optional[str] = None
    ) -> bool:
        """Persist updated dashboard settings to S3 or local cache with atomic write, key validation, and optimistic concurrency checks."""
        with _CONFIG_MUTEX:
            current = self.load_dashboard_config()

            # Check for optimistic concurrency conflict
            exp_upd = expected_updated_at if expected_updated_at is not None else new_settings.get("base_updated_at")
            exp_ver = expected_version if expected_version is not None else new_settings.get("base_version")
            exp_etag = expected_etag if expected_etag is not None else new_settings.get("base_etag")

            current_upd = current.get("updated_at")
            current_ver = current.get("version", 1)
            current_etag = current.get("_etag")

            if exp_upd is not None and current_upd is not None and str(exp_upd).strip() != str(current_upd).strip():
                _LOGGER.warning(f"Config conflict: expected updated_at={exp_upd}, but found {current_upd}")
                raise ConcurrencyConflictError(f"Configuration conflict: expected updated_at={exp_upd}, current={current_upd}")

            if exp_ver is not None and current_ver is not None:
                try:
                    if int(exp_ver) != int(current_ver):
                        _LOGGER.warning(f"Config conflict: expected version={exp_ver}, but found {current_ver}")
                        raise ConcurrencyConflictError(f"Configuration conflict: expected version={exp_ver}, current={current_ver}")
                except (ValueError, TypeError):
                    pass

            if exp_etag is not None and current_etag is not None and str(exp_etag).strip() != str(current_etag).strip():
                _LOGGER.warning(f"Config conflict: expected etag={exp_etag}, but found {current_etag}")
                raise ConcurrencyConflictError(f"Configuration conflict: expected etag={exp_etag}, current={current_etag}")

            # Filter for strictly allowed dashboard configuration keys
            filtered_updates = {
                k: v for k, v in new_settings.items()
                if k in self.ALLOWED_CONFIG_KEYS
            }
            # Unrelated configuration fields and reservation profiles are fully preserved
            current.update(filtered_updates)
            current["version"] = int(current_ver) + 1
            current["updated_at"] = datetime.now(TIMEZONE_SOFIA).isoformat()
            loaded_etag = current.pop("_etag", None)

            if not self.config.S3_BUCKET:
                try:
                    key_filename = Path(self.config.S3_CONFIG_KEY).name or "dashboard_config.json"
                    local_cache = CACHE_DIR / key_filename
                    local_cache.parent.mkdir(parents=True, exist_ok=True)

                    target_etag = exp_etag or loaded_etag
                    if local_cache.exists() and target_etag:
                        curr_text = local_cache.read_text(encoding="utf-8")
                        curr_etag = f"W/{hashlib.sha256(curr_text.encode('utf-8')).hexdigest()}"
                        if curr_etag != target_etag:
                            raise ConcurrencyConflictError(
                                f"Local cache conditional write conflict: expected {target_etag}, found {curr_etag}"
                            )

                    # Atomic file write via temporary file replacement
                    tmp_file = local_cache.with_suffix(f".tmp.{os.getpid()}.{py_time.time_ns()}")
                    tmp_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
                    tmp_file.replace(local_cache)
                    _LOGGER.info(f"Dashboard configuration updated successfully in local cache ({local_cache.name})")
                    return True
                except ConcurrencyConflictError:
                    raise
                except Exception as e:
                    _LOGGER.error(f"Failed to save local dashboard config: {e}")
                    return False

            try:
                import boto3
                from botocore.exceptions import ClientError
                s3 = boto3.client("s3")
                put_kwargs = {
                    "Bucket": self.config.S3_BUCKET,
                    "Key": self.config.S3_CONFIG_KEY,
                    "Body": json.dumps(current, indent=2).encode("utf-8"),
                    "ContentType": "application/json"
                }

                target_etag = exp_etag or loaded_etag
                if target_etag:
                    put_kwargs["IfMatch"] = target_etag
                elif exp_ver == 1:
                    put_kwargs["IfNoneMatch"] = "*"

                s3.put_object(**put_kwargs)
                _LOGGER.info(f"Dashboard configuration updated successfully in S3 (version={current['version']})")
                return True
            except ClientError as ce:
                error_code = ce.response.get("Error", {}).get("Code", "")
                if error_code in ("PreconditionFailed", "412"):
                    _LOGGER.warning(f"S3 conditional write failed (concurrent modification): {ce}")
                    raise ConcurrencyConflictError(
                        f"Configuration conflict: S3 object was modified concurrently by another process ({error_code})"
                    ) from ce
                _LOGGER.error(f"Failed to save dashboard configuration to S3: {ce}")
                return False
            except Exception as e:
                _LOGGER.error(f"Failed to save dashboard configuration to S3: {e}")
                return False

    def load_owner_test_session(self) -> Optional[OwnerTestSession]:
        """Load currently active Owner Test session from S3 or local cache."""
        data = None
        if not self.config.S3_BUCKET:
            local_test_file = CACHE_DIR / "owner_test_session.json"
            if local_test_file.exists():
                try:
                    data = json.loads(local_test_file.read_text(encoding="utf-8"))
                except Exception:
                    data = None
        else:
            try:
                import boto3
                s3 = boto3.client("s3")
                resp = s3.get_object(Bucket=self.config.S3_BUCKET, Key=self.config.S3_OWNER_TEST_KEY)
                data = json.loads(resp["Body"].read().decode("utf-8"))
            except Exception as e:
                _LOGGER.debug(f"Could not load owner test session from S3: {e}")
                data = None

        if not data or not data.get("active"):
            return None

        try:
            session = OwnerTestSession(
                active=bool(data.get("active", False)),
                started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
                expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
                duration_hours=float(data.get("duration_hours", 2.0)),
                target_temperature=float(data.get("target_temperature", 22.0)),
                mode=ACMode(data.get("mode", "Heat")),
                fan_speed=ACFanSpeed(data.get("fan_speed", "Auto")),
                display_led=bool(data.get("display_led", False)),
                living_room_power=bool(data.get("living_room_power", True)),
                bedroom_power=bool(data.get("bedroom_power", True)),
                created_by=str(data.get("created_by", "owner")),
                notes=str(data.get("notes", "")),
                session_type=str(data.get("session_type", "OWNER_TEST"))
            )
            # Verify expiry
            now = datetime.now(TIMEZONE_SOFIA)
            if not session.is_active(now):
                _LOGGER.info("Stored owner test session has expired; clearing session.")
                self.clear_owner_test_session()
                return None
            return session
        except Exception as e:
            _LOGGER.debug(f"Could not parse owner test session: {e}")
            return None

    def save_owner_test_session(self, session: OwnerTestSession) -> bool:
        """Persist Owner Test session to S3 or local cache."""
        if not self.config.S3_BUCKET:
            try:
                local_test_file = CACHE_DIR / "owner_test_session.json"
                local_test_file.parent.mkdir(parents=True, exist_ok=True)
                local_test_file.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
                _LOGGER.info(f"Owner test session saved to local cache (expires at {session.expires_at})")
                return True
            except Exception as e:
                _LOGGER.error(f"Failed to save local owner test session: {e}")
                return False

        try:
            import boto3
            s3 = boto3.client("s3")
            s3.put_object(
                Bucket=self.config.S3_BUCKET,
                Key=self.config.S3_OWNER_TEST_KEY,
                Body=json.dumps(session.to_dict(), indent=2).encode("utf-8"),
                ContentType="application/json"
            )
            _LOGGER.info(f"Owner test session saved to S3 (expires at {session.expires_at})")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to save owner test session to S3: {e}")
            return False

    def clear_owner_test_session(self) -> bool:
        """Clear/deactivate Owner Test session."""
        if not self.config.S3_BUCKET:
            try:
                local_test_file = CACHE_DIR / "owner_test_session.json"
                if local_test_file.exists():
                    local_test_file.write_text(
                        json.dumps({"active": False, "cleared_at": datetime.now().isoformat()}),
                        encoding="utf-8"
                    )
                return True
            except Exception as e:
                _LOGGER.error(f"Failed to clear local owner test session: {e}")
                return False

        try:
            import boto3
            s3 = boto3.client("s3")
            s3.put_object(
                Bucket=self.config.S3_BUCKET,
                Key=self.config.S3_OWNER_TEST_KEY,
                Body=json.dumps({"active": False, "cleared_at": datetime.now().isoformat()}).encode("utf-8"),
                ContentType="application/json"
            )
            _LOGGER.info("Owner test session cleared from S3")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to clear owner test session in S3: {e}")
            return False

    def get_reservation_profile(self, uid: str) -> Optional[Dict[str, Any]]:
        """Get heating profile assigned to a specific reservation UID."""
        config_data = self.load_dashboard_config()
        profiles = config_data.get("reservation_profiles", {})
        return profiles.get(uid)

    def set_reservation_profile(
        self,
        uid: str,
        profile_type: str,
        custom_temperatures: Optional[Dict[str, float]] = None,
        living_room_offset: Optional[float] = None,
        bedroom_offset: Optional[float] = None,
        end_date: Optional[str] = None
    ) -> bool:
        """Assign a heating profile to a specific reservation."""
        config_data = self.load_dashboard_config()
        profiles = dict(config_data.get("reservation_profiles", {}))
        profiles[uid] = {
            "uid": uid,
            "profile": str(profile_type).upper(),
            "custom_temperatures": custom_temperatures or {},
            "living_room_offset": living_room_offset,
            "bedroom_offset": bedroom_offset,
            "end_date": str(end_date) if end_date else None,
            "updated_at": datetime.now(TIMEZONE_SOFIA).isoformat()
        }
        self.config.RESERVATION_PROFILES = profiles
        return self.save_dashboard_config({"reservation_profiles": profiles})

    def clear_reservation_profile(self, uid: str) -> bool:
        """Remove assigned profile for a reservation."""
        config_data = self.load_dashboard_config()
        profiles = dict(config_data.get("reservation_profiles", {}))
        if uid in profiles:
            del profiles[uid]
            self.config.RESERVATION_PROFILES = profiles
            return self.save_dashboard_config({"reservation_profiles": profiles})
        return True

    def cleanup_expired_profiles(
        self,
        current_dt: datetime,
        reservations: Optional[List[Any]] = None
    ) -> int:
        """
        Automatically expire and purge reservation profiles whose checkout time has passed.
        Ensures profiles do not linger after guest checkout or affect future bookings.
        """
        config_data = self.load_dashboard_config()
        profiles = dict(config_data.get("reservation_profiles", {}))
        if not profiles:
            return 0

        now_date = current_dt.date()
        checkout_time = self.config.CHECKOUT_TIME
        now_time = current_dt.time()

        res_end_map = {}
        if reservations:
            for r in reservations:
                uid = getattr(r, "uid", None)
                end_date = getattr(r, "end_date", None)
                if uid and end_date:
                    res_end_map[uid] = end_date

        expired_uids = []
        for uid, pdata in list(profiles.items()):
            end_date = res_end_map.get(uid)
            if not end_date and isinstance(pdata, dict) and pdata.get("end_date"):
                try:
                    end_date = date.fromisoformat(pdata["end_date"])
                except Exception:
                    end_date = None

            if end_date:
                if now_date > end_date or (now_date == end_date and now_time >= checkout_time):
                    expired_uids.append(uid)

        if expired_uids:
            for uid in expired_uids:
                profiles.pop(uid, None)
            self.save_dashboard_config({"reservation_profiles": profiles})
            self.config.RESERVATION_PROFILES = profiles
            _LOGGER.info(f"Automatically purged {len(expired_uids)} expired reservation profiles: {expired_uids}")

        return len(expired_uids)

    def apply_to_config(self, target_config: AutomationConfig) -> AutomationConfig:
        """Hydrate an AutomationConfig instance with persistent dashboard settings."""
        settings = self.load_dashboard_config()

        target_config.SCHEDULE_MORNING_TEMP = float(settings.get("schedule_morning_temp", target_config.SCHEDULE_MORNING_TEMP))
        target_config.SCHEDULE_MORNING_FAN = str(settings.get("schedule_morning_fan", target_config.SCHEDULE_MORNING_FAN))
        target_config.SCHEDULE_DAY_TEMP = float(settings.get("schedule_day_temp", target_config.SCHEDULE_DAY_TEMP))
        target_config.SCHEDULE_DAY_FAN = str(settings.get("schedule_day_fan", target_config.SCHEDULE_DAY_FAN))
        target_config.SCHEDULE_EVENING_TEMP = float(settings.get("schedule_evening_temp", target_config.SCHEDULE_EVENING_TEMP))
        target_config.SCHEDULE_EVENING_FAN = str(settings.get("schedule_evening_fan", target_config.SCHEDULE_EVENING_FAN))
        target_config.SCHEDULE_NIGHT_TEMP = float(settings.get("schedule_night_temp", target_config.SCHEDULE_NIGHT_TEMP))
        target_config.SCHEDULE_NIGHT_FAN = str(settings.get("schedule_night_fan", target_config.SCHEDULE_NIGHT_FAN))

        if "living_room_morning_temp" in settings and settings["living_room_morning_temp"] is not None:
            target_config.LIVING_ROOM_MORNING_TEMP = float(settings["living_room_morning_temp"])
        if "living_room_day_temp" in settings and settings["living_room_day_temp"] is not None:
            target_config.LIVING_ROOM_DAY_TEMP = float(settings["living_room_day_temp"])
        if "living_room_evening_temp" in settings and settings["living_room_evening_temp"] is not None:
            target_config.LIVING_ROOM_EVENING_TEMP = float(settings["living_room_evening_temp"])
        if "living_room_night_temp" in settings and settings["living_room_night_temp"] is not None:
            target_config.LIVING_ROOM_NIGHT_TEMP = float(settings["living_room_night_temp"])

        if "bedroom_morning_temp" in settings and settings["bedroom_morning_temp"] is not None:
            target_config.BEDROOM_MORNING_TEMP = float(settings["bedroom_morning_temp"])
        if "bedroom_day_temp" in settings and settings["bedroom_day_temp"] is not None:
            target_config.BEDROOM_DAY_TEMP = float(settings["bedroom_day_temp"])
        if "bedroom_evening_temp" in settings and settings["bedroom_evening_temp"] is not None:
            target_config.BEDROOM_EVENING_TEMP = float(settings["bedroom_evening_temp"])
        if "bedroom_night_temp" in settings and settings["bedroom_night_temp"] is not None:
            target_config.BEDROOM_NIGHT_TEMP = float(settings["bedroom_night_temp"])

        if "checkin_time" in settings:
            try:
                h, m = settings["checkin_time"].split(":")
                target_config.CHECKIN_TIME = time(int(h), int(m))
            except Exception:
                pass

        if "checkout_time" in settings:
            try:
                h, m = settings["checkout_time"].split(":")
                target_config.CHECKOUT_TIME = time(int(h), int(m))
            except Exception:
                pass

        target_config.PRE_ARRIVAL_HOURS = int(settings.get("pre_arrival_hours", target_config.PRE_ARRIVAL_HOURS))
        target_config.PRE_ARRIVAL_TEMP = float(settings.get("pre_arrival_temp", target_config.PRE_ARRIVAL_TEMP))
        target_config.LIVING_ROOM_ENABLED = bool(settings.get("living_room_enabled", target_config.LIVING_ROOM_ENABLED))
        target_config.BEDROOM_ENABLED = bool(settings.get("bedroom_enabled", target_config.BEDROOM_ENABLED))
        target_config.LIVING_ROOM_TEMP_OFFSET = float(settings.get("living_room_temp_offset", getattr(target_config, "LIVING_ROOM_TEMP_OFFSET", 0.0)))
        target_config.BEDROOM_TEMP_OFFSET = float(settings.get("bedroom_temp_offset", target_config.BEDROOM_TEMP_OFFSET))
        target_config.DEFAULT_HEATING_PROFILE = str(settings.get("default_heating_profile", getattr(target_config, "DEFAULT_HEATING_PROFILE", "SKI_DAY"))).upper()
        target_config.RESERVATION_PROFILES = dict(settings.get("reservation_profiles", getattr(target_config, "RESERVATION_PROFILES", {})))
        target_config.DEFAULT_LED_DISPLAY = bool(settings.get("default_led_display", target_config.DEFAULT_LED_DISPLAY))
        target_config.NIGHT_LED_DISPLAY = bool(settings.get("night_led_display", target_config.NIGHT_LED_DISPLAY))
        target_config.BEEP_FEEDBACK_ENABLED = bool(settings.get("beep_feedback_enabled", target_config.BEEP_FEEDBACK_ENABLED))
        target_config.LIVING_ROOM_LED_MODE = str(settings.get("living_room_led_mode", getattr(target_config, "LIVING_ROOM_LED_MODE", "SCHEDULE"))).upper()
        target_config.BEDROOM_LED_MODE = str(settings.get("bedroom_led_mode", getattr(target_config, "BEDROOM_LED_MODE", "SCHEDULE"))).upper()
        target_config.HEAT_DURING_BLOCKED_DATES = bool(settings.get("heat_during_blocked_dates", target_config.HEAT_DURING_BLOCKED_DATES))
        target_config.SYSTEM_MODE = str(settings.get("system_mode", getattr(target_config, "SYSTEM_MODE", "AUTO")))
        target_config.MANUAL_OVERRIDE_PRESERVE = bool(settings.get("manual_override_preserve", target_config.MANUAL_OVERRIDE_PRESERVE))

        if "adaptive_comfort_enabled" in settings:
            target_config.ADAPTIVE_COMFORT_ENABLED = bool(settings["adaptive_comfort_enabled"])
        if "night_setback_delta" in settings:
            target_config.NIGHT_SETBACK_DELTA = float(settings["night_setback_delta"])
        if "night_setback_floor" in settings:
            target_config.NIGHT_SETBACK_FLOOR = float(settings["night_setback_floor"])
        if "economic_max_day_temp" in settings:
            target_config.ECONOMIC_MAX_DAY_TEMP = float(settings["economic_max_day_temp"])
        if "economic_enforcement_enabled" in settings:
            target_config.ECONOMIC_ENFORCEMENT_ENABLED = bool(settings["economic_enforcement_enabled"])
        if "economic_window_start" in settings:
            try:
                parts = str(settings["economic_window_start"]).split(":")
                target_config.ECONOMIC_WINDOW_START = time(int(parts[0]), int(parts[1]))
            except Exception:
                pass
        if "economic_window_end" in settings:
            try:
                parts = str(settings["economic_window_end"]).split(":")
                target_config.ECONOMIC_WINDOW_END = time(int(parts[0]), int(parts[1]))
            except Exception:
                pass
        if "guest_preference_preservation" in settings:
            target_config.GUEST_PREFERENCE_PRESERVATION = bool(settings["guest_preference_preservation"])

        return target_config
