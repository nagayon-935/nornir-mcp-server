import json

import pytest
from nornir.core import Nornir
from nornir.core.inventory import Host, Inventory
from nornir.core.task import Result
from nornir.plugins.runners import SerialRunner

import server
from server import (
    _JINJA_ENV,
    _get_nornir,
    custom_http_task,
    custom_serial_task,
    generate_config,
    get_inventory,
    netmiko_send_command,
    netmiko_send_config,
    run_http_request,
    run_netmiko_command,
    run_netmiko_config,
    run_serial_command,
)
from tests.support import CONNECTION_TOOLS, NO_MATCH_MESSAGE, FakeNornir, build_agg_result, fake_host


class TestGetNornir:
    def test_initializes_with_config_file_and_no_filter_by_default(self, monkeypatch):
        init_calls = []

        class RecordingNornir:
            def __init__(self):
                self.filters = []

            def filter(self, f_obj):
                self.filters.append(f_obj)
                return self

        fake_nr = RecordingNornir()

        def fake_init_nornir(**kwargs):
            init_calls.append(kwargs)
            return fake_nr

        monkeypatch.setattr(server, "InitNornir", fake_init_nornir)

        result = _get_nornir()

        assert result is fake_nr
        assert init_calls == [{"config_file": str(server.CONFIG_FILE)}]
        assert fake_nr.filters == []

    def test_applies_filter_and_num_workers_when_specified(self, monkeypatch):
        init_calls = []
        filter_calls = []

        class RecordingNornir:
            def filter(self, f_obj):
                filter_calls.append(f_obj)
                return self

        monkeypatch.setattr(server, "InitNornir", lambda **kwargs: (init_calls.append(kwargs), RecordingNornir())[1])

        _get_nornir(filter_criteria={"role": "core", "site": "tokyo"}, num_workers=1)

        assert init_calls == [
            {
                "config_file": str(server.CONFIG_FILE),
                "runner": {"plugin": "threaded", "options": {"num_workers": 1}},
            }
        ]
        assert len(filter_calls) == 1


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
    def test_serial_command_forces_single_worker_and_passes_command(self, patched_nornir):
        agg_result = build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"router1": fake_host()}, agg_result=agg_result))

        run_serial_command("show version", filter_criteria={"site": "tokyo"})

        assert patched_nornir.calls[-1] == {
            "filter_criteria": {"site": "tokyo"},
            "num_workers": 1,
        }
        assert nr.run_calls == [(custom_serial_task, {"command": "show version"})]

    def test_netmiko_command_forwards_command_and_textfsm_flag(self, patched_nornir):
        agg_result = build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"router1": fake_host()}, agg_result=agg_result))

        run_netmiko_command("show ip int brief", filter_criteria={"role": "core"}, use_textfsm=True)

        assert patched_nornir.calls[-1] == {"filter_criteria": {"role": "core"}, "num_workers": None}
        assert nr.run_calls == [(netmiko_send_command, {"command_string": "show ip int brief", "use_textfsm": True})]

    def test_netmiko_config_forwards_config_commands(self, patched_nornir):
        agg_result = build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"router1": fake_host()}, agg_result=agg_result))

        run_netmiko_config(["interface Gi0/1", "description uplink"], filter_criteria={"role": "core"})

        assert patched_nornir.calls[-1] == {"filter_criteria": {"role": "core"}, "num_workers": None}
        assert nr.run_calls == [(netmiko_send_config, {"config_commands": ["interface Gi0/1", "description uplink"]})]

    def test_http_request_forwards_method_path_and_json_data(self, patched_nornir):
        agg_result = build_agg_result(Result(host=None, result="ok"))
        nr = patched_nornir(FakeNornir(hosts={"router1": fake_host()}, agg_result=agg_result))

        run_http_request("POST", "/api/v1/items", filter_criteria={"site": "tokyo"}, json_data={"key": "val"})

        assert patched_nornir.calls[-1] == {"filter_criteria": {"site": "tokyo"}, "num_workers": None}
        task, run_kwargs = nr.run_calls[0]
        assert task is custom_http_task
        assert run_kwargs["method"] == "POST"
        assert run_kwargs["path"] == "/api/v1/items"
        assert run_kwargs["json_data"] == {"key": "val"}

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
