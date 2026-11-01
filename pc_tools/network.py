"""Network diagnosis and fixes."""

import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from .common import powershell_json, run_elevated
from .history import record_change


def _ping(host, count=10):
    import re
    result = subprocess.run(["ping", "-n", str(count), "-w", "1000", host], capture_output=True, text=True, timeout=count * 2 + 10)
    replies = [line for line in result.stdout.splitlines() if "TTL=" in line.upper()]
    times = [int(m.group(1)) for line in replies for m in [re.search(r"[=<](\d+)\s*ms", line)] if m]
    loss = 100 * (count - len(replies)) / count
    avg = sum(times) / len(times) if times else None
    jitter = (max(times) - min(times)) if times else None
    return loss, avg, jitter


def get_network_status():
    import re
    lines = []
    try:
        adapters = powershell_json(
            "Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name, InterfaceDescription, LinkSpeed, "
            "@{n='Wireless'; e={ $_.PhysicalMediaType -match '802.11' -or $_.InterfaceDescription -match 'Wi-?Fi|Wireless' }} | ConvertTo-Json",
            timeout=30,
        )
    except RuntimeError:  # no output means no adapter is up
        adapters = []
    if not adapters:
        return "No network adapter is connected. Check the cable or Wi-Fi connection."
    lines.append("Connected adapters:")
    for a in adapters:
        kind = "Wi-Fi" if a.get("Wireless") else "Wired"
        lines.append(f"  {a.get('Name')} ({kind}, {a.get('LinkSpeed')}): {a.get('InterfaceDescription')}")
        if a.get("Wireless"):
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=15).stdout
            m = re.search(r"(\d+)%", out)
            if m:
                signal = int(m.group(1))
                note = " (WEAK: move closer to the router or use a cable)" if signal < 50 else ""
                lines.append(f"    Wi-Fi signal: {signal}%{note}")

    try:
        gateway = powershell_json(
            "Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | Select-Object -First 1 NextHop | ConvertTo-Json",
            timeout=20,
        )[0]["NextHop"]
    except Exception:
        gateway = None

    targets = {"Internet (1.1.1.1)": "1.1.1.1"}
    if gateway:
        targets = {f"Router ({gateway})": gateway, **targets}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = {label: pool.submit(_ping, host) for label, host in targets.items()}
    lines.append("\nConnection quality (10 pings each):")
    for label, future in results.items():
        loss, avg, jitter = future.result()
        if avg is None:
            lines.append(f"  {label}: NO RESPONSE")
            continue
        flags = []
        if loss > 0:
            flags.append(f"{loss:.0f}% packet loss")
        if "Router" in label and avg > 10:
            flags.append("slow for a local router, suggests Wi-Fi interference or a router problem")
        if "Internet" in label and avg > 100:
            flags.append("high latency")
        if jitter and jitter > 50:
            flags.append("unstable (big swings in ping)")
        flag_text = f" - {'; '.join(flags)}" if flags else " - good"
        lines.append(f"  {label}: average {avg:.0f} ms, {loss:.0f}% loss, variation {jitter} ms{flag_text}")

    dns_times = []
    for host in ("www.microsoft.com", "www.google.com", "www.wikipedia.org"):
        start = time.time()
        try:
            socket.getaddrinfo(host, 443)
            dns_times.append((time.time() - start) * 1000)
        except OSError:
            dns_times.append(None)
    failed = dns_times.count(None)
    ok_times = [t for t in dns_times if t is not None]
    if failed == len(dns_times):
        lines.append("\nDNS (looking up website names): FAILING - websites can't be found. Flushing DNS or resetting the network may help.")
    else:
        lines.append(f"\nDNS (looking up website names): working, average {sum(ok_times) / len(ok_times):.0f} ms" + (f", {failed} lookup(s) failed" if failed else ""))
    try:
        dns = powershell_json(
            "Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object ServerAddresses | "
            "Select-Object InterfaceAlias, @{n='Servers'; e={ $_.ServerAddresses -join ', ' }} | ConvertTo-Json",
            timeout=20,
        )
        for d in dns:
            lines.append(f"  DNS servers on {d['InterfaceAlias']}: {d['Servers']}")
    except Exception:
        pass
    lines.append("\n(This measures the connection, not download speed. For speed, the user can visit a site like speedtest.net.)")
    return "\n".join(lines)


def flush_dns():
    result = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        # Newer Windows builds require admin for this; retry in an elevated window
        ok, error = run_elevated("ipconfig /flushdns", wait=True)
        if not ok:
            return f"Could not flush DNS: {error}."
    record_change("Flushed the DNS cache")
    return "Flushed the DNS cache. Websites will be looked up fresh from now on."


def reset_network():
    script = (
        "Write-Host 'Resetting network settings...' -ForegroundColor Cyan; "
        "netsh winsock reset; netsh int ip reset; ipconfig /flushdns; "
        "Write-Host 'Done. Restart your PC to finish.' -ForegroundColor Green; Start-Sleep 3"
    )
    ok, error = run_elevated(script, wait=True)
    if not ok:
        return f"The network reset did not run: {error}."
    record_change("Reset network settings (Winsock and TCP/IP)")
    return (
        "Reset the network stack (Winsock and TCP/IP settings) and flushed DNS. The user must RESTART the PC "
        "for this to take effect. Saved Wi-Fi passwords and VPN settings are kept."
    )
