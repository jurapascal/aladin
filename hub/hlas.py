"""
Hlas: diktování (řeč → text), předčítání odpovědí (text → řeč) a hovor
s Claudem — jen přes ElevenLabs (hub/eleven.py), vlastním klíčem člověka.

Kdo ElevenLabs napojený nemá (Nastavení → Hlas), mikrofon ani předčítání
nevidí. Místní Whisper a Piper (do 2.149.0 v ~/.claude/hlas a na serveru
v /usr/local/lib/claude-hub-hlas, ~2,5 GB) jsou pryč: `uklid()` stará data
na počítači smaže, na serveru je smaže gateway/install.sh.
"""
import os
import shutil
import threading

from . import core

LOCAL_DIR = os.path.join(core.CLAUDE_DIR, "hlas")
MAX_AUDIO = 20 * 1024 * 1024        # nahrávka z prohlížeče (webm/ogg/mp4/wav) — desítky minut
MIN_AUDIO = 1000                     # kratší nahrávka je ticho nebo cvaknutí
MAX_TEXT = 6000                      # znaků na jedno předčítání


def check_audio(data):
    if not data or len(data) < MIN_AUDIO:
        raise ValueError("Nahrávka je moc krátká.")
    if len(data) > MAX_AUDIO:
        raise ValueError("Nahrávka je moc dlouhá — rozděl ji na kratší kusy.")
    return data


def _smaz_stare():
    if os.path.isdir(LOCAL_DIR) and os.path.isfile(os.path.join(LOCAL_DIR, "venv", "pyvenv.cfg")) \
            or os.path.isdir(os.path.join(LOCAL_DIR, "modely")):
        shutil.rmtree(LOCAL_DIR, ignore_errors=True)
        core.log("hlas: smazán starý místní Whisper a Piper (~/.claude/hlas) — hlas je jen přes ElevenLabs")


def uklid():
    """Při startu hubu na pozadí: pryč se starou instalací Whisperu a Piperu."""
    if core.TEST_MODE:
        return
    threading.Thread(target=_smaz_stare, name="hlas-uklid", daemon=True).start()
