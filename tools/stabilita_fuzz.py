#!/usr/bin/env python3
# Použití: spustit zkušební hub (CLAUDE_CONFIG_DIR=<tmp> HUB_TEST=1 python3 claude-hub.py --no-browser --test) a pak
#   python3 tools/stabilita_fuzz.py <url z výpisu> <pid hubu> <soubor s logem>
"""Shazovací test nových endpointů hubu: zkomolené, obří a souběžné požadavky.
Selhání = 5xx, zaseknutí (>8 s), přerušené spojení, hub po testu nežije."""
import json, os, random, sys, threading, time, urllib.parse, http.client, collections

URL = sys.argv[1]; PID = int(sys.argv[2]); LOG = sys.argv[3]
u = urllib.parse.urlsplit(URL); HOST, PORT = u.hostname, u.port
TOKEN = urllib.parse.parse_qs(u.query)["t"][0]
random.seed(7)

def rss_threads(pid):
    try:
        st = open(f"/proc/{pid}/status").read()
        rss = int([l for l in st.splitlines() if l.startswith("VmRSS")][0].split()[1]) // 1024
        thr = int([l for l in st.splitlines() if l.startswith("Threads")][0].split()[1])
        return rss, thr
    except Exception:
        return None, None

def req(method, path, body=None, ctype="application/json", raw=False, timeout=8):
    c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    t0 = time.time()
    try:
        sep = "&" if "?" in path else "?"
        data = body if raw else (None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8", "surrogatepass"))
        h = {"Content-Type": ctype, "Origin": f"http://{HOST}:{PORT}"}
        c.request(method, f"/api/{path}{sep}t={TOKEN}", body=data, headers=h)
        r = c.getresponse(); r.read(2_000_000)
        return r.status, time.time() - t0
    except Exception as e:
        return type(e).__name__, time.time() - t0
    finally:
        c.close()

UUID = "00000000-0000-4000-8000-000000000000"
BAD = ["", " ", "x" * 5000, "../../etc/passwd", "..\\..\\x", "%00", "\x00", "‮\ud800", "🙂" * 300, "a" * 1_000_000,
       None, 0, -1, 1e999 if False else 10**30, True, [], {}, [[]], {"a": {"b": [None]}}, UUID, "`id`", "$(id)", "'; DROP--"]
deep = "[" * 20000 + "]" * 20000

ENDPOINTS = [
    ("POST", "chat-hledej", lambda v: {"q": v, "nastroje": v}),
    ("POST", "chat-hledej-stav", lambda v: {"job": v}),
    ("POST", "chat-hledej-zrus", lambda v: v),
    ("GET", "chat-export?id=", lambda v: None),
    ("POST", "chat-vetev", lambda v: {"chat": v, "uuid": v, "id": v}),
    ("POST", "rewind-prehled", lambda v: {"chat": v, "uuid": v}),
    ("POST", "rewind-proved", lambda v: {"chat": v, "uuid": v, "token": v, "smazat": v}),
    ("POST", "rewind-zpet", lambda v: {"zaloha": v}),
    ("GET", "stav-radek?id=", lambda v: None),
    ("GET", "tab-context?id=", lambda v: None),
    ("GET", "soubory-hledani?id=1&q=", lambda v: None),
    ("POST", "fronta", lambda v: {"tab": v, "id": v, "text": v}),
    ("POST", "fronta-uprav", lambda v: {"tab": v, "id": v, "text": v}),
    ("POST", "fronta-zrus", lambda v: {"tab": v, "id": v}),
    ("POST", "fronta-znovu", lambda v: {"tab": v, "id": v}),
    ("POST", "fronta-ihned", lambda v: {"tab": v, "id": v}),
    ("POST", "fronta-pauza", lambda v: {"tab": v, "on": v}),
]
jobs = []
for m, ep, mk in ENDPOINTS:
    for v in BAD:
        if m == "GET":
            qs = urllib.parse.quote(str(v)[:3000].encode("utf-8", "surrogatepass"), safe="") if v is not None else ""
            jobs.append((m, ep + qs, None, "application/json", False))
        else:
            try: jobs.append((m, ep, mk(v), "application/json", False))
            except Exception: pass
    jobs += [(m, ep, b"{not json", "application/json", True), (m, ep, b"", "application/json", True),
             (m, ep, b"\xff\xfe\x00bad", "text/plain", True), (m, ep, deep.encode(), "application/json", True),
             (m, ep, b"[" + b"1," * 200000 + b"1]", "application/json", True)]

rss0, thr0 = rss_threads(PID); print(f"před: RSS {rss0} MB, vláken {thr0}, požadavků {len(jobs)}")
res = collections.Counter(); bad = []; slow = []; lock = threading.Lock()
def worker(chunk):
    for m, ep, body, ct, raw in chunk:
        st, dt = req(m, ep, body, ct, raw)
        with lock:
            res[st] += 1
            if not isinstance(st, int) or st >= 500: bad.append((m, ep[:40], st))
            if dt > 5: slow.append((ep[:40], round(dt, 1)))
t0 = time.time()
random.shuffle(jobs)
th = [threading.Thread(target=worker, args=(jobs[i::24],)) for i in range(24)]
[t.start() for t in th]; [t.join() for t in th]
print(f"hotovo za {time.time()-t0:.1f} s; stavy: {dict(res)}")
print("5xx / chyby spojení:", bad[:15], "(celkem", len(bad), ")"); print("pomalé (>5 s):", slow[:10], "(celkem", len(slow), ")")

# ── syrové útoky na úrovni socketu ──
import socket
def raw(data, wait=1.0, send_more=None):
    try:
        so = socket.create_connection((HOST, PORT), timeout=6)
        so.sendall(data)
        if send_more: time.sleep(0.2); so.sendall(send_more)
        so.settimeout(wait)
        try: out = so.recv(300)
        except Exception: out = b""
        so.close(); return out.split(b"\r\n")[0].decode("latin1") or "(bez odpovědi)"
    except Exception as e:
        return type(e).__name__
H = f"Host: {HOST}:{PORT}\r\nOrigin: http://{HOST}:{PORT}\r\n"
Q = f"/api/fronta?t={TOKEN}"
rawtests = {
  "záporná délka": f"POST {Q} HTTP/1.1\r\n{H}Content-Length: -5\r\n\r\n".encode(),
  "nečíselná délka": f"POST {Q} HTTP/1.1\r\n{H}Content-Length: abc\r\n\r\n".encode(),
  "obří délka bez těla": f"POST {Q} HTTP/1.1\r\n{H}Content-Length: 99999999999\r\n\r\n".encode(),
  "délka 100 MB bez těla": f"POST {Q} HTTP/1.1\r\n{H}Content-Length: 104857600\r\n\r\n".encode(),
  "půl těla a zavřít": f"POST {Q} HTTP/1.1\r\n{H}Content-Length: 500\r\n\r\n{{\"a\":".encode(),
  "dvě Content-Length": f"POST {Q} HTTP/1.1\r\n{H}Content-Length: 2\r\nContent-Length: 9999\r\n\r\n{{}}".encode(),
  "chunked": f"POST {Q} HTTP/1.1\r\n{H}Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n".encode(),
  "smetí místo HTTP": b"\x00\x01\x02\xff" * 100 + b"\r\n\r\n",
  "URI 100 kB": f"GET /api/state?t={TOKEN}&x={'a'*100000} HTTP/1.1\r\n{H}\r\n".encode(),
  "hlavičky 5000×": (f"GET /api/state?t={TOKEN} HTTP/1.1\r\n{H}" + "X-A: b\r\n" * 5000 + "\r\n").encode(),
  "WebSocket bez klíče": f"GET /ws?t={TOKEN} HTTP/1.1\r\n{H}Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n".encode(),
}
print("syrové útoky:")
rawbad = []
for k, v in rawtests.items():
    r = raw(v); print("  %-24s -> %s" % (k, r))
    if r.startswith("HTTP/1.") and r.split()[1].startswith("5"): rawbad.append(k)
# 300 souběžných spojení, která jen drží
socks = []
for i in range(300):
    try: so = socket.create_connection((HOST, PORT), timeout=3); so.sendall(b"GET /api/state?t=x HTTP/1.1\r\nHost: x\r\n"); socks.append(so)
    except Exception as e: break
print("  držených spojení:", len(socks))
st, dt = req("GET", "state"); print(f"  /api/state za držení: {st} za {dt:.2f} s")
for so in socks: so.close()
if not (st == 200 and dt < 4) or rawbad: bad.append(("raw", rawbad, st))

time.sleep(3)
rss1, thr1 = rss_threads(PID); print(f"po: RSS {rss1} MB (+{(rss1 or 0)-(rss0 or 0)}), vláken {thr1} (+{(thr1 or 0)-(thr0 or 0)})")
st, _ = req("GET", "state"); print("hub žije, /api/state =", st)
log = "".join(open(f, errors="replace").read() for f in LOG.split(",") if os.path.exists(f))
tb = log.count("Traceback") + log.count("selhalo"); print("Traceback v logu:", tb)
sys.exit(0 if (not bad and not slow and st == 200 and tb == 0) else 1)
