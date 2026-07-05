import json
import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from nornir import InitNornir
from nornir.core.filter import F
from nornir_netmiko.tasks import netmiko_send_command, netmiko_send_config
from nornir.core.task import Result, Task
import httpx
from netmiko import ConnectHandler
from jinja2 import Template

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nornir-mcp-server")

CONFIG_FILE = "config.yaml"

mcp = FastMCP("nornir server", dependencies=["nornir", "nornir-netmiko"])

def _get_nornir(filter_criteria: Optional[dict[str, Any]] = None):
    """Initialize Nornir and apply filtering if provided."""
    nr = InitNornir(config_file=CONFIG_FILE)
    if filter_criteria:
        # F(**filter_criteria) allows matching by attributes like site, role, name, etc.
        nr = nr.filter(F(**filter_criteria))
    return nr

@mcp.tool()
def get_inventory(filter_criteria: Optional[dict[str, Any]] = None) -> str:
    """
    Get inventory hosts and their metadata, optionally filtered.
    
    Args:
        filter_criteria: Dictionary of key-value pairs to filter hosts (e.g. {"name": "router1"} or {"role": "core", "site": "tokyo"}).
                         Matches host attributes or data fields.
    """
    try:
        nr = _get_nornir(filter_criteria)
        hosts_info = {}
        for name, host in nr.inventory.hosts.items():
            hosts_info[name] = {
                "hostname": host.hostname,
                "platform": host.platform,
                "groups": [g.name for g in host.groups],
                "data": dict(host.data)
            }
        return json.dumps(hosts_info, indent=2)
    except Exception as e:
        logger.exception(f"Error loading inventory (filter={filter_criteria})")
        return f"Error loading inventory: {str(e)}"

@mcp.tool()
def run_netmiko_command(command: str, filter_criteria: Optional[dict[str, Any]] = None, use_textfsm: bool = False) -> str:
    """
    Run a show command concurrently on multiple hosts via Netmiko.
    
    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Dictionary of key-value pairs to filter target hosts. If empty, runs on ALL hosts!
        use_textfsm: If True, attempts to parse output into structured data using ntc-templates.
    """
    try:
        nr = _get_nornir(filter_criteria)
        if not nr.inventory.hosts:
            return "No hosts matched the filter criteria."
        
        # Run the task
        agg_result = nr.run(
            task=netmiko_send_command,
            command_string=command,
            use_textfsm=use_textfsm
        )
        
        # Parse the output
        output_data = {}
        for host_name, multi_result in agg_result.items():
            result = multi_result[0]
            if result.failed:
                output_data[host_name] = {"error": str(result.exception)}
            else:
                output_data[host_name] = {"output": result.result}
                
        return json.dumps(output_data, indent=2)
    except Exception as e:
        logger.exception(f"Error executing command (command={command!r}, filter={filter_criteria})")
        return f"Error executing command: {str(e)}"

def custom_http_task(task: Task, method: str, path: str, json_data: Optional[dict] = None) -> Result:
    """Custom Nornir task to execute HTTP requests."""
    # Build base URL from host attributes
    host = task.host
    base_url = host.data.get("base_url", f"https://{host.hostname}")
    headers = host.data.get("http_headers", {})
    verify = host.data.get("tls_verify", True)
    if not verify:
        logger.warning(f"TLS verification disabled for host {host.name}")

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    with httpx.Client(verify=verify) as client:
        response = client.request(
            method=method,
            url=url,
            headers=headers,
            json=json_data,
            timeout=10.0
        )
        response.raise_for_status()
        
        try:
            result = response.json()
        except ValueError:
            result = response.text
            
    return Result(host=task.host, result=result)

@mcp.tool()
def run_http_request(method: str, path: str, filter_criteria: Optional[dict[str, Any]] = None, json_data: Optional[dict] = None) -> str:
    """
    Run an HTTP API request (REST) concurrently on multiple hosts.
    
    Args:
        method: HTTP method (e.g., 'GET', 'POST', 'PUT').
        path: API path to request (e.g., '/api/v1/system').
        filter_criteria: Dictionary to filter target hosts.
        json_data: Optional JSON payload for POST/PUT requests.
    """
    try:
        nr = _get_nornir(filter_criteria)
        if not nr.inventory.hosts:
            return "No hosts matched the filter criteria."
        
        agg_result = nr.run(
            task=custom_http_task,
            method=method,
            path=path,
            json_data=json_data
        )
        
        output_data = {}
        for host_name, multi_result in agg_result.items():
            result = multi_result[0]
            if result.failed:
                output_data[host_name] = {"error": str(result.exception)}
            else:
                output_data[host_name] = {"output": result.result}
                
        return json.dumps(output_data, indent=2)
    except Exception as e:
        logger.exception(f"Error executing HTTP request (method={method}, path={path}, filter={filter_criteria})")
        return f"Error executing HTTP request: {str(e)}"

def custom_serial_task(task: Task, command: str) -> Result:
    """Custom Nornir task to execute commands via Serial port using Netmiko."""
    host = task.host
    serial_settings = host.data.get("serial_settings")
    if not serial_settings:
        raise ValueError("No 'serial_settings' found in host data.")
    
    # Merge platform if not in serial_settings
    device_type = serial_settings.get("device_type", host.platform or "autodetect")
    
    connection_params = {
        "device_type": device_type,
        "serial_settings": serial_settings
    }
    
    # Instantiate ConnectHandler directly for serial
    with ConnectHandler(**connection_params) as net_connect:
        output = net_connect.send_command(command)
        
    return Result(host=task.host, result=output)

@mcp.tool()
def run_serial_command(command: str, filter_criteria: Optional[dict[str, Any]] = None) -> str:
    """
    Run a CLI command on devices via direct Serial connection (using Netmiko).
    
    Note: Physical serial ports generally cannot be used concurrently by multiple threads.
    Ensure your filter_criteria targets a single host, or multiple hosts on different serial ports.
    
    Args:
        command: The CLI command to execute (e.g., 'show version').
        filter_criteria: Dictionary to filter target hosts.
    """
    try:
        nr = _get_nornir(filter_criteria)
        if not nr.inventory.hosts:
            return "No hosts matched the filter criteria."
        
        agg_result = nr.run(
            task=custom_serial_task,
            command=command
        )
        
        output_data = {}
        for host_name, multi_result in agg_result.items():
            result = multi_result[0]
            if result.failed:
                output_data[host_name] = {"error": str(result.exception)}
            else:
                output_data[host_name] = {"output": result.result}
                
        return json.dumps(output_data, indent=2)
    except Exception as e:
        logger.exception(f"Error executing serial command (command={command!r}, filter={filter_criteria})")
        return f"Error executing serial command: {str(e)}"

def custom_template_task(task: Task, template_string: str) -> Result:
    """Custom Nornir task to render Jinja2 templates using host inventory data."""
    template = Template(template_string)
    rendered = template.render(host=task.host)
    return Result(host=task.host, result=rendered)

@mcp.tool()
def generate_config(template_string: str, filter_criteria: Optional[dict[str, Any]] = None) -> str:
    """
    Generate configuration from a Jinja2 template string for each target host.
    Does NOT deploy the configuration; useful for dry-runs and auditing.
    
    Args:
        template_string: Jinja2 template string. Variables like {{ host.name }}, {{ host.hostname }}, {{ host.data['key'] }} can be used.
        filter_criteria: Dictionary to filter target hosts.
    """
    try:
        nr = _get_nornir(filter_criteria)
        if not nr.inventory.hosts:
            return "No hosts matched the filter criteria."
            
        agg_result = nr.run(
            task=custom_template_task,
            template_string=template_string
        )
        
        output_data = {}
        for host_name, multi_result in agg_result.items():
            result = multi_result[0]
            if result.failed:
                output_data[host_name] = {"error": str(result.exception)}
            else:
                output_data[host_name] = {"output": result.result}
                
        return json.dumps(output_data, indent=2)
    except Exception as e:
        return f"Error generating config: {str(e)}"

@mcp.tool()
def run_netmiko_config(commands: list[str], filter_criteria: Optional[dict[str, Any]] = None) -> str:
    """
    Deploy configuration commands concurrently to multiple hosts via Netmiko.
    
    Args:
        commands: List of configuration commands to execute.
        filter_criteria: Dictionary to filter target hosts.
    """
    try:
        nr = _get_nornir(filter_criteria)
        if not nr.inventory.hosts:
            return "No hosts matched the filter criteria."
            
        agg_result = nr.run(
            task=netmiko_send_config,
            config_commands=commands
        )
        
        output_data = {}
        for host_name, multi_result in agg_result.items():
            result = multi_result[0]
            if result.failed:
                output_data[host_name] = {"error": str(result.exception)}
            else:
                output_data[host_name] = {"output": result.result}
                
        return json.dumps(output_data, indent=2)
    except Exception as e:
        return f"Error executing config commands: {str(e)}"

if __name__ == "__main__":
    mcp.run()
