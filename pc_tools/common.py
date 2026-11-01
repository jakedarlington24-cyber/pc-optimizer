"""Small helpers shared by the tool modules."""

import json
import os
import subprocess
import winreg


SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:") + "\\"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def format_size(num_bytes):
    mb = num_bytes / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def folder_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def powershell_json(script, timeout=60):
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


def is_admin():
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))


WINDIR = os.environ.get("WINDIR", r"C:\Windows")


def run_elevated(script, wait):
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


def read_dword(hive, path, name):
    try:
        with winreg.OpenKey(hive, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def write_dword(hive, path, name, value):
    with winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))


def run_tool_func(func, **kwargs):
    try:
        return func(**kwargs)
    except Exception as e:
        return f"This check failed with an error: {e}"
