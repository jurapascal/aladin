"""
Slučování stejných souborů v mezipaměti napříč domovy — noční úklid brány.

Skoro každý prostor si stáhl totéž: nástroj pro Google (workspace-mcp přes uv,
~220 MB u každého), prohlížeč pro Puppeteer či Playwright, balíčky npm. Bajt po
bajtu stejné soubory leží na disku tolikrát, kolik je lidí. Tady se z nich
stane jeden soubor s víc jmény (hardlink): každý domov ho má dál na svém místě,
ale na disku je jednou.

Sahá se **jen na mezipaměti a nainstalované nástroje, do kterých nikdo nepíše**
(SLUCOVAT). Nikdy na dokumenty, projekty, chaty ani Obsidiany — tam se soubory
upravují a úprava jednoho by se propsala všem, kdo mají sloučenou kopii.

Pojistky:

* **Stejný obsah dvakrát ověřený**: nejdřív sha256, těsně před sloučením ještě
  porovnání bajt po bajtu. Soubor, který se mezitím změnil (číslo, velikost,
  čas změny), se nechá být.
* **Jen soubory, které všechna svá jména mají v těchhle mezipamětech.** Soubor
  s dalším jménem jinde (uv a pnpm dělají hardlinky do projektů, kde se dá
  knihovna upravit přímo v .venv) by sloučení nic neušetřilo — a hlavně by se
  pak úprava v projektu jednoho propsala do mezipaměti druhého. Proto tu není
  ani `~/.cache/uv`, ani úložiště pnpm: jsou zdrojem hardlinků do projektů.
* **Stejný vlastník, skupina a práva** (bez ohledu na zápis, viz níž), stejný
  oddíl. S oddělenými účty prostorů (`HUB_GW_UCTY=1`, každý prostor vlastním
  uživatelem systému) se mezi lidmi neslučuje vůbec — soubor by pak patřil
  cizímu účtu.
* **Sloučený soubor je jen ke čtení** (bez práva zápisu). Kdyby se ho přece
  jen něco pokusilo upravit na místě (editor, Claude), skončí na „Permission
  denied" a ne tím, že tiše změní soubor všem. Smazat ho jde dál (mazání je
  věc složky, ne souboru) — každý si svou mezipaměť smaže, kdy chce, a ostatním
  zůstane.
* **Cesty se otevírají po složkách s O_NOFOLLOW** (gateway/safefs.py): domov
  patří prostoru, a kdo by během úklidu prohodil složku za odkaz jinam na
  server, úklid nikam jinam nesáhne. Nové jméno vzniká jako dočasný hardlink
  a na místo se dostane atomickým `rename`.
* **Záznam a cesta zpět**: každé sloučení jde do `uloziste/slouceni.jsonl`
  (cesta, sha256, velikost, původní práva). `claude-hub-admin uklid --rozpojit`
  dá každému sloučenému souboru zase vlastní kopii s původními právy.

    claude-hub-admin uklid --suchy     co by se sloučilo, nic se nemění
    claude-hub-admin uklid             sloučit a přepočítat místo
    claude-hub-admin uklid --rozpojit  vrátit sloučené soubory zpátky
"""
import json
import os
import secrets
import stat
import time

from . import isolation, safefs, uloziste, workspace

SLUCOVAT = (
    ".local/share/uv/python",        # Pythony stažené přes uv
    ".local/share/uv/tools",         # nástroje přes uv (workspace-mcp pro Google)
    ".cache/ms-playwright",          # prohlížeče pro Playwright
    ".cache/puppeteer",              # Chrome pro Puppeteer
    ".cache/huggingface/hub",        # modely (bloby, adresované obsahem)
    ".npm/_cacache",                 # archivy balíčků npm (adresované obsahem)
)
MIN_BAJTU = 64 * 1024                # menší soubory nestojí za práci
LOG = os.path.join(uloziste.DIR, "slouceni.jsonl")
HASHE = os.path.join(uloziste.DIR, "hashe.json")
_DIRFL = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_BEZ_ZAPISU = ~0o222


class _Jmeno:
    """Jedno jméno souboru: domov, složky od domova dolů, jméno, stat."""
    __slots__ = ("home", "dirparts", "name", "st")

    def __init__(self, home, dirparts, name, st):
        self.home, self.dirparts, self.name, self.st = home, dirparts, name, st

    def rel(self):
        """Cesta od složky domovů (`adam.uher/.cache/…`) — do záznamu."""
        return "/".join((os.path.basename(self.home),) + self.dirparts + (self.name,))


def _projdi(home, rel_root, dev, out):
    parts = tuple(rel_root.split("/"))
    try:
        fd = safefs.open_dir(home, parts)
    except OSError:
        return                                   # není, nebo je to odkaz
    try:
        if os.fstat(fd).st_dev != dev:
            return
        _slozka(home, fd, parts, dev, out, 0)
    finally:
        os.close(fd)


def _slozka(home, fd, parts, dev, out, hloubka):
    try:
        entries = list(os.scandir(fd))
    except OSError:
        return
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != dev or hloubka > 60:
                continue
            try:
                cfd = os.open(e.name, _DIRFL, dir_fd=fd)
            except OSError:
                continue
            try:
                _slozka(home, cfd, parts + (e.name,), dev, out, hloubka + 1)
            finally:
                os.close(cfd)
        elif stat.S_ISREG(st.st_mode) and st.st_size >= MIN_BAJTU and st.st_dev == dev:
            out.append(_Jmeno(home, parts, e.name, st))


def _otevri(j):
    return safefs.open_dir(j.home, j.dirparts)


def _hash(j, cache, used):
    key = f"{j.st.st_dev}:{j.st.st_ino}:{j.st.st_size}:{j.st.st_mtime_ns}"
    used.add(key)
    if key in cache:
        return cache[key]
    try:
        dfd = _otevri(j)
        try:
            h = uloziste.sha256_fd(uloziste.otevrit_cteni(dfd, j.name))
        finally:
            os.close(dfd)
    except OSError:
        return ""
    cache[key] = h
    return h


def _stejne(afd, aname, bfd, bname):
    """Bajt po bajtu — poslední slovo před sloučením."""
    a = uloziste.otevrit_cteni(afd, aname)
    try:
        b = uloziste.otevrit_cteni(bfd, bname)
    except OSError:
        os.close(a)
        raise
    with os.fdopen(a, "rb") as fa, os.fdopen(b, "rb") as fb:
        while True:
            ka, kb = fa.read(1024 * 1024), fb.read(1024 * 1024)
            if ka != kb:
                return False
            if not ka:
                return True


def _klic(st):
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _nahradit(kanon, cil):
    """Jméno `cil` povede na soubor `kanon`. Vrací '' nebo důvod, proč ne."""
    kfd = _otevri(kanon)
    try:
        cfd = _otevri(cil)
    except OSError:
        os.close(kfd)
        raise
    tmp = f".{cil.name[:80]}.hub-slouceni-{secrets.token_hex(4)}"
    try:
        kst = os.stat(kanon.name, dir_fd=kfd, follow_symlinks=False)
        cst = os.stat(cil.name, dir_fd=cfd, follow_symlinks=False)
        if not (stat.S_ISREG(kst.st_mode) and stat.S_ISREG(cst.st_mode)):
            return "není soubor"
        if _klic(kst) != _klic(kanon.st) or _klic(cst) != _klic(cil.st):
            return "změnil se"
        if not _stejne(kfd, kanon.name, cfd, cil.name):
            return "liší se"
        os.link(kanon.name, tmp, src_dir_fd=kfd, dst_dir_fd=cfd, follow_symlinks=False)
        try:
            tst = os.stat(tmp, dir_fd=cfd, follow_symlinks=False)
            cst2 = os.stat(cil.name, dir_fd=cfd, follow_symlinks=False)
            if tst.st_ino != kst.st_ino or _klic(cst2) != _klic(cst):
                raise FileExistsError("změnil se")
            os.replace(tmp, cil.name, src_dir_fd=cfd, dst_dir_fd=cfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=cfd)
            except OSError:
                pass
            raise
        return ""
    except FileExistsError:
        return "změnil se"
    finally:
        os.close(kfd)
        os.close(cfd)


def _jen_cteni(j, mode):
    """Sloučenému souboru vezme právo zápisu (přes fd, ne cestou)."""
    try:
        dfd = _otevri(j)
        try:
            fd = os.open(j.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.fchmod(fd, stat.S_IMODE(mode) & _BEZ_ZAPISU)
            finally:
                os.close(fd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _zapsat(entries):
    if not entries:
        return
    os.makedirs(uloziste.DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _domovy(users):
    seen, out = set(), []
    for u in users:
        try:
            home = workspace.home_for(u)
        except OSError:
            continue
        if home not in seen and os.path.isdir(home) and not os.path.islink(home):
            seen.add(home)
            out.append(home)
    return out


def sloucit(users, suchy=False, log=print):
    """Najde a (bez `suchy`) sloučí stejné soubory v SLUCOVAT napříč domovy.
    Vrací shrnutí {"slouceno", "usetreno", "souboru", "preskoceno", …}."""
    start = time.time()
    out = {"cas": int(start), "suchy": bool(suchy), "slouceno": 0, "usetreno": 0, "souboru": 0,
           "skupin": 0, "preskoceno": {}, "oddelene_ucty": isolation.PER_USER}
    if isolation.PER_USER:
        # Každý prostor jiným uživatelem systému: soubor by po sloučení patřil
        # jednomu z nich a druhý by ho měl cizí. Tady se nedá nic bezpečně udělat.
        log("úklid: oddělené účty prostorů — mezi lidmi se neslučuje")
        out["trvalo"] = 0
        return out
    try:
        dev = os.stat(workspace.USERS_ROOT).st_dev
    except OSError:
        return out
    jmena = []
    for home in _domovy(users):
        for rel in SLUCOVAT:
            _projdi(home, rel, dev, jmena)
    out["souboru"] = len(jmena)

    inody = {}
    for j in jmena:
        inody.setdefault((j.st.st_dev, j.st.st_ino), []).append(j)
    podle_velikosti = {}
    for key, js in inody.items():
        podle_velikosti.setdefault(js[0].st.st_size, []).append(key)

    cache = uloziste._nacti(HASHE)
    used = set()
    me = os.geteuid()
    zaznam = []

    def preskoc(duvod):
        out["preskoceno"][duvod] = out["preskoceno"].get(duvod, 0) + 1

    for size, keys in podle_velikosti.items():
        if len(keys) < 2:
            continue
        podle_hashe = {}
        for key in keys:
            h = _hash(inody[key][0], cache, used)
            if h:
                podle_hashe.setdefault(h, []).append(key)
        for h, stejne in podle_hashe.items():
            if len(stejne) < 2:
                continue
            out["skupin"] += 1
            # Jen soubory, které mají všechna jména tady (uzavřené) — jinak by
            # se sdílený obsah dostal i tam, kde se upravuje.
            uzavrene = [k for k in stejne if len(inody[k]) == inody[k][0].st.st_nlink]
            for k in set(stejne) - set(uzavrene):
                preskoc("jméno i mimo mezipaměť")
            if len(uzavrene) < 2:
                continue
            # Kanonický: ten, co už má nejvíc jmen (sloučený minule), pak nejstarší.
            uzavrene.sort(key=lambda k: (-len(inody[k]), inody[k][0].st.st_mtime_ns))
            kanon = inody[uzavrene[0]][0]
            kst = kanon.st
            if kst.st_uid != me and me != 0:
                preskoc("cizí vlastník")
                continue
            sloucen = False
            for k in uzavrene[1:]:
                js = inody[k]
                st = js[0].st
                if (st.st_uid, st.st_gid) != (kst.st_uid, kst.st_gid):
                    preskoc("jiný vlastník")
                    continue
                if stat.S_IMODE(st.st_mode) & _BEZ_ZAPISU != stat.S_IMODE(kst.st_mode) & _BEZ_ZAPISU:
                    preskoc("jiná práva")
                    continue
                if suchy:
                    out["slouceno"] += len(js)
                    out["usetreno"] += st.st_blocks * 512
                    continue
                vsechna = True
                for j in js:
                    try:
                        duvod = _nahradit(kanon, j)
                    except OSError as exc:
                        duvod = "chyba: " + (exc.strerror or str(exc))
                    if duvod:
                        preskoc(duvod)
                        vsechna = False
                        continue
                    sloucen = True
                    out["slouceno"] += 1
                    zaznam.append({"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "akce": "slouceno",
                                   "cesta": j.rel(), "kanon": kanon.rel(), "bajtu": st.st_size,
                                   "sha256": h, "prava": oct(stat.S_IMODE(st.st_mode))})
                if vsechna:
                    out["usetreno"] += st.st_blocks * 512
            if sloucen:
                _jen_cteni(kanon, kst.st_mode)
                zaznam.append({"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "akce": "kanon",
                               "cesta": kanon.rel(), "sha256": h,
                               "prava": oct(stat.S_IMODE(kst.st_mode))})
        if len(zaznam) > 500:
            _zapsat(zaznam)
            zaznam = []
    _zapsat(zaznam)
    try:
        uloziste._ulozit(HASHE, {k: v for k, v in cache.items() if k in used})
    except OSError:
        pass
    out["trvalo"] = round(time.time() - start, 1)
    log(f"úklid{' (nanečisto)' if suchy else ''}: souborů v mezipamětech {out['souboru']}, "
        f"skupin stejných {out['skupin']}, sloučeno {out['slouceno']}, "
        f"{'ušetřilo by se' if suchy else 'ušetřeno'} {uloziste.velikost(out['usetreno'])}")
    return out


def shrnuti(out):
    keep = ("cas", "trvalo", "suchy", "slouceno", "usetreno", "souboru", "skupin", "preskoceno",
            "oddelene_ucty")
    return {k: out.get(k) for k in keep}


def rozpojit(log=print):
    """Každému sloučenému souboru ze záznamu dá zase vlastní kopii s původními
    právy (a kanonickému vrátí práva). Vrací počet rozpojených jmen."""
    try:
        with open(LOG, encoding="utf-8") as fh:
            radky = [json.loads(r) for r in fh if r.strip()]
    except (OSError, ValueError):
        radky = []
    posledni = {}
    for r in radky:
        if r.get("akce") in ("slouceno", "kanon", "rozpojeno"):
            posledni[r.get("cesta")] = r
    hotovo, zaznam = 0, []
    for cesta, r in posledni.items():
        if r.get("akce") == "rozpojeno" or not cesta:
            continue
        parts = cesta.split("/")
        try:
            prava = int(str(r.get("prava") or "0o644"), 8)
            dfd = safefs.open_dir(workspace.USERS_ROOT, parts[:-1])
        except (OSError, ValueError):
            continue
        try:
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                continue
            if r["akce"] == "kanon" or st.st_nlink == 1:
                _prava(dfd, parts[-1], prava)
                continue
            tmp = f".{parts[-1][:80]}.hub-rozpojeni-{secrets.token_hex(4)}"
            src = uloziste.otevrit_cteni(dfd, parts[-1])
            try:
                dst = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, prava, dir_fd=dfd)
                with os.fdopen(src, "rb") as fin, os.fdopen(dst, "wb") as fout:
                    src = -1
                    for kus in iter(lambda: fin.read(1024 * 1024), b""):
                        fout.write(kus)
                    os.fchmod(fout.fileno(), prava)
                os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns), dir_fd=dfd, follow_symlinks=False)
                os.replace(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                if src >= 0:
                    os.close(src)
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise
            hotovo += 1
            zaznam.append({"cas": time.strftime("%Y-%m-%d %H:%M:%S"), "akce": "rozpojeno",
                           "cesta": cesta, "bajtu": st.st_size})
        except OSError as exc:
            log(f"rozpojení {cesta}: {exc}")
        finally:
            os.close(dfd)
    _zapsat(zaznam)
    log(f"rozpojeno {hotovo} sloučených souborů")
    return hotovo


def _prava(dfd, name, prava):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    try:
        os.fchmod(fd, prava)
    finally:
        os.close(fd)


def posledni_zaznam(n=50):
    """Posledních `n` řádků záznamu sloučení (pro výpis v CLI)."""
    try:
        with open(LOG, encoding="utf-8") as fh:
            radky = fh.readlines()[-n:]
        return [json.loads(r) for r in radky if r.strip()]
    except (OSError, ValueError):
        return []
