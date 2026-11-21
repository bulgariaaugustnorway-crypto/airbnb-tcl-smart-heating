"""Data models for TCL Cloud responses and internal representations.

Portions adapted from 'ha-tcl-home-unofficial-integration'
Copyright (c) 2025 Andras Nemes
Licensed under the MIT License.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, List


def get_value(data: Dict[str, Any], keys: List[str], default=None) -> Any:
    """Helper to extract value by multiple possible key aliases."""
    if not isinstance(data, dict):
        return default
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return default


@dataclass
class TCLUser:
    username: str
    nickname: str
    country_abbr: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TCLUser":
        return cls(
            username=str(get_value(data, ["username"], "")),
            nickname=str(get_value(data, ["nickname"], "")),
            country_abbr=str(get_value(data, ["country_abbr", "countryAbbr"], "US")),
        )


@dataclass
class TCLAuthResult:
    status: int
    token: str
    refresh_token: str
    user: TCLUser
    expires_at: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["TCLAuthResult"]:
        status = get_value(data, ["status"])
        if status != 1:
            return None
        user_data = data.get("user") or {}
        return cls(
            status=status,
            token=str(get_value(data, ["token"], "")),
            refresh_token=str(get_value(data, ["refresh_token", "refreshtoken"], "")),
            user=TCLUser.from_dict(user_data),
        )


@dataclass
class TCLCloudUrls:
    sso_region: str
    cloud_region: str
    sso_url: str
    cloud_url: str
    device_url: str
    identity_pool_id: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["TCLCloudUrls"]:
        d = data.get("data") if "data" in data else data
        if not isinstance(d, dict):
            return None
        return cls(
            sso_region=str(get_value(d, ["sso_region", "ssoRegion"], "")),
            cloud_region=str(get_value(d, ["cloud_region", "cloudRegion"], "eu-central-1")),
            sso_url=str(get_value(d, ["sso_url", "ssoUrl"], "")),
            cloud_url=str(get_value(d, ["cloud_url", "cloudUrl"], "")),
            device_url=str(get_value(d, ["device_url", "deviceUrl"], "")),
            identity_pool_id=str(get_value(d, ["identity_pool_id", "identityPoolId"], "")),
        )


@dataclass
class TCLTokens:
    saas_token: str
    cognito_token: str
    cognito_id: str
    mqtt_endpoint: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["TCLTokens"]:
        d = data.get("data") if "data" in data else data
        if not isinstance(d, dict):
            return None
        return cls(
            saas_token=str(get_value(d, ["saas_token", "saasToken"], "")),
            cognito_token=str(get_value(d, ["cognito_token", "cognitoToken"], "")),
            cognito_id=str(get_value(d, ["cognito_id", "cognitoId"], "")),
            mqtt_endpoint=str(get_value(d, ["mqtt_endpoint", "mqttEndpoint"], "")),
        )


@dataclass
class TCLCredentials:
    access_key_id: str
    secret_key: str
    session_token: str
    expiration: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["TCLCredentials"]:
        creds = data.get("Credentials") or data
        if not isinstance(creds, dict):
            return None
        return cls(
            access_key_id=str(get_value(creds, ["AccessKeyId", "access_key_id"], "")),
            secret_key=str(get_value(creds, ["SecretKey", "secret_key"], "")),
            session_token=str(get_value(creds, ["SessionToken", "session_token"], "")),
            expiration=get_value(creds, ["Expiration", "expiration"]),
        )


@dataclass
class TCLDevice:
    device_id: str
    product_key: str
    nick_name: str
    device_name: str
    category: str
    is_online: bool
    room: str
    firmware_version: str
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TCLDevice":
        is_online_val = get_value(data, ["is_online", "isOnline"], 0)
        return cls(
            device_id=str(get_value(data, ["device_id", "deviceId"], "")),
            product_key=str(get_value(data, ["product_key", "productKey"], "")),
            nick_name=str(get_value(data, ["nick_name", "nickName"], "")),
            device_name=str(get_value(data, ["device_name", "deviceName"], "")),
            category=str(get_value(data, ["category"], "")),
            is_online=bool(int(is_online_val) == 1) if str(is_online_val).isdigit() else bool(is_online_val),
            room=str(get_value(data, ["room"], "")),
            firmware_version=str(get_value(data, ["firmware_version", "firmwareVersion"], "")),
            raw_data=data,
        )


@dataclass
class TCLAWSShadow:
    power: bool
    target_temperature: float
    current_temperature: Optional[float]
    mode: str
    fan_speed: str
    eco: bool
    display_led: bool = False
    beep_feedback: bool = False
    current_temperature_timestamp: Optional[datetime] = None
    raw_shadow: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_shadow_dict(cls, shadow_data: Dict[str, Any]) -> "TCLAWSShadow":
        state = shadow_data.get("state", {})
        # Prefer reported values, fallback to desired
        reported = state.get("reported", {})
        desired = state.get("desired", {})

        def get_prop(keys: List[str], default=None):
            for k in keys:
                if k in reported and reported[k] is not None:
                    return reported[k]
            for k in keys:
                if k in desired and desired[k] is not None:
                    return desired[k]
            return default

        power_val = get_prop(["powerSwitch", "Power", "power"], 0)
        power = bool(power_val == 1 or power_val is True)

        target_temp = float(get_prop(["targetTemperature", "SetTemp", "target_temperature"], 21.0))
        indoor_temp = get_prop(["currentTemperature", "IndoorTemp", "current_temperature"])
        current_temp = float(indoor_temp) if indoor_temp is not None else None

        # Extract temperature measurement timestamp from AWS IoT shadow metadata
        temp_dt: Optional[datetime] = None
        meta = shadow_data.get("metadata", {})
        if isinstance(meta, dict):
            meta_reported = meta.get("reported", {}) if isinstance(meta.get("reported"), dict) else {}
            meta_desired = meta.get("desired", {}) if isinstance(meta.get("desired"), dict) else {}
            temp_ts = None
            for k in ["currentTemperature", "IndoorTemp", "current_temperature"]:
                if k in meta_reported and isinstance(meta_reported[k], dict) and "timestamp" in meta_reported[k]:
                    temp_ts = meta_reported[k]["timestamp"]
                    break
            if temp_ts is None:
                for k in ["currentTemperature", "IndoorTemp", "current_temperature"]:
                    if k in meta_desired and isinstance(meta_desired[k], dict) and "timestamp" in meta_desired[k]:
                        temp_ts = meta_desired[k]["timestamp"]
                        break

            if temp_ts is not None:
                try:
                    from datetime import timezone
                    temp_dt = datetime.fromtimestamp(float(temp_ts), tz=timezone.utc)
                except Exception:
                    temp_dt = None

        # Mode parsing (supports string or workMode int)
        raw_mode = get_prop(["workMode", "Mode", "mode"], "Heat")
        if isinstance(raw_mode, int):
            mode_map = {0: "Auto", 1: "Cool", 2: "Dry", 3: "Fan", 4: "Heat"}
            mode = mode_map.get(raw_mode, "Heat")
        else:
            mode = str(raw_mode)

        # Fan speed parsing (supports string or windSpeed7Gear / windSpeed int)
        raw_fan = get_prop(["windSpeed7Gear", "windSpeed", "WindSpeed", "fan_speed"], "Auto")
        if isinstance(raw_fan, int):
            fan_map = {
                0: "Auto",
                1: "Quiet",
                2: "Low",
                3: "Mid",
                4: "Mid",
                5: "High",
                6: "High",
                7: "Turbo",
                8: "Auto",
            }
            fan_speed = fan_map.get(raw_fan, "Auto")
        else:
            fan_speed = str(raw_fan)

        eco_val = get_prop(["AIECOSwitch", "ECO", "Eco", "eco"], 0)
        eco = bool(eco_val == 1 or eco_val is True)

        screen_val = get_prop(["screen", "Screen", "Display", "display"], 0)
        display_led = bool(screen_val == 1 or screen_val is True)

        beep_val = get_prop(["beepSwitch", "Beep", "beep"], 0)
        beep_feedback = bool(beep_val == 1 or beep_val is True)

        return cls(
            power=power,
            target_temperature=target_temp,
            current_temperature=current_temp,
            current_temperature_timestamp=temp_dt,
            mode=mode,
            fan_speed=fan_speed,
            eco=eco,
            display_led=display_led,
            beep_feedback=beep_feedback,
            raw_shadow=shadow_data,
        )
