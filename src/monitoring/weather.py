"""Outdoor weather integration for a user-configured location.

Uses the free, public Open-Meteo API (zero API key, zero recurring cost)
to correlate indoor HVAC behavior with real outdoor temperatures.
"""

import json
import os
import logging
import urllib.request
from src import http_transport
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.monitoring.models import OutdoorWeatherReading

_LOGGER = logging.getLogger(__name__)

# Optional coordinates, never inferred from a property
WEATHER_LAT = os.getenv("WEATHER_LATITUDE")
WEATHER_LON = os.getenv("WEATHER_LONGITUDE")
WEATHER_ELEVATION_M = os.getenv("WEATHER_ELEVATION_M")


_UNSET = object()


class WeatherService:
    """Fetches, caches, and correlates outdoor weather observations for a configured location."""

    def __init__(
        self,
        s3_bucket: Optional[str] = _UNSET,
        local_dir: Optional[Path] = _UNSET,
        in_memory: bool = False,
    ):
        self.in_memory = in_memory
        self.s3_bucket = AutomationConfig.S3_BUCKET if s3_bucket is _UNSET else s3_bucket
        self.local_dir = (AutomationConfig.DATA_DIR / "history" / "weather") if local_dir is _UNSET else local_dir
        self._memory_cache: Dict[str, Dict[str, Any]] = {}

        if not self.in_memory and self.local_dir:
            try:
                self.local_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def get_weather_for_date(self, target_date: date) -> Optional[Dict[str, Any]]:
        """
        Retrieve weather data for target_date.
        Checks S3/local cache first, falls back to Open-Meteo API.
        """
        date_str = target_date.strftime("%Y-%m-%d")

        # 1. Check in-memory cache
        if date_str in self._memory_cache:
            return self._memory_cache[date_str]

        # 2. Check S3 cache
        s3_key = f"history/weather/{date_str}.json"
        if self.s3_bucket and not self.in_memory:
            try:
                import boto3
                s3 = boto3.client("s3")
                resp = s3.get_object(Bucket=self.s3_bucket, Key=s3_key)
                content = resp["Body"].read().decode("utf-8")
                data = json.loads(content)
                self._memory_cache[date_str] = data
                return data
            except Exception:
                pass

        # 3. Check local disk cache
        if self.local_dir and not self.in_memory:
            loc_f = self.local_dir / f"{date_str}.json"
            if loc_f.exists():
                try:
                    data = json.loads(loc_f.read_text(encoding="utf-8"))
                    self._memory_cache[date_str] = data
                    return data
                except Exception:
                    pass

        # 4. Fetch from Open-Meteo API
        return self._fetch_from_open_meteo(target_date)

    def _fetch_from_open_meteo(self, target_date: date) -> Optional[Dict[str, Any]]:
        """Fetch historical or recent daily weather for a configured location from Open-Meteo."""
        if not WEATHER_LAT or not WEATHER_LON:
            return None
        date_str = target_date.strftime("%Y-%m-%d")
        today = datetime.now(TIMEZONE_SOFIA).date()

        # Choose endpoint: historical archive vs forecast API
        if target_date < today:
            base_url = "https://archive-api.open-meteo.com/v1/archive"
        else:
            base_url = "https://api.open-meteo.com/v1/forecast"

        params = {
            "latitude": str(WEATHER_LAT),
            "longitude": str(WEATHER_LON),
            "start_date": date_str,
            "end_date": date_str,
            "hourly": "temperature_2m,relative_humidity_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "Europe/Sofia"
        }

        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        _LOGGER.debug("Fetching configured outdoor weather")

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TCL-Airbnb-HVAC-Monitor/1.0"})
            with http_transport.open_https(req, timeout=5.0) as resp:
                raw_json = json.loads(resp.read().decode("utf-8"))

            hourly = raw_json.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])
            humidities = hourly.get("relative_humidity_2m", [])

            daily = raw_json.get("daily", {})
            min_temp = daily.get("temperature_2m_min", [None])[0] if daily.get("temperature_2m_min") else None
            max_temp = daily.get("temperature_2m_max", [None])[0] if daily.get("temperature_2m_max") else None

            # Calculate overnight minimum (23:00 to 07:00)
            overnight_temps = []
            hourly_readings = []
            for t_str, temp in zip(times, temps):
                if temp is not None:
                    # t_str is format "YYYY-MM-DDTHH:MM"
                    hour = int(t_str.split("T")[-1].split(":")[0])
                    if hour >= 23 or hour <= 7:
                        overnight_temps.append(temp)
                    hourly_readings.append({"time": t_str, "temp_c": temp})

            overnight_min = min(overnight_temps) if overnight_temps else min_temp

            weather_data = {
                "date": date_str,
                "location": "Bansko, Bulgaria",
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "elevation_m": WEATHER_ELEVATION_M,
                "min_temp_c": min_temp,
                "max_temp_c": max_temp,
                "overnight_min_c": overnight_min,
                "hourly": hourly_readings,
                "source": "Open-Meteo (open-meteo.com)",
                "fetched_at": datetime.now(timezone.utc).isoformat()
            }

            self._save_to_cache(date_str, weather_data)
            return weather_data

        except Exception as e:
            _LOGGER.warning(f"Could not fetch weather from Open-Meteo for {date_str}: {e}")
            return None

    def _save_to_cache(self, date_str: str, data: Dict[str, Any]) -> None:
        """Save weather response to memory, disk, and S3."""
        self._memory_cache[date_str] = data
        if self.in_memory:
            return

        payload = json.dumps(data, indent=2).encode("utf-8")

        # Save local
        if self.local_dir:
            try:
                (self.local_dir / f"{date_str}.json").write_bytes(payload)
            except Exception:
                pass

        # Save S3
        if self.s3_bucket:
            try:
                import boto3
                s3 = boto3.client("s3")
                s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=f"history/weather/{date_str}.json",
                    Body=payload,
                    ContentType="application/json"
                )
            except Exception:
                pass

    def get_weather_summary_for_period(
        self,
        start_date: date,
        end_date: date
    ) -> List[Dict[str, Any]]:
        """Get daily weather summaries across a multi-day observation period."""
        results = []
        curr = start_date
        while curr <= end_date:
            w = self.get_weather_for_date(curr)
            if w:
                results.append(w)
            curr += timedelta(days=1)
        return results
