#!/usr/bin/env python3
"""Kouřová zkouška API: pustí zkušební hub (vlastní dočasné ~/.claude a domov,
náhodný port) a každý endpoint zavolá prázdným GET i POST {}. Selhání = 5xx,
přerušené spojení, déle než 10 s, nebo „/api/… selhalo" v logu zkušebního hubu.
Chytí třeba lokální import, který zastíní modul (UnboundLocalError), nebo
volání funkce, která neexistuje (NameError) — to unit testy nevidí.

    python3 tools/test_api_smoke.py

Endpointy, které by sahaly ven (aktualizace, restart, přihlášení, schránka,
otevírání oken, zápis do trezoru…), se vynechávají.
"""
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VYNECHAT = {
    "restart", "quit", "update", "update-check", "restore", "clipboard", "open-path",
    "soubor-otevrit", "okno-otevrit", "hlas-install", "hlas-rec", "hlas-proud", "hlas-pripravit",
    "hlas-eleven", "connect", "mcp", "claude-ucet", "remote", "pocitac", "prenos",
    "zrcadlo-na-server", "zrcadlo-prevzit", "zrcadlo-nastav", "log-clear", "account", "vault",
    "mcp-pc-nastav", "plugin-soubor", "motiv-soubor", "pluginy", "predplatne", "gemini",
    "clockify", "pomocnici", "setup", "config", "agents", "bypass", "vault-delete",
    "vault-rename", "vault-save", "vault-upload", "upload", "chat-delete", "app-vzhled",
    "logo", "jev", "stopka",
}


def endpointy():
    with open(os.path.join(ROOT, "hub", "server.py"), encoding="utf-8") as fh:
        names = set(re.findall(r'name == "([a-z0-9-]+)"', fh.read()))
    return sorted(names - VYNECHAT)


def main():
    tmp = tempfile.mkdtemp(prefix="hub-api-smoke-")
    cfg, home = os.path.join(tmp, "claude"), os.path.join(tmp, "home")
    os.makedirs(cfg); os.makedirs(home)
    env = {"HOME": home, "USERPROFILE": home, "PATH": os.environ.get("PATH", ""),
           "LANG": "C.UTF-8", "CLAUDE_CONFIG_DIR": cfg, "HUB_TEST": "1"}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    hub = subprocess.Popen([sys.executable, os.path.join(ROOT, "claude-hub.py"), "--no-browser", "--test"],
                           cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        url = ""
        konec = time.time() + 30
        while time.time() < konec and not url:
            line = hub.stdout.readline()
            if not line and hub.poll() is not None:
                break
            m = re.search(r"http://127\.0\.0\.1:\d+/\?t=[\w-]+", line)
            url = m.group(0) if m else ""
        if not url:
            print("zkušební hub nenastartoval")
            return 1
        u = urllib.parse.urlsplit(url)
        token = urllib.parse.parse_qs(u.query)["t"][0]
        spatne = []

        def call(method, name, body):
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
            t0 = time.time()
            try:
                data = None if body is None else json.dumps(body).encode()
                c.request(method, f"/api/{name}?t={token}", body=data,
                          headers={"Content-Type": "application/json",
                                   "Origin": f"http://{u.hostname}:{u.port}"})
                r = c.getresponse(); txt = r.read(4000).decode("utf-8", "replace")
                if r.status >= 500:
                    spatne.append(f"{method} {name}: {r.status} {txt[:160]}")
            except Exception as exc:              # noqa: BLE001 — každé selhání je nález
                spatne.append(f"{method} {name}: {type(exc).__name__} po {time.time() - t0:.1f} s")
            finally:
                c.close()

        names = endpointy()
        for name in names:
            call("GET", name, None)
            call("POST", name, {})
        log = os.path.join(cfg, "hub.log")
        if os.path.isfile(log):
            with open(log, encoding="utf-8", errors="replace") as fh:
                spatne += sorted({re.sub(r"^\S+ \S+\s+", "", l).strip()[:200]
                                  for l in fh if "selhalo" in l})
        if hub.poll() is not None:
            spatne.append("hub po zkoušce neběží")
        print(f"endpointů {len(names)}, nálezů {len(spatne)}")
        for s in spatne:
            print("  ", s)
        return 1 if spatne else 0
    finally:
        hub.terminate()
        try:
            hub.wait(10)
        except subprocess.TimeoutExpired:
            hub.kill()


if __name__ == "__main__":
    sys.exit(main())
