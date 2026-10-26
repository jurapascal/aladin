"""
Víc vlastních předplatných Claude na jeden účet a rychlé přepínání mezi nimi.

Když jednomu předplatnému dojde týdenní limit (ve čtvrtek 82 %), přepne člověk
na druhé — svoje, třeba druhý účet Pro — a po obnovení limitu zpátky. Tokeny
leží u brány (workspace.predplatna); tenhle modul je to, co s nimi dělá
/gw/claude:

* `uprav` — přidat, obnovit, přejmenovat, odebrat, přepnout (POST /gw/claude);
* `limity` — skutečný limit každého předplatného (5 h a týden) od Anthropicu.
  Ptá se brána, token z ní neodejde; ven jdou jen procenta;
* `cekej` / `vyzvedni_meta` — přidání přímo v prostoru (bez appky na
  počítači): `claude setup-token` běží v hubu prostoru, brána si token
  vyzvedne a tady si mezitím pamatuje, k čemu patří (nové / obnovení).

Sdílet předplatné mezi lidmi je proti podmínkám Anthropicu, víc předplatných
jednoho člověka ne — každý token patří účtu, který ho připojil.
"""
import threading
import time

from . import workspace
from hub import limity as _limity

LIMITY = _limity.Limity()
# Přidání v prostoru: {id účtu: {"replace", "label", "t"}} — do vyzvednutí tokenu.
_cekajici = {}
_lock = threading.Lock()
CEKANI = 15 * 60


def uprav(user, form, bearer=False, ua=""):
    """Změna podle POST /gw/claude. Vrací (změnilo se přihlášení?, id přidaného).

    Starší appka posílá jen {token, label} a „label“ u ní znamená, odkud token
    je; nový token u ní nahrazoval ten jediný. Pozná se podle chybějícího
    `source` a chová se pořád stejně — obnoví aktivní předplatné, nepřidá druhé.
    `ua` = prohlížeč, odkud to přišlo (zdroj u starší stránky v prostoru)."""
    if form.get("remove") is True:
        sid = str(form.get("id") or "") or None
        return workspace.remove_predplatne(user, sid), ""
    if form.get("activate"):
        return workspace.set_active_predplatne(user, str(form.get("activate"))), ""
    if form.get("rename"):
        workspace.rename_predplatne(user, str(form.get("rename")), form.get("label"))
        return False, ""
    if "source" in form:
        sid = workspace.save_predplatne(user, form.get("token"), str(form.get("source") or ""),
                                        label=str(form.get("label") or ""),
                                        replace=str(form.get("replace") or ""))
        return True, sid
    # Starší appka (bearer) nebo starší stránka v prostoru (cookie).
    _items, active = workspace.predplatna(user)
    source = str(form.get("label") or "") if bearer else str(ua or "")[:60]
    sid = workspace.save_predplatne(user, form.get("token"), source, replace=active)
    return True, sid


def limity(user, cekat=0.0, obnovit=False):
    """{"limity": {id: {ok, session, week, …}}, "limity_pocita": bool} pro
    všechna předplatná účtu. Drží se 5 minut (hub/limity.py)."""
    items, _active = workspace.predplatna(user)
    out, pocita = LIMITY.zjisti({it["id"]: it["token"] for it in items},
                                cekat=cekat, obnovit=obnovit)
    return {"limity": out, "limity_pocita": pocita}


def cekej(user, replace="", label=""):
    """Prostor začal přihlášení (`claude setup-token`) — k čemu token bude."""
    with _lock:
        _cekajici[user["id"]] = {"replace": str(replace or "")[:16],
                                 "label": str(label or "")[:80], "t": time.time()}


def zrus(user):
    with _lock:
        _cekajici.pop(user["id"], None)


def vyzvedni_meta(user):
    """K čemu je právě vyzvednutý token ({"replace", "label"}); jednorázové."""
    with _lock:
        meta = _cekajici.pop(user["id"], None) or {}
    if meta and time.time() - meta.get("t", 0) > CEKANI:
        meta = {}
    return {"replace": meta.get("replace", ""), "label": meta.get("label", "")}
