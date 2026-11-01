"""Unwanted software, suspicious startup entries, browser extensions, and removable preinstalled apps."""

import json
import os
import shutil
import subprocess
import tempfile

import psutil

from .common import LOCALAPPDATA, WINDIR, powershell_json
from .diagnostics import installed_programs
from .history import record_change, undo_handler
from .storage import CHROMIUM_BROWSERS

# Lower-case name fragments of programs that are commonly unwanted. These are well-known
# scareware/adware/"optimizers"; matching one is a strong hint, not proof.
_PUP_PATTERNS = {
    "driver updater (unnecessary, often pushy upsells)": [
        "driver booster", "driver easy", "driverpack", "driver support", "driver updater", "drivermax",
        "smart driver", "driver genius", "outbyte driver",
    ],
    "\"PC optimizer\" / registry cleaner (snake oil, often scareware)": [
        "advanced systemcare", "pc optimizer", "reimage", "restoro", "fortect", "mypc utilities",
        "wise care 365", "auslogics boostspeed", "glary utilities", "slimcleaner", "pc cleaner",
        "pc helpsoft", "advanced system optimizer", "winzip system utilities", "one safe pc cleaner",
        "outbyte pc repair", "systweak", "segurazo", "pc accelerate", "speedypc", "registry reviver",
    ],
    "adware / browser hijacker": [
        "wave browser", "webdiscover", "onelaunch", "shift browser", "search protect", "conduit",
        "ask toolbar", "mindspark", "babylon", "pc app store", "mobogenie", "iminent", "bytefence",
        "safe search", "cleanmypc", "yandex", "chromium (unofficial)",
    ],
    "trial antivirus (conflicts with Windows Defender if you didn't buy it)": [
        "mcafee", "norton", "totalav", "avast free", "avg antivirus", "segurazo",
    ],
}


# Microsoft Store apps that come preinstalled and are harmless but removable if unused.
BLOAT_APPS = {
    "Microsoft.BingNews": "Microsoft News",
    "Microsoft.BingWeather": "Weather",
    "Microsoft.MicrosoftSolitaireCollection": "Solitaire Collection (has ads)",
    "Microsoft.GetHelp": "Get Help",
    "Clipchamp.Clipchamp": "Clipchamp video editor",
    "Microsoft.Todos": "Microsoft To Do",
    "MSTeams": "Microsoft Teams (personal)",
    "Microsoft.People": "People",
    "Microsoft.MicrosoftOfficeHub": "Microsoft 365 (Office) promo app",
    "Microsoft.WindowsFeedbackHub": "Feedback Hub",
    "Microsoft.549981C3F5F10": "Cortana",
    "Microsoft.YourPhone": "Phone Link",
    "Microsoft.WindowsMaps": "Maps",
    "Microsoft.MixedReality.Portal": "Mixed Reality Portal",
    "Microsoft.Getstarted": "Tips",
    "Microsoft.PowerAutomateDesktop": "Power Automate",
    "king.com.CandyCrushSaga": "Candy Crush Saga",
    "king.com.CandyCrushSodaSaga": "Candy Crush Soda Saga",
    "SpotifyAB.SpotifyMusic": "Spotify (preinstalled)",
    "Disney.37853FC22B2CE": "Disney+",
    "BytedancePte.Ltd.TikTok": "TikTok",
    "Facebook.Facebook": "Facebook",
    "Facebook.InstagramBeta": "Instagram",
    "AmazonVideo.PrimeVideo": "Prime Video",
}


_SCRIPT_HOSTS = {"powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "cmd.exe", "regsvr32.exe"}


def _exe_from_command(command):
    import re
    cmd = os.path.expandvars((command or "").strip())
    if cmd.startswith('"'):
        path = cmd[1:].split('"', 1)[0]
    else:
        m = re.match(r"^(.+?\.(?:exe|bat|cmd|vbs|js|ps1|com|scr|lnk))\b", cmd, re.IGNORECASE)
        path = m.group(1) if m else cmd.split(" ")[0]
    if path and not os.path.isabs(path):
        found = shutil.which(path) or os.path.join(WINDIR, "System32", path)
        path = found
    return path


def _signatures(paths):
    """Returns {path: (status, signer)} using Windows' Authenticode check."""
    paths = [p for p in set(paths) if p and os.path.isfile(p)]
    if not paths:
        return {}
    quoted = json.dumps(paths).replace("'", "''")
    # Note: in Windows PowerShell 5.1, piping ConvertFrom-Json output passes the whole array as one item,
    # so loop with foreach instead, and cast to [string] so paths serialize as plain strings.
    rows = powershell_json(
        f"$paths = ConvertFrom-Json '{quoted}'; "
        "$out = foreach ($p in $paths) { $s = Get-AuthenticodeSignature -LiteralPath $p; "
        "[pscustomobject]@{ Path = [string]$p; Status = [string]$s.Status; "
        "Signer = if ($s.SignerCertificate) { $s.SignerCertificate.GetNameInfo('SimpleName', $false) } else { $null } } }; "
        "ConvertTo-Json @($out)",
        timeout=120,
    )
    return {r["Path"]: (r.get("Status"), r.get("Signer")) for r in rows}


def _all_startup_entries():
    rows = powershell_json(r"""
$entries = @()
$keys = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run', 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'
$skip = 'PSPath', 'PSParentPath', 'PSChildName', 'PSDrive', 'PSProvider'
foreach ($k in $keys) {
    if (Test-Path $k) {
        foreach ($p in (Get-ItemProperty $k).PSObject.Properties) {
            if ($p.Name -notin $skip) { $entries += [pscustomobject]@{ Name = $p.Name; Command = [string]$p.Value; Where = $k } }
        }
    }
}
$shell = New-Object -ComObject WScript.Shell
foreach ($d in "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp") {
    Get-ChildItem $d -File -ErrorAction SilentlyContinue | Where-Object Name -ne 'desktop.ini' | ForEach-Object {
        $cmd = if ($_.Extension -eq '.lnk') { '"' + $shell.CreateShortcut($_.FullName).TargetPath + '" ' + $shell.CreateShortcut($_.FullName).Arguments } else { '"' + $_.FullName + '"' }
        $entries += [pscustomobject]@{ Name = $_.BaseName; Command = $cmd; Where = 'Startup folder' }
    }
}
if ($entries.Count -eq 0) { '[]' } else { ConvertTo-Json @($entries) }
""")
    return rows


def check_unwanted_software():
    lines = []
    # Include both the short (C:\Users\ABCDEF~1) and long forms of the temp folder
    temp_dirs = [d.lower() for d in (tempfile.gettempdir(), os.path.join(LOCALAPPDATA, "Temp"), os.path.join(WINDIR, "Temp"))]
    downloads = os.path.join(os.path.expanduser("~"), "Downloads").lower()
    program_dirs = [
        os.environ.get(v, "").lower() for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)
    ]

    # 1. Known unwanted programs
    installed = installed_programs()
    found = []
    for name in installed:
        lower = name.lower()
        for reason, patterns in _PUP_PATTERNS.items():
            if any(p in lower for p in patterns):
                found.append(f"  {name}: {reason}")
                break
    lines.append("Known unwanted programs:")
    lines += found or ["  none found"]

    # 2. Preinstalled bloat apps
    try:
        appx = {a["Name"] for a in powershell_json("Get-AppxPackage | Select-Object Name | ConvertTo-Json", timeout=60)}
    except Exception:
        appx = set()
    bloat = [f"  {label} (package {pkg})" for pkg, label in BLOAT_APPS.items() if pkg in appx]
    lines.append("\nPreinstalled apps that are harmless but removable if unused (uninstall_store_app):")
    lines += bloat or ["  none found"]

    # 3. Startup entries and running programs in suspicious places
    entries = _all_startup_entries()
    for e in entries:
        e["Path"] = _exe_from_command(e.get("Command"))
    odd_procs = {}
    for p in psutil.process_iter(["name", "exe"]):
        exe = (p.info.get("exe") or "")
        if exe and any(exe.lower().startswith(d) for d in temp_dirs + [downloads]):
            odd_procs[exe] = p.info["name"]
    sigs = _signatures([e["Path"] for e in entries] + list(odd_procs))

    flagged, fine = [], []
    for e in entries:
        path = e["Path"]
        reasons = []
        if not os.path.exists(path):
            reasons.append("points to a file that no longer exists (a harmless leftover, safe to remove)")
        else:
            if any(path.lower().startswith(d) for d in temp_dirs):
                reasons.append("runs from a temporary folder, which legitimate programs rarely do")
            if os.path.basename(path).lower() in _SCRIPT_HOSTS:
                reasons.append(f"runs a script or command: {e['Command'][:200]}")
            else:
                status, signer = sigs.get(path, (None, None))
                if status and status != "Valid":
                    reason = f"is not digitally signed by its publisher (signature: {status})"
                    if any(path.lower().startswith(d) for d in program_dirs):
                        reason += ". It's in Program Files, which only installers with admin rights can write to, so this is lower risk; some legitimate apps just aren't signed"
                    reasons.append(reason)
        if reasons:
            flagged.append(f"  {e['Name']} -> {path}\n    " + "\n    ".join(reasons))
        else:
            signer = sigs.get(path, (None, None))[1]
            fine.append(f"{e['Name']}" + (f" (signed by {signer})" if signer else ""))
    lines.append("\nStartup entries needing a closer look:")
    lines += flagged or ["  none, every startup entry is signed by a known publisher"]
    if fine:
        lines.append("  Verified OK: " + ", ".join(fine))

    lines.append("\nRunning programs from Temp or Downloads folders:")
    if odd_procs:
        for exe, name in odd_procs.items():
            status, signer = sigs.get(exe, (None, None))
            sig = f"signed by {signer}" if status == "Valid" else "NOT signed"
            lines.append(f"  {name} -> {exe} ({sig})")
    else:
        lines.append("  none")

    lines.append(
        "\nNote: a flag means 'worth checking', not 'definitely malware'. If anything looks suspicious, "
        "run a Microsoft Defender scan (open_page 'defender')."
    )
    return "\n".join(lines)


def list_browser_extensions():
    import glob

    def chromium_name(manifest_path, manifest):
        name = manifest.get("name", "")
        if name.startswith("__MSG_") and name.endswith("__"):
            msg_key = name[6:-2].lower()
            locale = manifest.get("default_locale", "en")
            base = os.path.dirname(manifest_path)
            for loc in (locale, "en", "en_US"):
                try:
                    with open(os.path.join(base, "_locales", loc, "messages.json"), encoding="utf-8-sig") as f:
                        messages = {k.lower(): v for k, v in json.load(f).items()}
                    if msg_key in messages:
                        return messages[msg_key].get("message", name)
                except (OSError, ValueError):
                    continue
        return name

    lines = []
    browsers = {"Edge": CHROMIUM_BROWSERS[0], "Chrome": CHROMIUM_BROWSERS[1], "Brave": CHROMIUM_BROWSERS[2]}
    for browser, user_data in browsers.items():
        names = set()
        for manifest_path in glob.glob(os.path.join(glob.escape(user_data), "*", "Extensions", "*", "*", "manifest.json")):
            try:
                with open(manifest_path, encoding="utf-8-sig") as f:
                    manifest = json.load(f)
            except (OSError, ValueError):
                continue
            name = chromium_name(manifest_path, manifest)
            ext_id = manifest_path.split(os.sep)[-3]
            names.add(f"{name} (id {ext_id})")
        if os.path.isdir(user_data):
            lines.append(f"{browser}:")
            lines += [f"  {n}" for n in sorted(names)] or ["  no extensions installed"]

    for ext_json in glob.glob(os.path.join(glob.escape(os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")), "*", "extensions.json")):
        try:
            with open(ext_json, encoding="utf-8") as f:
                addons = json.load(f).get("addons", [])
        except (OSError, ValueError):
            continue
        lines.append("Firefox:")
        user_addons = [a for a in addons if a.get("type") == "extension" and a.get("location") == "app-profile"]
        for a in user_addons:
            name = (a.get("defaultLocale") or {}).get("name") or a.get("id")
            lines.append(f"  {name}" + ("" if a.get("active") else " (disabled)"))
        if not user_addons:
            lines.append("  no extensions installed")

    if not lines:
        return "No supported browsers (Edge, Chrome, Brave, Firefox) found."
    lines.append(
        "\nExtensions can read the pages you visit. Remove any the user doesn't recognise or use, from the "
        "browser's Extensions page (edge://extensions or chrome://extensions)."
    )
    return "\n".join(lines)


def uninstall_store_app(package_name):
    if package_name not in BLOAT_APPS:
        return (
            f"'{package_name}' isn't on the list of safely removable preinstalled apps. "
            "Other apps can be uninstalled from Settings > Apps > Installed apps."
        )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-AppxPackage -Name '{package_name}' | Remove-AppxPackage -ErrorAction Stop"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        return f"Could not uninstall {BLOAT_APPS[package_name]}: {result.stderr.strip()[:300]}"
    record_change(
        f"Uninstalled {BLOAT_APPS[package_name]}",
        {"type": "store_app", "label": BLOAT_APPS[package_name]},
    )
    return f"Uninstalled {BLOAT_APPS[package_name]}. It can be reinstalled from the Microsoft Store any time."


@undo_handler("store_app")
def _undo_store_app(undo):
    # Reinstalling needs the Store, so take the user to the app's Store page; it isn't
    # marked as undone because the user still has to click Install.
    from urllib.parse import quote
    os.startfile(f"ms-windows-store://search/?query={quote(undo['label'])}")
    return f"Opened the Microsoft Store so the user can reinstall {undo['label']} (click Get/Install).", False
