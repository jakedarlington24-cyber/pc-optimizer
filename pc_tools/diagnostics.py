"""Read-only health checks: usage, processes, disks, temperatures, security, installed programs."""

import os
import platform
import socket
import time
import winreg
from collections import defaultdict
from datetime import datetime

import psutil

from .common import RUN_KEY, SYSTEM_DRIVE, folder_size, format_size, is_admin, powershell_json


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
        return f"  {name}{copies}: {g['cpu']:.1f}% CPU, {format_size(g['mem'])} RAM ({g['mem'] / total_ram * 100:.1f}%)"

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
            results.append(f"  {folder}: {format_size(folder_size(path))}")
    return "\n".join(results) if results else "Could not read user folders."


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


def pending_restart_reasons():
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

    pending = pending_restart_reasons()
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
            f"  {part.mountpoint} {format_size(usage.free)} free of {format_size(usage.total)} "
            f"({free_pct:.0f}% free){flag}"
        )
    return "\n".join(lines) if lines else "Could not read any drives."


def get_disk_health():
    disks = powershell_json(
        "Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, BusType, HealthStatus, "
        "@{n='OperationalStatus';e={$_.OperationalStatus -join ', '}}, Size | ConvertTo-Json"
    )
    counters = {}
    if is_admin():
        try:
            for c in powershell_json(
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
            f"  {d.get('FriendlyName')}: {media}, {d.get('BusType')}, {format_size(d.get('Size') or 0)}, "
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
        zones = powershell_json(
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
        perf = powershell_json(
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
        s = powershell_json(
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
        profiles = powershell_json("Get-NetFirewallProfile | Select-Object Name, Enabled | ConvertTo-Json", timeout=20)
        off = [p["Name"] for p in profiles if not p.get("Enabled")]
        lines.append("  Firewall: ON for all networks" if not off else f"  Firewall: OFF for {', '.join(off)} (WARNING)")
    except Exception:
        lines.append("  Firewall status: could not be read.")
    return "\n".join(lines)


def installed_programs():
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
    programs = installed_programs()
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
        size_text = format_size(size) if size else "size not reported"
        lines.append(f"  {name}: {size_text}, installed {fmt_date(date)}")
    if len(ranked) > 40:
        lines.append(f"  ...and {len(ranked) - 40} more")
    lines.append("To uninstall, the user can go to Settings > Apps > Installed apps.")
    return "\n".join(lines)
