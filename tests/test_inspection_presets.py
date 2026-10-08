import asyncio

import pytest
from netmiko.exceptions import NetmikoAuthenticationException
from nornir.core import Nornir
from nornir.core.filter import F
from nornir.core.inventory import ConnectionOptions, Host, Inventory
from nornir.core.task import Result
from nornir.plugins.runners import ThreadedRunner

import server


@pytest.fixture
def mixed_inventory(monkeypatch):
    platforms = ["cisco_ios", "cisco_nxos", "cisco_xr", "juniper_junos", "arista_eos", "unsupported_os"]
    hosts = {platform: Host(name=platform, platform=platform, hostname="192.0.2.1") for platform in platforms}
    nr = Nornir(inventory=Inventory(hosts=hosts), runner=ThreadedRunner(num_workers=4))
    monkeypatch.setattr(
        server, "_get_nornir", lambda filters=None, **kwargs: nr.filter(F(**filters)) if filters else nr
    )
    sent = {}

    def show_command(task, command_string, use_textfsm):
        sent[task.host.name] = (command_string, use_textfsm)
        return Result(host=task.host, result=[{"interface": "Ethernet1", "status": "up"}])

    monkeypatch.setattr(server, "netmiko_send_command", show_command)
    return nr, sent


def test_mixed_vendor_interfaces_dispatch_commands_per_host(mixed_inventory):
    nr, sent = mixed_inventory
    report = server.run_inspection("interfaces", all_hosts=True, include_output=True)
    assert report["status"] == "partial_failure"
    assert report["summary"]["succeeded"] == 5
    assert report["summary"]["failed"] == 1
    assert sent == {
        "cisco_ios": ("show interfaces description", True),
        "cisco_nxos": ("show interface brief", True),
        "cisco_xr": ("show interfaces description", True),
        "juniper_junos": ("show interfaces terse", True),
        "arista_eos": ("show interfaces status", True),
    }
    assert report["results"]["unsupported_os"]["error"]["code"] == "unsupported_platform"
    assert not nr.inventory.hosts["unsupported_os"].connections


def test_selection_limits_which_hosts_receive_commands(mixed_inventory):
    _, sent = mixed_inventory
    report = server.run_inspection("os_version", filter_criteria={"platform": "arista_eos"})
    assert report["summary"]["total"] == 1
    assert sent == {"arista_eos": ("show version", True)}
    details = server.get_execution_details(report["execution_id"])
    assert details["results"]["arista_eos"]["output"]["command"] == "show version"


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("cisco_xe", "cisco_ios"),
        ("ios", "cisco_ios"),
        ("nxos", "cisco_nxos"),
        ("iosxr", "cisco_xr"),
        ("junos", "juniper_junos"),
        ("eos", "arista_eos"),
        ("cisco_ios_telnet", "cisco_ios"),
    ],
)
def test_nornir_aliases_and_xe_use_the_correct_dialect(platform, expected):
    assert server._inspection_platform(Host(name="r1", platform=platform)) == expected


def test_effective_connection_platform_takes_precedence_over_host_platform():
    host = Host(
        name="r1",
        platform="cisco_ios",
        connection_options={
            "netmiko": ConnectionOptions(platform="juniper_junos", extras={"device_type": "arista_eos"})
        },
    )
    assert server._inspection_platform(host) == "arista_eos"


def test_nested_authentication_failure_keeps_its_machine_readable_code(mixed_inventory, monkeypatch):
    def login_failure(task, **kwargs):
        raise NetmikoAuthenticationException("login rejected")

    monkeypatch.setattr(server, "netmiko_send_command", login_failure)
    report = server.run_inspection("os_version", filter_criteria={"platform": "cisco_ios"})
    assert report["status"] == "failed"
    assert report["results"]["cisco_ios"]["error"]["code"] == "authentication_failed"
    assert report["results"]["cisco_ios"]["error"]["message"] == "login rejected"


def test_raw_text_is_preserved_when_no_parser_matches(mixed_inventory, monkeypatch):
    monkeypatch.setattr(
        server, "netmiko_send_command", lambda task, **kwargs: Result(host=task.host, result="raw output")
    )
    report = server.run_inspection("interfaces", filter_criteria={"platform": "juniper_junos"}, include_output=True)
    output = report["results"]["juniper_junos"]["output"]
    assert output["parsed"] is False
    assert output["facts"] == []
    assert output["data"] == "raw output"


@pytest.mark.parametrize("text", ["% Invalid input detected", "error: unknown command", "syntax error: invalid option"])
def test_device_command_rejection_is_not_reported_as_success(mixed_inventory, monkeypatch, text):
    monkeypatch.setattr(server, "netmiko_send_command", lambda task, **kwargs: Result(host=task.host, result=text))
    report = server.run_inspection("interfaces", filter_criteria={"platform": "cisco_ios"})
    assert report["results"]["cisco_ios"]["error"]["code"] == "command_rejected"


@pytest.mark.parametrize(
    "row,version,model",
    [
        ({"version": "17.12", "hardware": ["C9300"]}, "17.12", ["C9300"]),
        ({"junos_version": "23.4R1", "model": "EX4300"}, "23.4R1", "EX4300"),
        ({"os": "10.5(1)", "platform": "N9K"}, "10.5(1)", "N9K"),
        ({"image": "4.32", "model": "DCS-7050"}, "4.32", "DCS-7050"),
    ],
)
def test_os_version_facts_use_common_field_names(row, version, model):
    fact = server._inspection_facts("os_version", [row])[0]
    assert fact["version"] == version
    assert fact["model"] == model
    assert fact["hostname"] is None


def test_interface_facts_preserve_native_states():
    rows = [{"port": "Et1", "status": "connected", "name": "uplink"}]
    assert server._inspection_facts("interfaces", rows) == [
        {"name": "Et1", "status": "connected", "protocol": None, "description": "uplink"}
    ]


def test_invalid_inspection_does_not_access_inventory(monkeypatch):
    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: pytest.fail("must not load inventory"))
    assert server.run_inspection("arbitrary-command", all_hosts=True)["status"] == "failed"


def test_mcp_inspection_schema_constrains_the_action_and_marks_it_read_only():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["run_inspection"].inputSchema["properties"]["inspection"]["enum"] == ["os_version", "interfaces"]
    assert tools["run_inspection"].annotations.readOnlyHint is True
    assert tools["run_inspection"].annotations.destructiveHint is False
    _, report = asyncio.run(server.mcp.call_tool("get_inspection_presets", {}))
    assert [preset["name"] for preset in report["inspections"]] == ["os_version", "interfaces"]


def test_preset_listing_does_not_expose_mutable_command_registry():
    report = server.get_inspection_presets()
    report["inspections"][0]["commands"]["cisco_ios"] = "configure terminal"
    assert server.get_inspection_presets()["inspections"][0]["commands"]["cisco_ios"] == "show version"
