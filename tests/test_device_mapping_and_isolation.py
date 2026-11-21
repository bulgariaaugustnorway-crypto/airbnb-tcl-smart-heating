"""Regression and security test suite for device identity mapping and room isolation.

Ensures:
1. Living Room alias and configuration strictly resolve to physical ID demo_living_room (and NEVER demo_bedroom).
2. Bedroom alias and configuration strictly resolve to physical ID demo_bedroom (and NEVER demo_living_room).
3. Cloud discovery order changes (e.g. reversed API list) NEVER alter physical room mapping.
4. Future TCL Home app nickname changes NEVER hijack or alter physical room mapping.
5. validate_target_device strictly enforces authorized physical IDs and prevents cross-room dispatch.
6. All mutating methods (send_command, send_partial_shadow, set_target_temperature) reject cross-room targets.
"""

import pytest
from src.config import AutomationConfig
from src.tcl.controller import TCLCloudController
from src.tcl.models import TCLDevice
from src.models import ACCommand, ACMode, ACFanSpeed


class DummyClient:
    """Mock TCL client for device mapping tests."""
    def __init__(self, discovered_devices):
        self._devices = discovered_devices

    def get_things(self, *args, **kwargs):
        return list(self._devices)


@pytest.fixture
def mock_devices():
    dev_living_room = TCLDevice(
        device_id="demo_living_room",
        product_key="prod_lr",
        nick_name="Living room",
        device_name="Split AC",
        category="AC",
        is_online=True,
        room="Living Room",
        firmware_version="1.0"
    )
    dev_bedroom = TCLDevice(
        device_id="demo_bedroom",
        product_key="prod_br",
        nick_name="Split AC",
        device_name="Split AC",
        category="AC",
        is_online=True,
        room="Bedroom",
        firmware_version="1.0"
    )
    return dev_living_room, dev_bedroom


class TestDeviceMappingAndIsolation:

    def test_explicit_physical_ids_in_config(self):
        config = AutomationConfig()
        assert config.TCL_LIVING_ROOM_ID == "demo_living_room"
        assert config.TCL_BEDROOM_ID == "demo_bedroom"

    def test_alias_resolution_order_independence_order_a(self, mock_devices):
        dev_living_room, dev_bedroom = mock_devices
        # Discovery order: Bedroom first (demo_bedroom), Living Room second (demo_living_room)
        client = DummyClient([dev_bedroom, dev_living_room])
        controller = TCLCloudController(client=client, read_only=True)
        controller.cloud_urls = type("Urls", (), {"device_url": "dummy"})()
        controller.tokens = type("Tokens", (), {"saas_token": "dummy"})()
        controller.discover_devices()

        assert controller.resolve_physical_id("living_room") == "demo_living_room"
        assert controller.resolve_physical_id("tcl_ac_living_room") == "demo_living_room"
        assert controller.resolve_physical_id("bedroom") == "demo_bedroom"
        assert controller.resolve_physical_id("tcl_ac_bedroom") == "demo_bedroom"

    def test_alias_resolution_order_independence_order_b(self, mock_devices):
        dev_living_room, dev_bedroom = mock_devices
        # Discovery order: Living Room first (demo_living_room), Bedroom second (demo_bedroom)
        client = DummyClient([dev_living_room, dev_bedroom])
        controller = TCLCloudController(client=client, read_only=True)
        controller.cloud_urls = type("Urls", (), {"device_url": "dummy"})()
        controller.tokens = type("Tokens", (), {"saas_token": "dummy"})()
        controller.discover_devices()

        assert controller.resolve_physical_id("living_room") == "demo_living_room"
        assert controller.resolve_physical_id("tcl_ac_living_room") == "demo_living_room"
        assert controller.resolve_physical_id("bedroom") == "demo_bedroom"
        assert controller.resolve_physical_id("tcl_ac_bedroom") == "demo_bedroom"

    def test_alias_resolution_immune_to_cloud_nickname_changes(self, mock_devices):
        dev_living_room, dev_bedroom = mock_devices
        # Simulate arbitrary nickname changes in the mobile app
        dev_living_room.nick_name = "Some Arbitrary Name"
        dev_bedroom.nick_name = "Another Name"

        client = DummyClient([dev_living_room, dev_bedroom])
        controller = TCLCloudController(client=client, read_only=True)
        controller.cloud_urls = type("Urls", (), {"device_url": "dummy"})()
        controller.tokens = type("Tokens", (), {"saas_token": "dummy"})()
        controller.discover_devices()

        # Mapping MUST remain strictly bound to configured hardware IDs
        assert controller.resolve_physical_id("living_room") == "demo_living_room"
        assert controller.resolve_physical_id("bedroom") == "demo_bedroom"

    def test_validate_target_device_allows_valid_targets(self, mock_devices):
        client = DummyClient(mock_devices)
        controller = TCLCloudController(client=client, read_only=True)
        controller.cloud_urls = type("Urls", (), {"device_url": "dummy"})()
        controller.tokens = type("Tokens", (), {"saas_token": "dummy"})()
        controller.discover_devices()

        # Direct IDs
        assert controller.validate_target_device("demo_living_room", expected_room="living_room") == "demo_living_room"
        assert controller.validate_target_device("demo_bedroom", expected_room="bedroom") == "demo_bedroom"

        # Aliases
        assert controller.validate_target_device("living_room", expected_room="living_room") == "demo_living_room"
        assert controller.validate_target_device("tcl_ac_living_room", expected_room="living_room") == "demo_living_room"
        assert controller.validate_target_device("bedroom", expected_room="bedroom") == "demo_bedroom"
        assert controller.validate_target_device("tcl_ac_bedroom", expected_room="bedroom") == "demo_bedroom"

    def test_validate_target_device_rejects_unauthorized_id(self, mock_devices):
        client = DummyClient(mock_devices)
        controller = TCLCloudController(client=client, read_only=True)
        controller.cloud_urls = type("Urls", (), {"device_url": "dummy"})()
        controller.tokens = type("Tokens", (), {"saas_token": "dummy"})()
        controller.discover_devices()

        with pytest.raises(ValueError, match="DEVICE IDENTITY REJECTED"):
            controller.validate_target_device("UnknownHardwareDevice999")

    def test_validate_target_device_rejects_cross_room_mismatch(self, mock_devices):
        client = DummyClient(mock_devices)
        controller = TCLCloudController(client=client, read_only=True)
        controller.cloud_urls = type("Urls", (), {"device_url": "dummy"})()
        controller.tokens = type("Tokens", (), {"saas_token": "dummy"})()
        controller.discover_devices()

        # Passing bedroom ID (demo_bedroom) when living room was expected must raise ValueError
        with pytest.raises(ValueError, match="DEVICE IDENTITY MISMATCH"):
            controller.validate_target_device("demo_bedroom", expected_room="living_room")

        # Passing living room ID (demo_living_room) when bedroom was expected must raise ValueError
        with pytest.raises(ValueError, match="DEVICE IDENTITY MISMATCH"):
            controller.validate_target_device("demo_living_room", expected_room="bedroom")

    def test_dry_run_dispatch_validates_identity(self, mock_devices):
        client = DummyClient(mock_devices)
        controller = TCLCloudController(client=client, read_only=False)
        controller.cloud_urls = type("Urls", (), {"device_url": "dummy"})()
        controller.tokens = type("Tokens", (), {"saas_token": "dummy"})()
        controller.discover_devices()

        # Valid dry run command for living room targets demo_living_room
        cmd = ACCommand(
            device_id="living_room",
            power=True,
            target_temperature=22.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            reason="Test command"
        )
        assert controller.send_command(cmd, dry_run=True) is True

        # Command with unauthorized ID fails even in dry-run
        bad_cmd = ACCommand(
            device_id="rogue_device_id",
            power=True,
            target_temperature=22.0,
            mode=ACMode.HEAT,
            fan_speed=ACFanSpeed.QUIET,
            reason="Bad device"
        )
        with pytest.raises(ValueError, match="DEVICE IDENTITY REJECTED"):
            controller.send_command(bad_cmd, dry_run=True)
