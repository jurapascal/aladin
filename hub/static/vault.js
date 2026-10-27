/* Náhled trezoru Obsidian přímo v hubu.
 *
 * Na serveru (brána) ani na počítači bez aplikace Obsidian se trezor nemá čím
 * otevřít — tlačítko „Otevřít Obsidian Brain" by nic neudělalo. Tady je trezor
 * vidět v okně hubu: poznámky po složkách, hledání v názvech i v textu,
 * vykreslená poznámka s odkazy [[…]] a co na ni odkazuje. Jen ke čtení.
 *
 * Markdown se převádí tady, ne knihovnou: hub běží i bez sítě a poznámky píše
 * i Claude. Cokoli z poznámky se proto nejdřív escapuje a HTML vzniká jen
 * z toho, co převod sám vyrobí — syrové HTML z poznámky se nikdy nevloží.
 */
(function (root) {
  'use strict';

  const ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);
  const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp|avif)$/i;
  const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

  /* ── Markdown → HTML ──────────────────────────────────────────────────── */

  /* Vlastnosti z frontmatteru: `klíč: hodnota`, vnořené klíče i seznamy
     (`- a`) se srovnají do jedné řady — na čtení stačí. */
  function frontmatter(md) {
    const m = /^---\n([\s\S]*?)\n---[ \t]*(?:\n|$)/.exec(md);
    if (!m) return [[], md];
    const props = [];
    for (const line of m[1].split('\n')) {
      const item = /^\s*-\s+(.*)$/.exec(line);
      if (item) {
        if (props.length) {
          const last = props[props.length - 1];
          last[1] = last[1] ? last[1] + ', ' + item[1] : item[1];
        }
        continue;
      }
      const kv = /^\s*([\w.-]+):\s*(.*)$/.exec(line);
      if (kv) props.push([kv[1], kv[2].replace(/^["']|["']$/g, '')]);
    }
    return [props.filter(([, v]) => v), md.slice(m[0].length)];
  }

  function safeDecode(text) {
    try { return decodeURIComponent(text); } catch (err) { return text; }
  }

  /* Řádkový text. Odkazy a kód se vyrobí napřed a schovají za zástupné
     značky, aby se jim dovnitř nepletlo tučné písmo ani hvězdičky z adres. */
  function inline(text, ctx) {
    const keep = [];
    const put = (html) => '\u0000' + (keep.push(html) - 1) + '\u0000';
    const link = (path, label, heading) =>
      put('<a class="vault-link' + (path || heading ? '' : ' missing') + '" data-note="' +
          esc(path || '') + '" data-heading="' + esc(heading || '') + '">' + esc(label) + '</a>');
    const ext = (url, label) =>
      put('<a class="vault-ext" data-href="' + esc(url) + '">' + esc(label || url) + '</a>');

    let s = String(text).replace(/\u0000/g, '');
    s = s.replace(/`([^`\n]+)`/g, (_m, code) => put('<code>' + esc(code) + '</code>'));
    // ![[obrázek.png]] a [[poznámka#nadpis|popisek]]
    s = s.replace(/(!?)\[\[([^\]\n]+?)\]\]/g, (_m, bang, inner) => {
      const [target, ...alias] = inner.split('|');
      const name = target.trim();
      const [note, ...hash] = name.split('#');
      if (bang && IMAGE_EXT.test(note.trim())) {
        const url = ctx.image(note.trim());
        return url ? put('<img class="vault-img" alt="' + esc(note.trim()) + '" src="' + esc(url) + '">')
                   : put('<span class="vault-link missing">' + esc(name) + '</span>');
      }
      const path = note.trim() ? ctx.resolve(note.trim()) : ctx.current;
      return link(path, alias.join('|').trim() || name, hash.join('#').trim());
    });
    // [popisek](adresa) a ![popisek](obrázek)
    s = s.replace(/(!?)\[([^\]\n]*)\]\(<?([^)\s>]+)>?(?:\s+"[^"]*")?\)/g, (_m, bang, label, href) => {
      if (/^(https?:|mailto:)/i.test(href)) return ext(href, label);
      // javascript:, data: a jiná schémata: jen text, nic klikacího.
      if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return put(esc(label || href));
      const [file, ...hash] = safeDecode(href).split('#');
      if (bang && IMAGE_EXT.test(file)) {
        const url = ctx.image(file);
        return url ? put('<img class="vault-img" alt="' + esc(label) + '" src="' + esc(url) + '">')
                   : put(esc(label || file));
      }
      const path = file ? ctx.resolve(file) : ctx.current;
      return link(path, label || file, hash.join('#'));
    });
    s = s.replace(/\bhttps?:\/\/[^\s<>()*\u0000]*[^\s<>().,;:!?*'"\u0000]/g, (url) => ext(url));

    s = esc(s)
      .replace(/\*\*(?=\S)([^\n]*?\S)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^\w])__(?=\S)([^\n]*?\S)__(?!\w)/g, '$1<strong>$2</strong>')
      .replace(/(^|[^*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/(^|[^_\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?![_\w])/g, '$1<em>$2</em>')
      .replace(/~~(?=\S)([^\n]*?\S)~~/g, '<del>$1</del>')
      .replace(/==(?=\S)([^\n]*?\S)==/g, '<mark>$1</mark>')
      .replace(/(^|\s)#([\p{L}\p{N}_/-]*\p{L}[\p{L}\p{N}_/-]*)/gu, '$1<span class="vault-tag">#$2</span>');
    return s.replace(/\u0000(\d+)\u0000/g, (_m, i) => keep[+i]);
  }

  /* Řádky odstavce. Poznámky od Clauda jsou v souboru zalomené po osmdesáti
     znacích, a kreslit každý konec řádku by text roztrhalo — spojují se proto
     mezerou jako v běžném Markdownu. Tvrdý zlom (dvě mezery nebo \ na konci)
     zůstane. `ctx.breaks` = každý konec řádku je zlom — odpovědi v chatu
     (cteni.js) zalomené nejsou, tam řádek znamená řádek. */
  function joinLines(lines, ctx) {
    return lines.map((line, i) => {
      const last = i === lines.length - 1;
      const hard = !last && (!!(ctx && ctx.breaks) || /( {2,}|\\)$/.test(line));
      const text = inline(line.replace(/( {2,}|\\)$/, '').trim(), ctx);
      return text + (last ? '' : hard ? '<br>' : ' ');
    }).join('');
  }

  // Řádek, kterým začíná jiný blok — ten už nepokračuje předchozí položku seznamu.
  function blockStart(line) {
    return /^\s*(`{3,}|~{3,}|#{1,6}\s|>)/.test(line) || /^\s*([-*_])(\s*\1){2,}\s*$/.test(line) ||
           LIST.test(line);
  }

  function listHtml(lines, ctx) {
    const items = [];
    for (const raw of lines) {
      const line = raw.replace(/\t/g, '    ');
      const m = LIST.exec(line);
      if (m) items.push({indent: m[1].length, ordered: /\d/.test(m[2]), text: [m[3]]});
      else if (items.length) items[items.length - 1].text.push(line.trim());
    }
    let pos = 0;
    function build(indent) {
      const tag = items[pos].ordered ? 'ol' : 'ul';
      const parts = [];
      while (pos < items.length && items[pos].indent >= indent) {
        const it = items[pos++];
        const task = /^\[([ xX])\]\s+/.exec(it.text[0]);
        if (task) it.text[0] = it.text[0].slice(task[0].length);
        let body = joinLines(it.text, ctx);
        if (task) {
          body = '<input type="checkbox" disabled' + (task[1] === ' ' ? '' : ' checked') + '>' + body;
        }
        if (pos < items.length && items[pos].indent > it.indent) body += build(items[pos].indent);
        const cls = task ? ' class="task' + (task[1] === ' ' ? '' : ' done') + '"' : '';
        parts.push('<li' + cls + '>' + body + '</li>');
      }
      return '<' + tag + '>' + parts.join('') + '</' + tag + '>';
    }
    return items.length ? build(items[0].indent) : '';
  }

  function tableHtml(lines, ctx) {
    const cells = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
    const head = cells(lines[0]);
    /* Obal je to, co se posouvá do strany — `overflow` na samotné <table> se
       neuplatní a sloupce by se místo toho mačkaly do úzkých sloupečků. */
    let html = '<div class="vault-table"><table><thead><tr>' + head.map((c) => '<th>' + inline(c, ctx) + '</th>').join('') +
               '</tr></thead><tbody>';
    for (const row of lines.slice(2)) {
      html += '<tr>' + cells(row).map((c) => '<td>' + inline(c, ctx) + '</td>').join('') + '</tr>';
    }
    return html + '</tbody></table></div>';
  }

  function blocks(lines, ctx) {
    const out = [];
    let para = [];
    const flush = () => {
      if (para.length) out.push('<p>' + joinLines(para, ctx) + '</p>');
      para = [];
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      let m;
      if ((m = /^\s*(`{3,}|~{3,})\s*([\w-]*)/.exec(line))) {
        flush();
        const fence = m[1];
        const lang = m[2].toLowerCase();
        const code = [];
        for (i++; i < lines.length && !lines[i].trim().startsWith(fence); i++) code.push(lines[i]);
        out.push('<pre class="vault-code"' + (lang ? ' data-lang="' + esc(lang) + '"' : '') + '><code>' +
                 esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      if (!line.trim()) { flush(); continue; }
      if ((m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) {
        flush();
        const n = m[1].length;
        out.push('<h' + n + ' data-heading="' + esc(m[2]) + '">' + inline(m[2], ctx) + '</h' + n + '>');
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push('<hr>'); continue; }
      if (/^\s*>/.test(line)) {
        flush();
        const quote = [];
        for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(lines[i].replace(/^\s*>\s?/, ''));
        i--;
        const call = /^\[!([\w-]+)\][+-]?\s*(.*)$/.exec(quote[0]);
        if (call) {
          const kind = call[1].toLowerCase();
          const title = call[2] || kind.charAt(0).toUpperCase() + kind.slice(1);
          out.push('<div class="vault-callout ' + esc(kind) + '"><div class="vault-callout-title">' +
                   inline(title, ctx) + '</div>' + blocks(quote.slice(1), ctx) + '</div>');
        } else {
          out.push('<blockquote>' + blocks(quote, ctx) + '</blockquote>');
        }
        continue;
      }
      const next = lines[i + 1] || '';
      if (line.includes('|') && next.includes('|') && /-/.test(next) && /^[\s|:-]+$/.test(next)) {
        flush();
        const rows = [line, next];
        for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) rows.push(lines[i]);
        i--;
        out.push(tableHtml(rows, ctx));
        continue;
      }
      if (LIST.test(line)) {
        flush();
        const start = i;
        // Odsazený i neodsazený řádek bez prázdného řádku před ním pokračuje
        // položku (i v Obsidianu); konec je až prázdný řádek nebo jiný blok.
        for (i++; i < lines.length && lines[i].trim() &&
                  (LIST.test(lines[i]) || /^\s{2,}\S/.test(lines[i]) || !blockStart(lines[i])); i++);
        out.push(listHtml(lines.slice(start, i), ctx));
        i--;
        continue;
      }
      para.push(line);
    }
    flush();
    return out.join('');
  }

  /* Poznámka → {props, html}. `ctx.resolve(jméno)` vrací cestu poznámky v trezoru
     (nebo ''), `ctx.image(jméno)` adresu obrázku, `ctx.current` cestu té, co se kreslí. */
  function render(md, ctx) {
    const [props, body] = frontmatter(String(md || '').replace(/\r\n?/g, '\n'));
    return {props, html: blocks(body.split('\n'), ctx)};
  }

  /* Hledání odkazů jako v Obsidianu: celá cesta, jinak jméno souboru — při
     shodě jmen vyhrává kratší cesta. */
  function indexOf(paths, strip) {
    const byPath = new Map();
    const byName = new Map();
    for (const path of paths) {
      const key = (strip ? path.replace(/\.md$/i, '') : path).toLowerCase();
      byPath.set(key, path);
      const name = key.split('/').pop();
      const prev = byName.get(name);
      if (!prev || path.length < prev.length) byName.set(name, path);
    }
    return (target) => {
      let key = String(target || '').trim().replace(/\\/g, '/').replace(/^(\.\.?\/)+/, '');
      if (strip) key = key.replace(/\.md$/i, '');
      key = key.toLowerCase();
      return byPath.get(key) || byName.get(key.split('/').pop()) || '';
    };
  }

  /* ── okno ─────────────────────────────────────────────────────────────── */

  let io = null;
  let box = null;
  let notes = [];
  let files = [];         // ostatní soubory trezoru (PDF, obrázky, tabulky…)
  let resolveNote = () => '';
  let resolveImage = () => '';
  let current = '';
  let back = [];
  let searchSeq = 0;
  let searchTimer = null;
  const openDirs = new Set(['memory']);

  // Který trezor: osobní (''), nebo firemní ('firma') — posílá se s každým dotazem.
  const vq = () => (io && io.vault ? '&vault=' + encodeURIComponent(io.vault) : '');
  // Trezor firmy: dnešní firemní ('firma') i další firmy ('firma:<zkratka>',
  // gateway/firmy.py). U dalších firem se brána ptá i na zkratku.
  const isFirma = () => !!(io && (io.vault === 'firma' || /^firma:/.test(io.vault || '')));
  const firmaQuery = () => (io && /^firma:/.test(io.vault || '')
    ? '?firma=' + encodeURIComponent(io.vault.slice(6)) : '');
  const fold = (s) => String(s).normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  const baseName = (path) => path.split('/').pop().replace(/\.md$/i, '');
  const q = (sel) => box.querySelector(sel);

  function plural(n) {
    if (n === 1) return '1 poznámka';
    if (n >= 2 && n <= 4) return n + ' poznámky';
    return n + ' poznámek';
  }

  function node(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  // Rozcestník Claudovy paměti česky (soubor se jmenuje dál MEMORY.md).
  const noteLabel = (path) => (/(^|\/)memory\/MEMORY\.md$/i.test(path) ? 'Přehled paměti' : baseName(path));

  function itemButton(path, snippet) {
    const b = node('button', 'vault-item' + (path === current ? ' on' : ''), noteLabel(path));
    b.dataset.path = path;
    b.title = path;
    if (restricted(path)) {
      b.classList.add('locked');
      b.title = path + ' — vidí jen ' + whoText(path);
    }
    if (snippet) b.appendChild(node('small', '', snippet));
    return b;
  }

  function treeOf(list, other = []) {
    const top = {path: '', dirs: new Map(), notes: [], files: []};
    const place = (path) => {
      let at = top;
      for (const part of path.split('/').slice(0, -1)) {
        if (!at.dirs.has(part)) {
          at.dirs.set(part, {name: part, path: (at.path ? at.path + '/' : '') + part,
                             dirs: new Map(), notes: [], files: []});
        }
        at = at.dirs.get(part);
      }
      return at;
    };
    for (const n of list) place(n.path).notes.push(n.path);
    for (const f of other) place(f.path).files.push(f);
    return top;
  }

  function count(dir) {
    let n = dir.notes.length + dir.files.length;
    for (const d of dir.dirs.values()) n += count(d);
    return n;
  }

  function drawDir(dir, parent) {
    const dirs = [...dir.dirs.values()].sort((a, b) => a.name.localeCompare(b.name, 'cs'));
    for (const d of dirs) {
      const det = node('details');
      det.dataset.dir = d.path;
      const sum = node('summary');
      sum.appendChild(node('span', '', dirLabel(d.path, d.name)));
      const special = linkDir(d.path);
      if (special) {
        const who = special.kind === 'osobni' ? 'jen ty'
          : (special.members || []).join(', ');
        const tag = node('span', 'vault-badge', special.kind === 'osobni' ? '🔒 jen ty' : '👥 ' + (special.members || []).length);
        tag.title = special.kind === 'osobni'
          ? 'Tvoje osobní poznámky — vidíš je jen ty, ukládají se hned.'
          : 'Sdílené poznámky „' + (special.name || d.name) + '“ — vidí: ' + who + '. Kdo je vidí, mění ten, kdo je založil (tlačítko ⋯ u Sdílených poznámek).';
        sum.appendChild(tag);
      } else if (restricted(d.path)) {
        const tag = node('span', 'vault-badge', '🔒');
        tag.title = 'Složku vidí jen ' + whoText(d.path);
        sum.appendChild(tag);
      }
      if (acl && acl.spravce && !special && !/^(Lidé|Sdílené)(\/|$)/.test(d.path)) {
        const who = node('button', 'vault-dir-who', 'Vidí…');
        who.type = 'button';
        who.title = 'Kdo tuhle složku vidí';
        who.onclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); openWho(d.path); };
        sum.appendChild(who);
      }
      sum.appendChild(node('span', 'vault-count', String(count(d))));
      det.appendChild(sum);
      const sub = node('div', 'vault-sub');
      det.appendChild(sub);
      // Obsah složky se kreslí až při otevření — skilly mají stovky poznámek.
      let drawn = false;
      const fill = () => {
        if (drawn || !det.open) return;
        drawn = true;
        drawDir(d, sub);
      };
      det.addEventListener('toggle', () => {
        if (det.open) openDirs.add(d.path); else openDirs.delete(d.path);
        fill();
      });
      det.open = openDirs.has(d.path);
      fill();
      parent.appendChild(det);
    }
    for (const path of dir.notes.sort((a, b) => a.localeCompare(b, 'cs'))) {
      parent.appendChild(itemButton(path));
    }
    for (const f of dir.files.sort((a, b) => a.path.localeCompare(b.path, 'cs'))) {
      const b = node('button', 'vault-item vault-file', f.path.split('/').pop());
      b.dataset.file = f.path;
      b.title = f.path + ' · ' + fileSize(f.size) + ' — kliknutím stáhnout';
      b.appendChild(node('small', '', fileSize(f.size)));
      parent.appendChild(b);
    }
  }

  function fileSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return Math.round(n / 1024) + ' kB';
    return (n / 1024 / 1024).toFixed(1).replace('.', ',') + ' MB';
  }

  function renderList() {
    const list = q('.vault-list');
    const text = q('.vault-search').value.trim();
    list.textContent = '';
    clearTimeout(searchTimer);
    const seq = ++searchSeq;
    if (!text) {
      drawDir(treeOf(notes, files), list);
      return;
    }
    const words = fold(text).split(/\s+/).filter(Boolean);
    const named = notes.filter((n) => words.every((w) => fold(n.path).includes(w))).slice(0, 200);
    list.appendChild(node('div', 'vault-group', 'Názvy (' + named.length + ')'));
    for (const n of named) list.appendChild(itemButton(n.path));
    if (!named.length) list.appendChild(node('div', 'vault-empty', 'Žádný název nesedí.'));
    const inText = node('div');
    inText.appendChild(node('div', 'vault-group', 'V textu…'));
    list.appendChild(inText);
    searchTimer = setTimeout(async () => {
      let res;
      try {
        res = await io.api('vault-search?q=' + encodeURIComponent(text) + vq());
      } catch (err) {
        if (seq === searchSeq) inText.textContent = '';
        return;
      }
      if (seq !== searchSeq || !box) return;
      const seen = new Set(named.map((n) => n.path));
      const hits = (res.results || []).filter((r) => !seen.has(r.path));
      inText.textContent = '';
      inText.appendChild(node('div', 'vault-group', 'V textu (' + hits.length + ')'));
      for (const r of hits) inText.appendChild(itemButton(r.path, r.snippet));
    }, 250);
  }

  function markCurrent() {
    for (const b of box.querySelectorAll('.vault-item')) {
      b.classList.toggle('on', b.dataset.path === current);
    }
    if (q('.vault-search').value.trim()) return;
    // Otevřít složky až k poznámce, ať je v seznamu vidět, kde člověk je.
    const parts = current.split('/').slice(0, -1);
    let missing = false;
    for (let i = 1; i <= parts.length; i++) {
      const dir = parts.slice(0, i).join('/');
      if (!openDirs.has(dir)) { openDirs.add(dir); missing = true; }
    }
    if (missing) renderList();
    const item = [...box.querySelectorAll('.vault-item')].find((b) => b.dataset.path === current);
    if (item) item.scrollIntoView({block: 'nearest'});
  }

  /* Drobečková navigace nad poznámkou: Moje poznámky › Domácnost › Nákupní
     seznam. Bez .md a lomítek; kliknutí na složku ji otevře v seznamu. */
  // Složky, které si zakládá Claude, česky (cesta zůstává, jen popisek).
  const DIR_LABELS = {memory: 'Paměť Clauda', skills: 'Postupy', projects: 'Projekty',
                      'hub-postup': 'Kde jsem skončil'};
  const dirLabel = (dir, name) => (!dir.includes('/') || /^(Lidé|Sdílené)\/[^/]+\/[^/]+$/.test(dir)
    ? DIR_LABELS[name] : '') || name;

  // „upraveno dnes ve 20:24“, „včera v 9:05“, „3. 9. 2026“.
  function kdyUpraveno(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    const hm = d.toLocaleTimeString('cs-CZ', {hour: 'numeric', minute: '2-digit'});
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day(now) - day(d)) / 86400000);
    if (diff === 0) return 'upraveno dnes ve ' + hm;
    if (diff === 1) return 'upraveno včera v ' + hm;
    return 'upraveno ' + d.toLocaleDateString('cs-CZ');
  }

  function rootLabel() {
    if (io && io.vault === 'firma') return 'Firemní poznámky';
    if (isFirma()) return io.title || 'Firemní poznámky';
    if (io && io.vault) return (io.title || 'Sdílené poznámky').replace(/^Sdílené poznámky — /, '');
    return 'Moje poznámky';
  }

  function crumbLabel(dir, name) {
    const l = linkDir(dir);
    if (l && l.kind === 'osobni') return 'Moje složka';
    if (l && l.kind === 'sdilene') return l.name || name;
    return dirLabel(dir, name);
  }

  function setPath(path) {
    const bar = q('.vault-path');
    bar.textContent = '';
    bar.title = path || '';
    if (!path) return;
    const parts = path.split('/');
    const add = (label, dir, last) => {
      if (bar.childElementCount) bar.appendChild(node('span', 'vault-crumb-sep', '›'));
      const b = node(last ? 'span' : 'button', 'vault-crumb' + (last ? ' on' : ''), label);
      if (!last) {
        b.type = 'button';
        b.title = dir ? 'Ukázat složku ' + dir.split('/').pop() : 'Ukázat všechny poznámky';
        b.onclick = () => showFolder(dir);
      }
      bar.appendChild(b);
    };
    add(rootLabel(), '', false);
    for (let i = 0; i < parts.length - 1; i++) {
      const dir = parts.slice(0, i + 1).join('/');
      add(crumbLabel(dir, parts[i]), dir, false);
    }
    add(noteLabel(path), path, true);
  }

  // Složku z drobečků otevřít v seznamu (na telefonu se vrátí na seznam).
  function showFolder(dir) {
    q('.vault-search').value = '';
    if (dir) {
      const parts = dir.split('/');
      for (let i = 1; i <= parts.length; i++) openDirs.add(parts.slice(0, i).join('/'));
    }
    box.classList.remove('reading');
    renderList();
    const det = dir ? [...box.querySelectorAll('.vault-list details')].find((d) => d.dataset.dir === dir) : null;
    const target = det || q('.vault-list');
    target.scrollIntoView({block: 'nearest'});
    if (det) {
      det.classList.add('flash');
      setTimeout(() => det.classList.remove('flash'), 1200);
    }
  }

  function scrollToHeading(heading) {
    const want = fold(heading).trim();
    const h = [...q('.vault-md').querySelectorAll('[data-heading]')]
      .find((el) => fold(el.dataset.heading).trim() === want);
    if (h) h.scrollIntoView({block: 'start'});
  }

  async function load(path, {remember = true, heading = ''} = {}) {
    if (!path || !box) return;
    if (path === current && heading) return scrollToHeading(heading);
    let note;
    try {
      note = await io.api('vault-note?path=' + encodeURIComponent(path) + vq());
    } catch (err) {
      io.toast('Poznámku se nepodařilo otevřít: ' + err.message);
      return;
    }
    if (!box) return;
    if (remember && current && current !== note.path) back.push(current);
    current = note.path;
    if (io.onPlace) io.onPlace(current);
    const out = render(note.text, {resolve: resolveNote, image: resolveImage, current});

    // Na telefonu se seznam a poznámka nevejdou vedle sebe: otevřená
    // poznámka seznam překryje a šipka zpět se vrací na něj.
    box.classList.add('reading');

    setPath(note.path);
    q('.vault-date').textContent = note.mtime
      ? kdyUpraveno(note.mtime) : '';
    // Na úzké obrazovce je šipka i cestou zpět na seznam, tak je vidět vždy.
    q('.vault-back').hidden = !back.length && !window.matchMedia('(max-width: 720px)').matches;
    const props = q('.vault-props');
    props.textContent = '';
    for (const [key, value] of out.props) {
      const chip = node('span', 'vault-prop');
      chip.appendChild(node('b', '', key + ': '));
      chip.appendChild(document.createTextNode(value));
      props.appendChild(chip);
    }
    const md = q('.vault-md');
    md.innerHTML = out.html || '<p class="vault-empty">(prázdná poznámka)</p>';
    if (note.truncated) md.appendChild(node('p', 'vault-empty', '… poznámka je dlouhá, zbytek je jen v souboru.'));

    const links = q('.vault-backlinks');
    links.textContent = '';
    if (note.backlinks && note.backlinks.length) {
      links.appendChild(node('div', 'set-title', 'Odkazuje sem (' + note.backlinks.length + ')'));
      for (const p of note.backlinks) links.appendChild(itemButton(p));
    }
    lastText = note.text || '';
    noteMtime = note.mtime || 0;
    dirty = false;
    clearTimeout(saveTimer);
    q('.vault-modes').hidden = !canEdit();
    // Přejmenovat a smazat umí hub jen v osobním trezoru — do společných
    // zapisuje brána až po potvrzení karty. Ve firmě maže brána: kapitán a člen
    // s právem mazat (gateway/firmy.py).
    q('.vault-rename').hidden = !canEdit() || proposes();
    q('.vault-del').hidden = !canDelete();
    setState(mode === 'edit' ? (proposes() ? 'úpravy se potvrzují kartou' : 'ukládá se samo') : '');
    if (mode === 'edit' && editor) editor.setDoc(lastText);
    if (graph) graph.setFocus(current);
    whoButton();

    q('.vault-panel').scrollTop = 0;
    if (heading) scrollToHeading(heading);
    markCurrent();
  }

  /* ── úpravy poznámky ──────────────────────────────────────────────────────
     Osobní trezor hub zapíše rovnou (ukládá se samo). Firemní a sdílený jsou
     jen ke čtení: z Uložit vznikne návrh a zapíše ho brána, až se potvrdí
     karta v hubu — stejně jako když poznámku připraví Claude. */

  let lastText = '';      // text otevřené poznámky, jak přišel ze serveru
  let mode = 'read';
  let editor = null;
  let saveTimer = null;
  let dirty = false;
  let noteMtime = 0;
  let graph = null;

  /* Ve firemním trezoru jsou Lidé/<já> (osobní trezor — zapisuje se rovnou)
     a Sdílené/<název> (sdílený — přes kartu). Posílá je brána (gateway/slozky.py),
     cesty v nich hub směruje sám (core.vault_route). */
  let links = [];
  const linkOf = (path) => links.find((l) => String(path || '').startsWith(l.path + '/')) || null;
  const linkDir = (path) => links.find((l) => l.path === path) || null;

  // Firemní trezor jen ke čtení (brána: company_level = read) úpravy nenabízí —
  // kromě vlastní a sdílené složky, ty pod firemní právo nespadají.
  const canEdit = (path = current) => !!(root.HubEditor && root.HubEditor.available) &&
    !(io && io.readonly && !linkOf(path));
  const canDelete = (path = current) => (isFirma() && !linkOf(path) ? !!io.mazat
                                                                     : canEdit(path) && !proposes(path));
  const proposes = (path = current) => {
    if (!(io && io.vault)) return false;
    const l = linkOf(path);
    return !(l && l.kind === 'osobni');
  };

  function setState(text, kind) {
    const el = q('.vault-state');
    el.textContent = text || '';
    el.className = 'vault-state' + (kind ? ' ' + kind : '');
  }

  function setMode(next) {
    if (!box || mode === next) return;
    if (next === 'edit' && !canEdit()) return io.toast('Editor se nenačetl — zkus obnovit stránku.');
    mode = next;
    for (const b of box.querySelectorAll('.vault-mode')) b.classList.toggle('on', b.dataset.mode === mode);
    q('.vault-md').hidden = mode === 'edit';
    q('.vault-edit').hidden = mode !== 'edit';
    q('.vault-backlinks').hidden = mode === 'edit';
    if (mode !== 'edit') {
      // Ve čtení má být vidět to, co se právě napsalo, ne text z načtení.
      if (editor) showRendered(editor.getDoc());
      if (dirty && !proposes()) saveNow();
      return;
    }
    mountEditor();
  }

  function showRendered(text) {
    const out = render(text, {resolve: resolveNote, image: resolveImage, current});
    const props = q('.vault-props');
    props.textContent = '';
    for (const [key, value] of out.props) {
      const chip = node('span', 'vault-prop');
      chip.appendChild(node('b', '', key + ': '));
      chip.appendChild(document.createTextNode(value));
      props.appendChild(chip);
    }
    q('.vault-md').innerHTML = out.html || '<p class="vault-empty">(prázdná poznámka)</p>';
  }

  function mountEditor() {
    const host = q('.vault-edit');
    if (editor) {
      editor.setDoc(lastText);
      return;
    }
    host.textContent = '';
    editor = root.HubEditor.create({
      parent: host,
      doc: lastText,
      readOnly: false,
      notes: () => notes.map((n) => n.path),
      image: (name) => resolveImage(name),
      onChange: () => {
        dirty = true;
        if (proposes()) setState('neuložené změny', 'warn');
        else { setState('ukládá se…'); scheduleSave(); }
      },
      onSave: () => saveNow(),
      onOpenNote: (target) => {
        const path = resolveNote(target);
        if (path) { setMode('read'); load(path); }
        else io.toast('Poznámka „' + target + '" tu (zatím) není.');
      },
    });
    // Editor vzniká ve chvíli, kdy je jeho místo teprve odkryté — bez přeměření
    // by první náhled počítal s nulovou výškou a nevykreslil nic.
    requestAnimationFrame(() => editor && editor.view.requestMeasure());
    setState(proposes() ? 'úpravy se potvrzují kartou' : 'ukládá se samo');
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 900);
  }

  async function saveNow() {
    clearTimeout(saveTimer);
    if (!editor || !dirty || !current) return;
    const text = editor.getDoc();
    const path = current;
    setState('ukládám…');
    let res;
    try {
      res = await io.api('vault-save' + (io.vault ? '?vault=' + encodeURIComponent(io.vault) : ''),
                         {path, text, mtime: noteMtime});
    } catch (err) {
      setState('neuloženo', 'bad');
      io.toast('Uložit se nepodařilo: ' + err.message);
      return;
    }
    if (!box) return;
    if (res.conflict) {
      setState('změnila se jinde', 'bad');
      io.toast('Poznámka se mezitím změnila jinde — otevři ji znovu a změny přenes ručně.');
      return;
    }
    if (!res.ok) {
      setState('neuloženo', 'bad');
      io.toast(res.error || 'Uložit se nepodařilo.');
      return;
    }
    dirty = false;
    lastText = text;
    if (res.proposal) {
      setState('čeká na potvrzení', 'warn');
      io.toast('Návrh je připravený — potvrď ho kartou „Nahrát".');
      return;
    }
    noteMtime = res.mtime || noteMtime;
    setState('uloženo');
    q('.vault-date').textContent = kdyUpraveno(noteMtime);
    if (res.created && !notes.some((n) => n.path === res.path)) {
      notes.push({path: res.path, mtime: noteMtime});
      resolveNote = indexOf(notes.map((n) => n.path), true);
      renderList();
      markCurrent();
    }
  }

  /* Okno „kam": výběr složky ze seznamu (a jméno) místo psaní cesty.
     Vrací {folder, name} nebo null. */
  function allFolders() {
    const set = new Set();
    for (const p of [...notes.map((n) => n.path), ...files.map((f) => f.path)]) {
      const parts = p.split('/').slice(0, -1);
      for (let i = 1; i <= parts.length; i++) set.add(parts.slice(0, i).join('/'));
    }
    for (const l of links) set.add(l.path);
    // Do cizích složek ani do kořene Lidé/Sdílené se zapisovat nedá.
    return [...set].filter((d) => d !== 'Lidé' && d !== 'Sdílené')
      .sort((a, b) => a.localeCompare(b, 'cs'));
  }

  function placeDialog({title, withName, name = '', folder = '', ok}) {
    return new Promise((resolve) => {
      const wrap = node('div', 'onb set-modal vault-access-modal' + (isFirma() ? ' firma' : ''));
      wrap.innerHTML = `
        <div class="onb-box acc-box">
          <div class="onb-head"><div><div class="onb-title"></div></div>
            <span class="spacer"></span><button class="set-x pl-close" title="Zavřít">×</button></div>
          <div class="acc-list">
            <label class="mcp-field pl-name"><span>Jméno</span><input class="set-input"></label>
            <label class="mcp-field"><span>Složka</span><select class="set-input pl-folder"></select></label>
            <label class="mcp-field pl-new" hidden><span>Jméno nové složky</span><input class="set-input"></label>
          </div>
          <div class="who-foot"><button class="btn ghost pl-close">Zrušit</button><button class="btn pl-ok"></button></div>
        </div>`;
      wrap.querySelector('.onb-title').textContent = title;
      wrap.querySelector('.pl-ok').textContent = ok;
      const nameIn = wrap.querySelector('.pl-name input');
      wrap.querySelector('.pl-name').hidden = !withName;
      nameIn.value = name;
      const sel = wrap.querySelector('.pl-folder');
      const opt = (value, label) => {
        const o = node('option', '', label);
        o.value = value;
        sel.appendChild(o);
      };
      opt('', 'Hlavní složka');
      for (const d of allFolders()) {
        const depth = d.split('/').length - 1;
        opt(d, '\u00a0\u00a0'.repeat(depth) + d.split('/').pop());
      }
      opt('\u0000new', '+ Nová složka…');
      sel.value = allFolders().includes(folder) ? folder : '';
      const newRow = wrap.querySelector('.pl-new');
      sel.onchange = () => {
        newRow.hidden = sel.value !== '\u0000new';
        if (!newRow.hidden) newRow.querySelector('input').focus();
      };
      const done = (v) => { wrap.remove(); resolve(v); };
      for (const b of wrap.querySelectorAll('.pl-close')) b.onclick = () => done(null);
      wrap.addEventListener('click', (ev) => { if (ev.target === wrap) done(null); });
      const submit = () => {
        let dir = sel.value;
        if (dir === '\u0000new') {
          const fresh = newRow.querySelector('input').value.trim().replace(/[\\/]+/g, '-');
          if (!fresh) return io.toast('Napiš jméno nové složky.');
          // Nová složka vzniká v hlavní úrovni — tam ji člověk hledá.
          // Ve firemních poznámkách v Lidé/<já>, když zrovna pracuje tam.
          const own = links.find((l) => l.kind === 'osobni' && (folder + '/').startsWith(l.path + '/'));
          dir = (own ? own.path + '/' : '') + fresh;
        }
        const n = nameIn.value.trim().replace(/[\\/]+/g, '-');
        if (withName && !n) return io.toast('Napiš jméno.');
        done({folder: dir, name: n});
      };
      wrap.querySelector('.pl-ok').onclick = submit;
      wrap.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') submit();
        if (ev.key === 'Escape') { ev.stopPropagation(); done(null); }
      });
      document.body.appendChild(wrap);
      (withName ? nameIn : sel).focus();
    });
  }

  async function newNote() {
    const got = await placeDialog({title: 'Nová poznámka', withName: true, ok: 'Vytvořit',
                                   folder: defaultFolder()});
    if (!got) return;
    const path = (got.folder ? got.folder + '/' : '') + got.name.replace(/\.md$/i, '') + '.md';
    if (notes.some((n) => n.path.toLowerCase() === path.toLowerCase())) {
      io.toast('Taková poznámka už tu je.');
      return load(path);
    }
    lastText = '# ' + baseName(path) + '\n\n';
    current = path;
    noteMtime = 0;
    dirty = true;
    setPath(path);
    q('.vault-date').textContent = '';
    q('.vault-props').textContent = '';
    q('.vault-backlinks').textContent = '';
    q('.vault-modes').hidden = false;
    setMode('edit');
    if (editor) editor.setDoc(lastText);
    if (!proposes()) saveNow();
  }

  async function renameNote() {
    if (!current || proposes() || !canEdit()) return;
    const was = current;
    const dir = was.includes('/') ? was.split('/').slice(0, -1).join('/') : '';
    const got = await placeDialog({title: 'Přejmenovat nebo přesunout', withName: true,
                                   name: baseName(was), folder: dir, ok: 'Uložit'});
    if (!got) return;
    const name = (got.folder ? got.folder + '/' : '') + got.name.replace(/\.md$/i, '');
    if (name === was.replace(/\.md$/i, '')) return;
    if (dirty) await saveNow();
    let res;
    try {
      res = await io.api('vault-rename' + (io.vault ? '?vault=' + encodeURIComponent(io.vault) : ''),
                         {path: was, to: name});
    } catch (err) {
      return io.toast('Přejmenovat se nepodařilo: ' + err.message);
    }
    if (!box) return;
    if (!res.ok) return io.toast(res.error || 'Přejmenovat se nepodařilo.');
    notes = notes.filter((n) => n.path !== was).concat([{path: res.path, mtime: noteMtime}]);
    resolveNote = indexOf(notes.map((n) => n.path), true);
    back = back.filter((p) => p !== was);
    current = res.path;
    renderList();
    await load(res.path, {remember: false});
    io.toast(res.relinked
      ? `Přejmenováno · odkazy opraveny v ${res.relinked} poznámkách`
      : 'Přejmenováno.');
  }

  async function deleteNote() {
    if (!current || !canDelete()) return;
    const was = current;
    const firma = isFirma() && !linkOf(was);
    if (!(await HubDialog.confirm(`Smazat poznámku „${baseName(was)}“? ` +
        (firma ? 'Zmizí všem ve firmě; na serveru se odloží stranou, správce ji umí vrátit.'
               : 'Přesune se do koše — dá se vrátit.'), {title: 'Smazat poznámku', ok: 'Smazat'}))) return;
    clearTimeout(saveTimer);
    dirty = false;
    let res;
    try {
      if (firma) {
        // Ve firmě maže brána (jen z téhle stránky, s cookie brány).
        const r = await fetch('/gw/firma/smazat', {method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'X-Hub-Firma': '1'},
          body: JSON.stringify({firma: io.vault === 'firma' ? 'firma' : io.vault.slice(6), cesta: was})});
        res = await r.json().catch(() => ({}));
        if (!r.ok) res = {ok: false, error: res.error};
      } else {
        res = await io.api('vault-delete' + (io.vault ? '?vault=' + encodeURIComponent(io.vault) : ''),
                           {path: was});
      }
    } catch (err) {
      return io.toast('Smazat se nepodařilo: ' + err.message);
    }
    if (!box) return;
    if (!res.ok) return io.toast(res.error || 'Smazat se nepodařilo.');
    notes = notes.filter((n) => n.path !== was);
    resolveNote = indexOf(notes.map((n) => n.path), true);
    back = back.filter((p) => p !== was);
    current = '';
    renderList();
    io.toast('Poznámka je v koši.');
    const next = back.pop() || (notes[0] && notes[0].path);
    if (next) return load(next, {remember: false});
    setMode('read');
    setPath('');
    q('.vault-md').textContent = '';
    q('.vault-rename').hidden = q('.vault-del').hidden = true;
  }

  /* ── soubory: nahrání a stažení ──────────────────────────────────────────
     Do osobního trezoru (i Lidé/<já> ve firemním) nahrává hub rovnou
     (/api/vault-upload). Do firemních a sdílených složek brána
     (/gw/firma/soubor) — jen z téhle stránky, s cookie brány, kterou Claude
     v prostoru nemá. Práva, cestu i velikost ověřuje server znovu. */
  const UPLOAD_MAX = 20 * 1024 * 1024;

  function defaultFolder() {
    const dir = current.includes('/') ? current.split('/').slice(0, -1).join('/') : '';
    if (io && io.vault === 'firma' && io.readonly && !linkOf(dir + '/x')) {
      const own = links.find((l) => l.kind === 'osobni');
      return own ? own.path : dir;
    }
    return dir;
  }

  function download(path) {
    const a = document.createElement('a');
    a.href = io.fileUrl(path) + '&download=1';
    a.download = path.split('/').pop();
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // Kam soubor na cestě `rel` doopravdy jde: {hub: true} nebo {vault, cesta}.
  function uploadTarget(rel) {
    if (!io.vault) return {hub: true, path: rel};
    if (io.vault !== 'firma') return {vault: io.vault, cesta: rel};
    const l = linkOf(rel);
    if (l && l.kind === 'osobni') return {hub: true, path: rel};
    if (l && l.slug) return {vault: 'sdilene:' + l.slug, cesta: rel.slice(l.path.length + 1)};
    return {vault: 'firma', cesta: rel};
  }

  async function sendFile(file, rel, overwrite) {
    const t = uploadTarget(rel);
    let res;
    if (t.hub) {
      res = await fetch(io.uploadUrl(t.path, overwrite), {method: 'POST', body: file,
        headers: {'Content-Type': 'application/octet-stream'}});
    } else {
      res = await fetch('/gw/firma/soubor?vault=' + encodeURIComponent(t.vault) +
                        '&cesta=' + encodeURIComponent(t.cesta) + (overwrite ? '&prepsat=1' : ''), {
        method: 'POST', body: file, credentials: 'same-origin',
        headers: {'Content-Type': 'application/octet-stream', 'X-Hub-Firma': '1'}});
    }
    const data = await res.json().catch(() => ({}));
    if (res.status === 409 || data.exists) return {exists: true};
    if (!res.ok || !data.ok) throw new Error(data.error || 'Nahrání se nepovedlo, zkus to znovu.');
    return data;
  }

  async function uploadFiles(list, folder) {
    folder = String(folder || '').trim().replace(/^\/+|\/+$/g, '');
    let done = 0;
    const sent = [];
    for (const file of list) {
      if (file.size > UPLOAD_MAX) { io.toast(file.name + ': víc než 20 MB — nenahráno.'); continue; }
      if (!file.size) { io.toast(file.name + ': prázdný soubor — nenahráno.'); continue; }
      const rel = (folder ? folder + '/' : '') + file.name;
      setState('nahrávám ' + file.name + '…');
      try {
        let out = await sendFile(file, rel, false);
        if (out.exists) {
          if (!(await HubDialog.confirm(`„${rel}“ už tu je. Přepsat?`, {title: 'Soubor už existuje', ok: 'Přepsat'}))) continue;
          out = await sendFile(file, rel, true);
        }
        done++;
        sent.push(rel);
      } catch (err) {
        io.toast(file.name + ': ' + err.message);
      }
    }
    setState('');
    if (!done || !box) return;
    await reloadTree();
    const missing = sent.filter((p) => !files.some((f) => f.path === p) && !notes.some((n) => n.path === p));
    io.toast((done === 1 ? 'Nahrán 1 soubor' : `Nahráno souborů: ${done}`) +
             (missing.length ? ' — ve složce s omezeným přístupem se ukáže po restartu prostoru.' : '.'));
  }

  async function reloadTree() {
    try {
      const tree = await io.api('vault-tree' + (io.vault ? '?vault=' + encodeURIComponent(io.vault) : ''));
      if (!box) return;
      notes = tree.notes || [];
      files = tree.files || [];
      links = tree.slozky || links;
      resolveNote = indexOf(notes.map((n) => n.path), true);
      renderList();
    } catch (err) { /* seznam zůstane, jaký byl */ }
  }

  /* ── graf trezoru ─────────────────────────────────────────────────────── */

  async function toggleGraph() {
    if (graph) return closeGraph();
    if (!root.HubGraph) return io.toast('Graf se nenačetl — zkus obnovit stránku.');
    const wrap = q('.vault-graph');
    wrap.hidden = false;
    q('.vault-graph-btn').classList.add('on');
    let data;
    try {
      data = await io.api('vault-graph' + (io.vault ? '?vault=' + encodeURIComponent(io.vault) : ''));
    } catch (err) {
      wrap.hidden = true;
      q('.vault-graph-btn').classList.remove('on');
      io.toast('Graf se nepodařilo načíst: ' + err.message);
      return;
    }
    if (!box || wrap.hidden) return;
    graph = root.HubGraph.render(q('.graph-mount'), data, {
      onOpenNote: (path) => { closeGraph(); load(path); },
      // Správce poznámek: pravým tlačítkem (na dotyku podržením) na puntík
      // nastaví, kdo poznámku vidí. Vrací, jestli se něco otevřelo.
      onNodeMenu: (path) => !!(acl && acl.spravce) && (openWho(path), true),
    });
    graph.setFocus(current);
    graph.setMarked(lockedPaths());
    graphPanel(data);
  }

  function closeGraph() {
    if (graph) graph.destroy();
    graph = null;
    if (!box) return;
    q('.vault-graph').hidden = true;
    q('.graph-side').textContent = '';
    q('.vault-graph-btn').classList.remove('on');
  }

  function graphPanel(data) {
    const side = q('.graph-side');
    side.textContent = '';
    const opts = graph.options();
    let counts = () => {};          // přepíše se, až bude kam počty psát
    const section = (title) => {
      const el = node('div', 'graph-section');
      el.appendChild(node('div', 'graph-title', title));
      side.appendChild(el);
      return el;
    };
    const slider = (parent, label, key, min, max, step) => {
      const row = node('label', 'graph-row');
      row.appendChild(node('span', '', label));
      const input = document.createElement('input');
      input.type = 'range';
      input.min = min; input.max = max; input.step = step;
      input.value = opts[key];
      input.addEventListener('input', () => {
        graph.setOption(key, parseFloat(input.value));
        counts();
      });
      row.appendChild(input);
      parent.appendChild(row);
    };
    const toggle = (parent, label, key, invert) => {
      const row = node('label', 'graph-row graph-check');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = invert ? !opts[key] : !!opts[key];
      input.addEventListener('change', () => {
        graph.setOption(key, invert ? !input.checked : input.checked);
        counts();
      });
      row.appendChild(input);
      row.appendChild(node('span', '', label));
      parent.appendChild(row);
    };

    const filters = section('Filtry');
    const find = document.createElement('input');
    find.type = 'search';
    find.className = 'vault-search graph-find';
    find.placeholder = 'Hledat v grafu…';
    find.addEventListener('input', () => { graph.setFilter(find.value); counts(); });
    filters.appendChild(find);
    toggle(filters, 'Poznámky bez odkazů', 'showOrphans');
    toggle(filters, 'Nenalezené odkazy', 'hideUnresolved', true);
    const local = node('label', 'graph-row');
    local.appendChild(node('span', '', 'Okolí otevřené'));
    const depth = document.createElement('select');
    for (const [v, t] of [[0, 'všechno'], [1, '1 krok'], [2, '2 kroky'], [3, '3 kroky']]) {
      const o = document.createElement('option');
      o.value = String(v); o.textContent = t;
      depth.appendChild(o);
    }
    depth.value = String(opts.localDepth || 0);
    depth.addEventListener('change', () => {
      graph.setFocus(current);
      graph.setOption('localDepth', parseInt(depth.value, 10));
      counts();
    });
    local.appendChild(depth);
    filters.appendChild(local);

    const display = section('Zobrazení');
    slider(display, 'Velikost uzlů', 'nodeSizeMultiplier', 0.3, 3, 0.1);
    slider(display, 'Tloušťka čar', 'lineSizeMultiplier', 0.2, 4, 0.1);
    slider(display, 'Mizení textu', 'textFadeMultiplier', -1, 3, 0.1);
    toggle(display, 'Šipky', 'showArrow');

    if (acl && acl.spravce) {
      const access = section('Přístupy');
      access.appendChild(node('p', 'graph-note',
        'Puntík s kroužkem vidí jen vybraní lidé. Pravým tlačítkem (na telefonu ' +
        'podržením) na puntík nastavíš, kdo poznámku vidí.'));
    }

    const forces = section('Síly');
    slider(forces, 'Střed', 'centerStrength', 0, 3, 0.1);
    slider(forces, 'Odpuzování', 'repelStrength', 1, 40, 1);
    slider(forces, 'Síla odkazů', 'linkStrength', 0, 3, 0.1);
    slider(forces, 'Délka odkazů', 'linkDistance', 40, 600, 10);

    const groups = (data.settings && data.settings.groups) || [];
    const legend = section(groups.length ? 'Skupiny' : 'Barvy podle složek');
    const folders = [...new Set(data.nodes.filter((n) => !n.missing && n.path.includes('/'))
                                  .map((n) => n.path.split('/')[0]))].slice(0, 10);
    const items = groups.length ? groups.map((g) => [g.color, g.query])
                                : folders.map((f, i) => [null, f]);
    for (const [color, label] of items) {
      const row = node('div', 'graph-legend');
      const dot = node('span', 'graph-dot');
      dot.style.background = color || '';
      if (!color) dot.dataset.folder = label;
      row.appendChild(dot);
      row.appendChild(node('span', '', label));
      legend.appendChild(row);
    }

    const foot = node('div', 'graph-foot');
    const info = node('span', 'graph-count', '');
    const center = node('button', 'btn ghost', 'Vycentrovat');
    center.onclick = () => graph.center();
    foot.appendChild(info);
    foot.appendChild(center);
    side.appendChild(foot);
    counts = () => {
      const c = graph.counts();
      info.textContent = c.nodes + ' poznámek · ' + c.links + ' odkazů';
    };
    counts();
    // Barvy podle složek přiřazuje graf sám — legenda si je vezme z plátna.
    for (const dot of side.querySelectorAll('.graph-dot[data-folder]')) {
      const found = data.nodes.find((n) => n.path.split('/')[0] === dot.dataset.folder);
      dot.style.background = found ? graphColor(data, found) : '';
    }
  }

  function graphColor(data, node) {
    const folders = [...new Set(data.nodes.filter((n) => !n.missing && n.path.includes('/'))
                                 .map((n) => n.path.split('/')[0]))];
    const PALETTE = ['#e0843c', '#5aa9e6', '#44cf6e', '#c678dd', '#e5c07b',
                     '#e06c75', '#56b6c2', '#a3be8c', '#d19a66', '#7aa2f7'];
    const i = folders.indexOf(node.path.split('/')[0]);
    return i >= 0 ? PALETTE[i % PALETTE.length] : '#9fa3ad';
  }

  function onKey(ev) {
    if (!box || ev.key !== 'Escape') return;
    ev.stopPropagation();
    if (graph) return closeGraph();
    // Při psaní patří Escape editoru (zavře našeptávání nebo hledání), ne oknu.
    if (mode === 'edit' && editor && editor.view.hasFocus) return;
    const search = q('.vault-search');
    if (document.activeElement === search && search.value) {
      search.value = '';
      renderList();
      return;
    }
    close();
  }

  function close() {
    if (!box) return;
    if (io && io.onPlace) io.onPlace(null);
    // Rozepsanou změnu v osobním trezoru ještě uložit — okno se zavírá i Escapem.
    if (dirty && editor && !proposes()) saveNow();
    if (graph) { graph.destroy(); graph = null; }
    if (editor) { editor.destroy(); editor = null; }
    document.removeEventListener('keydown', onKey, true);
    clearTimeout(searchTimer);
    clearTimeout(saveTimer);
    box.remove();
    box = null;
    current = '';
    back = [];
    acl = null;
    mode = 'read';
    lastText = '';
    dirty = false;
  }

  function failure(text) {
    q('.vault-md').textContent = '';
    q('.vault-md').appendChild(node('p', 'vault-empty', text));
  }

  /* ── přístupy k firemnímu Obsidianu (jen admin) ───────────────────────────
     Seznam lidí s úrovní: nevidí / čte / čte a zapisuje. Mění se hned
     v bráně; kdo přístup ztratil, tomu brána prostor zastaví, jakmile
     nepracuje, a trezor se mu po dalším startu do prostoru nepřiváže. */
  const LEVELS = [['none', 'Nevidí'], ['read', 'Čte'], ['write', 'Čte i zapisuje']];

  async function gw(method, body) {
    const res = await fetch('/gw/firma/pristupy', {
      method, credentials: 'same-origin',
      headers: body ? {'Content-Type': 'application/json', 'X-Hub-Account': '1'} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
    return data;
  }

  async function openAccess() {
    const wrap = node('div', 'onb set-modal vault-access-modal firma');
    wrap.innerHTML = `
      <div class="onb-box acc-box">
        <div class="onb-head">
          <div>
            <div class="onb-title">Kdo vidí firemní poznámky</div>
            <div class="onb-sub">Správci je vidí vždycky. Změna platí hned;
              kdo přístup nově dostal, uvidí poznámky po restartu svého prostoru.</div>
          </div>
          <span class="spacer"></span>
          <button class="set-x acc-close" title="Zavřít (Esc)">×</button>
        </div>
        <div class="acc-list"><p class="vault-empty">načítám…</p></div>
      </div>`;
    document.body.appendChild(wrap);
    const shut = () => { wrap.remove(); document.removeEventListener('keydown', esc, true); };
    function esc(ev) { if (ev.key === 'Escape') { ev.stopPropagation(); shut(); } }
    document.addEventListener('keydown', esc, true);
    wrap.querySelector('.acc-close').onclick = shut;
    wrap.addEventListener('click', (ev) => { if (ev.target === wrap) shut(); });
    const list = wrap.querySelector('.acc-list');
    let data;
    try {
      data = await gw('GET');
    } catch (err) {
      list.textContent = '';
      list.appendChild(node('p', 'vault-empty', err.message));
      return;
    }
    list.textContent = '';
    for (const p of data.people || []) {
      const row = node('div', 'acc-row');
      const who = node('div', 'acc-who');
      who.append(node('strong', '', p.name || p.email), node('span', 'acc-mail', p.email));
      row.appendChild(who);
      if (p.role === 'admin') {
        row.appendChild(node('span', 'acc-admin', 'správce · čte i upravuje'));
      } else {
        const sel = node('div', 'acc-seg');
        for (const [lvl, label] of LEVELS) {
          const b = node('button', 'acc-opt' + (p.level === lvl ? ' on' : ''), label);
          b.type = 'button';
          b.onclick = async () => {
            if (p.level === lvl) return;
            sel.classList.add('busy');
            try {
              const out = await gw('POST', {id: p.id, level: lvl});
              p.level = out.level;
              for (const o of sel.children) o.classList.toggle('on', o === b);
              io.toast((p.name || p.email) + ': ' + label.toLowerCase() +
                       (out.note ? ' — ' + out.note : ''));
            } catch (err) {
              io.toast(err.message);
            }
            sel.classList.remove('busy');
          };
          sel.appendChild(b);
        }
        row.appendChild(sel);
      }
      list.appendChild(row);
    }
  }

  /* ── kdo vidí kterou firemní poznámku (jen správci poznámek) ──────────────
     Rozhoduje brána (/gw/firma/poznamky, gateway/poznamky.py): omezenou
     poznámku ostatní v prostoru vůbec nemají. Správci poznámek je pevný
     seznam lidí na serveru, ne role admin. */
  let acl = null;         // {spravce, zmena, poznamky: {cesta: [{email, name}]}, lide}

  const restricted = (path) => !!(acl && acl.poznamky && acl.poznamky[path]);
  const whoText = (path) => {
    const people = (acl && acl.poznamky && acl.poznamky[path]) || [];
    return people.length ? people.map((p) => p.name || p.email).join(', ') : 'správci';
  };

  async function aclFetch(method, body) {
    const res = await fetch('/gw/firma/poznamky' + firmaQuery(), {
      method, credentials: 'same-origin',
      headers: body ? {'Content-Type': 'application/json', 'X-Hub-Account': '1'} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'Server teď neodpovídá, zkus to za chvíli.');
    return data;
  }

  async function loadAcl() {
    if (!isFirma()) return;
    try {
      acl = await aclFetch('GET');
    } catch (err) {
      acl = null;                      // bez brány (počítač) nic takového není
      return;
    }
    if (!box) return;
    const notice = q('.vault-notice');
    notice.hidden = !acl.zmena;
    if (acl.zmena) {
      notice.textContent = 'Přístupy k firemním poznámkám se změnily. ';
      const b = node('button', 'btn ghost', 'Restartovat prostor');
      b.onclick = () => io.restartSpace &&
        io.restartSpace('Změněné přístupy k firemním poznámkám se do prostoru načtou po restartu.');
      notice.appendChild(b);
    }
    renderList();
    whoButton();
    if (graph) graph.setMarked(lockedPaths());
  }

  const lockedPaths = () => new Set(acl && acl.spravce ? Object.keys(acl.poznamky || {}) : []);

  function whoButton() {
    const b = q('.vault-who');
    if (!b) return;
    b.hidden = !(acl && acl.spravce && current) || /^(Lidé|Sdílené)\//.test(current);
    if (b.hidden) return;
    b.textContent = restricted(current) ? '🔒 Vidí: ' + whoText(current) : 'Vidí: všichni';
    b.classList.toggle('on', restricted(current));
  }

  async function openWho(path) {
    if (!acl || !acl.spravce || !path) return;
    const chosen = new Set(((acl.poznamky || {})[path] || []).map((p) => p.email));
    let only = chosen.size > 0;
    const folder = !/\.md$/i.test(path);
    const what = folder ? 'Složku' : 'Poznámku';
    const wrap = node('div', 'onb set-modal vault-access-modal firma');
    wrap.innerHTML = `
      <div class="onb-box acc-box">
        <div class="onb-head">
          <div>
            <div class="onb-title who-title">Kdo vidí poznámku</div>
            <div class="onb-sub who-path"></div>
          </div>
          <span class="spacer"></span>
          <button class="set-x acc-close" title="Zavřít (Esc)">×</button>
        </div>
        <div class="acc-list">
          <div class="acc-seg who-mode">
            <button type="button" class="acc-opt" data-only="0">Všichni</button>
            <button type="button" class="acc-opt" data-only="1">Jen vybraní</button>
          </div>
          <p class="who-hint"></p>
          <div class="who-people"></div>
        </div>
        <div class="who-foot">
          <button class="btn ghost acc-close">Zrušit</button>
          <button class="btn who-save">Uložit</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    wrap.querySelector('.who-path').textContent = folder ? path + '/' : path;
    wrap.querySelector('.who-title').textContent = folder ? 'Kdo vidí složku' : 'Kdo vidí poznámku';
    const shut = () => { wrap.remove(); document.removeEventListener('keydown', esc, true); };
    function esc(ev) { if (ev.key === 'Escape') { ev.stopPropagation(); shut(); } }
    document.addEventListener('keydown', esc, true);
    for (const b of wrap.querySelectorAll('.acc-close')) b.onclick = shut;
    wrap.addEventListener('click', (ev) => { if (ev.target === wrap) shut(); });
    const list = wrap.querySelector('.who-people');
    const paint = () => {
      for (const b of wrap.querySelectorAll('.who-mode .acc-opt')) {
        b.classList.toggle('on', (b.dataset.only === '1') === only);
      }
      wrap.querySelector('.who-hint').textContent = only
        ? what + ' uvidí jen zaškrtnutí a správci poznámek. Ostatní ' + (folder ? 'ji i se vším, co v ní je (i s tím, co přibude),' : 'ji') +
          ' ve svém prostoru nebudou mít vůbec — ani Claude.'
        : what + ' vidí každý, kdo vidí firemní poznámky' + (folder ? ' (omezené poznámky v ní dál jen vybraní).' : '.');
      list.hidden = !only;
    };
    for (const b of wrap.querySelectorAll('.who-mode .acc-opt')) {
      b.onclick = () => { only = b.dataset.only === '1'; paint(); };
    }
    for (const p of acl.lide || []) {
      const row = node('label', 'acc-row who-row');
      const box_ = document.createElement('input');
      box_.type = 'checkbox';
      const who = node('div', 'acc-who');
      who.append(node('strong', '', p.name || p.email), node('span', 'acc-mail', p.email));
      row.append(box_, who);
      if (p.spravce) {
        box_.checked = true;
        box_.disabled = true;
        row.appendChild(node('span', 'acc-admin', 'správce · vidí vždy'));
      } else if (p.firma === 'none') {
        box_.disabled = true;
        row.appendChild(node('span', 'acc-admin', 'nevidí firemní poznámky'));
      } else {
        box_.checked = chosen.has(p.email);
        box_.onchange = () => (box_.checked ? chosen.add(p.email) : chosen.delete(p.email));
      }
      list.appendChild(row);
    }
    paint();
    const save = wrap.querySelector('.who-save');
    save.onclick = async () => {
      const emails = only ? [...chosen] : [];
      if (only && !emails.length &&
          !(await HubDialog.confirm('Nikoho jsi nevybral — ' + what.toLowerCase() + ' uvidí jen správci poznámek. Pokračovat?', {title: 'Nikdo nevybrán', ok: 'Pokračovat'}))) return;
      save.disabled = true;
      try {
        const out = await aclFetch('POST', {cesta: path, emaily: emails, jen: only});
        io.toast(path + ': ' + (!out.jen ? 'vidí všichni'
          : 'vidí jen ' + (out.people.length ? out.people.join(', ') + ' a správci' : 'správci')) +
                 (out.note ? ' — ' + out.note : ''));
        shut();
        await loadAcl();
      } catch (err) {
        io.toast(err.message);
        save.disabled = false;
      }
    };
  }

  async function open(opts) {
    close();
    io = opts;
    if (io.onPlace) io.onPlace(opts.path || '');
    // Firemní trezor je fialový jako všude jinde — třída přebije barvu
    // prostředí uvnitř celého okna, ať se osobní a firemní nespletou.
    box = node('div', 'onb set-modal vault-modal' + (isFirma() ? ' firma' : ''));
    box.innerHTML = `
      <div class="onb-box">
        <div class="onb-head">
          <span class="onb-mark"><svg class="ico" width="30" height="30"><use href="#i-book"/></svg></span>
          <div>
            <div class="onb-title">Obsidian</div>
            <div class="onb-sub">načítám…</div>
          </div>
          <div class="vault-trezory" hidden></div>
          <span class="spacer"></span>
          <button class="btn ghost vault-new" title="Nová poznámka" hidden>+ Nová</button>
          <button class="btn ghost vault-upload" title="Nahrát soubory (PDF, obrázky, tabulky…) — jde i přetáhnout na seznam" hidden>Nahrát soubor</button>
          <input class="vault-upload-input" type="file" multiple hidden>
          <button class="btn ghost vault-rename" title="Přejmenovat poznámku" hidden>Přejmenovat</button>
          <button class="btn ghost vault-del" title="Smazat poznámku" hidden>Smazat</button>
          <button class="btn ghost vault-graph-btn" title="Graf poznámek">Graf</button>
          <button class="btn ghost vault-access" title="Kdo firemní Obsidian vidí a kdo do něj smí zapisovat" hidden>Přístupy</button>
          <button class="btn ghost vault-app" hidden>Otevřít v Obsidianu</button>
          <button class="set-x vault-close" title="Zavřít (Esc)">×</button>
        </div>
        <div class="onb-body set-body">
          <nav class="set-nav vault-nav">
            <div class="vault-notice" hidden></div>
            <input class="vault-search" type="search" placeholder="Hledat v poznámkách…" autocomplete="off">
            <div class="vault-list"></div>
          </nav>
          <div class="set-panel vault-panel">
            <div class="vault-bar">
              <button class="btn ghost vault-back" title="Zpět" hidden>←</button>
              <span class="vault-path"></span>
              <span class="vault-state"></span>
              <span class="vault-date"></span>
              <button class="btn ghost vault-who" title="Kdo tuhle poznámku vidí" hidden></button>
              <div class="vault-modes" hidden>
                <button class="vault-mode on" data-mode="read">Čtení</button>
                <button class="vault-mode" data-mode="edit">Úpravy</button>
              </div>
            </div>
            <div class="vault-props"></div>
            <article class="vault-md"></article>
            <div class="vault-edit" hidden></div>
            <div class="vault-backlinks"></div>
          </div>
          <div class="vault-graph" hidden>
            <div class="graph-mount"></div>
            <aside class="graph-side"></aside>
          </div>
        </div>
      </div>`;
    document.body.appendChild(box);
    document.addEventListener('keydown', onKey, true);
    // Zavřít jen klikem, který na pozadí i začal: výběr textu tažením ven
    // z okna končí puštěním tlačítka na pozadí a prohlížeč to hlásí jako klik.
    let downOutside = false;
    box.addEventListener('pointerdown', (ev) => { downOutside = ev.target === box; });
    box.addEventListener('click', (ev) => { if (ev.target === box && downOutside) close(); });
    q('.vault-close').onclick = close;
    /* Přepínač Moje / Firemní / Sdílené — poznámky jsou jen tady, ne
       v panelu vlevo. S jediným trezorem se neukazuje. */
    const trezory = (io.trezory || []);
    const prepinac = q('.vault-trezory');
    if (trezory.length > 1 && io.prepni) {
      prepinac.hidden = false;
      for (const t of trezory) {
        const b = node('button', 'vault-trezor' + (t.id === (io.vault || '') ? ' on' : '') +
                                 (/^firma(:|$)/.test(t.id) ? ' firma' : ''), t.name);
        b.title = t.shared ? 'Sdílené poznámky' : t.name;
        b.onclick = () => { if (t.id !== (io.vault || '')) io.prepni(t.id); };
        prepinac.appendChild(b);
      }
    }
    if (io.sdilenyMenu || io.novySdileny) {
      prepinac.hidden = false;
      if (io.sdilenyMenu) {
        const m = node('button', 'vault-trezor ghost', 'Spravovat sdílené');
        m.title = 'Sdílené poznámky: kdo je vidí, odejít, smazat';
        m.onclick = () => { const r = m.getBoundingClientRect(); io.sdilenyMenu(r.left, r.bottom); };
        prepinac.appendChild(m);
      }
      if (io.novySdileny) {
        const n = node('button', 'vault-trezor ghost', '+ Sdílené');
        n.title = 'Založit poznámky jen pro vybrané lidi';
        n.onclick = io.novySdileny;
        prepinac.appendChild(n);
      }
    }
    /* Šipka zpět: nejdřív se vrací po odkazech, kterými se člověk proklikal,
       a když už žádné nejsou, vrátí na telefonu seznam poznámek. Na počítači
       je seznam pořád vedle, tam tedy jen mizí. */
    q('.vault-back').onclick = () => {
      if (back.length) return load(back.pop(), {remember: false});
      box.classList.remove('reading');
      current = '';
      renderList();
    };
    /* Na úzkém telefonu se pět tlačítek do hlavičky nevejde a láme se do
       druhého řádku. Přejmenovat a Smazat patří k otevřené poznámce, ne
       k trezoru — tak se stěhují dolů k ní, do řádku s cestou a režimem.
       Posunutím prvku se nic neodpojí, tlačítka fungují dál. */
    if (window.matchMedia('(max-width: 520px)').matches) {
      q('.vault-bar').append(q('.vault-rename'), q('.vault-del'));
    }
    q('.vault-new').hidden = !(root.HubEditor && root.HubEditor.available) ||
      !!(io && io.readonly && !links.length);
    q('.vault-new').onclick = newNote;
    q('.vault-upload').hidden = !(io && io.uploadUrl);
    q('.vault-upload').onclick = () => q('.vault-upload-input').click();
    q('.vault-upload-input').onchange = (ev) => {
      const picked = [...ev.target.files];
      ev.target.value = '';
      if (!picked.length) return;
      placeDialog({title: picked.length === 1 ? 'Nahrát „' + picked[0].name + '“'
                                              : 'Nahrát ' + picked.length + ' soubory',
                   ok: 'Nahrát', folder: defaultFolder()})
        .then((got) => { if (got) uploadFiles(picked, got.folder); });
    };
    // Přetažení souborů na seznam: do složky, na kterou se pustily.
    const nav = q('.vault-nav');
    nav.addEventListener('dragover', (ev) => {
      if (!io.uploadUrl || ![...(ev.dataTransfer.types || [])].includes('Files')) return;
      ev.preventDefault();
      nav.classList.add('drop');
    });
    nav.addEventListener('dragleave', (ev) => { if (!nav.contains(ev.relatedTarget)) nav.classList.remove('drop'); });
    nav.addEventListener('drop', (ev) => {
      nav.classList.remove('drop');
      if (!io.uploadUrl || !ev.dataTransfer.files.length) return;
      ev.preventDefault();
      const det = ev.target.closest('details[data-dir]');
      uploadFiles([...ev.dataTransfer.files], det ? det.dataset.dir : defaultFolder());
    });
    q('.vault-rename').onclick = renameNote;
    q('.vault-del').onclick = deleteNote;
    q('.vault-graph-btn').onclick = toggleGraph;
    // Správa přístupů: jen admin a jen ve firemním trezoru. Rozhoduje brána
    // (/gw/firma/pristupy) — tlačítko je jen cesta k ní.
    q('.vault-access').hidden = !(io && io.vault === 'firma' && io.admin);
    q('.vault-access').onclick = openAccess;
    q('.vault-who').onclick = () => openWho(current);
    for (const b of box.querySelectorAll('.vault-mode')) {
      b.onclick = () => setMode(b.dataset.mode);
    }
    q('.vault-search').addEventListener('input', renderList);
    const pick = (ev) => {
      const b = ev.target.closest('.vault-item');
      if (b && b.dataset.file) return download(b.dataset.file);
      if (b) load(b.dataset.path);
    };
    q('.vault-list').addEventListener('click', pick);
    q('.vault-backlinks').addEventListener('click', pick);
    q('.vault-md').addEventListener('click', (ev) => {
      const a = ev.target.closest('a');
      if (!a) return;
      ev.preventDefault();
      if (a.classList.contains('vault-ext')) return io.openLink(a.dataset.href);
      if (a.dataset.note) return load(a.dataset.note, {heading: a.dataset.heading});
      if (a.dataset.heading) return scrollToHeading(a.dataset.heading);
      io.toast('Poznámka „' + a.textContent + '" tu (zatím) není.');
    });
    if (io.obsidian) {
      const app = q('.vault-app');
      app.hidden = false;
      app.onclick = () => io.openInObsidian(current);
    }

    let tree;
    try {
      tree = await io.api('vault-tree' + (io.vault ? '?vault=' + encodeURIComponent(io.vault) : ''));
    } catch (err) {
      if (!box) return;
      q('.onb-sub').textContent = '';
      console.error(err);
      failure('Poznámky se teď nepodařilo načíst. Zkus to prosím za chvíli znovu.');
      return;
    }
    if (!box) return;
    notes = tree.notes || [];
    files = tree.files || [];
    links = tree.slozky || [];
    q('.vault-new').hidden = !(root.HubEditor && root.HubEditor.available) ||
      !!(io && io.readonly && !links.length);
    resolveNote = indexOf(notes.map((n) => n.path), true);
    const imageOf = indexOf(tree.images || [], false);
    resolveImage = (name) => {
      const path = imageOf(name);
      return path ? io.fileUrl(path) : '';
    };
    q('.onb-title').textContent = io.title || 'Moje poznámky';
    q('.onb-sub').textContent = tree.exists === false ? 'složka s poznámkami chybí' : plural(notes.length);
    renderList();
    loadAcl();
    if (!notes.length) {
      failure(tree.exists === false ? 'Složka s poznámkami chybí.'
        : 'Zatím tu nejsou žádné poznámky — vytvoř první tlačítkem + Nová, nebo nahraj soubor.');
      return;
    }
    const has = (p) => notes.some((n) => n.path === p);
    /* Na telefonu se otevírá seznam, ne rovnou poznámka: vidí se vždycky jen
       jedno a začít u rozcestníku by znamenalo hned klepnout zpět. Když si
       ale o konkrétní poznámku někdo řekl (odkaz z paměti), otevře se ta. */
    const uzky = window.matchMedia('(max-width: 720px)').matches;
    if (opts.path && has(opts.path)) return load(opts.path, {remember: false});
    if (uzky) return;
    const start = ['memory/MEMORY.md', 'README.md'].find(has)
        || notes.slice().sort((a, b) => b.mtime - a.mtime)[0].path;
    load(start, {remember: false});
  }

  const api = {open, close, render, indexOf};
  root.HubVault = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
