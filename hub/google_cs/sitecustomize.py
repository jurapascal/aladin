"""Czech OAuth callback page for workspace-mcp (Google sign-in).

workspace-mcp ends the Google sign-in on an English purple page. The hub puts
this directory on PYTHONPATH of that one server, so Python imports this module
at start-up and the page is replaced by a plain Czech one with a green check
mark. Everything is best effort: when workspace-mcp changes its layout, the
original page just stays.
"""
from html import escape as _esc

_OK = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Přihlášení hotové</title><style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f6f7f9;color:#1f2328}
@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}.k{background:#161b22;border-color:#30363d}.s{color:#8b949e}}
.k{max-width:380px;width:calc(100% - 32px);padding:36px 28px;text-align:center;background:#fff;
border:1px solid #e1e4e8;border-radius:20px;box-shadow:0 8px 30px rgba(0,0,0,.08)}
.c{width:84px;height:84px;margin:0 auto 20px;border-radius:50%;background:#2ea043;display:flex;
align-items:center;justify-content:center;animation:p .45s cubic-bezier(.2,1.3,.4,1)}
.c svg{width:44px;height:44px;stroke:#fff;stroke-width:5;fill:none;stroke-linecap:round;stroke-linejoin:round;
stroke-dasharray:60;stroke-dashoffset:60;animation:d .4s .25s ease forwards}
@keyframes p{from{transform:scale(.3);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes d{to{stroke-dashoffset:0}}
h1{font-size:22px;margin:0 0 8px}.s{margin:0;color:#59636e;font-size:15px;line-height:1.5;word-break:break-word}
</style></head><body><div class="k"><div class="c"><svg viewBox="0 0 48 48"><path d="M12 25l9 9 16-18"/></svg></div>
<h1>Přihlášení hotové</h1><p class="s">@@KDO@@ je připojený. Tohle okno můžeš zavřít a vrátit se do Aladina.</p></div>
<script>setTimeout(function(){try{window.close()}catch(e){}},2500)</script></body></html>"""

_ERR = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Přihlášení se nepovedlo</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f6f7f9;color:#1f2328}
.k{max-width:380px;width:calc(100% - 32px);padding:36px 28px;text-align:center;background:#fff;border:1px solid #e1e4e8;border-radius:20px}
.c{width:72px;height:72px;margin:0 auto 18px;border-radius:50%;background:#d1242f;color:#fff;font-size:40px;line-height:72px}
h1{font-size:21px;margin:0 0 8px}p{margin:0;color:#59636e;font-size:15px;line-height:1.5;word-break:break-word}</style></head>
<body><div class="k"><div class="c">!</div><h1>Přihlášení se nepovedlo</h1>
<p>@@ZPRAVA@@</p><p style="margin-top:10px">Zavři okno a zkus to v Aladinu znovu. Při přihlášení povol všechna požadovaná oprávnění.</p></div></body></html>"""


def _patch():
    try:
        from fastapi.responses import HTMLResponse
        import auth.oauth_responses as m
    except Exception:
        return

    def ok(verified_user_id=None):
        return HTMLResponse(content=_OK.replace("@@KDO@@", _esc(verified_user_id or "Google účet")))

    def err(error_message, status_code=400):
        return HTMLResponse(content=_ERR.replace("@@ZPRAVA@@", _esc(str(error_message))), status_code=status_code)

    m.create_success_response = ok
    m.create_error_response = err


_patch()
