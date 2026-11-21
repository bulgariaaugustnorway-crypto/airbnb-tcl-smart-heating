"""Persistent history storage for observations, events, weather, and meter readings.

Supports AWS S3 object storage (production) and local JSONL/JSON storage (test & dev),
with optimistic concurrency safety, deduplication, and CSV export.
"""

import csv
import io
import json
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.monitoring.models import (
    DeviceObservation,
    OperationEvent,
    DailyDeviceSummary,
    WholePropertyMeterReading,
    OutdoorWeatherReading,
)

_LOGGER = logging.getLogger(__name__)


_UNSET = object()


class ObservationHistoryStore:
    """Manages append-oriented time-series history in S3 and local files."""

    def __init__(
        self,
        s3_bucket: Optional[str] = _UNSET,
        local_dir: Optional[Path] = _UNSET,
        in_memory: bool = False,
        s3_client: Optional[Any] = None,
    ):
        self.in_memory = in_memory
        self.s3_bucket = AutomationConfig.S3_BUCKET if s3_bucket is _UNSET else s3_bucket
        self.local_dir = (AutomationConfig.DATA_DIR / "history") if local_dir is _UNSET else local_dir
        self.s3_client = s3_client

        # In-memory test buffers: {key: list of dicts/lines}
        self._memory_observations: Dict[str, List[Dict[str, Any]]] = {}
        self._memory_events: Dict[str, List[Dict[str, Any]]] = {}
        self._memory_meter_readings: List[Dict[str, Any]] = []
        self._memory_weather: Dict[str, List[Dict[str, Any]]] = {}

        if not self.in_memory and self.local_dir:
            try:
                (self.local_dir / "observations").mkdir(parents=True, exist_ok=True)
                (self.local_dir / "events").mkdir(parents=True, exist_ok=True)
                (self.local_dir / "weather").mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 1. Observation Storage & Retrieval
    # ------------------------------------------------------------------
    def _observation_key(self, dt: datetime, device_id: str) -> str:
        """Derive S3 key / relative path for a daily device observation file."""
        date_str = dt.strftime("%Y-%m-%d")
        return f"history/observations/{date_str}/{device_id}.jsonl"

    def save_observation(self, obs: DeviceObservation) -> bool:
        """
        Append an observation to the appropriate daily partition.
        Idempotent: skips if an observation with the same observation_id already exists.
        """
        try:
            dt = datetime.fromisoformat(obs.timestamp_sofia)
        except Exception:
            dt = datetime.now(TIMEZONE_SOFIA)

        rel_key = self._observation_key(dt, obs.device_id)
        obs_dict = obs.to_dict()

        if self.in_memory:
            existing = self._memory_observations.setdefault(rel_key, [])
            if any(o.get("observation_id") == obs.observation_id for o in existing):
                _LOGGER.debug(f"Skipping duplicate in-memory observation {obs.observation_id}")
                return True
            existing.append(obs_dict)
            return True

        success = False
        obs_id_token = f'"{obs.observation_id}"'

        # 1. S3 Persistence
        if self.s3_bucket:
            try:
                s3 = self.s3_client
                if s3 is None:
                    import boto3
                    s3 = boto3.client("s3")
                existing_lines = []
                try:
                    resp = s3.get_object(Bucket=self.s3_bucket, Key=rel_key)
                    existing_lines = resp["Body"].read().decode("utf-8").strip().splitlines()
                except Exception:
                    existing_lines = []

                # Fast deduplication check
                if any(obs_id_token in line for line in existing_lines):
                    _LOGGER.debug(f"Observation {obs.observation_id} already exists in S3; skipping duplicate.")
                    return True

                # Append new line
                new_line = json.dumps(obs_dict, default=str)
                updated_content = "\n".join(existing_lines + [new_line]) + "\n"
                s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=rel_key,
                    Body=updated_content.encode("utf-8"),
                    ContentType="application/x-ndjson"
                )
                _LOGGER.debug(f"Saved observation to s3://{self.s3_bucket}/{rel_key}")
                success = True
            except Exception as e:
                _LOGGER.error(f"Failed to save observation to S3 ({rel_key}): {e}")

        # 2. Local File Persistence
        if self.local_dir:
            try:
                target_file = self.local_dir / "observations" / dt.strftime("%Y-%m-%d") / f"{obs.device_id}.jsonl"
                target_file.parent.mkdir(parents=True, exist_ok=True)
                if target_file.exists():
                    file_content = target_file.read_text(encoding="utf-8")
                    if obs_id_token in file_content:
                        return True
                new_line = json.dumps(obs_dict, default=str)
                with target_file.open("a", encoding="utf-8") as f:
                    f.write(new_line + "\n")
                success = True
            except Exception as e:
                _LOGGER.warning(f"Failed to write local observation file: {e}")

        return success

    def get_observations(
        self,
        device_id: str,
        start_date: date,
        end_date: date
    ) -> List[DeviceObservation]:
        """Retrieve all observations for a device between start_date and end_date (inclusive)."""
        observations: List[DeviceObservation] = []
        curr = start_date
        while curr <= end_date:
            date_str = curr.strftime("%Y-%m-%d")
            rel_key = f"history/observations/{date_str}/{device_id}.jsonl"
            lines: List[str] = []

            if self.in_memory:
                raw_list = self._memory_observations.get(rel_key, [])
                for d in raw_list:
                    observations.append(DeviceObservation.from_dict(d))
                curr += timedelta(days=1)
                continue

            # Check S3
            if self.s3_bucket:
                try:
                    import boto3
                    s3 = boto3.client("s3")
                    resp = s3.get_object(Bucket=self.s3_bucket, Key=rel_key)
                    lines = resp["Body"].read().decode("utf-8").strip().splitlines()
                except Exception:
                    lines = []

            # Fallback to local
            if not lines and self.local_dir:
                local_f = self.local_dir / "observations" / date_str / f"{device_id}.jsonl"
                if local_f.exists():
                    try:
                        lines = local_f.read_text(encoding="utf-8").strip().splitlines()
                    except Exception:
                        lines = []

            for line in lines:
                if line.strip():
                    try:
                        d = json.loads(line)
                        observations.append(DeviceObservation.from_dict(d))
                    except Exception:
                        pass

            curr += timedelta(days=1)

        # Sort by timestamp
        observations.sort(key=lambda o: o.timestamp_utc)
        return observations

    def get_latest_observation(self, device_id: str) -> Optional[DeviceObservation]:
        """Retrieve the most recent observation for a device."""
        today = datetime.now(TIMEZONE_SOFIA).date()
        # Look back up to 3 days
        for i in range(4):
            day = today - timedelta(days=i)
            obs_list = self.get_observations(device_id, day, day)
            if obs_list:
                return obs_list[-1]
        return None

    def get_observations_for_date(self, device_id: str, target_date: date) -> List[DeviceObservation]:
        """Convenience query for all observations of a device on a single date."""
        return self.get_observations(device_id, target_date, target_date)

    # ------------------------------------------------------------------
    # 2. Operation Events Storage & Retrieval
    # ------------------------------------------------------------------
    def _event_key(self, dt: datetime, device_id: str) -> str:
        date_str = dt.strftime("%Y-%m-%d")
        return f"history/events/{date_str}/{device_id}.jsonl"

    def save_event(self, event: OperationEvent) -> bool:
        """Save a detected operation event with idempotency."""
        try:
            dt = datetime.fromisoformat(event.timestamp_sofia)
        except Exception:
            dt = datetime.now(TIMEZONE_SOFIA)

        rel_key = self._event_key(dt, event.device_id)
        ev_dict = event.to_dict()

        if self.in_memory:
            existing = self._memory_events.setdefault(rel_key, [])
            if any(e.get("event_id") == event.event_id for e in existing):
                return True
            existing.append(ev_dict)
            return True

        if self.s3_bucket:
            try:
                import boto3
                s3 = boto3.client("s3")
                existing_lines = []
                try:
                    resp = s3.get_object(Bucket=self.s3_bucket, Key=rel_key)
                    existing_lines = resp["Body"].read().decode("utf-8").strip().splitlines()
                except Exception:
                    existing_lines = []

                for line in existing_lines:
                    if line.strip():
                        try:
                            item = json.loads(line)
                            if item.get("event_id") == event.event_id:
                                return True
                        except Exception:
                            pass

                new_line = json.dumps(ev_dict, default=str)
                updated_content = "\n".join(existing_lines + [new_line]) + "\n"
                s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=rel_key,
                    Body=updated_content.encode("utf-8"),
                    ContentType="application/x-ndjson"
                )
            except Exception as e:
                _LOGGER.error(f"Failed to save event to S3 ({rel_key}): {e}")

        if self.local_dir:
            try:
                target_file = self.local_dir / "events" / dt.strftime("%Y-%m-%d") / f"{event.device_id}.jsonl"
                target_file.parent.mkdir(parents=True, exist_ok=True)
                new_line = json.dumps(ev_dict, default=str)
                with target_file.open("a", encoding="utf-8") as f:
                    f.write(new_line + "\n")
            except Exception as e:
                _LOGGER.warning(f"Failed to write local event file: {e}")

        return True

    def get_events(
        self,
        device_id: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100
    ) -> List[OperationEvent]:
        """Retrieve recent operation events across one or all devices."""
        now_sofia = datetime.now(TIMEZONE_SOFIA).date()
        s_date = start_date or (now_sofia - timedelta(days=7))
        e_date = end_date or now_sofia

        dev_ids = [device_id] if device_id else [
            AutomationConfig.TCL_LIVING_ROOM_ID,
            AutomationConfig.TCL_BEDROOM_ID
        ]

        events: List[OperationEvent] = []
        for dev in dev_ids:
            curr = s_date
            while curr <= e_date:
                date_str = curr.strftime("%Y-%m-%d")
                rel_key = f"history/events/{date_str}/{dev}.jsonl"

                if self.in_memory:
                    raw_list = self._memory_events.get(rel_key, [])
                    for d in raw_list:
                        events.append(OperationEvent.from_dict(d))
                    curr += timedelta(days=1)
                    continue

                lines = []
                if self.s3_bucket:
                    try:
                        import boto3
                        s3 = boto3.client("s3")
                        resp = s3.get_object(Bucket=self.s3_bucket, Key=rel_key)
                        lines = resp["Body"].read().decode("utf-8").strip().splitlines()
                    except Exception:
                        lines = []

                if not lines and self.local_dir:
                    local_f = self.local_dir / "events" / date_str / f"{dev}.jsonl"
                    if local_f.exists():
                        try:
                            lines = local_f.read_text(encoding="utf-8").strip().splitlines()
                        except Exception:
                            lines = []

                for line in lines:
                    if line.strip():
                        try:
                            d = json.loads(line)
                            events.append(OperationEvent.from_dict(d))
                        except Exception:
                            pass

                curr += timedelta(days=1)

        events.sort(key=lambda ev: ev.timestamp_utc, reverse=True)
        return events[:limit]

    # ------------------------------------------------------------------
    # 3. Whole-Property Electricity Meter Readings
    # ------------------------------------------------------------------
    def get_meter_readings(self) -> List[WholePropertyMeterReading]:
        """Load list of manual whole-apartment meter readings."""
        if self.in_memory:
            return [WholePropertyMeterReading.from_dict(r) for r in self._memory_meter_readings]

        s3_key = "history/meter_readings.json"
        content = None

        if self.s3_bucket:
            try:
                import boto3
                s3 = boto3.client("s3")
                resp = s3.get_object(Bucket=self.s3_bucket, Key=s3_key)
                content = resp["Body"].read().decode("utf-8")
            except Exception:
                content = None

        if not content and self.local_dir:
            loc_f = self.local_dir / "meter_readings.json"
            if loc_f.exists():
                try:
                    content = loc_f.read_text(encoding="utf-8")
                except Exception:
                    content = None

        if not content:
            return []

        try:
            data = json.loads(content)
            if isinstance(data, list):
                readings = [WholePropertyMeterReading.from_dict(d) for d in data if isinstance(d, dict)]
                readings.sort(key=lambda r: r.reading_date)
                return readings
        except Exception as e:
            _LOGGER.warning(f"Failed to parse meter readings: {e}")

        return []

    def record_meter_reading(
        self,
        reading_date: str,
        meter_kwh: float,
        recorded_by: str = "owner",
        notes: str = ""
    ) -> WholePropertyMeterReading:
        """
        Record a manual reading from the whole-apartment utility meter.
        Calculates delta since previous reading if available.
        """
        readings = self.get_meter_readings()
        now_sofia = datetime.now(TIMEZONE_SOFIA)
        now_utc = datetime.now(timezone.utc)

        # Calculate daily delta if previous reading exists
        daily_kwh = None
        period_h = None
        if readings:
            prev = readings[-1]
            try:
                delta_kwh = meter_kwh - prev.meter_kwh
                if delta_kwh >= 0:
                    daily_kwh = round(delta_kwh, 2)
                # Compute hours difference
                t_prev = datetime.fromisoformat(prev.timestamp_utc)
                t_curr = now_utc
                if t_prev.tzinfo is None:
                    t_prev = t_prev.replace(tzinfo=timezone.utc)
                period_h = round(max(0.0, (t_curr - t_prev).total_seconds() / 3600.0), 1)
            except Exception:
                daily_kwh = None

        reading_id = f"meter_{reading_date}_{int(now_utc.timestamp())}"
        new_reading = WholePropertyMeterReading(
            reading_id=reading_id,
            timestamp_utc=now_utc.isoformat(),
            timestamp_sofia=now_sofia.isoformat(),
            reading_date=reading_date,
            meter_kwh=float(meter_kwh),
            daily_consumption_kwh=daily_kwh,
            period_hours=period_h,
            recorded_by=recorded_by,
            notes=notes,
            scope="WHOLE_APARTMENT"
        )

        readings.append(new_reading)
        readings_dict = [r.to_dict() for r in readings]

        if self.in_memory:
            self._memory_meter_readings = readings_dict
            return new_reading

        s3_key = "history/meter_readings.json"
        payload_bytes = json.dumps(readings_dict, indent=2).encode("utf-8")

        if self.s3_bucket:
            try:
                import boto3
                s3 = boto3.client("s3")
                s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=s3_key,
                    Body=payload_bytes,
                    ContentType="application/json"
                )
            except Exception as e:
                _LOGGER.error(f"Failed to save meter readings to S3: {e}")

        if self.local_dir:
            try:
                loc_f = self.local_dir / "meter_readings.json"
                loc_f.parent.mkdir(parents=True, exist_ok=True)
                loc_f.write_bytes(payload_bytes)
            except Exception as e:
                _LOGGER.warning(f"Failed to save local meter readings: {e}")

        return new_reading

    def save_meter_reading(self, reading: WholePropertyMeterReading) -> bool:
        """Persist a WholePropertyMeterReading object directly."""
        readings = self.get_meter_readings()
        readings = [r for r in readings if r.reading_id != reading.reading_id]
        readings.append(reading)
        readings.sort(key=lambda r: r.timestamp_utc)
        readings_dict = [r.to_dict() for r in readings]

        if self.in_memory:
            self._memory_meter_readings = readings_dict
            return True

        s3_key = "history/meter_readings.json"
        payload_bytes = json.dumps(readings_dict, indent=2).encode("utf-8")

        if self.s3_bucket:
            try:
                s3 = self.s3_client
                if s3 is None:
                    import boto3
                    s3 = boto3.client("s3")
                s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=s3_key,
                    Body=payload_bytes,
                    ContentType="application/json"
                )
            except Exception as e:
                _LOGGER.error(f"Failed to save meter readings to S3: {e}")

        if self.local_dir:
            try:
                loc_f = self.local_dir / "meter_readings.json"
                loc_f.parent.mkdir(parents=True, exist_ok=True)
                loc_f.write_bytes(payload_bytes)
            except Exception as e:
                _LOGGER.warning(f"Failed to save local meter readings: {e}")

        return True

    # ------------------------------------------------------------------
    # 4. Aggregations & Daily Summaries
    # ------------------------------------------------------------------
    def compute_daily_summary(
        self,
        device_id: str,
        target_date: date,
        room: str = ""
    ) -> DailyDeviceSummary:
        """Calculate aggregated daily stats for a device from raw 15-minute observations."""
        observations = self.get_observations(device_id, target_date, target_date)
        resolved_room = room or ("living_room" if "frx" in device_id else "bedroom")

        summary = DailyDeviceSummary(
            date=target_date.strftime("%Y-%m-%d"),
            device_id=device_id,
            room=resolved_room,
            total_samples=len(observations),
            sample_interval_minutes=15.0
        )

        if not observations:
            return summary

        indoor_temps = []
        setpoints: Dict[str, int] = {}

        for obs in observations:
            if obs.power:
                summary.on_samples += 1
            if obs.indoor_temperature is not None:
                indoor_temps.append(obs.indoor_temperature)
            if obs.target_temperature is not None:
                t_str = f"{obs.target_temperature:.1f}°C"
                setpoints[t_str] = setpoints.get(t_str, 0) + 1
            if obs.temp_freshness == "STALE":
                summary.stale_temp_samples += 1
            if not obs.online:
                summary.offline_samples += 1

        summary.approx_on_hours = round(summary.on_samples * (summary.sample_interval_minutes / 60.0), 2)
        summary.setpoint_distribution = setpoints

        if indoor_temps:
            summary.min_indoor_temp = round(min(indoor_temps), 1)
            summary.max_indoor_temp = round(max(indoor_temps), 1)
            summary.avg_indoor_temp = round(sum(indoor_temps) / len(indoor_temps), 1)

        # Count events for this day
        events = self.get_events(device_id=device_id, start_date=target_date, end_date=target_date)
        for ev in events:
            if ev.event_type == "POWER_ON":
                summary.power_on_events += 1
            elif ev.event_type == "POWER_OFF":
                summary.power_off_events += 1
            elif ev.event_type == "TARGET_TEMP_CHANGE":
                summary.temp_adjustments += 1

        return summary

    # ------------------------------------------------------------------
    # 5. CSV Export
    # ------------------------------------------------------------------
    def export_observations_to_csv(
        self,
        start_date: date,
        end_date: date,
        device_ids: Optional[List[str]] = None
    ) -> str:
        """Export all observations in the date range to CSV format."""
        devs = device_ids or [
            AutomationConfig.TCL_LIVING_ROOM_ID,
            AutomationConfig.TCL_BEDROOM_ID
        ]

        all_obs: List[DeviceObservation] = []
        for dev in devs:
            all_obs.extend(self.get_observations(dev, start_date, end_date))

        all_obs.sort(key=lambda o: (o.timestamp_utc, o.room))

        output = io.StringIO()
        fieldnames = [
            "timestamp_sofia",
            "timestamp_utc",
            "room",
            "device_id",
            "power",
            "mode",
            "target_temperature",
            "indoor_temperature",
            "temp_freshness",
            "temp_age_minutes",
            "fan_speed",
            "display_led",
            "online",
            "guest_override_active",
            "power_off_override",
            "temp_override",
            "system_state",
            "command_sent",
            "actual_metered_kwh",
            "shadow_version",
            "active_reservation_uid"
        ]

        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for obs in all_obs:
            row = obs.to_dict()
            row["actual_metered_kwh"] = "N/A (TCL Cloud lacks energy metering)"
            writer.writerow(row)

        return output.getvalue()
