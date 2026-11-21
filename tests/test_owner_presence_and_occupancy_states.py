"""
Regression and verification tests for Occupied, Blocked, Unoccupied, and Owner-Present states.

Verifies:
1. Unoccupied state: empty calendar triggers IDLE_UNOCCUPIED (power=False or anti-freeze).
2. Blocked dates state: follows HEAT_DURING_BLOCKED_DATES setting.
3. Occupied state: confirmed guest stay follows diurnal per-room heating schedule.
4. Owner-Present state: active owner presence override runs full per-room heating schedule (including night mode).
5. Guest priority: confirmed guest bookings strictly retain priority over owner presence.
6. Expiration safety: expired owner overrides automatically fall through to calendar automation.
"""

from datetime import datetime, date, time, timedelta
import pytest

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.models import (
    Reservation,
    ReservationType,
    SystemState,
    ACMode,
    ACFanSpeed,
    OwnerTestSession
)
from src.scheduler import HVACScheduleEngine


@pytest.fixture
def test_config():
    cfg = AutomationConfig()
    cfg.TCL_LIVING_ROOM_ID = "LR_DEVICE_001"
    cfg.TCL_BEDROOM_ID = "BR_DEVICE_002"
    cfg.DEVICE_IDS = ["LR_DEVICE_001", "BR_DEVICE_002"]
    cfg.DEFAULT_HEATING_PROFILE = "AT_HOME"
    cfg.HEATING_PROFILES = {
        "AT_HOME": {
            "name": "At-Home Comfort",
            "morning_temp": 23.0,
            "day_temp": 23.0,
            "evening_temp": 23.0,
            "night_temp": 20.0,
            "morning_fan": "Mid",
            "day_fan": "Mid",
            "evening_fan": "Mid",
            "night_fan": "Quiet",
            "living_room": {
                "morning_temp": 23.0, "day_temp": 23.0, "evening_temp": 23.0, "night_temp": 20.0,
                "morning_fan": "Mid", "day_fan": "Mid", "evening_fan": "Mid", "night_fan": "Quiet"
            },
            "bedroom": {
                "morning_temp": 22.0, "day_temp": 21.0, "evening_temp": 22.0, "night_temp": 20.0,
                "morning_fan": "Auto", "day_fan": "Auto", "evening_fan": "Auto", "night_fan": "Quiet"
            }
        }
    }
    cfg.LIVING_ROOM_LED_MODE = "SCHEDULE"
    cfg.BEDROOM_LED_MODE = "SCHEDULE"
    cfg.DEFAULT_LED_DISPLAY = True
    cfg.NIGHT_LED_DISPLAY = False
    cfg.BEEP_FEEDBACK_ENABLED = False
    cfg.HEAT_DURING_BLOCKED_DATES = False
    return cfg


def test_unoccupied_state_triggers_idle_shutdown(test_config):
    """Empty calendar triggers IDLE_UNOCCUPIED with power=False."""
    engine = HVACScheduleEngine(config=test_config)
    now = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)

    decision = engine.evaluate(reservations=[], current_dt=now)

    assert decision.system_state == SystemState.IDLE_UNOCCUPIED
    assert decision.target_power is False
    assert "property unoccupied" in decision.decision_reason
    for dev_id in test_config.DEVICE_IDS:
        cmd = decision.commands_to_send[dev_id]
        assert cmd.power is False


def test_blocked_dates_suppression_when_heat_blocked_disabled(test_config):
    """When HEAT_DURING_BLOCKED_DATES is False, blocked dates result in IDLE_UNOCCUPIED."""
    test_config.HEAT_DURING_BLOCKED_DATES = False
    engine = HVACScheduleEngine(config=test_config)
    now = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)

    blocked_res = Reservation(
        uid="blocked-stay-123",
        start_date=date(2026, 10, 17),
        end_date=date(2026, 10, 20),
        summary="Airbnb (Not available)",
        reservation_type=ReservationType.BLOCKED,
        raw_status="CONFIRMED"
    )

    decision = engine.evaluate(reservations=[blocked_res], current_dt=now)

    assert decision.system_state == SystemState.IDLE_UNOCCUPIED
    assert decision.target_power is False
    assert "HEAT_DURING_BLOCKED_DATES is disabled" in decision.decision_reason


def test_blocked_dates_heating_when_heat_blocked_enabled(test_config):
    """When HEAT_DURING_BLOCKED_DATES is True, blocked dates follow occupied schedule."""
    test_config.HEAT_DURING_BLOCKED_DATES = True
    engine = HVACScheduleEngine(config=test_config)
    now = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)

    blocked_res = Reservation(
        uid="blocked-stay-123",
        start_date=date(2026, 10, 17),
        end_date=date(2026, 10, 20),
        summary="Airbnb (Not available)",
        reservation_type=ReservationType.BLOCKED,
        raw_status="CONFIRMED"
    )

    decision = engine.evaluate(reservations=[blocked_res], current_dt=now)

    assert decision.system_state == SystemState.OCCUPIED_DAY
    assert decision.target_power is True
    lr_cmd = decision.commands_to_send[test_config.TCL_LIVING_ROOM_ID]
    br_cmd = decision.commands_to_send[test_config.TCL_BEDROOM_ID]
    assert lr_cmd.target_temperature == 23.0
    assert br_cmd.target_temperature == 21.0


def test_confirmed_guest_occupied_day_and_night_transitions(test_config):
    """Confirmed guest follows daytime comfort and nighttime 23:00 setback."""
    engine = HVACScheduleEngine(config=test_config)
    guest_res = Reservation(
        uid="guest-booking-456",
        start_date=date(2026, 10, 17),
        end_date=date(2026, 10, 20),
        summary="Reserved",
        reservation_type=ReservationType.CONFIRMED,
        raw_status="CONFIRMED"
    )

    # 1. Daytime (14:00) -> OCCUPIED_DAY: 23°C LR, 21°C BR, LED ON
    day_dt = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)
    day_decision = engine.evaluate(reservations=[guest_res], current_dt=day_dt)
    assert day_decision.system_state == SystemState.OCCUPIED_DAY
    assert day_decision.target_power is True
    assert day_decision.commands_to_send[test_config.TCL_LIVING_ROOM_ID].target_temperature == 23.0
    assert day_decision.commands_to_send[test_config.TCL_LIVING_ROOM_ID].display_led is True
    assert day_decision.commands_to_send[test_config.TCL_BEDROOM_ID].target_temperature == 21.0
    assert day_decision.commands_to_send[test_config.TCL_BEDROOM_ID].display_led is True

    # 2. Nighttime (23:30) -> OCCUPIED_NIGHT: 20°C LR, 20°C BR, Quiet fan, LED OFF
    night_dt = datetime(2026, 10, 18, 23, 30, tzinfo=TIMEZONE_SOFIA)
    night_decision = engine.evaluate(reservations=[guest_res], current_dt=night_dt)
    assert night_decision.system_state == SystemState.OCCUPIED_NIGHT
    assert night_decision.target_power is True
    lr_cmd = night_decision.commands_to_send[test_config.TCL_LIVING_ROOM_ID]
    br_cmd = night_decision.commands_to_send[test_config.TCL_BEDROOM_ID]
    assert lr_cmd.target_temperature == 20.0
    assert lr_cmd.fan_speed == ACFanSpeed.QUIET
    assert lr_cmd.display_led is False
    assert br_cmd.target_temperature == 20.0
    assert br_cmd.fan_speed == ACFanSpeed.QUIET
    assert br_cmd.display_led is False


def test_owner_present_state_applies_diurnal_schedule_during_unoccupied_dates(test_config):
    """OWNER_PRESENT override runs full per-room diurnal heating schedule rather than vacancy shutdown."""
    engine = HVACScheduleEngine(config=test_config)
    now = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)

    owner_present = OwnerTestSession(
        active=True,
        started_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=3),
        duration_hours=4.0,
        session_type="OWNER_PRESENT"
    )

    # 1. Daytime: 14:00 -> OCCUPIED_DAY (23°C LR, 21°C BR, LED ON)
    decision_day = engine.evaluate(reservations=[], current_dt=now, owner_test=owner_present)
    assert decision_day.system_state == SystemState.OCCUPIED_DAY
    assert decision_day.target_power is True
    assert "Owner presence active" in decision_day.decision_reason
    lr_day = decision_day.commands_to_send[test_config.TCL_LIVING_ROOM_ID]
    br_day = decision_day.commands_to_send[test_config.TCL_BEDROOM_ID]
    assert lr_day.target_temperature == 23.0
    assert lr_day.display_led is True
    assert br_day.target_temperature == 21.0
    assert br_day.display_led is True

    # 2. Nighttime: 23:30 -> OCCUPIED_NIGHT (20°C LR, 20°C BR, Quiet fan, LED OFF)
    now_night = datetime(2026, 10, 18, 23, 30, tzinfo=TIMEZONE_SOFIA)
    owner_present_night = OwnerTestSession(
        active=True,
        started_at=now_night - timedelta(hours=1),
        expires_at=now_night + timedelta(hours=8),
        duration_hours=9.0,
        session_type="OWNER_PRESENT"
    )
    decision_night = engine.evaluate(reservations=[], current_dt=now_night, owner_test=owner_present_night)
    assert decision_night.system_state == SystemState.OCCUPIED_NIGHT
    assert decision_night.target_power is True
    lr_night = decision_night.commands_to_send[test_config.TCL_LIVING_ROOM_ID]
    br_night = decision_night.commands_to_send[test_config.TCL_BEDROOM_ID]
    assert lr_night.target_temperature == 20.0
    assert lr_night.fan_speed == ACFanSpeed.QUIET
    assert lr_night.display_led is False
    assert br_night.target_temperature == 20.0
    assert br_night.fan_speed == ACFanSpeed.QUIET
    assert br_night.display_led is False


def test_owner_test_fixed_hold_mode(test_config):
    """OWNER_TEST session_type holds a fixed temperature setpoint."""
    engine = HVACScheduleEngine(config=test_config)
    now = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)

    owner_test = OwnerTestSession(
        active=True,
        started_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
        duration_hours=2.0,
        target_temperature=24.5,
        session_type="OWNER_TEST"
    )

    decision = engine.evaluate(reservations=[], current_dt=now, owner_test=owner_test)
    assert decision.system_state == SystemState.OWNER_TEST_HOLD
    assert decision.target_temperature == 24.5


def test_confirmed_guest_reservation_strictly_prioritized_over_owner_present(test_config):
    """Confirmed guest reservation takes priority over owner presence override."""
    engine = HVACScheduleEngine(config=test_config)
    now = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)

    confirmed_guest = Reservation(
        uid="confirmed-guest-999",
        start_date=date(2026, 10, 17),
        end_date=date(2026, 10, 20),
        summary="Reserved",
        reservation_type=ReservationType.CONFIRMED,
        raw_status="CONFIRMED"
    )

    # Active owner presence on same day
    owner_present = OwnerTestSession(
        active=True,
        started_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=5),
        duration_hours=6.0,
        session_type="OWNER_PRESENT"
    )

    decision = engine.evaluate(reservations=[confirmed_guest], current_dt=now, owner_test=owner_present)

    # Must be driven by guest reservation, NOT owner test
    assert decision.active_reservation.uid == "confirmed-guest-999"
    assert "confir...-999" in decision.decision_reason
    assert "Owner presence active" not in decision.decision_reason


def test_expired_owner_present_falls_through_to_unoccupied(test_config):
    """Expired owner override is ignored and returns to IDLE_UNOCCUPIED."""
    engine = HVACScheduleEngine(config=test_config)
    now = datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA)

    expired_owner = OwnerTestSession(
        active=True,
        started_at=now - timedelta(hours=4),
        expires_at=now - timedelta(hours=1),  # Expired 1 hour ago
        duration_hours=3.0,
        session_type="OWNER_PRESENT"
    )

    decision = engine.evaluate(reservations=[], current_dt=now, owner_test=expired_owner)

    assert decision.system_state == SystemState.IDLE_UNOCCUPIED
    assert decision.target_power is False
    assert "property unoccupied" in decision.decision_reason


def test_upcoming_guest_pre_arrival_preempts_owner_presence(test_config):
    """On arrival day, owner presence heats until pre-arrival window begins, then guest pre-heating takes over."""
    engine = HVACScheduleEngine(config=test_config)

    # Guest arrives today (checkin 15:00, pre-arrival starts at 13:00)
    incoming_guest = Reservation(
        uid="guest-arriving-today",
        start_date=date(2026, 10, 18),
        end_date=date(2026, 10, 21),
        summary="Reserved",
        reservation_type=ReservationType.CONFIRMED,
        raw_status="CONFIRMED"
    )

    # 1. At 11:00 (before pre-arrival): Owner presence is active
    dt_11am = datetime(2026, 10, 18, 11, 0, tzinfo=TIMEZONE_SOFIA)
    owner_present = OwnerTestSession(
        active=True,
        started_at=dt_11am - timedelta(hours=2),
        expires_at=dt_11am + timedelta(hours=4),
        duration_hours=6.0,
        session_type="OWNER_PRESENT"
    )
    decision_11am = engine.evaluate(reservations=[incoming_guest], current_dt=dt_11am, owner_test=owner_present)
    assert decision_11am.system_state == SystemState.OCCUPIED_DAY
    assert "Owner presence active" in decision_11am.decision_reason

    # 2. At 13:30 (during pre-arrival window): Incoming guest pre-heating strictly takes over!
    dt_1330 = datetime(2026, 10, 18, 13, 30, tzinfo=TIMEZONE_SOFIA)
    decision_1330 = engine.evaluate(reservations=[incoming_guest], current_dt=dt_1330, owner_test=owner_present)
    assert decision_1330.system_state == SystemState.PRE_ARRIVAL_HEATING
    assert decision_1330.active_reservation.uid == "guest-arriving-today"
    assert "Pre-arrival heating" in decision_1330.decision_reason


def test_consecutive_guest_reservations_with_owner_presence(test_config):
    """
    On consecutive turnover day (Guest A leaves 11:00, Guest B arrives 15:00):
    - Before 11:00: Guest A checkout morning takes priority.
    - 11:00-13:00 (turnover gap): If owner presence is active, owner presence keeps home comfortable.
    - 13:00-15:00: Guest B pre-arrival takes strict priority over owner presence.
    - 15:00+: Guest B in-stay takes strict priority.
    """
    engine = HVACScheduleEngine(config=test_config)
    today = date(2026, 10, 18)

    guest_a = Reservation(
        uid="guest-a-departing",
        start_date=date(2026, 10, 15),
        end_date=today,
        summary="Guest A",
        reservation_type=ReservationType.CONFIRMED,
        raw_status="CONFIRMED"
    )
    guest_b = Reservation(
        uid="guest-b-arriving",
        start_date=today,
        end_date=date(2026, 10, 22),
        summary="Guest B",
        reservation_type=ReservationType.CONFIRMED,
        raw_status="CONFIRMED"
    )
    reservations = [guest_a, guest_b]

    owner_present = OwnerTestSession(
        active=True,
        started_at=datetime(2026, 10, 18, 8, 0, tzinfo=TIMEZONE_SOFIA),
        expires_at=datetime(2026, 10, 18, 18, 0, tzinfo=TIMEZONE_SOFIA),
        duration_hours=10.0,
        session_type="OWNER_PRESENT"
    )

    # 1. 10:00 (Before 11:00 checkout) -> Guest A departing takes priority
    t_1000 = datetime(2026, 10, 18, 10, 0, tzinfo=TIMEZONE_SOFIA)
    dec_1000 = engine.evaluate(reservations=reservations, current_dt=t_1000, owner_test=owner_present)
    assert dec_1000.active_reservation.uid == "guest-a-departing"
    assert "departing at 11:00" in dec_1000.decision_reason

    # 2. 12:00 (Turnover gap 11:00 - 13:00) -> Neither guest is active; Owner presence heats property
    t_1200 = datetime(2026, 10, 18, 12, 0, tzinfo=TIMEZONE_SOFIA)
    dec_1200 = engine.evaluate(reservations=reservations, current_dt=t_1200, owner_test=owner_present)
    assert dec_1200.system_state == SystemState.OCCUPIED_DAY
    assert dec_1200.target_power is True
    assert "Owner presence active" in dec_1200.decision_reason

    # 3. 13:30 (Pre-arrival window 13:00 - 15:00) -> Guest B pre-arrival takes strict priority
    t_1330 = datetime(2026, 10, 18, 13, 30, tzinfo=TIMEZONE_SOFIA)
    dec_1330 = engine.evaluate(reservations=reservations, current_dt=t_1330, owner_test=owner_present)
    assert dec_1330.system_state == SystemState.PRE_ARRIVAL_HEATING
    assert dec_1330.active_reservation.uid == "guest-b-arriving"

    # 4. 16:00 (Guest B checked in) -> Guest B takes strict priority
    t_1600 = datetime(2026, 10, 18, 16, 0, tzinfo=TIMEZONE_SOFIA)
    dec_1600 = engine.evaluate(reservations=reservations, current_dt=t_1600, owner_test=owner_present)
    assert dec_1600.system_state == SystemState.OCCUPIED_DAY
    assert dec_1600.active_reservation.uid == "guest-b-arriving"


def test_owner_presence_expiration_never_interrupts_active_guest(test_config):
    """
    An active confirmed guest stay is completely immune to owner presence expiration.
    When owner presence expires during a guest stay, heating remains continuously active.
    """
    engine = HVACScheduleEngine(config=test_config)
    today = date(2026, 10, 18)

    guest = Reservation(
        uid="active-midstay-guest",
        start_date=date(2026, 10, 16),
        end_date=date(2026, 10, 20),
        summary="Guest In Stay",
        reservation_type=ReservationType.CONFIRMED,
        raw_status="CONFIRMED"
    )

    # Owner presence expires at exactly 14:00
    owner_present = OwnerTestSession(
        active=True,
        started_at=datetime(2026, 10, 18, 10, 0, tzinfo=TIMEZONE_SOFIA),
        expires_at=datetime(2026, 10, 18, 14, 0, tzinfo=TIMEZONE_SOFIA),
        duration_hours=4.0,
        session_type="OWNER_PRESENT"
    )

    # At 13:55 (5 min before expiry): Guest is heated normally
    t_1355 = datetime(2026, 10, 18, 13, 55, tzinfo=TIMEZONE_SOFIA)
    dec_1355 = engine.evaluate(reservations=[guest], current_dt=t_1355, owner_test=owner_present)
    assert dec_1355.system_state == SystemState.OCCUPIED_DAY
    assert dec_1355.target_power is True
    assert dec_1355.active_reservation.uid == "active-midstay-guest"

    # At 14:05 (5 min after expiry): Guest continues being heated normally without interruption
    t_1405 = datetime(2026, 10, 18, 14, 5, tzinfo=TIMEZONE_SOFIA)
    dec_1405 = engine.evaluate(reservations=[guest], current_dt=t_1405, owner_test=owner_present)
    assert dec_1405.system_state == SystemState.OCCUPIED_DAY
    assert dec_1405.target_power is True
    assert dec_1405.active_reservation.uid == "active-midstay-guest"


def test_owner_present_expiration_warning_and_remaining_calculation():
    """Verify remaining_seconds, remaining_str, and is_expiring_soon warning behavior."""
    now = datetime(2026, 10, 18, 12, 0, tzinfo=TIMEZONE_SOFIA)

    # Case 1: 5 hours remaining -> is_expiring_soon is False
    session_long = OwnerTestSession(
        active=True,
        started_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=5),
        duration_hours=6.0,
        session_type="OWNER_PRESENT"
    )
    assert session_long.remaining_seconds(now) == 5 * 3600
    assert session_long.is_expiring_soon(threshold_minutes=120, current_dt=now) is False

    # Case 2: 90 minutes remaining -> is_expiring_soon is True (under 2 hour threshold)
    session_soon = OwnerTestSession(
        active=True,
        started_at=now - timedelta(hours=2),
        expires_at=now + timedelta(minutes=90),
        duration_hours=3.5,
        session_type="OWNER_PRESENT"
    )
    assert session_soon.remaining_seconds(now) == 90 * 60
    assert session_soon.is_expiring_soon(threshold_minutes=120, current_dt=now) is True

    # Case 3: to_dict serialization
    d = session_soon.to_dict(now)
    assert d["remaining_str"] == "1h 30m"
    assert d["is_expiring_soon"] is True
    assert "Sofia" in d["expires_at_formatted"]


def test_config_save_does_not_overwrite_owner_present(tmp_path):
    """
    Saving general dashboard configuration to S3/cache must NEVER overwrite or clear
    an active OWNER_PRESENT session stored in owner_test_session.json.
    """
    from src.dashboard.config_store import DashboardConfigStore
    cfg = AutomationConfig()
    cfg.S3_BUCKET = ""  # Use local cache directory

    import src.config
    import src.dashboard.config_store
    old_cache = src.config.CACHE_DIR
    old_cs_cache = src.dashboard.config_store.CACHE_DIR
    src.config.CACHE_DIR = tmp_path
    src.dashboard.config_store.CACHE_DIR = tmp_path
    try:
        store = DashboardConfigStore(config=cfg)

        # 1. Save an active OWNER_PRESENT session
        now = datetime.now(TIMEZONE_SOFIA)
        session = OwnerTestSession(
            active=True,
            started_at=now,
            expires_at=now + timedelta(hours=24),
            duration_hours=24.0,
            session_type="OWNER_PRESENT",
            notes="Owner personal stay"
        )
        assert store.save_owner_test_session(session) is True

        # 2. Modify general dashboard config (e.g. change temperatures and fan speed)
        updates = {
            "schedule_day_temp": 24.0,
            "schedule_night_temp": 19.5,
            "living_room_led_mode": "ALWAYS_OFF",
            "heat_during_blocked_dates": True
        }
        assert store.save_dashboard_config(updates) is True

        # 3. Reload owner test session and verify it is completely intact
        reloaded = store.load_owner_test_session()
        assert reloaded is not None
        assert reloaded.active is True
        assert reloaded.session_type == "OWNER_PRESENT"
        assert reloaded.duration_hours == 24.0
        assert reloaded.notes == "Owner personal stay"
    finally:
        src.config.CACHE_DIR = old_cache
        src.dashboard.config_store.CACHE_DIR = old_cs_cache


def test_dashboard_occupancy_distinction_states(test_config, tmp_path):
    """Verify that _build_status_data accurately distinguishes GUEST OCCUPIED, OWNER PRESENT, and UNOCCUPIED."""
    from src.dashboard.handler import _build_status_data
    from src.dashboard.config_store import DashboardConfigStore
    from src.state_store import StateStore

    test_config.S3_BUCKET = ""
    test_config.STATE_CACHE_FILE = tmp_path / "automation_state.json"
    import src.config
    import src.dashboard.config_store
    old_cache = src.config.CACHE_DIR
    old_cs_cache = src.dashboard.config_store.CACHE_DIR
    src.config.CACHE_DIR = tmp_path
    src.dashboard.config_store.CACHE_DIR = tmp_path
    try:
        state_store = StateStore(s3_bucket="")
        config_store = DashboardConfigStore(config=test_config)

        # Case 1: Empty calendar, no owner test -> UNOCCUPIED
        state_store.save_state({
            "last_system_state": "IDLE_UNOCCUPIED",
            "known_reservations": {},
            "decision_reason": "No reservations"
        })
        st1 = _build_status_data(test_config, config_store)
        assert st1["occupancy"]["status"] == "UNOCCUPIED"

        # Case 2: Owner present active -> OWNER PRESENT
        now = datetime.now(TIMEZONE_SOFIA)
        owner_session = OwnerTestSession(
            active=True,
            started_at=now,
            expires_at=now + timedelta(hours=12),
            duration_hours=12.0,
            session_type="OWNER_PRESENT"
        )
        config_store.save_owner_test_session(owner_session)
        st2 = _build_status_data(test_config, config_store)
        assert st2["occupancy"]["status"] == "OWNER PRESENT"
        assert st2["system_state"] == "OWNER_PRESENT"

        # Case 3: Confirmed guest active -> GUEST OCCUPIED (strictly supersedes owner present)
        today_str = now.date().isoformat()
        tomorrow_str = (now.date() + timedelta(days=2)).isoformat()
        state_store.save_state({
            "last_system_state": "OCCUPIED_DAY",
            "known_reservations": {
                "guest-uid-1": {
                    "uid": "guest-uid-1",
                    "start_date": today_str,
                    "end_date": tomorrow_str,
                    "type": "CONFIRMED",
                    "summary": "Reserved"
                }
            }
        })
        st3 = _build_status_data(test_config, config_store)
        assert st3["occupancy"]["status"] == "GUEST OCCUPIED"

        # Case 4: Blocked dates -> BLOCKED DATES (when no confirmed guest)
        config_store.clear_owner_test_session()
        state_store.save_state({
            "last_system_state": "IDLE_UNOCCUPIED",
            "known_reservations": {
                "synthetic-blocked-profile-two": {
                    "uid": "synthetic-blocked-profile-two",
                    "start_date": today_str,
                    "end_date": tomorrow_str,
                    "type": "BLOCKED",
                    "summary": "Airbnb (Not available)"
                }
            }
        })
        st4 = _build_status_data(test_config, config_store)
        assert st4["occupancy"]["status"] == "BLOCKED DATES"
    finally:
        src.config.CACHE_DIR = old_cache
        src.dashboard.config_store.CACHE_DIR = old_cs_cache


def test_owner_present_api_activation_and_extension(test_config, tmp_path):
    """Verify that POST /api/owner-test can activate and extend an OWNER_PRESENT session."""
    from src.dashboard.handler import handle_dashboard_request
    from src.dashboard import auth
    import json

    test_config.S3_BUCKET = ""
    test_config.STATE_CACHE_FILE = tmp_path / "automation_state.json"
    test_config.SESSION_SECRET_KEY = "test-secret-synthetic-session-key-for-tests-only-key-123-synthetic-session-key-for-tests-only45678901234567890"

    import src.config
    import src.dashboard.config_store
    old_cache = src.config.CACHE_DIR
    old_cs_cache = src.dashboard.config_store.CACHE_DIR
    src.config.CACHE_DIR = tmp_path
    src.dashboard.config_store.CACHE_DIR = tmp_path
    try:
        session_token = auth.create_session_token(test_config.SESSION_SECRET_KEY)
        csrf_token = auth.generate_csrf_token(session_token, test_config.SESSION_SECRET_KEY)

        cookie_header = f"session_token={session_token}"

        # 1. Start OWNER_PRESENT session for 24h
        start_payload = {
            "csrf_token": csrf_token,
            "action": "START",
            "session_type": "OWNER_PRESENT",
            "duration_hours": 24.0
        }
        event_start = {
            "httpMethod": "POST",
            "path": "/api/owner-test",
            "headers": {"cookie": cookie_header, "content-type": "application/json", "x-csrf-token": csrf_token},
            "body": json.dumps(start_payload)
        }
        resp_start = handle_dashboard_request(event_start, context=None, config=test_config)
        assert resp_start["statusCode"] == 200
        data_start = json.loads(resp_start["body"])
        assert data_start["status"] == "success"
        ot_start = data_start["owner_test"]
        assert ot_start["active"] is True
        assert ot_start["session_type"] == "OWNER_PRESENT"
        assert ot_start["duration_hours"] == 24.0
        assert "Sofia" in ot_start["expires_at_formatted"]
        assert "h " in ot_start["remaining_str"]

        # 2. Extend by 12 hours
        extend_payload = {
            "csrf_token": csrf_token,
            "action": "EXTEND",
            "additional_hours": 12.0
        }
        event_extend = {
            "httpMethod": "POST",
            "path": "/api/owner-test",
            "headers": {"cookie": cookie_header, "content-type": "application/json", "x-csrf-token": csrf_token},
            "body": json.dumps(extend_payload)
        }
        resp_extend = handle_dashboard_request(event_extend, context=None, config=test_config)
        assert resp_extend["statusCode"] == 200
        data_extend = json.loads(resp_extend["body"])
        assert data_extend["status"] == "success"
        ot_extend = data_extend["owner_test"]
        assert ot_extend["active"] is True
        # Duration extended to approx 36 hours
        assert ot_extend["duration_hours"] >= 35.9
        assert "Extended +12.0h" in ot_extend["notes"]

        # 3. Clear session
        cancel_payload = {
            "csrf_token": csrf_token,
            "action": "CANCEL"
        }
        event_cancel = {
            "httpMethod": "POST",
            "path": "/api/owner-test",
            "headers": {"cookie": cookie_header, "content-type": "application/json", "x-csrf-token": csrf_token},
            "body": json.dumps(cancel_payload)
        }
        resp_cancel = handle_dashboard_request(event_cancel, context=None, config=test_config)
        assert resp_cancel["statusCode"] == 200
        data_cancel = json.loads(resp_cancel["body"])
        assert data_cancel["status"] == "success"
    finally:
        src.config.CACHE_DIR = old_cache
        src.dashboard.config_store.CACHE_DIR = old_cs_cache
