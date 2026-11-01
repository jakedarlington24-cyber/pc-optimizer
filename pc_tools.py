"""Shared PC tools used by both the command-line (ask_claude.py) and GUI (ask_claude_gui.py) assistants."""

import heapq
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import winreg
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import psutil

MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096
SYSTEM_PROMPT = """You are a friendly PC optimization assistant for a non-expert, acting like a good, honest PC technician.

How to work:
- Diagnose before touching anything. Use the tools to check real data before answering questions about the user's PC. Never guess at numbers.
- When asked to scan or check the PC, use run_full_scan, then explain what you found in plain English and give a short list of recommended fixes ranked by impact (biggest improvement first). Say clearly when something is fine, so the user knows what NOT to worry about.
- Offer fixes one at a time. Before calling any tool that changes something, explain what it will do and why; the user will be asked to confirm before it actually runs.
- Every change is logged. Settings changes (startup programs, power plan, game settings) can be undone with undo_change; mention this when it would reassure the user. Cleanups and closed programs can't be undone, so say so before running them.
- Be honest when the real fix is hardware, e.g. a hard drive (HDD) instead of an SSD, too little RAM for how it's used, or a failing disk. That advice is often the most valuable thing you can give.
- If a disk reports anything other than Healthy, treat it as urgent: tell the user to back up their important files right away.

What to avoid:
- Never recommend registry cleaners, "RAM boosters", or "driver updater" programs; they are snake oil and can break things.
- Don't suggest disabling Windows services; disabling the wrong one can break Windows.
- For GPU/chipset drivers, point the user to the manufacturer's website (NVIDIA, AMD, Intel, or the PC/motherboard maker) instead of third-party tools.
- Only delete things through the provided tools, which stick to well-defined safe folders. For large files, show them and let the user decide; never suggest deleting files in Windows or Program Files.
- Don't call something malware unless you're sure. A flagged startup entry or unknown program is "worth checking": explain why, and suggest a Defender scan (open_page 'defender') or removing it if the user doesn't recognise it.
- For things the user should do themselves (installing Windows updates, uninstalling desktop programs, visual effects, page file, GPU scheduling, downloading drivers), use open_page to take them straight to the right place, and tell them exactly what to click.
- For internet problems, diagnose with get_network_status first. Try the gentle fix (flush_dns) before reset_network. If the router ping is bad, the problem is the Wi-Fi/router, not the PC: suggest restarting the router, moving closer, or using a cable.
- For gaming, Game Mode should normally be ON and background recording OFF. Leave the page file on automatic unless it's disabled.

Keep answers concise and friendly. Your replies are shown in a plain-text window, so don't use Markdown: no **bold**, # headings, or tables. Use short lines, simple "-" bullets, and numbered lists ("1.") instead."""

SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:") + "\\"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---- Safety net ----

def create_restore_point():
    if not _is_admin():
        return False, (
            "Skipped the safety restore point (Windows only allows this when the app runs as administrator). "
            "This change is easy to undo anyway."
        )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                'Checkpoint-Computer -Description "PC Assistant Action" -RestorePointType "MODIFY_SETTINGS"',
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode == 0:
            return True, "Restore point created."
        # Common causes: not running as administrator, System Restore turned off,
        # or Windows' limit of one restore point per 24 hours.
        return False, result.stderr.strip() or (
            "Could not create a restore point (this needs administrator rights, "
            "and Windows may limit it to once per 24 hours)."
        )
    except Exception as e:
        return False, f"Could not create a restore point: {e}"


# ---- Change history (so actions can be reviewed and undone) ----

HISTORY_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")), "PCAssistant", "history.json"
)


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp, HISTORY_FILE)


def _record_change(description, undo=None):
    """Log an action. `undo` is data for undo_change(), or None if the action can't be undone."""
    history = load_history()
    history.append({
        "id": (max((c["id"] for c in history), default=0) + 1),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": description,
        "undo": undo,
        "undone": False,
    })
    _save_history(history[-200:])


def _format_size(num_bytes):
    mb = num_bytes / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def _folder_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _powershell_json(script, timeout=60):
    """Run a PowerShell snippet that outputs ConvertTo-Json, returning parsed data (always a list)."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )
    out = result.stdout.strip()
    if not out:
        raise RuntimeError(result.stderr.strip() or "no output")
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def _is_admin():
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---- Read-only tools ----

def get_basic_stats():
    return (
        f"CPU usage: {psutil.cpu_percent(interval=1)}%\n"
        f"RAM usage: {psutil.virtual_memory().percent}%\n"
        f"Disk usage ({SYSTEM_DRIVE}): {psutil.disk_usage(SYSTEM_DRIVE).percent}%"
    )


def get_top_processes():
    # Sample CPU over 1 second, and group processes by name so a program that runs
    # as many processes (browsers, Discord, VS Code) shows up as one total.
    procs = list(psutil.process_iter(["name"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(1)

    cpu_count = psutil.cpu_count() or 1
    total_ram = psutil.virtual_memory().total
    groups = defaultdict(lambda: {"cpu": 0.0, "mem": 0, "count": 0})
    for p in procs:
        name = p.info["name"]
        if not name or name in ("System Idle Process", "Idle"):
            continue
        try:
            g = groups[name]
            g["cpu"] += p.cpu_percent(None) / cpu_count
            g["mem"] += p.memory_info().rss
            g["count"] += 1
        except psutil.Error:
            pass

    def fmt(name, g):
        copies = f" ({g['count']} processes)" if g["count"] > 1 else ""
        return f"  {name}{copies}: {g['cpu']:.1f}% CPU, {_format_size(g['mem'])} RAM ({g['mem'] / total_ram * 100:.1f}%)"

    by_mem = sorted(groups.items(), key=lambda kv: kv[1]["mem"], reverse=True)[:8]
    by_cpu = sorted(groups.items(), key=lambda kv: kv[1]["cpu"], reverse=True)[:5]
    return (
        "Top programs by memory:\n" + "\n".join(fmt(n, g) for n, g in by_mem)
        + "\n\nTop programs by CPU (sampled over 1 second):\n" + "\n".join(fmt(n, g) for n, g in by_cpu)
    )


def get_startup_programs():
    locations = [
        (winreg.HKEY_CURRENT_USER, "current user, can be disabled"),
        (winreg.HKEY_LOCAL_MACHINE, "all users, needs admin to change"),
    ]
    results = []
    for hive, label in locations:
        try:
            with winreg.OpenKey(hive, RUN_KEY) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                        results.append(f"  {name} [{label}]: {value}")
                        i += 1
                    except OSError:
                        break
        except FileNotFoundError:
            continue
    return "\n".join(results) if results else "No startup programs found."


def get_disk_breakdown():
    home = os.path.expanduser("~")
    results = []
    for folder in ["Downloads", "Documents", "Desktop", "Pictures", "Videos", "Music"]:
        path = os.path.join(home, folder)
        if os.path.isdir(path):
            results.append(f"  {folder}: {_format_size(_folder_size(path))}")
    return "\n".join(results) if results else "Could not read user folders."


def get_power_plan():
    try:
        result = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() or "Could not determine the current power plan."
    except Exception as e:
        return f"Error checking power plan: {e}"


def get_battery_status():
    battery = psutil.sensors_battery()
    if battery is None:
        return "No battery detected. This is likely a desktop PC."
    plugged = "plugged in" if battery.power_plugged else "on battery power"
    return f"Battery: {battery.percent}%, currently {plugged}."


def check_internet_connection():
    for host, port in [("8.8.8.8", 53), ("1.1.1.1", 53)]:
        start = time.time()
        try:
            with socket.create_connection((host, port), timeout=3):
                elapsed_ms = (time.time() - start) * 1000
            return f"Internet connection is working. Reached {host} in {elapsed_ms:.0f} ms."
        except OSError:
            continue
    return "No internet connection detected, or it's being blocked."


def _pending_restart_reasons():
    checks = [
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired", "Windows Update"),
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending", "a Windows component install"),
    ]
    reasons = []
    for path, reason in checks:
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path))
            reasons.append(reason)
        except OSError:
            pass
    return reasons


def get_system_info():
    uptime_seconds = time.time() - psutil.boot_time()
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    uptime = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
    if days >= 7:
        uptime += " (WARNING: hasn't restarted in over a week; a restart often fixes slowness)"

    pending = _pending_restart_reasons()
    restart_line = f"YES, needed by {', '.join(pending)}" if pending else "no"

    try:
        cpu_name = winreg.QueryValueEx(
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"),
            "ProcessorNameString",
        )[0].strip()
    except OSError:
        cpu_name = platform.processor()

    return (
        f"OS: {platform.system()} {platform.release()} (version {platform.version()})\n"
        f"CPU: {cpu_name} ({psutil.cpu_count(logical=False)} cores, {psutil.cpu_count()} threads)\n"
        f"RAM: {psutil.virtual_memory().total / (1024 ** 3):.1f} GB\n"
        f"Uptime: {uptime} since last restart\n"
        f"Restart pending: {restart_line}"
    )


def get_drive_space():
    lines = []
    for part in psutil.disk_partitions():
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        free_pct = 100 - usage.percent
        flag = ""
        if free_pct < 10:
            flag = " (CRITICAL: under 10% free, this slows Windows down)"
        elif free_pct < 15:
            flag = " (LOW: under 15% free)"
        lines.append(
            f"  {part.mountpoint} {_format_size(usage.free)} free of {_format_size(usage.total)} "
            f"({free_pct:.0f}% free){flag}"
        )
    return "\n".join(lines) if lines else "Could not read any drives."


def get_disk_health():
    disks = _powershell_json(
        "Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, BusType, HealthStatus, "
        "@{n='OperationalStatus';e={$_.OperationalStatus -join ', '}}, Size | ConvertTo-Json"
    )
    counters = {}
    if _is_admin():
        try:
            for c in _powershell_json(
                "Get-PhysicalDisk | Get-StorageReliabilityCounter | Select-Object DeviceId, Temperature, "
                "Wear, ReadErrorsTotal, WriteErrorsTotal, PowerOnHours | ConvertTo-Json"
            ):
                counters[str(c.get("DeviceId"))] = c
        except Exception:
            pass

    lines = []
    for d in disks:
        media = d.get("MediaType") or "Unknown"
        if media == "HDD":
            media = "HDD (spinning hard drive - much slower than an SSD)"
        line = (
            f"  {d.get('FriendlyName')}: {media}, {d.get('BusType')}, {_format_size(d.get('Size') or 0)}, "
            f"health: {d.get('HealthStatus')}, status: {d.get('OperationalStatus')}"
        )
        c = counters.get(str(d.get("DeviceId")))
        if c:
            details = []
            if c.get("Temperature"):
                details.append(f"temperature {c['Temperature']}°C")
            if c.get("Wear") is not None:
                details.append(f"wear {c['Wear']}% used")
            if c.get("PowerOnHours"):
                details.append(f"{c['PowerOnHours']} hours powered on")
            errors = (c.get("ReadErrorsTotal") or 0) + (c.get("WriteErrorsTotal") or 0)
            details.append(f"{errors} read/write errors")
            line += "\n    " + ", ".join(details)
        lines.append(line)
    if not counters:
        lines.append("  (Detailed SMART data like wear and error counts needs the app to run as administrator.)")
    return "\n".join(lines)


def get_cpu_temperature_and_throttling():
    lines = []
    try:
        zones = _powershell_json(
            "Get-CimInstance -Namespace root\\wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
            "Select-Object InstanceName, CurrentTemperature | ConvertTo-Json",
            timeout=20,
        )
        for z in zones:
            celsius = z["CurrentTemperature"] / 10 - 273.15
            lines.append(f"  Thermal zone {z['InstanceName']}: {celsius:.0f}°C")
    except Exception:
        lines.append(
            "  CPU temperature: not available. This PC's motherboard doesn't report it to Windows "
            "(common). A free tool like LibreHardwareMonitor or HWiNFO can show it."
        )

    try:
        perf = _powershell_json(
            "Get-CimInstance Win32_PerfFormattedData_Counters_ProcessorInformation -Filter \"Name='_Total'\" | "
            "Select-Object PercentProcessorPerformance, PercentofMaximumFrequency | ConvertTo-Json",
            timeout=20,
        )[0]
        load = psutil.cpu_percent(interval=1)
        perf_pct = perf.get("PercentProcessorPerformance")
        lines.append(f"  CPU load: {load:.0f}%, running at {perf_pct}% of its rated speed")
        if load > 50 and perf_pct is not None and perf_pct < 70:
            lines.append(
                "  POSSIBLE THROTTLING: the CPU is busy but running well below its rated speed. "
                "This usually means overheating (dust, blocked vents, failing fan) or a power-saving plan."
            )
        else:
            lines.append("  No sign of throttling right now (this check is most meaningful while the PC is under load).")
    except Exception as e:
        lines.append(f"  Could not read CPU speed: {e}")
    return "\n".join(lines)


def get_security_status():
    lines = []
    try:
        s = _powershell_json(
            "Get-MpComputerStatus | Select-Object AntivirusEnabled, RealTimeProtectionEnabled, "
            "AntivirusSignatureAge, QuickScanAge, FullScanAge | ConvertTo-Json",
            timeout=30,
        )[0]
        lines.append(f"  Windows Defender antivirus: {'ON' if s.get('AntivirusEnabled') else 'OFF'}")
        lines.append(f"  Real-time protection: {'ON' if s.get('RealTimeProtectionEnabled') else 'OFF (WARNING)'}")
        sig_age = s.get("AntivirusSignatureAge")
        lines.append(f"  Virus definitions: {sig_age} day(s) old" + (" (OUT OF DATE)" if (sig_age or 0) > 7 else ""))
        lines.append(f"  Last quick scan: {s.get('QuickScanAge')} day(s) ago")
    except Exception:
        lines.append("  Windows Defender status: not available (another antivirus may be installed instead).")
    try:
        profiles = _powershell_json("Get-NetFirewallProfile | Select-Object Name, Enabled | ConvertTo-Json", timeout=20)
        off = [p["Name"] for p in profiles if not p.get("Enabled")]
        lines.append("  Firewall: ON for all networks" if not off else f"  Firewall: OFF for {', '.join(off)} (WARNING)")
    except Exception:
        lines.append("  Firewall status: could not be read.")
    return "\n".join(lines)


def find_largest_files(folder="", count=15):
    root = os.path.expanduser(folder or "~")
    if not os.path.isdir(root):
        return f"Folder not found: {root}"
    count = max(1, min(int(count or 15), 50))
    deadline = time.time() + 60
    heap = []  # min-heap of (size, path, mtime)
    scanned = 0
    timed_out = False
    stack = [root]
    while stack:
        if time.time() > deadline:
            timed_out = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            scanned += 1
                            item = (st.st_size, entry.path, st.st_mtime)
                            if len(heap) < count:
                                heapq.heappush(heap, item)
                            elif st.st_size > heap[0][0]:
                                heapq.heapreplace(heap, item)
                    except OSError:
                        continue
        except OSError:
            continue

    lines = [f"Largest files in {root} ({scanned:,} files checked):"]
    for size, path, mtime in sorted(heap, reverse=True):
        modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        lines.append(f"  {_format_size(size)}  {path}  (last modified {modified})")
    if timed_out:
        lines.append("(Stopped after 60 seconds, so this may not include every file.)")
    return "\n".join(lines)


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


def _installed_programs():
    """Returns {display name: (size_bytes, install_date 'YYYYMMDD')} for user-visible installed programs."""
    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    def value(subkey, name):
        try:
            return winreg.QueryValueEx(subkey, name)[0]
        except OSError:
            return None

    programs = {}  # name -> (size_bytes, install_date)
    for hive, path in paths:
        try:
            with winreg.OpenKey(hive, path) as key:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            name = value(subkey, "DisplayName")
                            # Skip hidden system components and updates, which aren't uninstalled from here
                            if not name or value(subkey, "SystemComponent") == 1 or value(subkey, "ParentKeyName"):
                                continue
                            size_kb = value(subkey, "EstimatedSize") or 0
                            date = str(value(subkey, "InstallDate") or "")
                            if name not in programs or size_kb * 1024 > programs[name][0]:
                                programs[name] = (size_kb * 1024, date)
                    except OSError:
                        pass
        except FileNotFoundError:
            continue
    return programs


def list_installed_programs():
    programs = _installed_programs()
    if not programs:
        return "Could not read installed programs list."

    def fmt_date(d):
        try:
            return datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return "unknown"  # some installers write missing or invalid dates

    ranked = sorted(programs.items(), key=lambda kv: kv[1][0], reverse=True)
    lines = [f"{len(ranked)} programs installed, largest first (sizes are the installer's own estimate; some apps don't report one):"]
    for name, (size, date) in ranked[:40]:
        size_text = _format_size(size) if size else "size not reported"
        lines.append(f"  {name}: {size_text}, installed {fmt_date(date)}")
    if len(ranked) > 40:
        lines.append(f"  ...and {len(ranked) - 40} more")
    lines.append("To uninstall, the user can go to Settings > Apps > Installed apps.")
    return "\n".join(lines)


# ---- Action tools (change something on the PC) ----

# ---- Storage cleanup ----
# Each category is a fixed list of folders whose *contents* are safe to delete: Windows and
# apps recreate them as needed. Nothing outside these folders is ever deleted.

_LOCAL = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
_WINDIR = os.environ.get("WINDIR", r"C:\Windows")
_CHROMIUM_BROWSERS = [
    rf"{_LOCAL}\Microsoft\Edge\User Data",
    rf"{_LOCAL}\Google\Chrome\User Data",
    rf"{_LOCAL}\BraveSoftware\Brave-Browser\User Data",
]

CLEANUP_CATEGORIES = {
    "temp_files": {
        "label": "Your temporary files",
        "note": "Leftovers from installers and apps. Files currently in use are skipped.",
        "admin": False,
    },
    "windows_temp": {
        "label": "Windows temporary files",
        "note": "System-wide temp folder. Needs administrator approval (a Windows prompt will appear).",
        "admin": True,
    },
    "browser_cache": {
        "label": "Browser caches (Edge, Chrome, Brave, Firefox)",
        "note": "Cached images and scripts from websites. You stay logged in and keep history/bookmarks; sites may load slightly slower the first time. Close browsers first for the best result.",
        "admin": False,
    },
    "shader_cache": {
        "label": "Graphics shader caches (NVIDIA, AMD, DirectX)",
        "note": "Rebuilt automatically. Games may stutter or load slower the first time you play them afterwards.",
        "admin": False,
    },
    "crash_dumps": {
        "label": "App crash reports",
        "note": "Diagnostic files saved when programs crash. Only useful to developers.",
        "admin": False,
    },
    "windows_update_cache": {
        "label": "Windows Update downloads",
        "note": "Already-installed update files. Needs administrator approval (a Windows prompt will appear).",
        "admin": True,
    },
    "recycle_bin": {
        "label": "Recycle Bin",
        "note": "PERMANENT: files in the Recycle Bin can't be recovered afterwards.",
        "admin": False,
    },
}


def _category_folders(category):
    import glob
    if category == "temp_files":
        return [tempfile.gettempdir()]
    if category == "windows_temp":
        return [rf"{_WINDIR}\Temp"]
    if category == "windows_update_cache":
        return [rf"{_WINDIR}\SoftwareDistribution\Download"]
    if category == "crash_dumps":
        return [rf"{_LOCAL}\CrashDumps"]
    if category == "shader_cache":
        return [
            rf"{_LOCAL}\NVIDIA\DXCache", rf"{_LOCAL}\NVIDIA\GLCache",
            rf"{_LOCAL}\AMD\DxCache", rf"{_LOCAL}\AMD\GLCache", rf"{_LOCAL}\D3DSCache",
        ]
    if category == "browser_cache":
        folders = []
        for browser in _CHROMIUM_BROWSERS:
            for profile in glob.glob(os.path.join(glob.escape(browser), "*")):
                for sub in ("Cache", "Code Cache", "GPUCache"):
                    folders.append(os.path.join(profile, sub))
        folders += glob.glob(os.path.join(glob.escape(rf"{_LOCAL}\Mozilla\Firefox\Profiles"), "*", "cache2"))
        return folders
    return []


def _recycle_bin_size():
    import ctypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return None, None
    return info.i64Size, info.i64NumItems


def _category_size(category):
    """Returns (bytes, readable). readable is False if the folders need admin rights to look inside."""
    if category == "recycle_bin":
        size, _ = _recycle_bin_size()
        return (size or 0), size is not None
    total = 0
    readable = True
    for folder in _category_folders(category):
        if not os.path.isdir(folder):
            continue
        try:
            os.listdir(folder)
        except PermissionError:
            readable = False
            continue
        total += _folder_size(folder)
    return total, readable


def _delete_folder_contents(folder):
    freed = skipped = 0
    if not os.path.isdir(folder):
        return 0, 0
    for entry in os.scandir(folder):
        try:
            if entry.is_dir(follow_symlinks=False):
                size = _folder_size(entry.path)
                shutil.rmtree(entry.path, ignore_errors=True)
                freed += size - (_folder_size(entry.path) if os.path.exists(entry.path) else 0)
                if os.path.exists(entry.path):
                    skipped += 1
            else:
                size = entry.stat(follow_symlinks=False).st_size
                os.remove(entry.path)
                freed += size
        except OSError:
            skipped += 1
    return freed, skipped


def _run_elevated(script, wait):
    """Run a PowerShell script in a new administrator window (Windows shows a UAC prompt).
    Returns (ok, error_message)."""
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    args = "'-NoProfile','-EncodedCommand','" + encoded + "'"
    if not wait:
        args = "'-NoExit'," + args
    wait_flag = " -Wait" if wait else ""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Start-Process powershell -Verb RunAs{wait_flag} -ArgumentList {args}"],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        if "canceled by the user" in result.stderr:
            return False, "the user declined the Windows administrator prompt"
        return False, result.stderr.strip()
    return True, ""


def get_cleanup_report():
    lines = ["Space that can be safely freed (nothing has been deleted):"]
    total = 0
    for key, cat in CLEANUP_CATEGORIES.items():
        size, readable = _category_size(key)
        if not readable:
            amount = "size unknown (needs admin to check)"
        else:
            amount = _format_size(size)
            total += size
        lines.append(f"  [{key}] {cat['label']}: {amount}\n      {cat['note']}")
    lines.append(f"\nTotal (excluding unknown): about {_format_size(total)}")
    return "\n".join(lines)


def clean_up(categories):
    if isinstance(categories, str):
        categories = [categories]
    unknown = [c for c in categories if c not in CLEANUP_CATEGORIES]
    if unknown:
        return f"Unknown cleanup categories: {', '.join(unknown)}. Valid: {', '.join(CLEANUP_CATEGORIES)}."

    lines = []
    total_freed = 0
    admin_needed = []
    for key in categories:
        label = CLEANUP_CATEGORIES[key]["label"]
        if key == "recycle_bin":
            size, items = _recycle_bin_size()
            if not items:
                lines.append(f"  {label}: already empty")
                continue
            import ctypes
            # Flags: no confirmation, no progress UI, no sound
            if ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4) == 0:
                total_freed += size
                lines.append(f"  {label}: emptied, freed {_format_size(size)}")
            else:
                lines.append(f"  {label}: could not be emptied")
            continue
        if CLEANUP_CATEGORIES[key]["admin"] and not _is_admin():
            admin_needed.append(key)
            continue
        freed = skipped = 0
        for folder in _category_folders(key):
            f, s = _delete_folder_contents(folder)
            freed += f
            skipped += s
        total_freed += freed
        extra = f" ({skipped} items skipped because they're in use)" if skipped else ""
        lines.append(f"  {label}: freed {_format_size(freed)}{extra}")

    if admin_needed:
        free_before = psutil.disk_usage(SYSTEM_DRIVE).free
        folders = [f for key in admin_needed for f in _category_folders(key)]
        script = "Write-Host 'PC Assistant is cleaning up system files...' -ForegroundColor Cyan; " + "; ".join(
            f"Get-ChildItem -LiteralPath '{f}' -Force -ErrorAction SilentlyContinue | "
            "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
            for f in folders
        )
        ok, error = _run_elevated(script, wait=True)
        labels = ", ".join(CLEANUP_CATEGORIES[k]["label"] for k in admin_needed)
        if ok:
            freed = max(0, psutil.disk_usage(SYSTEM_DRIVE).free - free_before)
            total_freed += freed
            lines.append(f"  {labels}: cleaned with administrator rights, freed about {_format_size(freed)}")
        else:
            lines.append(f"  {labels}: skipped ({error})")

    labels = ", ".join(CLEANUP_CATEGORIES[k]["label"] for k in categories)
    _record_change(f"Cleaned up {labels}, freed about {_format_size(total_freed)}")
    return "Cleanup finished:\n" + "\n".join(lines) + f"\nTotal freed: about {_format_size(total_freed)}"


def find_duplicate_files(folder=""):
    import hashlib
    if folder:
        roots = [os.path.expanduser(folder)]
    else:
        home = os.path.expanduser("~")
        roots = [os.path.join(home, f) for f in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")]
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        return f"Folder not found: {folder}"

    min_size = 1024 * 1024  # ignore files under 1 MB; small duplicates aren't worth the user's time
    deadline = time.time() + 90
    by_size = defaultdict(list)
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            if time.time() > deadline:
                break
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size >= min_size:
                    by_size[size].append(path)

    def file_hash(path, partial):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            if partial:
                h.update(f.read(64 * 1024))
            else:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
        return h.hexdigest()

    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2 or time.time() > deadline:
            continue
        for partial in (True, False):  # cheap check on the first 64 KB, then a full hash
            buckets = defaultdict(list)
            for p in paths:
                try:
                    buckets[file_hash(p, partial)].append(p)
                except OSError:
                    pass
            matches = [b for b in buckets.values() if len(b) > 1]
            paths = [p for b in matches for p in b]
        groups += [(size, g) for g in matches]

    if not groups:
        return f"No duplicate files over 1 MB found in: {', '.join(roots)}"
    groups.sort(key=lambda g: g[0] * (len(g[1]) - 1), reverse=True)
    wasted = sum(size * (len(paths) - 1) for size, paths in groups)
    lines = [f"Found {len(groups)} sets of identical files, wasting about {_format_size(wasted)}:"]
    for size, paths in groups[:20]:
        lines.append(f"  {_format_size(size)} each:")
        lines += [f"    {p}" for p in paths]
    if len(groups) > 20:
        lines.append(f"  ...and {len(groups) - 20} more sets")
    if time.time() > deadline:
        lines.append("(Stopped after 90 seconds, so this may not include every file.)")
    return "\n".join(lines)


def disable_startup_program(program_name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            # Save the entry first so it can be restored with undo_change
            command, value_type = winreg.QueryValueEx(key, program_name)
            winreg.DeleteValue(key, program_name)
        _record_change(
            f"Disabled '{program_name}' from starting with Windows",
            {"type": "startup", "name": program_name, "command": command, "value_type": value_type},
        )
        return f"Disabled '{program_name}' from launching at startup. This can be undone from the change history."
    except FileNotFoundError:
        return (
            f"Could not find a current-user startup entry named '{program_name}'. "
            "It may be an all-users entry (needs admin), or the name may not match exactly."
        )
    except PermissionError:
        return f"Permission denied trying to remove '{program_name}'. It may require administrator access."


def kill_process(process_name):
    found = killed = denied = 0
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] and proc.info["name"].lower() == process_name.lower():
            found += 1
            try:
                proc.terminate()
                killed += 1
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                denied += 1
    if not found:
        return f"No running process found named '{process_name}'. Check get_top_processes for the exact name."
    msg = f"Closed {killed} process(es) named '{process_name}'."
    if killed:
        _record_change(f"Closed {process_name}")
    if denied:
        msg += f" {denied} could not be closed (access denied, may need administrator rights)."
    return msg


POWER_PLANS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "power saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
    "high performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
}


def set_power_plan(plan_name):
    key = plan_name.strip().lower()
    if key not in POWER_PLANS:
        return f"Unknown power plan '{plan_name}'. Choose from: Balanced, Power Saver, High Performance."
    import re
    try:
        before = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})\s+\((.+)\)", before, re.IGNORECASE)
        result = subprocess.run(
            ["powercfg", "/setactive", POWER_PLANS[key]],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return (
                f"Could not switch to '{plan_name}': {(result.stderr or result.stdout).strip()} "
                "(Many newer laptops hide this plan; Windows Settings > Power mode may be the alternative.)"
            )
        undo = {"type": "power_plan", "guid": m.group(1), "name": m.group(2)} if m else None
        previous = f" (was {m.group(2)})" if m else ""
        _record_change(f"Switched power plan to {plan_name}{previous}", undo)
        return f"Switched to the '{plan_name}' power plan."
    except Exception as e:
        return f"Could not switch power plan: {e}"


def repair_system_files():
    # DISM repairs the Windows component store first, then sfc uses it to fix system files.
    # Both need administrator rights, so open them in a separate elevated window (Windows shows
    # a UAC prompt) where the user can watch progress; they take 10-30 minutes.
    script = (
        "Write-Host 'Step 1 of 2: DISM is checking and repairing the Windows image...' -ForegroundColor Cyan; "
        "DISM /Online /Cleanup-Image /RestoreHealth; "
        "Write-Host ''; Write-Host 'Step 2 of 2: System File Checker is scanning system files...' -ForegroundColor Cyan; "
        "sfc /scannow; "
        "Write-Host ''; Write-Host 'Done. You can close this window. Restart your PC if any repairs were made.' -ForegroundColor Green"
    )
    ok, error = _run_elevated(script, wait=False)
    if not ok:
        return f"The repair did not start: {error}."
    _record_change("Started Windows system file repair (DISM + sfc)")
    return (
        "Started the system repair in a separate administrator window. It runs DISM then sfc /scannow "
        "and usually takes 10-30 minutes. The user should leave that window open until it says Done, "
        "then restart if repairs were made."
    )


# ---- Windows updates & drivers ----

def check_windows_updates():
    data = _powershell_json(r"""
$ErrorActionPreference = 'Stop'
$searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
$result = $searcher.Search("IsInstalled=0 and IsHidden=0")
$pending = @($result.Updates | ForEach-Object {
    [pscustomobject]@{ Title = $_.Title; Driver = ($_.Type -eq 2); Severity = $_.MsrcSeverity; Recommended = [bool]$_.AutoSelectOnWebSites }
})
$count = $searcher.GetTotalHistoryCount()
$last = @($searcher.QueryHistory(0, [Math]::Min($count, 200)) | Where-Object {
    $_.ResultCode -eq 2 -and $_.Operation -eq 1 -and $_.Title -notmatch 'Security Intelligence|Defender Antivirus'
} | Select-Object -First 1)
[pscustomobject]@{
    Pending = $pending
    LastTitle = if ($last) { $last[0].Title } else { $null }
    LastDate = if ($last) { $last[0].Date.ToString('yyyy-MM-dd') } else { $null }
} | ConvertTo-Json -Depth 4
""", timeout=180)[0]
    pending = data.get("Pending") or []
    if isinstance(pending, dict):
        pending = [pending]

    lines = []
    if data.get("LastDate"):
        days = (datetime.now() - datetime.strptime(data["LastDate"], "%Y-%m-%d")).days
        warn = " (WARNING: over a month ago)" if days > 31 else ""
        lines.append(f"Last Windows update installed: {data['LastDate']}, {days} day(s) ago{warn}\n  {data['LastTitle']}")
    else:
        lines.append("Last Windows update installed: unknown")

    if not pending:
        lines.append("Pending updates: none, Windows is up to date.")
    else:
        lines.append(f"Pending updates ({len(pending)}):")
        for u in pending:
            tags = []
            if u.get("Severity"):
                tags.append(f"{u['Severity']} security fix")
            if u.get("Driver"):
                tags.append("driver")
            if not u.get("Recommended"):
                tags.append("optional")
            lines.append(f"  - {u['Title']}" + (f" [{', '.join(tags)}]" if tags else ""))
        lines.append("Updates are installed from Settings > Windows Update (open_page 'windows_update').")

    pending_restart = _pending_restart_reasons()
    if pending_restart:
        lines.append(f"A restart is needed to finish installing updates ({', '.join(pending_restart)}).")
    return "\n".join(lines)


DRIVER_PAGES = {
    "nvidia_drivers": "https://www.nvidia.com/en-us/drivers/",
    "amd_drivers": "https://www.amd.com/en/support/download/drivers.html",
    "intel_drivers": "https://www.intel.com/content/www/us/en/support/detect.html",
}

_DEVICE_ERRORS = {
    1: "isn't configured correctly", 3: "driver may be corrupted", 10: "can't start",
    12: "not enough free resources", 14: "needs a restart", 18: "drivers need reinstalling",
    19: "registry settings are damaged", 22: "is disabled", 28: "has no driver installed",
    31: "isn't working properly", 39: "driver is missing or corrupted", 43: "stopped after reporting problems",
}


def check_drivers():
    info = _powershell_json(r"""
$gpu = @(Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, AdapterCompatibility,
    @{n='DriverDate'; e={ if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } }})
$board = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Manufacturer
$problems = @(Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode <> 0' |
    Where-Object { $_.ConfigManagerErrorCode -notin 24, 45 } | Select-Object Name, ConfigManagerErrorCode)
[pscustomobject]@{ Gpu = $gpu; Board = $board; CpuVendor = $cpu; Problems = $problems } | ConvertTo-Json -Depth 4
""", timeout=60)[0]

    def as_list(x):
        return [x] if isinstance(x, dict) else (x or [])

    lines = ["Graphics drivers:"]
    for g in as_list(info.get("Gpu")):
        name = g.get("Name") or "Unknown"
        vendor = f"{name} {g.get('AdapterCompatibility') or ''}".lower()
        page = None
        if "nvidia" in vendor:
            page = "nvidia_drivers"
        elif "amd" in vendor or "radeon" in vendor or "advanced micro" in vendor:
            page = "amd_drivers"
        elif "intel" in vendor:
            page = "intel_drivers"

        line = f"  {name}: driver {g.get('DriverVersion')}"
        if "basic display" in name.lower():
            line += " (WARNING: no proper graphics driver installed, so performance will be very poor)"
        elif g.get("DriverDate"):
            age = (datetime.now() - datetime.strptime(g["DriverDate"], "%Y-%m-%d")).days
            line += f", dated {g['DriverDate']} ({age} days old)"
            if page and age > 180:
                line += " - OUTDATED for gaming, worth updating"
            elif page:
                line += " - recent"
        if page:
            line += f"\n    Official download page: {DRIVER_PAGES[page]} (open_page '{page}')"
        lines.append(line)

    board = info.get("Board") or {}
    cpu_vendor = info.get("CpuVendor") or ""
    lines.append(f"\nMotherboard: {board.get('Manufacturer')} {board.get('Product')}")
    if "AMD" in cpu_vendor:
        lines.append("  Chipset drivers: from AMD's site (open_page 'amd_drivers'), or the motherboard maker's support page.")
    elif "Intel" in cpu_vendor:
        lines.append("  Chipset drivers: Intel's Driver & Support Assistant (open_page 'intel_drivers'), or the motherboard maker's support page.")

    problems = as_list(info.get("Problems"))
    if problems:
        lines.append("\nDevices with problems (from Device Manager):")
        for p in problems:
            code = p.get("ConfigManagerErrorCode")
            lines.append(f"  {p.get('Name')}: {_DEVICE_ERRORS.get(code, 'has a problem')} (error code {code})")
    else:
        lines.append("\nDevices with problems: none, all hardware is working normally.")
    lines.append("\nNever use third-party 'driver updater' programs; get drivers from the manufacturer only.")
    return "\n".join(lines)


# ---- Unwanted software & security ----

# Lower-case name fragments of programs that are commonly unwanted. These are well-known
# scareware/adware/"optimizers"; matching one is a strong hint, not proof.
_PUP_PATTERNS = {
    "driver updater (unnecessary, often pushy upsells)": [
        "driver booster", "driver easy", "driverpack", "driver support", "driver updater", "drivermax",
        "smart driver", "driver genius", "outbyte driver",
    ],
    "\"PC optimizer\" / registry cleaner (snake oil, often scareware)": [
        "advanced systemcare", "pc optimizer", "reimage", "restoro", "fortect", "mypc utilities",
        "wise care 365", "auslogics boostspeed", "glary utilities", "slimcleaner", "pc cleaner",
        "pc helpsoft", "advanced system optimizer", "winzip system utilities", "one safe pc cleaner",
        "outbyte pc repair", "systweak", "segurazo", "pc accelerate", "speedypc", "registry reviver",
    ],
    "adware / browser hijacker": [
        "wave browser", "webdiscover", "onelaunch", "shift browser", "search protect", "conduit",
        "ask toolbar", "mindspark", "babylon", "pc app store", "mobogenie", "iminent", "bytefence",
        "safe search", "cleanmypc", "yandex", "chromium (unofficial)",
    ],
    "trial antivirus (conflicts with Windows Defender if you didn't buy it)": [
        "mcafee", "norton", "totalav", "avast free", "avg antivirus", "segurazo",
    ],
}

# Microsoft Store apps that come preinstalled and are harmless but removable if unused.
_BLOAT_APPX = {
    "Microsoft.BingNews": "Microsoft News",
    "Microsoft.BingWeather": "Weather",
    "Microsoft.MicrosoftSolitaireCollection": "Solitaire Collection (has ads)",
    "Microsoft.GetHelp": "Get Help",
    "Clipchamp.Clipchamp": "Clipchamp video editor",
    "Microsoft.Todos": "Microsoft To Do",
    "MSTeams": "Microsoft Teams (personal)",
    "Microsoft.People": "People",
    "Microsoft.MicrosoftOfficeHub": "Microsoft 365 (Office) promo app",
    "Microsoft.WindowsFeedbackHub": "Feedback Hub",
    "Microsoft.549981C3F5F10": "Cortana",
    "Microsoft.YourPhone": "Phone Link",
    "Microsoft.WindowsMaps": "Maps",
    "Microsoft.MixedReality.Portal": "Mixed Reality Portal",
    "Microsoft.Getstarted": "Tips",
    "Microsoft.PowerAutomateDesktop": "Power Automate",
    "king.com.CandyCrushSaga": "Candy Crush Saga",
    "king.com.CandyCrushSodaSaga": "Candy Crush Soda Saga",
    "SpotifyAB.SpotifyMusic": "Spotify (preinstalled)",
    "Disney.37853FC22B2CE": "Disney+",
    "BytedancePte.Ltd.TikTok": "TikTok",
    "Facebook.Facebook": "Facebook",
    "Facebook.InstagramBeta": "Instagram",
    "AmazonVideo.PrimeVideo": "Prime Video",
}

_SCRIPT_HOSTS = {"powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "cmd.exe", "regsvr32.exe"}


def _exe_from_command(command):
    import re
    cmd = os.path.expandvars((command or "").strip())
    if cmd.startswith('"'):
        path = cmd[1:].split('"', 1)[0]
    else:
        m = re.match(r"^(.+?\.(?:exe|bat|cmd|vbs|js|ps1|com|scr|lnk))\b", cmd, re.IGNORECASE)
        path = m.group(1) if m else cmd.split(" ")[0]
    if path and not os.path.isabs(path):
        found = shutil.which(path) or os.path.join(_WINDIR, "System32", path)
        path = found
    return path


def _signatures(paths):
    """Returns {path: (status, signer)} using Windows' Authenticode check."""
    paths = [p for p in set(paths) if p and os.path.isfile(p)]
    if not paths:
        return {}
    quoted = json.dumps(paths).replace("'", "''")
    # Note: in Windows PowerShell 5.1, piping ConvertFrom-Json output passes the whole array as one item,
    # so loop with foreach instead, and cast to [string] so paths serialize as plain strings.
    rows = _powershell_json(
        f"$paths = ConvertFrom-Json '{quoted}'; "
        "$out = foreach ($p in $paths) { $s = Get-AuthenticodeSignature -LiteralPath $p; "
        "[pscustomobject]@{ Path = [string]$p; Status = [string]$s.Status; "
        "Signer = if ($s.SignerCertificate) { $s.SignerCertificate.GetNameInfo('SimpleName', $false) } else { $null } } }; "
        "ConvertTo-Json @($out)",
        timeout=120,
    )
    return {r["Path"]: (r.get("Status"), r.get("Signer")) for r in rows}


def _all_startup_entries():
    rows = _powershell_json(r"""
$entries = @()
$keys = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run', 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'
$skip = 'PSPath', 'PSParentPath', 'PSChildName', 'PSDrive', 'PSProvider'
foreach ($k in $keys) {
    if (Test-Path $k) {
        foreach ($p in (Get-ItemProperty $k).PSObject.Properties) {
            if ($p.Name -notin $skip) { $entries += [pscustomobject]@{ Name = $p.Name; Command = [string]$p.Value; Where = $k } }
        }
    }
}
$shell = New-Object -ComObject WScript.Shell
foreach ($d in "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp") {
    Get-ChildItem $d -File -ErrorAction SilentlyContinue | Where-Object Name -ne 'desktop.ini' | ForEach-Object {
        $cmd = if ($_.Extension -eq '.lnk') { '"' + $shell.CreateShortcut($_.FullName).TargetPath + '" ' + $shell.CreateShortcut($_.FullName).Arguments } else { '"' + $_.FullName + '"' }
        $entries += [pscustomobject]@{ Name = $_.BaseName; Command = $cmd; Where = 'Startup folder' }
    }
}
if ($entries.Count -eq 0) { '[]' } else { ConvertTo-Json @($entries) }
""")
    return rows


def check_unwanted_software():
    lines = []
    # Include both the short (C:\Users\ABCDEF~1) and long forms of the temp folder
    temp_dirs = [d.lower() for d in (tempfile.gettempdir(), os.path.join(_LOCAL, "Temp"), os.path.join(_WINDIR, "Temp"))]
    downloads = os.path.join(os.path.expanduser("~"), "Downloads").lower()
    program_dirs = [
        os.environ.get(v, "").lower() for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)
    ]

    # 1. Known unwanted programs
    installed = _installed_programs()
    found = []
    for name in installed:
        lower = name.lower()
        for reason, patterns in _PUP_PATTERNS.items():
            if any(p in lower for p in patterns):
                found.append(f"  {name}: {reason}")
                break
    lines.append("Known unwanted programs:")
    lines += found or ["  none found"]

    # 2. Preinstalled bloat apps
    try:
        appx = {a["Name"] for a in _powershell_json("Get-AppxPackage | Select-Object Name | ConvertTo-Json", timeout=60)}
    except Exception:
        appx = set()
    bloat = [f"  {label} (package {pkg})" for pkg, label in _BLOAT_APPX.items() if pkg in appx]
    lines.append("\nPreinstalled apps that are harmless but removable if unused (uninstall_store_app):")
    lines += bloat or ["  none found"]

    # 3. Startup entries and running programs in suspicious places
    entries = _all_startup_entries()
    for e in entries:
        e["Path"] = _exe_from_command(e.get("Command"))
    odd_procs = {}
    for p in psutil.process_iter(["name", "exe"]):
        exe = (p.info.get("exe") or "")
        if exe and any(exe.lower().startswith(d) for d in temp_dirs + [downloads]):
            odd_procs[exe] = p.info["name"]
    sigs = _signatures([e["Path"] for e in entries] + list(odd_procs))

    flagged, fine = [], []
    for e in entries:
        path = e["Path"]
        reasons = []
        if not os.path.exists(path):
            reasons.append("points to a file that no longer exists (a harmless leftover, safe to remove)")
        else:
            if any(path.lower().startswith(d) for d in temp_dirs):
                reasons.append("runs from a temporary folder, which legitimate programs rarely do")
            if os.path.basename(path).lower() in _SCRIPT_HOSTS:
                reasons.append(f"runs a script or command: {e['Command'][:200]}")
            else:
                status, signer = sigs.get(path, (None, None))
                if status and status != "Valid":
                    reason = f"is not digitally signed by its publisher (signature: {status})"
                    if any(path.lower().startswith(d) for d in program_dirs):
                        reason += ". It's in Program Files, which only installers with admin rights can write to, so this is lower risk; some legitimate apps just aren't signed"
                    reasons.append(reason)
        if reasons:
            flagged.append(f"  {e['Name']} -> {path}\n    " + "\n    ".join(reasons))
        else:
            signer = sigs.get(path, (None, None))[1]
            fine.append(f"{e['Name']}" + (f" (signed by {signer})" if signer else ""))
    lines.append("\nStartup entries needing a closer look:")
    lines += flagged or ["  none, every startup entry is signed by a known publisher"]
    if fine:
        lines.append("  Verified OK: " + ", ".join(fine))

    lines.append("\nRunning programs from Temp or Downloads folders:")
    if odd_procs:
        for exe, name in odd_procs.items():
            status, signer = sigs.get(exe, (None, None))
            sig = f"signed by {signer}" if status == "Valid" else "NOT signed"
            lines.append(f"  {name} -> {exe} ({sig})")
    else:
        lines.append("  none")

    lines.append(
        "\nNote: a flag means 'worth checking', not 'definitely malware'. If anything looks suspicious, "
        "run a Microsoft Defender scan (open_page 'defender')."
    )
    return "\n".join(lines)


def list_browser_extensions():
    import glob

    def chromium_name(manifest_path, manifest):
        name = manifest.get("name", "")
        if name.startswith("__MSG_") and name.endswith("__"):
            msg_key = name[6:-2].lower()
            locale = manifest.get("default_locale", "en")
            base = os.path.dirname(manifest_path)
            for loc in (locale, "en", "en_US"):
                try:
                    with open(os.path.join(base, "_locales", loc, "messages.json"), encoding="utf-8-sig") as f:
                        messages = {k.lower(): v for k, v in json.load(f).items()}
                    if msg_key in messages:
                        return messages[msg_key].get("message", name)
                except (OSError, ValueError):
                    continue
        return name

    lines = []
    browsers = {"Edge": _CHROMIUM_BROWSERS[0], "Chrome": _CHROMIUM_BROWSERS[1], "Brave": _CHROMIUM_BROWSERS[2]}
    for browser, user_data in browsers.items():
        names = set()
        for manifest_path in glob.glob(os.path.join(glob.escape(user_data), "*", "Extensions", "*", "*", "manifest.json")):
            try:
                with open(manifest_path, encoding="utf-8-sig") as f:
                    manifest = json.load(f)
            except (OSError, ValueError):
                continue
            name = chromium_name(manifest_path, manifest)
            ext_id = manifest_path.split(os.sep)[-3]
            names.add(f"{name} (id {ext_id})")
        if os.path.isdir(user_data):
            lines.append(f"{browser}:")
            lines += [f"  {n}" for n in sorted(names)] or ["  no extensions installed"]

    for ext_json in glob.glob(os.path.join(glob.escape(os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")), "*", "extensions.json")):
        try:
            with open(ext_json, encoding="utf-8") as f:
                addons = json.load(f).get("addons", [])
        except (OSError, ValueError):
            continue
        lines.append("Firefox:")
        user_addons = [a for a in addons if a.get("type") == "extension" and a.get("location") == "app-profile"]
        for a in user_addons:
            name = (a.get("defaultLocale") or {}).get("name") or a.get("id")
            lines.append(f"  {name}" + ("" if a.get("active") else " (disabled)"))
        if not user_addons:
            lines.append("  no extensions installed")

    if not lines:
        return "No supported browsers (Edge, Chrome, Brave, Firefox) found."
    lines.append(
        "\nExtensions can read the pages you visit. Remove any the user doesn't recognise or use, from the "
        "browser's Extensions page (edge://extensions or chrome://extensions)."
    )
    return "\n".join(lines)


def uninstall_store_app(package_name):
    if package_name not in _BLOAT_APPX:
        return (
            f"'{package_name}' isn't on the list of safely removable preinstalled apps. "
            "Other apps can be uninstalled from Settings > Apps > Installed apps."
        )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-AppxPackage -Name '{package_name}' | Remove-AppxPackage -ErrorAction Stop"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        return f"Could not uninstall {_BLOAT_APPX[package_name]}: {result.stderr.strip()[:300]}"
    _record_change(
        f"Uninstalled {_BLOAT_APPX[package_name]}",
        {"type": "store_app", "label": _BLOAT_APPX[package_name]},
    )
    return f"Uninstalled {_BLOAT_APPX[package_name]}. It can be reinstalled from the Microsoft Store any time."


# ---- Network ----

def _ping(host, count=10):
    import re
    result = subprocess.run(["ping", "-n", str(count), "-w", "1000", host], capture_output=True, text=True, timeout=count * 2 + 10)
    replies = [line for line in result.stdout.splitlines() if "TTL=" in line.upper()]
    times = [int(m.group(1)) for line in replies for m in [re.search(r"[=<](\d+)\s*ms", line)] if m]
    loss = 100 * (count - len(replies)) / count
    avg = sum(times) / len(times) if times else None
    jitter = (max(times) - min(times)) if times else None
    return loss, avg, jitter


def get_network_status():
    import re
    lines = []
    try:
        adapters = _powershell_json(
            "Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name, InterfaceDescription, LinkSpeed, "
            "@{n='Wireless'; e={ $_.PhysicalMediaType -match '802.11' -or $_.InterfaceDescription -match 'Wi-?Fi|Wireless' }} | ConvertTo-Json",
            timeout=30,
        )
    except RuntimeError:  # no output means no adapter is up
        adapters = []
    if not adapters:
        return "No network adapter is connected. Check the cable or Wi-Fi connection."
    lines.append("Connected adapters:")
    for a in adapters:
        kind = "Wi-Fi" if a.get("Wireless") else "Wired"
        lines.append(f"  {a.get('Name')} ({kind}, {a.get('LinkSpeed')}): {a.get('InterfaceDescription')}")
        if a.get("Wireless"):
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=15).stdout
            m = re.search(r"(\d+)%", out)
            if m:
                signal = int(m.group(1))
                note = " (WEAK: move closer to the router or use a cable)" if signal < 50 else ""
                lines.append(f"    Wi-Fi signal: {signal}%{note}")

    try:
        gateway = _powershell_json(
            "Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | Select-Object -First 1 NextHop | ConvertTo-Json",
            timeout=20,
        )[0]["NextHop"]
    except Exception:
        gateway = None

    targets = {"Internet (1.1.1.1)": "1.1.1.1"}
    if gateway:
        targets = {f"Router ({gateway})": gateway, **targets}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = {label: pool.submit(_ping, host) for label, host in targets.items()}
    lines.append("\nConnection quality (10 pings each):")
    for label, future in results.items():
        loss, avg, jitter = future.result()
        if avg is None:
            lines.append(f"  {label}: NO RESPONSE")
            continue
        flags = []
        if loss > 0:
            flags.append(f"{loss:.0f}% packet loss")
        if "Router" in label and avg > 10:
            flags.append("slow for a local router, suggests Wi-Fi interference or a router problem")
        if "Internet" in label and avg > 100:
            flags.append("high latency")
        if jitter and jitter > 50:
            flags.append("unstable (big swings in ping)")
        flag_text = f" - {'; '.join(flags)}" if flags else " - good"
        lines.append(f"  {label}: average {avg:.0f} ms, {loss:.0f}% loss, variation {jitter} ms{flag_text}")

    dns_times = []
    for host in ("www.microsoft.com", "www.google.com", "www.wikipedia.org"):
        start = time.time()
        try:
            socket.getaddrinfo(host, 443)
            dns_times.append((time.time() - start) * 1000)
        except OSError:
            dns_times.append(None)
    failed = dns_times.count(None)
    ok_times = [t for t in dns_times if t is not None]
    if failed == len(dns_times):
        lines.append("\nDNS (looking up website names): FAILING - websites can't be found. Flushing DNS or resetting the network may help.")
    else:
        lines.append(f"\nDNS (looking up website names): working, average {sum(ok_times) / len(ok_times):.0f} ms" + (f", {failed} lookup(s) failed" if failed else ""))
    try:
        dns = _powershell_json(
            "Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object ServerAddresses | "
            "Select-Object InterfaceAlias, @{n='Servers'; e={ $_.ServerAddresses -join ', ' }} | ConvertTo-Json",
            timeout=20,
        )
        for d in dns:
            lines.append(f"  DNS servers on {d['InterfaceAlias']}: {d['Servers']}")
    except Exception:
        pass
    lines.append("\n(This measures the connection, not download speed. For speed, the user can visit a site like speedtest.net.)")
    return "\n".join(lines)


def flush_dns():
    result = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        # Newer Windows builds require admin for this; retry in an elevated window
        ok, error = _run_elevated("ipconfig /flushdns", wait=True)
        if not ok:
            return f"Could not flush DNS: {error}."
    _record_change("Flushed the DNS cache")
    return "Flushed the DNS cache. Websites will be looked up fresh from now on."


def reset_network():
    script = (
        "Write-Host 'Resetting network settings...' -ForegroundColor Cyan; "
        "netsh winsock reset; netsh int ip reset; ipconfig /flushdns; "
        "Write-Host 'Done. Restart your PC to finish.' -ForegroundColor Green; Start-Sleep 3"
    )
    ok, error = _run_elevated(script, wait=True)
    if not ok:
        return f"The network reset did not run: {error}."
    _record_change("Reset network settings (Winsock and TCP/IP)")
    return (
        "Reset the network stack (Winsock and TCP/IP settings) and flushed DNS. The user must RESTART the PC "
        "for this to take effect. Saved Wi-Fi passwords and VPN settings are kept."
    )


# ---- Performance & gaming settings ----

def _reg_dword(hive, path, name):
    try:
        with winreg.OpenKey(hive, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _set_reg_dword(hive, path, name, value):
    with winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))


_GAMEBAR = r"Software\Microsoft\GameBar"
_GAMEDVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"
_GAMECONFIG = r"System\GameConfigStore"


def get_performance_settings():
    HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    lines = []

    game_mode = _reg_dword(HKCU, _GAMEBAR, "AutoGameModeEnabled")
    lines.append(f"Game Mode: {'OFF' if game_mode == 0 else 'ON'}" + (" (Windows default)" if game_mode is None else ""))

    capture = _reg_dword(HKCU, _GAMEDVR, "AppCaptureEnabled")
    background = _reg_dword(HKCU, _GAMEDVR, "HistoricalCaptureEnabled")
    if capture == 0:
        lines.append("Xbox Game Bar recording: OFF")
    else:
        lines.append("Xbox Game Bar recording: available")
        if background == 1:
            lines.append("  Background recording ('record what happened'): ON - this constantly records gameplay and costs FPS")
        else:
            lines.append("  Background recording ('record what happened'): off")

    hags = _reg_dword(HKLM, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode")
    hags_text = {2: "ON", 1: "OFF"}.get(hags, "Windows default")
    lines.append(f"Hardware-accelerated GPU scheduling: {hags_text} (change in graphics settings: open_page 'graphics_settings')")

    fx = _reg_dword(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "VisualFXSetting")
    fx_text = {1: "best appearance", 2: "best performance", 3: "custom"}.get(fx, "let Windows choose (default)")
    lines.append(f"Visual effects: {fx_text} (change: open_page 'visual_effects')")
    transparency = _reg_dword(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency")
    lines.append(f"Transparency effects: {'off' if transparency == 0 else 'on'}")

    ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    cores = psutil.cpu_count(logical=False) or 0
    if ram_gb < 8 or cores <= 2:
        lines.append(f"  This is a lower-end PC ({ram_gb:.0f} GB RAM, {cores} cores): turning visual effects to 'best performance' will help noticeably.")
    else:
        lines.append(f"  This PC ({ram_gb:.0f} GB RAM, {cores} cores) is powerful enough that visual effects make almost no difference.")

    try:
        pf = _powershell_json(
            "[pscustomobject]@{ Auto = (Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile; "
            "Files = @(Get-CimInstance Win32_PageFileUsage | Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage) } | ConvertTo-Json -Depth 3",
            timeout=30,
        )[0]
        files = pf.get("Files") or []
        if isinstance(files, dict):
            files = [files]
        if not files:
            lines.append("Page file (virtual memory): DISABLED - this can cause crashes when RAM fills up. Re-enable it (open_page 'page_file').")
        else:
            mode = "managed automatically by Windows (recommended)" if pf.get("Auto") else "set manually"
            lines.append(f"Page file (virtual memory): {mode}")
            for f in files:
                lines.append(f"  {f['Name']}: {f['AllocatedBaseSize']} MB, currently {f['CurrentUsage']} MB used, peak {f['PeakUsage']} MB")
    except Exception as e:
        lines.append(f"Page file: could not be read ({e})")

    lines.append(f"Power plan: {get_power_plan()}")
    return "\n".join(lines)


def _set_user_settings(description, values):
    """Set HKCU DWORD values [(path, name, value)], recording the previous values so they can be undone."""
    previous = [(path, name, _reg_dword(winreg.HKEY_CURRENT_USER, path, name)) for path, name, _ in values]
    for path, name, value in values:
        _set_reg_dword(winreg.HKEY_CURRENT_USER, path, name, value)
    _record_change(description, {"type": "user_registry", "values": previous})


def set_game_mode(enabled):
    state = "ON" if enabled else "OFF"
    _set_user_settings(f"Turned Game Mode {state}", [(_GAMEBAR, "AutoGameModeEnabled", 1 if enabled else 0)])
    return f"Game Mode is now {state}."


def set_game_recording(enabled):
    value = 1 if enabled else 0
    values = [(_GAMEDVR, "AppCaptureEnabled", value), (_GAMECONFIG, "GameDVR_Enabled", value)]
    if not enabled:
        values.append((_GAMEDVR, "HistoricalCaptureEnabled", 0))
    _set_user_settings(f"Turned Xbox Game Bar game recording {'ON' if enabled else 'OFF'}", values)
    return (
        f"Xbox Game Bar game recording is now {'ON' if enabled else 'OFF'}. "
        "Takes effect the next time a game is started."
    )


# ---- Reviewing and undoing changes ----

def get_change_history(count=15):
    history = load_history()
    if not history:
        return "No changes have been made by PC Assistant yet."
    lines = ["Changes made by PC Assistant (newest first):"]
    for c in reversed(history[-max(1, min(int(count or 15), 50)):]):
        if c.get("undone"):
            status = f"undone {c['undone']}" if isinstance(c["undone"], str) else "undone"
        elif c.get("undo"):
            status = "can be undone"
        else:
            status = "can't be undone"
        lines.append(f"  #{c['id']} [{c['time']}] {c['description']} ({status})")
    return "\n".join(lines)


def undo_change(change_id):
    history = load_history()
    change = next((c for c in history if c["id"] == int(change_id)), None)
    if change is None:
        return f"No change #{change_id} in the history."
    if change.get("undone"):
        return f"Change #{change_id} was already undone."
    undo = change.get("undo")
    if not undo:
        return f"Change #{change_id} ({change['description']}) can't be undone automatically."

    kind = undo["type"]
    if kind == "startup":
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, undo["name"], 0, undo["value_type"], undo["command"])
        result = f"'{undo['name']}' will start with Windows again."
    elif kind == "power_plan":
        r = subprocess.run(["powercfg", "/setactive", undo["guid"]], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"Could not switch back to the {undo['name']} power plan: {(r.stderr or r.stdout).strip()}"
        result = f"Switched back to the {undo['name']} power plan."
    elif kind == "user_registry":
        for path, name, old in undo["values"]:
            if old is None:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                        winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                _set_reg_dword(winreg.HKEY_CURRENT_USER, path, name, old)
        result = "Restored the previous settings."
    elif kind == "store_app":
        # Reinstalling needs the Store, so take the user to the app's Store search page
        from urllib.parse import quote
        os.startfile(f"ms-windows-store://search/?query={quote(undo['label'])}")
        return f"Opened the Microsoft Store so the user can reinstall {undo['label']} (click Get/Install)."
    else:
        return f"Don't know how to undo change #{change_id}."

    change["undone"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _save_history(history)
    return f"Undid change #{change_id}: {result}"


# ---- Opening Windows settings and official websites ----

OPEN_PAGES = {
    "windows_update": ("Windows Update settings", "ms-settings:windowsupdate"),
    "installed_apps": ("Installed apps (to uninstall programs)", "ms-settings:appsfeatures"),
    "startup_apps": ("Startup apps settings", "ms-settings:startupapps"),
    "storage_settings": ("Storage settings", "ms-settings:storagesense"),
    "game_mode": ("Game Mode settings", "ms-settings:gaming-gamemode"),
    "graphics_settings": ("Graphics settings (GPU scheduling, per-app GPU)", "ms-settings:display-advancedgraphics"),
    "visual_effects": ("Performance Options (visual effects)", "SystemPropertiesPerformance.exe"),
    "page_file": ("Advanced system settings (virtual memory is under Performance > Settings > Advanced)", "SystemPropertiesAdvanced.exe"),
    "network_settings": ("Network & internet settings", "ms-settings:network"),
    "defender": ("Windows Security (to run a virus scan)", "windowsdefender://threat"),
    "device_manager": ("Device Manager", "devmgmt.msc"),
    "nvidia_drivers": ("NVIDIA's official driver download page", DRIVER_PAGES["nvidia_drivers"]),
    "amd_drivers": ("AMD's official driver download page", DRIVER_PAGES["amd_drivers"]),
    "intel_drivers": ("Intel's official driver download page", DRIVER_PAGES["intel_drivers"]),
}


def open_page(page):
    if page not in OPEN_PAGES:
        return f"Unknown page '{page}'. Options: {', '.join(OPEN_PAGES)}."
    label, target = OPEN_PAGES[page]
    try:
        os.startfile(target)
    except OSError as e:
        return f"Could not open {label}: {e}"
    return f"Opened {label} for the user."


# ---- Tool registry ----

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
        tool_input = {**tool_input, "app": _BLOAT_APPX.get(tool_input["package_name"], tool_input["package_name"])}
    if "enabled" in tool_input:
        tool_input = {**tool_input, "state": "ON" if tool_input["enabled"] else "OFF"}
    try:
        return descriptions[name].format(**tool_input)
    except (KeyError, IndexError):
        details = "".join(f"\n  {k}: {v}" for k, v in tool_input.items())
        return f"Run {name}{details}"


def get_api_key():
    """Prefer the key saved with `setx` (HKCU\\Environment) over this process's copy. Terminals opened
    before the key was changed still carry the old value, which causes confusing 'invalid key' errors."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            saved = winreg.QueryValueEx(key, "ANTHROPIC_API_KEY")[0].strip()
            if saved:
                return saved
    except OSError:
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


def save_api_key(key):
    """Save the key for the current user (same as `setx ANTHROPIC_API_KEY ...`)."""
    import ctypes
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as env:
        winreg.SetValueEx(env, "ANTHROPIC_API_KEY", 0, winreg.REG_SZ, key)
    os.environ["ANTHROPIC_API_KEY"] = key
    # Tell other programs the environment changed, so new terminals see the key (HWND_BROADCAST, WM_SETTINGCHANGE)
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, None)


def run_tool(name, tool_input):
    """Run a tool by name, always returning a string (errors are reported, not raised)."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Unknown tool '{name}'."
    return run_tool_func(func, **(tool_input or {}))


def run_tool_func(func, **kwargs):
    try:
        return func(**kwargs)
    except Exception as e:
        return f"This check failed with an error: {e}"
