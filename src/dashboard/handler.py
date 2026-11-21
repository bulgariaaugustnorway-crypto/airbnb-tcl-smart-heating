"""HTTP request router and controller for Owner Dashboard via AWS Lambda Function URL."""

import base64
import json
import logging
import os
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.dashboard import auth
from src.dashboard.app import render_login_html, render_dashboard_html
from src.dashboard.config_store import DashboardConfigStore, ConcurrencyConflictError
from src.models import OwnerTestSession, ACMode, ACFanSpeed, mask_uid, ReservationType
from src.notifier import redact_sensitive_str
from src.state_store import StateStore

_LOGGER = logging.getLogger("dashboard_handler")

# Security Response Headers applied to ALL responses
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self';"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}

COOKIE_NAME = "session_token"


def _extract_request_metadata(event: Dict[str, Any]) -> Tuple[str, str, str, Dict[str, str], Optional[str]]:
    """
    Extract HTTP method, normalized path, client IP, cookies, and raw body.
    Supports both AWS Lambda Function URL format 2.0 and API Gateway format 1.0.
    """
    # 1. Method
    method = "GET"
    if "requestContext" in event and "http" in event["requestContext"]:
        method = event["requestContext"]["http"].get("method", "GET").upper()
    elif "httpMethod" in event:
        method = event["httpMethod"].upper()

    # 2. Path
    path = "/"
    if "rawPath" in event:
        path = event["rawPath"]
    elif "path" in event:
        path = event["path"]
    if not path or path == "":
        path = "/"

    # 3. Client IP
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    client_ip = "127.0.0.1"
    if "requestContext" in event and "http" in event["requestContext"]:
        client_ip = event["requestContext"]["http"].get("sourceIp", "127.0.0.1")
    elif "requestContext" in event and "identity" in event["requestContext"]:
        client_ip = event["requestContext"]["identity"].get("sourceIp", "127.0.0.1")

    # 4. Cookies
    cookie_dict = {}
    if "cookies" in event and isinstance(event["cookies"], list):
        for c in event["cookies"]:
            if "=" in c:
                k, v = c.split("=", 1)
                cookie_dict[k.strip()] = v.strip()
    elif "cookie" in headers:
        cookie_header = headers["cookie"]
        for pair in cookie_header.split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookie_dict[k.strip()] = v.strip()

    # 5. Body
    body_str = None
    raw_body = event.get("body")
    if raw_body:
        if event.get("isBase64Encoded"):
            try:
                body_str = base64.b64decode(raw_body).decode("utf-8")
            except Exception as e:
                _LOGGER.warning(f"Failed to decode base64 body: {e}")
                body_str = None
        else:
            body_str = raw_body

    return method, path, client_ip, cookie_dict, body_str


def _parse_body(body_str: Optional[str], content_type: str = "") -> Dict[str, Any]:
    """Parse JSON or form-urlencoded request body."""
    if not body_str:
        return {}

    content_type_lower = content_type.lower()
    if "application/json" in content_type_lower or (body_str.strip().startswith("{") and body_str.strip().endswith("}")):
        try:
            return json.loads(body_str)
        except Exception:
            return {}

    # Form URL-encoded
    try:
        parsed = urllib.parse.parse_qs(body_str)
        return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
    except Exception:
        return {}


def _create_response(
    status_code: int,
    body: str,
    content_type: str = "text/html; charset=utf-8",
    cookie_header: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Build unified HTTP response with mandatory security headers and cookie support."""
    headers = dict(SECURITY_HEADERS)
    headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)

    resp: Dict[str, Any] = {
        "statusCode": status_code,
        "headers": headers,
        "body": body,
        "isBase64Encoded": False
    }

    if cookie_header:
        # Both format 1.0 headers and format 2.0 cookies array
        headers["Set-Cookie"] = cookie_header
        resp["cookies"] = [cookie_header]

    return resp


def _get_secret_key(config: AutomationConfig) -> str:
    """Require an independent, explicitly configured session signing secret."""
    secret = config.SESSION_SECRET_KEY or ""
    if len(secret) < 32:
        raise ValueError("Configure SESSION_SECRET_KEY with at least 32 characters")
    return secret


def _get_stored_password_or_hash(config: AutomationConfig) -> str:
    """Dashboard authentication accepts only the supported password hash format."""
    stored = config.DASHBOARD_PASSWORD_HASH or ""
    return stored if stored.startswith("pbkdf2:sha256:") else ""


def _build_status_data(config: AutomationConfig, config_store: DashboardConfigStore) -> Dict[str, Any]:
    """Assemble real-time and cached telemetry for dashboard display with occupancy-aware overrides and freshness tracking."""
    state_store = StateStore(s3_bucket=config.S3_BUCKET, s3_key=config.S3_STATE_KEY)
    state = state_store.load_state()

    active_owner_test = config_store.load_owner_test_session()

    system_mode = getattr(config, "SYSTEM_MODE", "AUTO").upper()

    # Resolve devices
    lr_id = config.TCL_LIVING_ROOM_ID
    br_id = config.TCL_BEDROOM_ID

    device_states = state.get("device_states", {})
    last_commands = state.get("last_applied_commands", {})

    # Telemetry Timestamp and Freshness Calculation (Europe/Sofia)
    now_sofia = datetime.now(TIMEZONE_SOFIA)
    state_updated_at_raw = state.get("updated_at")

    telemetry_dt_sofia = None
    age_seconds = 0.0
    telemetry_is_stale = False
    telemetry_timestamp_sofia = "No telemetry recorded"
    telemetry_age_str = "Unknown"

    if state_updated_at_raw:
        try:
            dt_parsed = datetime.fromisoformat(state_updated_at_raw)
            if dt_parsed.tzinfo is None:
                from datetime import timezone
                dt_parsed = dt_parsed.replace(tzinfo=timezone.utc)
            telemetry_dt_sofia = dt_parsed.astimezone(TIMEZONE_SOFIA)
            telemetry_timestamp_sofia = telemetry_dt_sofia.strftime("%d %b %Y, %H:%M Sofia")
            age_seconds = max(0.0, (now_sofia - telemetry_dt_sofia).total_seconds())
            telemetry_is_stale = age_seconds > (20 * 60)  # > 20 minutes is stale (scheduler runs every 15 min)

            if age_seconds < 60:
                telemetry_age_str = "just now"
            elif age_seconds < 3600:
                mins = int(age_seconds // 60)
                telemetry_age_str = f"{mins}m ago"
            else:
                hrs = int(age_seconds // 3600)
                mins = int((age_seconds % 3600) // 60)
                telemetry_age_str = f"{hrs}h {mins}m ago"

            if telemetry_is_stale:
                telemetry_age_str += " (STALE)"
        except Exception as e:
            _LOGGER.debug(f"Could not parse state updated_at timestamp '{state_updated_at_raw}': {e}")
            telemetry_is_stale = True
            telemetry_age_str = "Invalid timestamp (STALE)"
    else:
        telemetry_is_stale = True
        telemetry_age_str = "No telemetry (STALE)"

    owner_test_info = None
    if active_owner_test and active_owner_test.is_active():
        owner_test_info = active_owner_test.to_dict()

    # Determine automation safety mode
    if config.TCL_READ_ONLY:
        automation_mode = "READ_ONLY"
        automation_label = "Read-Only Telemetry (Zero Cloud Commands Sent)"
    elif config.DRY_RUN:
        automation_mode = "DRY_RUN"
        automation_label = "Dry-Run Simulation (Cloud Commands Suppressed)"
    else:
        automation_mode = "LIVE"
        automation_label = "Live Autonomous Production (Active Cloud Commands)"

    # Occupancy and calendar status analysis
    known_res_dict = state.get("known_reservations", {})
    today = now_sofia.date()

    dash_cfg = config_store.load_dashboard_config()
    res_profiles = dash_cfg.get("reservation_profiles", {})
    default_profile = dash_cfg.get("default_heating_profile", getattr(config, "DEFAULT_HEATING_PROFILE", "SKI_DAY")).upper()

    current_stay = None
    upcoming_stay = None
    confirmed_count = 0
    blocked_count = 0

    sorted_res = []
    for uid, rdata in known_res_dict.items():
        try:
            start_d = datetime.fromisoformat(rdata["start_date"]).date() if "start_date" in rdata else None
            end_d = datetime.fromisoformat(rdata["end_date"]).date() if "end_date" in rdata else None
            rtype = rdata.get("type", "UNKNOWN")
            if rtype == "CONFIRMED":
                confirmed_count += 1
            elif rtype == "BLOCKED":
                blocked_count += 1
            if start_d and end_d:
                sorted_res.append((start_d, end_d, rtype, rdata))
        except Exception:
            pass

    sorted_res.sort(key=lambda x: x[0])

    for start_d, end_d, rtype, rdata in sorted_res:
        r_uid = rdata.get("uid")
        assigned_prof = res_profiles.get(r_uid, {}).get("profile", default_profile) if r_uid else default_profile
        if start_d <= today < end_d:
            current_stay = {
                "uid": r_uid,
                "type": rtype,
                "title": "Confirmed Guest Stay" if rtype == "CONFIRMED" else "Blocked Dates (Host/Maintenance)",
                "summary": rdata.get("summary", "Reserved"),
                "start_date": rdata.get("start_date"),
                "end_date": rdata.get("end_date"),
                "nights": rdata.get("nights", (end_d - start_d).days),
                "uid_masked": mask_uid(r_uid),
                "assigned_profile": assigned_prof
            }
            break
        elif start_d > today and upcoming_stay is None:
            upcoming_stay = {
                "uid": r_uid,
                "type": rtype,
                "title": "Upcoming Guest" if rtype == "CONFIRMED" else "Upcoming Blocked",
                "summary": rdata.get("summary", "Reserved"),
                "start_date": rdata.get("start_date"),
                "end_date": rdata.get("end_date"),
                "nights": rdata.get("nights", (end_d - start_d).days),
                "uid_masked": mask_uid(r_uid),
                "assigned_profile": assigned_prof
            }

    active_profile = current_stay.get("assigned_profile", default_profile) if current_stay else default_profile

    system_mode = getattr(config, "SYSTEM_MODE", "AUTO").upper()
    system_state = state.get("last_system_state", "STANDBY_OFF")

    if system_mode == "PAUSED":
        system_state = "PAUSED"
        occupancy_status = "SYSTEM PAUSED"
        occupancy_desc = "HVAC automation paused by host; all commands suspended, physical device states preserved"
    elif current_stay and current_stay["type"] == "CONFIRMED":
        occupancy_status = "GUEST OCCUPIED"
        occupancy_desc = f"Active guest booking: {current_stay['start_date']} to {current_stay['end_date']} ({current_stay['nights']} nights)"
        if not system_state or system_state == "STANDBY_OFF":
            system_state = "OCCUPIED_HEATING"
    elif active_owner_test and active_owner_test.is_active():
        expiry_str = active_owner_test.expires_at.strftime('%H:%M Sofia') if active_owner_test.expires_at else 'expiry'
        rem_str = active_owner_test.to_dict().get("remaining_str", "")
        if getattr(active_owner_test, "session_type", "OWNER_TEST") == "OWNER_PRESENT":
            occupancy_status = "OWNER PRESENT"
            occupancy_desc = f"Owner personal visit active ({rem_str} remaining, expires {expiry_str}) — following diurnal schedule"
            system_state = "OWNER_PRESENT"
        else:
            occupancy_status = "OWNER TEST MODE"
            occupancy_desc = f"Temporary test hold ({active_owner_test.target_temperature}°C) active until {expiry_str}"
            system_state = "OWNER_TEST_HOLD"
    elif system_mode == "MANUAL":
        occupancy_status = "MANUAL HOLD"
        occupancy_desc = "Continuous comfort heating active independent of calendar"
    elif current_stay and current_stay["type"] == "BLOCKED":
        occupancy_status = "BLOCKED DATES"
        occupancy_desc = f"Host/family blocked stay: {current_stay['start_date']} to {current_stay['end_date']} ({current_stay['nights']} nights)"
    else:
        occupancy_status = "UNOCCUPIED"
        if upcoming_stay:
            occupancy_desc = f"Unoccupied: next arrival on {upcoming_stay['start_date']} ({upcoming_stay['title']})"
        else:
            occupancy_desc = "Property unoccupied; standing by for upcoming Airbnb bookings"

    # Determine contextual override label based on current occupancy
    if current_stay and current_stay["type"] == "CONFIRMED":
        contextual_override_label = "GUEST OVERRIDE"
    elif active_owner_test and active_owner_test.is_active():
        contextual_override_label = "OWNER OVERRIDE"
    else:
        contextual_override_label = "MANUAL DEVICE ADJUSTMENT"

    # Helper to format device box data
    def _format_device(dev_id: str, default_name: str) -> Dict[str, Any]:
        st = device_states.get(dev_id, {})
        cmd = last_commands.get(dev_id, {})
        power = st.get("power", cmd.get("power", False))
        target_temp = st.get("target_temperature", cmd.get("target_temperature", 22.0))
        ambient = st.get("current_temperature")
        mode = st.get("mode", cmd.get("mode", "Heat"))
        fan = st.get("fan_speed", cmd.get("fan_speed", "Auto"))
        led = st.get("display_led", cmd.get("display_led", False))
        beep = st.get("beep_feedback", cmd.get("beep_feedback", False))
        raw_online = st.get("online", True)
        # Device is not claimed online if telemetry is stale
        online = raw_online and (not telemetry_is_stale)

        # Detect physical remote adjustments safely
        # Critical rule: NEVER flag setpoint differences when unit is powered OFF!
        override_active = False
        override_type = None
        override_details = None

        curr_pwr = st.get("power")
        cmd_pwr = cmd.get("power")
        curr_target = st.get("target_temperature")
        cmd_target = cmd.get("target_temperature")

        if curr_pwr is True:
            if cmd_pwr is False:
                # AC is running, but automation intended idle/off
                override_active = True
                override_type = contextual_override_label
                override_details = f"Unit turned ON manually ({curr_target}°C) while system set to idle"
            elif curr_target is not None and cmd_target is not None and abs(curr_target - cmd_target) >= 0.5:
                # AC is running, but setpoint was adjusted on the physical remote
                override_active = True
                override_type = contextual_override_label
                override_details = f"Manual setpoint: {curr_target}°C (Auto target: {cmd_target}°C)"
        elif curr_pwr is False:
            if cmd_pwr is True:
                # Automation commanded heating, but unit was manually switched OFF
                override_active = True
                override_type = contextual_override_label
                override_details = "Unit turned OFF manually via remote"
            else:
                # Both are OFF (e.g. unoccupied idle): hardware memory holding 22°C is NOT an override!
                override_active = False
                override_type = None
                override_details = None

        guest_overrides = state.get("guest_manual_overrides", {})
        dev_override = guest_overrides.get(dev_id, {})
        is_guest_power_off = bool(dev_override.get("power_off"))

        # Individual temperature measurement freshness
        temp_meas_iso = st.get("temp_measured_at")
        temp_meas_str = "Unavailable"
        temp_age_sec = None
        temp_age_str = "Unknown"
        temp_freshness = "UNAVAILABLE"

        if temp_meas_iso and ambient is not None and raw_online:
            try:
                meas_dt = datetime.fromisoformat(temp_meas_iso)
                if meas_dt.tzinfo is None:
                    from datetime import timezone
                    meas_dt = meas_dt.replace(tzinfo=timezone.utc)
                temp_dt_sofia = meas_dt.astimezone(TIMEZONE_SOFIA)
                temp_meas_str = temp_dt_sofia.strftime("%H:%M Sofia")
                temp_age_sec = max(0.0, (now_sofia - temp_dt_sofia).total_seconds())
                if temp_age_sec < 60:
                    temp_age_str = "just now"
                elif temp_age_sec < 3600:
                    temp_age_str = f"{int(temp_age_sec // 60)}m ago"
                else:
                    temp_age_str = f"{int(temp_age_sec // 3600)}h {int((temp_age_sec % 3600) // 60)}m ago"

                fresh_threshold_sec = getattr(config, "TEMP_FRESHNESS_MAX_AGE_MINUTES", 60.0) * 60.0
                if temp_age_sec <= fresh_threshold_sec:
                    temp_freshness = "FRESH"
                else:
                    temp_freshness = "STALE"
            except Exception:
                temp_freshness = "UNAVAILABLE"
                temp_meas_str = "Invalid timestamp"
        elif ambient is not None and raw_online:
            temp_freshness = "UNAVAILABLE"
            temp_meas_str = "Timestamp unavailable"
            temp_age_str = "Unknown"
        else:
            temp_freshness = "UNAVAILABLE"
            temp_meas_str = "Unavailable"
            temp_age_str = "No reading"

        # Collect active monitoring warnings for this room
        active_warnings = []
        if not raw_online:
            active_warnings.append("Device is OFFLINE on TCL Cloud")
        elif temp_freshness == "STALE":
            active_warnings.append(f"Temperature reading is STALE ({temp_age_str}) — unit in unventilated standby")
        elif temp_freshness == "UNAVAILABLE":
            active_warnings.append("Temperature reading UNAVAILABLE")

        frost_alert = state.get("last_frost_alerts", {}).get(dev_id)
        if frost_alert:
            active_warnings.append(f"Low Temperature Alert: {frost_alert.get('tier', 'WARNING')} ({frost_alert.get('temperature')}°C)")

        if is_guest_power_off:
            active_warnings.append("Guest Power-OFF Override active")

        return {
            "device_id": dev_id,
            "name": default_name,
            "power": power,
            "physical_power": curr_pwr,
            "physical_power_label": "ON" if curr_pwr is True else ("OFF" if curr_pwr is False else "UNKNOWN"),
            "target_temperature": target_temp,
            "current_temperature": ambient,
            "last_measured_temperature": ambient,
            "temp_measured_at": temp_meas_iso,
            "temp_measured_at_sofia": temp_meas_str,
            "temp_age_seconds": round(temp_age_sec) if temp_age_sec is not None else None,
            "temp_age_str": temp_age_str,
            "temp_freshness": temp_freshness,
            "is_fresh": (temp_freshness == "FRESH"),
            "is_stale": (temp_freshness == "STALE"),
            "is_unavailable": (temp_freshness == "UNAVAILABLE"),
            "mode": mode,
            "fan_speed": fan,
            "display_led": led,
            "beep_feedback": beep,
            "online": online,
            "raw_online": raw_online,
            "telemetry_is_stale": telemetry_is_stale,
            "override_active": override_active,
            "override_type": override_type,
            "override_details": override_details,
            "guest_override_active": override_active,
            "guest_power_off_override": is_guest_power_off,
            "active_warnings": active_warnings
        }

    # Notification Delivery Status Evaluation
    notif_state = state.get("notification_delivery_state", {})
    has_telegram = bool(getattr(config, "TELEGRAM_BOT_TOKEN", None) and getattr(config, "TELEGRAM_CHAT_ID", None))
    has_webhook = bool(getattr(config, "NOTIFICATION_WEBHOOK_URL", None))

    if not has_telegram and not has_webhook:
        notif_status = "NOT CONFIGURED"
        notif_channel = "NONE"
    else:
        notif_channel = "TELEGRAM" if has_telegram else "WEBHOOK"
        recorded_status = notif_state.get("status")
        if recorded_status == "LAST DELIVERY SUCCESSFUL":
            notif_status = "LAST DELIVERY SUCCESSFUL"
        elif recorded_status == "DELIVERY FAILED":
            notif_status = "DELIVERY FAILED"
        else:
            notif_status = "CONFIGURED — UNTESTED"

    notif_last_delivery = notif_state.get("last_delivery_timestamp")
    notif_last_delivery_sofia = notif_state.get("last_delivery_timestamp_sofia")
    notif_last_error = notif_state.get("last_delivery_error")
    if notif_last_error:
        notif_last_error = redact_sensitive_str(notif_last_error, getattr(config, "TELEGRAM_BOT_TOKEN", None))

    notifications_info = {
        "status": notif_status,
        "channel": notif_channel,
        "configured": (notif_status != "NOT CONFIGURED"),
        "last_delivery_timestamp": notif_last_delivery,
        "last_delivery_timestamp_sofia": notif_last_delivery_sofia,
        "last_delivery_error": notif_last_error,
        "last_attempt_timestamp": notif_state.get("last_attempt_timestamp"),
    }

    return {
        "system_state": system_state,
        "notifications": notifications_info,
        "notification_status": notif_status,
        "notification_channel": notif_channel,
        "active_profile": active_profile,
        "default_profile": default_profile,
        "decision_reason": state.get("decision_reason", "Automated schedule active"),
        "automation_mode": automation_mode,
        "automation_label": automation_label,
        "telemetry_source": "S3 Cached State (15-min cycle)" if not telemetry_is_stale else "S3 Cached State (STALE >20m)",
        "telemetry_is_live_direct": False,
        "telemetry_timestamp": telemetry_timestamp_sofia,
        "telemetry_age_seconds": round(age_seconds),
        "telemetry_age_str": telemetry_age_str,
        "telemetry_is_stale": telemetry_is_stale,
        "occupancy": {
            "status": occupancy_status,
            "description": occupancy_desc,
            "current_stay": current_stay,
            "upcoming_stay": upcoming_stay,
            "counts": {
                "confirmed": confirmed_count,
                "blocked": blocked_count,
                "total": len(known_res_dict)
            },
            "calendar_stale": bool(state.get("was_calendar_stale", False))
        },
        "devices": {
            "living_room": _format_device(lr_id, "Living Room"),
            "bedroom": _format_device(br_id, "Bedroom")
        },
        "verified_hardware": {
            "living_room_id": lr_id,
            "bedroom_id": br_id
        },
        "owner_test": owner_test_info,
        "adaptive_comfort": {
            "enabled": getattr(config, "ADAPTIVE_COMFORT_ENABLED", True),
            "living_room_preferred_day_temp": (
                state.get("preferred_day_temperatures", {}).get(lr_id, {}).get("temperature")
                or state.get("guest_manual_overrides", {}).get(lr_id, {}).get("preferred_day_temperature")
            ),
            "bedroom_preferred_day_temp": (
                state.get("preferred_day_temperatures", {}).get(br_id, {}).get("temperature")
                or state.get("guest_manual_overrides", {}).get(br_id, {}).get("preferred_day_temperature")
            ),
            "living_room_guest_power_off": bool(
                state.get("guest_manual_overrides", {}).get(lr_id, {}).get("power_off", False)
            ),
            "bedroom_guest_power_off": bool(
                state.get("guest_manual_overrides", {}).get(br_id, {}).get("power_off", False)
            ),
            "last_transitions": state.get("last_completed_transitions", {}),
            "recent_economic_interventions": state.get("recent_economic_interventions", []),
        },
        "dry_run": config.DRY_RUN,
        "read_only": config.TCL_READ_ONLY,
        "heat_during_blocked_dates": config.HEAT_DURING_BLOCKED_DATES,
        "last_updated": state.get("updated_at", now_sofia.isoformat()),
        "monitoring": {
            "enabled": getattr(config, "MONITORING_ENABLED", True),
            "tcl_cloud_metered_kwh_supported": False,
            "metered_kwh_note": "TCL Cloud hardware lacks power/energy telemetry. Device kWh is UNAVAILABLE.",
            "living_room_approx_on_hours_today": 0.0,
            "bedroom_approx_on_hours_today": 0.0,
            "recent_events": [],
            "meter_readings": [],
            "weather_today": None,
        }
    }

    # Hydrate monitoring metrics if available
    try:
        from src.monitoring.history_store import ObservationHistoryStore
        from src.monitoring.weather import WeatherService
        h_store = ObservationHistoryStore(s3_bucket=config.S3_BUCKET, local_dir=config.DATA_DIR / "history")
        today_date = now_sofia.date()
        lr_sum = h_store.compute_daily_summary(lr_id, today_date, "living_room")
        br_sum = h_store.compute_daily_summary(br_id, today_date, "bedroom")
        status_data["monitoring"]["living_room_approx_on_hours_today"] = lr_sum.approx_on_hours
        status_data["monitoring"]["bedroom_approx_on_hours_today"] = br_sum.approx_on_hours
        events = h_store.get_events(limit=10)
        status_data["monitoring"]["recent_events"] = [e.to_dict() for e in events]
        readings = h_store.get_meter_readings()
        status_data["monitoring"]["meter_readings"] = [r.to_dict() for r in readings[-7:]]
        w_service = WeatherService(s3_bucket=config.S3_BUCKET, local_dir=config.DATA_DIR / "history" / "weather")
        status_data["monitoring"]["weather_today"] = w_service.get_weather_for_date(today_date)
    except Exception as e:
        _LOGGER.debug(f"Could not assemble monitoring summary for status data: {e}")

    return status_data


def handle_dashboard_request(event: Dict[str, Any], context: Any, config: Optional[AutomationConfig] = None) -> Dict[str, Any]:
    """
    Main router for Owner Dashboard HTTP requests.
    Validates sessions, handles login/logout, serves HTML views, and dispatches JSON APIs.
    """
    cfg = config or AutomationConfig()
    config_store = DashboardConfigStore(config=cfg)

    method, path, client_ip, cookies, raw_body = _extract_request_metadata(event)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    content_type = headers.get("content-type", "")
    try:
        secret_key = _get_secret_key(cfg)
    except ValueError:
        return _create_response(503, "Dashboard authentication is not configured.")

    session_token = cookies.get(COOKIE_NAME)
    is_authenticated = auth.verify_session_token(session_token, secret_key) if session_token else False

    _LOGGER.info(f"Dashboard HTTP {method} {path} from IP={client_ip} (Auth={is_authenticated})")

    # -------------------------------------------------------------
    # 1. GET / or /login
    # -------------------------------------------------------------
    if path in ("/", "/login") and method == "GET":
        if is_authenticated and session_token:
            csrf_token = auth.generate_csrf_token(session_token, secret_key)
            status_data = _build_status_data(cfg, config_store)
            config_data = config_store.load_dashboard_config()
            html_content = render_dashboard_html(csrf_token, status_data, config_data)
            return _create_response(200, html_content)
        else:
            try:
                limited, remaining = auth.is_rate_limited(client_ip)
            except auth.RateLimiterUnavailableError as e:
                _LOGGER.critical(f"Rate limiter service unavailable on GET: {e}")
                return _create_response(503, render_login_html(error_message="Security service temporarily unavailable. Access restricted."))
            html_content = render_login_html(error_message=None, rate_limit_remaining=remaining)
            status_code = 429 if limited else 200
            return _create_response(status_code, html_content)

    # -------------------------------------------------------------
    # 2. POST /api/login or /login
    # -------------------------------------------------------------
    if path in ("/api/login", "/login") and method == "POST":
        try:
            limited, remaining = auth.is_rate_limited(client_ip)
        except auth.RateLimiterUnavailableError as e:
            _LOGGER.critical(f"Rate limiter service unavailable on POST login: {e}")
            if "application/json" in content_type:
                return _create_response(503, json.dumps({
                    "status": "error",
                    "error": "Authentication service temporarily unavailable. Security backends offline."
                }), content_type="application/json")
            return _create_response(503, render_login_html(error_message="Authentication service temporarily unavailable. Security systems offline."))

        if limited:
            if "application/json" in content_type:
                return _create_response(429, json.dumps({
                    "status": "error",
                    "error": f"Too many failed attempts. Lockout active for {remaining} seconds."
                }), content_type="application/json")
            return _create_response(429, render_login_html(error_message=None, rate_limit_remaining=remaining))

        body_data = _parse_body(raw_body, content_type)
        password = str(body_data.get("password", "")).strip()

        stored_pass = _get_stored_password_or_hash(cfg)
        if not stored_pass:
            _LOGGER.error("No dashboard password configured in environment or SSM.")
            return _create_response(500, json.dumps({
                "status": "error",
                "error": "Dashboard password not configured on server."
            }), content_type="application/json")

        if auth.verify_password(password, stored_pass):
            auth.clear_failed_attempts(client_ip)
            new_token = auth.create_session_token(secret_key)
            cookie_header = f"{COOKIE_NAME}={new_token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={auth.SESSION_DURATION_SECONDS}"

            if "application/json" in content_type:
                return _create_response(200, json.dumps({"status": "success"}), content_type="application/json", cookie_header=cookie_header)

            return _create_response(
                302,
                "",
                cookie_header=cookie_header,
                extra_headers={"Location": "/"}
            )
        else:
            try:
                lockout_secs = auth.record_failed_attempt(client_ip)
            except auth.RateLimiterUnavailableError as e:
                _LOGGER.critical(f"Failed to record attempt in rate limiter: {e}")
                if "application/json" in content_type:
                    return _create_response(503, json.dumps({
                        "status": "error",
                        "error": "Authentication service temporarily unavailable. Security backends offline."
                    }), content_type="application/json")
                return _create_response(503, render_login_html(error_message="Authentication service temporarily unavailable."))

            _LOGGER.warning(f"Failed login attempt from {client_ip}. Lockout secs: {lockout_secs}")
            if "application/json" in content_type:
                return _create_response(401, json.dumps({
                    "status": "error",
                    "error": "Invalid password."
                }), content_type="application/json")

            return _create_response(
                401,
                render_login_html(
                    error_message="Invalid password. Please verify credentials.",
                    rate_limit_remaining=lockout_secs
                )
            )

    # -------------------------------------------------------------
    # 3. POST /api/logout or /logout
    # -------------------------------------------------------------
    if path in ("/api/logout", "/logout") and method == "POST":
        cookie_header = f"{COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"
        if "application/json" in content_type:
            return _create_response(200, json.dumps({"status": "logged_out"}), content_type="application/json", cookie_header=cookie_header)
        return _create_response(302, "", cookie_header=cookie_header, extra_headers={"Location": "/"})

    # -------------------------------------------------------------
    # Require authentication for all subsequent /api/* endpoints
    # -------------------------------------------------------------
    if not is_authenticated or not session_token:
        if path.startswith("/api/"):
            return _create_response(401, json.dumps({"status": "error", "error": "Unauthorized"}), content_type="application/json")
        return _create_response(302, "", extra_headers={"Location": "/login"})

    # -------------------------------------------------------------
    # 4. GET /api/status
    # -------------------------------------------------------------
    if path == "/api/status" and method == "GET":
        status_data = _build_status_data(cfg, config_store)
        return _create_response(200, json.dumps(status_data), content_type="application/json")

    # -------------------------------------------------------------
    # 5. GET /api/config
    # -------------------------------------------------------------
    if path == "/api/config" and method == "GET":
        config_data = config_store.load_dashboard_config()
        return _create_response(200, json.dumps(config_data), content_type="application/json")

    # -------------------------------------------------------------
    # 6. POST /api/config
    # -------------------------------------------------------------
    if path == "/api/config" and method == "POST":
        # Validate CSRF Token
        csrf_header = headers.get("x-csrf-token")
        body_data = _parse_body(raw_body, content_type)
        csrf_candidate = csrf_header or body_data.get("csrf_token")

        if not auth.verify_csrf_token(csrf_candidate, session_token, secret_key):
            _LOGGER.warning(f"CSRF token verification failed on /api/config from {client_ip}")
            return _create_response(403, json.dumps({"status": "error", "error": "Invalid or missing CSRF token"}), content_type="application/json")

        # Sanitize and validate inputs
        sanitized = {}
        for key in (
            "schedule_morning_temp", "schedule_day_temp", "schedule_evening_temp", "schedule_night_temp", "pre_arrival_temp",
            "living_room_morning_temp", "living_room_day_temp", "living_room_evening_temp", "living_room_night_temp",
            "bedroom_morning_temp", "bedroom_day_temp", "bedroom_evening_temp", "bedroom_night_temp"
        ):
            if key in body_data and body_data[key] is not None:
                try:
                    val = float(body_data[key])
                    if cfg.TEMP_MIN_SAFE <= val <= cfg.TEMP_MAX_SAFE:
                        sanitized[key] = val
                    else:
                        return _create_response(400, json.dumps({"status": "error", "error": f"{key} out of range (16-30°C)"}), content_type="application/json")
                except ValueError:
                    return _create_response(400, json.dumps({"status": "error", "error": f"Invalid number for {key}"}), content_type="application/json")

        for key in ("schedule_morning_fan", "schedule_day_fan", "schedule_evening_fan", "schedule_night_fan"):
            if key in body_data:
                fan_val = str(body_data[key])
                if fan_val in ("Auto", "Quiet", "Low", "Mid", "High"):
                    sanitized[key] = fan_val

        for key in (
            "default_led_display", "night_led_display", "beep_feedback_enabled",
            "heat_during_blocked_dates", "manual_override_preserve",
            "living_room_enabled", "bedroom_enabled",
            "adaptive_comfort_enabled", "economic_enforcement_enabled", "guest_preference_preservation"
        ):
            if key in body_data:
                val = body_data[key]
                sanitized[key] = val is True or str(val).lower() in ("true", "1", "yes")

        for key, min_v, max_v in (
            ("night_setback_delta", 0.5, 5.0),
            ("night_setback_floor", 16.0, 24.0),
            ("economic_max_day_temp", 18.0, 28.0),
        ):
            if key in body_data and body_data[key] is not None:
                try:
                    val = float(body_data[key])
                    if min_v <= val <= max_v:
                        sanitized[key] = val
                except ValueError:
                    pass

        for key in ("living_room_led_mode", "bedroom_led_mode"):
            if key in body_data:
                mode_val = str(body_data[key]).upper()
                if mode_val in ("SCHEDULE", "ALWAYS_OFF", "ALWAYS_ON"):
                    sanitized[key] = mode_val

        if "system_mode" in body_data:
            sm = str(body_data["system_mode"]).upper()
            if sm in ("AUTO", "MANUAL", "PAUSED", "OFF"):
                sanitized["system_mode"] = sm

        if "bedroom_temp_offset" in body_data:
            try:
                offset = float(body_data["bedroom_temp_offset"])
                if -5.0 <= offset <= 5.0:
                    sanitized["bedroom_temp_offset"] = offset
            except ValueError:
                pass

        if "living_room_temp_offset" in body_data:
            try:
                offset = float(body_data["living_room_temp_offset"])
                if -5.0 <= offset <= 5.0:
                    sanitized["living_room_temp_offset"] = offset
            except ValueError:
                pass

        if "default_heating_profile" in body_data:
            prof = str(body_data["default_heating_profile"]).upper()
            if prof in ("AT_HOME", "SKI_DAY", "CUSTOM"):
                sanitized["default_heating_profile"] = prof

        if "pre_arrival_hours" in body_data:
            try:
                hrs = int(body_data["pre_arrival_hours"])
                if 1 <= hrs <= 6:
                    sanitized["pre_arrival_hours"] = hrs
            except ValueError:
                pass

        for time_key in ("checkin_time", "checkout_time"):
            if time_key in body_data:
                tstr = str(body_data[time_key]).strip()
                try:
                    h, m = tstr.split(":")
                    if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                        sanitized[time_key] = f"{int(h):02d}:{int(m):02d}"
                except Exception:
                    pass

        # Reject unauthorized attempts to modify protected safety locks
        for forbidden in ("tcl_dry_run", "dry_run", "tcl_read_only", "read_only"):
            if forbidden in body_data:
                _LOGGER.warning(f"Rejected attempt to modify protected safety flag '{forbidden}' from {client_ip}")

        # Extract optimistic concurrency metadata
        base_updated_at = body_data.get("base_updated_at")
        base_version = body_data.get("base_version")
        base_etag = body_data.get("base_etag")

        try:
            success = config_store.save_dashboard_config(
                sanitized,
                expected_updated_at=base_updated_at,
                expected_version=base_version,
                expected_etag=base_etag
            )
        except ConcurrencyConflictError as ce:
            _LOGGER.warning(f"Optimistic concurrency conflict on /api/config from {client_ip}: {ce}")
            return _create_response(409, json.dumps({
                "status": "error",
                "error": "Configuration conflict: Settings were modified by another session. Please reload the dashboard to review latest values."
            }), content_type="application/json")

        if success:
            updated_cfg = config_store.load_dashboard_config()
            return _create_response(200, json.dumps({"status": "success", "config": updated_cfg}), content_type="application/json")
        else:
            return _create_response(500, json.dumps({"status": "error", "error": "Failed to persist configuration"}), content_type="application/json")

    # -------------------------------------------------------------
    # 7. POST /api/owner-test
    # -------------------------------------------------------------
    if path == "/api/owner-test" and method == "POST":
        csrf_header = headers.get("x-csrf-token")
        body_data = _parse_body(raw_body, content_type)
        csrf_candidate = csrf_header or body_data.get("csrf_token")

        if not auth.verify_csrf_token(csrf_candidate, session_token, secret_key):
            _LOGGER.warning(f"CSRF token verification failed on /api/owner-test from {client_ip}")
            return _create_response(403, json.dumps({"status": "error", "error": "Invalid or missing CSRF token"}), content_type="application/json")

        action = str(body_data.get("action", "START")).upper()

        if action in ("STOP", "CANCEL", "CLEAR"):
            config_store.clear_owner_test_session()
            return _create_response(200, json.dumps({"status": "success", "message": "Owner session cleared"}), content_type="application/json")

        if action in ("EXTEND", "ADD_TIME"):
            active_session = config_store.load_owner_test_session()
            if not active_session or not active_session.is_active():
                return _create_response(400, json.dumps({
                    "status": "error",
                    "error": "No active owner session to extend. Please activate a new session."
                }), content_type="application/json")

            add_hours = float(body_data.get("additional_hours", body_data.get("duration_hours", 2.0)))
            if add_hours < 0.25:
                add_hours = 0.25
            elif add_hours > 72.0:
                add_hours = 72.0

            now = datetime.now(TIMEZONE_SOFIA)
            current_expires = active_session.expires_at
            if current_expires:
                if current_expires.tzinfo is None:
                    current_expires = current_expires.replace(tzinfo=TIMEZONE_SOFIA)
                base_time = max(now, current_expires)
            else:
                base_time = now

            new_expires = base_time + timedelta(hours=add_hours)
            # Enforce max 72h from current moment
            if new_expires > now + timedelta(hours=72.0):
                new_expires = now + timedelta(hours=72.0)

            active_session.expires_at = new_expires
            if active_session.started_at:
                active_session.duration_hours = max(0.25, round((new_expires - active_session.started_at).total_seconds() / 3600.0, 2))
            active_session.notes = f"{active_session.notes} | Extended +{add_hours:.1f}h at {now.strftime('%H:%M')}".strip(" |")

            config_store.save_owner_test_session(active_session)
            return _create_response(200, json.dumps({
                "status": "success",
                "message": f"Owner presence extended until {new_expires.strftime('%H:%M (%d %b) Sofia')}",
                "owner_test": active_session.to_dict()
            }), content_type="application/json")

        if action in ("START", "OWNER_PRESENT"):
            now = datetime.now(TIMEZONE_SOFIA)
            dur_hours = float(body_data.get("duration_hours", 2.0))
            if dur_hours < 0.25:
                dur_hours = 0.25
            elif dur_hours > 72.0:
                dur_hours = 72.0

            target_temp = float(body_data.get("target_temperature", 22.0))
            if target_temp < cfg.TEMP_MIN_SAFE:
                target_temp = cfg.TEMP_MIN_SAFE
            elif target_temp > cfg.TEMP_MAX_SAFE:
                target_temp = cfg.TEMP_MAX_SAFE

            mode_str = str(body_data.get("mode", "Heat"))
            fan_str = str(body_data.get("fan_speed", "Auto"))
            session_type = str(body_data.get("session_type", "OWNER_PRESENT" if action == "OWNER_PRESENT" else "OWNER_TEST")).upper()
            if session_type not in ("OWNER_TEST", "OWNER_PRESENT"):
                session_type = "OWNER_TEST"

            default_note = "Owner Personal Stay (Diurnal Schedule)" if session_type == "OWNER_PRESENT" else "Initiated via Owner Dashboard"

            session = OwnerTestSession(
                active=True,
                started_at=now,
                expires_at=now + timedelta(hours=dur_hours),
                duration_hours=dur_hours,
                target_temperature=target_temp,
                mode=ACMode(mode_str) if mode_str in ACMode.__members__ else ACMode.HEAT,
                fan_speed=ACFanSpeed(fan_str) if fan_str in ACFanSpeed.__members__ else ACFanSpeed.AUTO,
                display_led=bool(body_data.get("display_led", False)),
                living_room_power=bool(body_data.get("living_room_power", True)),
                bedroom_power=bool(body_data.get("bedroom_power", True)),
                created_by="owner_dashboard",
                notes=str(body_data.get("notes", default_note)),
                session_type=session_type
            )

            config_store.save_owner_test_session(session)
            return _create_response(200, json.dumps({
                "status": "success",
                "owner_test": session.to_dict()
            }), content_type="application/json")

    # -------------------------------------------------------------
    # 8. POST /api/reservation-profile
    # -------------------------------------------------------------
    if path == "/api/reservation-profile" and method == "POST":
        csrf_header = headers.get("x-csrf-token")
        body_data = _parse_body(raw_body, content_type)
        csrf_candidate = csrf_header or body_data.get("csrf_token")

        if not auth.verify_csrf_token(csrf_candidate, session_token, secret_key):
            _LOGGER.warning(f"CSRF token verification failed on /api/reservation-profile from {client_ip}")
            return _create_response(403, json.dumps({"status": "error", "error": "Invalid or missing CSRF token"}), content_type="application/json")

        uid = str(body_data.get("uid", "")).strip()
        if not uid:
            return _create_response(400, json.dumps({"status": "error", "error": "Missing reservation UID"}), content_type="application/json")

        action = str(body_data.get("action", "")).upper()
        if action == "CLEAR":
            config_store.clear_reservation_profile(uid)
            return _create_response(200, json.dumps({"status": "success", "message": f"Cleared profile for {uid}"}), content_type="application/json")

        profile_type = str(body_data.get("profile", "SKI_DAY")).upper()
        if profile_type not in ("AT_HOME", "SKI_DAY", "CUSTOM"):
            return _create_response(400, json.dumps({"status": "error", "error": f"Invalid profile: {profile_type}"}), content_type="application/json")

        custom_temperatures = body_data.get("custom_temperatures")
        if custom_temperatures and not isinstance(custom_temperatures, dict):
            custom_temperatures = None

        lr_offset = None
        if "living_room_offset" in body_data and body_data["living_room_offset"] is not None:
            try:
                lr_offset = float(body_data["living_room_offset"])
            except (ValueError, TypeError):
                pass

        br_offset = None
        if "bedroom_offset" in body_data and body_data["bedroom_offset"] is not None:
            try:
                br_offset = float(body_data["bedroom_offset"])
            except (ValueError, TypeError):
                pass

        end_date = body_data.get("end_date")

        saved = config_store.set_reservation_profile(
            uid=uid,
            profile_type=profile_type,
            custom_temperatures=custom_temperatures,
            living_room_offset=lr_offset,
            bedroom_offset=br_offset,
            end_date=end_date
        )

        if saved:
            return _create_response(200, json.dumps({
                "status": "success",
                "uid": uid,
                "profile": profile_type,
                "living_room_offset": lr_offset,
                "bedroom_offset": br_offset
            }), content_type="application/json")
        else:
            return _create_response(500, json.dumps({"status": "error", "error": "Failed to persist reservation profile"}), content_type="application/json")

    # -------------------------------------------------------------
    # 9. GET /api/monitoring/summary
    # -------------------------------------------------------------
    if path == "/api/monitoring/summary" and method == "GET":
        status_data = _build_status_data(cfg, config_store)
        return _create_response(200, json.dumps(status_data.get("monitoring", {})), content_type="application/json")

    # -------------------------------------------------------------
    # 10. POST /api/monitoring/meter-reading
    # -------------------------------------------------------------
    if path == "/api/monitoring/meter-reading" and method == "POST":
        csrf_header = headers.get("x-csrf-token")
        body_data = _parse_body(raw_body, content_type)
        csrf_candidate = csrf_header or body_data.get("csrf_token")

        if not auth.verify_csrf_token(csrf_candidate, session_token, secret_key):
            _LOGGER.warning(f"CSRF token verification failed on /api/monitoring/meter-reading from {client_ip}")
            return _create_response(403, json.dumps({"status": "error", "error": "Invalid or missing CSRF token"}), content_type="application/json")

        reading_date = str(body_data.get("reading_date", "")).strip()
        meter_kwh_raw = body_data.get("meter_kwh")
        notes = str(body_data.get("notes", "")).strip()

        if not reading_date or meter_kwh_raw is None:
            return _create_response(400, json.dumps({"status": "error", "error": "Missing reading_date or meter_kwh"}), content_type="application/json")

        try:
            meter_kwh = float(meter_kwh_raw)
            if meter_kwh < 0.0:
                raise ValueError("Meter reading cannot be negative")
        except (ValueError, TypeError) as e:
            return _create_response(400, json.dumps({"status": "error", "error": f"Invalid meter_kwh value: {e}"}), content_type="application/json")

        from src.monitoring.history_store import ObservationHistoryStore
        h_store = ObservationHistoryStore(s3_bucket=cfg.S3_BUCKET, local_dir=cfg.DATA_DIR / "history")
        reading = h_store.record_meter_reading(
            reading_date=reading_date,
            meter_kwh=meter_kwh,
            recorded_by="owner",
            notes=notes
        )

        return _create_response(200, json.dumps({"status": "success", "reading": reading.to_dict()}), content_type="application/json")

    # -------------------------------------------------------------
    # 11. GET /api/monitoring/export.csv
    # -------------------------------------------------------------
    if path == "/api/monitoring/export.csv" and method == "GET":
        now_sofia = datetime.now(TIMEZONE_SOFIA).date()
        days_back = 7
        try:
            query_str = event.get("rawQueryString", "")
            if "days=" in query_str:
                parsed_q = urllib.parse.parse_qs(query_str)
                days_back = int(parsed_q.get("days", [7])[0])
        except Exception:
            days_back = 7

        start_d = now_sofia - timedelta(days=days_back)
        end_d = now_sofia

        from src.monitoring.history_store import ObservationHistoryStore
        h_store = ObservationHistoryStore(s3_bucket=cfg.S3_BUCKET, local_dir=cfg.DATA_DIR / "history")
        csv_text = h_store.export_observations_to_csv(start_d, end_d)

        filename = f"hvac_monitoring_{start_d.strftime('%Y%m%d')}_{end_d.strftime('%Y%m%d')}.csv"
        extra = {
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
        return _create_response(200, csv_text, content_type="text/csv; charset=utf-8", extra_headers=extra)

    # -------------------------------------------------------------
    # 12. GET /api/monitoring/report
    # -------------------------------------------------------------
    if path == "/api/monitoring/report" and method == "GET":
        now_sofia = datetime.now(TIMEZONE_SOFIA).date()
        start_d = now_sofia - timedelta(days=7)
        end_d = now_sofia

        from src.monitoring.report_generator import SevenDayReportGenerator
        rep_gen = SevenDayReportGenerator(config=cfg)
        rep_data = rep_gen.generate_report(start_d, end_d)

        query_str = event.get("rawQueryString", "")
        if "format=markdown" in query_str or "format=md" in query_str:
            md_text = rep_gen.render_markdown_report(rep_data)
            return _create_response(200, md_text, content_type="text/markdown; charset=utf-8")

        return _create_response(200, json.dumps(rep_data), content_type="application/json")

    return _create_response(404, "Not Found", content_type="text/plain")
