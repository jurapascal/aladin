/* Diakritika v tabu. (Schránku řeší clipboard.js.)
 *
 * Naměřeno na WebKitGTK (hostitel okna na Linuxu) — jeden znak s diakritikou
 * vypadá takhle:
 *
 *     keydown(229) → beforeinput(insertFromComposition) → input → compositionend
 *
 * Tedy BEZ `compositionstart` a BEZ `compositionupdate`. xterm.js s tím počítá
 * a odesílá composition dvěma cestami:
 *
 *   1. CompositionHelper.keydown() → _handleAnyTextareaChanges(), který porovná
 *      skrytou textareu před a po a pošle rozdíl — ten je vždycky správně;
 *   2. compositionend → _finalizeComposition(), který pošle
 *      textarea.value.substring(start) až do konce hodnoty.
 *
 * `start` se nastavuje v `compositionstart`, jenže ten tady nikdy nepřijde, takže
 * zůstane na nule — a textareu xterm nikdy nevyprázdní. Druhá cesta proto od
 * druhého znaku dál posílá znovu celý nashromážděný ocas:
 *
 *     ř → "ř"          ✓
 *     í → "í" + "í"        (taValue "ří", substring(1))
 *     š → "š" + "íš"       (taValue "říš", substring(1))   →  "příílišíš"
 *
 * Stačí tedy držet tu textareu prázdnou: pak je `substring(start)` vždycky
 * prázdný řetězec a zbude jen ta správná cesta. Žádnou událost neblokujeme —
 * dřívější pokus brát composition xtermu z ruky rozbil klávesové zkratky.
 */
'use strict';

(function (global) {

  /* Vrací funkci, která to zase odpojí. */
  function install(term) {
    const ta = term.textarea;
    const root = term.element;
    if (!ta || !root) return () => {};

    let composing = false;
    let pending = 0;        // potvrzené znaky, které xterm ještě nedočetl

    function onCompositionStart() { composing = true; }

    function onCompositionEnd() {
      composing = false;
      pending++;
      // xterm čte tuhle textareu ve vlastním setTimeout(0) navěšeném na tutéž
      // událost. Náš posluchač je zaregistrovaný později, takže i náš timeout
      // běží až po tom jeho — hodnotu mu tím nesebereme.
      setTimeout(() => { ta.value = ''; pending--; }, 0);
    }

    // Vyprázdnit až po znaku nestačí: do textarey se umí dostat i to, co
    // composition není — třeba mezera, kterou xterm nezrušil. Zbylá mezera
    // posune `substring(start)` přesně na nový znak místo za něj a ten pak
    // odejde dvakrát („žluťoučký" → „žžluťoučký"). Proto se maže i těsně PŘED
    // znakem: keyCode 229 znamená „tuhle klávesu si bere vstupní metoda", a
    // capture na předkovi textarey nás pustí ke slovu dřív než xterm.
    //
    // Jen když zrovna neběží composition — tam, kde `compositionstart` opravdu
    // chodí (Chromium), drží prohlížeč rozepsaný text právě v té textarea a
    // vymazat mu ho pod rukama by rozbilo mrtvé klávesy.
    // `pending` je pojistka pro případ, že by prohlížeč doručil dva znaky
    // dřív, než doběhne první úklid: mazat textareu pod nedočteným znakem by
    // ho zahodilo, a ztracený znak je horší než zdvojený.
    function onKeyDownIME(ev) {
      if (ev.keyCode === 229 && !composing && pending === 0) ta.value = '';
    }

    ta.addEventListener('compositionstart', onCompositionStart);
    ta.addEventListener('compositionend', onCompositionEnd);
    // capture na předkovi textarey = jsme na řadě dřív než xterm
    root.addEventListener('keydown', onKeyDownIME, true);

    return function teardown() {
      ta.removeEventListener('compositionstart', onCompositionStart);
      ta.removeEventListener('compositionend', onCompositionEnd);
      root.removeEventListener('keydown', onKeyDownIME, true);
    };
  }

  /* Enter v poli na psaní. Na telefonu a tabletu bez myši (dotyková
     klávesnice) dělá nový řádek a odesílá se tlačítkem — jako ve WhatsAppu;
     na počítači Enter odešle a Shift+Enter dá nový řádek. */
  const dotykove = () => {
    try {
      return global.matchMedia('(pointer: coarse)').matches && !global.matchMedia('(any-pointer: fine)').matches;
    } catch (_) { return false; }
  };
  function enterOdesle(ev) {
    return ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && !dotykove();
  }

  global.HubIME = { install, enterOdesle, dotykove };

})(window);
