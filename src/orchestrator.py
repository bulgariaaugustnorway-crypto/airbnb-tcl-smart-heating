"""Central automation orchestrator coordinating calendar, state machine, safeguards, and controllers."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from src.models import (
    Reservation,
    ReservationType,
    ScheduleDecision,
    SystemState,
    ACCommand,
    ACState,
    ACMode,
    ACFanSpeed,
    OwnerTestSession,
    mask_uid,
)
from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.calendar_parser import AirbnbCalendarParser, CalendarSyncResult
from src.scheduler import HVACScheduleEngine
from src.safeguards import SafeguardEngine
from src.device_controller import BaseACController, MockACController
from src.notifier import NotificationDispatcher
from src.state_store import StateStore, ConcurrencyConflictError

_LOGGER = logging.getLogger(__name__)


@dataclass
class CycleResult:
    """Summary of actions taken during a single execution cycle."""
    timestamp: datetime
    system_state: SystemState
    active_reservation: Optional[Reservation]
    decision_reason: str
    safeguard_decisions: Dict[str, Tuple[bool, Optional[ACCommand], str]]
    commands_sent: Dict[str, bool]
    cancelled_uids: List[str]
    in_grace_period_uids: List[str] = field(default_factory=list)
    dry_run: bool = True
    calendar_stale: bool = False


class AutomationOrchestrator:
    """End-to-end coordinator for the TCL AC Airbnb automation."""

    def __init__(
        self,
        config: Optional[AutomationConfig] = None,
        controller: Optional[BaseACController] = None,
        notifier: Optional[NotificationDispatcher] = None,
        calendar_parser: Optional[AirbnbCalendarParser] = None,
        schedule_engine: Optional[HVACScheduleEngine] = None,
        safeguard_engine: Optional[SafeguardEngine] = None,
        state_store: Optional[StateStore] = None,
        collector: Optional[Any] = None,
    ):
        self.config = config or AutomationConfig()
        self.controller = controller or MockACController(device_ids=self.config.DEVICE_IDS)
        self.notifier = notifier or NotificationDispatcher(config=self.config)
        self.calendar_parser = calendar_parser or AirbnbCalendarParser()
        self.schedule_engine = schedule_engine or HVACScheduleEngine(config=self.config)
        self.safeguard_engine = safeguard_engine or SafeguardEngine(config=self.config)
        self.state_store = state_store or StateStore()

        if collector is not None:
            self.collector = collector
        elif getattr(self.config, "MONITORING_ENABLED", True):
            try:
                from src.monitoring.collector import TelemetryCollector
                from src.monitoring.history_store import ObservationHistoryStore
                h_store = ObservationHistoryStore(
                    s3_bucket=self.config.S3_BUCKET,
                    local_dir=self.config.DATA_DIR / "history"
                )
                self.collector = TelemetryCollector(history_store=h_store, config=self.config)
            except Exception as e:
                _LOGGER.warning(f"Could not initialize telemetry collector: {e}")
                self.collector = None
        else:
            self.collector = None

        self.last_system_state: Optional[SystemState] = None
        self.known_confirmed_uids: set = set()
        self.known_reservations_cache: Dict[str, Reservation] = {}
        self.active_grace_periods: Dict[str, Dict[str, Any]] = {}
        self.was_calendar_stale: bool = False
        self.owner_test_session: Optional[OwnerTestSession] = None
        self.last_device_states: Dict[str, Any] = {}
        self.last_decision_reason: str = ""
        self.last_completed_transitions: Dict[str, str] = {}
        self._last_frost_alerts: Dict[str, Dict[str, Any]] = {}
        self._last_economic_alerts: Dict[str, Dict[str, Any]] = {}
        self._last_stale_alerts: Dict[str, Dict[str, Any]] = {}

        # Restore state on initialization
        self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        """Hydrate runtime state from persistent storage."""
        try:
            state = self.state_store.load_state()
            if not state:
                return

            if "last_system_state" in state and state["last_system_state"]:
                try:
                    self.last_system_state = SystemState(state["last_system_state"])
                except ValueError:
                    pass

            if "known_confirmed_uids" in state:
                self.known_confirmed_uids = set(state["known_confirmed_uids"])

            if "known_reservations" in state:
                for uid, rdict in state["known_reservations"].items():
                    try:
                        self.known_reservations_cache[uid] = Reservation(
                            uid=rdict["uid"],
                            start_date=date.fromisoformat(rdict["start_date"]),
                            end_date=date.fromisoformat(rdict["end_date"]),
                            summary=rdict.get("summary", ""),
                            reservation_type=ReservationType(rdict["type"]),
                            description=rdict.get("description", ""),
                            raw_status=rdict.get("raw_status", "CONFIRMED")
                        )
                    except Exception:
                        pass

            if "active_grace_periods" in state:
                self.active_grace_periods = state["active_grace_periods"]

            if "was_calendar_stale" in state:
                self.was_calendar_stale = bool(state.get("was_calendar_stale", False))

            if "owner_test" in state and state["owner_test"]:
                odata = state["owner_test"]
                try:
                    self.owner_test_session = OwnerTestSession(
                        active=bool(odata.get("active", False)),
                        started_at=datetime.fromisoformat(odata["started_at"]) if odata.get("started_at") else None,
                        expires_at=datetime.fromisoformat(odata["expires_at"]) if odata.get("expires_at") else None,
                        duration_hours=float(odata.get("duration_hours", 2.0)),
                        target_temperature=float(odata.get("target_temperature", 22.0)),
                        mode=ACMode(odata.get("mode", "Heat")),
                        fan_speed=ACFanSpeed(odata.get("fan_speed", "Auto")),
                        display_led=bool(odata.get("display_led", False)),
                        living_room_power=bool(odata.get("living_room_power", True)),
                        bedroom_power=bool(odata.get("bedroom_power", True)),
                        created_by=str(odata.get("created_by", "owner")),
                        notes=str(odata.get("notes", ""))
                    )
                except Exception as e:
                    _LOGGER.warning(f"Could not load owner test session from state: {e}")

            # Restore safeguard power toggles
            if "last_power_toggle" in state:
                toggles = {}
                for k, v in state["last_power_toggle"].items():
                    try:
                        toggles[k] = datetime.fromisoformat(v)
                    except Exception:
                        pass
                self.safeguard_engine._last_power_toggle = toggles

            # Restore safeguard last commands
            if "last_applied_commands" in state:
                cmds = {}
                for dev_id, cdict in state["last_applied_commands"].items():
                    try:
                        cmds[dev_id] = ACCommand(
                            device_id=cdict["device_id"],
                            power=cdict["power"],
                            target_temperature=cdict["target_temperature"],
                            mode=ACMode(cdict["mode"]),
                            fan_speed=ACFanSpeed(cdict["fan_speed"]),
                            eco_mode=cdict.get("eco_mode", False),
                            display_led=bool(cdict.get("display_led", False)),
                            beep_feedback=bool(cdict.get("beep_feedback", False)),
                            reason=cdict.get("reason", "")
                        )
                    except Exception:
                        pass
                self.safeguard_engine._last_applied_command = cmds

            # Restore guest manual overrides and adaptive preferences
            if "guest_manual_overrides" in state and isinstance(state["guest_manual_overrides"], dict):
                self.safeguard_engine.set_manual_overrides(state["guest_manual_overrides"])

            if "preferred_day_temperatures" in state and isinstance(state["preferred_day_temperatures"], dict):
                self.safeguard_engine.set_preferred_day_temperatures(state["preferred_day_temperatures"])

            if "last_completed_transitions" in state and isinstance(state["last_completed_transitions"], dict):
                self.last_completed_transitions = state["last_completed_transitions"]

            if "last_frost_alerts" in state and isinstance(state["last_frost_alerts"], dict):
                self._last_frost_alerts = state["last_frost_alerts"]

            if "last_economic_alerts" in state and isinstance(state["last_economic_alerts"], dict):
                self._last_economic_alerts = state["last_economic_alerts"]

            if "last_stale_alerts" in state and isinstance(state["last_stale_alerts"], dict):
                self._last_stale_alerts = state["last_stale_alerts"]

            if "notification_delivery_state" in state and isinstance(state["notification_delivery_state"], dict):
                self.notifier.set_delivery_state(state["notification_delivery_state"])

            if "initialized_reservation_uid" in state:
                self.safeguard_engine.initialized_reservation_uid = state.get("initialized_reservation_uid")

            if "has_live_command_history" in state:
                self.safeguard_engine.has_live_command_history = bool(state.get("has_live_command_history", False))

            _LOGGER.debug(f"Hydrated state from state store: {len(self.known_confirmed_uids)} known UIDs, {len(self.active_grace_periods)} grace periods")
        except Exception as e:
            _LOGGER.warning(f"Error restoring persisted state: {e}")

    def _save_persisted_state(self) -> None:
        """Persist runtime state for next cycle / AWS Lambda invocation."""
        try:
            state_data = {
                "last_system_state": self.last_system_state.value if self.last_system_state else None,
                "known_confirmed_uids": list(self.known_confirmed_uids),
                "known_reservations": {
                    uid: r.to_dict() for uid, r in self.known_reservations_cache.items()
                },
                "active_grace_periods": self.active_grace_periods,
                "was_calendar_stale": self.was_calendar_stale,
                "owner_test": self.owner_test_session.to_dict() if self.owner_test_session and self.owner_test_session.is_active() else None,
                "guest_manual_overrides": self.safeguard_engine.get_manual_overrides(),
                "preferred_day_temperatures": self.safeguard_engine.get_preferred_day_temperatures(),
                "last_completed_transitions": self.last_completed_transitions,
                "last_frost_alerts": self._last_frost_alerts,
                "last_economic_alerts": self._last_economic_alerts,
                "last_stale_alerts": self._last_stale_alerts,
                "notification_delivery_state": self.notifier.get_delivery_state(),
                "initialized_reservation_uid": self.safeguard_engine.initialized_reservation_uid,
                "has_live_command_history": self.safeguard_engine.has_live_command_history,
                "last_power_toggle": {
                    k: v.isoformat() for k, v in self.safeguard_engine._last_power_toggle.items()
                },
                "last_applied_commands": {
                    dev_id: {
                        "device_id": cmd.device_id,
                        "power": cmd.power,
                        "target_temperature": cmd.target_temperature,
                        "mode": cmd.mode.value if cmd.mode else "Heat",
                        "fan_speed": cmd.fan_speed.value if cmd.fan_speed else "Auto",
                        "eco_mode": cmd.eco_mode,
                        "display_led": cmd.display_led,
                        "beep_feedback": cmd.beep_feedback,
                        "reason": cmd.reason,
                    }
                    for dev_id, cmd in self.safeguard_engine._last_applied_command.items()
                },
                "device_states": {
                    dev_id: {
                        "power": s.power,
                        "target_temperature": s.target_temperature,
                        "current_temperature": s.current_temperature,
                        "temp_measured_at": s.temp_measured_at.isoformat() if getattr(s, "temp_measured_at", None) else None,
                        "mode": s.mode.value if s.mode else "Heat",
                        "fan_speed": s.fan_speed.value if s.fan_speed else "Auto",
                        "display_led": getattr(s, "display_led", False),
                        "beep_feedback": getattr(s, "beep_feedback", False),
                        "online": getattr(s, "is_online", True)
                    }
                    for dev_id, s in getattr(self, "last_device_states", {}).items()
                },
                "decision_reason": self.last_decision_reason,
                "updated_at": datetime.now().isoformat(),
            }
            self.state_store.save_state(state_data)
        except ConcurrencyConflictError as cce:
            _LOGGER.critical(
                f"ABORTED STATE OVERWRITE: S3/Memory concurrency conflict detected: {cce}. "
                f"Preserving existing valid overrides."
            )
            raise
        except Exception as e:
            _LOGGER.warning(f"Error saving persisted state: {e}")


    def _is_in_active_confirmed_guest_stay(self, dt: datetime) -> bool:
        """Defense-in-depth check: returns True if trustworthy snapshot contains an active confirmed guest."""
        today = dt.date()
        curr_time = dt.time()
        pre_arrival_start = (
            datetime.combine(today, self.config.CHECKIN_TIME)
            - timedelta(hours=self.config.PRE_ARRIVAL_HOURS)
        ).time()
        for r in getattr(self, "known_reservations_cache", {}).values():
            if r.is_confirmed:
                in_stay = (
                    (r.start_date < today < r.end_date)
                    or (r.start_date == today and curr_time >= pre_arrival_start)
                    or (r.end_date == today and curr_time < self.config.CHECKOUT_TIME)
                )
                if in_stay:
                    return True
        return False

    def run_cycle(
        self,
        current_dt: Optional[datetime] = None,
        calendar_url: Optional[str] = None,
        calendar_file: Optional[Path] = None,
        override_reservations: Optional[List[Reservation]] = None,
        owner_test: Optional[OwnerTestSession] = None,
        reload_config: bool = False
    ) -> CycleResult:
        """
        Execute a complete automation tick.
        Can run with live calendar, fallback file, or mocked reservation objects.
        """
        sofia_now = self.schedule_engine.to_sofia_time(current_dt)
        today = sofia_now.date()
        is_calendar_stale = False
        in_grace_period_uids = []

        # Hydrate latest dynamic settings from config store if explicitly requested
        if reload_config:
            try:
                from src.dashboard.config_store import DashboardConfigStore
                self.config = DashboardConfigStore(self.config).apply_to_config(self.config)
                self.schedule_engine.config = self.config
            except Exception as e:
                _LOGGER.debug(f"Could not re-hydrate config from store: {e}")

        # -------------------------------------------------------------
        # 1. Sync and parse calendar
        # -------------------------------------------------------------
        if override_reservations is not None:
            reservations = override_reservations
            cancelled_uids = []
            for r in reservations:
                self.known_reservations_cache[r.uid] = r
        else:
            sync_res: CalendarSyncResult = self.calendar_parser.sync_calendar(
                url=calendar_url or self.config.AIRBNB_ICAL_URL,
                fallback_file=calendar_file,
                known_previous_uids=self.known_confirmed_uids
            )
            reservations = sync_res.reservations
            cancelled_uids = sync_res.cancelled_uids
            is_calendar_stale = sync_res.is_stale

            if is_calendar_stale:
                self.notifier.notify_stale_calendar(
                    sync_res.error_message or "Calendar feed is stale beyond safe threshold."
                )
                self.was_calendar_stale = True
            elif self.was_calendar_stale:
                self.notifier.notify_calendar_recovered(
                    "Calendar feed restored successfully after stale period. Resuming normal scheduling."
                )
                self.was_calendar_stale = False
            elif sync_res.error_message:
                self.notifier.notify_calendar_warning(sync_res.error_message)

            # Update cache of known reservations
            for r in reservations:
                self.known_reservations_cache[r.uid] = r

            # Check for newly disappeared reservations
            for uid in cancelled_uids:
                if uid in self.active_grace_periods:
                    continue  # Already in an active grace period

                cached_res = self.known_reservations_cache.get(uid)
                is_in_progress = cached_res and (cached_res.start_date <= today <= cached_res.end_date)

                if is_in_progress:
                    # In-progress stay disappeared! Activate Grace Period
                    grace_until = sofia_now + timedelta(hours=self.config.CANCELLATION_GRACE_PERIOD_HOURS)
                    self.active_grace_periods[uid] = {
                        "disappeared_at": sofia_now.isoformat(),
                        "grace_until": grace_until.isoformat(),
                        "reservation": {
                            "uid": cached_res.uid,
                            "start_date": cached_res.start_date.isoformat(),
                            "end_date": cached_res.end_date.isoformat(),
                            "summary": cached_res.summary,
                            "type": cached_res.reservation_type.value,
                            "raw_status": cached_res.raw_status,
                        }
                    }
                    self.notifier.notify_grace_period(
                        uid=uid,
                        grace_hours=self.config.CANCELLATION_GRACE_PERIOD_HOURS,
                        grace_until=grace_until.strftime("%H:%M on %Y-%m-%d"),
                        metadata={"disappeared_at": sofia_now.isoformat()}
                    )
                else:
                    # Future booking cancelled normally
                    self.notifier.notify_cancellation(uid)

            # Process active grace periods: check if resolved or expired
            current_feed_uids = {r.uid for r in reservations if r.is_confirmed and r.raw_status != "GRACE_PERIOD"}
            in_grace_period_uids = []
            expired_grace_uids = []

            for uid, ginfo in list(self.active_grace_periods.items()):
                if uid in current_feed_uids:
                    # Reservation reappeared! Glitch resolved
                    _LOGGER.info(f"Reservation {mask_uid(uid)} restored in Airbnb calendar feed. Grace period cleared.")
                    del self.active_grace_periods[uid]
                else:
                    # Check expiry
                    expiry_dt = datetime.fromisoformat(ginfo["grace_until"])
                    if sofia_now > expiry_dt:
                        expired_grace_uids.append(uid)
                    else:
                        # Grace period active! Inject temporary reservation to protect guest
                        in_grace_period_uids.append(uid)
                        rdata = ginfo["reservation"]
                        res_type = (
                            ReservationType(rdata.get("type", ReservationType.CONFIRMED.value))
                            if "type" in rdata
                            else ReservationType.CONFIRMED
                        )
                        grace_res = Reservation(
                            uid=rdata["uid"],
                            start_date=date.fromisoformat(rdata["start_date"]),
                            end_date=date.fromisoformat(rdata["end_date"]),
                            summary=rdata["summary"],
                            reservation_type=res_type,
                            raw_status="GRACE_PERIOD"
                        )
                        # Re-inject so scheduler maintains comfort heating
                        if not any(r.uid == uid for r in reservations):
                            reservations.append(grace_res)

            for uid in expired_grace_uids:
                _LOGGER.warning(f"Grace period expired for reservation {mask_uid(uid)}. Transitioning to unoccupied.")
                self.notifier.notify_cancellation(uid, metadata={"grace_period_expired": True})
                del self.active_grace_periods[uid]
                self.known_reservations_cache.pop(uid, None)

            self.known_confirmed_uids = {r.uid for r in reservations if r.is_confirmed and r.raw_status != "GRACE_PERIOD"}

        # Automatically purge expired reservation profiles
        try:
            from src.dashboard.config_store import DashboardConfigStore
            DashboardConfigStore(self.config).cleanup_expired_profiles(sofia_now, reservations)
        except Exception as e:
            _LOGGER.debug(f"Could not cleanup expired reservation profiles: {e}")

        # -------------------------------------------------------------
        # 2. Evaluate scheduling state machine
        # -------------------------------------------------------------
        # Stale Calendar Safe Fallback:
        # Never shut down heating during an established in-stay reservation solely because feed is stale.
        # Preserve active in-stay reservation until scheduled checkout (11:00 on checkout date).
        # Suppress unverified future arrivals to avoid heating if cancelled.
        if is_calendar_stale:
            in_stay_candidates = []
            candidates_pool = reservations if reservations else list(self.known_reservations_cache.values())
            for r in candidates_pool:
                if r.is_occupancy_eligible(self.config.HEAT_DURING_BLOCKED_DATES) and (
                    (r.start_date <= today < r.end_date)
                    or (today == r.end_date and sofia_now.time() < self.config.CHECKOUT_TIME)
                ):
                    in_stay_candidates.append(r)

            if in_stay_candidates:
                _LOGGER.warning(
                    f"Stale calendar feed detected, but preserved {len(in_stay_candidates)} active in-stay reservation(s) "
                    f"({[r.masked_uid for r in in_stay_candidates]}) until scheduled checkout to protect guest comfort."
                )
                effective_reservations = in_stay_candidates
            else:
                _LOGGER.warning("Enforcing safe unoccupied state: calendar is stale and no established active in-stay guest found.")
                effective_reservations = []
        else:
            effective_reservations = reservations

        active_owner_test = owner_test or self.owner_test_session
        if active_owner_test and not active_owner_test.is_active(sofia_now):
            _LOGGER.info("Active owner test session has expired. Resuming normal scheduling.")
            self.owner_test_session = None
            active_owner_test = None

        # Collect recorded guest preferred day temperatures
        preferred_day_temps = {}
        for dev_id in self.config.DEVICE_IDS:
            pt = self.safeguard_engine.get_preferred_day_temperature(dev_id)
            if pt is not None:
                preferred_day_temps[dev_id] = pt

        decision: ScheduleDecision = self.schedule_engine.evaluate(
            reservations=effective_reservations,
            current_dt=sofia_now,
            owner_test=active_owner_test,
            preferred_day_temperatures=preferred_day_temps if preferred_day_temps else None
        )

        # Append grace period notice to decision reason if applicable
        if in_grace_period_uids:
            decision.decision_reason += f" [ACTIVE GRACE PERIOD FOR {', '.join(mask_uid(u) for u in in_grace_period_uids)}]"

        self.last_decision_reason = decision.decision_reason

        # -------------------------------------------------------------
        # 3. Notify on system state transitions
        # -------------------------------------------------------------
        is_daypart_transition = (self.last_system_state is not None and self.last_system_state != decision.system_state)
        is_transition_tick = (self.last_system_state != decision.system_state)
        if is_transition_tick:
            old_str = self.last_system_state.value if self.last_system_state else "STARTUP"
            new_str = decision.system_state.value
            self.notifier.notify_state_change(
                old_state=old_str,
                new_state=new_str,
                reason=decision.decision_reason,
                metadata={
                    "sofia_time": sofia_now.isoformat(),
                    "active_reservation_uid": decision.active_reservation.masked_uid if decision.active_reservation else None,
                    "target_temp": decision.target_temperature,
                    "target_power": decision.target_power
                }
            )
            self.last_system_state = decision.system_state

        # -------------------------------------------------------------
        # 4. Fetch current device states
        # -------------------------------------------------------------
        current_states = self.controller.get_all_states()
        self.last_device_states = current_states

        # -------------------------------------------------------------
        # 4a. Multi-Tier Low Temperature, Freshness & Safety Monitoring
        # -------------------------------------------------------------
        # In unheated rooms or when guest turns bedroom AC OFF, frost can damage pipes.
        # Living room heating (21°C-23°C) does NOT guarantee adequate temperature in a closed bedroom.
        # Multi-tier monitoring:
        # - CRITICAL: <= 8.0°C (Immediate freeze risk)
        # - ELEVATED: <= 12.0°C (Elevated danger to plumbing/structure)
        # - WARNING:  <= 16.0°C (Comfort alert / inadequate heating)
        # Cooldown: 4.0 hours per tier. Hysteresis: +1.0°C recovery.
        for dev_id, st in current_states.items():
            # Separate offline devices from online/powered-off devices
            if not getattr(st, "is_online", True):
                _LOGGER.warning(f"Device {dev_id} is OFFLINE. Cannot verify indoor temperature.")
                continue

            freshness = st.get_freshness_status(
                now=sofia_now,
                max_fresh_minutes=self.config.TEMP_FRESHNESS_MAX_AGE_MINUTES
            )

            # Calculate reading age and human-readable strings
            age_hours = None
            age_str = None
            measured_at_str = None
            if st.temp_measured_at is not None:
                try:
                    eval_ts = sofia_now
                    meas_ts = st.temp_measured_at
                    if eval_ts.tzinfo is None and meas_ts.tzinfo is not None:
                        eval_ts = eval_ts.replace(tzinfo=TIMEZONE_SOFIA)
                    elif eval_ts.tzinfo is not None and meas_ts.tzinfo is None:
                        meas_ts = meas_ts.replace(tzinfo=TIMEZONE_SOFIA)
                    age_sec = max(0.0, (eval_ts - meas_ts).total_seconds())
                    age_hours = age_sec / 3600.0
                    if age_sec < 60:
                        age_str = "just now"
                    elif age_sec < 3600:
                        age_str = f"{int(age_sec // 60)}m"
                    else:
                        age_str = f"{int(age_sec // 3600)}h {int((age_sec % 3600) // 60)}m"
                    meas_sofia = meas_ts.astimezone(TIMEZONE_SOFIA) if meas_ts.tzinfo else meas_ts
                    measured_at_str = meas_sofia.strftime("%H:%M Sofia")
                except Exception:
                    age_hours = None

            # 1. Prolonged Stale Telemetry Warning
            # When unit is unpowered / stopped, fan does not circulate and thermistor may stagnate
            if (
                freshness == "STALE"
                and age_hours is not None
                and age_hours >= self.config.PROLONGED_STALE_ALERT_HOURS
            ):
                last_stale = self._last_stale_alerts.get(dev_id)
                should_alert_stale = False
                if not last_stale:
                    should_alert_stale = True
                else:
                    try:
                        last_ts = datetime.fromisoformat(last_stale["timestamp"])
                        cooldown_sec = self.config.LOW_TEMP_ALERT_COOLDOWN_HOURS * 3600
                        if (sofia_now - last_ts).total_seconds() >= cooldown_sec:
                            should_alert_stale = True
                    except Exception:
                        should_alert_stale = True

                if should_alert_stale:
                    self.notifier.notify_stale_telemetry_warning(
                        device_id=dev_id,
                        age_hours=age_hours,
                        last_temp=st.current_temperature,
                        is_guest_disabled=(not st.power),
                        is_offline=False
                    )
                    self._last_stale_alerts[dev_id] = {
                        "timestamp": sofia_now.isoformat(),
                        "age_hours": age_hours,
                        "temperature": st.current_temperature
                    }
            elif freshness == "FRESH":
                # Clear stale alert once fresh reading arrives
                self._last_stale_alerts.pop(dev_id, None)

            # 2. Temperature Threshold Evaluation
            if st.current_temperature is not None:
                curr_temp = st.current_temperature
                tier = None
                threshold = None
                if curr_temp <= self.config.LOW_TEMP_CRITICAL_THRESHOLD:
                    tier = "CRITICAL"
                    threshold = self.config.LOW_TEMP_CRITICAL_THRESHOLD
                elif curr_temp <= self.config.LOW_TEMP_ELEVATED_THRESHOLD:
                    tier = "ELEVATED"
                    threshold = self.config.LOW_TEMP_ELEVATED_THRESHOLD
                elif curr_temp <= self.config.LOW_TEMP_WARNING_THRESHOLD:
                    tier = "WARNING"
                    threshold = self.config.LOW_TEMP_WARNING_THRESHOLD

                last_alert = self._last_frost_alerts.get(dev_id)
                if tier:
                    tier_rank = {"WARNING": 1, "ELEVATED": 2, "CRITICAL": 3}
                    should_alert = False
                    if not last_alert:
                        should_alert = True
                    else:
                        prev_rank = tier_rank.get(last_alert.get("tier"), 0)
                        curr_rank = tier_rank.get(tier, 0)
                        # Escalation to higher severity immediately bypasses cooldown!
                        if curr_rank > prev_rank:
                            should_alert = True
                        else:
                            try:
                                last_ts = datetime.fromisoformat(last_alert["timestamp"])
                                cooldown_sec = self.config.LOW_TEMP_ALERT_COOLDOWN_HOURS * 3600
                                if (sofia_now - last_ts).total_seconds() >= cooldown_sec:
                                    should_alert = True
                            except Exception:
                                should_alert = True

                    if should_alert:
                        sev_label = "CRITICAL" if tier == "CRITICAL" else "WARNING"
                        is_stale_reading = (freshness == "STALE")
                        self.notifier.notify_frost_warning(
                            device_id=dev_id,
                            measured_temp=curr_temp,
                            threshold=threshold,
                            severity=sev_label,
                            is_guest_disabled=(not st.power),
                            is_stale=is_stale_reading,
                            reading_age_str=age_str,
                            measured_at_str=measured_at_str,
                            metadata={"tier": tier, "freshness": freshness, "age_hours": age_hours}
                        )
                        self._last_frost_alerts[dev_id] = {
                            "tier": tier,
                            "timestamp": sofia_now.isoformat(),
                            "temperature": curr_temp,
                            "is_stale": is_stale_reading
                        }
                elif curr_temp >= (self.config.LOW_TEMP_WARNING_THRESHOLD + 1.0):
                    # CRITICAL SAFETY REQUIREMENT:
                    # An old measurement above 16°C does NOT automatically imply that the room is safe!
                    # Only clear active frost alert if reading is confirmed FRESH!
                    if freshness == "FRESH":
                        self._last_frost_alerts.pop(dev_id, None)
                    else:
                        _LOGGER.debug(
                            f"Device {dev_id} reported {curr_temp}°C >= warning threshold, "
                            f"but reading is STALE ({age_str} old). Retaining active frost alert for safety."
                        )

        # -------------------------------------------------------------
        # 4c. Economic Comfort Policy & High Temperature Alerting
        # -------------------------------------------------------------
        for dev_id, st in current_states.items():
            if st.is_online and st.power and st.target_temperature is not None:
                if st.target_temperature > self.config.ECONOMIC_MAX_DAY_TEMP:
                    last_econ = self._last_economic_alerts.get(dev_id)
                    should_econ_alert = False
                    if not last_econ:
                        should_econ_alert = True
                    else:
                        try:
                            last_ts = datetime.fromisoformat(last_econ["timestamp"])
                            if (sofia_now - last_ts).total_seconds() >= 4.0 * 3600:
                                should_econ_alert = True
                        except Exception:
                            should_econ_alert = True

                    if should_econ_alert:
                        is_cold = (st.current_temperature is not None and st.current_temperature < 20.0)
                        if is_cold:
                            reason = (
                                f"Room is currently cold ({st.current_temperature}°C); "
                                f"high target setpoint reflects warming demand or open window/draft, not waste."
                            )
                        else:
                            reason = (
                                f"Setpoint exceeds recommended economic comfort threshold ({self.config.ECONOMIC_MAX_DAY_TEMP}°C)."
                            )
                        self.notifier.notify_economic_alert(
                            device_id=dev_id,
                            requested_temp=st.target_temperature,
                            measured_temp=st.current_temperature,
                            limit=self.config.ECONOMIC_MAX_DAY_TEMP,
                            reason=reason
                        )
                        self._last_economic_alerts[dev_id] = {
                            "timestamp": sofia_now.isoformat(),
                            "requested_temperature": st.target_temperature,
                            "measured_temperature": st.current_temperature
                        }

        # -------------------------------------------------------------
        # 4b. First-Start Live Telemetry Initialization
        # -------------------------------------------------------------
        # If starting fresh, running after restart, or command history is missing/DRY_RUN only:
        # Read physical telemetry directly to initialize and preserve existing guest choices.
        # Idempotent: repeated runs for same reservation do not overwrite valid overrides.
        if decision.active_reservation and decision.active_reservation.is_confirmed:
            if (
                self.safeguard_engine.initialized_reservation_uid != decision.active_reservation.uid
                or not self.safeguard_engine.has_live_command_history
            ):
                self.safeguard_engine.initialize_from_physical_telemetry(
                    current_states=current_states,
                    active_reservation=decision.active_reservation,
                    system_state=decision.system_state,
                    eval_time=sofia_now,
                    scheduled_commands=decision.commands_to_send,
                )

        # -------------------------------------------------------------
        # 5. Apply safeguards, debounce, manual override, and compressor checks
        # -------------------------------------------------------------
        safeguard_results = self.safeguard_engine.filter_and_validate(
            decision=decision,
            current_states=current_states,
            now=sofia_now,
            is_transition_tick=is_daypart_transition,
            known_reservations=list(self.known_reservations_cache.values()) if hasattr(self, "known_reservations_cache") else None,
            calendar_stale=is_calendar_stale
        )


        # -------------------------------------------------------------
        # 6. Dispatch validated commands with retry & acknowledgment
        # -------------------------------------------------------------
        commands_sent = {}
        for dev_id, (should_send, cmd, rationale) in safeguard_results.items():
            if should_send and cmd:
                # Pre-dispatch defense-in-depth: Revalidate occupancy before physical Power=False
                if not cmd.power and self._is_in_active_confirmed_guest_stay(sofia_now):
                    _LOGGER.critical(
                        f"PRE-DISPATCH VIOLATION INTERCEPTED: Power=False command for {dev_id} was blocked at dispatch layer. "
                        f"Active confirmed guest stay detected at {sofia_now.isoformat()}."
                    )
                    commands_sent[dev_id] = False
                    continue

                success = False
                max_retries = 2
                for attempt in range(max_retries):
                    try:
                        success = self.controller.send_command(cmd, dry_run=self.config.DRY_RUN)
                        if success:
                            break
                    except Exception as e:
                        _LOGGER.warning(f"Command attempt {attempt + 1} to {dev_id} failed: {e}")
                        if attempt < max_retries - 1:
                            time.sleep(1.0)

                commands_sent[dev_id] = success
                if success and not self.config.DRY_RUN:
                    self.safeguard_engine.record_applied_command(cmd)
                    self.safeguard_engine.mark_live_command_executed()
                if not success:
                    self.notifier.notify_device_error(
                        dev_id, f"Failed to dispatch command after retries: {rationale}"
                    )

            else:
                commands_sent[dev_id] = False
                _LOGGER.debug(f"Command skipped for {dev_id}: {rationale}")

        # -------------------------------------------------------------
        # 6b. Track durable transition window completion
        # -------------------------------------------------------------
        curr_time = sofia_now.time()
        today_iso = today.isoformat()
        if self.config.MORNING_WINDOW_START <= curr_time <= self.config.MORNING_WINDOW_END:
            self.last_completed_transitions["morning"] = today_iso
        elif self.config.ECONOMIC_WINDOW_START <= curr_time <= self.config.ECONOMIC_WINDOW_END:
            self.last_completed_transitions["evening"] = today_iso
        elif self.config.NIGHT_WINDOW_START <= curr_time <= self.config.NIGHT_WINDOW_END:
            self.last_completed_transitions["night"] = today_iso

        # -------------------------------------------------------------
        # 7. Persist state for next invocation
        # -------------------------------------------------------------
        self._save_persisted_state()

        # -------------------------------------------------------------
        # 7b. Collect 15-minute telemetry observation (Read-Only 7-Day Monitoring)
        # -------------------------------------------------------------
        if getattr(self.config, "MONITORING_ENABLED", True) and getattr(self, "collector", None):
            try:
                self.collector.record_cycle(
                    current_states=current_states,
                    eval_time_sofia=sofia_now,
                    system_state=decision.system_state,
                    active_reservation=decision.active_reservation,
                    manual_overrides=self.safeguard_engine.get_manual_overrides(),
                    commands_sent=commands_sent,
                )
            except Exception as e:
                _LOGGER.error(f"Failed to record monitoring observation: {e}")

        return CycleResult(
            timestamp=sofia_now,
            system_state=decision.system_state,
            active_reservation=decision.active_reservation,
            decision_reason=decision.decision_reason,
            safeguard_decisions=safeguard_results,
            commands_sent=commands_sent,
            cancelled_uids=cancelled_uids,
            in_grace_period_uids=in_grace_period_uids,
            dry_run=self.config.DRY_RUN,
            calendar_stale=is_calendar_stale
        )
