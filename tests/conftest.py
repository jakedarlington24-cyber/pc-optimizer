import winreg

import pytest

from pc_tools import history, settings

TEST_KEY = r"Software\PCAssistantTests"


def _delete_tree(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            subkeys = []
            i = 0
            while True:
                try:
                    subkeys.append(winreg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
    except FileNotFoundError:
        return
    for sub in subkeys:
        _delete_tree(rf"{path}\{sub}")
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the change history and every registry location the actions write to at throwaway
    test locations, so tests never touch the real PC's settings."""
    monkeypatch.setattr(history, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(settings, "RUN_KEY", rf"{TEST_KEY}\Run")
    monkeypatch.setattr(settings, "_GAMEBAR", rf"{TEST_KEY}\GameBar")
    monkeypatch.setattr(settings, "_GAMEDVR", rf"{TEST_KEY}\GameDVR")
    monkeypatch.setattr(settings, "_GAMECONFIG", rf"{TEST_KEY}\GameConfig")
    _delete_tree(TEST_KEY)
    yield
    _delete_tree(TEST_KEY)
