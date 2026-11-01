"""The one-click full scan, which runs every diagnostic check in parallel."""

from concurrent.futures import ThreadPoolExecutor

from .common import run_tool_func
from .diagnostics import (
    get_basic_stats, get_cpu_temperature_and_throttling, get_disk_health, get_drive_space,
    get_security_status, get_startup_programs, get_system_info, get_top_processes,
)
from .security import check_unwanted_software
from .settings import get_performance_settings
from .storage import get_cleanup_report
from .updates import check_drivers, check_windows_updates


def run_full_scan():
    checks = [
        ("System & uptime", get_system_info),
        ("CPU, RAM & disk usage", get_basic_stats),
        ("Free disk space", get_drive_space),
        ("Space that could be cleaned up", get_cleanup_report),
        ("Disk health", get_disk_health),
        ("CPU temperature & throttling", get_cpu_temperature_and_throttling),
        ("Programs using the most resources", get_top_processes),
        ("Startup programs", get_startup_programs),
        ("Performance & gaming settings", get_performance_settings),
        ("Security", get_security_status),
        ("Unwanted software & suspicious startup items", check_unwanted_software),
        ("Windows updates", check_windows_updates),
        ("Drivers & devices", check_drivers),
    ]
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = [(title, pool.submit(run_tool_func, func)) for title, func in checks]
        sections = [f"=== {title} ===\n{future.result()}" for title, future in futures]
    return "\n\n".join(sections)
