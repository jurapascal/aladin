"""
Nastavení brány na jednom místě. Všechno jde přebít proměnnou prostředí, takže
systemd unit i test si řeknou o svoje bez zásahu do kódu.
"""
import os

# Kam se brána instaluje a kde má stav (databáze účtů).
GATEWAY_DIR = os.environ.get("HUB_GW_DIR", "/home/hub/gateway")
DB_PATH = os.environ.get("HUB_GW_DB", os.path.join(GATEWAY_DIR, "accounts.db"))

# Na čem brána poslouchá. Výchozí je loopback: TLS terminuje nginx před ní,
# takže brána sama nikdy nestojí holá na síti.
HOST = os.environ.get("HUB_GW_HOST", "127.0.0.1")
PORT = int(os.environ.get("HUB_GW_PORT", "8800"))

# Čím se pouští session uživatele. Na serveru vždy `bwrap` — `none` si brána
# dovolí jen na loopbacku bez nginx, viz isolation.check().
ISOLATION = os.environ.get("HUB_GW_ISOLATION", "bwrap")

# Cookie s přihlašovacím tokenem. Token sám je v databázi jen jako otisk.
SESSION_COOKIE = "gw_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 90      # 90 dní od posledního použití (posuvná, server.py _proxy)
# Dvoufázové ověření: po heslu kód z aplikace v mobilu (gateway/totp.py). Kdo ho
# ještě nemá, nastaví si ho při přihlášení — bez něj se dál nedostane.
REQUIRE_2FA = os.environ.get("HUB_GW_REQUIRE_2FA", "1") != "0"
# Hádání hesel a kódů (heslo, kód z aplikace, současné heslo při změně): po
# LOGIN_TRIES neúspěších na tentýž účet z téže adresy se přihlášení na LOGIN_LOCK
# sekund zamkne. LOGIN_WIDE je pojistka proti rozesílání — tolik neúspěchů
# z jedné adresy na různé účty, nebo na jeden účet z různých adres. Přísný limit
# platí pro účet a adresu dohromady, ne pro samotnou adresu: z kanceláře za
# jednou veřejnou IP by jinak tři překlepy jednoho zamkly všechny. Zámky leží v databázi účtů, restart je nezruší;
# zruší je `claude-hub-admin zamky odemknout <e-mail|ip>`.
LOGIN_TRIES = int(os.environ.get("HUB_GW_LOGIN_TRIES", "3"))
LOGIN_LOCK = int(os.environ.get("HUB_GW_LOGIN_LOCK", str(60 * 60)))
LOGIN_WIDE = int(os.environ.get("HUB_GW_LOGIN_WIDE", "10"))

# Kolik instancí hubu smí běžet naráz a kdy uspat nečinnou. Změřeno v README
# brány: na 8GB stroj s weby a mailem se vejdou realisticky čtyři.
def _default_max_sessions():
    """Kolik prostorů smí běžet naráz, když to není nastavené: podle paměti
    stroje, nejmíň 4. Nečinný prostor bere kolem 20 MB, s Claude Code stovky;
    600 MB na prostor nechává rezervu pro weby a databáze vedle. Pevné 4
    nestačily — s víc lidmi s otevřeným hubem se prostory navzájem uspávaly."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return max(4, int(line.split()[1]) // 1024 // 600)
    except (OSError, ValueError, IndexError):
        pass
    return 4


MAX_SESSIONS = int(os.environ.get("HUB_GW_MAX_SESSIONS") or _default_max_sessions())
IDLE_SLEEP = int(os.environ.get("HUB_GW_IDLE_SLEEP", str(30 * 60)))

# Klíč API Anthropicu pro prostory na `central` (claude-hub-admin apikey set).
# Čte ho jen uživatel hub (0600); do prostoru přijde jako ANTHROPIC_API_KEY.
API_KEY_FILE = os.environ.get("HUB_GW_API_KEY_FILE",
                              os.path.join(GATEWAY_DIR, "anthropic-api-key"))
# Klíče po účtech (claude-hub-admin apikey set --user). Soubor se jmenuje podle
# čísla účtu, ne podle e-mailu — e-mail se do cesty nedostane. Kdo svůj klíč
# nemá, dostane společný z API_KEY_FILE. Vlastní klíč na účet je jediný způsob,
# jak dát každému svůj strop v Anthropic Console a jak zajistit, že kdo si klíč
# v prostoru přečte z prostředí, přečte jen ten svůj.
API_KEYS_DIR = os.environ.get("HUB_GW_API_KEYS_DIR",
                              os.path.join(GATEWAY_DIR, "api-keys"))

# Vlastní předplatné Claude po účtech (token z `claude setup-token`, připojuje ho
# appka na počítači nebo `claude-hub-admin predplatne set`). Soubor podle čísla
# účtu, 0600 ve složce 0700; do prostoru přijde jako CLAUDE_CODE_OAUTH_TOKEN.
CLAUDE_TOKENS_DIR = os.environ.get("HUB_GW_CLAUDE_TOKENS_DIR",
                                   os.path.join(GATEWAY_DIR, "claude-predplatne"))

# Tlačítko Nahlásit chybu / Podat návrh v prostorech (Verxee Debug,
# hub/static/verxee.js): veřejný klíč `vxd_…` (claude-hub-admin verxee set).
# Do prostoru přijde v hub-config.json při jeho startu; bez souboru se
# tlačítko neukáže.
VERXEE_KLIC_FILE = os.environ.get("HUB_GW_VERXEE_KLIC_FILE",
                                  os.path.join(GATEWAY_DIR, "verxee-debug-klic"))

# Úkoly na později pro počítače uživatelů (gateway/pocitac.py): zadání a přílohy,
# které Claude z prostoru nechal počítači, dokud se nepřipojí. Složka podle čísla
# účtu, 0700; přílohy se smažou, jakmile si je počítač převezme.
POCITAC_UKOLY_DIR = os.environ.get("HUB_GW_POCITAC_UKOLY_DIR",
                                   os.path.join(GATEWAY_DIR, "pocitac-ukoly"))

# Klient OAuth pro napojení na Google (claude-hub-admin google set). Zakládá se
# jednou pro všechny; do prostorů přijde jako GOOGLE_OAUTH_CLIENT_ID/SECRET.
GOOGLE_CLIENT_FILE = os.environ.get("HUB_GW_GOOGLE_CLIENT_FILE",
                                    os.path.join(GATEWAY_DIR, "google-oauth-client.json"))
# Firemní Obsidian: jeden společný trezor pro všechny účty. V prostorech je jen
# ke čtení; zapisuje do něj brána, a to jen po potvrzení v hubu
# (workspace.publish_company). Záznam o nahráních leží vedle, mimo trezor.
COMPANY_DIR = os.environ.get("HUB_GW_COMPANY_DIR",
                             os.path.join(os.path.dirname(GATEWAY_DIR), "firma"))
COMPANY_VAULT = os.environ.get("HUB_GW_COMPANY_VAULT",
                               os.path.join(COMPANY_DIR, "Firemní Brain"))
COMPANY_LOG = os.path.join(COMPANY_DIR, "nahrano.jsonl")
# Firemní skilly: `<trezor>/skills/<kategorie>/<skill>/SKILL.md`. Leží ve
# firemním trezoru, takže je má každý prostor jen ke čtení jako zbytek trezoru
# a nikdo je nemá zvlášť u sebe. Zavádí a aktualizuje je claude-hub-admin
# skills; pokyny k nim dostane Claude v ~/.claude/CLAUDE.md prostoru.
COMPANY_SKILLS = os.path.join(COMPANY_VAULT, "skills")
# Další firmy (gateway/firmy.py): každá ve vlastní složce s trezorem `Brain`,
# registr firem, lidí a přístupů vedle nich. Dnešní firemní trezor zůstává,
# kde je (COMPANY_DIR) — je to firma `firma`.
FIRMY_DIR = os.environ.get("HUB_GW_FIRMY_DIR",
                           os.path.join(os.path.dirname(GATEWAY_DIR), "firmy"))
# Sdílené Obsidiany pro vybrané lidi (gateway/shared.py): každý ve vlastní složce,
# registr členů vedle nich.
SHARED_DIR = os.environ.get("HUB_GW_SHARED_DIR",
                            os.path.join(os.path.dirname(GATEWAY_DIR), "sdilene"))
