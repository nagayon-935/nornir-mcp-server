import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Template
from jinja2.sandbox import SandboxedEnvironment
from mcp.server.fastmcp import FastMCP
from netmiko import ConnectHandler
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


def _format_agg_result(agg_result: AggregatedResult) -> str:
    """Convert an AggregatedResult into a JSON string of per-host outputs/errors."""
    output_data: dict[str, dict[str, Any]] = {}
    for host_name, multi_result in agg_result.items():
        if len(multi_result) == 0:
            output_data[host_name] = {"error": "No result returned for host."}
            continue
        failed_result = next((result for result in multi_result if result.failed), None)
        if failed_result is not None:
            result = failed_result
            detail = str(result.exception) if result.exception else str(result.result)
            output_data[host_name] = {"error": detail}
        else:
            output_data[host_name] = {"output": multi_result[0].result}
    return json.dumps(output_data, indent=2, default=str)


def _host_metadata(host: Host) -> dict[str, Any]:
    """Expose host metadata and inherited data without top-level credentials."""
    return {
        "hostname": host.hostname,
        "platform": host.platform,
        "groups": [group.name for group in host.groups],
        "data": dict(host.items()),
    }


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
def run_netmiko_command(command: str, filter_criteria: dict[str, Any] | None = None, use_textfsm: bool = False) -> str:
    """
    Run a show command concurrently on multiple hosts via Netmiko.

    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Dictionary of key-value pairs to filter target hosts. If empty, runs on ALL hosts!
        use_textfsm: If True, attempts to parse output into structured data using ntc-templates.
    """
    try:
        # Nornir's context manager also closes connections on failed hosts.
        with _get_nornir(filter_criteria) as nr:
            if not nr.inventory.hosts:
                return "No hosts matched the filter criteria."
            agg_result = nr.run(task=netmiko_send_command, command_string=command, use_textfsm=use_textfsm)
            return _format_agg_result(agg_result)
    except Exception as e:
        logger.exception(f"Error executing command (command={command!r}, filter={filter_criteria})")
        return f"Error executing command: {str(e)}"


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
) -> str:
    """
    Run an HTTP API request (REST) concurrently on multiple hosts.

    Args:
        method: HTTP method (e.g., 'GET', 'POST', 'PUT').
        path: API path to request (e.g., '/api/v1/system').
        filter_criteria: Dictionary to filter target hosts.
        json_data: Optional JSON payload for POST/PUT requests.

    Per-host settings come from inventory data, including group/default inheritance:
    'base_url', 'http_headers', 'tls_verify', and 'http_timeout' (seconds, defaults to 10).
    """
    try:
        with _get_nornir(filter_criteria) as nr, ExitStack() as stack:
            if not nr.inventory.hosts:
                return "No hosts matched the filter criteria."

            # Register cleanup immediately, including for partial initialization.
            # Clients are shared across threads, one per TLS verification setting.
            verify_values = {_get_tls_verify(host) for host in nr.inventory.hosts.values()}
            clients = {}
            for verify in verify_values:
                client = httpx.Client(verify=verify, timeout=HTTP_TIMEOUT)
                stack.callback(client.close)
                clients[verify] = client
            agg_result = nr.run(task=custom_http_task, clients=clients, method=method, path=path, json_data=json_data)
            return _format_agg_result(agg_result)
    except Exception as e:
        logger.exception(f"Error executing HTTP request (method={method}, path={path}, filter={filter_criteria})")
        return f"Error executing HTTP request: {str(e)}"


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
def run_serial_command(command: str, filter_criteria: dict[str, Any] | None = None) -> str:
    """
    Run a CLI command on devices via direct Serial connection (using Netmiko).

    Note: Physical serial ports generally cannot be used concurrently by multiple threads,
    so hosts are processed sequentially (one at a time).

    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Dictionary to filter target hosts.
    """
    try:
        with _get_nornir(filter_criteria, num_workers=1) as nr:
            if not nr.inventory.hosts:
                return "No hosts matched the filter criteria."
            agg_result = nr.run(task=custom_serial_task, command=command)
            return _format_agg_result(agg_result)
    except Exception as e:
        logger.exception(f"Error executing serial command (command={command!r}, filter={filter_criteria})")
        return f"Error executing serial command: {str(e)}"


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
def generate_config(template_string: str, filter_criteria: dict[str, Any] | None = None) -> str:
    """
    Generate configuration from a Jinja2 template string for each target host.
    Does NOT deploy the configuration; useful for dry-runs and auditing.

    Args:
        template_string: Jinja2 template string. Available variables: {{ host.name }}, {{ host.hostname }},
                         {{ host.platform }}, {{ host.groups }}, {{ host.data['key'] }}. Other host
                         attributes (e.g. credentials) are not exposed.
                         host.data includes inherited group/default values.
        filter_criteria: Dictionary to filter target hosts.
    """
    try:
        with _get_nornir(filter_criteria) as nr:
            if not nr.inventory.hosts:
                return "No hosts matched the filter criteria."

            # Compile once for all hosts, inside the connection cleanup scope.
            template = _JINJA_ENV.from_string(template_string)
            agg_result = nr.run(task=custom_template_task, template=template)
            return _format_agg_result(agg_result)
    except Exception as e:
        logger.exception(f"Error generating config (filter={filter_criteria})")
        return f"Error generating config: {str(e)}"


@mcp.tool()
def run_netmiko_config(commands: list[str], filter_criteria: dict[str, Any] | None = None) -> str:
    """
    WRITE OPERATION: modifies device configuration.
    Deploy configuration commands concurrently to multiple hosts via Netmiko.
    Confirm with the user before calling this tool.

    Args:
        commands: List of configuration commands to execute.
        filter_criteria: Dictionary to filter target hosts.
    """
    try:
        with _get_nornir(filter_criteria) as nr:
            if not nr.inventory.hosts:
                return "No hosts matched the filter criteria."
            agg_result = nr.run(task=netmiko_send_config, config_commands=commands)
            return _format_agg_result(agg_result)
    except Exception as e:
        logger.exception(f"Error executing config commands (filter={filter_criteria})")
        return f"Error executing config commands: {str(e)}"


if __name__ == "__main__":
    mcp.run()
