"""Integration tests for TCL Cloud integration with mocked HTTP and AWS IoT responses."""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime

from src.models import ACState, ACCommand, ACMode, ACFanSpeed
from src.tcl.models import (
    TCLUser,
    TCLAuthResult,
    TCLCloudUrls,
    TCLTokens,
    TCLCredentials,
    TCLDevice,
    TCLAWSShadow,
)
from src.tcl.client import TCLCloudClient, mask_secret, mask_email, is_jwt_expired
from src.tcl.controller import TCLCloudController, ReadOnlyModeError
from src.config import AutomationConfig


def make_mock_jwt(sub: str = "eu-central-1:12345678-abcd-1234-abcd-1234567890ab", exp: float = 9999999999.0) -> str:
    """Construct simple unverified test JWT string."""
    import base64
    import json
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload_dict = {"sub": sub, "exp": exp}
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return f"{header}.{payload}.dummy_signature"


class TestTCLIntegration(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock(spec=TCLCloudClient)

        # Mock Auth Result
        self.auth_result = TCLAuthResult(
            status=1,
            token=make_mock_jwt(sub="user-123", exp=datetime.now().timestamp() + 3600),
            refresh_token="mock-refresh-token-123",
            user=TCLUser(username="test-bot@example.com", nickname="HostBot", country_abbr="BG"),
        )

        # Mock Cloud URLs
        self.cloud_urls = TCLCloudUrls(
            sso_region="eu",
            cloud_region="eu-central-1",
            sso_url="https://sso-eu.tcl.com",
            cloud_url="https://cloud-eu.tcl.com",
            device_url="https://device-eu.tcl.com",
            identity_pool_id="eu-central-1:pool-123",
        )

        # Mock Tokens
        self.tokens = TCLTokens(
            saas_token="mock-saas-token-xyz",
            cognito_token=make_mock_jwt(sub="eu-central-1:identity-456", exp=datetime.now().timestamp() + 3600),
            cognito_id="eu-central-1:identity-456",
            mqtt_endpoint="a1mock-ats.iot.eu-central-1.amazonaws.com",
        )

        # Mock AWS Credentials
        self.credentials = TCLCredentials(
            access_key_id="ASIAEXAMPLEKEYID123",
            secret_key="mockSecretKeyXYZ",
            session_token="mockSessionTokenABC",
            expiration=datetime.now().timestamp() + 3600,
        )

        # Mock 2 Split AC Devices
        self.device_1 = TCLDevice(
            device_id="tcl_dev_living_room_001",
            product_key="pk_split_ac",
            nick_name="Living Room AC",
            device_name="Split AC 12000 BTU",
            category="AC",
            is_online=True,
            room="Living Room",
            firmware_version="v2.1.0",
        )
        self.device_2 = TCLDevice(
            device_id="tcl_dev_bedroom_002",
            product_key="pk_split_ac",
            nick_name="Bedroom AC",
            device_name="Split AC 9000 BTU",
            category="AC",
            is_online=True,
            room="Bedroom",
            firmware_version="v2.1.0",
        )

        # Wire up mock client methods
        self.mock_client.do_account_auth.return_value = self.auth_result
        self.mock_client.get_cloud_urls.return_value = self.cloud_urls
        self.mock_client.refresh_tokens.return_value = self.tokens
        self.mock_client.get_aws_credentials.return_value = self.credentials
        self.mock_client.get_things.return_value = [self.device_1, self.device_2]

        self.test_config = AutomationConfig()
        self.test_config.TCL_LIVING_ROOM_ID = "tcl_dev_living_room_001"
        self.test_config.TCL_BEDROOM_ID = "tcl_dev_bedroom_002"

        self.controller = TCLCloudController(
            username="test-bot@example.com",
            password="mockPassword123!",
            read_only=True,
            client=self.mock_client,
            config=self.test_config,
            target_device_ids=["tcl_ac_living_room", "tcl_ac_bedroom"]
        )

    def test_full_authentication_and_device_discovery(self):
        success = self.controller.authenticate_and_connect()
        self.assertTrue(success)

        # Verify client calls
        self.mock_client.do_account_auth.assert_called_once()
        self.mock_client.get_cloud_urls.assert_called_once()
        self.mock_client.refresh_tokens.assert_called_once()
        self.mock_client.get_aws_credentials.assert_called_once()
        self.mock_client.get_things.assert_called_once()

        # Verify two independent AC units discovered and mapped
        self.assertEqual(len(self.controller.discovered_devices), 2)
        self.assertIn("tcl_dev_living_room_001", self.controller.discovered_devices)
        self.assertIn("tcl_dev_bedroom_002", self.controller.discovered_devices)

        # Verify default slot alias mapping
        self.assertEqual(
            self.controller.resolve_physical_id("tcl_ac_living_room"),
            "tcl_dev_living_room_001"
        )
        self.assertEqual(
            self.controller.resolve_physical_id("tcl_ac_bedroom"),
            "tcl_dev_bedroom_002"
        )

    def test_read_only_mode_blocks_physical_commands(self):
        cmd = ACCommand(
            device_id="tcl_ac_living_room",
            power=True,
            target_temperature=22.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.AUTO,
            reason="Physical command attempt"
        )

        # 1. Real execution with read_only=True MUST raise ReadOnlyModeError
        with self.assertRaises(ReadOnlyModeError):
            self.controller.send_command(cmd, dry_run=False)

        # 2. Dry-run execution is permitted safely
        dry_run_success = self.controller.send_command(cmd, dry_run=True)
        self.assertTrue(dry_run_success)

    def test_get_state_from_shadow(self):
        self.controller.authenticate_and_connect()

        # Mock Shadow response
        mock_shadow = TCLAWSShadow(
            power=True,
            target_temperature=22.0,
            current_temperature=21.5,
            mode="Heat",
            fan_speed="Auto",
            eco=False
        )
        self.mock_client.get_thing_shadow.return_value = mock_shadow

        state = self.controller.get_state("tcl_ac_living_room")

        self.assertIsNotNone(state)
        self.assertTrue(state.power)
        self.assertEqual(state.target_temperature, 22.0)
        self.assertEqual(state.current_temperature, 21.5)
        self.assertEqual(state.mode, ACMode.HEAT)
        self.assertEqual(state.fan_speed, ACFanSpeed.AUTO)
        self.assertTrue(state.is_online)

        # Verify physical ID was used in call
        self.mock_client.get_thing_shadow.assert_called_with(
            mqtt_endpoint=self.tokens.mqtt_endpoint,
            aws_region="eu-central-1",
            credentials=self.credentials,
            device_id="tcl_dev_living_room_001"
        )

    def test_device_offline_handling(self):
        self.controller.authenticate_and_connect()

        # Mark device 2 as offline in discovered metadata
        self.controller.discovered_devices["tcl_dev_bedroom_002"].is_online = False

        state = self.controller.get_state("tcl_ac_bedroom")

        self.assertIsNotNone(state)
        self.assertFalse(state.is_online, "State must reflect device is offline")
        self.assertFalse(state.power)
        # Should not make shadow network call for offline unit
        self.mock_client.get_thing_shadow.assert_not_called()

    def test_expired_token_automatic_refresh(self):
        # Setup controller with an expired Cognito token
        expired_cognito_jwt = make_mock_jwt(exp=datetime.now().timestamp() - 300)
        self.controller.auth_result = self.auth_result
        self.controller.cloud_urls = self.cloud_urls
        self.controller.tokens = TCLTokens(
            saas_token="old-saas",
            cognito_token=expired_cognito_jwt,
            cognito_id="eu-central-1:identity-456",
            mqtt_endpoint="old-endpoint",
        )
        self.controller.credentials = self.credentials

        # When ensure_valid_session runs, it should refresh tokens
        refreshed = self.controller.ensure_valid_session()
        self.assertTrue(refreshed)
        self.mock_client.refresh_tokens.assert_called()
        self.mock_client.get_aws_credentials.assert_called()

    def test_api_error_invalid_credentials(self):
        # Mock login error
        self.mock_client.do_account_auth.side_effect = ValueError("Invalid credentials")

        success = self.controller.authenticate_and_connect()
        self.assertFalse(success, "Must return False gracefully on invalid credentials")

    def test_network_failure_handling(self):
        # Mock network failure
        self.mock_client.do_account_auth.side_effect = ConnectionError("Network unreachable")

        success = self.controller.authenticate_and_connect()
        self.assertFalse(success, "Must handle network failures gracefully")

    def test_sensitive_data_redaction_in_diagnostics(self):
        self.controller.authenticate_and_connect()
        diag = self.controller.get_diagnostics()

        # Password must not be present
        self.assertNotIn("mockPassword123!", str(diag))
        # Email must be masked
        self.assertEqual(diag["user"], "t***@example.com")
        self.assertTrue(diag["read_only_mode"])
        self.assertEqual(diag["discovered_device_count"], 2)

    def test_mask_helpers(self):
        self.assertEqual(mask_email("guest@airbnb.com"), "g***@airbnb.com")
        self.assertEqual(mask_secret("TEST_ONLY_ACCESS_KEY"), "***REDACTED***")
        self.assertEqual(mask_secret("short"), "***REDACTED***")

    def test_send_partial_shadow_bundles_beep_switch_zero(self):
        """send_partial_shadow automatically injects beepSwitch: 0 when omitted to ensure silence."""
        self.controller.authenticate_and_connect()
        self.controller.read_only = False

        success = self.controller.send_partial_shadow(
            device_id="tcl_dev_living_room_001",
            desired_attributes={"screen": 1},
            dry_run=False
        )
        self.assertTrue(success)

        self.mock_client.update_thing_shadow.assert_called_once()
        _, kwargs = self.mock_client.update_thing_shadow.call_args
        self.assertEqual(kwargs["desired_state"]["screen"], 1)
        self.assertEqual(kwargs["desired_state"]["beepSwitch"], 0)

    def test_set_target_temperature_bundles_beep_switch_zero(self):
        """set_target_temperature bundles beepSwitch: 0 in shadow payload to suppress audible chime."""
        self.controller.authenticate_and_connect()
        self.controller.read_only = False

        success = self.controller.set_target_temperature(
            device_id="tcl_dev_living_room_001",
            target_temp=23.0,
            dry_run=False
        )
        self.assertTrue(success)

        self.mock_client.update_thing_shadow.assert_called_once()
        _, kwargs = self.mock_client.update_thing_shadow.call_args
        self.assertEqual(kwargs["desired_state"]["targetTemperature"], 23.0)
        self.assertEqual(kwargs["desired_state"]["beepSwitch"], 0)


if __name__ == "__main__":
    unittest.main()
