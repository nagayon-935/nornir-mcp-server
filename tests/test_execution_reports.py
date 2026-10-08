import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from nornir.core.task import AggregatedResult, MultiResult, Result

import server


@pytest.fixture(autouse=True)
def isolated_cache():
    with server._RESULT_LOCK:
        server._RESULT_CACHE.clear()
    yield
    with server._RESULT_LOCK:
        server._RESULT_CACHE.clear()


def aggregate(**results):
    report = AggregatedResult("task")
    for name, result in results.items():
        report[name] = MultiResult("task")
        report[name].append(result)
    return report


def test_partial_failure_has_counts_and_machine_readable_errors():
    report = server._format_agg_result(
        aggregate(
            r1=Result(host=None, result="version output"),
            r2=Result(host=None, failed=True, exception=NetmikoAuthenticationException("login rejected")),
        ),
        include_output=False,
    )
    assert report["status"] == "partial_failure"
    assert report["summary"] == {"total": 2, "succeeded": 1, "failed": 1, "duration_seconds": 0.0}
    assert report["results"]["r1"] == {"status": "success"}
    assert report["results"]["r2"]["error"]["code"] == "authentication_failed"
    details = server.get_execution_details(report["execution_id"], host_names=["r1"])
    assert details["results"]["r1"]["output"] == "version output"
    assert details["summary"] == report["summary"]


@pytest.mark.parametrize(
    "results,status",
    [
        ({}, "no_hosts"),
        ({"r1": Result(host=None, result="ok")}, "success"),
        ({"r1": Result(host=None, failed=True, exception=ValueError("failed"))}, "failed"),
    ],
)
def test_execution_statuses(results, status):
    assert server._format_agg_result(aggregate(**results))["status"] == status


@pytest.mark.parametrize(
    "error,code",
    [
        (NetmikoAuthenticationException("login rejected"), "authentication_failed"),
        (NetmikoTimeoutException("timed out"), "timeout"),
        (httpx.ReadTimeout("timed out"), "timeout"),
        (httpx.ConnectError("unreachable"), "connection_failed"),
    ],
)
def test_known_errors_provide_recovery_hints(error, code):
    report = server._format_agg_result(aggregate(r1=Result(host=None, failed=True, exception=error)))
    detail = report["results"]["r1"]["error"]
    assert detail["code"] == code
    assert detail["hint"]


def test_details_are_sorted_and_paginated():
    report = server._format_agg_result(
        aggregate(
            z=Result(host=None, result="z"),
            a=Result(host=None, result="a"),
            b=Result(host=None, result="b"),
        )
    )
    first = server.get_execution_details(report["execution_id"], limit=2)
    second = server.get_execution_details(report["execution_id"], offset=first["next_offset"], limit=2)
    assert list(first["results"]) == ["a", "b"]
    assert list(second["results"]) == ["z"]
    assert second["next_offset"] is None


def test_mutating_a_returned_report_does_not_change_cached_results():
    report = server._format_agg_result(aggregate(r1=Result(host=None, result={"value": 1})))
    execution_id = report["execution_id"]
    report["results"]["r1"]["output"]["value"] = 2
    details = server.get_execution_details(execution_id)
    assert details["results"]["r1"]["output"] == {"value": 1}
    details["results"]["r1"]["output"]["value"] = 3
    assert server.get_execution_details(execution_id)["results"]["r1"]["output"] == {"value": 1}


def test_cached_results_expire_without_rerunning_commands(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server, "monotonic", lambda: now[0])
    report = server._format_agg_result(aggregate(r1=Result(host=None, result="ok")))
    now[0] += server._RESULT_TTL
    assert server.get_execution_details(report["execution_id"])["error"]["code"] == "result_not_found"


def test_cache_evicts_least_recently_used_result(monkeypatch):
    monkeypatch.setattr(server, "_MAX_RESULTS", 2)
    first = server._format_agg_result(aggregate(r1=Result(host=None, result="1")))
    second = server._format_agg_result(aggregate(r1=Result(host=None, result="2")))
    server.get_execution_details(first["execution_id"])
    third = server._format_agg_result(aggregate(r1=Result(host=None, result="3")))
    assert server.get_execution_details(second["execution_id"])["status"] == "failed"
    assert server.get_execution_details(first["execution_id"])["status"] == "success"
    assert server.get_execution_details(third["execution_id"])["status"] == "success"


def test_oversized_result_is_returned_instead_of_losing_output(monkeypatch):
    monkeypatch.setattr(server, "_MAX_RESULT_BYTES", 1)
    report = server._format_agg_result(aggregate(r1=Result(host=None, result="large output")), include_output=False)
    assert report["execution_id"] is None
    assert report["details_available"] is False
    assert report["results"]["r1"]["output"] == "large output"


def test_cache_is_safe_under_concurrent_tool_calls():
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(
            pool.map(lambda value: server._format_agg_result(aggregate(r1=Result(host=None, result=value))), range(20))
        )
    for value, report in enumerate(reports):
        assert server.get_execution_details(report["execution_id"])["results"]["r1"]["output"] == value


def test_details_reject_unknown_hosts():
    report = server._format_agg_result(aggregate(r1=Result(host=None, result="ok")))
    assert (
        server.get_execution_details(report["execution_id"], host_names=["missing"])["error"]["code"] == "unknown_host"
    )


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 201)])
def test_details_reject_invalid_pagination(offset, limit):
    assert server.get_execution_details("missing", offset=offset, limit=limit)["error"]["code"] == "invalid_arguments"


def test_mcp_returns_validated_structured_execution_results(monkeypatch):
    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: pytest.fail("must not access inventory"))
    content, structured = asyncio.run(server.mcp.call_tool("run_netmiko_command", {"command": "show version"}))
    assert content
    assert structured["error"]["code"] == "target_selection_required"
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["run_netmiko_command"].outputSchema["properties"]["summary"]


def test_cleanup_failure_preserves_completed_task_results(monkeypatch):
    from types import SimpleNamespace

    class FailingCleanup:
        inventory = SimpleNamespace(hosts={"r1": object()})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise RuntimeError("cleanup failed")

        def run(self, **kwargs):
            return aggregate(r1=Result(host=None, result="configuration applied"))

    monkeypatch.setattr(server, "_get_nornir", lambda *args, **kwargs: FailingCleanup())
    report = server.run_netmiko_config(["hostname r1"], all_hosts=True)
    assert report["error"]["code"] == "cleanup_failed"
    assert report["summary"]["succeeded"] == 1
    assert server.get_execution_details(report["execution_id"])["results"]["r1"]["output"] == "configuration applied"
