# Aladin — rozšíření do prohlížeče

Aladin vedle každé stránky. Boční panel s chatem, který běží jako obyčejný chat
v tvém prostoru na serveru (Obsidian, napojení, skilly — všechno jako v Aladinovi)
a ke každé zprávě dostane stránku, na které jsi: adresu, název, označený text
a text stránky. Na kliknutí i screenshot.

- **Přihlášení** jako appka na počítači: vybereš server (Brněnská Buržoazie,
  Tiskárna Lančov nebo jiný), otevře se stránka Aladina, přihlásíš se (i s 2FA
  nebo passkey) a klikneš na „Povolit“. Rozšíření dostane token zařízení.
- **Chat** jde do tvého prostoru (`hub/rozsireni.py`, nástroje `stranka_poslat`,
  `stranka_cteni`, `stranka_chaty` přes `/gw/prostor/volani`). Je vidět i v Aladinovi
  na počítači a na telefonu; v panelu jde přepnout na kterýkoli nedávný chat.
- **Stránka**: text se uloží do prostoru jako soubor (`~/.claude/hub-stranky/`,
  maže se po 14 dnech), do zprávy jde cesta a krátký začátek. Banky a platební
  brány se nepřikládají nikdy. Přikládání jde vypnout v nastavení (⚙) nebo
  jednorázově křížkem u názvu stránky.
- **Pravé tlačítko** na označeném textu nebo stránce: „Zeptat se Aladina…“.
- **Zkratka** Alt+Shift+A otevře panel.

## Sestavení

    ./build.sh            # → dist/aladin-chrome-edge.zip, aladin-opera.zip, aladin-firefox.zip, safari-zdroj/

| Prohlížeč | Balíček | Panel |
|---|---|---|
| Chrome, Edge | `aladin-chrome-edge.zip` | boční panel (side panel) |
| Opera | `aladin-opera.zip` | postranní lišta Opery; tlačítko v liště otevře okno |
| Firefox | `aladin-firefox.zip` | postranní lišta (sidebar) |
| Safari | `safari-zdroj/` | vyskakovací okno (popup) |

## Instalace bez obchodu (na zkoušku)

- **Chrome / Edge / Opera:** rozbal zip → `chrome://extensions` (Edge `edge://extensions`,
  Opera `opera://extensions`) → zapnout *Režim pro vývojáře* → *Načíst rozbalené* → vybrat složku.
- **Firefox:** `about:debugging#/runtime/this-firefox` → *Načíst dočasný doplněk* → vybrat
  `manifest.json` ze složky (do restartu Firefoxu). Natrvalo jen podepsané (AMO,
  „Na vlastní pěst“ dá podepsaný `.xpi` hned).
- **Safari:** na Macu s Xcode: `xcrun safari-web-extension-converter safari-zdroj/ --app-name Aladin`,
  otevřít projekt v Xcode, spustit; v Safari povolit v Nastavení → Rozšíření.
  Pro ostatní lidi je potřeba Apple Developer účet (99 USD/rok) a App Store.

## Obchody

- Chrome Web Store: jednorázově 5 USD za vývojářský účet, zveřejnit jako *Neveřejné* (jen přes odkaz).
- Edge Add-ons, Opera add-ons: zdarma.
- Firefox AMO: zdarma; linter `npx web-ext lint --source-dir dist/firefox` musí projít bez chyb.
