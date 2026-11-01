"""The tools Claude can call: their JSON schemas, safety lists, and confirmation text."""

from .common import run_tool_func
from .diagnostics import (
    check_internet_connection, get_basic_stats, get_battery_status, get_cpu_temperature_and_throttling,
    get_disk_breakdown, get_disk_health, get_drive_space, get_security_status, get_startup_programs,
    get_system_info, get_top_processes, list_installed_programs,
)
from .history import get_change_history, load_history, undo_change
from .network import flush_dns, get_network_status, reset_network
from .scan import run_full_scan
from .security import BLOAT_APPS, check_unwanted_software, list_browser_extensions, uninstall_store_app
from .settings import (
    OPEN_PAGES, disable_startup_program, get_performance_settings, get_power_plan, kill_process, open_page,
    repair_system_files, set_game_mode, set_game_recording, set_power_plan,
)
from .storage import (
    CLEANUP_CATEGORIES, clean_up, find_duplicate_files, find_largest_files, get_cleanup_report,
)
from .updates import check_drivers, check_windows_updates


def _no_args(name, description):
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": {}}}


def _one_arg(name, description, arg, arg_description):
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {arg: {"type": "string", "description": arg_description}},
            "required": [arg],
        },
    }


TOOLS = [
    _no_args("run_full_scan", "Run every diagnostic check at once (system & uptime, usage, free space, disk health, temperature/throttling, resource-heavy programs, startup programs, power plan, security). Use this when the user asks to scan, check, or diagnose their PC, or asks why it's slow."),
    _no_args("get_basic_stats", "Get current CPU, RAM, and system disk usage percentages."),
    _no_args("get_top_processes", "Get the programs using the most memory and CPU right now, grouped by program name."),
    _no_args("get_drive_space", "Get free space on every drive, flagging drives under 15% free."),
    _no_args("get_disk_health", "Get each physical disk's type (SSD or HDD), health status, and SMART details where available. A failing disk is a common cause of slowness."),
    _no_args("get_cpu_temperature_and_throttling", "Check CPU temperature (if the hardware reports it) and whether the CPU is running below its rated speed, which suggests overheating."),
    _no_args("get_security_status", "Check that Windows Defender antivirus, real-time protection, and the firewall are on, and that virus definitions are up to date."),
    {
        "name": "find_largest_files",
        "description": "Find the largest files in a folder (default: the user's home folder, including subfolders) so the user can decide what to delete. Read-only; takes up to a minute.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Folder to search, e.g. 'C:\\Users\\Name\\Downloads' or '~/Videos'. Leave empty for the whole home folder."},
                "count": {"type": "integer", "description": "How many files to list (default 15, max 50)."},
            },
        },
    },
    _no_args("get_startup_programs", "Get the list of programs configured to launch automatically when Windows starts, labelled by whether they are current-user (can be disabled) or all-users."),
    _no_args("get_disk_breakdown", "Check how much disk space common user folders (Downloads, Documents, Desktop, Pictures, Videos, Music) are using."),
    _no_args("get_power_plan", "Get the currently active Windows power plan."),
    _no_args("get_battery_status", "Get the current battery percentage and whether the device is plugged in. Only relevant for laptops."),
    _no_args("check_internet_connection", "Test whether the PC currently has a working internet connection, and roughly how fast it responds."),
    _no_args("get_system_info", "Get the Windows version, CPU cores, total RAM, and how long the PC has been running since its last restart."),
    _no_args("list_installed_programs", "List installed programs, largest first, with install dates. Useful for finding unused software worth uninstalling."),
    _no_args("get_cleanup_report", "Measure how much space each safe cleanup category uses (temp files, browser caches, shader caches, crash reports, Windows Update downloads, Recycle Bin) without deleting anything. Always call this before clean_up so the user can choose."),
    {
        "name": "clean_up",
        "description": "Delete the contents of the chosen safe cleanup categories. Only call after showing get_cleanup_report and the user choosing which categories to clean; explain each category's side effects (e.g. Recycle Bin is permanent, shader cache makes games load slower once). The user will be asked to confirm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(CLEANUP_CATEGORIES)},
                    "description": "Which categories to clean.",
                }
            },
            "required": ["categories"],
        },
    },
    {
        "name": "find_duplicate_files",
        "description": "Find identical files over 1 MB (compared by content, not just name) in the user's Desktop, Documents, Downloads, Pictures, Videos and Music, or in a given folder. Read-only; the user decides what to delete.",
        "input_schema": {
            "type": "object",
            "properties": {"folder": {"type": "string", "description": "Optional folder to search instead of the default user folders."}},
        },
    },
    _one_arg("disable_startup_program", "Remove a current-user program from the Windows startup list. The user will be asked to confirm.",
             "program_name", "The exact name of the startup entry, as shown by get_startup_programs."),
    _one_arg("kill_process", "Close a running program by its process name. Unsaved work in that program may be lost. The user will be asked to confirm.",
             "process_name", "The exact process name to close, e.g. 'Discord.exe', as shown by get_top_processes."),
    _one_arg("set_power_plan", "Switch the active Windows power plan. The user will be asked to confirm.",
             "plan_name", "One of: 'Balanced', 'Power Saver', 'High Performance'."),
    _no_args("repair_system_files", "Repair corrupted Windows system files by running DISM /RestoreHealth then sfc /scannow in a separate administrator window (10-30 minutes). Suggest this for crashes, errors, or strange Windows behaviour, not general slowness. The user will be asked to confirm."),
    # Updates & drivers
    _no_args("check_windows_updates", "Check for pending Windows updates (security fixes, drivers, optional updates) and when the last update was installed. Takes a few seconds and needs internet."),
    _no_args("check_drivers", "Check the graphics driver version and age, devices with problems in Device Manager, and where to get chipset drivers. Gives official manufacturer download pages."),
    # Unwanted software & security
    _no_args("check_unwanted_software", "Look for known unwanted programs (scareware 'optimizers', driver updaters, adware, trial antivirus), removable preinstalled apps, startup entries that are unsigned/in temp folders/run scripts, and programs running from Temp or Downloads."),
    _no_args("list_browser_extensions", "List installed extensions in Edge, Chrome, Brave and Firefox, so the user can remove ones they don't recognise."),
    _one_arg("uninstall_store_app", "Uninstall a preinstalled Microsoft Store app reported by check_unwanted_software (current user only; it can be reinstalled from the Store). The user will be asked to confirm.",
             "package_name", "The exact package name shown by check_unwanted_software, e.g. 'Microsoft.BingNews'."),
    # Network
    _no_args("get_network_status", "Diagnose the internet connection: adapter type and speed, Wi-Fi signal, ping/packet loss to the router and the internet, and DNS. Takes about 10 seconds. Use for slow, laggy, or dropping internet."),
    _no_args("flush_dns", "Clear the DNS cache. A quick, harmless fix for websites not loading or loading the wrong page. The user will be asked to confirm."),
    _no_args("reset_network", "Reset Windows' network stack (Winsock and TCP/IP) in an administrator window; needs a restart afterwards. A last resort for internet problems after other checks. The user will be asked to confirm."),
    # Performance & gaming settings
    _no_args("get_performance_settings", "Check Game Mode, Xbox Game Bar background recording, GPU scheduling, visual effects, transparency, page file (virtual memory), and the power plan."),
    {
        "name": "set_game_mode",
        "description": "Turn Windows Game Mode on or off. The user will be asked to confirm.",
        "input_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]},
    },
    {
        "name": "set_game_recording",
        "description": "Turn Xbox Game Bar game recording (including background recording) on or off. Turning it off can improve FPS, but the user can no longer capture clips with Win+Alt+G. The user will be asked to confirm.",
        "input_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]},
    },
    {
        "name": "get_change_history",
        "description": "List the changes PC Assistant has made on this PC (newest first), with IDs and whether each can be undone. Use when the user asks what was changed, or wants to undo something.",
        "input_schema": {"type": "object", "properties": {"count": {"type": "integer", "description": "How many to show (default 15)."}}},
    },
    {
        "name": "undo_change",
        "description": "Undo a change from get_change_history by its ID (e.g. re-enable a startup program, switch back to the previous power plan, restore game settings). The user will be asked to confirm.",
        "input_schema": {"type": "object", "properties": {"change_id": {"type": "integer"}}, "required": ["change_id"]},
    },
    _one_arg("open_page", "Open a Windows settings page or an official driver download page for the user. Use this for things the user should do themselves: installing Windows updates, uninstalling programs, visual effects, page file, GPU settings, virus scans, or downloading drivers.",
             "page", "One of: " + ", ".join(OPEN_PAGES) + "."),
]


TOOL_FUNCTIONS = {
    "run_full_scan": run_full_scan,
    "get_drive_space": get_drive_space,
    "get_disk_health": get_disk_health,
    "get_cpu_temperature_and_throttling": get_cpu_temperature_and_throttling,
    "get_security_status": get_security_status,
    "find_largest_files": find_largest_files,
    "repair_system_files": repair_system_files,
    "get_basic_stats": get_basic_stats,
    "get_top_processes": get_top_processes,
    "get_startup_programs": get_startup_programs,
    "get_disk_breakdown": get_disk_breakdown,
    "get_power_plan": get_power_plan,
    "get_battery_status": get_battery_status,
    "check_internet_connection": check_internet_connection,
    "get_system_info": get_system_info,
    "list_installed_programs": list_installed_programs,
    "get_cleanup_report": get_cleanup_report,
    "clean_up": clean_up,
    "find_duplicate_files": find_duplicate_files,
    "disable_startup_program": disable_startup_program,
    "kill_process": kill_process,
    "set_power_plan": set_power_plan,
    "check_windows_updates": check_windows_updates,
    "check_drivers": check_drivers,
    "check_unwanted_software": check_unwanted_software,
    "list_browser_extensions": list_browser_extensions,
    "uninstall_store_app": uninstall_store_app,
    "get_network_status": get_network_status,
    "flush_dns": flush_dns,
    "reset_network": reset_network,
    "get_performance_settings": get_performance_settings,
    "set_game_mode": set_game_mode,
    "set_game_recording": set_game_recording,
    "open_page": open_page,
    "get_change_history": get_change_history,
    "undo_change": undo_change,
}


# Tools that change something on the PC and need the user's confirmation first
ACTIONS_REQUIRING_CONFIRMATION = {
    "clean_up", "disable_startup_program", "kill_process", "set_power_plan", "repair_system_files",
    "uninstall_store_app", "flush_dns", "reset_network", "set_game_mode", "set_game_recording",
    "undo_change",
}


# Actions that change Windows settings, where a System Restore point is actually useful.
# (A restore point can't bring back deleted temp files or a closed program.)
ACTIONS_NEEDING_RESTORE_POINT = {"disable_startup_program", "set_power_plan"}


def describe_action(name, tool_input):
    """Plain-English description of an action, for the confirmation prompt."""
    tool_input = tool_input or {}
    if name == "clean_up":
        cats = tool_input.get("categories") or []
        if isinstance(cats, str):
            cats = [cats]
        lines = ["Clean up:"]
        for c in cats:
            cat = CLEANUP_CATEGORIES.get(c)
            lines.append(f"  • {cat['label']}\n    {cat['note']}" if cat else f"  • {c}")
        return "\n".join(lines)
    descriptions = {
        "disable_startup_program": "Stop '{program_name}' from launching when Windows starts.",
        "kill_process": "Close all running copies of '{process_name}'. Unsaved work in it may be lost.",
        "set_power_plan": "Switch the power plan to '{plan_name}'.",
        "repair_system_files": "Run Windows system file repair (DISM + sfc) in an administrator window. Takes 10-30 minutes.",
        "uninstall_store_app": "Uninstall '{app}'. It can be reinstalled from the Microsoft Store any time.",
        "flush_dns": "Clear the DNS cache (harmless; fixes some websites not loading). Windows may ask for administrator approval.",
        "reset_network": "Reset Windows network settings in an administrator window. You'll need to restart your PC afterwards. Saved Wi-Fi passwords are kept.",
        "set_game_mode": "Turn Game Mode {state}.",
        "set_game_recording": "Turn Xbox Game Bar game recording {state}.",
    }
    if name == "undo_change":
        change = next((c for c in load_history() if c["id"] == tool_input.get("change_id")), None)
        return f"Undo: {change['description']}" if change else f"Undo change #{tool_input.get('change_id')}"
    if "package_name" in tool_input:
        tool_input = {**tool_input, "app": BLOAT_APPS.get(tool_input["package_name"], tool_input["package_name"])}
    if "enabled" in tool_input:
        tool_input = {**tool_input, "state": "ON" if tool_input["enabled"] else "OFF"}
    try:
        return descriptions[name].format(**tool_input)
    except (KeyError, IndexError):
        details = "".join(f"\n  {k}: {v}" for k, v in tool_input.items())
        return f"Run {name}{details}"


def run_tool(name, tool_input):
    """Run a tool by name, always returning a string (errors are reported, not raised)."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Unknown tool '{name}'."
    return run_tool_func(func, **(tool_input or {}))
