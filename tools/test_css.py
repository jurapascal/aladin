#!/usr/bin/env python3
"""Zkouška stylů: každý blok v CSS je uzavřený.

Neuzavřený `@media (…) {` je v prohlížeči tichá chyba — všechno CSS za ním
pak platí jen uvnitř té podmínky (2.104.0: po sloučení větví platily styly
zvonku, předplatných a Úložiště jen na telefonu do 560 px). Komentáře se
nepočítají, závorka v nich nic neotvírá.

    python3 tools/test_css.py
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def neuzavrene(text):
    s = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    stack, line, chyby = [], 1, []
    for i, ch in enumerate(s):
        if ch == "\n":
            line += 1
        elif ch == "{":
            j, k = s.rfind("}", 0, i), s.rfind(";", 0, i)
            stack.append((line, s[max(j, k) + 1:i].strip()[:60]))
        elif ch == "}":
            if stack:
                stack.pop()
            else:
                chyby.append((line, "závorka navíc"))
    return chyby + stack


def main():
    soubory = sorted(glob.glob(os.path.join(ROOT, "hub", "static", "*.css"))
                     + glob.glob(os.path.join(ROOT, "hub", "motivy", "*", "*.css")))
    spatne = 0
    for path in soubory:
        with open(path, encoding="utf-8") as fh:
            for line, co in neuzavrene(fh.read()):
                spatne += 1
                print(f"{os.path.relpath(path, ROOT)}:{line}: neuzavřený blok {co}")
    print("OK" if not spatne else f"CHYBA: {spatne}")
    return 1 if spatne else 0


if __name__ == "__main__":
    sys.exit(main())
