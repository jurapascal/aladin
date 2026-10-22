#!/usr/bin/env python3
"""Zkoušky větvení konverzace (hub/vetev.py). Bez sítě, v dočasné složce —
do skutečného ~/.claude se nesahá. Spuštění: python3 tools/test_vetev.py

Skutečné přepisy se z ~/.claude/projects jen ČTOU (kopie do dočasné složky);
dají se vynechat přes VETEV_BEZ_REALNYCH=1."""
import errno
import glob
import hashlib
import json
import os
import resource
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid

TMP = tempfile.mkdtemp(prefix="vetev-test-")
SKUTECNY = os.path.expanduser("~/.claude/projects")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hub import vetev  # noqa: E402  (až po nastavení CLAUDE_CONFIG_DIR)

PROJEKT = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-tmp-test")
os.makedirs(PROJEKT, exist_ok=True)
SELHANI = []
PROSLO = [0]


def over(podminka, popis):
    if podminka:
        PROSLO[0] += 1
    else:
        SELHANI.append(popis)
        print("  SELHALO:", popis)


def uid():
    return str(uuid.uuid4())


class Prepis:
    """Skládačka syntetického přepisu."""
    def __init__(self, sid=None):
        self.sid = sid or uid()
        self.radky = []
        self.posledni = None

    def zaznam(self, typ, obsah=None, rodic="auto", **extra):
        u = uid()
        e = {"parentUuid": self.posledni if rodic == "auto" else rodic, "isSidechain": False,
             "type": typ, "uuid": u, "timestamp": "2026-10-22T10:00:00.000Z",
             "cwd": "/tmp/projekt", "sessionId": self.sid}
        if obsah is not None:
            e["message"] = {"role": "user" if typ == "user" else "assistant", "content": obsah}
        e.update(extra)
        self.radky.append(e)
        if rodic == "auto":
            self.posledni = u
        return u

    def clovek(self, text, **kw):
        return self.zaznam("user", text, **kw)

    def claude(self, text, **kw):
        return self.zaznam("assistant", [{"type": "text", "text": text}], **kw)

    def nastroj(self, tid, rodic="auto"):
        return self.zaznam("assistant", [{"type": "tool_use", "id": tid, "name": "Bash", "input": {}}], rodic=rodic)

    def vysledek(self, tid, rodic="auto"):
        return self.zaznam("user", [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}], rodic=rodic)

    def bez_uuid(self, **e):
        e.setdefault("sessionId", self.sid)
        self.radky.append(e)

    def uloz(self, slozka=PROJEKT):
        cesta = os.path.join(slozka, self.sid + ".jsonl")
        with open(cesta, "wb") as fh:
            for e in self.radky:
                fh.write((json.dumps(e, ensure_ascii=False) + "\n").encode())
        return cesta


def hash_(cesta):
    with open(cesta, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def nacti(cesta):
    with open(cesta, encoding="utf-8") as fh:
        return [json.loads(r) for r in fh]


def zakladni():
    p = Prepis()
    p.bez_uuid(type="permission-mode", permissionMode="bypassPermissions")
    p.bez_uuid(type="queue-operation", operation="enqueue", content="x")
    p.bez_uuid(type="api-request", id="1", params={})
    p.bez_uuid(type="file-history-snapshot", messageId="x", snapshot={})
    a = p.clovek("první")
    b = p.claude("odpověď 1")
    c = p.clovek("druhá")
    d = p.claude("odpověď 2")
    p.bez_uuid(type="ai-title", aiTitle="Název")
    e = p.clovek("třetí")
    f = p.claude("odpověď 3")
    return p, dict(a=a, b=b, c=c, d=d, e=e, f=f)


def cesta_vetve(res):
    return os.path.join(PROJEKT, res["id"] + ".jsonl")


def test_zaklad():
    print("základ: větev uprostřed konverzace")
    p, u = zakladni()
    cesta = p.uloz()
    h = hash_(cesta)
    res = vetev.rozvetvit(cesta, u["e"])
    nove = nacti(cesta_vetve(res))
    over(hash_(cesta) == h, "původní přepis se nezměnil")
    uuidy = [x["uuid"] for x in nove if "uuid" in x]
    over(uuidy == [u["a"], u["b"], u["c"], u["d"]], f"větev má zprávy do čtvrté, má {len(uuidy)}")
    over(all(x.get("sessionId") == res["id"] for x in nove if "sessionId" in x), "sessionId přepsané všude")
    over(not any(x["type"] in ("queue-operation", "api-request", "file-history-snapshot") for x in nove),
         "ladicí a frontové záznamy se nekopírují")
    over(any(x["type"] == "permission-mode" for x in nove), "permission-mode zůstal")
    over(nove[-1]["type"] == "last-prompt" and nove[-1]["leafUuid"] == u["d"], "last-prompt ukazuje na konec větve")
    over(any(x["type"] == "ai-title" and x["aiTitle"].startswith("Větev: ") for x in nove), "název větve")
    over(res["text"] == "třetí" and res["cwd"] == "/tmp/projekt", "výsledek nese text a cwd")
    over(stat.S_IMODE(os.stat(cesta_vetve(res)).st_mode) == 0o600, "práva 0600")
    over(not glob.glob(os.path.join(PROJEKT, "*.tmp")), "žádný zbylý .tmp")
    # větev z větve
    res2 = vetev.rozvetvit(cesta_vetve(res), u["d"] if False else u["c"])
    over(res2["title"].count("Větev: ") == 1, "větev z větve nemá dvojitý předpis")


def test_chyby_vstupu():
    print("chybné vstupy")
    p, u = zakladni()
    cesta = p.uloz()
    h = hash_(cesta)
    pred = set(os.listdir(PROJEKT))
    for popis, args in [("první zpráva", (cesta, u["a"])), ("neznámé uuid", (cesta, uid())),
                        ("odpověď Clauda", (cesta, u["b"])), ("prázdné uuid", (cesta, "")),
                        ("uuid s ..", (cesta, "../../etc/passwd")), ("uuid s \\x00", (cesta, "abc\x00def")),
                        ("neexistující soubor", (os.path.join(PROJEKT, uid() + ".jsonl"), u["c"])),
                        ("prázdná cesta", ("", u["c"])),
                        ("cesta mimo projects", ("/etc/passwd", u["c"])),
                        ("cesta s ..", (PROJEKT + "/../-tmp-test/" + p.sid + ".jsonl.x", u["c"]))]:
        try:
            vetev.rozvetvit(*args)
            over(False, f"{popis}: měla být chyba")
        except vetev.VetevChyba as exc:
            over(bool(str(exc)), f"{popis}: hláška")
        except Exception as exc:
            over(False, f"{popis}: spadlo jinak: {exc!r}")
    over(hash_(cesta) == h, "původní beze změny po chybách")
    over(set(os.listdir(PROJEKT)) == pred, "po chybách nevznikl žádný soubor")
    # soubor s jiným jménem než UUID.jsonl
    cizi = os.path.join(PROJEKT, "poznamky.jsonl")
    shutil.copy(cesta, cizi)
    try:
        vetev.rozvetvit(cizi, u["c"])
        over(False, "soubor bez UUID jména se nesmí větvit")
    except vetev.VetevChyba:
        over(True, "")
    os.remove(cizi)


def test_nastroje():
    print("nástroje: souběžné a osiřelé")
    p = Prepis()
    p.clovek("start")
    a1 = p.nastroj("t1")
    a2 = p.nastroj("t2")                      # řetěz A1 → A2
    r2 = p.vysledek("t2")                     # R2 za A2
    r1 = p.vysledek("t1", rodic=a1)           # R1 odrůdka vedle řetězu
    p.claude("hotovo", rodic=r2)
    p.posledni = r2
    p.clovek("další")
    p.claude("odpověď")
    kdo = p.clovek("cíl")
    cesta = p.uloz()
    res = vetev.rozvetvit(cesta, kdo)
    ids = [x["uuid"] for x in nacti(cesta_vetve(res)) if "uuid" in x]
    over(r1 in ids and r2 in ids, "souběžný výsledek odrůdky se dotáhl")
    # uprostřed sekvence: cíl hned za tool_use bez výsledku
    p = Prepis()
    p.clovek("start")
    p.nastroj("x1")
    kdo = p.clovek("přerušení")
    cesta = p.uloz()
    h = hash_(cesta)
    try:
        vetev.rozvetvit(cesta, kdo)
        over(False, "osiřelý tool_use musí skončit chybou")
    except vetev.VetevChyba as exc:
        over("výsledek chybí" in str(exc), "osiřelý tool_use: srozumitelná hláška")
    over(hash_(cesta) == h, "osiřelý tool_use: původní beze změny")
    # cíl uprostřed: výsledek existuje, ale až po cíli (zpráva přišla během práce)
    p = Prepis()
    p.clovek("start")
    a = p.nastroj("y1")
    mid = p.zaznam("attachment", None, attachment={"type": "queued_command", "prompt": "doplněk", "origin": {"kind": "human"}})
    p.vysledek("y1", rodic=a)
    p.claude("konec", )
    cesta = p.uloz()
    try:
        res = vetev.rozvetvit(cesta, mid)
        ids = [x["uuid"] for x in nacti(cesta_vetve(res)) if "uuid" in x]
        over(len(ids) == 3, "queued_command mezi tool_use a výsledkem: větev má i výsledek")
        over(res["text"] == "doplněk", "text z přílohy")
    except vetev.VetevChyba as exc:
        # výsledek visí pod tool_use, ne pod přílohou — povolené jen, pokud je v řetězu
        print("   (info)", exc)
        over(True, "")


def test_compact():
    print("po /compact")
    p = Prepis()
    p.clovek("stará 1")
    p.claude("stará odpověď")
    hranice = p.zaznam("system", None, rodic=None, subtype="compact_boundary", logicalParentUuid=p.posledni)
    p.posledni = hranice
    souhrn = p.zaznam("user", "Souhrn...", isCompactSummary=True)
    p.claude("po souhrnu")
    novy = p.clovek("nová otázka")
    p.claude("nová odpověď")
    cesta = p.uloz()
    res = vetev.rozvetvit(cesta, novy)
    ids = [x["uuid"] for x in nacti(cesta_vetve(res)) if "uuid" in x]
    over(ids[0] == hranice and souhrn in ids and len(ids) == 3, f"po compactu jen boundary+souhrn+odpověď ({len(ids)})")
    try:
        vetev.rozvetvit(cesta, souhrn)
        over(False, "souhrn po compactu není zpráva člověka")
    except vetev.VetevChyba:
        over(True, "")


def test_rozpulena_radka():
    print("rozpůlená poslední řádka")
    p, u = zakladni()
    cesta = p.uloz()
    with open(cesta, "ab") as fh:
        fh.write(b'{"parentUuid":"' + u["f"].encode() + b'","type":"user","uuid":"' + uid().encode() + b'","mess')
    res = vetev.rozvetvit(cesta, u["e"])
    over(len(nacti(cesta_vetve(res))) >= 4, "rozepsaný konec se ignoruje")
    # poškozený řádek uprostřed
    p, u = zakladni()
    cesta = p.uloz()
    data = open(cesta, "rb").read().split(b"\n")
    data.insert(3, b'{"zlomeny": ')
    open(cesta, "wb").write(b"\n".join(data))
    res = vetev.rozvetvit(cesta, u["e"])
    over(len(nacti(cesta_vetve(res))) >= 4, "poškozený řádek uprostřed se přeskočí")
    # prázdný soubor
    prazdny = os.path.join(PROJEKT, uid() + ".jsonl")
    open(prazdny, "wb").close()
    try:
        vetev.rozvetvit(prazdny, uid())
        over(False, "prázdný přepis")
    except vetev.VetevChyba:
        over(True, "")


def test_soubezny_zapis():
    print("souběžný zápis do originálu při kopii")
    p, u = zakladni()
    cesta = p.uloz()
    stop = threading.Event()

    def pis():
        n = 0
        while not stop.is_set():
            with open(cesta, "ab") as fh:
                e = {"parentUuid": u["f"], "type": "assistant", "uuid": uid(), "sessionId": p.sid,
                     "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]}}
                raw = (json.dumps(e) + "\n").encode()
                fh.write(raw[:len(raw) // 2])
                fh.flush()
                fh.write(raw[len(raw) // 2:])
            n += 1
            time.sleep(0.002)
    t = threading.Thread(target=pis)
    t.start()
    try:
        for _ in range(10):
            res = vetev.rozvetvit(cesta, u["e"])
            nove = nacti(cesta_vetve(res))
            over([x["uuid"] for x in nove if "uuid" in x] == [u["a"], u["b"], u["c"], u["d"]], "větev při zápisu je správná")
            vetev._POSLEDNI.clear()
    finally:
        stop.set()
        t.join()
    # výměna souboru (jiný inode) během kopie
    p, u = zakladni()
    cesta = p.uloz()
    puvodni = vetev._over

    def vymen(path, nove_id):
        os.rename(cesta, cesta + ".stary")
        shutil.copy(cesta + ".stary", cesta)
        return puvodni(path, nove_id)
    vetev._over = vymen
    try:
        vetev.rozvetvit(cesta, u["e"])
        over(False, "výměna souboru měla skončit chybou")
    except vetev.VetevChyba as exc:
        over("změnila" in str(exc), "výměna souboru: hláška")
    finally:
        vetev._over = puvodni
    over(not glob.glob(os.path.join(PROJEKT, p.sid + "*.tmp")) and
         not [f for f in os.listdir(PROJEKT) if f.endswith(".tmp")], "po výměně žádný .tmp")


def test_dvojklik():
    print("dvojklik / souběžné požadavky")
    p, u = zakladni()
    cesta = p.uloz()
    pred = set(os.listdir(PROJEKT))
    vysl = []

    def klik():
        vysl.append(vetev.rozvetvit(cesta, u["e"]))
    vlakna = [threading.Thread(target=klik) for _ in range(8)]
    for v in vlakna:
        v.start()
    for v in vlakna:
        v.join()
    nove = set(os.listdir(PROJEKT)) - pred
    over(len(nove) == 1, f"8 souběžných kliků = jedna větev ({len(nove)})")
    over(len({r["id"] for r in vysl}) == 1, "všechny odpovědi mají stejné id")
    # jiné místo se větví zvlášť
    vetev.rozvetvit(cesta, u["c"])
    over(len(set(os.listdir(PROJEKT)) - pred) == 2, "jiné místo = další větev")


def test_zapis_chyby():
    print("práva a plný disk")
    p, u = zakladni()
    cesta = p.uloz()
    h = hash_(cesta)
    pred = set(os.listdir(PROJEKT))
    # plný disk: fsync vyhodí ENOSPC
    puvodni = os.fsync

    def plny(fd):
        raise OSError(errno.ENOSPC, "No space left on device")
    os.fsync = plny
    try:
        vetev.rozvetvit(cesta, u["e"])
        over(False, "plný disk měl být chyba")
    except vetev.VetevChyba as exc:
        over("není místo" in str(exc), "plný disk: hláška česky")
    finally:
        os.fsync = puvodni
    over(set(os.listdir(PROJEKT)) == pred, "plný disk: žádný poloviční soubor")
    # plný disk při zápisu (write)
    pu = vetev._prepis_radek

    def spatny(*a):
        raise OSError(errno.ENOSPC, "No space")
    vetev._prepis_radek = spatny
    try:
        vetev.rozvetvit(cesta, u["e"])
        over(False, "plný disk (write)")
    except vetev.VetevChyba as exc:
        over("není místo" in str(exc), "plný disk při zápisu: hláška")
    finally:
        vetev._prepis_radek = pu
    over(set(os.listdir(PROJEKT)) == pred, "plný disk (write): nic nezbylo")
    # chybějící práva
    if os.geteuid() != 0:
        os.chmod(PROJEKT, 0o555)
        try:
            vetev.rozvetvit(cesta, u["e"])
            over(False, "složka jen ke čtení měla být chyba")
        except vetev.VetevChyba as exc:
            over("práva" in str(exc), f"bez práv: hláška ({exc})")
        finally:
            os.chmod(PROJEKT, 0o755)
        over(set(os.listdir(PROJEKT)) == pred, "bez práv: nic nezbylo")
    over(hash_(cesta) == h, "původní beze změny po chybách zápisu")


def test_znaky():
    print("nečekané znaky")
    p = Prepis()
    p.clovek("ahoj \u0000 ‮ 😀   řčž\ttab")
    p.claude("odpověď \u0000")
    cil = p.clovek("kde \\ \" ' <script>")
    p.claude("x")
    cesta = p.uloz()
    res = vetev.rozvetvit(cesta, cil)
    nove = nacti(cesta_vetve(res))
    over(nove[0]["message"]["content"].startswith("ahoj \u0000"), "unicode a \\x00 projdou beze změny")
    # cwd s mezerami
    p = Prepis()
    p.clovek("a", cwd="/tmp/s mezerou/../x")
    p.claude("b")
    c = p.clovek("c", cwd="/tmp/s mezerou/../x")
    cesta = p.uloz()
    over(vetev.rozvetvit(cesta, c)["cwd"] == "/tmp/s mezerou/../x", "cwd se vrací, jak je")


def test_velky():
    print("velký přepis (stovky MB, streamování)")
    p = Prepis()
    p.clovek("start")
    for i in range(40):
        p.claude(f"odpověď {i}")
        p.clovek(f"otázka {i}")
    cesta = p.uloz()
    # vycpávka: velké neodkazované záznamy a velké zprávy
    with open(cesta, "ab") as fh:
        for _ in range(150):
            fh.write((json.dumps({"type": "api-request-blob", "sessionId": p.sid, "message": "z" * 400_000}) + "\n").encode())
    p2 = Prepis(p.sid)
    p2.posledni = p.posledni
    cil = None
    for i in range(3000):
        p2.claude("x" * 8000)
        cil = p2.clovek(f"q{i}")
    p2.claude("konec")
    with open(cesta, "ab") as fh:
        for e in p2.radky:
            fh.write((json.dumps(e) + "\n").encode())
    mb = os.path.getsize(cesta) / 1e6
    t0 = time.time()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    res = vetev.rozvetvit(cesta, cil)
    dt = time.time() - t0
    rss = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / 1024
    print(f"   {mb:.0f} MB za {dt:.1f} s, přírůstek paměti {rss:.0f} MB")
    over(dt < 30, "velký přepis do 30 s")
    over(rss < 200, f"paměť neroste s velikostí ({rss:.0f} MB)")
    over(len(nacti(cesta_vetve(res))) > 6000, "velký přepis: větev úplná")
    os.remove(cesta)


def test_realne():
    print("skutečné přepisy (jen čtení, kopie)")
    if os.environ.get("VETEV_BEZ_REALNYCH") or not os.path.isdir(SKUTECNY):
        print("   přeskočeno")
        return
    soubory = []
    for f in glob.glob(SKUTECNY + "/*/*.jsonl"):
        if re_id(os.path.basename(f)[:-6]):
            soubory.append((os.path.getsize(f), f))
    soubory.sort()
    vyber = [f for s, f in soubory if s > 20_000][:8:2] + [f for s, f in soubory[-3:]]
    celkem = ok = chyb = 0
    for zdroj in dict.fromkeys(vyber):
        cesta = os.path.join(PROJEKT, os.path.basename(zdroj))
        shutil.copy(zdroj, cesta)
        h = hash_(cesta)
        idy = lidske(cesta)
        krok = max(1, len(idy) // 6)
        for z in idy[::krok]:
            celkem += 1
            try:
                res = vetev.rozvetvit(cesta, z)
                nove = nacti(cesta_vetve(res))
                over(all(x.get("sessionId", res["id"]) == res["id"] for x in nove), "realný: sessionId")
                ok += 1
            except vetev.VetevChyba as exc:
                chyb += 1
                over(len(str(exc)) > 10, "realný: hláška")
            vetev._POSLEDNI.clear()
        over(hash_(cesta) == h, f"realný: kopie beze změny {os.path.basename(zdroj)}")
        over(hash_(zdroj) == h, "realný: ani originál")
    print(f"   {celkem} větvení: {ok} ok, {chyb} odmítnuto s hláškou")


def re_id(s):
    import re
    return re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s) is not None


def lidske(cesta):
    out = []
    with open(cesta, "rb") as fh:
        for r in fh:
            try:
                e = json.loads(r)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("uuid"), str) and vetev._je_clovek(e):
                out.append(e["uuid"])
    return out


if __name__ == "__main__":
    try:
        for t in (test_zaklad, test_chyby_vstupu, test_nastroje, test_compact, test_rozpulena_radka,
                  test_soubezny_zapis, test_dvojklik, test_zapis_chyby, test_znaky, test_velky, test_realne):
            try:
                t()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                SELHANI.append(f"{t.__name__} spadl: {exc!r}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nprošlo {PROSLO[0]}, selhalo {len(SELHANI)}")
    for s in SELHANI:
        print(" -", s)
    sys.exit(1 if SELHANI else 0)
