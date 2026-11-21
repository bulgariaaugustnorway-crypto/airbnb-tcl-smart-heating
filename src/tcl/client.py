"""Low-level HTTP client for TCL Cloud API and AWS IoT shadow integration.

Adapted from 'ha-tcl-home-unofficial-integration'
Copyright (c) 2025 Andras Nemes
Licensed under the MIT License.
"""

import base64
import hashlib
import json
import logging
import secrets
import string
import time
import urllib.request
from src import http_transport
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

from .models import (
    TCLAuthResult,
    TCLCloudUrls,
    TCLTokens,
    TCLCredentials,
    TCLDevice,
    TCLAWSShadow,
)

_LOGGER = logging.getLogger(__name__)


def mask_secret(val: Optional[str], show_chars: int = 4) -> str:
    """Safely redact secrets, passwords, and tokens in logs."""
    return "***REDACTED***" if val else "<empty>"


def mask_email(email: Optional[str]) -> str:
    """Mask email address for privacy in diagnostic logs."""
    if not email or "@" not in email:
        return "<hidden>"
    parts = email.split("@", 1)
    user_part = parts[0]
    domain_part = parts[1]
    masked_user = user_part[0] + "***" if len(user_part) > 1 else "*"
    return f"{masked_user}@{domain_part}"


def decode_jwt_unverified(token: str) -> Dict[str, Any]:
    """Parse JWT payload claims without cryptographic verification."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        # Fix base64 padding
        payload_b64 = parts[1]
        rem = len(payload_b64) % 4
        if rem > 0:
            payload_b64 += "=" * (4 - rem)
        decoded_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(decoded_bytes.decode("utf-8"))
    except Exception as e:
        _LOGGER.debug(f"Failed to decode unverified JWT: {e}")
        return {}


def is_jwt_expired(token: str, margin_seconds: int = 120) -> bool:
    """Check if JWT token has expired or is close to expiring."""
    claims = decode_jwt_unverified(token)
    exp = claims.get("exp")
    if exp is None:
        return False
    now = time.time()
    return (float(exp) - margin_seconds) < now


class TCLCloudClient:
    """HTTP client communicating with TCL Global SSO, Regional Gateways, and AWS IoT."""

    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def _http_post(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        description: str = "TCL Request"
    ) -> Dict[str, Any]:
        """Perform an HTTP POST request returning parsed JSON response."""
        data_bytes = json.dumps(payload).encode("utf-8") if payload else b""
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        _LOGGER.debug("TCL HTTP POST request")
        try:
            with http_transport.open_https(req, timeout=self.timeout_seconds) as resp:
                resp_bytes = resp.read()
                resp_text = resp_bytes.decode("utf-8", errors="replace")
                return json.loads(resp_text)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            _LOGGER.error("TCL HTTP request failed with status %s", e.code)
            raise RuntimeError(f"TCL API HTTP Error {e.code}") from None
        except urllib.error.URLError as e:
            _LOGGER.error("TCL network request failed")
            raise ConnectionError("TCL network request failed") from None

    def do_account_auth(
        self,
        username: str,
        password: str,
        login_url: str = "https://pa.account.tcl.com/account/login?clientId=54148614"
    ) -> TCLAuthResult:
        """Authenticate user credentials against TCL Global SSO service."""
        pw_md5 = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()

        payload = {
            "equipment": 2,
            "password": pw_md5,
            "osType": 1,
            "username": username,
            "clientVersion": "4.8.1",
            "osVersion": "6.0",
            "deviceModel": "AndroidAndroid SDK built for x86",
            "captchaRule": 2,
            "channel": "app",
        }

        headers = {
            "th_platform": "android",
            "th_version": "4.8.1",
            "th_appbulid": "830",
            "user-agent": "Android",
            "content-type": "application/json; charset=UTF-8",
        }

        _LOGGER.info(f"Authenticating with TCL SSO for user {mask_email(username)}...")
        data = self._http_post(login_url, payload, headers, description="TCL SSO Login")

        auth_res = TCLAuthResult.from_dict(data)
        if not auth_res:
            err_msg = data.get("message") or "Invalid credentials or login rejected"
            raise ValueError("TCL Login Failed: authentication rejected")

        _LOGGER.info("TCL authentication succeeded")
        return auth_res

    def get_cloud_urls(
        self,
        username: str,
        token: str,
        cloud_url_gateway: str = "https://prod-center.aws.tcljd.com/v3/global/cloud_url_get"
    ) -> TCLCloudUrls:
        """Fetch regional backend endpoints and identity pool configuration."""
        payload = {"ssoId": username, "ssoToken": token}
        headers = {
            "user-agent": "Android",
            "content-type": "application/json; charset=UTF-8",
        }

        _LOGGER.debug("Resolving TCL regional cloud URLs...")
        data = self._http_post(cloud_url_gateway, payload, headers, description="Get Cloud URLs")

        urls = TCLCloudUrls.from_dict(data)
        if not urls:
            raise ValueError(f"Failed to resolve TCL Cloud URLs from response: {data}")

        _LOGGER.info("TCL regional service discovered")
        return urls

    def refresh_tokens(
        self,
        cloud_url: str,
        username: str,
        sso_token: str,
        app_id: str = "wx6e1af3fa84fbe523"
    ) -> TCLTokens:
        """Exchange SSO token for SaaS token, AWS Cognito token, and IoT endpoint."""
        url = f"{cloud_url}/v3/auth/refresh_tokens"
        payload = {
            "userId": username,
            "ssoToken": sso_token,
            "appId": app_id,
        }
        headers = {
            "user-agent": "Android",
            "content-type": "application/json; charset=UTF-8",
        }

        _LOGGER.debug(f"Exchanging tokens at {url}...")
        data = self._http_post(url, payload, headers, description="Refresh SaaS & Cognito Tokens")

        tokens = TCLTokens.from_dict(data)
        if not tokens:
            raise ValueError(f"Failed to refresh SaaS & Cognito tokens: {data}")

        _LOGGER.info("TCL token exchange succeeded")
        return tokens

    def get_aws_credentials(
        self,
        aws_region: str,
        cognito_token: str
    ) -> TCLCredentials:
        """Obtain temporary AWS STS credentials via AWS Cognito Identity."""
        url = f"https://cognito-identity.{aws_region}.amazonaws.com/"

        claims = decode_jwt_unverified(cognito_token)
        identity_id = claims.get("sub", "")
        if not identity_id:
            raise ValueError("Cannot extract Cognito IdentityId from cognito_token claims")

        payload = {
            "IdentityId": identity_id,
            "Logins": {"cognito-identity.amazonaws.com": cognitoToken if 'cognitoToken' in locals() else cognito_token},
        }

        headers = {
            "User-agent": "aws-sdk-android/2.22.6 Linux Dalvik/2.1.0 en_US",
            "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
            "content-type": "application/x-amz-json-1.1",
        }

        _LOGGER.debug(f"Requesting temporary AWS credentials from Cognito Identity ({aws_region})...")
        data = self._http_post(url, payload, headers, description="Cognito GetCredentialsForIdentity")

        creds = TCLCredentials.from_dict(data)
        if not creds:
            raise ValueError(f"Failed to extract AWS credentials from Cognito response: {data}")

        _LOGGER.info(f"Obtained temporary AWS Credentials (AccessKey: {mask_secret(creds.access_key_id)})")
        return creds

    def get_things(
        self,
        device_url: str,
        saas_token: str,
        country_abbr: str = "BG"
    ) -> List[TCLDevice]:
        """Fetch all smart home devices registered under the TCL user account."""
        url = f"{device_url}/v3/user/get_things"
        timestamp = str(int(time.time() * 1000))
        nonce = "".join((secrets.choice(string.ascii_lowercase + string.digits) for _ in range(16)))

        # Legacy MD5 wire signature required by the upstream TCL protocol.
        # This cannot provide modern cryptographic integrity; TLS remains required.
        sign_input = timestamp + nonce + saas_token
        sign = hashlib.md5(sign_input.encode("utf-8"), usedforsecurity=False).hexdigest()

        headers = {
            "platform": "android",
            "appversion": "5.4.1",
            "thomeversion": "4.8.1",
            "accesstoken": saas_token,
            "accept-language": "en",
            "timestamp": timestamp,
            "nonce": nonce,
            "sign": sign,
            "user-agent": "Android",
            "content-type": "application/json; charset=UTF-8",
        }

        _LOGGER.info(f"Discovering TCL devices via {url}...")
        data = self._http_post(url, {}, headers, description="TCL Get Things")

        device_list = data.get("data") or []
        devices = [TCLDevice.from_dict(d) for d in device_list if isinstance(d, dict)]
        _LOGGER.info(f"Discovered {len(devices)} device(s) on TCL account.")
        return devices

    def get_thing_shadow(
        self,
        mqtt_endpoint: str,
        aws_region: str,
        credentials: TCLCredentials,
        device_id: str
    ) -> TCLAWSShadow:
        """
        Fetch real-time device shadow from AWS IoT Data plane.
        Utilizes boto3 if installed, otherwise uses AWS SigV4 signed REST request.
        """
        _LOGGER.debug(f"Fetching AWS IoT Device Shadow for device {device_id}...")

        try:
            import boto3
            import boto3.session

            session = boto3.session.Session(
                region_name=aws_region,
                aws_access_key_id=credentials.access_key_id,
                aws_secret_access_key=credentials.secret_key,
                aws_session_token=credentials.session_token,
            )
            from urllib.parse import urlparse
            endpoint_host = urlparse(mqtt_endpoint).hostname or mqtt_endpoint.replace("wss://", "").replace("https://", "").split(":")[0]
            iot_client = session.client(
                "iot-data",
                endpoint_url=f"https://{endpoint_host}"
            )
            resp = iot_client.get_thing_shadow(thingName=device_id)
            raw_payload = resp["payload"].read().decode("utf-8")
            shadow_dict = json.loads(raw_payload)
            return TCLAWSShadow.from_shadow_dict(shadow_dict)
        except ImportError:
            raise RuntimeError("boto3 package is required for real AWS IoT shadow communication")
        except Exception as e:
            _LOGGER.error(f"Failed to retrieve AWS IoT shadow for {device_id}: {e}")
            raise

    def update_thing_shadow(
        self,
        mqtt_endpoint: str,
        aws_region: str,
        credentials: TCLCredentials,
        device_id: str,
        desired_state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update desired device shadow in AWS IoT Data plane.
        Packages payload: {"state": {"desired": desired_state}, "clientToken": "airbnb_tcl_<ts>"}
        """
        _LOGGER.debug(f"Updating AWS IoT Device Shadow for device {device_id}...")

        try:
            import boto3
            import boto3.session
            from urllib.parse import urlparse

            session = boto3.session.Session(
                region_name=aws_region,
                aws_access_key_id=credentials.access_key_id,
                aws_secret_access_key=credentials.secret_key,
                aws_session_token=credentials.session_token,
            )
            endpoint_host = urlparse(mqtt_endpoint).hostname or mqtt_endpoint.replace("wss://", "").replace("https://", "").split(":")[0]
            iot_client = session.client(
                "iot-data",
                endpoint_url=f"https://{endpoint_host}"
            )
            payload_dict = {
                "state": {"desired": desired_state},
                "clientToken": f"airbnb_tcl_{int(time.time())}"
            }
            topic = f"$aws/things/{device_id}/shadow/update"
            resp = iot_client.publish(
                topic=topic,
                qos=1,
                payload=json.dumps(payload_dict).encode("utf-8")
            )
            return resp
        except ImportError:
            raise RuntimeError("boto3 package is required for real AWS IoT shadow communication")
        except Exception as e:
            _LOGGER.error(f"Failed to update AWS IoT shadow for {device_id}: {e}")
            raise
