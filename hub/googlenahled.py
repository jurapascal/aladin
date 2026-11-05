"""Náhled Google souboru v chatu přes připojené napojení (workspace-mcp).

Dřív se v chatu ukázala Googlí /preview v rámu, a ta chce, aby byl PROHLÍŽEČ
přihlášený ke Googlu — kdo měl Google napojený jen pro Clauda, viděl bílou plochu.
Tady si hub obsah stáhne sám tokenem, který už má (stejný soubor jako MCP
google), a ukáže ho jako HTML (dokument, tabulka), PDF (prezentace) nebo obrázek.
Vrací (kód, typ, tělo). Nic se neukládá.
"""
import datetime
import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import connect, core

TIMEOUT = 25
MAX_BYTES = 25 * 1024 * 1024


def _ucty():
    """Účty s refresh tokenem, výchozí první."""
    vse = [a["name"] for a in connect._google_accounts() if a.get("state") == "ok"]
    try:
        vychozi = connect._google_default()
    except Exception:
        vychozi = ""
    return sorted(vse, key=lambda e: e != vychozi)


def _token(email):
    try:
        with open(connect._google_file(email), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return ""
    ted = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    try:
        konec = datetime.datetime.fromisoformat(str(d.get("expiry") or "")) if d.get("expiry") else None
    except ValueError:
        konec = None
    if d.get("token") and konec and konec - ted > datetime.timedelta(seconds=90):
        return d["token"]
    cid, secret = d.get("client_id"), d.get("client_secret")
    if not (cid and secret):
        cid, secret, _ = connect.google_client()
    if not (d.get("refresh_token") and cid and secret):
        return ""
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                                   "client_id": cid, "client_secret": secret}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(connect.GOOGLE_TOKEN, data=data),
                                    timeout=TIMEOUT) as r:
            novy = json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    return novy.get("access_token") or ""


def _get(url, token, binarne=False):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        telo = r.read(MAX_BYTES + 1)
        if len(telo) > MAX_BYTES:
            raise ValueError("Soubor je moc velký na náhled.")
        return telo if binarne else telo.decode("utf-8", "replace"), r.headers.get("Content-Type", "")


def _stranka(telo, nadpis=""):
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<style>body{font:14px/1.5 system-ui,sans-serif;margin:14px;color:#1f2328;background:#fff}"
            "table{border-collapse:collapse;margin:6px 0 18px}td,th{border:1px solid #d0d7de;padding:4px 8px;"
            "vertical-align:top;white-space:pre-wrap}th{background:#f6f8fa;text-align:left}"
            "h3{margin:16px 0 4px;font-size:15px}.pozn{color:#59636e;font-size:12px}</style>"
            + (f"<h3>{html.escape(nadpis)}</h3>" if nadpis else "") + telo)


def _tabulka(token, fid):
    meta, _ = _get("https://sheets.googleapis.com/v4/spreadsheets/" + fid +
                   "?fields=sheets.properties.title", token)
    listy = [s["properties"]["title"] for s in json.loads(meta).get("sheets", [])][:6]
    if not listy:
        return None
    rng = "&".join("ranges=" + urllib.parse.quote(f"'{t}'!A1:AD300") for t in listy)
    data, _ = _get("https://sheets.googleapis.com/v4/spreadsheets/" + fid + "/values:batchGet?" + rng, token)
    casti = []
    for vr in json.loads(data).get("valueRanges", []):
        nazev = vr.get("range", "").split("!")[0].strip("'")
        radky = vr.get("values") or []
        if not radky:
            casti.append(f"<h3>{html.escape(nazev)}</h3><p class=pozn>prázdný list</p>")
            continue
        sirka = max(len(r) for r in radky)
        tr = "".join("<tr>" + "".join(f"<td>{html.escape(str(r[i]) if i < len(r) else '')}</td>"
                                       for i in range(sirka)) + "</tr>" for r in radky)
        casti.append(f"<h3>{html.escape(nazev)}</h3><table>{tr}</table>")
    return _stranka("".join(casti) + "<p class=pozn>Náhled: prvních 300 řádků a 30 sloupců.</p>")


def nahled(fid, druh):
    """→ (status, content-type, bytes). 404 = nejde (klient použije Googlí náhled)."""
    if not fid or not all(c.isalnum() or c in "-_" for c in fid):
        return 400, "text/plain; charset=utf-8", b"Neplatne id"
    for email in _ucty():
        token = _token(email)
        if not token:
            continue
        try:
            if druh == "document":
                telo, _ = _get("https://www.googleapis.com/drive/v3/files/" + fid +
                               "/export?mimeType=text%2Fhtml", token)
                return 200, "text/html; charset=utf-8", telo.encode("utf-8")
            if druh == "spreadsheets":
                stranka = _tabulka(token, fid)
                if stranka:
                    return 200, "text/html; charset=utf-8", stranka.encode("utf-8")
            if druh == "presentation":
                telo, _ = _get("https://www.googleapis.com/drive/v3/files/" + fid +
                               "/export?mimeType=application%2Fpdf", token, binarne=True)
                return 200, "application/pdf", telo
            if druh == "file":
                meta, _ = _get("https://www.googleapis.com/drive/v3/files/" + fid + "?fields=mimeType", token)
                mime = json.loads(meta).get("mimeType", "")
                if mime.startswith("image/") or mime == "application/pdf":
                    telo, ct = _get("https://www.googleapis.com/drive/v3/files/" + fid + "?alt=media",
                                    token, binarne=True)
                    return 200, mime, telo
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                continue           # tenhle účet k souboru nemá přístup — zkusit další
            core.log(f"google náhled: HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            core.log(f"google náhled: {exc}")
    return 404, "text/plain; charset=utf-8", b"Nelze"
