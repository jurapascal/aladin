# Claude Code Hub — samostatná aplikace (desktop/)

Okno hubu jako skutečná aplikace, stejně jako VS Code: v liště **Claude Code Hub**
místo „Chrome", vlastní menu a **Cmd/Ctrl+N otevře další okno hubu** (ne prázdný
Chrome). Postavené na [Electronu](https://www.electronjs.org/) — stejné jádro
(Chromium), na kterém hub běží dnes, a stejné UI. Nic se nepřepisuje.

## Jak se k lidem dostane

Sama, aktualizací hubu — nic se nestahuje ručně. Instalačky přikládají appku
ke každému vydání (`.github/workflows/instalacky.yml`, job `okno`) pod pevnými
jmény (`Claude-Code-Hub-Okno-<systém>-<procesor>`, k tomu `okno.json` s verzí).
Hub od 2.78 si ji po startu stáhne do `~/.claude/appka/v<verze>/`
(`hub/appka.py`) a okno pak otevírá v ní; poprvé stažená hned převezme okno
v Chromu — na téže instanci, taby i rozdělaná práce Clauda běží dál.

- Když appka nenaběhne do 30 s (nebo hned spadne), hub otevře okno v Chromu
  jako dřív a v tom běhu ji už nezkouší.
- Kdo ji nechce: `"appka": false` v `~/.claude/hub-config.json`
  (nebo `--window=…` na příkazové řádce).
- Na serveru (prostor brány), bez displeje a na procesorech, pro které se
  nestaví (Linux ARM), se nestahuje vůbec.
- Nová verze appky = vyšší `version` v `package.json`; hub ji při dalším
  startu stáhne vedle a přepne na ni (běžící appka jede ze staré složky).

## Jak to funguje

- **Menu** kreslí hub jako proužek nahoře (`hub.js`, `appMenu`; přes
  `preload.cjs`) a nativní lišta je schovaná (Alt ji ukáže, zkratky platí
  dál). Nativní menu Electronu má na Linuxu a Windows rozbalené položky
  velké jako pro prst a zvenku se to nastavit nedá. Na Macu je menu v systému.
- **Spuštěná hubem** (`--hub-url=<adresa>`) se appka jen připojí k hubu, který
  ji pustil, a nic sama nespouští.
- Appka najde **nainstalovaný hub** (`~/.claude/claude-hub.py`, Python z
  `~/.claude/runtime/python`, jinak systémový) a spustí ho s `--vlastni-okno`:
  hub jen vypíše adresu a žije, dokud je otevřená aspoň jedna stránka — pak se
  ukončí jako dřív. Aktualizace hubu tedy fungují beze změny.
- **Hub už běží** (`hub-bezi.json`)? Appka se k němu jen připojí — druhá
  instance nad týmiž chaty by si přepisovala konverzace.
- **Starší hub**, který `--vlastni-okno` nezná, se spustí s `--no-browser`
  a appka ho při ukončení zavře sama.
- **Nové okno** (Cmd/Ctrl+N, Soubor → Nové okno) = další okno téhož hubu.
  Okna sdílejí taby, každé má aktivní jiný — třeba jeden chat na monitor.
- **Aktualizace hubu za běhu**: hub přejde na nový port, okna se přepnou sama
  a nová okna jdou rovnou tam.
- Odkazy a cizí stránky jdou do systémového prohlížeče. Okno ukazuje jen
  lokální hub (`127.0.0.1`) a prostor na serveru z nastavení (`gw_server`).
- Mikrofon (diktování), schránka a upozornění jsou povolené jen hubu.
- „Přesný snímek“ tlačítka Nahlásit chybu (Verxee Debug, `getDisplayMedia`)
  dostane jen obsah vlastního okna — na klik a jen hubu nebo prostoru, bez zvuku.

## Vývoj a zkouška

```bash
cd desktop
npm install
npm start          # appka nad nainstalovaným hubem
npm test           # zkouška: scénáře se skutečným hubem z repa
```

Zkouška (`test/smoke.mjs`) používá hub z repa a dočasnou konfiguraci
(`CLAUDE_CONFIG_DIR`), takže nesahá na nainstalovaný hub ani na `~/.claude`.
Projde: okno a menu, Nové okno + sdílené taby, odkazy ven, konec oken = konec
hubu; připojení k běžícímu hubu; starší hub bez přepínače; aktualizaci za běhu.
Linux bez displeje: `xvfb-run -a npm test`. Zabalená appka:
`HUB_DESKTOP_EXE=<spustitelný soubor> npm test`.

## Instalačky

```bash
npm run dist       # pro systém, na kterém běží (dmg+zip / Setup.exe / AppImage)
```

GitHub Actions (`.github/workflows/desktop.yml`) je postaví na Windows, Linuxu
a macOS (Apple Silicon i Intel) a každou zabalenou appku otestují stejnou
zkouškou. Appky nejsou podepsané certifikátem: Mac (ad-hoc podpis jako
`packaging/mac/build.sh`) je poprvé pustí až Nastavení systému → Soukromí
a zabezpečení → Přesto otevřít, Windows ukáže SmartScreen („Další informace →
Přesto spustit").

## Co zatím není

- Podpis certifikátem (Apple Developer, Windows code signing). Appka stažená
  hubem varování nedostane (nemá značku stažení z internetu); samostatně
  stažený `.dmg`/Setup.exe ano.
- Instalačky v `packaging/` nesou dál okno Chromu — appku si hub stáhne sám
  při prvním startu.
