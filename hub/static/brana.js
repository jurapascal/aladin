/* Stará brána na serveru nezná nové adresy (/gw/…): vrací holé „404".
 *
 * Prostor se aktualizuje dřív než brána (brána se přepne, až v prostorech
 * nikdo nepracuje), takže nová okna na pár hodin volají to, co brána ještě
 * neumí — a každé volání by hlásilo „Server teď neodpovídá". Tohle je
 * zachytí na jednom místě: 404 s prostým textem místo JSON chyby se změní na
 * 404 s věcnou hláškou, kterou už všechna místa v UI ukazují (data.error).
 * Skutečné chyby (JSON s `error`, jiné stavy) zůstávají, jak jsou.
 * Načítá se jako první, před ostatními skripty.
 */
'use strict';

(function () {
  const HLASKA = 'Tohle bude fungovat, až se server aktualizuje.';
  const puvodni = window.fetch.bind(window);

  function cesta(vstup) {
    try {
      const u = typeof vstup === 'string' ? vstup : (vstup && vstup.url) || '';
      return new URL(u, location.href).pathname;
    } catch (_) { return ''; }
  }

  window.fetch = async function (vstup, nastaveni) {
    const res = await puvodni(vstup, nastaveni);
    if (res.status !== 404 || !cesta(vstup).startsWith('/gw/')) return res;
    let text = '';
    try { text = (await res.clone().text()).trim(); } catch (_) { return res; }
    // Brána neznámou adresu odbývá holým „404"; skutečná chyba (soubor tu
    // není…) nese JSON s vysvětlením.
    if (text && text !== '404' && !/^<(!doctype|html)/i.test(text)) return res;
    return new Response(JSON.stringify({error: HLASKA, stara_brana: true}), {
      status: 404, headers: {'Content-Type': 'application/json'},
    });
  };
  window.HubStaraBrana = {hlaska: HLASKA};
})();
