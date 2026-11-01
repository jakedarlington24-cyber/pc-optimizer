"""Windows Update status and driver checks."""

from datetime import datetime

from .common import powershell_json
from .diagnostics import pending_restart_reasons


def check_windows_updates():
    data = powershell_json(r"""
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

    pending_restart = pending_restart_reasons()
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
    info = powershell_json(r"""
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
