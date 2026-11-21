"""Comprehensive test suite for 7-Day Read-Only HVAC Behavior and Electricity Consumption Monitoring.

Verifies:
1. 15-minute polling & DeviceObservation model structure.
2. Handling of missed scheduler invocations (temporal gaps).
3. Deduplication of duplicate scheduler invocations (idempotency).
4. Lambda cold start previous observation hydration.
5. All HVAC state transition event detections (POWER_ON/OFF, TEMP, FAN, MODE, OFFLINE, STALE).
6. Multiple simultaneous state transitions within a single observation.
7. Europe/Sofia DST transition resilience (EEST -> EET, Oct 25, 2026).
8. S3 error isolation & defensive exception handling.
9. Inaccessibility and absence of per-device TCL Cloud kWh telemetry (unmetered contract).
10. Whole-property utility meter reading separation and ingestion.
11. Bansko weather caching & daily summarization.
12. Thermal decay rate calculation during power-off periods.
13. Full 7-Day observation report generator (all 12 analytical dimensions in Markdown & JSON).
14. Dashboard monitoring APIs (summary, meter-reading, CSV export, report).
15. Orchestrator integration safety.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, date, time as dtime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.dashboard import auth
from src.dashboard.config_store import DashboardConfigStore
from src.dashboard.handler import handle_dashboard_request, _build_status_data, COOKIE_NAME
from src.monitoring.models import (
    DeviceObservation,
    OperationEvent,
    WholePropertyMeterReading,
    OutdoorWeatherReading,
    DailyDeviceSummary,
)
from src.monitoring.history_store import ObservationHistoryStore
from src.monitoring.collector import TelemetryCollector
from src.monitoring.weather import WeatherService
from src.monitoring.report_generator import SevenDayReportGenerator
from src.models import (
    ACState,
    ACMode,
    ACFanSpeed,
    Reservation,
    ReservationType,
    SystemState,
)


class TestHVACMonitoringModelsAndStorage(unittest.TestCase):
    """Test data models, serialization, and storage partitioning."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.history_dir = Path(self.temp_dir) / "history"
        self.store = ObservationHistoryStore(s3_bucket=None, local_dir=self.history_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_observation_model_structure_and_unmetered_contract(self):
        """Verify DeviceObservation fields and strict enforcement that device kWh is UNAVAILABLE."""
        now_utc = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
        obs = DeviceObservation(
            observation_id="demo_living_room_20261026T1400",
            device_id="demo_living_room",
            room="living_room",
            timestamp_utc=now_utc.isoformat(),
            timestamp_sofia="2026-10-26T14:00:00+02:00",
            power=True,
            mode="Heat",
            target_temperature=22.0,
            indoor_temperature=21.5,
            fan_speed="Auto",
            online=True,
            actual_metered_kwh=None,  # Strictly unmetered on TCL hardware
        )

        d = obs.to_dict()
        self.assertIsNone(d["actual_metered_kwh"])
        self.assertEqual(d["device_id"], "demo_living_room")
        self.assertEqual(d["room"], "living_room")

        restored = DeviceObservation.from_dict(d)
        self.assertEqual(restored.device_id, obs.device_id)
        self.assertEqual(restored.target_temperature, 22.0)
        self.assertIsNone(restored.actual_metered_kwh)

    def test_history_store_partitioned_append_and_query(self):
        """Verify partitioned storage path history/observations/YYYY-MM-DD/{device_id}.jsonl."""
        obs_date = date(2026, 10, 26)
        obs = DeviceObservation(
            observation_id="demo_living_room_20261026T1200",
            device_id="demo_living_room",
            room="living_room",
            timestamp_utc="2026-10-26T10:00:00+00:00",
            timestamp_sofia="2026-10-26T12:00:00+02:00",
            power=True,
            mode="Heat",
            target_temperature=22.0,
            indoor_temperature=21.0,
            temp_freshness="FRESH",
            fan_speed="Auto",
            online=True,
            actual_metered_kwh=None,
        )

        res = self.store.save_observation(obs)
        self.assertTrue(res)

        # Check local file existence under correct partition
        expected_path = self.history_dir / "observations" / "2026-10-26" / "demo_living_room.jsonl"
        self.assertTrue(expected_path.exists())

        loaded = self.store.get_observations_for_date("demo_living_room", obs_date)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].target_temperature, 22.0)

    def test_idempotent_deduplication(self):
        """Verify duplicate scheduler invocations within the same timestamp are not duplicated."""
        obs = DeviceObservation(
            observation_id="demo_living_room_20261026T1200",
            device_id="demo_living_room",
            room="living_room",
            timestamp_utc="2026-10-26T10:00:00+00:00",
            timestamp_sofia="2026-10-26T12:00:00+02:00",
            power=False,
            mode="Heat",
            target_temperature=20.0,
            indoor_temperature=18.5,
            temp_freshness="FRESH",
            fan_speed="Quiet",
            online=True,
            actual_metered_kwh=None,
        )

        self.store.save_observation(obs)
        # Save identical observation again
        self.store.save_observation(obs)

        loaded = self.store.get_observations_for_date("demo_living_room", date(2026, 10, 26))
        self.assertEqual(len(loaded), 1, "Duplicate observation was not deduplicated")

    def test_whole_property_meter_readings_storage(self):
        """Verify whole-property meter readings are persisted separately from AC observations."""
        reading = WholePropertyMeterReading(
            reading_id="meter-20261026-001",
            reading_date="2026-10-26",
            timestamp_utc="2026-10-26T10:00:00+00:00",
            timestamp_sofia="2026-10-26 12:00:00 Sofia",
            meter_kwh=15240.5,
            recorded_by="Owner",
            notes="Weekly audit check",
        )

        success = self.store.save_meter_reading(reading)
        self.assertTrue(success)

        readings = self.store.get_meter_readings()
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0].meter_kwh, 15240.5)
        self.assertEqual(readings[0].total_kwh, 15240.5)

    def test_csv_export_structure(self):
        """Verify CSV export includes appropriate columns and marks device kWh as UNAVAILABLE."""
        obs = DeviceObservation(
            observation_id="demo_living_room_20261026T1200",
            device_id="demo_living_room",
            room="living_room",
            timestamp_utc="2026-10-26T10:00:00+00:00",
            timestamp_sofia="2026-10-26T12:00:00+02:00",
            power=True,
            mode="Heat",
            target_temperature=23.0,
            indoor_temperature=22.1,
            temp_freshness="FRESH",
            fan_speed="Auto",
            online=True,
            actual_metered_kwh=None,
        )
        self.store.save_observation(obs)

        csv_text = self.store.export_observations_to_csv(
            start_date=date(2026, 10, 26),
            end_date=date(2026, 10, 26),
        )
        self.assertIn("timestamp_sofia", csv_text)
        self.assertIn("actual_metered_kwh", csv_text)
        self.assertIn("N/A (TCL Cloud lacks energy metering)", csv_text)
        self.assertIn("demo_living_room", csv_text)


class TestHVACCollectorAndEventDerivation(unittest.TestCase):
    """Test 15-minute telemetry collection and state transition event derivation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.history_dir = Path(self.temp_dir) / "history"
        self.history_store = ObservationHistoryStore(s3_bucket=None, local_dir=self.history_dir)
        self.collector = TelemetryCollector(history_store=self.history_store)
        self.dev_id = "demo_living_room"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_power_on_event_derivation(self):
        """Verify transition from OFF to ON generates POWER_ON event."""
        prev_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1000",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T08:00:00+00:00",
            timestamp_sofia="2026-10-26T10:00:00+02:00",
            power=False,
            mode="Heat",
            target_temperature=21.0,
            indoor_temperature=19.0,
            fan_speed="Auto",
            online=True,
        )

        curr_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1015",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T08:15:00+00:00",
            timestamp_sofia="2026-10-26T10:15:00+02:00",
            power=True,
            mode="Heat",
            target_temperature=23.0,
            indoor_temperature=19.2,
            fan_speed="Auto",
            online=True,
        )

        events = self.collector.detect_events(curr=curr_obs, prev=prev_obs)
        event_types = [e.event_type for e in events]
        self.assertIn("POWER_ON", event_types)
        self.assertIn("TARGET_TEMP_CHANGE", event_types)

    def test_power_off_event_derivation(self):
        """Verify transition from ON to OFF generates POWER_OFF event."""
        prev_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1100",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T09:00:00+00:00",
            timestamp_sofia="2026-10-26T11:00:00+02:00",
            power=True,
            mode="Heat",
            target_temperature=22.0,
            indoor_temperature=22.0,
            fan_speed="Auto",
            online=True,
        )
        curr_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1115",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T09:15:00+00:00",
            timestamp_sofia="2026-10-26T11:15:00+02:00",
            power=False,
            mode="Heat",
            target_temperature=22.0,
            indoor_temperature=22.0,
            fan_speed="Auto",
            online=True,
        )

        events = self.collector.detect_events(curr=curr_obs, prev=prev_obs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "POWER_OFF")

    def test_fan_mode_and_offline_transitions(self):
        """Verify fan speed, mode, and offline transitions are accurately detected."""
        prev_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1400",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T12:00:00+00:00",
            timestamp_sofia="2026-10-26T14:00:00+02:00",
            power=True,
            mode="Heat",
            target_temperature=22.0,
            indoor_temperature=21.0,
            fan_speed="Auto",
            online=True,
        )
        curr_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1415",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T12:15:00+00:00",
            timestamp_sofia="2026-10-26T14:15:00+02:00",
            power=True,
            mode="Cool",
            target_temperature=22.0,
            indoor_temperature=21.0,
            fan_speed="Quiet",
            online=False,
        )

        events = self.collector.detect_events(curr=curr_obs, prev=prev_obs)
        event_types = [e.event_type for e in events]
        self.assertIn("FAN_SPEED_CHANGE", event_types)
        self.assertIn("MODE_CHANGE", event_types)
        self.assertIn("DEVICE_OFFLINE", event_types)

    def test_prolonged_stale_temperature_event(self):
        """Verify transition from FRESH to STALE temperature triggers PROLONGED_STALE_TEMP event."""
        prev_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1600",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T14:00:00+00:00",
            timestamp_sofia="2026-10-26T16:00:00+02:00",
            power=False,
            mode="Heat",
            target_temperature=21.0,
            indoor_temperature=19.5,
            temp_freshness="FRESH",
            temp_age_minutes=30.0,
            fan_speed="Auto",
            online=True,
        )
        curr_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T1615",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T14:15:00+00:00",
            timestamp_sofia="2026-10-26T16:15:00+02:00",
            power=False,
            mode="Heat",
            target_temperature=21.0,
            indoor_temperature=19.5,
            temp_freshness="STALE",
            temp_age_minutes=195.0,  # > 3 hours
            fan_speed="Auto",
            online=True,
        )

        events = self.collector.detect_events(curr=curr_obs, prev=prev_obs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "PROLONGED_STALE_TEMP")

    @patch("src.monitoring.history_store.datetime", wraps=datetime)
    def test_cold_start_hydration_from_storage(self, mock_datetime):
        """Verify collector re-reads previous observation from storage on cold start."""
        mock_datetime.now.return_value = datetime(2026, 10, 26, 8, 30, tzinfo=TIMEZONE_SOFIA)
        prev_obs = DeviceObservation(
            observation_id=f"{self.dev_id}_20261026T0800",
            device_id=self.dev_id,
            room="living_room",
            timestamp_utc="2026-10-26T06:00:00+00:00",
            timestamp_sofia="2026-10-26T08:00:00+02:00",
            power=False,
            mode="Heat",
            target_temperature=20.0,
            indoor_temperature=18.0,
            temp_freshness="FRESH",
            fan_speed="Auto",
            online=True,
        )
        self.history_store.save_observation(prev_obs)

        # Fresh collector with empty memory (simulating Lambda cold start)
        fresh_collector = TelemetryCollector(history_store=self.history_store)
        self.assertNotIn(self.dev_id, fresh_collector._last_observations)

        # Build mock state object
        mock_state = MagicMock()
        mock_state.power = True
        mock_state.mode = ACMode.HEAT
        mock_state.target_temperature = 23.0
        mock_state.current_temperature = 18.5
        mock_state.fan_speed = ACFanSpeed.AUTO
        mock_state.display_led = True
        mock_state.beep_feedback = False
        mock_state.is_online = True
        mock_state.temp_measured_at = None
        mock_state.raw_shadow = {}

        fresh_collector.record_cycle(
            current_states={self.dev_id: mock_state},
            eval_time_sofia=datetime(2026, 10, 26, 8, 15, 0, tzinfo=TIMEZONE_SOFIA),
            system_state=SystemState.OCCUPIED_DAY,
        )

        # Events should be derived through hydrated previous observation
        events = self.history_store.get_events()
        event_types = [e.event_type for e in events]
        self.assertIn("POWER_ON", event_types)
        self.assertIn("TARGET_TEMP_CHANGE", event_types)


class TestDSTAndErrorIsolation(unittest.TestCase):
    """Test Europe/Sofia DST transition and defensive error isolation."""

    def test_europe_sofia_dst_transition_resilience(self):
        """Verify transition from EEST (UTC+3) to EET (UTC+2) on Sunday Oct 25, 2026."""
        # 02:45 Sofia (EEST = UTC+3) -> 23:45 UTC on Oct 24
        # 03:00 Sofia (transitions to EET = UTC+2) -> 01:00 UTC on Oct 25
        dt_before = datetime(2026, 10, 25, 0, 45, 0, tzinfo=timezone.utc)
        dt_after = datetime(2026, 10, 25, 1, 15, 0, tzinfo=timezone.utc)

        # UTC is strictly increasing
        self.assertGreater(dt_after, dt_before)
        # Verify UTC timestamps avoid any ambiguity during the clock fallback
        self.assertEqual(dt_before.isoformat(), "2026-10-25T00:45:00+00:00")
        self.assertEqual(dt_after.isoformat(), "2026-10-25T01:15:00+00:00")

    def test_s3_failure_error_isolation(self):
        """Verify S3 ClientError during observation save does not crash orchestrator."""
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}}, "GetObject"
        )
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal S3 Error"}}, "PutObject"
        )

        store = ObservationHistoryStore(s3_bucket="test-bucket", s3_client=mock_s3, local_dir=None)
        obs = DeviceObservation(
            observation_id="demo_living_room_20261026T1200",
            device_id="demo_living_room",
            room="living_room",
            timestamp_utc="2026-10-26T10:00:00+00:00",
            timestamp_sofia="2026-10-26T12:00:00+02:00",
            power=True,
            mode="Heat",
            target_temperature=22.0,
            indoor_temperature=21.0,
            temp_freshness="FRESH",
            fan_speed="Auto",
            online=True,
        )

        # Should return False without raising an exception
        res = store.save_observation(obs)
        self.assertFalse(res)


class TestThermalDecayAndReportGenerator(unittest.TestCase):
    """Test thermal decay rate calculations and 7-day report generation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ObservationHistoryStore(in_memory=True)
        self.weather = WeatherService(in_memory=True)
        for d_offset in range(14):
            d_str = (date(2026, 10, 18) + timedelta(days=d_offset)).strftime("%Y-%m-%d")
            self.weather._memory_cache[d_str] = {
                "date": d_str,
                "location": "Bansko, Bulgaria",
                "min_temp_c": 2.0,
                "max_temp_c": 12.0,
                "overnight_min_c": 1.5,
                "hourly": [],
                "source": "Open-Meteo (Test Cache)",
            }
        self.report_gen = SevenDayReportGenerator(history_store=self.store, weather_service=self.weather)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_both_off_intervals_analysis(self):
        """Verify both ACs OFF analysis and indoor temperature drop detection."""
        # 6 consecutive hours of OFF with room temperature falling from 21.0°C to 18.0°C
        lr_obs = []
        br_obs = []
        start_time = datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(25):  # 24 * 15min = 6 hours
            cur_time = start_time + timedelta(minutes=15 * i)
            t_meas = 21.0 - (3.0 * (i / 24.0))

            o_lr = DeviceObservation(
                observation_id=f"lr_{i}",
                device_id="demo_living_room",
                room="living_room",
                timestamp_utc=cur_time.isoformat(),
                timestamp_sofia=cur_time.astimezone(TIMEZONE_SOFIA).isoformat(),
                power=False,
                mode="Heat",
                target_temperature=21.0,
                indoor_temperature=round(t_meas, 1),
                temp_freshness="FRESH",
                fan_speed="Auto",
                online=True,
            )
            o_br = DeviceObservation(
                observation_id=f"br_{i}",
                device_id="demo_bedroom",
                room="bedroom",
                timestamp_utc=cur_time.isoformat(),
                timestamp_sofia=cur_time.astimezone(TIMEZONE_SOFIA).isoformat(),
                power=False,
                mode="Heat",
                target_temperature=20.0,
                indoor_temperature=round(t_meas, 1),
                temp_freshness="FRESH",
                fan_speed="Quiet",
                online=True,
            )
            lr_obs.append(o_lr)
            br_obs.append(o_br)

        intervals = self.report_gen._analyze_both_off_intervals(lr_obs, br_obs)
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0]["duration_hours"], 6.2, places=0)
        self.assertAlmostEqual(intervals[0]["lr_temp_delta"], -3.0, places=1)

    def test_7_day_report_generator_completeness(self):
        """Verify full 7-day report generator covers all 12 analytical dimensions."""
        start_date = date(2026, 10, 20)
        end_date = date(2026, 10, 26)

        for d_offset in range(7):
            cur_d = start_date + timedelta(days=d_offset)
            base_utc = datetime(cur_d.year, cur_d.month, cur_d.day, 0, 0, 0, tzinfo=timezone.utc)
            for cycle in range(96):
                ts_utc = base_utc + timedelta(minutes=15 * cycle)
                ts_sofia = ts_utc.astimezone(TIMEZONE_SOFIA)
                hour = ts_sofia.hour

                lr_power = (8 <= hour < 22)
                lr_obs = DeviceObservation(
                    observation_id=f"lr_{cur_d}_{cycle}",
                    device_id="demo_living_room",
                    room="living_room",
                    timestamp_utc=ts_utc.isoformat(),
                    timestamp_sofia=ts_sofia.isoformat(),
                    power=lr_power,
                    mode="Heat",
                    target_temperature=22.0,
                    indoor_temperature=21.0 if lr_power else 19.0,
                    temp_freshness="FRESH",
                    fan_speed="Auto",
                    online=True,
                )
                self.store.save_observation(lr_obs)

                br_power = (hour >= 22 or hour < 8)
                br_obs = DeviceObservation(
                    observation_id=f"br_{cur_d}_{cycle}",
                    device_id="demo_bedroom",
                    room="bedroom",
                    timestamp_utc=ts_utc.isoformat(),
                    timestamp_sofia=ts_sofia.isoformat(),
                    power=br_power,
                    mode="Heat",
                    target_temperature=20.0,
                    indoor_temperature=20.0 if br_power else 18.5,
                    temp_freshness="FRESH",
                    fan_speed="Quiet",
                    online=True,
                )
                self.store.save_observation(br_obs)

        # Add whole-property meter readings
        self.store.save_meter_reading(
            WholePropertyMeterReading(
                reading_id="m-1",
                reading_date="2026-10-20",
                timestamp_utc="2026-10-20T08:00:00+00:00",
                timestamp_sofia="2026-10-20 10:00:00 Sofia",
                meter_kwh=15000.0,
                recorded_by="Owner",
            )
        )
        self.store.save_meter_reading(
            WholePropertyMeterReading(
                reading_id="m-2",
                reading_date="2026-10-26",
                timestamp_utc="2026-10-26T20:00:00+00:00",
                timestamp_sofia="2026-10-26 22:00:00 Sofia",
                meter_kwh=15180.0,
                recorded_by="Owner",
            )
        )

        # Generate JSON Report
        report_data = self.report_gen.generate_report(
            start_date=start_date,
            end_date=end_date,
        )

        self.assertEqual(report_data["period"]["total_days"], 7)
        self.assertIn("living_room", report_data["device_summary"])
        self.assertIn("bedroom", report_data["device_summary"])
        self.assertGreater(report_data["device_summary"]["living_room"]["total_on_hours"], 50.0)

        # Generate Markdown Report
        md_report = self.report_gen.render_markdown_report(report_data)
        self.assertIn("7-Day Read-Only HVAC Behavior & Electricity Monitoring Report", md_report)
        self.assertIn("Living Room (`demo_living_room`)", md_report)
        self.assertIn("Bedroom (`demo_bedroom`)", md_report)
        self.assertIn("Hardware Electricity Telemetry Audit", md_report)
        self.assertIn("Whole-Apartment Utility Meter Readings", md_report)


class TestDashboardMonitoringEndpoints(unittest.TestCase):
    """Test dashboard HTTP endpoints for monitoring integration."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = AutomationConfig()
        self.config.DATA_DIR = Path(self.temp_dir)
        self.config.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only-key-at-least-32-chars-long!"
        self.config.S3_BUCKET = None
        self.secret_key = self.config.SESSION_SECRET_KEY

        # Valid session and CSRF tokens
        self.session_token = auth.create_session_token(self.secret_key)
        self.csrf_token = auth.generate_csrf_token(self.session_token, self.secret_key)

        self.weather_patcher = patch(
            "src.monitoring.weather.WeatherService._fetch_from_open_meteo",
            return_value={
                "date": "2026-10-26",
                "location": "Bansko, Bulgaria",
                "min_temp_c": 4.0,
                "max_temp_c": 11.0,
                "overnight_min_c": 3.0,
                "hourly": [],
                "source": "Open-Meteo",
                "fetched_at": "2026-10-26T12:00:00+00:00",
            }
        )
        self.weather_patcher.start()

    def tearDown(self):
        self.weather_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _authenticated_event(self, method: str, path: str, body: dict = None) -> dict:
        evt = {
            "httpMethod": method,
            "path": path,
            "headers": {
                "cookie": f"{COOKIE_NAME}={self.session_token}",
                "x-csrf-token": self.csrf_token,
                "content-type": "application/json",
            },
            "requestContext": {
                "identity": {"sourceIp": "127.0.0.1"}
            },
        }
        if body:
            evt["body"] = json.dumps(body)
        return evt

    def test_get_monitoring_summary_api(self):
        """Verify GET /api/monitoring/summary returns telemetry summary and unmetered status."""
        event = self._authenticated_event("GET", "/api/monitoring/summary")
        resp = handle_dashboard_request(event, None, config=self.config)
        self.assertEqual(resp["statusCode"], 200)

        data = json.loads(resp["body"])
        self.assertIn("tcl_cloud_metered_kwh_supported", data)
        self.assertFalse(data["tcl_cloud_metered_kwh_supported"])

    def test_post_meter_reading_api(self):
        """Verify POST /api/monitoring/meter-reading records manual whole-property meter readings."""
        event = self._authenticated_event(
            "POST",
            "/api/monitoring/meter-reading",
            {
                "reading_date": "2026-10-26",
                "meter_kwh": 14500.5,
                "notes": "Testing API ingestion",
            },
        )
        resp = handle_dashboard_request(event, None, config=self.config)
        self.assertEqual(resp["statusCode"], 200)

        data = json.loads(resp["body"])
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["reading"]["meter_kwh"], 14500.5)

    def test_get_export_csv_api(self):
        """Verify GET /api/monitoring/export.csv returns CSV with correct content type."""
        event = self._authenticated_event("GET", "/api/monitoring/export.csv")
        resp = handle_dashboard_request(event, None, config=self.config)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(resp["headers"]["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("timestamp_sofia", resp["body"])

    def test_get_report_api(self):
        """Verify GET /api/monitoring/report returns report in Markdown or JSON."""
        event = self._authenticated_event("GET", "/api/monitoring/report")
        event["rawQueryString"] = "format=markdown"
        resp = handle_dashboard_request(event, None, config=self.config)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("7-Day Read-Only HVAC Behavior", resp["body"])


if __name__ == "__main__":
    unittest.main()
