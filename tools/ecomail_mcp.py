#!/usr/bin/env python3
"""
Ecomail MCP with fixes — `python3 tools/ecomail_mcp.py npx -y ecomail-mcp@1.0.0`.

A stdio proxy in front of the official server (ecomail-mcp 1.0.0, the only
version on npm). Every JSON-RPC message passes through unchanged, except for
the gaps that kept Claude from building a newsletter "like last time"
(found 2. 11. 2026):

* `get-campaign` drops `template_id`, yet a campaign's HTML lives only in its
  template (the API's campaign detail has no `html_text`, the archive URL is
  rendered by JavaScript). The proxy appends the template ID and the
  recipients (lists and segments), and adds the tool `get-campaign-html`.
* `create-campaign` only takes whole lists. Campaigns sent to a segment
  (Brno, Olomouc…) would go to the whole list. A `segments` argument is added
  and such a campaign is created by the proxy itself.
* `update-campaign` sends only the given fields, and the API refuses that
  ("The title field is required."). The proxy loads the campaign and sends it
  back whole with the changes, `segments` included.

The API key comes from ECOMAIL_API_KEY like for the server itself. Standard
library only, so it runs wherever the hub runs (PC, space, gateway sandbox).
"""
import json
import os
import shutil
import subprocess
import sys
import threading

API = "https://api2.ecomailapp.cz"
KEY = os.environ.get("ECOMAIL_API_KEY", "")
TIMEOUT = 60

_out_lock = threading.Lock()
_pending = {}                 # request id → what to do with the reply
_pending_lock = threading.Lock()

SEGMENTS_SCHEMA = {
    "type": "array",
    "description": "Segments to send to instead of whole lists, e.g. "
                   "[{\"id\": \"69dc2782069ae\", \"list\": 1}]. Segment IDs come from "
                   "get-segments; a previous campaign's segments are shown by get-campaign.",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Segment ID"},
            "list": {"type": "number", "description": "ID of the list the segment belongs to"},
        },
        "required": ["id", "list"],
    },
}

HTML_TOOL = {
    "name": "get-campaign-html",
    "title": "Get Campaign HTML",
    "description": "Return the full HTML of a campaign (sent or draft) — the email exactly "
                   "as it was built. Use it to make a new campaign in the same style as a "
                   "previous one.",
    "inputSchema": {
        "type": "object",
        "properties": {"campaign_id": {"type": "number", "description": "Campaign ID"}},
        "required": ["campaign_id"],
    },
}


# ── Ecomail API ──────────────────────────────────────────────────────────────
def _api(method, path, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"key": KEY, "Content-Type": "application/json",
                 "User-Agent": "claude-code-hub-ecomail"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            detail = data.get("message") or data.get("errors") or raw
        except ValueError:
            detail = raw
        if isinstance(detail, dict):
            detail = "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}"
                               for k, v in detail.items())
        raise RuntimeError(f"Ecomail API error ({exc.code}): {str(detail)[:500]}") from None
    return json.loads(text) if text.strip() else {}


def _campaign(cid):
    data = _api("GET", f"/campaigns/{int(cid)}")
    return data.get("campaign", data) if isinstance(data, dict) else {}


_names_cache = {"at": 0.0, "names": {}}


def _segment_names():
    """{segment id: name} across all lists — best effort, cached for 5 minutes."""
    import time
    if time.time() - _names_cache["at"] < 300:
        return _names_cache["names"]
    names = {}
    try:
        lists = _api("GET", "/lists")
        ids = [l.get("id") for l in (lists if isinstance(lists, list) else [])]
    except Exception:
        return names
    for lid in ids[:20]:
        try:
            segs = _api("GET", f"/lists/{lid}/segments").get("segments") or {}
        except Exception:
            continue
        for s in (segs.values() if isinstance(segs, dict) else segs):
            if isinstance(s, dict) and s.get("id"):
                names[str(s["id"])] = str(s.get("name") or "")
    _names_cache.update(at=time.time(), names=names)
    return names


def _recipients_text(rl, names=None):
    if isinstance(rl, dict) and rl.get("segments"):
        names = names if names is not None else _segment_names()
        parts = []
        for s in rl["segments"]:
            if not isinstance(s, dict):
                continue
            label = names.get(str(s.get("id")), "")
            excl = " (excluded)" if s.get("is_exclusionary") else ""
            parts.append(f"{label or '?'} (ID: {s.get('id')}, list {s.get('list')}){excl}")
        return "segments " + ", ".join(parts)
    if isinstance(rl, list) and rl:
        return "lists " + ", ".join(str(x.get("id", x) if isinstance(x, dict) else x)
                                    for x in rl)
    return "not set"


def _segments_arg(value):
    if not isinstance(value, list) or not value:
        raise ValueError("segments must be a non-empty array of {id, list}.")
    out = []
    for s in value:
        if not isinstance(s, dict) or not str(s.get("id") or "").strip() or s.get("list") is None:
            raise ValueError("Every segment needs an id (string) and a list (number).")
        out.append({"id": str(s["id"]).strip(), "list": int(s["list"])})
    return {"segments": out}


# ── our tools ────────────────────────────────────────────────────────────────
def tool_get_campaign_html(args):
    c = _campaign(args["campaign_id"])
    tid = c.get("template_id")
    if not tid:
        raise RuntimeError("This campaign has no template, so Ecomail has no HTML for it.")
    t = _api("GET", f"/templates/{int(tid)}")
    t = t.get("template", t) if isinstance(t, dict) else {}
    html = t.get("html") or ""
    if not html:
        raise RuntimeError(f"Template {tid} of this campaign is empty.")
    return (f"Campaign: {c.get('title')} (ID: {c.get('id')})\nSubject: {c.get('subject')}\n"
            f"Template ID: {tid}\nRecipients: {_recipients_text(c.get('recepient_lists'))}\n"
            f"\n--- HTML ({len(html)} characters) ---\n{html}\n")


FIELDS = ("title", "subject", "from_name", "from_email", "reply_to")


def tool_create_campaign(args):
    body = {k: args[k] for k in (*FIELDS, "html_text") if args.get(k) not in (None, "")}
    missing = [k for k in (*FIELDS, "html_text") if k not in body]
    if missing:
        raise ValueError("Missing: " + ", ".join(missing))
    body["recepient_lists"] = _segments_arg(args.get("segments"))
    made = _api("POST", "/campaigns", body)
    made = made.get("campaign", made) if isinstance(made, dict) else {}
    cid = made.get("id")
    check = _campaign(cid) if cid else made
    return (f"Campaign created successfully!\nID: {cid}\nTitle: {check.get('title')}\n"
            f"Subject: {check.get('subject')}\nStatus: Draft\n"
            f"Recipients: {_recipients_text(check.get('recepient_lists'))}")


def tool_update_campaign(args):
    cid = int(args["campaign_id"])
    cur = _campaign(cid)
    if cur.get("status") not in (0, None):
        raise RuntimeError("Only draft campaigns can be updated (this one is not a draft).")
    body = {k: cur.get(k) for k in FIELDS if cur.get(k) not in (None, "")}
    for k in (*FIELDS, "html_text"):
        if args.get(k) not in (None, ""):
            body[k] = args[k]
    if args.get("segments") is not None:
        body["recepient_lists"] = _segments_arg(args["segments"])
    elif args.get("recepient_lists") is not None:
        body["recepient_lists"] = args["recepient_lists"]
    _api("PUT", f"/campaigns/{cid}", body)
    check = _campaign(cid)
    return (f"Campaign {cid} updated.\nTitle: {check.get('title')}\n"
            f"Subject: {check.get('subject')}\n"
            f"Recipients: {_recipients_text(check.get('recepient_lists'))}")


def _local_tool(name, args):
    """Tools the proxy answers itself; None = pass to the real server."""
    if name == "get-campaign-html":
        return tool_get_campaign_html
    if name == "create-campaign" and args.get("segments") is not None:
        return tool_create_campaign
    if name == "update-campaign":
        return tool_update_campaign
    return None


# ── rewriting the real server's replies ──────────────────────────────────────
def _patch_tools_list(result):
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    for t in tools:
        if not isinstance(t, dict):
            continue
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else None
        if t.get("name") == "get-campaign":
            t["description"] = (t.get("description") or "") + \
                " Also shows the campaign's Template ID and recipients (lists or segments); " \
                "the campaign's HTML is returned by get-campaign-html."
        elif t.get("name") == "create-campaign" and schema:
            schema.setdefault("properties", {})["segments"] = SEGMENTS_SCHEMA
            schema["required"] = [r for r in schema.get("required") or []
                                  if r != "recepient_lists"]
            t["description"] = (t.get("description") or "") + \
                " Pass either recepient_lists (whole lists) or segments (send only to " \
                "segments, like previous campaigns that went to a segment)."
        elif t.get("name") == "update-campaign" and schema:
            schema.setdefault("properties", {})["segments"] = SEGMENTS_SCHEMA
            t["description"] = (t.get("description") or "") + \
                " Fields that are not given keep their current value; segments replaces " \
                "the recipients with those segments."
    if not any(isinstance(t, dict) and t.get("name") == HTML_TOOL["name"] for t in tools):
        tools.append(HTML_TOOL)
    return result


def _patch_get_campaign(result, args):
    if not isinstance(result, dict) or result.get("isError"):
        return result
    try:
        c = _campaign(args["campaign_id"])
        extra = (f"Template ID: {c.get('template_id') or 'none'}"
                 f" (HTML: get-campaign-html)\n"
                 f"Recipients: {_recipients_text(c.get('recepient_lists'))}\n")
    except Exception as exc:
        extra = f"(Template ID unavailable: {exc})\n"
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict) \
            and content[0].get("type") == "text":
        content[0]["text"] = str(content[0].get("text") or "") + extra
    return result


# ── plumbing ─────────────────────────────────────────────────────────────────
def _write(msg):
    data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _write_patched(msg, args):
    try:
        msg["result"] = _patch_get_campaign(msg["result"], args)
    except Exception:
        pass
    _write(msg)


def _key(mid):
    return json.dumps(mid, sort_keys=True)


def _answer_locally(mid, fn, args):
    try:
        text = fn(args)
        result = {"content": [{"type": "text", "text": text}]}
    except Exception as exc:
        result = {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
    _write({"jsonrpc": "2.0", "id": mid, "result": result})


def _from_child(child):
    for raw in child.stdout:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue                       # not JSON-RPC, Claude would choke on it
        if isinstance(msg, dict) and "id" in msg and "method" not in msg:
            with _pending_lock:
                todo = _pending.pop(_key(msg["id"]), None)
            if todo and isinstance(msg.get("result"), dict):
                if todo[0] == "get-campaign":
                    # Extra API calls — off this thread, other replies keep flowing.
                    threading.Thread(target=_write_patched, args=(msg, todo[1]),
                                     daemon=True).start()
                    continue
                try:
                    msg["result"] = _patch_tools_list(msg["result"])
                except Exception:
                    pass                   # an unpatched reply beats no reply
        _write(msg)
    os._exit(child.wait() or 0)


def _to_child(child, msg):
    child.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    child.stdin.flush()


def main(argv):
    if not argv:
        sys.stderr.write("usage: ecomail_mcp.py <command of the real server…>\n")
        return 2
    exe = shutil.which(argv[0]) or argv[0]          # npx.cmd on Windows
    child = subprocess.Popen([exe, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    threading.Thread(target=_from_child, args=(child,), daemon=True).start()
    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg:
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            fn = _local_tool(name, args) if isinstance(args, dict) else None
            if fn:
                threading.Thread(target=_answer_locally, args=(msg["id"], fn, args),
                                 daemon=True).start()
                continue
            if name == "get-campaign":
                with _pending_lock:
                    _pending[_key(msg["id"])] = ("get-campaign", args)
        elif isinstance(msg, dict) and msg.get("method") == "tools/list" and "id" in msg:
            with _pending_lock:
                _pending[_key(msg["id"])] = ("list", None)
        try:
            _to_child(child, msg)
        except OSError:
            break
    try:
        child.stdin.close()
    except OSError:
        pass
    return child.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
