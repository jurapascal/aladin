#!/usr/bin/env python3
"""Zkouška karty dotazu na oprávnění (composer.js): trvalé povolení „Ano a už se
neptej" musí být rozpoznané, česky popsané a odlišené od jednorázového Ano.

    python3 tools/test_povoleni_karta.py            # jednotkové zkoušky (node, bez sítě)
    python3 tools/test_povoleni_karta.py --dump DIR # vzorové výstupy dialogů jako ANSI soubory
                                                    # (pro tools/uitest_povoleni.mjs)

Dialogy jsou vyrobené podle toho, co Claude Code 2.1.x kreslí: rámeček z čar,
❯ u vybrané volby, barvy a posuny kurzoru (ANSI). Pro jednotkovou zkoušku se
ANSI odstraní stejně, jako to udělá terminál, a řádky jdou do funkcí
z composer.js (scanOptions, permInfo) načtených v node.
"""
import json
import os
import re
import subprocess
import sys
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSER = os.path.join(ROOT, 'hub', 'static', 'composer.js')

ESC = '\x1b'
DIM = ESC + '[2m'
BLUE = ESC + '[38;5;105m'
RESET = ESC + '[0m'
CSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def box(lines, width):
    """Rámeček jako Claude Code: každý řádek zalomený na šířku a obalený │ │."""
    inner = width - 4
    out = ['╭' + '─' * (width - 2) + '╮']
    for raw in lines:
        parts = [raw[i:i + inner] for i in range(0, max(len(raw), 1), inner)] or ['']
        for p in parts:
            out.append('│ ' + p + ' ' * (inner - len(p)) + ' │')
    out.append('╰' + '─' * (width - 2) + '╯')
    return out


def menu(opts, sel=0, width=100):
    """Číslované volby; delší znění se zalomí s odsazením, jako v terminálu."""
    lines = []
    for i, o in enumerate(opts):
        head = ('❯ ' if i == sel else '  ') + f'{i + 1}. '
        room = width - 4 - len(head)
        # Claude Code zalamuje na hranici slov, ne uprostřed.
        parts = textwrap.wrap(o, room, break_long_words=False) or ['']
        lines.append(head + parts[0])
        lines.extend(' ' * len(head) + p for p in parts[1:])
    return lines


def dialog(title, body, opts, sel=0, width=100, hint=None):
    lines = [title, ''] + body + ['', 'Do you want to proceed?'] + menu(opts, sel, width)
    out = box(lines, width)
    if hint:
        out.append(' ' + hint)
    return out


NO = 'No, and tell Claude what to do differently (esc)'
DIALOGS = {
    # jméno: (řádky, počet voleb)
    'bash3': dialog('Bash command', ['  git status', '  Show working tree status'],
                    ['Yes', "Yes, and don't ask again for git status commands in /home/x/proj", NO]),
    'bash2': dialog('Bash command', ['  ls -la'], ['Yes', NO]),
    'bash4': dialog('Bash command', ['  npm test'],
                    ['Yes', "Yes, and don't ask again for npm test commands in /home/x/proj",
                     "Yes, and don't ask again for npm commands in /home/x/proj", NO]),
    'edit3': dialog('Edit file', ['  hub/static/composer.js', '', '  12 - const a = 1;', '  12 + const a = 2;'],
                    ['Yes', 'Yes, allow all edits during this session (shift+tab)', NO]),
    'dlouhy': dialog('Bash command', ['  ' + 'x' * 150],
                     ['Yes', "Yes, and don't ask again for " + 'dlouhy-prikaz:* ' * 4 + 'commands in /home/uzivatel/slozka s mezerou/a/b/c/d/e/f', NO],
                     width=70),
    'viceradek': dialog('Bash command', ['  for f in *.txt; do', '    echo "$f"', '  done'],
                        ['Yes', "Yes, and don't ask again for for commands in /home/x", NO]),
    'unicode': dialog('Bash command', ['  echo "příliš žluťoučký kůň 🐎 úpěl ďábelské ódy"'],
                      ['Yes', "Yes, and don't ask again for echo commands in /home/příšerná složka/ž", NO]),
    'uzky': dialog('Bash command', ['  git status'],
                   ['Yes', "Yes, and don't ask again for git status commands in /home/x/proj", NO], width=44),
    'mcp': dialog('Tool use', ['  server - tool (MCP)'],
                  ['Yes', "Yes, and don't ask again for server - tool", NO]),
    'jen_yes_no': dialog('Bash command', ['  rm x'], ['Yes', 'No']),
}


def ansi_screen(lines):
    """Výstup tak, jak ho posílá Claude Code: barvy, posuny kurzoru, rámeček."""
    out = [ESC + '[2J' + ESC + '[H']
    for i, l in enumerate(lines):
        l = l.replace('❯', BLUE + '❯' + RESET)
        out.append(ESC + f'[{i + 1};1H' + DIM + RESET + l + ESC + '[K')
    return ''.join(out)


def visible(lines):
    return [CSI.sub('', l) for l in lines]


NODE = r"""
const vm = require('vm'), fs = require('fs');
const win = {};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'),
  {window: win, document: {}, localStorage: {}, console});
const t = win.HubComposer._test;
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = inp.map((c) => {
  if (c.perm !== undefined) return t.permInfo(c.perm);
  const opts = t.scanOptions(c.lines);
  const perms = t.permsOf(c.lines, opts);
  return opts.map((o, i) => ({key: o.key, label: o.label, sel: o.sel, perm: perms[i]}));
});
console.log(JSON.stringify(out));
"""


def node(cases):
    p = subprocess.run(['node', '-e', NODE, COMPOSER], input=json.dumps(cases),
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RuntimeError(p.stderr[-800:])
    return json.loads(p.stdout)


class Zkousky(unittest.TestCase):
    def test_permInfo_priklady(self):
        r = node([{'perm': x} for x in [
            "Yes, and don't ask again for git status commands in /home/x/proj",
            'Yes, allow all edits during this session (shift+tab)',
            'Yes', 'No, and tell Claude what to do differently (esc)', '', None,
            "Yes, and don't ask again for server - tool",
            "Yes, and don’t ask again for npm test commands in C:\\Users\\a b\\proj",
        ]])
        self.assertEqual(r[0]['label'], 'Ano a příště se neptat')
        self.assertIn('„git status"', r[0]['detail'])
        self.assertIn('ve složce /home/x/proj', r[0]['detail'])
        self.assertIn('napořád', r[0]['detail'])
        self.assertEqual(r[1]['label'], 'Ano, do konce relace')
        self.assertIn('úpravy souborů', r[1]['detail'])
        self.assertNotIn('shift+tab', r[1]['detail'])
        self.assertIsNone(r[2])
        self.assertIsNone(r[3])
        self.assertIsNone(r[4])
        self.assertIsNone(r[5])
        self.assertIn('„server - tool"', r[6]['detail'])
        self.assertIn('ve složce C:\\Users\\a b\\proj', r[7]['detail'])

    def test_dialogy(self):
        """Číslování volí dialog, ne pevné 1/2/3; trvalá volba je poznaná jen tam, kde je."""
        names = list(DIALOGS)
        res = node([{'lines': visible(DIALOGS[n])} for n in names])
        for n, opts in zip(names, res):
            with self.subTest(dialog=n):
                self.assertTrue(opts, 'volby nenalezeny')
                self.assertEqual([o['key'] for o in opts], [str(i + 1) for i in range(len(opts))])
                self.assertTrue(opts[0]['sel'])
        count = {n: len(o) for n, o in zip(names, res)}
        self.assertEqual((count['bash2'], count['bash3'], count['bash4']), (2, 3, 4))
        by = dict(zip(names, res))
        permidx = {n: [i for i, o in enumerate(v) if o['perm']] for n, v in by.items()}
        self.assertEqual(permidx['bash2'], [])
        self.assertEqual(permidx['jen_yes_no'], [])
        self.assertEqual(permidx['bash3'], [1])
        self.assertEqual(permidx['edit3'], [1])
        self.assertEqual(permidx['bash4'], [1, 2])
        # zalomené znění (dlouhý příkaz, úzké okno) nesmí ztratit složku
        self.assertIn('/home/uzivatel/slozka s mezerou/a/b/c/d/e/f', by['dlouhy'][1]['perm']['detail'])
        self.assertIn('/home/x/proj', by['uzky'][1]['perm']['detail'])
        self.assertIn('/home/příšerná složka/ž', by['unicode'][1]['perm']['detail'])
        self.assertIn('„for"', by['viceradek'][1]['perm']['detail'])
        # jednorázové Ano ani Ne nejsou trvalé
        for n, v in by.items():
            self.assertIsNone(v[0]['perm'], n)
            self.assertIsNone(v[-1]['perm'], n)

    def test_hrube_vstupy(self):
        junk = [{'perm': x} for x in ['\x00' * 50, 'don\'t ask again for ' + 'a' * 100000,
                                       "don't ask again for: in", 'during this session',
                                       '\u202e' + "don't ask again for x in /", '   ']]
        r = node(junk)
        self.assertEqual(len(r), len(junk))
        self.assertIsNone(r[0])
        self.assertTrue(all(x is None or x['label'] for x in r))


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--dump':
        os.makedirs(sys.argv[2], exist_ok=True)
        for n, lines in DIALOGS.items():
            with open(os.path.join(sys.argv[2], n + '.ansi'), 'w', encoding='utf-8') as f:
                f.write(ansi_screen(lines))
        print('uloženo', len(DIALOGS), 'dialogů do', sys.argv[2])
    else:
        unittest.main()
