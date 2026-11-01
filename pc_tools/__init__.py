"""PC Assistant's tools: everything Claude can check or change on the PC.

Modules, from lowest level to highest:
    common       shared helpers (sizes, PowerShell, registry, admin windows)
    history      change log + undo (each module registers undo handlers for its own changes)
    diagnostics  read-only health checks
    storage      cleanup, largest files, duplicates
    updates      Windows Update and drivers
    security     unwanted software, startup entry checks, browser extensions
    network      connection diagnosis and fixes
    settings     startup programs, power plans, gaming settings, repairs
    scan         the one-click full scan
    catalog      tool schemas for Claude, safety lists, confirmation text
    prompt       model settings and system prompt
    api_key      loading/saving the Anthropic API key
"""

from .api_key import get_api_key, save_api_key
from .catalog import (
    ACTIONS_NEEDING_RESTORE_POINT,
    ACTIONS_REQUIRING_CONFIRMATION,
    TOOL_FUNCTIONS,
    TOOLS,
    describe_action,
    run_tool,
)
from .history import load_history, undo_change
from .prompt import MAX_TOKENS, MODEL, SYSTEM_PROMPT
from .settings import create_restore_point

__all__ = [
    "ACTIONS_NEEDING_RESTORE_POINT", "ACTIONS_REQUIRING_CONFIRMATION", "MAX_TOKENS", "MODEL", "SYSTEM_PROMPT",
    "TOOL_FUNCTIONS", "TOOLS", "create_restore_point", "describe_action", "get_api_key", "load_history",
    "run_tool", "save_api_key", "undo_change",
]
