"""Shared fakes and builders for the test suite."""

from types import SimpleNamespace

import pytest
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, MultiResult, Result

from server import (
    generate_config,
    run_http_request,
    run_netmiko_command,
    run_netmiko_config,
    run_serial_command,
)

NO_MATCH_MESSAGE = "No hosts matched the filter criteria."


def fake_host(**data) -> Host:
    """Build a real inventory host without opening device connections."""
    return Host(name="router1", hostname="192.168.1.1", data=data)


def build_agg_result(result: Result) -> AggregatedResult:
    multi_result = MultiResult("task")
    multi_result.append(result)
    agg_result = AggregatedResult("task")
    agg_result["router1"] = multi_result
    return agg_result


class FakeNornir:
    """Stands in for a filtered Nornir object so tools can be tested without devices."""

    def __init__(self, hosts=None, agg_result=None, run_error=None):
        self.inventory = SimpleNamespace(hosts=hosts if hosts is not None else {})
        self.agg_result = agg_result
        self.run_error = run_error
        self.closed = False
        self.closed_failed_hosts = False
        self.run_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close_connections(on_failed=True)
        return False

    def run(self, task, **kwargs):
        self.run_calls.append((task, kwargs))
        if self.run_error is not None:
            raise self.run_error
        return self.agg_result

    def close_connections(self, on_failed=False):
        self.closed = True
        self.closed_failed_hosts = on_failed


# Every tool that opens connections: (callable, kwargs needed to invoke it).
CONNECTION_TOOLS = [
    pytest.param(run_netmiko_command, {"command": "show version"}, id="run_netmiko_command"),
    pytest.param(run_http_request, {"method": "GET", "path": "/api"}, id="run_http_request"),
    pytest.param(run_serial_command, {"command": "show version"}, id="run_serial_command"),
    pytest.param(generate_config, {"template_string": "hostname {{ host.name }}"}, id="generate_config"),
    pytest.param(run_netmiko_config, {"commands": ["hostname r1"]}, id="run_netmiko_config"),
]


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.requests = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        return self._response


class FakeConnectHandler:
    """Records the params netmiko would receive, and doubles as its context manager."""

    instances = []

    def __init__(self, **params):
        self.params = params
        self.commands = []
        self.exited = False
        FakeConnectHandler.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.exited = True
        return False

    def send_command(self, command):
        self.commands.append(command)
        return f"output of {command}"
