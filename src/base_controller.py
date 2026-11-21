"""Abstract Base Controller definitions for HVAC and climate devices."""

import abc
from enum import Enum
from typing import Dict, Optional, Any

from src.models import ACCommand, ACState


class DeviceProviderType(str, Enum):
    """Supported smart home device provider backends."""
    MOCK = "mock"
    TCL_CLOUD = "tcl_cloud"
    SHELLY_CLOUD = "shelly_cloud"


class BaseHVACDeviceController(abc.ABC):
    """Abstract interface for HVAC and climate device controllers."""

    @property
    def provider_type(self) -> DeviceProviderType:
        return DeviceProviderType.MOCK

    @abc.abstractmethod
    def get_state(self, device_id: str) -> Optional[ACState]:
        """Fetch current status of a single managed device."""
        pass

    @abc.abstractmethod
    def get_all_states(self) -> Dict[str, ACState]:
        """Fetch current status of all managed devices."""
        pass

    @abc.abstractmethod
    def send_command(self, command: ACCommand, dry_run: bool = False) -> bool:
        """Transmit a command to the specified device."""
        pass

    def send_partial_shadow(
        self,
        device_id: str,
        desired_attributes: Dict[str, Any],
        dry_run: bool = False
    ) -> bool:
        """Transmit a selective subset of shadow attributes."""
        return True


# Backward-compatible alias
BaseACController = BaseHVACDeviceController
