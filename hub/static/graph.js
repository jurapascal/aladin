/* Graf trezoru — jako zobrazení grafu v Obsidianu.
 *
 * Kreslí se na plátno: poznámka = uzel, odkaz = čára. Uzly se rozestoupí samy
 * (odpuzování, pružiny odkazů, tah ke středu), dají se tahat, graf se posouvá
 * a přibližuje kolečkem. Najetím se zvýrazní soused, klik poznámku otevře.
 *
 * Barvy: když má trezor v Obsidianu barevné skupiny (.obsidian/graph.json),
 * použijí se ty. Jinak se obarví podle složky, ať je v tisícovce poznámek vidět,
 * co k sobě patří. Nenalezené odkazy jsou šedé jako v originále.
 */
(function (root) {
  'use strict';

  // Fialová je akcent grafu v Obsidianu; zbytek se bere z motivu hubu, aby byl
  // graf čitelný i ve světlém.
  const ACCENT = '#8a5cf5';

  function readColors() {
    const cs = typeof getComputedStyle === 'function' ? getComputedStyle(document.body) : null;
    const v = (name, fallback) => ((cs && cs.getPropertyValue(name)) || '').trim() || fallback;
    return {
      node: v('--dim', '#9fa3ad'), missing: v('--border', '#5c6068'),
      line: v('--border', '#30363d'), text: v('--fg', '#c8ccd4'),
      bright: v('--fg-bright', '#ffffff'), tag: v('--green', '#44cf6e'),
    };
  }
  // Barvy podle složek — když si trezor barevné skupiny nenastavil.
  const PALETTE = ['#e0843c', '#5aa9e6', '#44cf6e', '#c678dd', '#e5c07b',
                   '#e06c75', '#56b6c2', '#a3be8c', '#d19a66', '#7aa2f7'];

  const DEFAULTS = {
    nodeSizeMultiplier: 1, lineSizeMultiplier: 1, textFadeMultiplier: 0,
    centerStrength: 1, repelStrength: 10, linkStrength: 1, linkDistance: 250,
    showArrow: false, showTags: false, showOrphans: true, hideUnresolved: false,
    localDepth: 0,
  };

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const baseName = (p) => p.split('/').pop().replace(/\.md$/i, '');
  const folderOf = (p) => (p.includes('/') ? p.split('/')[0] : '');

  function colorFor(node, groups, folders, colors) {
    for (const g of groups) {
      if (!g.color) continue;
      const q = g.query.trim().toLowerCase();
      const path = node.path.toLowerCase();
      if (q.startsWith('path:') && path.includes(q.slice(5).trim())) return g.color;
      if (q.startsWith('file:') && baseName(path).includes(q.slice(5).trim())) return g.color;
      if (q.startsWith('tag:') && (node.tags || []).includes(q.slice(4).replace(/^#/, '').trim())) return g.color;
      if (!/^\w+:/.test(q) && path.includes(q)) return g.color;
    }
    if (node.missing) return colors.missing;
    if (node.tag) return colors.tag;
    if (!groups.length && folders.size > 1) {
      const idx = [...folders].indexOf(folderOf(node.path));
      if (idx >= 0) return PALETTE[idx % PALETTE.length];
    }
    return colors.node;
  }

  /* ── síly ──────────────────────────────────────────────────────────────── */

  // Barnes-Hut: uzly daleko od sebe se sečtou do jednoho těžiště, jinak by
  // tisíc poznámek znamenalo milion výpočtů na jedno překreslení.
  function quadtree(nodes) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    }
    const size = Math.max(maxX - minX, maxY - minY, 1) + 1;
    const root = {x: minX, y: minY, size, mass: 0, cx: 0, cy: 0, kids: null, node: null};
    const insert = (cell, n, depth) => {
      cell.mass++; cell.cx += n.x; cell.cy += n.y;
      if (depth > 18) return;
      if (!cell.kids && !cell.node) { cell.node = n; return; }
      if (!cell.kids) {
        const old = cell.node; cell.node = null;
        cell.kids = [0, 1, 2, 3].map((i) => ({
          x: cell.x + (i % 2) * cell.size / 2, y: cell.y + ((i >> 1) % 2) * cell.size / 2,
          size: cell.size / 2, mass: 0, cx: 0, cy: 0, kids: null, node: null,
        }));
        insert(cell.kids[which(cell, old)], old, depth + 1);
      }
      insert(cell.kids[which(cell, n)], n, depth + 1);
    };
    const which = (cell, n) => (n.x > cell.x + cell.size / 2 ? 1 : 0) +
                               (n.y > cell.y + cell.size / 2 ? 2 : 0);
    for (const n of nodes) insert(root, n, 0);
    return root;
  }

  function repel(node, cell, strength) {
    if (!cell.mass) return;
    const cx = cell.cx / cell.mass, cy = cell.cy / cell.mass;
    let dx = node.x - cx, dy = node.y - cy;
    let d2 = dx * dx + dy * dy;
    if (d2 < 0.01) { dx = (Math.random() - 0.5) * 2; dy = (Math.random() - 0.5) * 2; d2 = 1; }
    if (!cell.kids || cell.size * cell.size / d2 < 0.7) {
      if (cell.node === node) return;
      const f = strength * cell.mass / d2;
      node.vx += dx * f; node.vy += dy * f;
      return;
    }
    for (const kid of cell.kids) repel(node, kid, strength);
  }

  /* ── vykreslení ────────────────────────────────────────────────────────── */

  function render(parent, data, opts) {
    const cfg = Object.assign({}, DEFAULTS, data.settings || {});
    const groups = (data.settings && data.settings.groups) || [];
    const box = document.createElement('div');
    box.className = 'graph-box';
    const canvas = document.createElement('canvas');
    canvas.className = 'graph-canvas';
    box.appendChild(canvas);
    parent.appendChild(box);
    const ctx = canvas.getContext('2d');

    const folders = new Set(data.nodes.filter((n) => !n.missing).map((n) => folderOf(n.path)).filter(Boolean));
    const nodes = data.nodes.map((n, i) => ({
      i, path: n.path, missing: !!n.missing, tags: n.tags || [], deg: 0,
      x: Math.cos(i) * (60 + i % 400), y: Math.sin(i * 1.7) * (60 + i % 400), vx: 0, vy: 0,
    }));
    const links = [];
    const seen = new Set();
    for (const [a, b] of data.links) {
      const key = a < b ? a + ':' + b : b + ':' + a;
      if (seen.has(key) || !nodes[a] || !nodes[b]) continue;
      seen.add(key);
      links.push({a: nodes[a], b: nodes[b]});
      nodes[a].deg++; nodes[b].deg++;
    }
    const near = new Map(nodes.map((n) => [n, new Set()]));
    for (const l of links) { near.get(l.a).add(l.b); near.get(l.b).add(l.a); }
    let colors = readColors();
    // Poznámky s omezeným přístupem (vault.js, jen správci) — kroužek kolem.
    let marked = new Set();
    const paintNodes = () => {
      for (const n of nodes) n.color = colorFor(n, groups, folders, colors);
    };
    paintNodes();

    let view = {x: 0, y: 0, scale: 0.6};
    let ruka = false;                  // člověk sám posouvá / přibližuje — samo se pak nic nepřizpůsobuje
    /* Celý graf na jednu obrazovku (jako po otevření v Obsidianu): těžiště
       doprostřed a měřítko podle rozptylu uzlů, osamělé body daleko na okraji se přeskočí (jdou najít posunem). */
    function vejdi() {
      if (!shown.length) return;
      const rect = box.getBoundingClientRect();
      if (!rect.width) return;
      const xs = shown.map((n) => n.x).sort((a, b) => a - b);
      const ys = shown.map((n) => n.y).sort((a, b) => a - b);
      const lo = Math.floor(shown.length * 0.12), hi = Math.max(lo, Math.ceil(shown.length * 0.88) - 1);
      const w = Math.max(xs[hi] - xs[lo], 80), h = Math.max(ys[hi] - ys[lo], 80);
      view.scale = clamp(Math.min(rect.width / (w + 120), rect.height / (h + 120)), 0.06, 1.2);
      view.x = -((xs[lo] + xs[hi]) / 2) * view.scale;
      view.y = -((ys[lo] + ys[hi]) / 2) * view.scale;
    }
    let alpha = 1, hover = null, dragged = null, focus = '';
    let filter = '', shown = nodes, shownLinks = links;
    let raf = 0, stopped = false;

    function applyFilter() {
      const text = filter.trim().toLowerCase();
      const localSet = cfg.localDepth > 0 && focus ? localNodes(focus, cfg.localDepth) : null;
      const ok = (n) => {
        if (n.missing && cfg.hideUnresolved) return false;
        if (!cfg.showOrphans && !n.deg) return false;
        if (localSet && !localSet.has(n)) return false;
        if (text && !n.path.toLowerCase().includes(text)) return false;
        return true;
      };
      shown = nodes.filter(ok);
      const set = new Set(shown);
      shownLinks = links.filter((l) => set.has(l.a) && set.has(l.b));
      kick();
    }

    function localNodes(path, depth) {
      const start = nodes.find((n) => n.path === path);
      if (!start) return null;
      const set = new Set([start]);
      let edge = [start];
      for (let d = 0; d < depth; d++) {
        const next = [];
        for (const n of edge) {
          for (const m of near.get(n)) if (!set.has(m)) { set.add(m); next.push(m); }
        }
        edge = next;
      }
      return set;
    }

    function size(node) {
      return (3.2 + Math.sqrt(node.deg) * 1.8) * (cfg.nodeSizeMultiplier || 1);
    }

    function tick() {
      const tree = shown.length > 1 ? quadtree(shown) : null;
      // Kladné = uzly se odstrkávají; se záporným by se všechny slily do bodu.
      const repelK = cfg.repelStrength * 14;
      for (const n of shown) {
        if (n === dragged) continue;
        if (tree) repel(n, tree, repelK);
        n.vx -= n.x * 0.004 * cfg.centerStrength;
        n.vy -= n.y * 0.004 * cfg.centerStrength;
      }
      for (const l of shownLinks) {
        const dx = l.b.x - l.a.x, dy = l.b.y - l.a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = (dist - cfg.linkDistance / 3) / dist * 0.015 * cfg.linkStrength;
        const fx = dx * f, fy = dy * f;
        if (l.a !== dragged) { l.a.vx += fx; l.a.vy += fy; }
        if (l.b !== dragged) { l.b.vx -= fx; l.b.vy -= fy; }
      }
      for (const n of shown) {
        if (n === dragged) { n.vx = n.vy = 0; continue; }
        n.vx *= 0.82; n.vy *= 0.82;
        n.x += clamp(n.vx, -30, 30) * alpha;
        n.y += clamp(n.vy, -30, 30) * alpha;
      }
      alpha = Math.max(alpha * 0.985, 0.02);
    }

    function draw() {
      const rect = box.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.save();
      ctx.translate(rect.width / 2 + view.x, rect.height / 2 + view.y);
      ctx.scale(view.scale, view.scale);

      const lit = hover ? new Set([hover, ...near.get(hover)]) : null;
      ctx.lineWidth = Math.max(0.5, 0.8 * (cfg.lineSizeMultiplier || 1)) / view.scale;
      for (const l of shownLinks) {
        const on = lit && (lit.has(l.a) && lit.has(l.b));
        ctx.strokeStyle = on ? ACCENT : colors.line;
        ctx.globalAlpha = lit ? (on ? 0.9 : 0.08) : 0.32;
        ctx.beginPath();
        ctx.moveTo(l.a.x, l.a.y);
        ctx.lineTo(l.b.x, l.b.y);
        ctx.stroke();
        if (cfg.showArrow && on) arrow(l);
      }
      ctx.globalAlpha = 1;
      for (const n of shown) {
        const r = size(n);
        const dim = lit && !lit.has(n);
        ctx.globalAlpha = dim ? 0.25 : 1;
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = n === hover || n.path === focus ? ACCENT : n.color;
        ctx.fill();
        if (n.path === focus) {
          ctx.lineWidth = 2 / view.scale;
          ctx.strokeStyle = ACCENT;
          ctx.stroke();
        }
        if (marked.has(n.path)) {
          ctx.beginPath();
          ctx.arc(n.x, n.y, r + 3 / view.scale, 0, Math.PI * 2);
          ctx.lineWidth = 1.5 / view.scale;
          ctx.strokeStyle = colors.bright;
          ctx.stroke();
        }
      }
      // Jména jako v Obsidianu: při oddálení žádná (jen u najetého a otevřeného),
      // s přiblížením se plynule objeví, nejdřív u uzlů s nejvíc odkazy.
      const fade = 1 + (cfg.textFadeMultiplier || 0);
      const zoomPrah = clamp(0.9 - fade * 0.12, 0.25, 1.3);
      ctx.font = `${Math.max(9, 11 / view.scale * 0.9)}px system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      for (const n of shown) {
        const r = size(n);
        const vzdy = n === hover || n.path === focus;
        const duleziti = clamp(Math.sqrt(n.deg) / 4, 0, 0.6);          // hlavní uzly se ukážou dřív
        const vidit = clamp((view.scale - (zoomPrah - duleziti * zoomPrah)) / 0.3, 0, 1);
        if (!vzdy && vidit <= 0.02) continue;
        ctx.globalAlpha = (vzdy ? 1 : vidit * 0.85) * (lit && !lit.has(n) ? 0.3 : 1);
        ctx.fillStyle = n === hover ? colors.bright : colors.text;
        ctx.fillText(baseName(n.path), n.x, n.y + r + 2 / view.scale);
      }
      ctx.restore();
      ctx.globalAlpha = 1;
    }

    function arrow(l) {
      const dx = l.b.x - l.a.x, dy = l.b.y - l.a.y;
      const d = Math.hypot(dx, dy) || 1;
      const r = size(l.b) + 2;
      const tipX = l.b.x - dx / d * r, tipY = l.b.y - dy / d * r;
      const a = Math.atan2(dy, dx);
      const s = 6 / view.scale;
      ctx.beginPath();
      ctx.moveTo(tipX, tipY);
      ctx.lineTo(tipX - Math.cos(a - 0.4) * s, tipY - Math.sin(a - 0.4) * s);
      ctx.lineTo(tipX - Math.cos(a + 0.4) * s, tipY - Math.sin(a + 0.4) * s);
      ctx.closePath();
      ctx.fillStyle = ACCENT;
      ctx.fill();
    }

    let frame = 0;
    function loop() {
      if (stopped) return;
      // Motiv se dá přepnout za běhu — barvy se každou chvíli načtou znovu.
      if (frame++ % 30 === 0) {
        const fresh = readColors();
        if (fresh.node !== colors.node || fresh.text !== colors.text) {
          colors = fresh;
          paintNodes();
        }
      }
      tick();
      if (!ruka && alpha > 0.05) vejdi();
      draw();
      raf = requestAnimationFrame(loop);
    }
    const kick = () => { alpha = Math.max(alpha, 0.55); };

    /* ── myš a dotyk ───────────────────────────────────────────────────── */

    const at = (ev) => {
      const rect = box.getBoundingClientRect();
      return {x: (ev.clientX - rect.left - rect.width / 2 - view.x) / view.scale,
              y: (ev.clientY - rect.top - rect.height / 2 - view.y) / view.scale};
    };
    const nodeAt = (p) => {
      let best = null, bestD = Infinity;
      for (const n of shown) {
        const d = Math.hypot(n.x - p.x, n.y - p.y);
        const r = size(n) + 6 / view.scale;
        if (d < r && d < bestD) { best = n; bestD = d; }
      }
      return best;
    };
    let panFrom = null, moved = false, held = null, heldDone = false, downAt = null;
    canvas.addEventListener('pointerdown', (ev) => {
      ruka = true;
      canvas.setPointerCapture(ev.pointerId);
      moved = false;
      heldDone = false;
      downAt = {x: ev.clientX, y: ev.clientY};
      const p = at(ev);
      dragged = nodeAt(p);
      if (dragged) { dragged.vx = dragged.vy = 0; kick(); }
      else panFrom = {x: ev.clientX - view.x, y: ev.clientY - view.y};
      // Podržení prstu na puntíku = totéž co pravé tlačítko myši.
      clearTimeout(held);
      if (dragged && ev.pointerType !== 'mouse' && opts.onNodeMenu && !dragged.missing) {
        const n = dragged;
        held = setTimeout(() => {
          if (moved || dragged !== n) return;
          heldDone = !!opts.onNodeMenu(n.path);
        }, 550);
      }
    });
    canvas.addEventListener('contextmenu', (ev) => {
      if (!opts.onNodeMenu) return;
      const n = nodeAt(at(ev));
      if (n && !n.missing && opts.onNodeMenu(n.path)) ev.preventDefault();
    });
    canvas.addEventListener('pointermove', (ev) => {
      const p = at(ev);
      if (dragged) {
        // Prst se při podržení vždycky trochu pohne — to ještě není tažení.
        if (!moved && ev.pointerType !== 'mouse' && downAt &&
            Math.hypot(ev.clientX - downAt.x, ev.clientY - downAt.y) < 8) return;
        dragged.x = p.x; dragged.y = p.y; moved = true; kick(); return;
      }
      if (panFrom) { view.x = ev.clientX - panFrom.x; view.y = ev.clientY - panFrom.y; moved = true; return; }
      const found = nodeAt(p);
      if (found !== hover) {
        hover = found;
        canvas.style.cursor = found ? 'pointer' : 'grab';
        if (opts.onHover) opts.onHover(found ? found.path : '');
      }
    });
    const release = (ev) => {
      clearTimeout(held);
      if (ev && ev.button === 2) { dragged = null; panFrom = null; return; }
      if (dragged && !moved && !heldDone && opts.onOpenNote && !dragged.missing) opts.onOpenNote(dragged.path);
      dragged = null; panFrom = null;
      if (ev && canvas.hasPointerCapture && canvas.hasPointerCapture(ev.pointerId)) {
        canvas.releasePointerCapture(ev.pointerId);
      }
    };
    canvas.addEventListener('pointerup', release);
    canvas.addEventListener('pointercancel', release);
    canvas.addEventListener('pointerleave', () => { hover = null; });
    canvas.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      ruka = true;
      const rect = box.getBoundingClientRect();
      const mx = ev.clientX - rect.left - rect.width / 2, my = ev.clientY - rect.top - rect.height / 2;
      const before = view.scale;
      view.scale = clamp(view.scale * (ev.deltaY < 0 ? 1.12 : 0.89), 0.06, 6);
      view.x = mx - (mx - view.x) * (view.scale / before);
      view.y = my - (my - view.y) * (view.scale / before);
    }, {passive: false});

    applyFilter();
    loop();

    return {
      destroy() { stopped = true; cancelAnimationFrame(raf); box.remove(); },
      setFilter(text) { filter = text; applyFilter(); },
      setFocus(path) { focus = path || ''; applyFilter(); },
      setMarked(paths) { marked = new Set(paths || []); },
      setOption(key, value) { cfg[key] = value; applyFilter(); kick(); },
      options: () => Object.assign({}, cfg),
      counts: () => ({nodes: shown.length, links: shownLinks.length}),
      center() { ruka = false; kick(); vejdi(); },
    };
  }

  root.HubGraph = {render, DEFAULTS};
})(typeof window !== 'undefined' ? window : globalThis);
