"""Deterministic offline demonstration with no credentials or persistent data."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def main():
    # Clear inherited integrations before importing modules that read configuration.
    for name in list(os.environ):
        if name.startswith(("AWS_", "TCL_", "S3_", "SSM_", "TELEGRAM_", "DASHBOARD_", "SHELLY_")) or name in {
            "AIRBNB_ICAL_URL", "NOTIFICATION_WEBHOOK_URL", "HEATING_RUNTIME_DIR", "SESSION_SECRET_KEY",
            "LAMBDA_TASK_ROOT", "MONITORING_ENABLED", "HEAT_DURING_BLOCKED_DATES", "RATE_LIMIT_BACKEND",
            "WEATHER_LATITUDE", "WEATHER_LONGITUDE", "WEATHER_ELEVATION_M",
        }:
            os.environ.pop(name, None)
    os.environ.update(HEATING_LOAD_ENV="false", AWS_EC2_METADATA_DISABLED="true")
    denied = []

    def offline_guard(event, args):
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
            denied.append(event)
            raise RuntimeError("Offline demo blocks network and subprocess access")

    sys.addaudithook(offline_guard)
    with tempfile.TemporaryDirectory(prefix="smart-heating-demo-") as runtime_dir:
        os.environ["HEATING_RUNTIME_DIR"] = runtime_dir
        from src.config import AutomationConfig, TIMEZONE_SOFIA, SAMPLE_CALENDARS_DIR
        from src.orchestrator import AutomationOrchestrator
        from src.device_controller import MockACController
        from src.state_store import StateStore

        config = AutomationConfig()
        # Mock writes update simulation state only; cloud controller is never created.
        config.DRY_RUN = False
        config.MONITORING_ENABLED = False
        controller = MockACController(device_ids=config.DEVICE_IDS)
        orchestrator = AutomationOrchestrator(config=config, controller=controller, state_store=StateStore(in_memory=True))
        for label, stamp in [
            ("Before arrival", "2026-10-19T14:00:00"),
            ("Pre-arrival heating", "2026-10-20T13:30:00"),
            ("Occupied evening", "2026-10-20T20:00:00"),
            ("Checkout", "2026-10-23T11:05:00"),
        ]:
            result = orchestrator.run_cycle(
                current_dt=datetime.fromisoformat(stamp).replace(tzinfo=TIMEZONE_SOFIA),
                calendar_file=SAMPLE_CALENDARS_DIR / "normal_stay.ics",
            )
            print(json.dumps({"scenario": label, "state": result.system_state.value, "reason": result.decision_reason}))
        if denied:
            raise RuntimeError("An offline dependency attempted network or subprocess access")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
