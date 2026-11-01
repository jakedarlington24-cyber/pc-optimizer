"""Loading and saving the Anthropic API key."""

import os
import winreg


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
