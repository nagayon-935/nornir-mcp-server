import asyncio
import json
from pathlib import Path

import pytest
from nornir.core.configuration import Config
from nornir.core.inventory import Host
from nornir.core.task import Result

import server


@pytest.fixture
def local_setup(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("inventory:\n  plugin: SimpleInventory\nlogging:\n  enabled: false\n")
    (tmp_path / "hosts.yaml").write_text("r1:\n  hostname: 192.0.2.1\n  groups: [ios]\n  data:\n    role: core\n")
    (tmp_path / "groups.yaml").write_text(
        "ios:\n  platform: cisco_ios\n  username: user\n  password: super-secret-value\n  data:\n    site: tokyo\n"
    )
    (tmp_path / "defaults.yaml").write_text("{}\n")
    monkeypatch.setattr(server, "CONFIG_FILE", config)
    return tmp_path


def codes(report):
    return {check["code"] for check in report["checks"]}


def test_valid_setup_is_checked_without_opening_connections(local_setup, monkeypatch):
    monkeypatch.setattr(server, "ConnectHandler", lambda **kwargs: pytest.fail("must not connect"))
    report = server.diagnose_setup()
    assert report["status"] == "success"
    assert report["hosts_checked"] == 1
    assert report["network_accessed"] is False
    assert report["inventory_files"]["host_file"] == str(local_setup / "hosts.yaml")
    assert "super-secret-value" not in json.dumps(report)


def test_relative_inventory_paths_are_independent_of_cwd(local_setup, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    with server._get_nornir({"site": "tokyo"}, num_workers=1) as nr:
        assert list(nr.inventory.hosts) == ["r1"]
        assert nr.inventory.hosts["r1"].password == "super-secret-value"
        assert nr.config.runner.options["num_workers"] == 1
        result = nr.run(task=lambda task: Result(host=task.host, result=task.host.get("role")))
        assert result["r1"][0].result == "core"


def test_absolute_paths_are_preserved(local_setup):
    config = Config.from_dict(inventory={"options": {"host_file": "/absolute/hosts.yaml"}})
    assert server._inventory_paths(config)["host_file"] == Path("/absolute/hosts.yaml")


def test_missing_config_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / "missing.yaml")
    report = server.diagnose_setup()
    assert report["status"] == "failed"
    assert "config_missing" in codes(report)


def test_malformed_config_does_not_expose_parser_excerpts(tmp_path, monkeypatch):
    config = tmp_path / "invalid.yaml"
    config.write_text("password: super-secret-value\ninventory: [\n")
    monkeypatch.setattr(server, "CONFIG_FILE", config)
    report = server.diagnose_setup()
    assert "config_invalid" in codes(report)
    assert "super-secret-value" not in json.dumps(report)


def test_missing_host_file_is_an_error(local_setup):
    (local_setup / "hosts.yaml").unlink()
    report = server.diagnose_setup()
    assert report["status"] == "failed"
    assert "inventory_file_missing" in codes(report)


def test_missing_optional_files_are_warnings(local_setup):
    (local_setup / "defaults.yaml").unlink()
    assert server.diagnose_setup()["status"] == "warning"


def test_bad_group_reference_is_reported_without_secrets(local_setup):
    (local_setup / "hosts.yaml").write_text("r1:\n  groups: [missing-group]\n  password: super-secret-value\n")
    report = server.diagnose_setup()
    assert "inventory_invalid" in codes(report)
    assert "super-secret-value" not in json.dumps(report)


def test_placeholders_are_checked_using_inherited_credentials(local_setup):
    group = local_setup / "groups.yaml"
    group.write_text(group.read_text().replace("super-secret-value", "CHANGE_ME"))
    report = server.diagnose_setup()
    assert report["status"] == "failed"
    assert "placeholder_credentials" in codes(report)


def test_ssh_key_auth_does_not_require_a_password():
    from nornir.core.inventory import ConnectionOptions

    host = Host(
        name="r1",
        hostname="192.0.2.1",
        platform="cisco_ios",
        username="user",
        connection_options={"netmiko": ConnectionOptions(extras={"use_keys": True})},
    )
    assert not server._diagnose_host(host)


@pytest.mark.parametrize(
    "data,code",
    [
        ({"http_timeout": -1}, "invalid_http_timeout"),
        ({"http_timeout": "nan"}, "invalid_http_timeout"),
        ({"tls_verify": "false"}, "invalid_tls_setting"),
        ({"base_url": "ftp://example.org"}, "invalid_base_url"),
        ({"base_url": "https://user:super-secret-value@example.org"}, "invalid_base_url"),
        ({"http_headers": ["super-secret-value"]}, "invalid_http_headers"),
        ({"serial_settings": {"baudrate": 9600}}, "invalid_serial_settings"),
        ({"serial_settings": {"port": "COM1", "device_type": "arista_eos"}}, "unsupported_serial_platform"),
    ],
)
def test_invalid_connection_settings_are_reported_without_values(data, code):
    host = Host(name="r1", hostname="192.0.2.1", platform="cisco_ios", username="user", password="password", data=data)
    checks = server._diagnose_host(host)
    assert code in {check["code"] for check in checks}
    assert "super-secret-value" not in json.dumps(checks)


def test_unsupported_driver_is_reported():
    host = Host(name="r1", hostname="192.0.2.1", platform="made_up_vendor", username="user", password="password")
    assert "unsupported_platform" in {check["code"] for check in server._diagnose_host(host)}


def test_netbox_diagnosis_does_not_fetch_remote_inventory(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("inventory:\n  plugin: NetBoxInventory2\nlogging:\n  enabled: false\n")
    monkeypatch.setattr(server, "CONFIG_FILE", config)
    monkeypatch.setenv("NB_URL", "https://netbox.example")
    monkeypatch.setenv("NB_TOKEN", "super-secret-value")
    monkeypatch.setattr(server, "InitNornir", lambda **kwargs: pytest.fail("must not initialize remote inventory"))
    report = server.diagnose_setup()
    assert report["network_accessed"] is False
    assert report["hosts_checked"] is None
    assert "remote_inventory_skipped" in codes(report)
    assert "super-secret-value" not in json.dumps(report)


def test_missing_netbox_token_is_reported(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("inventory:\n  plugin: NetBoxInventory2\nlogging:\n  enabled: false\n")
    monkeypatch.setattr(server, "CONFIG_FILE", config)
    monkeypatch.delenv("NB_TOKEN", raising=False)
    assert "netbox_token_missing" in codes(server.diagnose_setup())


def test_unknown_inventory_plugin_is_reported(local_setup):
    config = local_setup / "config.yaml"
    config.write_text(config.read_text().replace("SimpleInventory", "missing_plugin"))
    assert "inventory_plugin_missing" in codes(server.diagnose_setup())


def test_diagnostics_have_a_validated_mcp_schema(local_setup):
    _, report = asyncio.run(server.mcp.call_tool("diagnose_setup", {}))
    assert report["status"] == "success"
    assert report["network_accessed"] is False
