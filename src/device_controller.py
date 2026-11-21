"""Device Controller abstractions: Mock simulator, TCL Cloud adapter, and extensible Shelly Cloud stub."""

import abc
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any

from src.models import ACCommand, ACState, ACMode, ACFanSpeed
from src.config import AutomationConfig
from src.base_controller import BaseACController, BaseHVACDeviceController, DeviceProviderType
from src.tcl.controller import TCLCloudController, ReadOnlyModeError

_LOGGER = logging.getLogger(__name__)


class MockACController(BaseACController):
    """In-memory mock controller simulating physical TCL AC units for development and testing."""

    @property
    def provider_type(self) -> DeviceProviderType:
        return DeviceProviderType.MOCK

    def __init__(self, device_ids: Optional[List[str]] = None):
        self.device_ids = device_ids or AutomationConfig.DEVICE_IDS
        self.states: Dict[str, ACState] = {}
        self.command_history: List[Dict[str, Any]] = []
        self._simulate_offline: Dict[str, bool] = {}
        self._simulate_failure: Dict[str, bool] = {}

        # Initialize mock units in OFF state (default cold start)
        for dev_id in self.device_ids:
            self.states[dev_id] = ACState(
                device_id=dev_id,
                power=False,
                target_temperature=21.0,
                current_temperature=19.5,
                mode=ACMode.OFF,
                fan_speed=ACFanSpeed.AUTO,
                eco_mode=False,
                is_online=True,
                last_updated=datetime.now()
            )

    def set_offline(self, device_id: str, is_offline: bool) -> None:
        """Inject an offline hardware state for testing."""
        self._simulate_offline[device_id] = is_offline
        if device_id in self.states:
            self.states[device_id].is_online = not is_offline

    def set_failure(self, device_id: str, will_fail: bool) -> None:
        """Inject a communication failure for testing."""
        self._simulate_failure[device_id] = will_fail

    def get_state(self, device_id: str) -> Optional[ACState]:
        if self._simulate_offline.get(device_id, False):
            return None
        return self.states.get(device_id)

    def get_all_states(self) -> Dict[str, ACState]:
        return {
            dev_id: state
            for dev_id, state in self.states.items()
            if not self._simulate_offline.get(dev_id, False)
        }

    def send_command(self, command: ACCommand, dry_run: bool = False) -> bool:
        """Simulate sending a command to an AC unit."""
        dev_id = command.device_id
        timestamp = datetime.now()

        if self._simulate_offline.get(dev_id, False):
            _LOGGER.error(f"[MOCK] Cannot send command to {dev_id}: Device is OFFLINE")
            self.command_history.append({
                "timestamp": timestamp.isoformat(),
                "device_id": dev_id,
                "command": command.to_shadow_payload(),
                "success": False,
                "error": "Device offline",
                "dry_run": dry_run
            })
            return False

        if self._simulate_failure.get(dev_id, False):
            _LOGGER.error(f"[MOCK] Injected communication failure for {dev_id}")
            self.command_history.append({
                "timestamp": timestamp.isoformat(),
                "device_id": dev_id,
                "command": command.to_shadow_payload(),
                "success": False,
                "error": "Injected communication failure",
                "dry_run": dry_run
            })
            return False

        if dry_run:
            _LOGGER.info(
                f"[DRY-RUN / MOCK] Simulated command for {dev_id}: "
                f"Power={command.power}, Temp={command.target_temperature}°C, "
                f"Mode={command.mode.value}, Fan={command.fan_speed.value} | Reason: {command.reason}"
            )
        else:
            state = self.states[dev_id]
            state.power = command.power
            state.target_temperature = command.target_temperature
            state.mode = command.mode
            state.fan_speed = command.fan_speed
            state.eco_mode = command.eco_mode
            state.display_led = command.display_led
            state.beep_feedback = command.beep_feedback
            state.last_updated = timestamp
            _LOGGER.info(
                f"[MOCK DISPATCH] Successfully applied state to {dev_id}: "
                f"Power={command.power}, Temp={command.target_temperature}°C, Mode={command.mode.value}, "
                f"LED={command.display_led}, Beep={command.beep_feedback}"
            )

        self.command_history.append({
            "timestamp": timestamp.isoformat(),
            "device_id": dev_id,
            "command": command.to_shadow_payload(),
            "success": True,
            "dry_run": dry_run,
            "reason": command.reason
        })
        return True

    def send_partial_shadow(
        self,
        device_id: str,
        desired_attributes: Dict[str, Any],
        dry_run: bool = False
    ) -> bool:
        """Simulate selective shadow attribute update."""
        if self._simulate_offline.get(device_id, False) or self._simulate_failure.get(device_id, False):
            return False

        attrs = dict(desired_attributes)
        if "beepSwitch" not in attrs:
            attrs["beepSwitch"] = 1 if AutomationConfig.BEEP_FEEDBACK_ENABLED else 0

        if not dry_run and device_id in self.states:
            st = self.states[device_id]
            if "screen" in attrs:
                st.display_led = bool(attrs["screen"])
            if "beepSwitch" in attrs:
                st.beep_feedback = bool(attrs["beepSwitch"])
            if "powerSwitch" in attrs:
                st.power = bool(attrs["powerSwitch"])
            if "windSpeed7Gear" in attrs:
                gear = attrs["windSpeed7Gear"]
                st.fan_speed = ACFanSpeed.QUIET if gear == 1 else ACFanSpeed.AUTO
            st.last_updated = datetime.now()

        self.command_history.append({
            "timestamp": datetime.now().isoformat(),
            "device_id": device_id,
            "partial_shadow": attrs,
            "success": True,
            "dry_run": dry_run
        })
        return True


class ShellyCloudController(BaseACController):
    """
    Extensible Controller Interface for Shelly Cloud Devices (Shelly TRV, Pro Relays, Plus H&T).
    Enables future multi-vendor HVAC control without architectural rework.
    """

    @property
    def provider_type(self) -> DeviceProviderType:
        return DeviceProviderType.SHELLY_CLOUD

    def __init__(
        self,
        auth_key: Optional[str] = None,
        server_url: Optional[str] = None,
        device_ids: Optional[List[str]] = None
    ):
        self.auth_key = auth_key or AutomationConfig.SHELLY_AUTH_KEY
        self.server_url = server_url or AutomationConfig.SHELLY_SERVER_URL
        self.device_ids = device_ids or []

    def is_configured(self) -> bool:
        return bool(self.auth_key and self.server_url)

    def get_state(self, device_id: str) -> Optional[ACState]:
        """Fetch Shelly Cloud device status via REST API: /device/status."""
        if not self.is_configured():
            _LOGGER.debug(f"ShellyCloudController: Not configured for device {device_id}")
            return None
        # Future implementation: calls Shelly Cloud REST API
        return None

    def get_all_states(self) -> Dict[str, ACState]:
        return {dev_id: self.get_state(dev_id) for dev_id in self.device_ids if self.get_state(dev_id)}

    def send_command(self, command: ACCommand, dry_run: bool = False) -> bool:
        """Send command to Shelly Cloud device via REST API: /device/relay/control or /device/thermostat."""
        if dry_run:
            _LOGGER.info(f"[SHELLY DRY-RUN] Would command {command.device_id} to Power={command.power}")
            return True
        if not self.is_configured():
            _LOGGER.error("Shelly credentials not configured")
            return False
        return True
