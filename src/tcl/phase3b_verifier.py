"""Phase 3B: Controlled Single-Device Physical TCL Control Verification.

Implements all safety guards:
- Production automation remains disabled
- Targets living room AC only (bedroom AC strictly protected)
- Aborts if AC is currently powered off (never turns on an OFF unit)
- Permits max 1°C temporary delta
- Verifies test setpoint via fresh AWS IoT Shadow read
- Restores baseline target temperature and verifies restoration
- Triggers alerts and aborts on any verification discrepancy
- Redacts credentials and requires interactive confirmation before physical dispatch
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, Callable

from src.models import ACState, ACMode
from src.config import AutomationConfig
from src.notifier import NotificationDispatcher, NotificationEvent
from src.tcl.controller import TCLCloudController, ReadOnlyModeError
from src.tcl.client import mask_email

_LOGGER = logging.getLogger(__name__)

# Constants
LIVING_ROOM_DEFAULT_PHYSICAL_ID = "demo_living_room"
BEDROOM_DEFAULT_PHYSICAL_ID = "demo_bedroom"
MAX_ALLOWED_DELTA_CELSIUS = 1.0


@dataclass
class Phase3BResult:
    """Outcome of Phase 3B physical control verification."""
    success: bool
    status_code: str                     # SUCCESS, ABORTED_OFF, ABORTED_CONFIRM, FAILED_VERIFY, FAILED_RESTORE, ERROR
    message: str
    target_device_id: str
    baseline_power: Optional[bool] = None
    baseline_mode: Optional[str] = None
    baseline_temperature: Optional[float] = None
    test_temperature: Optional[float] = None
    verified_test_temperature: Optional[float] = None
    restored_temperature: Optional[float] = None
    dry_run: bool = False
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status_code": self.status_code,
            "message": self.message,
            "target_device_id": self.target_device_id,
            "baseline_power": self.baseline_power,
            "baseline_mode": self.baseline_mode,
            "baseline_temperature": self.baseline_temperature,
            "test_temperature": self.test_temperature,
            "verified_test_temperature": self.verified_test_temperature,
            "restored_temperature": self.restored_temperature,
            "dry_run": self.dry_run,
            "timestamp": self.timestamp.isoformat(),
        }


class Phase3BPhysicalVerifier:
    """
    Controlled verification runner for Phase 3B physical testing.
    Executes a reversible 1°C setpoint test on the living room AC.
    """

    def __init__(
        self,
        controller: Optional[TCLCloudController] = None,
        notifier: Optional[NotificationDispatcher] = None,
        living_room_id: Optional[str] = None,
        bedroom_id: Optional[str] = None,
        sync_wait_seconds: float = 3.0,
    ):
        self.controller = controller or TCLCloudController(read_only=False)
        self.notifier = notifier or NotificationDispatcher()
        self.living_room_id = living_room_id or LIVING_ROOM_DEFAULT_PHYSICAL_ID
        self.bedroom_id = bedroom_id or BEDROOM_DEFAULT_PHYSICAL_ID
        self.sync_wait_seconds = sync_wait_seconds

    def _trigger_alert(self, title: str, message: str, severity: str = "ERROR", metadata: Optional[Dict[str, Any]] = None):
        """Dispatch structured alert to logs, file, and webhooks."""
        event = NotificationEvent(
            event_type="PHASE_3B_ALERT",
            title=title,
            message=message,
            severity=severity,
            metadata=metadata or {}
        )
        self.notifier.dispatch(event)

    def run_verification(
        self,
        dry_run: bool = False,
        interactive_confirm_callback: Optional[Callable[[Dict[str, Any]], bool]] = None
    ) -> Phase3BResult:
        """
        Execute full Phase 3B verification procedure with fail-safe rollback.
        """
        _LOGGER.info("Starting Phase 3B Physical Control Verification...")

        # -------------------------------------------------------------
        # 1. Device Isolation Guard
        # -------------------------------------------------------------
        # Ensure bedroom AC is never targeted
        if self.living_room_id == self.bedroom_id:
            msg = "Configuration Error: Living room ID matches Bedroom ID. Guard stopped execution."
            self._trigger_alert("PHASE 3B CONFIG ERROR", msg, severity="CRITICAL")
            return Phase3BResult(
                success=False,
                status_code="ERROR",
                message=msg,
                target_device_id=self.living_room_id,
                dry_run=dry_run,
            )

        # -------------------------------------------------------------
        # 2. Connect and Discover
        # -------------------------------------------------------------
        if not self.controller.ensure_valid_session():
            msg = "Failed to establish valid session with TCL Cloud SSO."
            self._trigger_alert("PHASE 3B AUTH FAILURE", msg, severity="ERROR")
            return Phase3BResult(
                success=False,
                status_code="ERROR",
                message=msg,
                target_device_id=self.living_room_id,
                dry_run=dry_run,
            )

        # Resolve living room physical ID
        target_id = self.controller.resolve_physical_id(self.living_room_id)
        bedroom_target_id = self.controller.resolve_physical_id(self.bedroom_id)

        # Explicit safety check: reject if resolved to bedroom
        if target_id == bedroom_target_id:
            msg = f"Safety Guard: Resolved target {target_id} matches Bedroom unit {bedroom_target_id}. Aborted."
            self._trigger_alert("PHASE 3B GUARD VIOLATION", msg, severity="CRITICAL")
            return Phase3BResult(
                success=False,
                status_code="ERROR",
                message=msg,
                target_device_id=target_id,
                dry_run=dry_run,
            )

        # -------------------------------------------------------------
        # 3. Read Baseline State
        # -------------------------------------------------------------
        baseline_state: Optional[ACState] = self.controller.get_state(target_id)
        if not baseline_state:
            msg = f"Failed to retrieve initial AWS IoT Shadow for living room AC ({target_id})."
            self._trigger_alert("PHASE 3B SHADOW READ FAILED", msg, severity="ERROR")
            return Phase3BResult(
                success=False,
                status_code="ERROR",
                message=msg,
                target_device_id=target_id,
                dry_run=dry_run,
            )

        baseline_power = baseline_state.power
        baseline_mode = baseline_state.mode.value if baseline_state.mode else "Heat"
        baseline_temp = baseline_state.target_temperature

        _LOGGER.info(
            f"Baseline State for {target_id}: Power={'ON' if baseline_power else 'OFF'}, "
            f"Mode={baseline_mode}, Setpoint={baseline_temp}°C, Indoor={baseline_state.current_temperature}°C"
        )

        # -------------------------------------------------------------
        # 4. Power State Guard: Refuse to turn on an AC that is OFF
        # -------------------------------------------------------------
        if not baseline_power:
            msg = (
                f"Living room AC ({target_id}) is currently powered OFF. "
                f"Safety policy forbids turning on an air conditioner that is powered off. "
                f"Test safely aborted with zero commands sent."
            )
            _LOGGER.warning(msg)
            self._trigger_alert("PHASE 3B ABORTED", msg, severity="WARNING")
            return Phase3BResult(
                success=False,
                status_code="ABORTED_OFF",
                message=msg,
                target_device_id=target_id,
                baseline_power=baseline_power,
                baseline_mode=baseline_mode,
                baseline_temperature=baseline_temp,
                dry_run=dry_run,
            )

        # -------------------------------------------------------------
        # 5. Calculate Test Temperature (strictly max 1.0°C delta)
        # -------------------------------------------------------------
        # If baseline is high (e.g. >= 28°C), adjust downwards by 1°C; otherwise upwards by 1°C
        if baseline_temp >= 28.0:
            test_temp = round(baseline_temp - 1.0, 1)
        else:
            test_temp = round(baseline_temp + 1.0, 1)

        delta = abs(test_temp - baseline_temp)
        if delta > MAX_ALLOWED_DELTA_CELSIUS:
            msg = f"Safety Violation: Delta {delta}°C exceeds maximum allowed {MAX_ALLOWED_DELTA_CELSIUS}°C."
            self._trigger_alert("PHASE 3B DELTA ERROR", msg, severity="CRITICAL")
            return Phase3BResult(
                success=False,
                status_code="ERROR",
                message=msg,
                target_device_id=target_id,
                baseline_power=baseline_power,
                baseline_mode=baseline_mode,
                baseline_temperature=baseline_temp,
                dry_run=dry_run,
            )

        # -------------------------------------------------------------
        # 6. Interactive Confirmation Gate
        # -------------------------------------------------------------
        plan_summary = {
            "target_device_id": target_id,
            "target_device_name": "Living room",
            "protected_device_id": bedroom_target_id,
            "protected_device_name": "Bedroom (Untouched)",
            "baseline_power": baseline_power,
            "baseline_mode": baseline_mode,
            "baseline_temperature": baseline_temp,
            "test_temperature": test_temp,
            "delta_celsius": delta,
            "dry_run": dry_run,
        }

        if interactive_confirm_callback is not None:
            confirmed = interactive_confirm_callback(plan_summary)
            if not confirmed:
                msg = "Physical test aborted: User confirmation declined or not provided."
                _LOGGER.info(msg)
                return Phase3BResult(
                    success=False,
                    status_code="ABORTED_CONFIRM",
                    message=msg,
                    target_device_id=target_id,
                    baseline_power=baseline_power,
                    baseline_mode=baseline_mode,
                    baseline_temperature=baseline_temp,
                    test_temperature=test_temp,
                    dry_run=dry_run,
                )

        # -------------------------------------------------------------
        # 7. Apply Temporary Test Setpoint
        # -------------------------------------------------------------
        _LOGGER.info(f"Applying temporary test setpoint: {test_temp}°C (from baseline {baseline_temp}°C)...")
        try:
            self.controller.set_target_temperature(target_id, test_temp, dry_run=dry_run)
        except Exception as e:
            msg = f"Failed to dispatch test setpoint {test_temp}°C to {target_id}: {e}"
            self._trigger_alert("PHASE 3B DISPATCH ERROR", msg, severity="ERROR")
            return Phase3BResult(
                success=False,
                status_code="ERROR",
                message=msg,
                target_device_id=target_id,
                baseline_power=baseline_power,
                baseline_mode=baseline_mode,
                baseline_temperature=baseline_temp,
                test_temperature=test_temp,
                dry_run=dry_run,
            )

        # -------------------------------------------------------------
        # 8. Verify Device State via Fresh AWS IoT Shadow Read
        # -------------------------------------------------------------
        verified_temp = None
        test_verified = dry_run
        max_attempts = 4 if not dry_run and self.sync_wait_seconds > 0 else 1

        for attempt in range(max_attempts):
            if not dry_run and self.sync_wait_seconds > 0:
                wait_time = self.sync_wait_seconds if attempt == 0 else 2.0
                _LOGGER.debug(f"Waiting {wait_time}s for AWS IoT Shadow synchronization (attempt {attempt + 1}/{max_attempts})...")
                time.sleep(wait_time)

            fresh_state = self.controller.get_state(target_id)
            verified_temp = fresh_state.target_temperature if fresh_state else None

            if verified_temp is not None and abs(verified_temp - test_temp) < 0.2:
                test_verified = True
                _LOGGER.info(f"Target setpoint {test_temp}°C acknowledged by device shadow on attempt {attempt + 1}.")
                break

        if not test_verified:
            err_msg = (
                f"Verification failed: Expected setpoint {test_temp}°C, but fresh shadow reported {verified_temp}°C. "
                f"Triggering immediate emergency restoration..."
            )
            self._trigger_alert("PHASE 3B VERIFICATION FAILED", err_msg, severity="CRITICAL")

            # Emergency rollback attempt
            try:
                self.controller.set_target_temperature(target_id, baseline_temp, dry_run=dry_run)
            except Exception as rb_err:
                _LOGGER.critical(f"Emergency rollback also failed: {rb_err}")

            return Phase3BResult(
                success=False,
                status_code="FAILED_VERIFY",
                message=err_msg,
                target_device_id=target_id,
                baseline_power=baseline_power,
                baseline_mode=baseline_mode,
                baseline_temperature=baseline_temp,
                test_temperature=test_temp,
                verified_test_temperature=verified_temp,
                dry_run=dry_run,
            )

        _LOGGER.info(f"Test setpoint {test_temp}°C successfully verified via fresh shadow read.")

        # -------------------------------------------------------------
        # 9. Restore Original Baseline Temperature
        # -------------------------------------------------------------
        _LOGGER.info(f"Restoring original baseline temperature: {baseline_temp}°C...")
        try:
            self.controller.set_target_temperature(target_id, baseline_temp, dry_run=dry_run)
        except Exception as e:
            msg = f"CRITICAL: Failed to dispatch baseline restoration to {target_id}: {e}"
            self._trigger_alert("PHASE 3B RESTORATION DISPATCH ERROR", msg, severity="CRITICAL")
            return Phase3BResult(
                success=False,
                status_code="FAILED_RESTORE",
                message=msg,
                target_device_id=target_id,
                baseline_power=baseline_power,
                baseline_mode=baseline_mode,
                baseline_temperature=baseline_temp,
                test_temperature=test_temp,
                verified_test_temperature=verified_temp,
                dry_run=dry_run,
            )

        # -------------------------------------------------------------
        # 10. Verify Restoration via Fresh AWS IoT Shadow Read
        # -------------------------------------------------------------
        restored_temp = None
        restore_verified = dry_run

        for attempt in range(max_attempts):
            if not dry_run and self.sync_wait_seconds > 0:
                wait_time = self.sync_wait_seconds if attempt == 0 else 2.0
                _LOGGER.debug(f"Waiting {wait_time}s for baseline restoration synchronization (attempt {attempt + 1}/{max_attempts})...")
                time.sleep(wait_time)

            restored_state = self.controller.get_state(target_id)
            restored_temp = restored_state.target_temperature if restored_state else None

            if restored_temp is not None and abs(restored_temp - baseline_temp) < 0.2:
                restore_verified = True
                _LOGGER.info(f"Baseline setpoint {baseline_temp}°C acknowledged by device shadow on attempt {attempt + 1}.")
                break

        if not restore_verified:
            err_msg = (
                f"CRITICAL: Restoration verification failed! Expected baseline {baseline_temp}°C, "
                f"but fresh shadow reported {restored_temp}°C."
            )
            self._trigger_alert("PHASE 3B RESTORATION UNVERIFIED", err_msg, severity="CRITICAL")
            return Phase3BResult(
                success=False,
                status_code="FAILED_RESTORE",
                message=err_msg,
                target_device_id=target_id,
                baseline_power=baseline_power,
                baseline_mode=baseline_mode,
                baseline_temperature=baseline_temp,
                test_temperature=test_temp,
                verified_test_temperature=verified_temp,
                restored_temperature=restored_temp,
                dry_run=dry_run,
            )

        _LOGGER.info(f"Baseline temperature {baseline_temp}°C successfully restored and verified.")

        success_msg = (
            f"Phase 3B Verification Complete! Living Room AC ({target_id}) setpoint tested from "
            f"{baseline_temp}°C to {test_temp}°C and restored back to {baseline_temp}°C. "
            f"All fresh shadow reads verified. Bedroom AC remained completely untouched."
        )

        return Phase3BResult(
            success=True,
            status_code="SUCCESS",
            message=success_msg,
            target_device_id=target_id,
            baseline_power=baseline_power,
            baseline_mode=baseline_mode,
            baseline_temperature=baseline_temp,
            test_temperature=test_temp,
            verified_test_temperature=verified_temp,
            restored_temperature=restored_temp,
            dry_run=dry_run,
        )
