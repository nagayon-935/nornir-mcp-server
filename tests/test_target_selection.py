import asyncio

import pytest
from nornir.core import Nornir
from nornir.core.inventory import Defaults, Group, Host, Inventory, ParentGroups
from nornir.plugins.runners import SerialRunner

import server


@pytest.fixture
def inventory(monkeypatch):
    group = Group(name="tokyo", data={"site": "tokyo", "http_headers": {"Authorization": "secret-token"}})
    defaults = Defaults(data={"role": "core"})
    hosts = {
        "r2": Host(name="r2", hostname="192.0.2.2", platform="juniper_junos", data={"site": "osaka", "role": "edge"}),
        "r1": Host(
            name="r1", hostname="192.0.2.1", platform="cisco_ios", groups=ParentGroups([group]), defaults=defaults
        ),
    }
    nr = Nornir(inventory=Inventory(hosts=hosts), runner=SerialRunner())
    monkeypatch.setattr(server, "_get_nornir", lambda filters=None, **kwargs: nr.filter(**filters) if filters else nr)
    return nr


def test_summary_discovers_inherited_filter_values(inventory):
    report = server.get_inventory_summary()
    assert report["total_hosts"] == 2
    assert report["choices"]["site"] == [
        {"value": "osaka", "count": 1, "filter_criteria": {"site": "osaka"}},
        {"value": "tokyo", "count": 1, "filter_criteria": {"site": "tokyo"}},
    ]
    assert "secret-token" not in str(report)


def test_preview_filters_and_excludes_credentials(inventory):
    report = server.preview_targets({"site": "tokyo"})
    assert report["matched_hosts"] == 1
    assert report["hosts"] == [{"name": "r1", "hostname": "192.0.2.1", "platform": "cisco_ios", "groups": ["tokyo"]}]
    assert report["next_offset"] is None
    assert "secret-token" not in str(report)
    assert not inventory.data.failed_hosts


def test_preview_is_paginated_in_stable_name_order(inventory):
    first = server.preview_targets(limit=1)
    second = server.preview_targets(offset=first["next_offset"], limit=1)
    assert first["hosts"][0]["name"] == "r1"
    assert second["hosts"][0]["name"] == "r2"
    assert second["next_offset"] is None


def test_no_match_still_returns_available_choices(inventory):
    report = server.preview_targets({"site": "missing"})
    assert report["status"] == "no_hosts"
    assert report["matched_hosts"] == 0
    assert report["choices"]["site"]


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 201)])
def test_invalid_pagination_does_not_load_inventory(monkeypatch, offset, limit):
    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: pytest.fail("must not load inventory"))
    assert server.preview_targets(offset=offset, limit=limit)["status"] == "failed"


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        (server.run_netmiko_command, {"command": "show version"}),
        (server.run_http_request, {"method": "GET", "path": "/api"}),
        (server.run_serial_command, {"command": "show version"}),
        (server.generate_config, {"template_string": "hostname {{ host.name }}"}),
        (server.run_netmiko_config, {"commands": ["hostname r1"]}),
        (server.run_inspection, {"inspection": "os_version"}),
    ],
)
@pytest.mark.parametrize("filters", [None, {}])
def test_missing_selection_is_rejected_before_inventory_access(monkeypatch, tool, kwargs, filters):
    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: pytest.fail("must not load inventory"))
    assert tool(**kwargs, filter_criteria=filters)["error"]["code"] == "target_selection_required"


def test_netbox_choices_provide_the_correct_nested_filter(monkeypatch):
    nr = Nornir(inventory=Inventory(hosts={"r1": Host(name="r1", data={"site": {"slug": "tokyo", "name": "Tokyo"}})}))
    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: nr)
    choice = server.get_inventory_summary()["choices"]["site"][0]
    assert choice["filter_criteria"] == {"site__slug": "tokyo"}


def test_discovery_tools_have_mcp_output_schemas():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["preview_targets"].outputSchema
    assert tools["get_inventory_summary"].outputSchema
