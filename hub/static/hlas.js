/* Hlas — diktování do pole, předčítání odpovědí a hovor s Claudem.
 *
 * Všechno přes ElevenLabs (hub/eleven.py) vlastním klíčem člověka:
 * přepis řeči (Scribe) a hlas (Text to Speech, proudem). Kdo ElevenLabs
 * napojený nemá (Nastavení → Hlas), mikrofon, Hovor ani 🔊 nevidí.
 *
 * Hovor: klik na sluchátka → mluvíš, po chvilce ticha se řeč přepíše a sama
 * odešle Claudovi, jeho odpověď se přečte nahlas a mikrofon se zase zapne.
 * Jako hlasový agent, jen se mluví s Claudem v tomhle chatu.
 */
'use strict';

(function (global) {
  const KLIC_AUTO = 'hub-hlas-predcitat';
  const MAX_NAHRAVKA_S = 5 * 60;
  const KUS = 500;                     // znaků na jedno předčítání (věty se nedělí)

  let io = null;                       // {api(name, body), url(name)}
  let stav = null;
  let stavP = null;

  function init(opts) { io = opts; }

  /* Hláška o chybě: technický detail patří do konzole a pokročilým
     (window.HUB_ADVANCED), ostatním stačí srozumitelná věta. */
  function chyba(vec, err) {
    console.warn('hlas:', err);
    const detail = (err && err.message) || String(err || '');
    return global.HUB_ADVANCED && detail ? vec + ': ' + detail
      : vec + ' — zkus to za chvíli znovu.';
  }

  function ready() {
    if (!io) return Promise.resolve(false);
    if (!stavP) {
      // ready = ElevenLabs napojený (diktování, hovor); cteni = navíc vybraný hlas.
      stavP = io.api('hlas').then((s) => { stav = s; return !!s.ready; })
        .catch(() => { stav = {ready: false}; return false; });
    }
    return stavP;
  }

  /* Po změně v Nastavení → Hlas: nový stav hned platí i pro tlačítka u
     odpovědí, které už jsou na stránce, a rozehrané čtení starým hlasem se
     zastaví — žádné zavírání appky. */
  function refresh() {
    stav = null; stavP = null;
    stopVse();
    if (hovorStav.on) hovorKonec();
    return ready().then((ok) => {
      const lze = !!(stav && stav.cteni);
      document.querySelectorAll('.hlas-cti').forEach((b) => { b.hidden = !lze; });
      document.querySelectorAll('[data-hlas="mic"]').forEach((b) => { b.hidden = !ok || !umiMikrofon(); });
      document.querySelectorAll('[data-hlas="hovor"]').forEach((b) => { b.hidden = !ok || !lze || !umiMikrofon(); });
      return ok;
    });
  }

  const umiMikrofon = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

  /* ── nahrávání ────────────────────────────────────────────────────────── */
  /* Zvukový kontext musí vzniknout a rozběhnout se přímo v kliknutí —
     prohlížeče (Safari, Chrome i WebKitGTK) ho jinak nechají uspaný a z
     mikrofonu nepřijde ani vzorek. Proto ho zakládá obsluha kliknutí dřív,
     než se čeká na povolení mikrofonu, a sem ho jen předá. */
  function kontext() {
    const Ctx = global.AudioContext || global.webkitAudioContext;
    const ctx = new Ctx();
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    return ctx;
  }

  /* Nahrávání. Přednost má MediaRecorder — nativní nahrávání prohlížeče
     (Chrome, Firefox i na Androidu, Safari): nejmíň věcí, které se můžou
     pokazit, formát (webm/ogg/mp4) rozbalí server. Kde ho prohlížeč nemá,
     nahrává se přes Web Audio do WAV. Měřič hlasitosti je jen ozdoba —
     když selže, nahrává se dál. */
  const TYPY = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm'];

  async function nahravat(ctx, naUroven, hovor) {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        // Potlačení ozvěny je pro hovor (z reproduktoru mluví Claude), ne pro
        // diktování — hlas jím zbytečně trpí. Šum a hlasitost srovnat smí.
        audio: {echoCancellation: !!hovor, noiseSuppression: true, autoGainControl: true}});
    } catch (err) {
      ctx.close().catch(() => {});
      throw err;
    }
    if (ctx.state !== 'running') await ctx.resume().catch(() => {});

    let src = null, merak = null;
    try {
      src = ctx.createMediaStreamSource(stream);
      const an = ctx.createAnalyser();
      an.fftSize = 1024;
      src.connect(an);
      const d = new Float32Array(an.fftSize);
      merak = setInterval(() => {
        an.getFloatTimeDomainData(d);
        let sum = 0;
        for (let i = 0; i < d.length; i += 4) sum += d[i] * d[i];
        if (naUroven) naUroven(Math.min(1, Math.sqrt(sum / (d.length / 4)) * 10));
      }, 80);
    } catch (_) { src = null; }

    const konec = () => {
      clearInterval(merak);
      try { if (src) src.disconnect(); } catch (_) { /* už je */ }
      stream.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
    };

    if (global.MediaRecorder) {
      try {
        const typ = TYPY.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
        const mr = new MediaRecorder(stream, typ ? {mimeType: typ} : undefined);
        const casti = [];
        let bajtu = 0;
        mr.ondataavailable = (ev) => {
          if (ev.data && ev.data.size) { casti.push(ev.data); bajtu += ev.data.size; }
        };
        mr.start(250);
        return {
          stop: () => new Promise((hotovo, chyba) => {
            mr.onstop = async () => {
              konec();
              try {
                const blob = new Blob(casti, {type: mr.mimeType || typ || 'audio/webm'});
                hotovo(new Uint8Array(await blob.arrayBuffer()));
              } catch (err) { chyba(err); }
            };
            try { mr.stop(); } catch (err) { konec(); chyba(err); }
          }),
          zrusit() { try { mr.stop(); } catch (_) { /* už stojí */ } konec(); },
          get vzorku() { return bajtu; },
        };
      } catch (_) { /* tenhle prohlížeč MediaRecorder nemá pořádně — Web Audio */ }
    }

    if (!src) src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const kusy = [];
    proc.onaudioprocess = (ev) => kusy.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
    src.connect(proc);
    proc.connect(ctx.destination);
    return {
      stop() {
        try { proc.disconnect(); } catch (_) { /* už je */ }
        konec();
        return wav16(kusy, ctx.sampleRate);
      },
      zrusit() { try { proc.disconnect(); } catch (_) { /* už je */ } konec(); },
      get vzorku() { let n = 0; for (const k of kusy) n += k.length; return n; },
    };
  }

  /* Nahrávka jako WAV 16 kHz mono (prohlížeč bez MediaRecorderu). Prohlížeč nahrává na 44,1
     nebo 48 kHz a převzorkovat se musí s filtrem: prosté „každý třetí
     vzorek“ zvuk zkreslí (aliasing) a přepis pak plete slova. */
  async function wav16(kusy, rate) {
    let delka = 0;
    for (const k of kusy) delka += k.length;
    const vse = new Float32Array(delka);
    let o = 0;
    for (const k of kusy) { vse.set(k, o); o += k.length; }
    const cil = 16000;
    let pcm = vse;
    if (rate !== cil && vse.length) {
      pcm = null;
      const Off = global.OfflineAudioContext || global.webkitOfflineAudioContext;
      if (Off) {
        try {
          const n = Math.ceil(vse.length * cil / rate);
          const off = new Off(1, n, cil);
          const buf = off.createBuffer(1, vse.length, rate);
          buf.getChannelData(0).set(vse);
          const src = off.createBufferSource();
          src.buffer = buf;
          src.connect(off.destination);
          src.start(0);
          const out = await new Promise((hotovo, chyba) => {
            off.oncomplete = (ev) => hotovo(ev.renderedBuffer);
            const p = off.startRendering();
            if (p && p.then) p.then(hotovo, chyba);
          });
          pcm = out.getChannelData(0);
        } catch (_) { pcm = null; }
      }
      if (!pcm) {
        // Záloha: průměr přes okno — hrubý, ale pořád filtr.
        const krok = rate / cil;
        const n = Math.floor(vse.length / krok);
        pcm = new Float32Array(n);
        for (let i = 0; i < n; i++) {
          const a = Math.floor(i * krok), b = Math.min(vse.length, Math.floor((i + 1) * krok));
          let sum = 0;
          for (let j = a; j < b; j++) sum += vse[j];
          pcm[i] = sum / Math.max(1, b - a);
        }
      }
    }
    // Tichou nahrávku zesílit (nanejvýš 8×): slabý hlas přepis snadno přeslechne.
    let spicka = 0;
    for (let i = 0; i < pcm.length; i++) { const a = Math.abs(pcm[i]); if (a > spicka) spicka = a; }
    const zisk = spicka > 0 ? Math.min(8, 0.9 / spicka) : 1;
    const n = pcm.length;
    const buf = new ArrayBuffer(44 + n * 2);
    const v = new DataView(buf);
    const str = (at, t) => { for (let i = 0; i < t.length; i++) v.setUint8(at + i, t.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVE');
    str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, cil, true); v.setUint32(28, cil * 2, true); v.setUint16(32, 2, true);
    v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const x = Math.max(-1, Math.min(1, pcm[i] * zisk));
      v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    }
    return new Uint8Array(buf);
  }

  function base64(bytes) {
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  }

  /* Tlačítko mikrofonu v poli: klik = začít, klik = hotovo a přepsat do pole.
     Esc během nahrávání ji zahodí. */
  function mic(btn, input, opts = {}) {
    if (!btn || !input) return;
    btn.hidden = true;
    btn.dataset.hlas = 'mic';
    if (!umiMikrofon()) return;
    // Jen s napojeným ElevenLabs (přepisuje ho Scribe).
    ready().then((ok) => { btn.hidden = !ok; });
    let rec = null, timer = null, start = 0, busy = false;
    const puvodni = input.placeholder;

    function uklid() {
      clearInterval(timer);
      timer = null;
      rec = null;
      btn.classList.remove('rec');
      btn.style.removeProperty('--uroven');
      input.placeholder = puvodni;
    }

    async function hotovo() {
      const r = rec;
      const delka = (Date.now() - start) / 1000;
      uklid();
      if (!r) return;
      busy = true;
      btn.classList.add('busy');
      input.placeholder = 'Přepisuju…';
      try {
        const data = await r.stop();
        if (delka < 0.7 || !data || data.length < 1000) {
          if (opts.notice) opts.notice('Nahrávka byla moc krátká — podrž mikrofon zapnutý, dokud mluvíš.');
          return;
        }
        const out = await io.api('hlas-prepis', {audio: base64(data)});
        const text = (out.text || '').trim();
        if (!text) {
          if (opts.notice) opts.notice('Nic jsem nezachytil — zkus to znovu a blíž k mikrofonu.');
        } else {
          const pred = input.value;
          const mezera = pred && !/\s$/.test(pred) ? ' ' : '';
          input.value = pred + mezera + text;
          input.dispatchEvent(new Event('input', {bubbles: true}));
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        }
      } catch (err) {
        if (opts.notice) opts.notice(chyba('Přepis řeči se nepovedl', err));
      } finally {
        busy = false;
        btn.classList.remove('busy');
        input.placeholder = puvodni;
      }
    }

    btn.addEventListener('click', async (ev) => {
      ev.preventDefault();
      if (busy) return;
      if (rec) return hotovo();
      const ctx = kontext();                  // hned v kliknutí, ne až po await
      try {
        rec = await nahravat(ctx, (u) => btn.style.setProperty('--uroven', u.toFixed(2)));
      } catch (err) {
        rec = null;
        if (opts.notice) {
          opts.notice(err && err.name === 'NotAllowedError'
            ? 'Mikrofon není povolený — povol ho prohlížeči pro tuhle stránku.'
            : chyba('Mikrofon se nepodařilo zapnout', err));
        }
        return;
      }
      stopVse();                          // při diktování nemá nic mluvit
      start = Date.now();
      btn.classList.add('rec');
      input.placeholder = 'Poslouchám… klikni na mikrofon, až domluvíš (Esc zruší)';
      timer = setInterval(() => {
        const s = Math.round((Date.now() - start) / 1000);
        input.placeholder = `Poslouchám… ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` +
                            ' — klikni na mikrofon, až domluvíš (Esc zruší)';
        if (s >= MAX_NAHRAVKA_S) hotovo();
        // Po dvou vteřinách bez jediného vzorku mikrofon nic nedává —
        // lepší to říct hned, než nechat člověka mluvit do prázdna.
        if (s >= 3 && rec && rec.vzorku === 0) {
          rec.zrusit();
          uklid();
          if (opts.notice) opts.notice('Z mikrofonu nejde zvuk — zkontroluj, jestli ho prohlížeč smí používat.');
        }
      }, 500);
    });

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && rec) {
        ev.preventDefault();
        ev.stopPropagation();
        rec.zrusit();
        uklid();
      }
    }, true);
  }

  /* ── předčítání ───────────────────────────────────────────────────────── */
  /* Z Markdownu jen to, co se dá říct: kód, adresy a značky pryč. */
  function plain(md) {
    return String(md || '')
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/`([^`\n]+)`/g, '$1')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/g, 'odkaz')
      .replace(/^\s*\|?\s*:?-{2,}.*$/gm, ' ')
      .replace(/\|/g, ', ')
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')
      .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, '')
      .replace(/^\s*>\s?/gm, '')
      .replace(/(\*\*|__|\*|_|~~|==)(?=\S)([^\n]*?\S)\1/g, '$2')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n{2,}/g, '\n')
      .trim();
  }

  /* Kusy na předčítání. První je krátký (první věta) — zazní hned, zatímco
     se počítá zbytek; další můžou být delší, ať hlas drží intonaci. */
  const PRVNI = 160;
  function kusy(text) {
    const vety = text.match(/[^.!?…\n]+[.!?…]*\s*|\n/g) || [text];
    const out = [];
    let cur = '';
    for (const v of vety) {
      const strop = out.length ? KUS : PRVNI;
      if ((cur + v).length > strop && cur.trim()) { out.push(cur.trim()); cur = ''; }
      cur += v;
      if (!out.length && cur.trim().length >= 40 && /[.!?…]\s*$/.test(cur)) { out.push(cur.trim()); cur = ''; }
    }
    if (cur.trim()) out.push(cur.trim());
    return out;
  }

  /* `opts` = {voice_id, model_id} přebije uložený hlas (ukázka v Nastavení).
     Zvuk jde z ElevenLabs proudem (<audio src> na hlas-proud): hraje se od
     prvních bajtů a další kus se začne stahovat hned, jak se připraví.
     Když proud nejde připravit, zkusí se celý kus najednou (hlas-rec).
     Vrací {audio, uklid}. */
  async function zvuk(text, opts) {
    const telo = JSON.stringify(Object.assign({text}, opts || {}));
    const r = await io.api('hlas-pripravit', JSON.parse(telo));
    if (r && r.proud && r.id) {
      const a = new Audio(io.url('hlas-proud') + '&id=' + encodeURIComponent(r.id));
      a.preload = 'auto';
      a.load();
      return {audio: a, uklid() { a.removeAttribute('src'); try { a.load(); } catch (_) { /* nic */ } }};
    }
    if (r && r.error) throw new Error(r.error);
    const res = await fetch(io.url('hlas-rec'), {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: telo});
    if (!res.ok) {
      let msg = 'Předčítání se nepovedlo.';
      try { msg = (await res.json()).error || msg; } catch (_) { /* není JSON */ }
      throw new Error(msg);
    }
    const url = URL.createObjectURL(await res.blob());
    return {audio: new Audio(url), uklid() { URL.revokeObjectURL(url); }};
  }

  let fronta = [];                     // [{text, btn, opts}]
  let hraje = null;                    // {audio, btn, zruseno}

  function stopVse() {
    fronta = [];
    if (hraje) {
      hraje.zruseno = true;
      try { hraje.audio && hraje.audio.pause(); } catch (_) { /* nic */ }
      if (hraje.btn) hraje.btn.classList.remove('hraje');
      hraje = null;
    }
  }

  async function dalsi(notice) {
    if (hraje || !fronta.length) return;
    const {text, btn, opts} = fronta.shift();
    const casti = kusy(plain(text));
    const ja = {audio: null, btn, zruseno: false};
    hraje = ja;
    // „pripravuje" = hlas se teprve počítá (první věta trvá pár vteřin).
    if (btn) btn.classList.add('hraje', 'pripravuje');
    try {
      let pristi = casti.length ? zvuk(casti[0], opts) : null;
      for (let i = 0; i < casti.length && !ja.zruseno; i++) {
        const z = await pristi;
        // Další kus se chystá (a stahuje), zatímco tenhle hraje.
        pristi = i + 1 < casti.length ? zvuk(casti[i + 1], opts) : null;
        if (pristi) pristi.catch(() => {});
        if (ja.zruseno) { z.uklid(); break; }
        if (btn) btn.classList.remove('pripravuje');
        const zahraj = (z) => new Promise((hotovo) => {
          const a = z.audio;
          ja.audio = a;
          let konec = false;
          const dohrano = (chyba) => {
            if (konec) return;
            konec = true;
            const nic = chyba === true && !(a.currentTime > 0);
            z.uklid();
            hotovo(nic);
          };
          a.onended = () => dohrano(false);
          a.onerror = () => dohrano(true);
          a.onpause = () => { if (ja.zruseno) dohrano(false); };
          a.play().catch(() => dohrano(true));
        });
        // Proud z ElevenLabs nezahrál ani kousek (kredit, síť) → říct to, ne mlčet.
        if (await zahraj(z) && !ja.zruseno) {
          throw new Error('ElevenLabs nevrátil zvuk (kredit nebo síť).');
        }
      }
      if (ja.zruseno && pristi) pristi.then((z) => z.uklid(), () => {});
    } catch (err) {
      if (notice) notice(chyba('Předčítání se nepovedlo', err));
    }
    if (btn) btn.classList.remove('hraje', 'pripravuje');
    if (hraje === ja) hraje = null;
    if (!fronta.length) document.dispatchEvent(new CustomEvent('hlas-docteno'));
    dalsi(notice);
  }

  /* Přečíst text; `btn` se během čtení rozsvítí a druhým klikem čtení zastaví. */
  function speak(text, btn, notice, opts) {
    if (hraje && btn && hraje.btn === btn) { stopVse(); return; }
    if (btn) stopVse();                 // klik na jiné tlačítko = tohle hned
    fronta.push({text, btn, opts});
    dalsi(notice);
  }

  /* Tlačítko 🔊 k bublině s odpovědí. */
  function tlacitko(box, text, notice) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'hlas-cti';
    b.title = 'Přečíst nahlas';
    b.setAttribute('aria-label', 'Přečíst nahlas');
    b.innerHTML = '<svg class="ico hlas-i-cti"><use href="#i-speak"/></svg>' +
                  '<svg class="ico hlas-i-stop"><use href="#i-stop"/></svg>';
    b.hidden = true;
    // Jen s ElevenLabs a vybraným hlasem.
    ready().then(() => { b.hidden = !(stav && stav.cteni); });
    b.onclick = (ev) => { ev.stopPropagation(); speak(text, b, notice); };
    box.appendChild(b);
    return b;
  }

  const auto = {
    // Během hovoru se odpovědi čtou vždycky.
    get() {
      if (hovorStav.on) return true;
      try { return localStorage.getItem(KLIC_AUTO) === '1'; } catch (_) { return false; }
    },
    set(on) { try { localStorage.setItem(KLIC_AUTO, on ? '1' : '0'); } catch (_) { /* nic */ } },
  };

  /* ── hovor s Claudem ─────────────────────────────────────────────────────
     Jako hlasový agent: poslouchá → po tichu přepíše a pošle → Claude odpovídá
     a odpověď se čte (cteni.js přes auto) → až dočte a Claude nepracuje,
     poslouchá znovu. Druhý klik na sluchátka nebo Esc hovor ukončí. */
  const TICHO_MS = 1300;               // tolik ticha po řeči = domluvil
  const PRAH = 0.07;                   // úroveň z měřiče (0–1), nad ní je řeč
  const MAX_VETA_S = 90;
  const BEZ_RECI_S = 45;               // nikdo nemluví → hovor se ukončí
  const hovorStav = {on: false, btn: null, input: null, opts: null, rec: null,
                     faze: '', hlidac: null, poslano: 0, pracovalo: false, puvodni: ''};

  function hovorFaze(f, text) {
    const h = hovorStav;
    h.faze = f;
    if (h.btn) {
      h.btn.classList.toggle('on', h.on);
      h.btn.classList.toggle('posloucha', f === 'posloucha');
      h.btn.classList.toggle('ceka', f === 'prepis' || f === 'odpoved');
      if (f !== 'posloucha') h.btn.style.removeProperty('--uroven');
    }
    if (h.input) h.input.placeholder = text || h.puvodni;
  }

  function hovorKonec() {
    const h = hovorStav;
    if (!h.on) return;
    h.on = false;
    clearInterval(h.hlidac);
    h.hlidac = null;
    if (h.rec) { try { h.rec.zrusit(); } catch (_) { /* nic */ } h.rec = null; }
    stopVse();
    hovorFaze('', '');
    document.removeEventListener('keydown', hovorEsc, true);
  }

  function hovorEsc(ev) {
    if (ev.key === 'Escape' && hovorStav.on) { ev.preventDefault(); ev.stopPropagation(); hovorKonec(); }
  }

  async function hovorPoslouchej() {
    const h = hovorStav;
    if (!h.on || h.rec) return;
    let rec;
    let mluvil = 0, tichoOd = 0;
    const start = Date.now();
    const ctx = kontext();
    try {
      rec = await nahravat(ctx, (u) => {
        if (!h.on || h.rec !== rec) return;
        if (h.btn) h.btn.style.setProperty('--uroven', u.toFixed(2));
        const ted = Date.now();
        if (u > PRAH) { mluvil += 80; tichoOd = 0; } else if (mluvil >= 250 && !tichoOd) tichoOd = ted;
        if (mluvil >= 250 && tichoOd && ted - tichoOd >= TICHO_MS) hovorVeta();
        else if (ted - start > MAX_VETA_S * 1000 && mluvil) hovorVeta();
        else if (!mluvil && ted - start > BEZ_RECI_S * 1000) {
          hovorKonec();
          if (h.opts.notice) h.opts.notice('Hovor jsem ukončil — dlouho nikdo nemluvil.');
        }
      }, true);
    } catch (err) {
      hovorKonec();
      if (h.opts.notice) {
        h.opts.notice(err && err.name === 'NotAllowedError'
          ? 'Mikrofon není povolený — povol ho prohlížeči pro tuhle stránku.'
          : chyba('Mikrofon se nepodařilo zapnout', err));
      }
      return;
    }
    if (!h.on) { rec.zrusit(); return; }
    h.rec = rec;
    hovorFaze('posloucha', 'Hovor: mluv, po chvilce ticha to pošlu Claudovi (Esc ukončí)');
  }

  async function hovorVeta() {
    const h = hovorStav;
    const rec = h.rec;
    if (!rec) return;
    h.rec = null;
    hovorFaze('prepis', 'Hovor: přepisuju…');
    let text = '';
    try {
      const data = await rec.stop();
      if (data && data.length >= 1000) {
        const out = await io.api('hlas-prepis', {audio: base64(data)});
        text = (out.text || '').trim();
      }
    } catch (err) {
      if (h.opts.notice) h.opts.notice(chyba('Přepis řeči se nepovedl', err));
    }
    if (!h.on) return;
    if (!text) { hovorPoslouchej(); return; }
    h.poslano = Date.now();
    h.pracovalo = false;
    hovorFaze('odpoved', 'Hovor: Claude odpovídá…');
    try { h.opts.odeslat(text); } catch (err) {
      if (h.opts.notice) h.opts.notice(chyba('Zprávu se nepodařilo odeslat', err));
      hovorKonec();
    }
  }

  /* Hlídač fáze „odpověď“: znovu poslouchat, až Claude doběhl a dočetlo se. */
  function hovorHlidej() {
    const h = hovorStav;
    if (!h.on || h.faze !== 'odpoved') return;
    const pracuje = !!(h.opts.pracuje && h.opts.pracuje());
    if (pracuje) { h.pracovalo = true; return; }
    const ted = Date.now();
    // Claude se ještě nerozjel — chvilku počkat, než se uzná, že nic nedělá.
    if (!h.pracovalo && ted - h.poslano < 8000) return;
    if (hraje || fronta.length) return;
    // Odpověď mohla dorazit těsně před koncem práce — dát předčítání šanci začít.
    if (!h.klidOd) { h.klidOd = ted; return; }
    if (ted - h.klidOd < 1200) return;
    h.klidOd = 0;
    hovorPoslouchej();
  }

  /* Tlačítko se sluchátky vedle mikrofonu. opts: {odeslat(text), pracuje(), notice}. */
  function hovor(btn, input, opts = {}) {
    if (!btn || !input || !opts.odeslat) return;
    btn.hidden = true;
    btn.dataset.hlas = 'hovor';
    if (!umiMikrofon()) return;
    ready().then((ok) => { btn.hidden = !(ok && stav && stav.cteni); });
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      const h = hovorStav;
      if (h.on && h.btn === btn) { hovorKonec(); return; }
      if (h.on) hovorKonec();
      Object.assign(h, {on: true, btn, input, opts, rec: null, poslano: 0, pracovalo: false,
                        klidOd: 0, puvodni: input.placeholder});
      stopVse();
      document.addEventListener('keydown', hovorEsc, true);
      h.hlidac = setInterval(hovorHlidej, 300);
      hovorPoslouchej();
    });
  }

  global.HubHlas = {init, ready, refresh, mic, hovor, speak, stop: stopVse, tlacitko, auto, plain,
                    stav: () => stav, hovorBezi: () => hovorStav.on};
})(window);
