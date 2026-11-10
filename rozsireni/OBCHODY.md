# Texty pro obchody s rozšířeními

## Krátký popis (do 132 znaků)

Aladin vedle každé stránky: chat s Claudem ve tvém prostoru, který vidí stránku, na které jsi.

## Dlouhý popis

Aladin je pracovní prostor s Claudem pro firmy (Claude Code Hub). Rozšíření ho dává
vedle každé webové stránky:

• Boční panel s chatem — běží ve tvém prostoru na serveru Aladina, takže má tvůj
  Obsidian, napojení (Google, Freelo, Shopify…) a skilly.
• Ke každé zprávě se přiloží stránka, na které jsi: adresa, název, označený text
  a text stránky. Na kliknutí i screenshot viditelné části.
• Chaty z panelu najdeš i v Aladinovi na počítači a v telefonu, a naopak.
• Aladin se do prohlížeče umí podívat i sám (karty, text a kód stránky, screenshot)
  a se tvým souhlasem klikat a psát — před každou akcí se zeptá.
• Pravé tlačítko na textu: „Zeptat se Aladina…“. Zkratka Alt+Shift+A.
• Bankovní a platební stránky se nikdy nepřikládají.

Rozšíření potřebuje účet na serveru Aladina — napíšeš jeho adresu a přihlásíš se
stejně jako v appce na počítači (s dvoufázovým ověřením nebo passkey). Serverů jde
mít víc a přepínat mezi nimi.

## Jediný účel (Chrome Web Store)

Poslat obsah aktuální stránky do chatu s asistentem Aladin, když o to uživatel
napíše zprávu, a zobrazit odpověď v bočním panelu.

## Zdůvodnění oprávnění

- **sidePanel** — chat je v bočním panelu vedle stránky.
- **storage** — přihlášení (token zařízení) a nastavení rozšíření.
- **scripting** + **přístup k webům (`<all_urls>`)** — přečíst text, název a označený
  text aktuální stránky v okamžiku, kdy uživatel odešle zprávu. Rozšíření nevkládá
  žádný trvalý skript do stránek a na pozadí nic nečte.
- **activeTab**, **tabs** — zjistit, která karta je aktivní (název a adresa do panelu),
  a udělat screenshot viditelné části, když o něj uživatel klikne.
- **contextMenus** — položka „Zeptat se Aladina…“ v menu po pravém kliknutí.

## Údaje (formulář o zacházení s daty)

- Sbírá: obsah webu (text stránky, screenshot) a adresu stránky — jen při odeslání
  zprávy uživatelem; ověřovací údaje (token zařízení od serveru Aladina).
- Neprodává se, nepoužívá se k reklamě ani k posuzování úvěruschopnosti.
- Posílá se jen na server Aladina, který si uživatel vybral při přihlášení (a odtud
  modelu Claude od Anthropicu jako součást konverzace).

## Zásady ochrany soukromí

Zveřejněné: https://test.alba-rosa.cz/ochrana-soukromi#rozsireni
(každý server Aladina má stejnou stránku na /ochrana-soukromi). Text:

Rozšíření Aladin posílá na server Aladina, ke kterému se uživatel přihlásil, text
zprávy a — pokud to uživatel nevypne — adresu, název, označený text a text aktuální
stránky, na kliknutí i screenshot. Data se ukládají do pracovního prostoru
uživatele na tomto serveru (text stránky jako soubor, který se po 14 dnech maže)
a zpracovává je asistent Claude (Anthropic) jako součást konverzace. Rozšíření
nesbírá historii prohlížení, nic neposílá bez akce uživatele, nepřikládá bankovní
a platební stránky a neposílá data třetím stranám za účelem reklamy. Odhlášením
v nastavení rozšíření se smaže uložený token. Kontakt: správce serveru Aladina.

## Obrázky

- Ikona 128×128: `src/icons/icon-128.png`
- Screenshoty 1280×800: vyfotit panel vedle stránky (Chrome s otevřeným bočním panelem).
