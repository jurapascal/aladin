"""
Putting a window around the local UI.

The hub is a web app, but it should not look or feel like a browser tab. Three
hosts are tried in order, and the first one that works wins:

  1. a chromium-family browser in --app mode  (Windows: Edge is always there)
  2. WebKitGTK through PyGObject             (Linux, no browser needed at all)
  3. whatever the desktop's default browser is (last resort, plain tab)

Only WebKitGTK runs in-process and can tell us reliably when its window closed.
For a spawned browser the process is NOT a reliable signal — see _open_chromium —
so the launcher watches the page's websocket instead.
"""
import os
import sys
import shutil
import subprocess
import webbrowser

from . import core

PROFILE_DIR = os.path.join(core.CLAUDE_DIR, "hub-browser-profile")

BROWSERS_WINDOWS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
]
BROWSERS_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]
BROWSERS_UNIX = ["google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "brave-browser", "microsoft-edge", "vivaldi"]


def find_browser():
    """Path to a chromium-family browser that understands --app, or ''."""
    configured = core.CONFIG.get("browser", "")
    if configured:
        found = shutil.which(configured)
        if found:
            return found
        if os.path.isfile(configured):
            return configured
    if core.IS_WINDOWS:
        candidates = BROWSERS_WINDOWS
    elif core.IS_MAC:
        candidates = BROWSERS_MAC
    else:
        candidates = BROWSERS_UNIX
    for name in candidates:
        if os.path.isabs(name):
            if os.path.isfile(name):
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    return ""


def has_webkit():
    """(gtk_version, webkit_module) if WebKitGTK is importable here, else None."""
    if core.IS_WINDOWS or core.IS_MAC:
        return None
    try:
        import gi
    except ImportError:
        return None
    for gtk_ver, wk_ver, wk_name in (("4.0", "6.0", "WebKit"),
                                     ("3.0", "4.1", "WebKit2"),
                                     ("3.0", "4.0", "WebKit2")):
        try:
            gi.require_version("Gtk", gtk_ver)
            gi.require_version(wk_name, wk_ver)
            __import__("gi.repository", fromlist=["Gtk", wk_name])
            return gtk_ver, wk_name
        except Exception:
            continue  # a different version pair may already be pinned
    return None


def _open_chromium(browser, url):
    """Spawn the browser. We keep the handle, but its lifetime is NOT the hub's:
    a chromium launcher that hands the window to an already running process exits
    immediately, and treating that as 'window closed' would kill the server out
    from under a window the user is still looking at."""
    profile = PROFILE_DIR
    if not core.IS_WINDOWS and not core.IS_MAC and (browser.startswith("/snap/") or "/snap/" in os.path.realpath(browser)):
        # Snapový Chromium nesmí zapisovat do skrytých složek (~/.claude), okno
        # by se hned zavřelo. Snap povoluje jen ~/snap/<balíček>/common.
        profile = os.path.join(os.path.expanduser("~"), "snap", "chromium", "common", "hub-browser-profile")
    argv = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",   # a profile of our own, not the user's
        "--no-first-run",
        "--no-default-browser-check",
        # Sestavení „Chrome for Testing" (např. to od Playwrightu) jinak nad
        # appkou drží žlutý pruh, že se nemá používat na běžné prohlížení.
        # Okno hubu je jediná stránka, kterou tenhle profil kdy otevře.
        "--disable-infobars",
        "--window-size=1360,860",
    ]
    if not core.IS_WINDOWS and not core.IS_MAC:
        argv += ["--class=aladin", "--ozone-platform=x11"]  # matches StartupWMClass in the .desktop
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _open_webkit(url, versions):
    """Native GTK window with a WebKit view — no browser involved."""
    gtk_ver, wk_name = versions
    import gi
    gi.require_version("Gtk", gtk_ver)
    gi.require_version(wk_name, "6.0" if wk_name == "WebKit" else "4.1")
    from gi.repository import GLib, Gtk  # noqa: E402
    webkit = __import__("gi.repository", fromlist=[wk_name])
    WebKit = getattr(webkit, wk_name)

    # prgname is what Wayland/X11 map to claude-code-hub.desktop, which is where
    # the window gets its icon and its place in the dock.
    GLib.set_prgname("claude-code-hub")
    GLib.set_application_name("Aladin")

    view = WebKit.WebView()
    view.load_uri(url)

    # Požadavek na nové okno (tlačítko u odkazu z terminálu, když okno ukazuje
    # prostor na serveru) by WebKitGTK bez obsluhy tiše zahodil. Odkaz patří do
    # prohlížeče na tomhle počítači, ne do dalšího okna appky.
    def new_window(_view, action):
        uri = action.get_request().get_uri()
        if uri:
            core.open_path(uri)
        return None

    try:
        view.connect("create", new_window)
    except TypeError:
        pass  # jiná verze WebKitu — okno pojede i bez toho, jen bez odkazů ven

    _follow_theme(view, Gtk)
    _allow_microphone(view, WebKit, url)
    holder = {}                    # okno vzniká až níž, zpráva přijde později
    _follow_app(view, holder)

    if gtk_ver == "4.0":
        Gtk.init()
        window = Gtk.Window()
        holder["window"] = window
        window.set_default_size(1360, 860)
        window.set_title(_app_title())
        window.set_child(view)
        loop = GLib.MainLoop()
        window.connect("close-request", lambda *_: (loop.quit(), False)[1])
        window.present()
        return loop.run

    Gtk.init([])
    window = Gtk.Window(title=_app_title())
    holder["window"] = window
    window.set_default_size(1360, 860)
    icon = _app_icon()
    if icon:
        try:
            window.set_icon_from_file(icon)
        except Exception:
            pass
    window.add(view)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    return Gtk.main


def _follow_app(view, holder):
    """Uložený název a ikona appky (Nastavení → Vzhled) platí v okně hned,
    ne až po dalším spuštění: stránka pošle `hubApp` a okno si je přečte
    znovu. Obsah zprávy se nepoužije — název i ikonu bere okno z hubu."""
    manager = view.get_user_content_manager()

    def received(*_args):
        window = holder.get("window")
        if window is None:
            return
        try:
            window.set_title(_app_title())
            icon = _app_icon()
            if icon and hasattr(window, "set_icon_from_file"):
                window.set_icon_from_file(icon)
        except Exception:
            pass

    try:
        manager.connect("script-message-received::hubApp", received)
        try:
            manager.register_script_message_handler("hubApp")
        except TypeError:
            manager.register_script_message_handler("hubApp", None)   # WebKit 6
    except Exception:
        pass


def _app_title():
    """Vlastní název appky (Nastavení → Vzhled), jinak Aladin."""
    try:
        from . import vzhled
        vzhled.sync_desktop()      # instalace spouštěč přepisuje na výchozí
        return vzhled.zobrazeny()
    except Exception:
        return "Aladin"


def _app_icon():
    try:
        from . import vzhled
        custom = vzhled.ikona(512)
    except Exception:
        custom = ""
    if custom:
        return custom
    return core.ICON_PATH if core.ICON_PATH and os.path.isfile(core.ICON_PATH) else ""


def _allow_microphone(view, WebKit, url):
    """Mikrofon pro diktování (hlas.js). WebKitGTK bez obsluhy každou žádost
    o mikrofon tiše zamítne. Povoluje se jen mikrofon (ne kamera, ne poloha)
    a jen stránce hubu — okno nic jiného neotevírá, ale kdyby se tam přece
    jen něco dostalo (prostor na serveru je jiná adresa), zeptá se zamítnutím."""
    try:
        view.get_settings().set_enable_media_stream(True)
    except Exception:
        return                      # starý WebKit — diktování tu nepůjde

    from urllib.parse import urlsplit
    # Stránka hubu, a brána, kterou okno ukazuje jako prostor na serveru.
    allowed = {urlsplit(url).netloc}
    gw = str(core.CONFIG.get("gw_server") or "")
    if gw.startswith("https://"):
        allowed.add(urlsplit(gw).netloc)

    def request(_view, req):
        wanted = getattr(WebKit, "UserMediaPermissionRequest", None)
        if not (wanted and isinstance(req, wanted)):
            return False            # ostatní žádosti výchozí cestou (zamítnout)
        try:
            is_video = req.get_property("is-for-video-device")
        except Exception:
            is_video = True
        if is_video or urlsplit(view.get_uri() or "").netloc not in allowed:
            req.deny()
        else:
            req.allow()
        return True

    try:
        view.connect("permission-request", request)
    except TypeError:
        pass


def _follow_theme(view, Gtk):
    """GTK motiv okna podle motivu stránky.

    Rozbalený <select> kreslí WebKitGTK jako GTK nabídku, a ta se řídí motivem
    GTK, ne stránkou — v tmavém hubu se tak otevíral bílý seznam. Stránka při
    každé změně motivu pošle `hubTheme` ("dark"/"light", hub.js applyTheme)
    a okno si podle toho přepne preferenci tmavého motivu. Jiná zpráva než
    tyhle dvě se zahodí.
    """
    manager = view.get_user_content_manager()

    def received(_manager, message):
        # WebKit2 4.x posílá JavascriptResult, WebKit 6 rovnou JSCValue.
        value = message.get_js_value() if hasattr(message, "get_js_value") else message
        try:
            theme = value.to_string()
        except Exception:
            return
        if theme not in ("dark", "light"):
            return
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", theme == "dark")

    try:
        manager.connect("script-message-received::hubTheme", received)
        try:
            manager.register_script_message_handler("hubTheme")
        except TypeError:
            manager.register_script_message_handler("hubTheme", None)   # WebKit 6
    except Exception:
        pass  # starší WebKit — seznam zůstane podle systému, stránka jede dál


def zavri_okno(base):
    """Zavře okno appky (Chromium `--app=<base>…`), které patří téhle instanci.
    Po aktualizaci by jinak zůstalo staré okno vedle nového — a ukazovalo by
    na server, který už neběží. Jen to, co spustil hub; ostatní okna prohlížeče
    nechává být."""
    if not base:
        return 0
    base = base.split("/?")[0].rstrip("/")
    needle = f"--app={base}"
    killed = 0
    try:
        if sys.platform == "win32":
            cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + needle +
                   "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return 1
        out = subprocess.run(["pgrep", "-f", "--", needle], capture_output=True, text=True, timeout=5).stdout
        for pid in out.split():
            if int(pid) != os.getpid():
                try:
                    os.kill(int(pid), 15)
                    killed += 1
                except OSError:
                    pass
    except Exception:
        pass
    return killed


def open_window(url, prefer="", appka_ok=True):
    """Open the hub UI.

    Returns (host_name, proc, blocking_wait). Exactly one of the last two is set:
    `blocking_wait` is an in-process loop that owns the window's lifetime
    (WebKitGTK), otherwise `proc` is the browser we spawned — which may or may not
    outlive the window, so the caller must watch the page instead.
    """
    # Samostatná appka (hub/appka.py), když je nainstalovaná — má přednost před
    # Chromem i před starší volbou "window". Jen pro hub na tomhle počítači:
    # prostor na serveru jde dál do prohlížeče.
    if appka_ok and url.startswith("http://127.0.0.1:"):
        from . import appka
        if appka.zapnuta() and appka.spustitelny():
            try:
                return "appka", appka.spust(url), None
            except OSError as exc:
                core.log(f"appka: nejde spustit ({exc})", "warn")
    order = ["chromium", "webkit", "browser"]
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)

    for host in order:
        if host == "chromium":
            browser = find_browser()
            if browser:
                return os.path.basename(browser), _open_chromium(browser, url), None
        elif host == "webkit":
            versions = has_webkit()
            if versions:
                try:
                    return "webkitgtk", None, _open_webkit(url, versions)
                except Exception:
                    continue  # broken WebKit install → next host
    webbrowser.open(url)
    return "browser", None, None  # launcher watches the page
