import inspect
import os
import tempfile

import pytest

import pc_tools


# ---- Tool registry: every tool Claude can see must be wired up correctly ----

def test_every_tool_has_a_function_and_vice_versa():
    names = [t["name"] for t in pc_tools.TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names"
    assert set(names) == set(pc_tools.TOOL_FUNCTIONS)


@pytest.mark.parametrize("tool", pc_tools.TOOLS, ids=lambda t: t["name"])
def test_tool_schema_matches_function_signature(tool):
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    properties = schema.get("properties", {})
    assert set(schema.get("required", [])) <= set(properties)
    params = inspect.signature(pc_tools.TOOL_FUNCTIONS[tool["name"]]).parameters
    assert set(properties) <= set(params), "schema has arguments the function doesn't accept"
    required_params = {n for n, p in params.items() if p.default is inspect.Parameter.empty}
    assert required_params <= set(schema.get("required", [])), "function needs arguments the schema doesn't require"


def test_safety_lists_only_contain_real_tools():
    assert pc_tools.ACTIONS_REQUIRING_CONFIRMATION <= set(pc_tools.TOOL_FUNCTIONS)
    assert pc_tools.ACTIONS_NEEDING_RESTORE_POINT <= pc_tools.ACTIONS_REQUIRING_CONFIRMATION


@pytest.mark.parametrize("name", sorted(pc_tools.ACTIONS_REQUIRING_CONFIRMATION))
def test_every_action_has_a_plain_english_confirmation(name):
    sample_inputs = {
        "clean_up": {"categories": ["temp_files"]},
        "disable_startup_program": {"program_name": "Discord"},
        "kill_process": {"process_name": "Discord.exe"},
        "set_power_plan": {"plan_name": "Balanced"},
        "uninstall_store_app": {"package_name": "Microsoft.BingNews"},
        "set_game_mode": {"enabled": True},
        "set_game_recording": {"enabled": False},
        "undo_change": {"change_id": 1},
    }
    text = pc_tools.describe_action(name, sample_inputs.get(name, {}))
    assert text and not text.startswith(f"Run {name}"), "fell back to the generic description"


def test_describe_clean_up_lists_each_category_with_its_side_effect():
    text = pc_tools.describe_action("clean_up", {"categories": ["recycle_bin", "shader_cache"]})
    assert "Recycle Bin" in text and "PERMANENT" in text
    assert "Graphics shader caches" in text


def test_run_tool_reports_errors_instead_of_crashing(monkeypatch):
    def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setitem(pc_tools.TOOL_FUNCTIONS, "get_basic_stats", boom)
    assert "kaboom" in pc_tools.run_tool("get_basic_stats", {})
    assert "Unknown tool" in pc_tools.run_tool("not_a_tool", {})


# ---- Cleanup safety ----

def test_cleanup_only_targets_known_safe_folders():
    safe_roots = [
        os.path.normcase(tempfile.gettempdir()),
        os.path.normcase(os.environ["LOCALAPPDATA"]),
        os.path.normcase(os.environ["WINDIR"]),
    ]
    for category in pc_tools.CLEANUP_CATEGORIES:
        for folder in pc_tools._category_folders(category):
            folder = os.path.normcase(folder)
            assert any(folder.startswith(root) for root in safe_roots), f"{category} targets {folder}"
            # Only the temp folder may be cleaned at the top level; everything else is a subfolder
            if folder in safe_roots:
                assert category == "temp_files"


def test_clean_up_rejects_unknown_categories():
    result = pc_tools.clean_up(["documents"])
    assert "Unknown cleanup categories" in result


def test_delete_folder_contents_skips_files_in_use(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.tmp").write_bytes(b"x" * 2000)
    (tmp_path / "sub" / "b.tmp").write_bytes(b"y" * 1000)
    with open(tmp_path / "locked.tmp", "wb") as locked:
        locked.write(b"z" * 500)
        locked.flush()
        freed, skipped = pc_tools._delete_folder_contents(str(tmp_path))
    assert freed == 3000
    assert skipped == 1
    assert os.listdir(tmp_path) == ["locked.tmp"]


def test_delete_folder_contents_ignores_missing_folder(tmp_path):
    assert pc_tools._delete_folder_contents(str(tmp_path / "nope")) == (0, 0)


# ---- File finders (read-only) ----

def test_find_duplicate_files_compares_content_not_just_size(tmp_path):
    data = os.urandom(1_200_000)
    (tmp_path / "x").mkdir()
    (tmp_path / "one.bin").write_bytes(data)
    (tmp_path / "x" / "copy.bin").write_bytes(data)
    (tmp_path / "lookalike.bin").write_bytes(data[:-1] + b"!")  # same size, different content
    (tmp_path / "small1.txt").write_bytes(b"same")
    (tmp_path / "small2.txt").write_bytes(b"same")  # under 1 MB, ignored

    result = pc_tools.find_duplicate_files(str(tmp_path))
    assert "Found 1 sets" in result
    assert "one.bin" in result and "copy.bin" in result
    assert "lookalike.bin" not in result and "small1.txt" not in result


def test_find_largest_files_orders_by_size(tmp_path):
    for name, size in [("small.bin", 10), ("big.bin", 5000), ("medium.bin", 500)]:
        (tmp_path / name).write_bytes(b"0" * size)
    lines = pc_tools.find_largest_files(str(tmp_path), count=2).splitlines()
    assert "big.bin" in lines[1] and "medium.bin" in lines[2]
    assert not any("small.bin" in line for line in lines)


# ---- Helpers ----

@pytest.mark.parametrize("size, expected", [
    (0, "0.0 MB"),
    (512 * 1024, "0.5 MB"),
    (1024 ** 3, "1.00 GB"),
    (int(2.5 * 1024 ** 3), "2.50 GB"),
])
def test_format_size(size, expected):
    assert pc_tools._format_size(size) == expected


@pytest.mark.parametrize("command, expected_end", [
    (r'"C:\Program Files\App\app.exe" --tray', r"\Program Files\App\app.exe"),
    (r"C:\Tools\tool.exe -silent", r"\Tools\tool.exe"),
    (r'"%WINDIR%\system32\notepad.exe"', r"\system32\notepad.exe"),
    ("rundll32.exe something.dll,Entry", "rundll32.exe"),
])
def test_exe_from_command(command, expected_end):
    assert pc_tools._exe_from_command(command).lower().endswith(expected_end.lower())


def test_actions_refuse_inputs_outside_their_allow_lists():
    assert "isn't on the list" in pc_tools.uninstall_store_app("Microsoft.WindowsCalculator")
    assert "Unknown page" in pc_tools.open_page("C:\\Windows\\System32\\cmd.exe")
    assert "Unknown power plan" in pc_tools.set_power_plan("Turbo")
