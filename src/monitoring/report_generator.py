"""Seven-Day Read-Only HVAC Observation and Analysis Report Generator."""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.monitoring.models import (
    DeviceObservation,
    OperationEvent,
    DailyDeviceSummary,
    WholePropertyMeterReading,
)
from src.monitoring.history_store import ObservationHistoryStore
from src.monitoring.weather import WeatherService

_LOGGER = logging.getLogger(__name__)


class SevenDayReportGenerator:
    """Generates comprehensive, exportable 7-day HVAC behavior and electricity consumption reports."""

    def __init__(
        self,
        history_store: Optional[ObservationHistoryStore] = None,
        weather_service: Optional[WeatherService] = None,
        config: Optional[AutomationConfig] = None,
    ):
        self.config = config or AutomationConfig()
        self.history_store = history_store or ObservationHistoryStore(
            s3_bucket=self.config.S3_BUCKET,
            local_dir=self.config.DATA_DIR / "history"
        )
        self.weather_service = weather_service or WeatherService(
            s3_bucket=self.config.S3_BUCKET,
            local_dir=self.config.DATA_DIR / "history" / "weather"
        )

    def generate_report(
        self,
        start_date: date,
        end_date: date,
        reservation_uid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate structured 7-day observation report data covering all 12 analytical dimensions.
        """
        lr_id = self.config.TCL_LIVING_ROOM_ID
        br_id = self.config.TCL_BEDROOM_ID

        # Collect raw observations
        lr_obs = self.history_store.get_observations(lr_id, start_date, end_date)
        br_obs = self.history_store.get_observations(br_id, start_date, end_date)

        # Collect events
        events = self.history_store.get_events(start_date=start_date, end_date=end_date, limit=500)

        # Collect meter readings
        meter_readings = self.history_store.get_meter_readings()
        period_meter_readings = [
            r for r in meter_readings
            if start_date.strftime("%Y-%m-%d") <= r.reading_date <= end_date.strftime("%Y-%m-%d")
        ]

        # Collect weather
        weather_days = self.weather_service.get_weather_summary_for_period(start_date, end_date)

        # 1. Daily Summaries
        days_span = (end_date - start_date).days + 1
        daily_lr: List[DailyDeviceSummary] = []
        daily_br: List[DailyDeviceSummary] = []

        curr = start_date
        while curr <= end_date:
            daily_lr.append(self.history_store.compute_daily_summary(lr_id, curr, "living_room"))
            daily_br.append(self.history_store.compute_daily_summary(br_id, curr, "bedroom"))
            curr += timedelta(days=1)

        # Total ON/OFF events
        lr_on_events = sum(d.power_on_events for d in daily_lr)
        lr_off_events = sum(d.power_off_events for d in daily_lr)
        br_on_events = sum(d.power_on_events for d in daily_br)
        br_off_events = sum(d.power_off_events for d in daily_br)

        # Total ON hours
        lr_total_on_hours = sum(d.approx_on_hours for d in daily_lr)
        br_total_on_hours = sum(d.approx_on_hours for d in daily_br)
        lr_avg_daily_on_hours = round(lr_total_on_hours / max(1, days_span), 1)
        br_avg_daily_on_hours = round(br_total_on_hours / max(1, days_span), 1)

        # Setpoint preferences aggregation
        lr_setpoints: Dict[str, int] = {}
        br_setpoints: Dict[str, int] = {}
        for d in daily_lr:
            for k, v in d.setpoint_distribution.items():
                lr_setpoints[k] = lr_setpoints.get(k, 0) + v
        for d in daily_br:
            for k, v in d.setpoint_distribution.items():
                br_setpoints[k] = br_setpoints.get(k, 0) + v

        # Weekday vs Weekend split
        weekday_on_hours = 0.0
        weekend_on_hours = 0.0
        weekday_days = 0
        weekend_days = 0

        curr = start_date
        for dlr, dbr in zip(daily_lr, daily_br):
            combined_on = dlr.approx_on_hours + dbr.approx_on_hours
            if curr.weekday() >= 5:  # Saturday or Sunday
                weekend_on_hours += combined_on
                weekend_days += 1
            else:
                weekday_on_hours += combined_on
                weekday_days += 1
            curr += timedelta(days=1)

        # Both devices OFF periods analysis & thermal decay
        both_off_intervals = self._analyze_both_off_intervals(lr_obs, br_obs)

        # Prolonged heating analysis (sessions >= 6 hours continuous ON)
        prolonged_heating = self._analyze_prolonged_heating(lr_obs, br_obs)

        # Comfort and low temperature risks
        comfort_risks = self._analyze_comfort_risks(lr_obs, br_obs)

        # Potential energy saving opportunities (conservative, read-only observations)
        opportunities = self._identify_saving_opportunities(daily_lr, daily_br, weather_days)

        report_data = {
            "period": {
                "start_date": start_date.strftime("%Y-%m-%d"),
                "end_date": end_date.strftime("%Y-%m-%d"),
                "total_days": days_span,
                "reservation_uid": reservation_uid or "Active Reservation",
            },
            "device_summary": {
                "living_room": {
                    "device_id": lr_id,
                    "total_on_hours": round(lr_total_on_hours, 1),
                    "avg_daily_on_hours": lr_avg_daily_on_hours,
                    "on_events": lr_on_events,
                    "off_events": lr_off_events,
                    "preferred_setpoints": sorted(lr_setpoints.items(), key=lambda x: x[1], reverse=True),
                    "daily": [d.to_dict() for d in daily_lr]
                },
                "bedroom": {
                    "device_id": br_id,
                    "total_on_hours": round(br_total_on_hours, 1),
                    "avg_daily_on_hours": br_avg_daily_on_hours,
                    "on_events": br_on_events,
                    "off_events": br_off_events,
                    "preferred_setpoints": sorted(br_setpoints.items(), key=lambda x: x[1], reverse=True),
                    "daily": [d.to_dict() for d in daily_br]
                }
            },
            "weekday_vs_weekend": {
                "weekday_avg_combined_on_hours": round(weekday_on_hours / max(1, weekday_days), 1),
                "weekend_avg_combined_on_hours": round(weekend_on_hours / max(1, weekend_days), 1),
                "weekday_days": weekday_days,
                "weekend_days": weekend_days,
            },
            "both_devices_off_intervals": both_off_intervals,
            "prolonged_heating_sessions": prolonged_heating,
            "comfort_and_frost_risks": comfort_risks,
            "energy_saving_opportunities": opportunities,
            "weather": weather_days,
            "electricity_metering": {
                "tcl_cloud_metered_kwh_available": False,
                "metered_status_note": (
                    "TCL Cloud hardware does not provide instantaneous power or cumulative energy fields. "
                    "All device-level kWh values are marked UNAVAILABLE. Converting inverter frequency to kWh "
                    "is unverified and speculative."
                ),
                "whole_property_meter_readings": [r.to_dict() for r in period_meter_readings],
            },
            "data_quality_and_confidence": {
                "total_expected_samples_per_device": days_span * 96,
                "lr_actual_samples": len(lr_obs),
                "br_actual_samples": len(br_obs),
                "lr_stale_samples": sum(d.stale_temp_samples for d in daily_lr),
                "br_stale_samples": sum(d.stale_temp_samples for d in daily_br),
                "confidence_limitations": [
                    "TCL Cloud does not report electrical power (W) or energy (kWh).",
                    "Device thermistors stagnate when units are powered OFF (fan inactive), causing prolonged stale readings.",
                    "15-minute polling resolution provides upper-bound estimates of operation transition times unless shadow metadata timestamps are available.",
                ]
            }
        }

        return report_data

    def _analyze_both_off_intervals(
        self,
        lr_obs: List[DeviceObservation],
        br_obs: List[DeviceObservation]
    ) -> List[Dict[str, Any]]:
        """Identify periods when both units were simultaneously OFF and calculate room thermal decay."""
        # Index bedroom by observation timestamp
        br_map = {o.timestamp_utc: o for o in br_obs}
        intervals = []
        current_interval = None

        for o_lr in lr_obs:
            o_br = br_map.get(o_lr.timestamp_utc)
            both_off = (not o_lr.power) and (o_br is not None and not o_br.power)

            if both_off:
                if current_interval is None:
                    current_interval = {
                        "start_sofia": o_lr.timestamp_sofia,
                        "end_sofia": o_lr.timestamp_sofia,
                        "samples": 1,
                        "lr_start_temp": o_lr.indoor_temperature,
                        "lr_end_temp": o_lr.indoor_temperature,
                        "br_start_temp": o_br.indoor_temperature if o_br else None,
                        "br_end_temp": o_br.indoor_temperature if o_br else None,
                    }
                else:
                    current_interval["end_sofia"] = o_lr.timestamp_sofia
                    current_interval["samples"] += 1
                    current_interval["lr_end_temp"] = o_lr.indoor_temperature
                    if o_br and o_br.indoor_temperature is not None:
                        current_interval["br_end_temp"] = o_br.indoor_temperature
            else:
                if current_interval is not None:
                    duration_h = round(current_interval["samples"] * 0.25, 1)
                    if duration_h >= 1.0:  # Only record periods of at least 1 hour
                        current_interval["duration_hours"] = duration_h
                        # Compute thermal drop
                        if current_interval["lr_start_temp"] and current_interval["lr_end_temp"]:
                            current_interval["lr_temp_delta"] = round(current_interval["lr_end_temp"] - current_interval["lr_start_temp"], 1)
                        intervals.append(current_interval)
                    current_interval = None

        if current_interval is not None and (current_interval["samples"] * 0.25) >= 1.0:
            current_interval["duration_hours"] = round(current_interval["samples"] * 0.25, 1)
            if current_interval.get("lr_start_temp") is not None and current_interval.get("lr_end_temp") is not None:
                current_interval["lr_temp_delta"] = round(current_interval["lr_end_temp"] - current_interval["lr_start_temp"], 1)
            intervals.append(current_interval)

        return intervals

    def _analyze_prolonged_heating(
        self,
        lr_obs: List[DeviceObservation],
        br_obs: List[DeviceObservation]
    ) -> List[Dict[str, Any]]:
        """Identify continuous ON sessions of >= 6 hours."""
        sessions = []
        for name, obs_list in (("Living Room", lr_obs), ("Bedroom", br_obs)):
            current_session = None
            for obs in obs_list:
                if obs.power:
                    if current_session is None:
                        current_session = {
                            "room": name,
                            "start_sofia": obs.timestamp_sofia,
                            "end_sofia": obs.timestamp_sofia,
                            "samples": 1,
                            "target_temp": obs.target_temperature,
                        }
                    else:
                        current_session["end_sofia"] = obs.timestamp_sofia
                        current_session["samples"] += 1
                else:
                    if current_session:
                        dur_h = round(current_session["samples"] * 0.25, 1)
                        if dur_h >= 6.0:
                            current_session["duration_hours"] = dur_h
                            sessions.append(current_session)
                        current_session = None
            if current_session and (current_session["samples"] * 0.25) >= 6.0:
                current_session["duration_hours"] = round(current_session["samples"] * 0.25, 1)
                sessions.append(current_session)

        return sessions

    def _analyze_comfort_risks(
        self,
        lr_obs: List[DeviceObservation],
        br_obs: List[DeviceObservation]
    ) -> List[Dict[str, Any]]:
        """Identify instances where indoor temperature dipped dangerously or comfort was impaired."""
        risks = []
        for name, obs_list in (("Living Room", lr_obs), ("Bedroom", br_obs)):
            for o in obs_list:
                if o.indoor_temperature is not None and o.indoor_temperature <= 16.0:
                    risks.append({
                        "room": name,
                        "timestamp_sofia": o.timestamp_sofia,
                        "indoor_temp": o.indoor_temperature,
                        "power": o.power,
                        "risk_level": "CRITICAL" if o.indoor_temperature <= 8.0 else ("ELEVATED" if o.indoor_temperature <= 12.0 else "WARNING"),
                        "description": (
                            f"{name} measured {o.indoor_temperature}°C while unit was {'ON' if o.power else 'OFF'}."
                        )
                    })
        return risks[:20]  # Cap at top 20 events

    def _identify_saving_opportunities(
        self,
        daily_lr: List[DailyDeviceSummary],
        daily_br: List[DailyDeviceSummary],
        weather: List[Dict[str, Any]]
    ) -> List[str]:
        """Formulate conservative energy optimization opportunities for owner review."""
        opps = []
        # Check high night heating
        total_on_hours = sum(d.approx_on_hours for d in daily_lr) + sum(d.approx_on_hours for d in daily_br)
        if total_on_hours > 150:
            opps.append("High overall AC runtime (> 150 combined hours). Consider evaluating thermal insulation and window draughts.")

        # Check setpoints > 24°C
        high_setpoints = False
        for d in daily_lr + daily_br:
            for sp, count in d.setpoint_distribution.items():
                try:
                    val = float(sp.replace("°C", ""))
                    if val >= 25.0 and count > 10:
                        high_setpoints = True
                except ValueError:
                    pass

        if high_setpoints:
            opps.append(
                "Frequent high target setpoints (>= 25°C) observed. Recommending a soft ceiling of 24°C "
                "or gradual diurnal staging to reduce electrical consumption while maintaining guest warmth."
            )

        opps.append(
            "Observe for full 7 days before modifying automated schedules. "
            "Ensure that persistent power-off overrides by guests are honored to avoid wasting heat in unneeded rooms."
        )

        return opps

    def render_markdown_report(self, report_data: Dict[str, Any]) -> str:
        """Render report as human-readable Markdown for export or dashboard display."""
        p = report_data["period"]
        ds = report_data["device_summary"]
        lr = ds["living_room"]
        br = ds["bedroom"]
        ww = report_data["weekday_vs_weekend"]
        dq = report_data["data_quality_and_confidence"]

        md = []
        md.append(f"# 7-Day Read-Only HVAC Behavior & Electricity Monitoring Report")
        md.append(f"**Property:** Airbnb Apartment, Bansko, Bulgaria")
        md.append(f"**Observation Period:** {p['start_date']} to {p['end_date']} ({p['total_days']} complete days)")
        md.append(f"**Reservation Reference:** {p['reservation_uid']}")
        md.append(f"**Operational Safety:** Autonomous Actuation Disabled (`TCL_DRY_RUN=true`, `TCL_READ_ONLY=true`)\n")

        md.append("## 1. Executive Summary & Operation Overview")
        md.append("| Metric | Living Room (`demo_living_room`) | Bedroom (`demo_bedroom`) |")
        md.append("| :--- | :---: | :---: |")
        md.append(f"| Total Estimated ON Hours | **{lr['total_on_hours']} h** | **{br['total_on_hours']} h** |")
        md.append(f"| Daily Average ON Hours | {lr['avg_daily_on_hours']} h/day | {br['avg_daily_on_hours']} h/day |")
        md.append(f"| Observed Power ON Transitions | {lr['on_events']} events | {br['on_events']} events |")
        md.append(f"| Observed Power OFF Transitions | {lr['off_events']} events | {br['off_events']} events |\n")

        md.append("## 2. Guest Preferred Setpoints")
        md.append("- **Living Room:** " + (", ".join([f"{k} ({v} samples)" for k, v in lr['preferred_setpoints'][:3]]) or "None recorded"))
        md.append("- **Bedroom:** " + (", ".join([f"{k} ({v} samples)" for k, v in br['preferred_setpoints'][:3]]) or "None recorded") + "\n")

        md.append("## 3. Weekday vs. Weekend Heating Comparison")
        md.append(f"- **Weekday Combined Average:** {ww['weekday_avg_combined_on_hours']} hours/day ({ww['weekday_days']} days)")
        md.append(f"- **Weekend Combined Average:** {ww['weekend_avg_combined_on_hours']} hours/day ({ww['weekend_days']} days)\n")

        md.append("## 4. Hardware Electricity Telemetry Audit & Consumption")
        md.append("> **CRITICAL TRANSPARENCY REQUIREMENT:**")
        md.append(f"> {report_data['electricity_metering']['metered_status_note']}\n")

        meter_list = report_data["electricity_metering"]["whole_property_meter_readings"]
        if meter_list:
            md.append("### Recorded Whole-Apartment Utility Meter Readings:")
            md.append("| Date | Meter Reading (kWh) | Daily Delta (kWh) | Notes |")
            md.append("| :--- | :---: | :---: | :--- |")
            for r in meter_list:
                delta_str = f"{r.get('daily_consumption_kwh')} kWh" if r.get('daily_consumption_kwh') is not None else "Baseline"
                md.append(f"| {r.get('reading_date')} | {r.get('meter_kwh'):.1f} | {delta_str} | {r.get('notes') or 'N/A'} |")
            md.append("\n*Note: Whole-property readings include water heater, kitchen appliances, and lighting in addition to both ACs.*\n")
        else:
            md.append("*No manual whole-property meter readings were logged during this period.*\n")

        md.append("## 5. Thermal Decay & Inoccupied Periods (Both Units OFF)")
        both_off = report_data.get("both_devices_off_intervals", [])
        if both_off:
            md.append(f"Detected **{len(both_off)}** period(s) where both AC units were turned off simultaneously for >= 1 hour:")
            for bo in both_off[:5]:
                md.append(f"- From {bo['start_sofia']} to {bo['end_sofia']} ({bo['duration_hours']}h): Living room indoor temperature moved from {bo['lr_start_temp']}°C to {bo['lr_end_temp']}°C.")
            md.append("")
        else:
            md.append("*No simultaneous dual-unit OFF periods >= 1.0h observed during this period.*\n")

        md.append("## 6. Comfort & Low-Temperature Safety Risks")
        risks = report_data.get("comfort_and_frost_risks", [])
        if risks:
            md.append(f"Identified **{len(risks)}** temperature drop event(s) <= 16.0°C:")
            for r in risks[:5]:
                md.append(f"- [{r['risk_level']}] {r['description']} at {r['timestamp_sofia']}")
            md.append("")
        else:
            md.append("No indoor temperatures below 16.0°C recorded during this observation period.\n")

        md.append("## 7. Data Quality & Limitations")
        for lim in dq["confidence_limitations"]:
            md.append(f"- {lim}")
        md.append("")

        md.append("## 8. Energy Optimization Opportunities (For Owner Review Only)")
        for opp in report_data.get("energy_saving_opportunities", []):
            md.append(f"- {opp}")
        md.append("\n*Strict Safety Constraint: Autonomous heating optimizations will NOT be deployed until at least 7 complete days of observations are validated by the owner.*")

        return "\n".join(md)
