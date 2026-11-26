"""ElevenLabs — jediný hlas hubu: diktování (Speech to Text, model Scribe),
předčítání odpovědí (Text to Speech) a hovor s Claudem (hlas.js).

Člověk vloží API klíč v Nastavení → Hlas, vybere hlas (i vlastní klon nebo
hlas z knihovny, který má v „My Voices“) a model. Klíč leží v
`~/.claude/elevenlabs.json` jen pro majitele a do prohlížeče se nikdy nevrací.
Bez klíče hub mikrofon ani předčítání neukazuje.

Zvuk i text jdou na ElevenLabs a účtují se z kreditů účtu (minuty nahrávky,
znaky textu — u modelů Flash/Turbo poloviční).
"""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import core

CONFIG_PATH = os.path.join(core.CLAUDE_DIR, "elevenlabs.json")
API = "https://api.elevenlabs.io"
TIMEOUT = 25
FORMAT = "mp3_44100_128"
# Výchozí model: rychlý a za poloviční cenu, česky umí. Kdo chce nejvyšší
# kvalitu, přepne si na v3 / Multilingual v2.
VYCHOZI_MODEL = "eleven_flash_v2_5"
ID_RE = re.compile(r"[A-Za-z0-9]{8,40}")

_cache = {}                         # klíč → (čas, data) pro hlasy a modely
CACHE_S = 600


def _load():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _write(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    # Klíč se nesmí ani na chvíli objevit čitelný pro ostatní.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _key():
    return str(_load().get("api_key") or "")


def _request(key, method, path, body=None, raw=False):
    """(HTTP kód, data). Chyba sítě → (0, {"error": …})."""
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg" if raw else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            data = res.read()
            return res.status, (data if raw else json.loads(data or b"{}"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {"error": str(exc)}


def explain(code, data):
    """Chyba z ElevenLabs jako věta, se kterou člověk něco udělá."""
    detail = data.get("detail") if isinstance(data, dict) else None
    st = (detail.get("status") or "") if isinstance(detail, dict) else ""
    msg = (detail.get("message") if isinstance(detail, dict) else detail) or \
        (data.get("error") if isinstance(data, dict) else "") or ""
    msg = str(msg)
    if code == 0:
        return "ElevenLabs není dostupný: " + (msg or "chyba sítě")
    if st == "missing_permissions":
        return ("Klíč nemá potřebné právo — v ElevenLabs mu povol Text to Speech, "
                "Speech to Text a Voices (čtení), nebo vytvoř klíč bez omezení.")
    if st in ("payment_issue", "payment_required") or (isinstance(detail, dict) and detail.get("type") == "payment_required"):
        return ("Na účtu ElevenLabs je nezaplacená platba — doplať poslední fakturu "
                "na elevenlabs.io (Subscription), pak hlas zase pojede.")
    if code == 401 or st in ("invalid_api_key", "needs_authorization"):
        return "Klíč neplatí — zkopíruj ho znovu z ElevenLabs (Developers → API Keys)."
    if st in ("quota_exceeded", "insufficient_credits") or "quota" in msg.lower():
        return "Na účtu ElevenLabs došly kredity — dobij je na elevenlabs.io."
    if "fine-tuned" in msg or "fine_tuned" in msg:
        return "Tenhle hlas ElevenLabs ještě dotrénovává — vyber zatím jiný."
    if st == "voice_not_found" or code == 404:
        return "Vybraný hlas na účtu už není — vyber jiný v Nastavení → Hlas."
    if code == 429:
        return "ElevenLabs teď nestíhá (moc požadavků najednou) — zkus to za chvíli."
    return f"ElevenLabs vrátil {code}: {msg[:200] or 'bez popisu'}"


# ── stav a nastavení ─────────────────────────────────────────────────────────
def status():
    cfg = _load()
    key = str(cfg.get("api_key") or "")
    return {
        "connected": bool(key),
        # Klíč zpátky do prohlížeče nikdy — jen konec, ať se pozná, který to je.
        "key_hint": ("…" + key[-4:]) if key else "",
        "active": bool(key) and bool(cfg.get("active")) and bool(cfg.get("voice_id")),
        "voice_id": str(cfg.get("voice_id") or ""),
        "voice_name": str(cfg.get("voice_name") or ""),
        "model_id": str(cfg.get("model_id") or VYCHOZI_MODEL),
    }


def _hlasy(key, fresh=False):
    hit = _cache.get(("hlasy", key))
    if hit and not fresh and time.time() - hit[0] < CACHE_S:
        return hit[1]
    code, data = _request(key, "GET", "/v1/voices")
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(explain(code, data))
    out = []
    for v in data.get("voices") or []:
        labels = v.get("labels") or {}
        jazyky = {str(labels.get("language") or "")} | {
            str(x.get("language") or "") for x in (v.get("verified_languages") or [])}
        jazyky.discard("")
        # Profesionální klon jde použít, až ho ElevenLabs dotrénuje —
        # do té doby vrací 400 „not fine-tuned“.
        stavy = ((v.get("fine_tuning") or {}).get("state") or {}).values()
        ok = v.get("category") != "professional" or "fine_tuned" in stavy
        name = str(v.get("name") or "")
        # „Roger - Laid-Back, Casual" → jméno a popis zvlášť.
        jmeno, _, popis = name.partition(" - ")
        out.append({
            "id": str(v.get("voice_id") or ""),
            "name": jmeno.strip() or name,
            "desc": popis.strip() or str(labels.get("descriptive") or ""),
            "category": str(v.get("category") or ""),
            "gender": str(labels.get("gender") or ""),
            "accent": str(labels.get("accent") or ""),
            "cs": "cs" in jazyky,
            "own": v.get("category") in ("cloned", "professional", "generated"),
            "ok": ok,
        })
    # Použitelné, česky mluvící a vlastní hlasy nahoru, pak ostatní podle jména.
    # Profesionální klon zní líp než rychlý — mezi vlastními má přednost.
    out.sort(key=lambda v: (not v["ok"], not v["cs"], not v["own"],
                            v["category"] != "professional", v["name"].lower()))
    _cache[("hlasy", key)] = (time.time(), out)
    return out


def _modely(key):
    hit = _cache.get(("modely", key))
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    code, data = _request(key, "GET", "/v1/models")
    if code != 200 or not isinstance(data, list):
        raise RuntimeError(explain(code, data))
    out = []
    for m in data:
        if not m.get("can_do_text_to_speech"):
            continue
        langs = {str(l.get("language_id") or "") for l in (m.get("languages") or [])}
        if "cs" not in langs:
            continue                 # odpovědi jsou česky — angličtina nás netrápí
        rates = m.get("model_rates") or {}
        out.append({"id": str(m.get("model_id") or ""),
                    "name": str(m.get("name") or m.get("model_id") or ""),
                    "half": float(rates.get("character_cost_multiplier") or 1) < 1})
    _cache[("modely", key)] = (time.time(), out)
    return out


def nabidka(fresh=False):
    """Hlasy a modely z účtu pro výběr v Nastavení."""
    key = _key()
    if not key:
        return {"ok": False, "error": "ElevenLabs není napojený."}
    try:
        return {"ok": True, "voices": _hlasy(key, fresh), "models": _modely(key), **status()}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc), **status()}


def save(api_key=""):
    """Uloží klíč, jen když s ním ElevenLabs doopravdy odpoví. Hned vybere
    první česky mluvící hlas, ať předčítání funguje bez dalšího klikání."""
    key = str(api_key or "").strip()
    if not key or len(key) < 20 or any(c.isspace() for c in key):
        return {"ok": False, "error": "Vlož celý API klíč z ElevenLabs."}
    try:
        hlasy = _hlasy(key, fresh=True)
        _modely(key)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    cfg = _load()
    cfg["api_key"] = key
    if not cfg.get("voice_id") or not any(v["id"] == cfg["voice_id"] and v["ok"] for v in hlasy):
        prvni = next((v for v in hlasy if v["ok"]), {"id": "", "name": ""})
        cfg["voice_id"], cfg["voice_name"] = prvni["id"], prvni["name"]
    cfg.setdefault("model_id", VYCHOZI_MODEL)
    cfg["active"] = True
    _write(cfg)
    return {"ok": True, **nabidka()}


def vyber(voice_id=None, model_id=None, active=None):
    cfg = _load()
    if not cfg.get("api_key"):
        return {"ok": False, "error": "ElevenLabs není napojený."}
    if voice_id is not None:
        voice_id = str(voice_id)
        hlas = next((v for v in _hlasy(cfg["api_key"]) if v["id"] == voice_id), None)
        if not hlas:
            return {"ok": False, "error": "Takový hlas na účtu není."}
        if not hlas["ok"]:
            return {"ok": False, "error": "Tenhle hlas ElevenLabs ještě dotrénovává — zatím nejde použít."}
        cfg["voice_id"], cfg["voice_name"] = hlas["id"], hlas["name"]
    if model_id is not None:
        if not any(m["id"] == model_id for m in _modely(cfg["api_key"])):
            return {"ok": False, "error": "Takový model ElevenLabs nemá."}
        cfg["model_id"] = str(model_id)
    if active is not None:
        cfg["active"] = bool(active)
    _write(cfg)
    return {"ok": True, **status()}


def remove():
    try:
        os.remove(CONFIG_PATH)
    except FileNotFoundError:
        pass
    _cache.clear()
    return {"ok": True, **status()}


# ── řeč ──────────────────────────────────────────────────────────────────────
def aktivni():
    return status()["active"]


def rec(text, voice_id="", model_id=""):
    """MP3 s přečteným textem. `voice_id`/`model_id` přebijí uložené (ukázka)."""
    cfg = _load()
    key = str(cfg.get("api_key") or "")
    if not key:
        raise RuntimeError("ElevenLabs není napojený.")
    voice = str(voice_id or cfg.get("voice_id") or "")
    model = str(model_id or cfg.get("model_id") or VYCHOZI_MODEL)
    if not ID_RE.fullmatch(voice) or not re.fullmatch(r"[a-z0-9_]{3,60}", model):
        raise ValueError("Vyber hlas v Nastavení → Hlas.")
    path = (f"/v1/text-to-speech/{urllib.parse.quote(voice)}"
            f"?output_format={FORMAT}")
    code, data = _request(key, "POST", path,
                          {"text": text, "model_id": model, "language_code": "cs"}, raw=True)
    if code == 400 and "language" in json.dumps(data).lower():
        # Model, který vynucení jazyka neumí — bez něj.
        code, data = _request(key, "POST", path, {"text": text, "model_id": model}, raw=True)
    if code != 200 or not isinstance(data, bytes):
        raise RuntimeError(explain(code, data))
    return data


# ── přepis řeči (diktování a hovor) ──────────────────────────────────────────
STT_MODEL = "scribe_v1"
STT_TIMEOUT = 120


def _typ_zvuku(data):
    """(přípona, MIME) podle prvních bajtů — prohlížeč nahrává webm, ogg nebo mp4."""
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm", "audio/webm"
    if data[:4] == b"OggS":
        return "ogg", "audio/ogg"
    if data[:4] == b"RIFF":
        return "wav", "audio/wav"
    if data[4:8] == b"ftyp":
        return "m4a", "audio/mp4"
    return "webm", "application/octet-stream"


def prepis(data):
    """Text z nahrávky (česky) přes ElevenLabs Speech to Text. RuntimeError s větou."""
    key = _key()
    if not key:
        raise RuntimeError("ElevenLabs není napojený — napoj ho v Nastavení → Hlas.")
    pripona, mime = _typ_zvuku(data)
    hranice = "----aladin" + secrets.token_hex(12)
    casti = []
    for jmeno, hodnota in (("model_id", STT_MODEL), ("language_code", "ces"),
                           ("tag_audio_events", "false"), ("diarize", "false")):
        casti.append((f'--{hranice}\r\nContent-Disposition: form-data; name="{jmeno}"\r\n\r\n'
                      f"{hodnota}\r\n").encode("utf-8"))
    casti.append((f'--{hranice}\r\nContent-Disposition: form-data; name="file"; '
                  f'filename="nahravka.{pripona}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8"))
    telo = b"".join(casti) + data + f"\r\n--{hranice}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        API + "/v1/speech-to-text", method="POST", data=telo,
        headers={"xi-api-key": key, "Content-Type": f"multipart/form-data; boundary={hranice}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=STT_TIMEOUT) as res:
            out = json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read() or b"{}")
        except ValueError:
            err = {}
        raise RuntimeError(explain(exc.code, err)) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(explain(0, {"error": str(exc)})) from None
    return str(out.get("text") or "").strip()


# ── řeč proudem (předčítání bez čekání) ──────────────────────────────────────
# Prohlížeč dostane jen krátké id (POST hlas-pripravit) a zvuk si stáhne jako
# obyčejný <audio src> — přehrávat začne s prvními bajty, ještě než ElevenLabs
# dopočítá zbytek. Text tak neleží v adrese (ani v historii, ani v logu).
_pripravene = {}                    # id → (čas, text, hlas, model)
_zamek = threading.Lock()


def priprav(text, voice_id="", model_id=""):
    cfg = _load()
    voice = str(voice_id or cfg.get("voice_id") or "")
    model = str(model_id or cfg.get("model_id") or VYCHOZI_MODEL)
    if not cfg.get("api_key"):
        raise RuntimeError("ElevenLabs není napojený.")
    if not ID_RE.fullmatch(voice) or not re.fullmatch(r"[a-z0-9_]{3,60}", model):
        raise ValueError("Vyber hlas v Nastavení → Hlas.")
    ted = time.time()
    with _zamek:
        for k in [k for k, v in _pripravene.items() if ted - v[0] > 120]:
            del _pripravene[k]
        rid = secrets.token_urlsafe(12)
        _pripravene[rid] = (ted, text, voice, model)
    return rid


def proud(rid):
    """Otevřená odpověď ElevenLabs (čte se po kouscích), nebo RuntimeError."""
    with _zamek:
        hit = _pripravene.pop(str(rid or ""), None)
    if not hit:
        raise ValueError("Tenhle kus už vypršel.")
    _ts, text, voice, model = hit
    key = _key()
    path = (f"/v1/text-to-speech/{urllib.parse.quote(voice)}/stream"
            f"?output_format={FORMAT}")
    for body in ({"text": text, "model_id": model, "language_code": "cs"},
                 {"text": text, "model_id": model}):
        req = urllib.request.Request(
            API + path, method="POST", data=json.dumps(body).encode("utf-8"),
            headers={"xi-api-key": key, "Content-Type": "application/json",
                     "Accept": "audio/mpeg"})
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read() or b"{}")
            except ValueError:
                data = {}
            if exc.code == 400 and "language" in json.dumps(data).lower() and "language_code" in body:
                continue                 # model bez vynucení jazyka — znovu bez něj
            raise RuntimeError(explain(exc.code, data))
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(explain(0, {"error": str(exc)}))
    raise RuntimeError("ElevenLabs nevrátil zvuk.")
