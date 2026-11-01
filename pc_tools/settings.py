"""Actions that change Windows settings: startup programs, power plan, gaming settings, repairs."""

import os
import subprocess
import winreg

import psutil

from .common import RUN_KEY, is_admin, powershell_json, read_dword, run_elevated, write_dword
from .history import record_change, undo_handler
from .updates import DRIVER_PAGES


def create_restore_point():
    if not is_admin():
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


def get_power_plan():
    try:
        result = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() or "Could not determine the current power plan."
    except Exception as e:
        return f"Error checking power plan: {e}"


def disable_startup_program(program_name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            # Save the entry first so it can be restored with undo_change
            command, value_type = winreg.QueryValueEx(key, program_name)
            winreg.DeleteValue(key, program_name)
        record_change(
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


@undo_handler("startup")
def _undo_startup(undo):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, undo["name"], 0, undo["value_type"], undo["command"])
    return f"'{undo['name']}' will start with Windows again.", True


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
        record_change(f"Closed {process_name}")
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
        record_change(f"Switched power plan to {plan_name}{previous}", undo)
        return f"Switched to the '{plan_name}' power plan."
    except Exception as e:
        return f"Could not switch power plan: {e}"


@undo_handler("power_plan")
def _undo_power_plan(undo):
    r = subprocess.run(["powercfg", "/setactive", undo["guid"]], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return f"Could not switch back to the {undo['name']} power plan: {(r.stderr or r.stdout).strip()}", False
    return f"Switched back to the {undo['name']} power plan.", True


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
    ok, error = run_elevated(script, wait=False)
    if not ok:
        return f"The repair did not start: {error}."
    record_change("Started Windows system file repair (DISM + sfc)")
    return (
        "Started the system repair in a separate administrator window. It runs DISM then sfc /scannow "
        "and usually takes 10-30 minutes. The user should leave that window open until it says Done, "
        "then restart if repairs were made."
    )


_GAMEBAR = r"Software\Microsoft\GameBar"


_GAMEDVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"


_GAMECONFIG = r"System\GameConfigStore"


def get_performance_settings():
    HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    lines = []

    game_mode = read_dword(HKCU, _GAMEBAR, "AutoGameModeEnabled")
    lines.append(f"Game Mode: {'OFF' if game_mode == 0 else 'ON'}" + (" (Windows default)" if game_mode is None else ""))

    capture = read_dword(HKCU, _GAMEDVR, "AppCaptureEnabled")
    background = read_dword(HKCU, _GAMEDVR, "HistoricalCaptureEnabled")
    if capture == 0:
        lines.append("Xbox Game Bar recording: OFF")
    else:
        lines.append("Xbox Game Bar recording: available")
        if background == 1:
            lines.append("  Background recording ('record what happened'): ON - this constantly records gameplay and costs FPS")
        else:
            lines.append("  Background recording ('record what happened'): off")

    hags = read_dword(HKLM, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode")
    hags_text = {2: "ON", 1: "OFF"}.get(hags, "Windows default")
    lines.append(f"Hardware-accelerated GPU scheduling: {hags_text} (change in graphics settings: open_page 'graphics_settings')")

    fx = read_dword(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "VisualFXSetting")
    fx_text = {1: "best appearance", 2: "best performance", 3: "custom"}.get(fx, "let Windows choose (default)")
    lines.append(f"Visual effects: {fx_text} (change: open_page 'visual_effects')")
    transparency = read_dword(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency")
    lines.append(f"Transparency effects: {'off' if transparency == 0 else 'on'}")

    ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    cores = psutil.cpu_count(logical=False) or 0
    if ram_gb < 8 or cores <= 2:
        lines.append(f"  This is a lower-end PC ({ram_gb:.0f} GB RAM, {cores} cores): turning visual effects to 'best performance' will help noticeably.")
    else:
        lines.append(f"  This PC ({ram_gb:.0f} GB RAM, {cores} cores) is powerful enough that visual effects make almost no difference.")

    try:
        pf = powershell_json(
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
    previous = [(path, name, read_dword(winreg.HKEY_CURRENT_USER, path, name)) for path, name, _ in values]
    for path, name, value in values:
        write_dword(winreg.HKEY_CURRENT_USER, path, name, value)
    record_change(description, {"type": "user_registry", "values": previous})


@undo_handler("user_registry")
def _undo_user_settings(undo):
    for path, name, old in undo["values"]:
        if old is None:  # the value didn't exist before, so remove it again
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        else:
            write_dword(winreg.HKEY_CURRENT_USER, path, name, old)
    return "Restored the previous settings.", True


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
