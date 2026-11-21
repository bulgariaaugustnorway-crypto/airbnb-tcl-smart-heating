"""Telemetry collector for periodic HVAC behavior observation and event detection."""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.models import SystemState, Reservation, mask_uid
from src.monitoring.models import DeviceObservation, OperationEvent
from src.monitoring.history_store import ObservationHistoryStore

_LOGGER = logging.getLogger(__name__)


class TelemetryCollector:
    """
    Collects 15-minute read-only HVAC observations from device states and shadow metadata,
    detects discrete operation events (ON/OFF, setpoint changes), and records them idempotently.
    """

    def __init__(
        self,
        history_store: Optional[ObservationHistoryStore] = None,
        config: Optional[AutomationConfig] = None,
    ):
        self.config = config or AutomationConfig()
        self.history_store = history_store or ObservationHistoryStore(
            s3_bucket=self.config.S3_BUCKET,
            local_dir=self.config.DATA_DIR / "history"
        )
        # In-memory cache of latest observations per device: {device_id: DeviceObservation}
        self._last_observations: Dict[str, DeviceObservation] = {}

    def _resolve_room_name(self, device_id: str) -> str:
        """Map physical device ID to human-readable room name."""
        if device_id == self.config.TCL_LIVING_ROOM_ID or "frx" in device_id:
            return "living_room"
        if device_id == self.config.TCL_BEDROOM_ID or "g2h" in device_id:
            return "bedroom"
        return "unknown_room"

    def collect_device_observation(
        self,
        device_id: str,
        state_obj: Any,
        eval_time_sofia: datetime,
        system_state: SystemState,
        active_reservation: Optional[Reservation] = None,
        manual_override: Optional[Dict[str, Any]] = None,
        command_sent: bool = False,
    ) -> DeviceObservation:
        """Build a structured DeviceObservation from device state and metadata."""
        now_sofia = eval_time_sofia
        if now_sofia.tzinfo is None:
            now_sofia = now_sofia.replace(tzinfo=TIMEZONE_SOFIA)
        now_utc = now_sofia.astimezone(timezone.utc)

        # Deterministic minute timestamp for idempotency: YYYYMMDDTHHMM
        minute_str = now_sofia.strftime("%Y%m%dT%H%M")
        obs_id = f"{device_id}_{minute_str}"

        room = self._resolve_room_name(device_id)

        # Device properties with graceful fallbacks
        power = bool(getattr(state_obj, "power", False))
        mode_val = getattr(state_obj, "mode", None)
        mode_str = mode_val.value if hasattr(mode_val, "value") else str(mode_val or "Heat")
        target_temp = float(getattr(state_obj, "target_temperature", 22.0) or 22.0)
        indoor_temp = getattr(state_obj, "current_temperature", None)
        if indoor_temp is not None:
            try:
                indoor_temp = float(indoor_temp)
            except (ValueError, TypeError):
                indoor_temp = None

        fan_val = getattr(state_obj, "fan_speed", None)
        fan_str = fan_val.value if hasattr(fan_val, "value") else str(fan_val or "Auto")
        display_led = bool(getattr(state_obj, "display_led", False))
        beep_feedback = bool(getattr(state_obj, "beep_feedback", False))
        online = bool(getattr(state_obj, "is_online", getattr(state_obj, "online", True)))

        # Temperature measurement timestamp & freshness
        temp_dt = getattr(state_obj, "temp_measured_at", getattr(state_obj, "current_temperature_timestamp", None))
        temp_measured_at_str = None
        temp_age_minutes = None
        temp_freshness = "UNAVAILABLE"

        if indoor_temp is None or not online:
            temp_freshness = "UNAVAILABLE"
        elif temp_dt is not None:
            try:
                temp_measured_at_str = temp_dt.isoformat()
                t_meas = temp_dt
                if t_meas.tzinfo is None:
                    t_meas = t_meas.replace(tzinfo=timezone.utc)
                age_sec = max(0.0, (now_utc - t_meas).total_seconds())
                temp_age_minutes = round(age_sec / 60.0, 1)
                max_fresh = getattr(self.config, "TEMP_FRESHNESS_MAX_AGE_MINUTES", 60.0)
                temp_freshness = "FRESH" if temp_age_minutes <= max_fresh else "STALE"
            except Exception:
                temp_freshness = "FRESH"
        else:
            temp_freshness = "FRESH"

        # Shadow metadata timestamps & shadow version
        shadow_version = None
        meta_power_ts_str = None
        meta_temp_ts_str = None

        raw_shadow = getattr(state_obj, "raw_shadow", {})
        if isinstance(raw_shadow, dict):
            shadow_version = raw_shadow.get("version")
            meta_rep = raw_shadow.get("metadata", {}).get("reported", {})
            if isinstance(meta_rep, dict):
                # Power switch metadata
                p_meta = meta_rep.get("powerSwitch", meta_rep.get("Power"))
                if isinstance(p_meta, dict) and "timestamp" in p_meta:
                    try:
                        p_dt = datetime.fromtimestamp(float(p_meta["timestamp"]), tz=timezone.utc)
                        meta_power_ts_str = p_dt.astimezone(TIMEZONE_SOFIA).strftime("%Y-%m-%d %H:%M:%S Sofia")
                    except Exception:
                        pass
                # Target temperature metadata
                t_meta = meta_rep.get("targetTemperature", meta_rep.get("SetTemp"))
                if isinstance(t_meta, dict) and "timestamp" in t_meta:
                    try:
                        t_dt = datetime.fromtimestamp(float(t_meta["timestamp"]), tz=timezone.utc)
                        meta_temp_ts_str = t_dt.astimezone(TIMEZONE_SOFIA).strftime("%Y-%m-%d %H:%M:%S Sofia")
                    except Exception:
                        pass

        # Guest manual override status
        guest_override_active = False
        power_off_override = False
        temp_override_val = None
        if manual_override and isinstance(manual_override, dict):
            guest_override_active = True
            power_off_override = bool(manual_override.get("power_off", False))
            temp_override_val = manual_override.get("temp_override")

        # Reservation metadata
        res_uid_masked = None
        if active_reservation:
            res_uid_masked = active_reservation.masked_uid

        return DeviceObservation(
            observation_id=obs_id,
            timestamp_utc=now_utc.isoformat(),
            timestamp_sofia=now_sofia.isoformat(),
            device_id=device_id,
            room=room,
            power=power,
            mode=mode_str,
            target_temperature=target_temp,
            indoor_temperature=indoor_temp,
            temp_measured_at=temp_measured_at_str,
            temp_freshness=temp_freshness,
            temp_age_minutes=temp_age_minutes,
            fan_speed=fan_str,
            display_led=display_led,
            beep_feedback=beep_feedback,
            online=online,
            shadow_version=shadow_version,
            guest_override_active=guest_override_active,
            power_off_override=power_off_override,
            temp_override=temp_override_val,
            external_change_detected=guest_override_active or (not power and not command_sent),
            active_reservation_uid=res_uid_masked,
            system_state=system_state.value if hasattr(system_state, "value") else str(system_state),
            command_sent=command_sent,
            dry_run=self.config.DRY_RUN,
            read_only=self.config.TCL_READ_ONLY,
            actual_metered_kwh=None,  # TCL Cloud does not expose energy data
            metadata_power_timestamp=meta_power_ts_str,
            metadata_temp_timestamp=meta_temp_ts_str,
        )

    def detect_events(
        self,
        curr: DeviceObservation,
        prev: Optional[DeviceObservation]
    ) -> List[OperationEvent]:
        """Compare current observation with previous observation to detect discrete events."""
        events: List[OperationEvent] = []
        if not prev:
            return events

        room_label = "Living Room" if curr.room == "living_room" else "Bedroom"

        # 1. Power State Change
        if prev.power != curr.power:
            ev_type = "POWER_ON" if curr.power else "POWER_OFF"
            exact_ts = curr.metadata_power_timestamp
            det_method = "METADATA_TIMESTAMP" if exact_ts else "POLL_INTERVAL"
            desc = (
                f"{room_label} turned {'ON' if curr.power else 'OFF'}. "
                f"(Detected via {det_method.lower().replace('_', ' ')})"
            )
            ev_id = f"ev_power_{curr.device_id}_{curr.timestamp_utc}_{1 if curr.power else 0}"
            events.append(OperationEvent(
                event_id=ev_id,
                timestamp_utc=curr.timestamp_utc,
                timestamp_sofia=curr.timestamp_sofia,
                device_id=curr.device_id,
                room=curr.room,
                event_type=ev_type,
                previous_value=prev.power,
                new_value=curr.power,
                detection_method=det_method,
                exact_change_time_sofia=exact_ts,
                description=desc,
                reservation_uid_masked=curr.active_reservation_uid,
            ))

        # 2. Target Temperature Adjustment
        if abs(curr.target_temperature - prev.target_temperature) >= 0.5:
            exact_ts = curr.metadata_temp_timestamp
            det_method = "METADATA_TIMESTAMP" if exact_ts else "POLL_INTERVAL"
            desc = (
                f"{room_label} target temperature adjusted from {prev.target_temperature:.1f}°C "
                f"to {curr.target_temperature:.1f}°C."
            )
            ev_id = f"ev_temp_{curr.device_id}_{curr.timestamp_utc}_{curr.target_temperature}"
            events.append(OperationEvent(
                event_id=ev_id,
                timestamp_utc=curr.timestamp_utc,
                timestamp_sofia=curr.timestamp_sofia,
                device_id=curr.device_id,
                room=curr.room,
                event_type="TARGET_TEMP_CHANGE",
                previous_value=prev.target_temperature,
                new_value=curr.target_temperature,
                detection_method=det_method,
                exact_change_time_sofia=exact_ts,
                description=desc,
                reservation_uid_masked=curr.active_reservation_uid,
            ))

        # 3. Fan Speed Change
        if prev.fan_speed != curr.fan_speed:
            desc = f"{room_label} fan speed changed from {prev.fan_speed} to {curr.fan_speed}."
            ev_id = f"ev_fan_{curr.device_id}_{curr.timestamp_utc}_{curr.fan_speed}"
            events.append(OperationEvent(
                event_id=ev_id,
                timestamp_utc=curr.timestamp_utc,
                timestamp_sofia=curr.timestamp_sofia,
                device_id=curr.device_id,
                room=curr.room,
                event_type="FAN_SPEED_CHANGE",
                previous_value=prev.fan_speed,
                new_value=curr.fan_speed,
                detection_method="POLL_INTERVAL",
                description=desc,
                reservation_uid_masked=curr.active_reservation_uid,
            ))

        # 4. Mode Change
        if prev.mode != curr.mode:
            desc = f"{room_label} operating mode changed from {prev.mode} to {curr.mode}."
            ev_id = f"ev_mode_{curr.device_id}_{curr.timestamp_utc}_{curr.mode}"
            events.append(OperationEvent(
                event_id=ev_id,
                timestamp_utc=curr.timestamp_utc,
                timestamp_sofia=curr.timestamp_sofia,
                device_id=curr.device_id,
                room=curr.room,
                event_type="MODE_CHANGE",
                previous_value=prev.mode,
                new_value=curr.mode,
                detection_method="POLL_INTERVAL",
                description=desc,
                reservation_uid_masked=curr.active_reservation_uid,
            ))

        # 5. Connectivity Transition (Online/Offline)
        if prev.online != curr.online:
            ev_type = "DEVICE_ONLINE" if curr.online else "DEVICE_OFFLINE"
            desc = f"{room_label} connectivity changed to {'ONLINE' if curr.online else 'OFFLINE'}."
            ev_id = f"ev_online_{curr.device_id}_{curr.timestamp_utc}_{curr.online}"
            events.append(OperationEvent(
                event_id=ev_id,
                timestamp_utc=curr.timestamp_utc,
                timestamp_sofia=curr.timestamp_sofia,
                device_id=curr.device_id,
                room=curr.room,
                event_type=ev_type,
                previous_value=prev.online,
                new_value=curr.online,
                detection_method="POLL_INTERVAL",
                description=desc,
                reservation_uid_masked=curr.active_reservation_uid,
            ))

        # 6. Prolonged Stale Telemetry (> 3.0 hours without fresh measurement)
        if (
            curr.temp_freshness == "STALE"
            and prev.temp_freshness != "STALE"
            and curr.temp_age_minutes is not None
            and curr.temp_age_minutes >= 180.0
        ):
            desc = (
                f"{room_label} telemetry is prolonged STALE ({curr.temp_age_minutes / 60.0:.1f}h old). "
                f"Thermistor stagnant while unit is unpowered."
            )
            ev_id = f"ev_stale_{curr.device_id}_{curr.timestamp_utc}"
            events.append(OperationEvent(
                event_id=ev_id,
                timestamp_utc=curr.timestamp_utc,
                timestamp_sofia=curr.timestamp_sofia,
                device_id=curr.device_id,
                room=curr.room,
                event_type="PROLONGED_STALE_TEMP",
                previous_value=prev.temp_freshness,
                new_value=curr.temp_freshness,
                detection_method="POLL_INTERVAL",
                description=desc,
                reservation_uid_masked=curr.active_reservation_uid,
            ))

        return events

    def record_cycle(
        self,
        current_states: Dict[str, Any],
        eval_time_sofia: datetime,
        system_state: SystemState,
        active_reservation: Optional[Reservation] = None,
        manual_overrides: Optional[Dict[str, Any]] = None,
        commands_sent: Optional[Dict[str, bool]] = None,
    ) -> List[DeviceObservation]:
        """
        Record observations for all active devices during the 15-minute cycle.
        Completely isolated in defensive try/except to protect the main automation loop.
        """
        observations: List[DeviceObservation] = []
        overrides = manual_overrides or {}
        cmds_sent = commands_sent or {}

        for dev_id, st in current_states.items():
            try:
                # 1. Fetch previous observation for event comparison
                prev_obs = self._last_observations.get(dev_id)
                if not prev_obs:
                    prev_obs = self.history_store.get_latest_observation(dev_id)

                # 2. Build current observation
                is_cmd_sent = bool(cmds_sent.get(dev_id, False))
                dev_override = overrides.get(dev_id)
                curr_obs = self.collect_device_observation(
                    device_id=dev_id,
                    state_obj=st,
                    eval_time_sofia=eval_time_sofia,
                    system_state=system_state,
                    active_reservation=active_reservation,
                    manual_override=dev_override,
                    command_sent=is_cmd_sent,
                )

                # 3. Detect discrete events
                events = self.detect_events(curr=curr_obs, prev=prev_obs)

                # 4. Save observation and events to persistent storage
                self.history_store.save_observation(curr_obs)
                for ev in events:
                    self.history_store.save_event(ev)

                # 5. Update cached state
                self._last_observations[dev_id] = curr_obs
                observations.append(curr_obs)

            except Exception as e:
                _LOGGER.error(f"Error collecting telemetry observation for {dev_id}: {e}", exc_info=True)

        return observations
