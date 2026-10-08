import json
import logging
import os
from collections import Counter, OrderedDict
from contextlib import ExitStack
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Literal, NotRequired, TypedDict
from uuid import uuid4

import httpx
from jinja2 import Template
from jinja2.sandbox import SandboxedEnvironment
from mcp.server.fastmcp import FastMCP
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.filter import F
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, Result, Task
from nornir_netmiko.tasks import netmiko_send_command, netmiko_send_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nornir-mcp-server")

CONFIG_FILE = Path(os.environ.get("NORNIR_MCP_CONFIG", str(Path(__file__).parent / "config.yaml")))
# Default per-request HTTP timeout; override per host with 'http_timeout' in host.data.
HTTP_TIMEOUT = 10.0
SERIAL_DEVICE_TYPE_SUFFIX = "_serial"

# Mirrors the direct imports above (and pyproject.toml): `mcp install` builds its
# isolated environment from this list, so a missing entry breaks that path at import.
mcp = FastMCP(
    "nornir server",
    dependencies=["httpx", "jinja2", "netmiko", "nornir", "nornir-netbox", "nornir-netmiko"],
)

# Jinja2 environments are thread-safe and shareable across renders.
_JINJA_ENV = SandboxedEnvironment()


class ErrorDetail(TypedDict):
    code: str
    message: str
    hint: str


class HostResult(TypedDict):
    status: Literal["success", "failed"]
    output: NotRequired[Any]
    error: NotRequired[ErrorDetail]


class ExecutionSummary(TypedDict):
    total: int
    succeeded: int
    failed: int
    duration_seconds: float


class ExecutionReport(TypedDict):
    status: Literal["success", "partial_failure", "failed", "no_hosts"]
    execution_id: str | None
    summary: ExecutionSummary
    results: dict[str, HostResult]
    error: NotRequired[ErrorDetail]
    next_offset: NotRequired[int | None]
    details_available: NotRequired[bool]
    message: NotRequired[str]


_RESULT_TTL = 900.0
_MAX_RESULTS = 100
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_RESULT_CACHE: OrderedDict[str, tuple[float, int, ExecutionReport]] = OrderedDict()
_RESULT_LOCK = Lock()


def _purge_results(now: float) -> None:
    """Caller holds _RESULT_LOCK; expiry follows creation, independent of LRU order."""
    for execution_id, (created, _, _) in list(_RESULT_CACHE.items()):
        if now - created >= _RESULT_TTL:
            del _RESULT_CACHE[execution_id]


def _error_detail(error: Exception | None, message: str) -> ErrorDetail:
    code, hint = "execution_error", "Check the operation and server configuration."
    if isinstance(error, NetmikoAuthenticationException):
        code, hint = "authentication_failed", "Check the device credentials and account permissions."
    elif isinstance(error, (NetmikoTimeoutException, httpx.TimeoutException)):
        code, hint = "timeout", "Check device reachability and the configured timeout."
    elif isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in (401, 403):
            code, hint = "authentication_failed", "Check the HTTP credentials and endpoint permissions."
        else:
            code, hint = "http_error", "Check the HTTP method, path and payload."
    elif isinstance(error, httpx.ConnectError):
        code, hint = "connection_failed", "Check the address, network connectivity and TLS settings."
    elif message.startswith("Target selection required:"):
        code, hint = (
            "target_selection_required",
            "Preview targets and provide a filter, or explicitly set all_hosts=True.",
        )
    return {"code": code, "message": message, "hint": hint}


def _empty_report(status: Literal["failed", "no_hosts"], started: float | None = None) -> ExecutionReport:
    return {
        "status": status,
        "execution_id": None,
        "summary": {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "duration_seconds": round(monotonic() - started, 3) if started is not None else 0.0,
        },
        "results": {},
    }


def _tool_error(
    operation: str, error: Exception, started: float, completed: ExecutionReport | None = None
) -> ExecutionReport:
    logger.exception("Error %s", operation)
    if completed is not None:
        report = {**completed, "status": "partial_failure" if completed["summary"]["succeeded"] else "failed"}
        report["error"] = _error_detail(error, str(error))
        report["error"]["code"] = "cleanup_failed"
        report["message"] = "Tasks completed but cleanup failed. Inspect their results before repeating any operation."
        with _RESULT_LOCK:
            entry = _RESULT_CACHE.get(report["execution_id"])
            if entry is not None:
                full_report = {
                    **entry[2],
                    "status": report["status"],
                    "error": report["error"],
                    "message": report["message"],
                }
                _RESULT_CACHE[report["execution_id"]] = (entry[0], entry[1], full_report)
        return report
    report = _empty_report("failed", started)
    report["error"] = _error_detail(error, str(error))
    return report


def _get_nornir(filter_criteria: dict[str, Any] | None = None, num_workers: int | None = None) -> Nornir:
    """Initialize Nornir and apply filtering if provided."""
    kwargs: dict[str, Any] = {}
    if num_workers is not None:
        kwargs["runner"] = {"plugin": "threaded", "options": {"num_workers": num_workers}}
    nr = InitNornir(config_file=str(CONFIG_FILE), **kwargs)
    if filter_criteria:
        # F(**filter_criteria) allows matching by attributes like site, role, name, etc.
        nr = nr.filter(F(**filter_criteria))
    return nr


def _format_agg_result(
    agg_result: AggregatedResult, include_output: bool = True, started: float | None = None
) -> ExecutionReport:
    """Summarize a run and cache its full output for later, paginated retrieval."""
    if not agg_result:
        return _empty_report("no_hosts", started)
    output_data: dict[str, HostResult] = {}
    for host_name, multi_result in agg_result.items():
        if len(multi_result) == 0:
            output_data[host_name] = {"status": "failed", "error": _error_detail(None, "No result returned for host.")}
            continue
        failed_result = next((result for result in multi_result if result.failed), None)
        if failed_result is not None:
            result = failed_result
            detail = str(result.exception) if result.exception else str(result.result)
            output_data[host_name] = {"status": "failed", "error": _error_detail(result.exception, detail)}
        else:
            output_data[host_name] = {"status": "success", "output": multi_result[0].result}
    failed = sum(result["status"] == "failed" for result in output_data.values())
    total = len(output_data)
    report: ExecutionReport = {
        "status": "success" if not failed else "failed" if failed == total else "partial_failure",
        "execution_id": uuid4().hex,
        "summary": {
            "total": total,
            "succeeded": total - failed,
            "failed": failed,
            "duration_seconds": round(monotonic() - started, 3) if started is not None else 0.0,
        },
        "results": output_data,
        "details_available": True,
    }
    # Normalize non-JSON values once so MCP output and cached details agree.
    serialized = json.dumps(report, default=str)
    report = json.loads(serialized)
    size = len(serialized.encode())
    with _RESULT_LOCK:
        _purge_results(monotonic())
        if size <= _MAX_RESULT_BYTES:
            while _RESULT_CACHE and (
                len(_RESULT_CACHE) >= _MAX_RESULTS
                or sum(entry[1] for entry in _RESULT_CACHE.values()) + size > _MAX_RESULT_BYTES
            ):
                _RESULT_CACHE.popitem(last=False)
            _RESULT_CACHE[report["execution_id"]] = (monotonic(), size, report)
        else:
            report["execution_id"] = None
            report["details_available"] = False
            report["message"] = "Output exceeds the result-cache limit; full output is included in this response."
            include_output = True
    if include_output:
        return json.loads(json.dumps(report))
    return {
        **report,
        "results": {
            name: {k: v for k, v in result.items() if k != "output"} for name, result in report["results"].items()
        },
    }


@mcp.tool()
def get_execution_details(
    execution_id: str, host_names: list[str] | None = None, offset: int = 0, limit: int = 20
) -> ExecutionReport:
    """Retrieve stored output without rerunning an operation or connecting to devices.

    Results expire after 15 minutes and may be evicted earlier from the bounded in-memory
    cache. A server restart clears them. Select exact host_names or paginate by sorted host
    name (limit 1–200). summary/status always describe the full original execution.
    """
    if offset < 0 or not 1 <= limit <= 200:
        report = _empty_report("failed")
        report["error"] = _error_detail(None, "offset must be non-negative and limit must be between 1 and 200.")
        report["error"]["code"] = "invalid_arguments"
        return report
    with _RESULT_LOCK:
        _purge_results(monotonic())
        entry = _RESULT_CACHE.get(execution_id)
        if entry is None:
            report = _empty_report("failed")
            report["error"] = {
                "code": "result_not_found",
                "message": "Execution ID is unknown, expired or evicted.",
                "hint": "Results are temporary. Do not repeat configuration changes just to retrieve their output.",
            }
            return report
        _RESULT_CACHE.move_to_end(execution_id)
        cached = entry[2]
        names = sorted(cached["results"] if host_names is None else set(host_names))
        if any(name not in cached["results"] for name in names):
            report = _empty_report("failed")
            report["error"] = _error_detail(None, "A requested host is not part of this execution.")
            report["error"]["code"] = "unknown_host"
            return report
        selected = names[offset : offset + limit]
        report = {
            **cached,
            "results": {name: cached["results"][name] for name in selected},
            "next_offset": offset + len(selected) if offset + len(selected) < len(names) else None,
        }
        # Avoid returning mutable references into the shared cache.
        return json.loads(json.dumps(report))


def _host_metadata(host: Host) -> dict[str, Any]:
    """Expose host metadata and inherited data without top-level credentials."""
    return {
        "hostname": host.hostname,
        "platform": host.platform,
        "groups": [group.name for group in host.groups],
        "data": dict(host.items()),
    }


def _target_nornir(filter_criteria: dict[str, Any] | None, all_hosts: bool, num_workers: int | None = None) -> Nornir:
    """Require a deliberate selection before initializing any inventory backend."""
    if not filter_criteria and not all_hosts:
        raise ValueError("Target selection required: provide non-empty filter_criteria or set all_hosts=True.")
    return _get_nornir(filter_criteria, num_workers=num_workers)


def _inventory_choices(nr: Nornir) -> dict[str, list[dict[str, Any]]]:
    """Only aggregate public selection fields, never arbitrary inventory data."""
    choices = {}
    for field in ("site", "role", "platform"):
        counts = Counter()
        for host in nr.inventory.hosts.values():
            value = host.get(field)
            filter_field = field
            if isinstance(value, dict):
                nested_field = "slug" if value.get("slug") else "name"
                filter_field = f"{field}__{nested_field}"
                value = value.get(nested_field)
            if isinstance(value, str) and value:
                counts[(filter_field, value)] += 1
        choices[field] = [
            {"value": value, "count": count, "filter_criteria": {filter_field: value}}
            for (filter_field, value), count in sorted(counts.items())
        ]
    return choices


@mcp.tool()
def get_inventory_summary() -> dict[str, Any]:
    """Discover registered site, role and platform values and host counts without connecting to devices.

    For NetBox, site/role values are slugs from nested metadata; use site__slug/role__slug
    in filters. For YAML string metadata, use site/role directly.
    """
    try:
        nr = _get_nornir()
        return {"status": "success", "total_hosts": len(nr.inventory.hosts), "choices": _inventory_choices(nr)}
    except Exception:
        logger.exception("Error summarizing inventory")
        return {"status": "failed", "error": "Could not load inventory. Check the server configuration."}


@mcp.tool()
def preview_targets(filter_criteria: dict[str, Any] | None = None, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """Preview matching hosts, counts and platforms without connecting to devices.

    Empty filters preview all registered hosts. This is a live inventory preview, not
    a reservation: execution reloads the inventory. limit must be between 1 and 200.
    Only names, addresses, platforms and groups are returned; no credential data.
    """
    if offset < 0 or not 1 <= limit <= 200:
        return {"status": "failed", "error": "offset must be non-negative and limit must be between 1 and 200."}
    try:
        nr = _get_nornir()
        available_choices = _inventory_choices(nr)
        if filter_criteria:
            nr = nr.filter(F(**filter_criteria))
        names = sorted(nr.inventory.hosts)
        hosts = []
        for name in names[offset : offset + limit]:
            host = nr.inventory.hosts[name]
            hosts.append(
                {
                    "name": name,
                    "hostname": host.hostname,
                    "platform": host.platform,
                    "groups": [group.name for group in host.groups],
                }
            )
        next_offset = offset + len(hosts)
        return {
            "status": "success" if names else "no_hosts",
            "matched_hosts": len(names),
            "hosts": hosts,
            "next_offset": next_offset if next_offset < len(names) else None,
            "choices": available_choices,
            "platforms": _inventory_choices(nr)["platform"],
        }
    except Exception:
        logger.exception("Error previewing targets (filter=%s)", filter_criteria)
        return {"status": "failed", "error": "Could not load or filter inventory. Check the filter and configuration."}


@mcp.tool()
def get_inventory(filter_criteria: dict[str, Any] | None = None) -> str:
    """
    Get inventory hosts and their metadata, optionally filtered.
    Returned data includes values inherited from groups and defaults.

    Args:
        filter_criteria: Dictionary of key-value pairs to filter hosts
                         (e.g. {"name": "router1"} or {"role": "core", "site": "tokyo"}).
                         Matches host attributes or data fields.
    """
    try:
        nr = _get_nornir(filter_criteria)
        hosts_info = {name: _host_metadata(host) for name, host in nr.inventory.hosts.items()}
        return json.dumps(hosts_info, indent=2, default=str)
    except Exception as e:
        logger.exception(f"Error loading inventory (filter={filter_criteria})")
        return f"Error loading inventory: {str(e)}"


@mcp.tool()
def run_netmiko_command(
    command: str,
    filter_criteria: dict[str, Any] | None = None,
    use_textfsm: bool = False,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    Run a show command concurrently on multiple hosts via Netmiko.

    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Non-empty dictionary of key-value pairs to filter target hosts.
        use_textfsm: If True, attempts to parse output into structured data using ntc-templates.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        # Nornir's context manager also closes connections on failed hosts.
        with _target_nornir(filter_criteria, all_hosts) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)
            agg_result = nr.run(
                task=netmiko_send_command, raise_on_error=False, command_string=command, use_textfsm=use_textfsm
            )
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing command", e, started, report)


def _get_tls_verify(host: Host) -> bool:
    return bool(host.get("tls_verify", True))


def _get_http_timeout(host: Host) -> float:
    """Per-host request timeout; falls back to HTTP_TIMEOUT when unset."""
    return float(host.get("http_timeout", HTTP_TIMEOUT))


def custom_http_task(
    task: Task,
    clients: dict[bool, httpx.Client],
    method: str,
    path: str,
    json_data: dict | None = None,
) -> Result:
    """Custom Nornir task to execute HTTP requests using shared clients."""
    # Build base URL from host attributes
    host = task.host
    base_url = host.get("base_url", f"https://{host.hostname}")
    headers = host.get("http_headers", {})
    verify = _get_tls_verify(host)
    if not verify:
        logger.warning(f"TLS verification disabled for host {host.name}")

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    response = clients[verify].request(
        method=method, url=url, headers=headers, json=json_data, timeout=_get_http_timeout(host)
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError:
        result = response.text

    return Result(host=task.host, result=result)


@mcp.tool()
def run_http_request(
    method: str,
    path: str,
    filter_criteria: dict[str, Any] | None = None,
    json_data: dict | None = None,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    Run an HTTP API request (REST) concurrently on multiple hosts.

    Args:
        method: HTTP method (e.g., 'GET', 'POST', 'PUT').
        path: API path to request (e.g., '/api/v1/system').
        filter_criteria: Dictionary to filter target hosts.
        json_data: Optional JSON payload for POST/PUT requests.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.

    Per-host settings come from inventory data, including group/default inheritance:
    'base_url', 'http_headers', 'tls_verify', and 'http_timeout' (seconds, defaults to 10).
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts) as nr, ExitStack() as stack:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)

            # Register cleanup immediately, including for partial initialization.
            # Clients are shared across threads, one per TLS verification setting.
            verify_values = {_get_tls_verify(host) for host in nr.inventory.hosts.values()}
            clients = {}
            for verify in verify_values:
                client = httpx.Client(verify=verify, timeout=HTTP_TIMEOUT)
                stack.callback(client.close)
                clients[verify] = client
            agg_result = nr.run(
                task=custom_http_task,
                raise_on_error=False,
                clients=clients,
                method=method,
                path=path,
                json_data=json_data,
            )
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing HTTP request", e, started, report)


def _resolve_serial_device_type(serial_settings: dict[str, Any], platform: str | None) -> str:
    """Netmiko selects its serial drivers by a device_type ending in '_serial'."""
    device_type = serial_settings.get("device_type") or platform
    if not device_type:
        raise ValueError(
            "Cannot determine serial device_type: set 'device_type' in serial_settings or 'platform' on the host."
        )
    if not device_type.endswith(SERIAL_DEVICE_TYPE_SUFFIX):
        device_type = f"{device_type}{SERIAL_DEVICE_TYPE_SUFFIX}"
    return device_type


def custom_serial_task(task: Task, command: str) -> Result:
    """Custom Nornir task to execute commands via Serial port using Netmiko."""
    host = task.host
    serial_settings = host.get("serial_settings")
    if not serial_settings:
        raise ValueError("No 'serial_settings' found in host data.")

    device_type = _resolve_serial_device_type(serial_settings, host.platform)
    # 'device_type' is Netmiko metadata; the rest goes straight to pyserial.
    pyserial_settings = {key: value for key, value in serial_settings.items() if key != "device_type"}

    connection_params = {
        "device_type": device_type,
        "serial_settings": pyserial_settings,
        "username": host.username,
        "password": host.password,
    }

    # Instantiate ConnectHandler directly for serial
    with ConnectHandler(**connection_params) as net_connect:
        output = net_connect.send_command(command)

    return Result(host=task.host, result=output)


@mcp.tool()
def run_serial_command(
    command: str, filter_criteria: dict[str, Any] | None = None, all_hosts: bool = False, include_output: bool = False
) -> ExecutionReport:
    """
    Run a CLI command on devices via direct Serial connection (using Netmiko).

    Note: Physical serial ports generally cannot be used concurrently by multiple threads,
    so hosts are processed sequentially (one at a time).

    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Dictionary to filter target hosts.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts, num_workers=1) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)
            agg_result = nr.run(task=custom_serial_task, raise_on_error=False, command=command)
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing serial command", e, started, report)


def custom_template_task(task: Task, template: Template) -> Result:
    """Custom Nornir task to render a precompiled Jinja2 template using host inventory data.

    The template MUST come from `_JINJA_ENV` (the module-level SandboxedEnvironment).
    The annotation cannot express that: a plain `jinja2.Template` would render here just
    as well, with the sandbox silently gone.
    """
    host = task.host
    # Exclude top-level credentials; inventory data is exposed as provided.
    safe_host = {"name": host.name, **_host_metadata(host)}
    rendered = template.render(host=safe_host)
    return Result(host=task.host, result=rendered)


@mcp.tool()
def generate_config(
    template_string: str,
    filter_criteria: dict[str, Any] | None = None,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    Generate configuration from a Jinja2 template string for each target host.
    Does NOT deploy the configuration; useful for dry-runs and auditing.

    Args:
        template_string: Jinja2 template string. Available variables: {{ host.name }}, {{ host.hostname }},
                         {{ host.platform }}, {{ host.groups }}, {{ host.data['key'] }}. Other host
                         attributes (e.g. credentials) are not exposed.
                         host.data includes inherited group/default values.
        filter_criteria: Dictionary to filter target hosts.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)

            # Compile once for all hosts, inside the connection cleanup scope.
            template = _JINJA_ENV.from_string(template_string)
            agg_result = nr.run(task=custom_template_task, raise_on_error=False, template=template)
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("generating config", e, started, report)


@mcp.tool()
def run_netmiko_config(
    commands: list[str],
    filter_criteria: dict[str, Any] | None = None,
    all_hosts: bool = False,
    include_output: bool = False,
) -> ExecutionReport:
    """
    WRITE OPERATION: modifies device configuration.
    Deploy configuration commands concurrently to multiple hosts via Netmiko.
    Confirm with the user before calling this tool.

    Args:
        commands: List of configuration commands to execute.
        filter_criteria: Dictionary to filter target hosts.
        all_hosts: Explicitly allow all hosts when filter_criteria is empty. Defaults to False.
        include_output: Include full output now; otherwise retrieve it later with get_execution_details.
    """
    started = monotonic()
    report = None
    try:
        with _target_nornir(filter_criteria, all_hosts) as nr:
            if not nr.inventory.hosts:
                return _empty_report("no_hosts", started)
            agg_result = nr.run(task=netmiko_send_config, raise_on_error=False, config_commands=commands)
            report = _format_agg_result(agg_result, include_output, started)
            return report
    except Exception as e:
        return _tool_error("executing config commands", e, started, report)


if __name__ == "__main__":
    mcp.run()
