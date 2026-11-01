import json
import subprocess
import winreg
from types import SimpleNamespace

import pc_tools

HKCU = winreg.HKEY_CURRENT_USER


def test_disabled_startup_program_can_be_restored_exactly(sandbox):
    command = r'"%ProgramFiles%\Fake\fake.exe" --tray'
    with winreg.CreateKeyEx(HKCU, pc_tools.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "FakeApp", 0, winreg.REG_EXPAND_SZ, command)

    assert "Disabled" in pc_tools.disable_startup_program("FakeApp")
    with winreg.OpenKey(HKCU, pc_tools.RUN_KEY) as key:
        try:
            winreg.QueryValueEx(key, "FakeApp")
            assert False, "entry should have been removed"
        except FileNotFoundError:
            pass

    assert "start with Windows again" in pc_tools.undo_change(1)
    with winreg.OpenKey(HKCU, pc_tools.RUN_KEY) as key:
        assert winreg.QueryValueEx(key, "FakeApp") == (command, winreg.REG_EXPAND_SZ)


def test_game_settings_undo_restores_previous_values(sandbox):
    # One value existed before (should be restored), one didn't (should be removed again)
    pc_tools._set_reg_dword(HKCU, pc_tools._GAMEDVR, "AppCaptureEnabled", 1)

    pc_tools.set_game_mode(False)
    pc_tools.set_game_recording(False)
    assert pc_tools._reg_dword(HKCU, pc_tools._GAMEBAR, "AutoGameModeEnabled") == 0
    assert pc_tools._reg_dword(HKCU, pc_tools._GAMEDVR, "AppCaptureEnabled") == 0

    pc_tools.undo_change(2)
    pc_tools.undo_change(1)
    assert pc_tools._reg_dword(HKCU, pc_tools._GAMEBAR, "AutoGameModeEnabled") is None
    assert pc_tools._reg_dword(HKCU, pc_tools._GAMEDVR, "AppCaptureEnabled") == 1
    assert pc_tools._reg_dword(HKCU, pc_tools._GAMEDVR, "HistoricalCaptureEnabled") is None


def test_power_plan_undo_switches_back_to_previous_plan(sandbox, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        out = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)" if "/getactivescheme" in args else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    pc_tools.set_power_plan("High Performance")
    assert calls[-1] == ["powercfg", "/setactive", pc_tools.POWER_PLANS["high performance"]]

    assert "Balanced" in pc_tools.undo_change(1)
    assert calls[-1] == ["powercfg", "/setactive", "381b4222-f694-41f0-9685-ff5bb260df2e"]


def test_history_marks_undone_and_refuses_repeats(sandbox):
    pc_tools.set_game_mode(True)
    pc_tools._record_change("Cleaned up something")  # an action with no undo data

    assert "Undid change #1" in pc_tools.undo_change(1)
    assert "already undone" in pc_tools.undo_change(1)
    assert "can't be undone" in pc_tools.undo_change(2)
    assert "No change #99" in pc_tools.undo_change(99)

    history = pc_tools.get_change_history()
    assert "#2" in history and "#1" in history
    assert history.index("#2") < history.index("#1"), "newest should be listed first"


def test_history_is_capped(sandbox):
    for i in range(205):
        pc_tools._record_change(f"change {i}")
    with open(pc_tools.HISTORY_FILE, encoding="utf-8") as f:
        history = json.load(f)
    assert len(history) == 200
    assert history[-1]["id"] == 205
