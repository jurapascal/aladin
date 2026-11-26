"""
Reaching the hub from a phone, over Tailscale.

The desktop window keeps talking to a random loopback port, exactly as before.
Remote access adds a *second* listener on a fixed port with its own long-lived
token, so turning the phone on or off never disturbs a running session.

Two ways to get the traffic in, tried in this order:

  1. `tailscale serve` — tailscaled terminates TLS on the tailnet and proxies to
     our loopback port. Gives a real https://<stroj>.<tailnet>.ts.net cert, which
     is what Android needs before it will install the page as an app.
  2. binding straight to the 100.x Tailscale address — no certificate, plain
     http, but it needs no extra permission and works the moment Tailscale is up.

Nothing is ever bound to 0.0.0.0: outside the tailnet there is no listener at
all, and inside it every request still has to carry the token.
"""
import json
import os
import secrets
import shutil
import subprocess

from . import core

DEFAULT_PORT = 8760
TOKEN_KEY = "remote_token"
CANDIDATES = [
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
]


def binary():
    """Path to the tailscale CLI, or '' when it isn't installed."""
    found = shutil.which("tailscale")
    if found:
        return found
    return next((p for p in CANDIDATES if os.path.isfile(p)), "")


def _run(args, timeout=10):
    """Run the tailscale CLI. Returns (ok, stdout, stderr) and never raises."""
    exe = binary()
    if not exe:
        return False, "", "Tailscale není nainstalovaný."
    try:
        proc = subprocess.run([exe] + args, capture_output=True, text=True,
                              timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                              if core.IS_WINDOWS else 0)
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return False, "", str(exc)


def tailscale_state():
    """What Tailscale is doing right now: installed / logged in / hostname."""
    info = {"installed": bool(binary()), "running": False, "host": "",
            "ip": "", "state": "", "https": False}
    if not info["installed"]:
        return info
    ok, out, _ = _run(["status", "--json"], timeout=8)
    if not ok or not out:
        return info
    try:
        data = json.loads(out)
    except Exception:
        return info
    info["state"] = data.get("BackendState", "")
    info["running"] = info["state"] == "Running"
    self_node = data.get("Self") or {}
    # DNSName arrives fully qualified with a trailing dot.
    info["host"] = (self_node.get("DNSName") or "").rstrip(".")
    ips = self_node.get("TailscaleIPs") or []
    info["ip"] = next((ip for ip in ips if ":" not in ip), "")
    # MagicDNS off means no ts.net name, so no certificate either.
    info["https"] = bool(info["host"]) and data.get("MagicDNSSuffix", "") != ""
    return info


# ── the long-lived phone token ───────────────────────────────────────────────
def token(create=True):
    """The token the phone keeps. Survives restarts; rotating it logs it out."""
    existing = core.CONFIG.get(TOKEN_KEY) or ""
    if existing or not create:
        return existing
    fresh = secrets.token_urlsafe(24)
    core.save_config({TOKEN_KEY: fresh})
    return fresh


def rotate():
    fresh = secrets.token_urlsafe(24)
    core.save_config({TOKEN_KEY: fresh})
    return fresh


def port():
    try:
        value = int(core.CONFIG.get("remote_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return value if 1024 <= value <= 65535 else DEFAULT_PORT


# ── tailscale serve ──────────────────────────────────────────────────────────
def serve_start(local_port):
    """Point tailscaled's https listener at our port. Returns (ok, message)."""
    ok, _, err = _run(["serve", "--bg", "--https=443",
                       f"http://127.0.0.1:{local_port}"], timeout=25)
    if ok:
        return True, ""
    low = err.lower()
    if "denied" in low or "permission" in low or "operator" in low:
        return False, ("Tailscale nepustí `serve` pod tvým účtem. Jednou spusť: "
                       f"sudo tailscale set --operator=$USER")
    if "https" in low or "cert" in low or "magicdns" in low:
        return False, ("Tailnet nemá zapnuté HTTPS certifikáty — zapni je v "
                       "admin konzoli (DNS → HTTPS Certificates).")
    return False, err or "tailscale serve selhal."


def serve_stop():
    _run(["serve", "--https=443", "off"], timeout=15)


def url(mode, host, ip, local_port, tok):
    if mode == "serve" and host:
        base = f"https://{host}"
    elif ip:
        base = f"http://{ip}:{local_port}"
    else:
        return ""
    return f"{base}/?t={tok}"
