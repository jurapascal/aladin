"""
Ikonka v oznamovací oblasti (tray) — appka je vidět, i když má zavřené okno.

Samostatný proces, který pouští claude-hub.py a který skončí s ním:

* Linux: StatusNotifierItem přes D-Bus (Gio z PyGObject, které má appka kvůli
  oknu WebKitGTK). Nabídka přes com.canonical.dbusmenu. Ukazuje ji rozšíření
  AppIndicator v GNOME/Zorinu, KDE, Xfce, Cinnamon… Žádná knihovna navíc
  (libayatana-appindicator) potřeba není.
* Windows: NotifyIcon z .NET přes PowerShell (skrytě).

Klik na ikonku otevře okno appky (POST /api/okno-otevrit), v nabídce je
Otevřít a Ukončit (POST /api/quit). Ukončit se napřed zeptá — zavřou se
všechny chaty (uloží se a příště vrátí) a utnula by se práce, která běží.

    python3 -m hub.tray <adresa s ?t=token> <pid appky> <ikona.png>
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

NAZEV = "Aladin"


def _api(base, name):
    u = urllib.parse.urlsplit(base)
    q = urllib.parse.parse_qs(u.query)
    url = f"{u.scheme}://{u.netloc}/api/{name}?t={urllib.parse.quote((q.get('t') or [''])[0])}"
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read() or b"{}")
    except Exception:
        return {}


def _otazka_konec(base):
    """Text dotazu před ukončením: kolik chatů se zavře a jestli se něco utne."""
    info = _api(base, "konec-info")
    taby, pracuje = int(info.get("taby") or 0), int(info.get("pracuje") or 0)
    if not taby:
        return f"Ukončit {NAZEV}?"
    slovo = "chat" if taby == 1 else "chaty" if taby < 5 else "chatů"
    text = f"Ukončit {NAZEV}? Zavře se {taby} {slovo}."
    if pracuje:
        text += (f" Claude teď pracuje v {pracuje} z nich — rozdělaná práce se přeruší."
                 if taby > 1 else " Claude v něm teď pracuje — rozdělaná práce se přeruší.")
    return text + "\n\nVšechno se uloží a po dalším spuštění se chaty vrátí."


def _potvrd_konec_linux(base):
    """True = ukončit. Dialog přes zenity / kdialog, jinak okénko GTK; bez
    čehokoli z toho se ukončí bez ptaní (jako dřív)."""
    text = _otazka_konec(base)
    try:
        if shutil.which("zenity"):
            return subprocess.run(["zenity", "--question", "--title", NAZEV, "--text", text,
                                   "--ok-label", "Ukončit", "--cancel-label", "Zrušit",
                                   "--icon-name", "dialog-warning", "--width", "380"]).returncode == 0
        if shutil.which("kdialog"):
            return subprocess.run(["kdialog", "--title", NAZEV, "--yesno", text,
                                   "--yes-label", "Ukončit", "--no-label", "Zrušit"]).returncode == 0
        kod = ("import gi,sys;gi.require_version('Gtk','3.0');from gi.repository import Gtk;"
               "d=Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,text=sys.argv[1]);"
               "d.set_title(sys.argv[2]);d.add_buttons('Zrušit',0,'Ukončit',1);"
               "sys.exit(0 if d.run()==1 else 1)")
        return subprocess.run([sys.executable, "-c", kod, text, NAZEV], timeout=600).returncode == 0
    except Exception:
        return True


def _zije(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ── Linux: StatusNotifierItem ────────────────────────────────────────────────
SNI_XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewStatus"><arg type="s"/></signal>
 </interface>
</node>"""

MENU_XML = """<node>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
   <arg type="as" name="propertyNames" direction="in"/>
   <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
   <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
   <arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
   <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup">
   <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <method name="AboutToShow"><arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/></method>
  <method name="AboutToShowGroup">
   <arg type="ai" name="ids" direction="in"/><arg type="ai" name="updatesNeeded" direction="out"/>
   <arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
 </interface>
</node>"""


def _pixmapy(GdkPixbuf, GLib, ikona):
    """PNG → IconPixmap: ARGB32 v síťovém pořadí bajtů, víc velikostí."""
    out = []
    for size in (22, 32, 48, 64):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(ikona, size, size)
        except GLib.Error:
            continue
        if not pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0)
        w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
        px = pb.get_pixels()
        data = bytearray()
        for y in range(h):
            row = px[y * rs:y * rs + w * 4]
            for x in range(0, w * 4, 4):
                r, g, b, a = row[x], row[x + 1], row[x + 2], row[x + 3]
                data += bytes((a, r, g, b))
        out.append((w, h, bytes(data)))
    return out


def linux(base, ppid, ikona):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, Gio, GLib

    loop = GLib.MainLoop()
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    jmeno = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
    pix = _pixmapy(GdkPixbuf, GLib, ikona)
    pix_v = GLib.Variant("a(iiay)", pix)
    ikony_dir = os.path.dirname(os.path.abspath(ikona))
    polozky = {1: "Otevřít " + NAZEV, 2: None, 3: "Ukončit appku"}

    def otevri():
        _api(base, "okno-otevrit")

    def ukonci():
        # Dialog čeká na člověka — mimo smyčku, ať ikonka mezitím odpovídá.
        def jed():
            if not _potvrd_konec_linux(base):
                return
            _api(base, "quit")
            GLib.timeout_add(2500, lambda: loop.quit() or False)
        threading.Thread(target=jed, daemon=True).start()

    sni_props = {
        "Category": GLib.Variant("s", "ApplicationStatus"),
        "Id": GLib.Variant("s", "claude-code-hub"),
        "Title": GLib.Variant("s", NAZEV),
        "Status": GLib.Variant("s", "Active"),
        "WindowId": GLib.Variant("i", 0),
        "IconName": GLib.Variant("s", ""),
        "IconPixmap": pix_v,
        "IconThemePath": GLib.Variant("s", ikony_dir),
        "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], NAZEV, "Běží — klikni pro otevření okna")),
        "ItemIsMenu": GLib.Variant("b", False),
        "Menu": GLib.Variant("o", "/MenuBar"),
    }

    def sni_call(conn, sender, path, iface, method, params, inv):
        if method == "Activate" or method == "SecondaryActivate":
            GLib.idle_add(lambda: otevri() or False)
        inv.return_value(None)

    def sni_get(conn, sender, path, iface, prop):
        return sni_props.get(prop)

    def props(i):
        if polozky[i] is None:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", polozky[i]), "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True)}

    def layout():
        deti = [GLib.Variant("(ia{sv}av)", (i, props(i), [])) for i in polozky]
        return (1, (0, {"children-display": GLib.Variant("s", "submenu")}, deti))

    def menu_call(conn, sender, path, iface, method, params, inv):
        if method == "GetLayout":
            inv.return_value(GLib.Variant("(u(ia{sv}av))", layout()))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or list(polozky)
            inv.return_value(GLib.Variant("(a(ia{sv}))", ([(i, props(i)) for i in ids if i in polozky],)))
        elif method == "GetProperty":
            i, name = params.unpack()
            inv.return_value(GLib.Variant("(v)", (props(i).get(name, GLib.Variant("s", "")),)))
        elif method in ("Event", "EventGroup"):
            udalosti = [params.unpack()[:2]] if method == "Event" else \
                [(e[0], e[1]) for e in params.unpack()[0]]
            for i, ev in udalosti:
                if ev == "clicked":
                    GLib.idle_add(lambda i=i: (otevri() if i == 1 else ukonci() if i == 3 else None) or False)
            inv.return_value(None if method == "Event" else GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_value(None)

    menu_props = {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                  "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}

    def menu_get(conn, sender, path, iface, prop):
        return menu_props.get(prop)

    sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
    menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
    bus.register_object("/StatusNotifierItem", sni_info, sni_call, sni_get, None)
    bus.register_object("/MenuBar", menu_info, menu_call, menu_get, None)

    def registruj(*_a):
        try:
            bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                          "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                          GLib.Variant("(s)", (jmeno,)), None, Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error as exc:
            print("tray: watcher nepřijal ikonku:", exc.message, file=sys.stderr)

    Gio.bus_own_name_on_connection(bus, jmeno, Gio.BusNameOwnerFlags.NONE, registruj, None)
    # Panel (watcher) se restartoval (třeba po odhlášení rozšíření) → znovu.
    Gio.bus_watch_name_on_connection(bus, "org.kde.StatusNotifierWatcher",
                                     Gio.BusNameWatcherFlags.NONE, registruj, None)

    def hlidej():
        if not _zije(ppid):
            loop.quit()
            return False
        return True
    GLib.timeout_add_seconds(2, hlidej)
    loop.run()


# ── Windows: NotifyIcon ──────────────────────────────────────────────────────
PS = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$base = $env:HUB_TRAY_URL; $ppid_ = [int]$env:HUB_TRAY_PID
function Hub($name) {
  $u = [Uri]$base; $t = [Web.HttpUtility]::ParseQueryString($u.Query)['t']
  try { return Invoke-RestMethod -Method Post -ContentType 'application/json' -Body '{}' `
        -Uri ("{0}://{1}/api/{2}?t={3}" -f $u.Scheme, $u.Authority, $name, [Uri]::EscapeDataString($t)) } catch { return $null }
}
function Konec {
  $i = Hub 'konec-info'; $n = [int]$i.taby; $p = [int]$i.pracuje
  $txt = "Ukončit $($env:HUB_TRAY_NAME)?"
  if ($n -gt 0) {
    $txt += " Zavře se chatů: $n."
    if ($p -gt 0) { $txt += " Claude teď pracuje v $p z nich — rozdělaná práce se přeruší." }
    $txt += "`n`nVšechno se uloží a po dalším spuštění se chaty vrátí."
  }
  $r = [System.Windows.Forms.MessageBox]::Show($txt, $env:HUB_TRAY_NAME, 'OKCancel', 'Question')
  if ($r -ne 'OK') { return }
  Hub 'quit' | Out-Null; Start-Sleep -Milliseconds 1500; $ni.Visible = $false; [System.Windows.Forms.Application]::Exit()
}
Add-Type -AssemblyName System.Web
$ni = New-Object System.Windows.Forms.NotifyIcon
$bmp = [System.Drawing.Bitmap]::FromFile($env:HUB_TRAY_ICON)
$ni.Icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
$ni.Text = $env:HUB_TRAY_NAME
$m = New-Object System.Windows.Forms.ContextMenuStrip
$m.Items.Add('Otevřít ' + $env:HUB_TRAY_NAME, $null, { Hub 'okno-otevrit' }) | Out-Null
$m.Items.Add('-') | Out-Null
$m.Items.Add('Ukončit appku', $null, { Konec }) | Out-Null
$ni.ContextMenuStrip = $m
$ni.add_MouseClick({ param($s, $e) if ($e.Button -eq 'Left') { Hub 'okno-otevrit' } })
$ni.Visible = $true
$tm = New-Object System.Windows.Forms.Timer; $tm.Interval = 2000
$tm.add_Tick({ if (-not (Get-Process -Id $ppid_ -ErrorAction SilentlyContinue)) { $ni.Visible = $false; [System.Windows.Forms.Application]::Exit() } })
$tm.Start()
[System.Windows.Forms.Application]::Run()
"""


def windows(base, ppid, ikona):
    env = dict(os.environ, HUB_TRAY_URL=base, HUB_TRAY_PID=str(ppid), HUB_TRAY_ICON=ikona, HUB_TRAY_NAME=NAZEV)
    subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                    "-Command", PS], env=env, creationflags=0x08000000)


# ── spuštění z appky ─────────────────────────────────────────────────────────
def spust(url):
    """Pustí ikonku jako samostatný proces (z claude-hub.py). Tichý neúspěch:
    bez D-Bus / panelu se nic neukáže, appka běží dál."""
    from . import core
    if core.TEST_MODE or core.on_gateway() or sys.platform == "darwin":
        return None
    if not core.CONFIG.get("tray", True):
        return None
    from . import vzhled
    ikona = vzhled.ikona(512) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icon-256.png")
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": open(os.path.join(core.CLAUDE_DIR, "hub-tray.log"), "a")}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000                  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        return subprocess.Popen([sys.executable, "-m", "hub.tray", url, str(os.getpid()), ikona,
                                 vzhled.zobrazeny()],
                                cwd=root, **kw)
    except OSError as exc:
        core.log(f"tray: nespuštěno ({exc})", "warn")
        return None


if __name__ == "__main__":
    base, ppid, ikona = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    if len(sys.argv) > 4 and sys.argv[4].strip():
        NAZEV = sys.argv[4].strip()[:40]
    try:
        (windows if sys.platform == "win32" else linux)(base, ppid, ikona)
    except Exception as exc:                               # bez panelu / D-Bus
        print("tray:", repr(exc), file=sys.stderr)
        sys.exit(1)
