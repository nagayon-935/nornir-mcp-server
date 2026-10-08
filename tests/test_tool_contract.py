import json

import pytest
from nornir.core import Nornir
from nornir.core.inventory import Host, Inventory
from nornir.core.task import Result
from nornir.plugins.runners import SerialRunner

import server
from server import _JINJA_ENV, generate_config, get_inventory, run_netmiko_command, run_serial_command
from tests.support import CONNECTION_TOOLS, NO_MATCH_MESSAGE, FakeNornir, build_agg_result, fake_host


class TestNoHostsShortCircuit:
    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_returns_no_match_message_without_running(self, patched_nornir, tool, kwargs):
        nr = patched_nornir(FakeNornir(hosts={}))

        assert tool(**kwargs) == NO_MATCH_MESSAGE
        assert nr.run_calls == []
        assert nr.closed is True

    def test_get_inventory_returns_empty_mapping(self, patched_nornir):
        patched_nornir(FakeNornir(hosts={}))

        assert json.loads(get_inventory()) == {}


class TestConnectionCleanup:
    """CLAUDE.md treats close_connections() as mandatory: a leak holds device VTYs open."""

    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_closes_connections_on_success(self, patched_nornir, tool, kwargs):
        agg_result = build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"router1": fake_host()}, agg_result=agg_result))

        assert json.loads(tool(**kwargs)) == {"router1": {"output": "ok"}}
        assert nr.closed is True
        assert nr.closed_failed_hosts is True

    @pytest.mark.parametrize("tool,kwargs", CONNECTION_TOOLS)
    def test_closes_connections_and_reports_error_when_run_raises(self, patched_nornir, tool, kwargs):
        nr = patched_nornir(FakeNornir(hosts={"router1": fake_host()}, run_error=RuntimeError("boom")))

        output = tool(**kwargs)

        assert nr.closed is True
        assert "boom" in output
        assert output.startswith("Error ")


class TestToolRunnerArguments:
    def test_serial_command_forces_single_worker(self, patched_nornir):
        agg_result = build_agg_result(Result(host=None, result="ok"))
        patched_nornir(FakeNornir(hosts={"router1": fake_host()}, agg_result=agg_result))

        run_serial_command("show version", filter_criteria={"site": "tokyo"})

        assert patched_nornir.calls[-1] == {
            "filter_criteria": {"site": "tokyo"},
            "num_workers": 1,
        }

    @pytest.mark.parametrize(
        "tool,kwargs",
        [param for param in CONNECTION_TOOLS if param.id != "run_serial_command"],
    )
    def test_other_tools_leave_worker_count_to_config(self, patched_nornir, tool, kwargs):
        agg_result = build_agg_result(Result(host=None, result="ok"))
        patched_nornir(FakeNornir(hosts={"router1": fake_host()}, agg_result=agg_result))

        tool(**kwargs)

        assert patched_nornir.calls[-1]["num_workers"] is None

    def test_generate_config_compiles_template_once_for_all_hosts(self, patched_nornir, monkeypatch):
        compiled = []
        original_from_string = _JINJA_ENV.from_string

        def counting_from_string(source, *args, **kwargs):
            compiled.append(source)
            return original_from_string(source, *args, **kwargs)

        monkeypatch.setattr(_JINJA_ENV, "from_string", counting_from_string)
        agg_result = build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"r1": fake_host(), "r2": fake_host()}, agg_result=agg_result))

        generate_config("hostname {{ host.name }}")

        # Compiled exactly once for a two-host run, and handed to the task already
        # compiled, so the task cannot re-parse it per host.
        assert compiled == ["hostname {{ host.name }}"]
        _, run_kwargs = nr.run_calls[0]
        assert "template_string" not in run_kwargs
        assert hasattr(run_kwargs["template"], "render")

    def test_generate_config_reports_bad_template_once_without_running(self, patched_nornir):
        """A malformed template fails at compile time, before any host task is dispatched."""
        nr = patched_nornir(FakeNornir(hosts={"r1": fake_host(), "r2": fake_host()}))

        output = generate_config("hostname {{ unclosed")

        assert output.startswith("Error generating config:")
        assert nr.run_calls == []
        assert nr.closed is True


def test_failed_nornir_hosts_have_their_connections_closed(monkeypatch):
    """Use the real Nornir runner: the fake cannot detect its failed-host exclusion."""
    hosts = {name: Host(name=name) for name in ("healthy", "failed")}
    closed_hosts = []

    class Connection:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed_hosts.append(self.name)

    def command_task(task, **kwargs):
        task.host.connections["netmiko"] = Connection(task.host.name)
        if task.host.name == "failed":
            raise RuntimeError("command failed after connecting")
        return Result(host=task.host, result="ok")

    nr = Nornir(inventory=Inventory(hosts=hosts), runner=SerialRunner())
    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: nr)
    monkeypatch.setattr(server, "netmiko_send_command", command_task)

    output = json.loads(run_netmiko_command("show version"))

    assert output["healthy"] == {"output": "ok"}
    assert output["failed"] == {"error": "command failed after connecting"}
    assert sorted(closed_hosts) == ["failed", "healthy"]
    assert all(not host.connections for host in hosts.values())
