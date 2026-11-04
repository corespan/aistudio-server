"""Tool registry: the list of tools this server exposes."""

from __future__ import annotations

from app.tools.actions import delete_run_results, start_jupyter, start_workload, stop_jupyter
from app.tools.aistudio import (
    check_service_health,
    get_benchmark,
    get_task_logs,
    get_task_status,
    list_benchmarks,
    model_config,
    platform_catalog,
    queue_status,
    summary,
    throughput_compare,
    workload_history,
)
from app.tools.browser_probes import frontend_check
from app.tools.docker_probes import (
    container_diff,
    docker_logs,
    inspect_container,
    list_containers,
    timeline,
)
from app.tools.fs_probes import (
    git_log,
    git_show,
    grep_logs,
    list_dir,
    read_file,
)
from app.tools.net_probes import (
    check_clock_skew,
    check_tls_cert,
    list_listening_ports,
    network_check,
    tcpdump_capture,
)
from app.tools.ssh_probes import (
    check_node_docker,
    check_ssh_connectivity,
    node_docker_logs,
    node_exec,
)

ALL_TOOLS: list = [
    # containers
    timeline,
    list_containers,
    docker_logs,
    inspect_container,
    container_diff,
    # codebase / filesystem
    read_file,
    list_dir,
    grep_logs,
    git_log,
    git_show,
    # network
    network_check,
    list_listening_ports,
    tcpdump_capture,
    check_tls_cert,
    check_clock_skew,
    # frontend
    frontend_check,
    # aistudio-specific
    workload_history,
    throughput_compare,
    queue_status,
    check_service_health,
    platform_catalog,
    list_benchmarks,
    get_benchmark,
    summary,
    model_config,
    get_task_status,
    get_task_logs,
    check_ssh_connectivity,
    check_node_docker,
    node_docker_logs,
    node_exec,
    # actions (write tools)
    start_workload,
    start_jupyter,
    stop_jupyter,
    delete_run_results,
]


# Fail at import if a tool has no policy row.
from app.policy import TOOL_POLICY  # noqa: E402

_unlisted = {t.name for t in ALL_TOOLS} - set(TOOL_POLICY)
if _unlisted:
    raise RuntimeError(f"registry: add these tools to app/policy.py: {sorted(_unlisted)}")
