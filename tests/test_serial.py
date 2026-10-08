from types import SimpleNamespace

import pytest
from nornir.core.inventory import Host

import server
from server import _resolve_serial_device_type, custom_serial_task
from tests.support import FakeConnectHandler


class TestResolveSerialDeviceType:
    def test_prefers_explicit_device_type_in_serial_settings(self):
        device_type = _resolve_serial_device_type({"device_type": "cisco_ios_serial"}, platform="cisco_xr")

        assert device_type == "cisco_ios_serial"

    def test_appends_serial_suffix_to_platform(self):
        device_type = _resolve_serial_device_type({}, platform="cisco_ios")

        assert device_type == "cisco_ios_serial"

    def test_appends_serial_suffix_to_explicit_device_type(self):
        device_type = _resolve_serial_device_type({"device_type": "cisco_ios"}, platform=None)

        assert device_type == "cisco_ios_serial"

    def test_raises_when_device_type_cannot_be_determined(self):
        with pytest.raises(ValueError, match="serial device_type"):
            _resolve_serial_device_type({}, platform=None)


class TestCustomSerialTask:
    @pytest.fixture
    def connect_handler(self, monkeypatch):
        FakeConnectHandler.instances = []
        monkeypatch.setattr(server, "ConnectHandler", FakeConnectHandler)
        return FakeConnectHandler

    def _build_task(self, **overrides) -> SimpleNamespace:
        host = Host(
            name="router1",
            hostname="192.168.1.1",
            platform=overrides.pop("platform", "cisco_ios"),
            username="serial-user",
            password="serial-pass",
            data=overrides.pop("data", {"serial_settings": {"port": "/dev/ttyUSB0", "baudrate": 9600}}),
        )
        return SimpleNamespace(host=host)

    def test_uses_top_level_credentials_not_connection_options(self, connect_handler):
        # The asymmetry groups.yaml.example now documents: serial reads host.username
        # / host.password, never connection_options.netmiko.extras.
        custom_serial_task(self._build_task(), "show version")

        params = connect_handler.instances[0].params
        assert params["username"] == "serial-user"
        assert params["password"] == "serial-pass"

    def test_derives_serial_device_type_from_platform(self, connect_handler):
        custom_serial_task(self._build_task(), "show version")

        assert connect_handler.instances[0].params["device_type"] == "cisco_ios_serial"

    def test_strips_device_type_from_pyserial_settings(self, connect_handler):
        task = self._build_task(data={"serial_settings": {"port": "/dev/ttyUSB0", "device_type": "cisco_ios_serial"}})

        custom_serial_task(task, "show version")

        assert connect_handler.instances[0].params["serial_settings"] == {"port": "/dev/ttyUSB0"}

    def test_returns_command_output_and_closes_connection(self, connect_handler):
        result = custom_serial_task(self._build_task(), "show version")

        assert result.result == "output of show version"
        assert connect_handler.instances[0].exited is True

    def test_raises_when_serial_settings_missing(self, connect_handler):
        with pytest.raises(ValueError, match="serial_settings"):
            custom_serial_task(self._build_task(data={}), "show version")

        assert connect_handler.instances == []
