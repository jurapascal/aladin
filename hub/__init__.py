"""Claude Code Hub."""

import os
import subprocess

# Jediné místo, kde se čísluje verze. Aktualizace v aplikaci porovnává tohle
# s nejnovější značkou na GitHubu, takže se to musí zvyšovat spolu s vydáním.
__version__ = "2.137.6"


# Windows: podproces bez příznaku „bez okna“ si vezme vlastní konzoli a appka
# (pythonw.exe, bez konzole) ji ukáže jako černé okno cmd. Platí hlavně pro
# .cmd skripty (npx.cmd, claude.CMD) — ty CreateProcess pouští přes cmd.exe.
#
# Hlídání bylo v hub/core.py, ale to je jen jeden modul z mnoha. Mosty MCP
# v tools/ si berou `from hub import prohlizec` a core.py se jim naimportuje
# teprve uvnitř funkce (prohlizec.py: `from . import core`), takže v jejich
# procesu hlídání neplatilo. Playwright MCP tak otevíral okno při každém novém
# chatu (Tomáš, 9. 11. 2026). Tady v __init__.py projde import kterékoli části
# hubu — a tím i každý most.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

if _NO_WINDOW and not getattr(subprocess.Popen.__init__, "_hub_bez_okna", False):
    _popen_init = subprocess.Popen.__init__

    def _popen_bez_okna(self, *args, **kwargs):
        # Kdo si vyslovil vlastní příznaky (odpojený proces, nová konzole),
        # tomu se nesahá.
        if not kwargs.get("creationflags") and not kwargs.get("startupinfo"):
            kwargs["creationflags"] = _NO_WINDOW
        _popen_init(self, *args, **kwargs)

    _popen_bez_okna._hub_bez_okna = True      # ať se hlídání nenavrství dvakrát
    subprocess.Popen.__init__ = _popen_bez_okna
