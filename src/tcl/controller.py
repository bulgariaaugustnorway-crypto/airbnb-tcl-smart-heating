"""High-level TCL Cloud Controller managing session lifecycle, discovery, and device states.

Adapted from 'ha-tcl-home-unofficial-integration'
Copyright (c) 2025 Andras Nemes
Licensed under the MIT License.
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

from src.models import ACState, ACCommand, ACMode, ACFanSpeed
from src.base_controller import BaseACController
from src.config import AutomationConfig
from .models import (
    TCLAuthResult,
    TCLCloudUrls,
    TCLTokens,
    TCLCredentials,
    TCLDevice,
    TCLAWSShadow,
)
from .client import TCLCloudClient, mask_secret, mask_email, is_jwt_expired

_LOGGER = logging.getLogger(__name__)


class ReadOnlyModeError(RuntimeError):
    """Raised when an attempt is made to mutate physical hardware while read-only mode is active."""
    pass


class TCLCloudController(BaseACController):
    """
    Production-ready controller connecting to TCL Cloud / AWS IoT Core.
    Implements authentication, device discovery, token refresh, and read-only shadow querying.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        login_url: Optional[str] = None,
        cloud_url_gateway: Optional[str] = None,
        app_id: Optional[str] = None,
        client: Optional[TCLCloudClient] = None,
        target_device_ids: Optional[List[str]] = None,
        config: Optional[AutomationConfig] = None,
        read_only: Optional[bool] = None,
    ):
        self.config = config or AutomationConfig()
        self.username = username or self.config.TCL_USERNAME
        self.password = password or self.config.TCL_PASSWORD
        self.login_url = login_url or self.config.TCL_APP_LOGIN_URL
        self.cloud_url_gateway = cloud_url_gateway or self.config.TCL_APP_CLOUD_URL
        self.app_id = app_id or self.config.TCL_APP_ID
        self.read_only = read_only if read_only is not None else self.config.TCL_READ_ONLY

        self.client = client or TCLCloudClient()
        self.target_device_ids = target_device_ids or self.config.DEVICE_IDS

        # Active session state
        self.auth_result: Optional[TCLAuthResult] = None
        self.cloud_urls: Optional[TCLCloudUrls] = None
        self.tokens: Optional[TCLTokens] = None
        self.credentials: Optional[TCLCredentials] = None

        # Discovered devices registry: physical_id -> TCLDevice
        self.discovered_devices: Dict[str, TCLDevice] = {}
        # Device mapping: alias_name (e.g. tcl_ac_living_room) -> physical_id
        self.device_aliases: Dict[str, str] = {}
        if self.config.TCL_LIVING_ROOM_ID:
            self.device_aliases["tcl_ac_living_room"] = self.config.TCL_LIVING_ROOM_ID
            self.device_aliases["living_room"] = self.config.TCL_LIVING_ROOM_ID
            self.device_aliases[self.config.TCL_LIVING_ROOM_ID] = self.config.TCL_LIVING_ROOM_ID
        if self.config.TCL_BEDROOM_ID:
            self.device_aliases["tcl_ac_bedroom"] = self.config.TCL_BEDROOM_ID
            self.device_aliases["bedroom"] = self.config.TCL_BEDROOM_ID
            self.device_aliases[self.config.TCL_BEDROOM_ID] = self.config.TCL_BEDROOM_ID
        # Cached states
        self.cached_states: Dict[str, ACState] = {}

    def is_configured(self) -> bool:
        """Check if username and password credentials are set."""
        return bool(self.username and self.password)

    def authenticate_and_connect(self) -> bool:
        """
        Execute full 4-step authentication and discovery pipeline:
        1. Global SSO Authentication
        2. Regional Cloud URLs resolution
        3. Refresh Tokens & Cognito OpenID Token exchange
        4. AWS STS Credentials retrieval
        5. Discover devices
        """
        if not self.is_configured():
            _LOGGER.warning("TCLCloudController: Credentials not configured. Please set TCL_USERNAME and TCL_PASSWORD.")
            return False

        try:
            # 1. Login
            self.auth_result = self.client.do_account_auth(
                username=self.username,
                password=self.password,
                login_url=self.login_url
            )

            # Use internal account ID (auth_result.user.username) required by TCL APIs
            sso_account_id = (self.auth_result.user.username if self.auth_result and self.auth_result.user else None) or self.username

            # 2. Cloud URLs
            self.cloud_urls = self.client.get_cloud_urls(
                username=sso_account_id,
                token=self.auth_result.token,
                cloud_url_gateway=self.cloud_url_gateway
            )

            # 3. Tokens (SaaS & Cognito)
            self.tokens = self.client.refresh_tokens(
                cloud_url=self.cloud_urls.cloud_url,
                username=sso_account_id,
                sso_token=self.auth_result.token,
                app_id=self.app_id
            )

            # 4. AWS Credentials
            self.credentials = self.client.get_aws_credentials(
                aws_region=self.cloud_urls.cloud_region,
                cognito_token=self.tokens.cognito_token
            )

            # 5. Discover devices
            self.discover_devices()
            return True

        except Exception as e:
            _LOGGER.error(f"Failed to authenticate and connect to TCL Cloud: {e}")
            return False

    def ensure_valid_session(self) -> bool:
        """Verify tokens and refresh them if expired."""
        if not self.is_configured():
            return False

        if not self.auth_result or not self.tokens or not self.credentials:
            return self.authenticate_and_connect()

        # Check if SSO token expired
        if is_jwt_expired(self.auth_result.token, margin_seconds=120):
            _LOGGER.info("TCL SSO Token expired; re-authenticating...")
            return self.authenticate_and_connect()

        # Check if SaaS or Cognito token expired
        if is_jwt_expired(self.tokens.cognito_token, margin_seconds=120):
            _LOGGER.info("TCL Cognito Token expired; refreshing tokens...")
            try:
                sso_account_id = (self.auth_result.user.username if self.auth_result and self.auth_result.user else None) or self.username
                self.tokens = self.client.refresh_tokens(
                    cloud_url=self.cloud_urls.cloud_url,
                    username=sso_account_id,
                    sso_token=self.auth_result.token,
                    app_id=self.app_id
                )
                self.credentials = self.client.get_aws_credentials(
                    aws_region=self.cloud_urls.cloud_region,
                    cognito_token=self.tokens.cognito_token
                )
                return True
            except Exception as e:
                _LOGGER.warning(f"Token refresh failed ({e}); performing full login...")
                return self.authenticate_and_connect()

        return True

    def discover_devices(self) -> List[TCLDevice]:
        """Fetch all devices on account and establish device mapping for the 2 AC units."""
        if not self.cloud_urls or not self.tokens:
            if not self.authenticate_and_connect():
                return []

        devices = self.client.get_things(
            device_url=self.cloud_urls.device_url,
            saas_token=self.tokens.saas_token,
            country_abbr=self.auth_result.user.country_abbr if self.auth_result else "BG"
        )

        self.discovered_devices = {d.device_id: d for d in devices}

        # Identify Split AC units
        ac_devices = [
            d for d in devices
            if "ac" in d.category.lower() or "air" in d.category.lower() or "split" in d.device_name.lower() or "ac" in d.nick_name.lower()
        ]
        _LOGGER.info(f"Identified {len(ac_devices)} AC unit(s) among {len(devices)} total device(s).")

        # Map to configured device IDs / aliases
        # Deterministic, explicit mapping strictly bound to configured physical IDs
        self.device_aliases = {}
        for dev in devices:
            self.device_aliases[dev.device_id] = dev.device_id

        lr_id = self.config.TCL_LIVING_ROOM_ID
        br_id = self.config.TCL_BEDROOM_ID

        if lr_id:
            self.device_aliases["tcl_ac_living_room"] = lr_id
            self.device_aliases["living_room"] = lr_id
        if br_id:
            self.device_aliases["tcl_ac_bedroom"] = br_id
            self.device_aliases["bedroom"] = br_id

        _LOGGER.info(
            f"Device alias mapping established: living_room -> {self.device_aliases.get('tcl_ac_living_room')} | "
            f"bedroom -> {self.device_aliases.get('tcl_ac_bedroom')}"
        )

        return devices

    def resolve_physical_id(self, device_id_or_alias: str) -> str:
        """Translate alias (e.g. tcl_ac_living_room) to physical device_id."""
        return self.device_aliases.get(device_id_or_alias, device_id_or_alias)

    def validate_target_device(self, target_device_id: str, expected_room: Optional[str] = None) -> str:
        """
        Validate that the target device matches the explicit hardware configuration.
        Prevents cross-device command leakage.
        """
        physical_id = self.resolve_physical_id(target_device_id)

        lr_id = self.config.TCL_LIVING_ROOM_ID
        br_id = self.config.TCL_BEDROOM_ID
        known_ids = {lr_id: "living_room", br_id: "bedroom"}

        if physical_id not in known_ids:
            raise ValueError(
                f"DEVICE IDENTITY REJECTED: Physical ID '{physical_id}' is not an authorized configured device "
                f"(Authorized: {list(known_ids.keys())})."
            )

        assigned_room = known_ids[physical_id]
        if expected_room and expected_room.lower() != assigned_room:
            raise ValueError(
                f"DEVICE IDENTITY MISMATCH: Target '{target_device_id}' resolves to '{physical_id}' "
                f"which belongs to '{assigned_room}', but command was intended for '{expected_room}'."
            )

        return physical_id

    def get_state(self, device_id: str) -> Optional[ACState]:
        """Retrieve real-time state for a specific device (Read-Only)."""
        physical_id = self.resolve_physical_id(device_id)

        if not self.ensure_valid_session():
            return None

        # Check device online status from discovery if available
        device_meta = self.discovered_devices.get(physical_id)
        if device_meta and not device_meta.is_online:
            _LOGGER.warning(f"Device {device_id} ({physical_id}) is reported OFFLINE by TCL Cloud.")
            offline_state = ACState(
                device_id=device_id,
                power=False,
                is_online=False,
                last_updated=datetime.now()
            )
            self.cached_states[device_id] = offline_state
            return offline_state

        try:
            shadow: TCLAWSShadow = self.client.get_thing_shadow(
                mqtt_endpoint=self.tokens.mqtt_endpoint,
                aws_region=self.cloud_urls.cloud_region,
                credentials=self.credentials,
                device_id=physical_id
            )

            # Map TCL mode & fan to canonical Enums
            mode_enum = ACMode.HEAT
            try:
                mode_enum = ACMode(shadow.mode.capitalize())
            except ValueError:
                mode_enum = ACMode.HEAT

            fan_enum = ACFanSpeed.AUTO
            try:
                fan_enum = ACFanSpeed(shadow.fan_speed.capitalize())
            except ValueError:
                fan_enum = ACFanSpeed.AUTO

            ac_state = ACState(
                device_id=device_id,
                power=shadow.power,
                target_temperature=shadow.target_temperature,
                current_temperature=shadow.current_temperature,
                temp_measured_at=shadow.current_temperature_timestamp,
                mode=mode_enum,
                fan_speed=fan_enum,
                eco_mode=shadow.eco,
                display_led=shadow.display_led,
                beep_feedback=shadow.beep_feedback,
                is_online=True,
                last_updated=datetime.now()
            )
            self.cached_states[device_id] = ac_state
            return ac_state

        except Exception as e:
            _LOGGER.error(f"Failed to fetch state for {device_id} ({physical_id}): {e}")
            return None

    def get_all_states(self) -> Dict[str, ACState]:
        """Query and return states for all managed AC devices."""
        states = {}
        for dev_id in self.target_device_ids:
            st = self.get_state(dev_id)
            if st:
                states[dev_id] = st
        return states

    def set_target_temperature(self, device_id: str, target_temp: float, dry_run: bool = False) -> bool:
        """
        Directly update the target temperature setpoint of an AC unit.
        Subject to read_only safeguards, temperature clamping, device identity validation, and dry-run simulation.
        """
        physical_id = self.validate_target_device(device_id)

        # Temperature safety clamping (16.0°C to 30.0°C)
        clamped_temp = max(AutomationConfig.TEMP_MIN_SAFE, min(AutomationConfig.TEMP_MAX_SAFE, float(target_temp)))
        if clamped_temp != target_temp:
            _LOGGER.warning(
                f"Clamping {device_id} ({physical_id}) temperature from {target_temp}°C to safe limit {clamped_temp}°C"
            )
        target_temp = clamped_temp

        if dry_run:
            _LOGGER.info(
                f"[TCL CLOUD DRY-RUN] Target temp simulated for {device_id} ({physical_id}): {target_temp}°C"
            )
            return True

        if self.read_only:
            err_msg = (
                f"[READ-ONLY ACTIVE] Refusing to set target temperature on {device_id}. "
                f"Read-only mode must be explicitly deactivated."
            )
            _LOGGER.warning(err_msg)
            raise ReadOnlyModeError(err_msg)

        if not self.ensure_valid_session():
            raise RuntimeError(f"Cannot set target temperature on {device_id}: TCL session invalid")

        fahrenheit_val = round(target_temp * 9 / 5 + 32)
        desired_state = {
            "targetTemperature": target_temp,
            "targetFahrenheitTemp": fahrenheit_val,
            "beepSwitch": 1 if AutomationConfig.BEEP_FEEDBACK_ENABLED else 0,
        }

        _LOGGER.info(
            f"Dispatching target temperature {target_temp}°C ({fahrenheit_val}°F) to {device_id} ({physical_id})..."
        )
        self.client.update_thing_shadow(
            mqtt_endpoint=self.tokens.mqtt_endpoint,
            aws_region=self.cloud_urls.cloud_region,
            credentials=self.credentials,
            device_id=physical_id,
            desired_state=desired_state
        )
        return True

    def send_partial_shadow(
        self,
        device_id: str,
        desired_attributes: Dict[str, Any],
        dry_run: bool = False
    ) -> bool:
        """
        Transmit a selective subset of shadow attributes (e.g. only 'screen' or 'beepSwitch').
        Guarantees that no other device properties are mutated or overridden.
        Subject to read_only safeguards, device identity validation, and dry-run simulation.
        Bundles beepSwitch: 0 in the same partial shadow payload whenever supported.
        """
        physical_id = self.validate_target_device(device_id)

        # Bundle beepSwitch into payload if omitted to suppress audible feedback
        payload = dict(desired_attributes)
        if "beepSwitch" not in payload:
            payload["beepSwitch"] = 1 if AutomationConfig.BEEP_FEEDBACK_ENABLED else 0

        if dry_run:
            _LOGGER.info(
                f"[TCL CLOUD DRY-RUN] Partial shadow simulated for {device_id} ({physical_id}): {payload}"
            )
            return True

        if self.read_only:
            err_msg = (
                f"[READ-ONLY ACTIVE] Refusing to send partial shadow to {device_id}. "
                f"Read-only mode must be explicitly deactivated."
            )
            _LOGGER.warning(err_msg)
            raise ReadOnlyModeError(err_msg)

        if not self.ensure_valid_session():
            raise RuntimeError(f"Cannot dispatch partial shadow to {device_id}: TCL session invalid")

        _LOGGER.info(
            f"Dispatching selective shadow attributes to {device_id} ({physical_id}): {payload}"
        )
        self.client.update_thing_shadow(
            mqtt_endpoint=self.tokens.mqtt_endpoint,
            aws_region=self.cloud_urls.cloud_region,
            credentials=self.credentials,
            device_id=physical_id,
            desired_state=payload
        )
        return True

    def send_command(self, command: ACCommand, dry_run: bool = False) -> bool:
        """
        Transmit full AC command (power, temp, mode, fan speed).
        Subject to read_only safeguards, device identity validation, and dry-run simulation.
        """
        physical_id = self.validate_target_device(command.device_id)

        if dry_run:
            _LOGGER.info(
                f"[TCL CLOUD DRY-RUN] Command simulated for {command.device_id} ({physical_id}): "
                f"Power={command.power}, Temp={command.target_temperature}°C | Reason: {command.reason}"
            )
            return True

        if self.read_only:
            err_msg = (
                f"[READ-ONLY ACTIVE] Refusing to send mutating command to {command.device_id}. "
                f"Phase 3A requires explicit owner sign-off before hardware commands are permitted."
            )
            _LOGGER.warning(err_msg)
            raise ReadOnlyModeError(err_msg)

        if not self.ensure_valid_session():
            raise RuntimeError(f"Cannot dispatch command to {command.device_id}: TCL session invalid")

        desired: Dict[str, Any] = {
            "powerSwitch": 1 if command.power else 0,
        }
        if command.target_temperature is not None:
            clamped = max(AutomationConfig.TEMP_MIN_SAFE, min(AutomationConfig.TEMP_MAX_SAFE, float(command.target_temperature)))
            desired["targetTemperature"] = clamped
            desired["targetFahrenheitTemp"] = round(clamped * 9 / 5 + 32)

        if command.mode:
            mode_map = {"Auto": 0, "Cool": 1, "Dry": 2, "Fan": 3, "Heat": 4}
            desired["workMode"] = mode_map.get(command.mode.value, 4)

        if command.fan_speed:
            fan_map = {"Auto": 0, "Quiet": 1, "Low": 2, "Mid": 4, "High": 6, "Turbo": 7}
            desired["windSpeed7Gear"] = fan_map.get(command.fan_speed.value, 0)

        # LED screen display (0 = OFF, 1 = ON)
        desired["screen"] = 1 if command.display_led else 0

        # Buzzer sound switch (0 = Mute / OFF, 1 = ON)
        desired["beepSwitch"] = 1 if command.beep_feedback else 0

        _LOGGER.info(f"Dispatching desired shadow state to {command.device_id} ({physical_id}): {desired}")
        self.client.update_thing_shadow(
            mqtt_endpoint=self.tokens.mqtt_endpoint,
            aws_region=self.cloud_urls.cloud_region,
            credentials=self.credentials,
            device_id=physical_id,
            desired_state=desired
        )
        return True

    def get_diagnostics(self) -> Dict[str, Any]:
        """Return safe, sanitized diagnostic information without exposing secrets."""
        return {
            "authenticated": bool(self.auth_result),
            "user": mask_email(self.username),
            "region": self.cloud_urls.cloud_region if self.cloud_urls else None,
            "mqtt_endpoint": self.tokens.mqtt_endpoint if self.tokens else None,
            "read_only_mode": self.read_only,
            "discovered_device_count": len(self.discovered_devices),
            "devices": [
                {
                    "device_id": d.device_id,
                    "name": d.device_name,
                    "nick_name": d.nick_name,
                    "category": d.category,
                    "is_online": d.is_online,
                    "room": d.room,
                }
                for d in self.discovered_devices.values()
            ],
            "active_mappings": self.device_aliases,
        }
