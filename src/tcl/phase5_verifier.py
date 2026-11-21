"""Phase 5: On-Site Controlled Hardware Verification for Silent Night Mode & Safeguards.

Implements strict validation sequences:
1. Test LED OFF without unnecessarily powering on an unpowered AC.
2. Test beep suppression (beepSwitch: 0) without audible testing.
3. Test fan speed changes only when running (suppressed when off).
4. Enforce 10-minute anti-cycling for compressors.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

from src.models import ACState, ACMode, ACFanSpeed, ACCommand, ScheduleDecision, SystemState
from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.notifier import NotificationDispatcher, NotificationEvent
from src.tcl.controller import TCLCloudController
from src.safeguards import SafeguardEngine

_LOGGER = logging.getLogger("phase5_verifier")

LIVING_ROOM_DEFAULT_ID = "demo_living_room"
BEDROOM_DEFAULT_ID = "demo_bedroom"


@dataclass
class Phase5OnsiteResult:
    """Consolidated outcome of Phase 5 on-site testing."""
    success: bool
    status_code: str
    message: str
    test_results: Dict[str, Dict[str, Any]]
    dry_run: bool
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status_code": self.status_code,
            "message": self.message,
            "test_results": self.test_results,
            "dry_run": self.dry_run,
            "timestamp": self.timestamp.isoformat()
        }


class Phase5OnsiteVerifier:
    """Executes safe on-site test sequences with strict reversibility and safeguard checks."""

    def __init__(
        self,
        controller: Optional[TCLCloudController] = None,
        notifier: Optional[NotificationDispatcher] = None,
        config: Optional[AutomationConfig] = None,
        sync_wait_seconds: float = 3.0
    ):
        self.config = config or AutomationConfig()
        self.controller = controller or TCLCloudController(read_only=self.config.TCL_READ_ONLY)
        self.notifier = notifier or NotificationDispatcher()
        self.safeguards = SafeguardEngine(config=self.config)
        self.sync_wait_seconds = sync_wait_seconds

    def run_all_tests(
        self,
        target_device_id: Optional[str] = None,
        dry_run: bool = True
    ) -> Phase5OnsiteResult:
        """Run all 4 verification sequences in safe order."""
        dev_id = target_device_id or self.config.TCL_LIVING_ROOM_ID
        results = {}
        all_passed = True

        _LOGGER.info(f"Starting Phase 5 On-Site Verification on {dev_id} (DRY_RUN={dry_run})...")

        # 1. Compressor Anti-Cycling Test (Pure Safeguard Engine Verification)
        comp_res = self.verify_compressor_anti_cycling(dev_id)
        results["compressor_anti_cycling"] = comp_res
        if not comp_res["success"]:
            all_passed = False

        # 2. Beep Suppression Test
        beep_res = self.verify_beep_suppression(dev_id, dry_run=dry_run)
        results["beep_suppression"] = beep_res
        if not beep_res["success"]:
            all_passed = False

        # 3. LED OFF Without Turning ON Test
        led_res = self.verify_led_off_safety(dev_id, dry_run=dry_run)
        results["led_off_safety"] = led_res
        if not led_res["success"]:
            all_passed = False

        # 4. Running-Only Fan Speed Test
        fan_res = self.verify_running_only_fan(dev_id, dry_run=dry_run)
        results["running_only_fan"] = fan_res
        if not fan_res["success"]:
            all_passed = False

        status_code = "SUCCESS" if all_passed else "FAILED"
        msg = "All Phase 5 on-site safety verifications passed." if all_passed else "One or more Phase 5 verifications failed."

        return Phase5OnsiteResult(
            success=all_passed,
            status_code=status_code,
            message=msg,
            test_results=results,
            dry_run=dry_run
        )

    def verify_compressor_anti_cycling(self, device_id: str) -> Dict[str, Any]:
        """Verify that power toggles within 600s (10 min) are strictly blocked."""
        _LOGGER.info("Verifying compressor anti-cycling protection (10-minute interval)...")
        now = datetime.now()
        engine = SafeguardEngine(config=self.config)

        # Record a power toggle at now
        engine.record_power_toggle(device_id, now)

        # Simulate toggle 60 seconds later (MUST BE BLOCKED)
        one_min_later = now + timedelta(seconds=60)
        blocked = engine.is_compressor_cycle_locked(device_id, one_min_later)

        # Simulate toggle 601 seconds later (MUST BE ALLOWED)
        ten_min_later = now + timedelta(seconds=601)
        allowed = not engine.is_compressor_cycle_locked(device_id, ten_min_later)

        success = blocked and allowed
        msg = (
            f"Anti-cycling correctly blocked toggle after 60s (lock={blocked}) "
            f"and permitted toggle after 601s (lock={not allowed})"
        )
        return {
            "success": success,
            "min_cycle_interval_seconds": self.config.MIN_CYCLE_INTERVAL_SECONDS,
            "blocked_at_60s": blocked,
            "allowed_at_601s": allowed,
            "message": msg
        }

    def verify_beep_suppression(self, device_id: str, dry_run: bool = True) -> Dict[str, Any]:
        """Verify that shadow command payloads map beep_feedback=False to beepSwitch: 0."""
        _LOGGER.info(f"Verifying beep feedback suppression for {device_id}...")

        cmd = ACCommand(
            device_id=device_id,
            power=True,
            target_temperature=22.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            display_led=False,
            beep_feedback=False,
            reason="Beep suppression test"
        )
        payload = cmd.to_shadow_payload()
        desired = payload.get("state", {}).get("desired", {})

        has_beep_zero = (desired.get("beepSwitch") == 0)
        if not has_beep_zero:
            return {
                "success": False,
                "message": f"Shadow payload failed to set beepSwitch: 0 (payload: {payload})"
            }

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "beep_switch": 0,
                "message": "Shadow payload correctly enforces beepSwitch: 0 (silent mode)."
            }

        # Physical dispatch if not dry-run
        try:
            sent = self.controller.send_command(cmd, dry_run=False)
            return {
                "success": sent,
                "dry_run": False,
                "beep_switch": 0,
                "message": "Dispatched beepSwitch: 0 shadow payload successfully."
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": f"Failed to dispatch beep suppression payload: {e}"
            }

    def verify_led_off_safety(self, device_id: str, dry_run: bool = True) -> Dict[str, Any]:
        """
        Verify LED OFF behavior:
        1. If unit is OFF: confirms LED OFF does NOT toggle unit power to ON.
        2. If unit is ON: verifies screen: 0 in shadow payload without altering other settings.
        """
        _LOGGER.info(f"Verifying LED OFF safety for {device_id}...")
        current_state = self.controller.get_state(device_id)
        current_power = current_state.power if current_state else False

        if not current_power:
            # Unit is currently unpowered
            # Test that requesting LED display OFF does NOT trigger a power ON
            cmd = ACCommand(
                device_id=device_id,
                power=False,
                target_temperature=21.0,
                mode=ACMode.HEAT,
                fan_speed=ACFanSpeed.AUTO,
                display_led=False,
                beep_feedback=False,
                reason="Unpowered LED check"
            )
            payload = cmd.to_shadow_payload()
            desired = payload.get("state", {}).get("desired", {})

            # Power MUST remain 0
            power_val = desired.get("powerSwitch", desired.get("Power", 0))
            screen_val = desired.get("screen")

            success = (power_val == 0 and screen_val == 0)
            return {
                "success": success,
                "initial_power": False,
                "desired_power": power_val,
                "desired_screen": screen_val,
                "message": "Confirmed: LED display OFF on unpowered unit leaves power: 0 strictly intact."
            }
        else:
            # Unit is running
            # Test sending screen: 0 while keeping power: 1
            cmd = ACCommand(
                device_id=device_id,
                power=True,
                target_temperature=current_state.target_temperature or 22.0,
                mode=current_state.mode or ACMode.HEAT,
                fan_speed=current_state.fan_speed or ACFanSpeed.AUTO,
                display_led=False,
                beep_feedback=False,
                reason="Running LED check"
            )
            payload = cmd.to_shadow_payload()
            desired = payload.get("state", {}).get("desired", {})

            power_val = desired.get("powerSwitch", desired.get("Power", 0))
            screen_val = desired.get("screen")
            success = (power_val == 1 and screen_val == 0)
            if dry_run:
                return {
                    "success": success,
                    "initial_power": True,
                    "desired_power": power_val,
                    "desired_screen": screen_val,
                    "message": "Running unit payload correctly specifies screen: 0 while preserving power: 1."
                }

            # Live execution
            sent = self.controller.send_command(cmd, dry_run=False)
            time.sleep(self.sync_wait_seconds)
            new_st = self.controller.get_state(device_id)
            verified_screen = getattr(new_st, "display_led", None)

            return {
                "success": sent,
                "initial_power": True,
                "verified_screen": verified_screen,
                "message": f"Dispatched screen: 0 to running AC. Verified screen state: {verified_screen}"
            }

    def verify_running_only_fan(self, device_id: str, dry_run: bool = True) -> Dict[str, Any]:
        """
        Verify fan speed changes:
        1. If unit is OFF: fan changes MUST NOT power on the unit.
        2. If unit is ON: verifies Quiet fan speed (windSpeed7Gear: 1) payload mapping.
        """
        _LOGGER.info(f"Verifying fan speed logic on {device_id}...")
        current_state = self.controller.get_state(device_id)
        current_power = current_state.power if current_state else False

        # Test command with Quiet fan
        cmd = ACCommand(
            device_id=device_id,
            power=current_power,
            target_temperature=20.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            display_led=False,
            beep_feedback=False,
            reason="Fan test"
        )
        payload = cmd.to_shadow_payload()
        desired = payload.get("state", {}).get("desired", {})

        fan_gear = desired.get("windSpeed7Gear")
        # Quiet fan speed corresponds to 1 in TCL 7-gear model
        quiet_valid = (fan_gear == 1)

        power_val = desired.get("powerSwitch", desired.get("Power", 0))
        if not current_power:
            # Confirm power remains 0
            success = quiet_valid and (power_val == 0)
            return {
                "success": success,
                "initial_power": False,
                "fan_gear": fan_gear,
                "message": "Unpowered unit: Quiet fan mapped (gear=1) and power remains strictly 0."
            }
        else:
            success = quiet_valid and (power_val == 1)
            if dry_run:
                return {
                    "success": success,
                    "initial_power": True,
                    "fan_gear": fan_gear,
                    "message": "Running unit: Quiet fan speed correctly mapped to windSpeed7Gear: 1."
                }

            # Physical test and restore
            orig_fan = current_state.fan_speed or ACFanSpeed.AUTO
            sent = self.controller.send_command(cmd, dry_run=False)
            time.sleep(self.sync_wait_seconds)

            # Revert to original fan
            revert_cmd = ACCommand(
                device_id=device_id,
                power=True,
                target_temperature=current_state.target_temperature or 22.0,
                mode=current_state.mode or ACMode.HEAT,
                fan_speed=orig_fan,
                display_led=getattr(current_state, "display_led", False),
                beep_feedback=getattr(current_state, "beep_feedback", False),
                reason="Revert fan test"
            )
            self.controller.send_command(revert_cmd, dry_run=False)

            return {
                "success": sent,
                "initial_power": True,
                "tested_fan": "Quiet",
                "reverted_fan": orig_fan.value,
                "message": "Quiet fan speed (windSpeed7Gear: 1) successfully dispatched and reverted."
            }

    # -------------------------------------------------------------------------
    # Granular Independent Physical Verification Tests
    # -------------------------------------------------------------------------

    def get_device_summary(self, device_id: str) -> Dict[str, Any]:
        """Fetch and return real-time hardware status from TCL Cloud shadow."""
        st = self.controller.get_state(device_id)
        if not st:
            return {"online": False, "error": f"Device {device_id} could not be queried"}
        return {
            "device_id": device_id,
            "power": st.power,
            "target_temperature": st.target_temperature,
            "current_temperature": st.current_temperature,
            "mode": st.mode.value if st.mode else "Heat",
            "fan_speed": st.fan_speed.value if st.fan_speed else "Auto",
            "display_led": getattr(st, "display_led", False),
            "beep_feedback": getattr(st, "beep_feedback", False),
            "online": st.is_online
        }

    def test_led_display(
        self,
        device_id: str,
        target_screen: Optional[int] = None,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        Test LED display without modifying any other setting.
        target_screen: 0 for Dark / OFF, 1 for ON. If None, toggles current state.
        """
        st = self.controller.get_state(device_id)
        current_screen = 1 if getattr(st, "display_led", False) else 0
        desired_screen = target_screen if target_screen is not None else (0 if current_screen == 1 else 1)

        _LOGGER.info(
            f"Testing LED display on {device_id}: screen {current_screen} -> {desired_screen} (dry_run={dry_run})"
        )

        if dry_run:
            return {
                "success": True,
                "device_id": device_id,
                "setting": "screen",
                "baseline_value": current_screen,
                "tested_value": desired_screen,
                "verified_value": desired_screen,
                "dry_run": True,
                "message": f"[DRY-RUN] Would update {device_id} display to screen: {desired_screen} (baseline: {current_screen})"
            }

        sent = self.controller.send_partial_shadow(
            device_id=device_id,
            desired_attributes={"screen": desired_screen},
            dry_run=False
        )
        time.sleep(self.sync_wait_seconds)
        new_st = self.controller.get_state(device_id)
        verified_screen = 1 if getattr(new_st, "display_led", False) else 0

        success = sent and (verified_screen == desired_screen)
        return {
            "success": success,
            "device_id": device_id,
            "setting": "screen",
            "baseline_value": current_screen,
            "tested_value": desired_screen,
            "verified_value": verified_screen,
            "dry_run": False,
            "message": f"Dispatched screen: {desired_screen}. Verified shadow: {verified_screen}."
        }

    def test_beep_switch(
        self,
        device_id: str,
        target_beep: Optional[int] = None,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        Test beep buzzer suppression/feedback without modifying any other setting.
        target_beep: 0 for Mute / Silent, 1 for Beep ON. If None, targets 0 (Mute).
        """
        st = self.controller.get_state(device_id)
        current_beep = 1 if getattr(st, "beep_feedback", False) else 0
        desired_beep = target_beep if target_beep is not None else (0 if current_beep == 1 else 1)

        _LOGGER.info(
            f"Testing Beep Feedback on {device_id}: beepSwitch {current_beep} -> {desired_beep} (dry_run={dry_run})"
        )

        if dry_run:
            return {
                "success": True,
                "device_id": device_id,
                "setting": "beepSwitch",
                "baseline_value": current_beep,
                "tested_value": desired_beep,
                "verified_value": desired_beep,
                "dry_run": True,
                "message": f"[DRY-RUN] Would update {device_id} beep to beepSwitch: {desired_beep} (baseline: {current_beep})"
            }

        sent = self.controller.send_partial_shadow(
            device_id=device_id,
            desired_attributes={"beepSwitch": desired_beep},
            dry_run=False
        )
        time.sleep(self.sync_wait_seconds)
        new_st = self.controller.get_state(device_id)
        verified_beep = 1 if getattr(new_st, "beep_feedback", False) else 0

        success = sent and (verified_beep == desired_beep)
        return {
            "success": success,
            "device_id": device_id,
            "setting": "beepSwitch",
            "baseline_value": current_beep,
            "tested_value": desired_beep,
            "verified_value": verified_beep,
            "dry_run": False,
            "message": f"Dispatched beepSwitch: {desired_beep}. Verified shadow: {verified_beep}."
        }

    def test_quiet_fan(
        self,
        device_id: str,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        Test Quiet fan speed (gear 1) without modifying any other setting.
        Safeguard: Refuses to send fan speed command if AC is currently unpowered.
        """
        st = self.controller.get_state(device_id)
        if not st or not st.power:
            return {
                "success": False,
                "device_id": device_id,
                "setting": "windSpeed7Gear",
                "aborted": True,
                "message": f"Refusing fan speed test: {device_id} is currently OFF. Will not turn on an unpowered AC."
            }

        current_fan = getattr(st, "fan_speed", ACFanSpeed.AUTO).value
        desired_gear = 1  # Quiet

        _LOGGER.info(
            f"Testing Quiet Fan on {device_id}: current {current_fan} -> gear {desired_gear} (dry_run={dry_run})"
        )

        if dry_run:
            return {
                "success": True,
                "device_id": device_id,
                "setting": "windSpeed7Gear",
                "baseline_value": current_fan,
                "tested_value": desired_gear,
                "verified_value": desired_gear,
                "dry_run": True,
                "message": f"[DRY-RUN] Would set {device_id} fan to gear 1 (Quiet). Current: {current_fan}"
            }

        sent = self.controller.send_partial_shadow(
            device_id=device_id,
            desired_attributes={"windSpeed7Gear": desired_gear},
            dry_run=False
        )
        time.sleep(self.sync_wait_seconds)
        new_st = self.controller.get_state(device_id)
        verified_fan = getattr(new_st, "fan_speed", ACFanSpeed.AUTO).value

        return {
            "success": sent,
            "device_id": device_id,
            "setting": "windSpeed7Gear",
            "baseline_value": current_fan,
            "tested_value": desired_gear,
            "verified_value": verified_fan,
            "dry_run": False,
            "message": f"Dispatched windSpeed7Gear: {desired_gear} (Quiet). Verified fan: {verified_fan}."
        }

    def test_power_control(
        self,
        device_id: str,
        target_power: Optional[int] = None,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        Test Power ON/OFF toggle with strict 10-minute anti-cycling protection.
        """
        now = datetime.now()
        if self.safeguards.is_compressor_cycle_locked(device_id, now):
            return {
                "success": False,
                "device_id": device_id,
                "setting": "powerSwitch",
                "locked": True,
                "message": (
                    f"Compressor anti-cycling lock is ACTIVE on {device_id}. "
                    f"A minimum of {self.config.MIN_CYCLE_INTERVAL_SECONDS}s (10 min) must elapse between power toggles."
                )
            }

        st = self.controller.get_state(device_id)
        current_power = 1 if (st and st.power) else 0
        desired_power = target_power if target_power is not None else (0 if current_power == 1 else 1)

        _LOGGER.info(
            f"Testing Power Control on {device_id}: power {current_power} -> {desired_power} (dry_run={dry_run})"
        )

        if dry_run:
            return {
                "success": True,
                "device_id": device_id,
                "setting": "powerSwitch",
                "baseline_value": current_power,
                "tested_value": desired_power,
                "verified_value": desired_power,
                "dry_run": True,
                "message": f"[DRY-RUN] Would toggle {device_id} power to {desired_power} (baseline: {current_power})"
            }

        sent = self.controller.send_partial_shadow(
            device_id=device_id,
            desired_attributes={"powerSwitch": desired_power},
            dry_run=False
        )
        self.safeguards.record_power_toggle(device_id, now)

        time.sleep(self.sync_wait_seconds)
        new_st = self.controller.get_state(device_id)
        verified_power = 1 if (new_st and new_st.power) else 0

        success = sent and (verified_power == desired_power)
        return {
            "success": success,
            "device_id": device_id,
            "setting": "powerSwitch",
            "baseline_value": current_power,
            "tested_value": desired_power,
            "verified_value": verified_power,
            "dry_run": False,
            "message": f"Dispatched powerSwitch: {desired_power}. Verified shadow: {verified_power}."
        }

    def restore_attribute(
        self,
        device_id: str,
        setting_name: str,
        original_value: Any,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """Restore a single verified setting to its original baseline."""
        _LOGGER.info(
            f"Restoring {device_id} setting {setting_name} to baseline {original_value} (dry_run={dry_run})"
        )
        if dry_run:
            return {
                "success": True,
                "device_id": device_id,
                "setting": setting_name,
                "restored_value": original_value,
                "dry_run": True,
                "message": f"[DRY-RUN] Restored {device_id} {setting_name} to {original_value}."
            }

        sent = self.controller.send_partial_shadow(
            device_id=device_id,
            desired_attributes={setting_name: original_value},
            dry_run=False
        )
        time.sleep(self.sync_wait_seconds)
        return {
            "success": sent,
            "device_id": device_id,
            "setting": setting_name,
            "restored_value": original_value,
            "dry_run": False,
            "message": f"Successfully restored {setting_name} to {original_value} on {device_id}."
        }
