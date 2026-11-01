"""Storage: measuring and cleaning safe-to-delete folders, and finding large or duplicate files."""

import heapq
import os
import shutil
import tempfile
import time
from collections import defaultdict
from datetime import datetime

import psutil

from .common import LOCALAPPDATA, SYSTEM_DRIVE, WINDIR, folder_size, format_size, is_admin, run_elevated
from .history import record_change


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
        lines.append(f"  {format_size(size)}  {path}  (last modified {modified})")
    if timed_out:
        lines.append("(Stopped after 60 seconds, so this may not include every file.)")
    return "\n".join(lines)


CHROMIUM_BROWSERS = [
    rf"{LOCALAPPDATA}\Microsoft\Edge\User Data",
    rf"{LOCALAPPDATA}\Google\Chrome\User Data",
    rf"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\User Data",
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
        return [rf"{WINDIR}\Temp"]
    if category == "windows_update_cache":
        return [rf"{WINDIR}\SoftwareDistribution\Download"]
    if category == "crash_dumps":
        return [rf"{LOCALAPPDATA}\CrashDumps"]
    if category == "shader_cache":
        return [
            rf"{LOCALAPPDATA}\NVIDIA\DXCache", rf"{LOCALAPPDATA}\NVIDIA\GLCache",
            rf"{LOCALAPPDATA}\AMD\DxCache", rf"{LOCALAPPDATA}\AMD\GLCache", rf"{LOCALAPPDATA}\D3DSCache",
        ]
    if category == "browser_cache":
        folders = []
        for browser in CHROMIUM_BROWSERS:
            for profile in glob.glob(os.path.join(glob.escape(browser), "*")):
                for sub in ("Cache", "Code Cache", "GPUCache"):
                    folders.append(os.path.join(profile, sub))
        folders += glob.glob(os.path.join(glob.escape(rf"{LOCALAPPDATA}\Mozilla\Firefox\Profiles"), "*", "cache2"))
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
        total += folder_size(folder)
    return total, readable


def _delete_folder_contents(folder):
    freed = skipped = 0
    if not os.path.isdir(folder):
        return 0, 0
    for entry in os.scandir(folder):
        try:
            if entry.is_dir(follow_symlinks=False):
                size = folder_size(entry.path)
                shutil.rmtree(entry.path, ignore_errors=True)
                freed += size - (folder_size(entry.path) if os.path.exists(entry.path) else 0)
                if os.path.exists(entry.path):
                    skipped += 1
            else:
                size = entry.stat(follow_symlinks=False).st_size
                os.remove(entry.path)
                freed += size
        except OSError:
            skipped += 1
    return freed, skipped


def get_cleanup_report():
    lines = ["Space that can be safely freed (nothing has been deleted):"]
    total = 0
    for key, cat in CLEANUP_CATEGORIES.items():
        size, readable = _category_size(key)
        if not readable:
            amount = "size unknown (needs admin to check)"
        else:
            amount = format_size(size)
            total += size
        lines.append(f"  [{key}] {cat['label']}: {amount}\n      {cat['note']}")
    lines.append(f"\nTotal (excluding unknown): about {format_size(total)}")
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
                lines.append(f"  {label}: emptied, freed {format_size(size)}")
            else:
                lines.append(f"  {label}: could not be emptied")
            continue
        if CLEANUP_CATEGORIES[key]["admin"] and not is_admin():
            admin_needed.append(key)
            continue
        freed = skipped = 0
        for folder in _category_folders(key):
            f, s = _delete_folder_contents(folder)
            freed += f
            skipped += s
        total_freed += freed
        extra = f" ({skipped} items skipped because they're in use)" if skipped else ""
        lines.append(f"  {label}: freed {format_size(freed)}{extra}")

    if admin_needed:
        free_before = psutil.disk_usage(SYSTEM_DRIVE).free
        folders = [f for key in admin_needed for f in _category_folders(key)]
        script = "Write-Host 'PC Assistant is cleaning up system files...' -ForegroundColor Cyan; " + "; ".join(
            f"Get-ChildItem -LiteralPath '{f}' -Force -ErrorAction SilentlyContinue | "
            "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
            for f in folders
        )
        ok, error = run_elevated(script, wait=True)
        labels = ", ".join(CLEANUP_CATEGORIES[k]["label"] for k in admin_needed)
        if ok:
            freed = max(0, psutil.disk_usage(SYSTEM_DRIVE).free - free_before)
            total_freed += freed
            lines.append(f"  {labels}: cleaned with administrator rights, freed about {format_size(freed)}")
        else:
            lines.append(f"  {labels}: skipped ({error})")

    labels = ", ".join(CLEANUP_CATEGORIES[k]["label"] for k in categories)
    record_change(f"Cleaned up {labels}, freed about {format_size(total_freed)}")
    return "Cleanup finished:\n" + "\n".join(lines) + f"\nTotal freed: about {format_size(total_freed)}"


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
    lines = [f"Found {len(groups)} sets of identical files, wasting about {format_size(wasted)}:"]
    for size, paths in groups[:20]:
        lines.append(f"  {format_size(size)} each:")
        lines += [f"    {p}" for p in paths]
    if len(groups) > 20:
        lines.append(f"  ...and {len(groups) - 20} more sets")
    if time.time() > deadline:
        lines.append("(Stopped after 90 seconds, so this may not include every file.)")
    return "\n".join(lines)
