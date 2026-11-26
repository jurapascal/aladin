/* Editor poznámek se živým náhledem — jako v Obsidianu.
 *
 * Píše se Markdown, ale formátování je vidět rovnou: nadpisy jsou velké, tučné
 * je tučné, [[odkazy]] jsou odkazy. Značky (`**`, `#`, `[[`) se odhalí jen na
 * řádku, kde je kurzor — tam se text upravuje, jinde se čte.
 *
 * Stojí na CodeMirroru 6 (hub/static/vendor/codemirror.js, sestavuje
 * tools/build-editor.sh) — na stejném jádru jako Obsidian. Obsidianovská
 * syntaxe navíc ([[odkaz]], ==zvýraznění==, #štítek) je tady, ne v knihovně.
 */
(function (root) {
  'use strict';

  const CM = root.CM;
  if (!CM) return;
  const {EditorState, EditorSelection, EditorView, Decoration, ViewPlugin, WidgetType,
         keymap, drawSelection, rectangularSelection, highlightActiveLine, placeholder,
         defaultKeymap, history, historyKeymap, indentWithTab, undo, redo,
         syntaxTree, HighlightStyle, syntaxHighlighting, indentOnInput, indentUnit,
         markdown, markdownLanguage, markdownKeymap, insertNewlineContinueMarkup,
         autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
         search, searchKeymap, highlightSelectionMatches, tags, styleTags,
         GFM, Prec} = CM;

  /* ── obsidianovská syntaxe pro parser ─────────────────────────────────── */

  // [[poznámka|popisek]] a vložení ![[obrázek.png]]
  const WikiLink = {
    defineNodes: [{name: 'WikiLink', style: tags.link}, {name: 'WikiLinkMark', style: tags.processingInstruction}],
    parseInline: [{
      name: 'WikiLink',
      before: 'Link',
      parse(cx, next, pos) {
        let at = pos;
        if (next === 33 /* ! */) at++;                       // ![[…]]
        if (cx.char(at) !== 91 || cx.char(at + 1) !== 91) return -1;
        const end = cx.text.indexOf(']]', at + 2 - cx.offset);
        if (end < 0) return -1;
        const to = cx.offset + end + 2;
        if (cx.text.slice(at + 2 - cx.offset, end).includes('[')) return -1;
        return cx.addElement(cx.elt('WikiLink', pos, to, [
          cx.elt('WikiLinkMark', pos, at + 2),
          cx.elt('WikiLinkMark', to - 2, to),
        ]));
      },
    }],
  };

  // ==zvýrazněné==
  const HighlightDelim = {resolve: 'Highlight', mark: 'HighlightMark'};
  const Highlight = {
    defineNodes: [{name: 'Highlight', style: tags.special(tags.string)},
                  {name: 'HighlightMark', style: tags.processingInstruction}],
    parseInline: [{
      name: 'Highlight',
      parse(cx, next, pos) {
        if (next !== 61 /* = */ || cx.char(pos + 1) !== 61) return -1;
        return cx.addDelimiter(HighlightDelim, pos, pos + 2, true, true);
      },
      after: 'Emphasis',
    }],
  };

  // #štítek (ne nadpis „# Nadpis“ — za mřížkou musí být rovnou znak)
  const HashTag = {
    defineNodes: [{name: 'HashTag', style: tags.meta}],
    parseInline: [{
      name: 'HashTag',
      parse(cx, next, pos) {
        if (next !== 35 /* # */) return -1;
        const before = pos > cx.offset ? cx.text[pos - cx.offset - 1] : ' ';
        if (!/[\s([]/.test(before)) return -1;
        let end = pos + 1;
        while (end < cx.end && /[\p{L}\p{N}_/-]/u.test(cx.text[end - cx.offset] || '')) end++;
        const name = cx.text.slice(pos + 1 - cx.offset, end - cx.offset);
        if (!/\p{L}/u.test(name)) return -1;
        return cx.addElement(cx.elt('HashTag', pos, end));
      },
    }],
  };

  /* ── živý náhled ───────────────────────────────────────────────────────── */

  // Značky, které se schovají, když kurzor není na jejich řádku.
  const MARKS = new Set(['HeaderMark', 'EmphasisMark', 'StrikethroughMark', 'CodeMark',
                         'QuoteMark', 'LinkMark', 'HighlightMark', 'WikiLinkMark',
                         'CodeInfo', 'URL']);
  const HEADINGS = {ATXHeading1: 1, ATXHeading2: 2, ATXHeading3: 3,
                    ATXHeading4: 4, ATXHeading5: 5, ATXHeading6: 6};

  class CheckWidget extends WidgetType {
    constructor(done, pos) { super(); this.done = done; this.pos = pos; }
    eq(other) { return other.done === this.done && other.pos === this.pos; }
    toDOM(view) {
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.className = 'cm-task';
      box.checked = this.done;
      box.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        view.dispatch({changes: {from: this.pos + 1, to: this.pos + 2, insert: this.done ? ' ' : 'x'}});
      });
      return box;
    }
    ignoreEvent() { return false; }
  }

  const style = HighlightStyle.define([
    {tag: tags.heading1, class: 'cm-h1'}, {tag: tags.heading2, class: 'cm-h2'},
    {tag: tags.heading3, class: 'cm-h3'}, {tag: tags.heading4, class: 'cm-h4'},
    {tag: tags.heading5, class: 'cm-h5'}, {tag: tags.heading6, class: 'cm-h6'},
    {tag: tags.strong, class: 'cm-b'}, {tag: tags.emphasis, class: 'cm-i'},
    {tag: tags.strikethrough, class: 'cm-strike'},
    {tag: tags.link, class: 'cm-wiki'}, {tag: tags.url, class: 'cm-url'},
    {tag: tags.monospace, class: 'cm-code'}, {tag: tags.meta, class: 'cm-tag'},
    {tag: tags.special(tags.string), class: 'cm-mark'},
    {tag: tags.quote, class: 'cm-quote'}, {tag: tags.processingInstruction, class: 'cm-syntax'},
  ]);

  const HIDE = Decoration.replace({});

  function livePreview(view, image) {
    const marks = [];
    const sel = view.state.selection;
    // Řádek s kurzorem (nebo výběrem) ukazuje značky tak, jak jsou v souboru.
    // Dokud se v editoru nepíše, je vidět jen výsledek — jako v Obsidianu.
    const open = new Set();
    for (const r of (view.hasFocus ? sel.ranges : [])) {
      const a = view.state.doc.lineAt(r.from).number;
      const b = view.state.doc.lineAt(r.to).number;
      for (let n = a; n <= b; n++) open.add(n);
    }
    const shown = (from, to) => {
      const a = view.state.doc.lineAt(from).number;
      const b = view.state.doc.lineAt(to).number;
      for (let n = a; n <= b; n++) if (open.has(n)) return true;
      return false;
    };
    for (const {from, to} of view.visibleRanges) {
      syntaxTree(view.state).iterate({
        from, to,
        enter(node) {
          const name = node.name;
          if (HEADINGS[name]) {
            marks.push({from: node.from, to: node.to,
                        deco: Decoration.line({class: 'cm-line-h' + HEADINGS[name]}), line: true});
            return;
          }
          if (name === 'TaskMarker') {
            if (shown(node.from, node.to)) return;
            const text = view.state.doc.sliceString(node.from, node.to);
            marks.push({from: node.from, to: node.to,
                        deco: Decoration.replace({widget: new CheckWidget(/[xX]/.test(text), node.from)})});
            return;
          }
          if (name === 'Image' || name === 'WikiLink') {
            // Vložený obrázek se ukáže; na řádku s kurzorem zůstane zápis.
            const text = view.state.doc.sliceString(node.from, node.to);
            const wiki = /^!\[\[([^\]|#\n]+)/.exec(text);
            const md = /^!\[([^\]]*)\]\(<?([^)\s>]+?)>?\)$/.exec(text);
            const target = wiki ? wiki[1].trim() : md ? safeDecode(md[2]) : '';
            if (!target || !IMAGE_EXT.test(target)) return;
            if (shown(node.from, node.to)) return;
            const url = image(target);
            if (!url) return;
            marks.push({from: node.from, to: node.to,
                        deco: Decoration.replace({widget: new ImageWidget(url, (md && md[1]) || target)})});
            return false;                       // dovnitř už se nechodí
          }
          if (name === 'ListMark') {
            // Odrážka se ukáže jako puntík; číslovaný seznam si číslo nechá.
            if (shown(node.from, node.to)) return;
            if (/^[-*+]$/.test(view.state.doc.sliceString(node.from, node.to))) {
              marks.push({from: node.from, to: node.to,
                          deco: Decoration.replace({widget: new BulletWidget()})});
            }
            return;
          }
          if (name === 'HorizontalRule') {
            if (shown(node.from, node.to)) return;
            marks.push({from: node.from, to: node.to, deco: Decoration.replace({widget: new RuleWidget()})});
            return;
          }
          if (!MARKS.has(name)) return;
          if (shown(node.from, node.to)) return;
          // U odkazu [popisek](adresa) se schová i adresa, ať zbyde popisek.
          let {from: a, to: b} = node;
          if (name === 'HeaderMark') {
            const after = view.state.doc.sliceString(b, b + 1);
            if (after === ' ') b += 1;
          }
          if (a < b) marks.push({from: a, to: b, deco: HIDE});
        },
      });
    }
    marks.sort((x, y) => x.from - y.from || (y.line ? 1 : 0) - (x.line ? 1 : 0));
    return Decoration.set(marks.map((m) => m.line ? m.deco.range(m.from) : m.deco.range(m.from, m.to)), true);
  }

  class BulletWidget extends WidgetType {
    toDOM() {
      const el = document.createElement('span');
      el.className = 'cm-bullet';
      el.textContent = '•';
      return el;
    }
  }

  // Vložený obrázek: ![[obrazek.png]] i ![popisek](obrazek.png).
  const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp|avif)$/i;

  class ImageWidget extends WidgetType {
    constructor(url, alt) { super(); this.url = url; this.alt = alt; }
    eq(other) { return other.url === this.url && other.alt === this.alt; }
    toDOM() {
      const img = document.createElement('img');
      img.className = 'cm-embed';
      img.src = this.url;
      img.alt = this.alt || '';
      return img;
    }
  }

  class RuleWidget extends WidgetType {
    toDOM() {
      const el = document.createElement('span');
      el.className = 'cm-rule';
      return el;
    }
  }

  function previewPlugin(image) {
    return ViewPlugin.fromClass(class {
      constructor(view) { this.decorations = livePreview(view, image); }
      update(u) {
        // geometryChanged: editor se zakládá ještě schovaný (přepnutí z Čtení),
        // takže při prvním výpočtu nemá co kreslit — po změření se to dopočítá.
        if (u.docChanged || u.selectionSet || u.viewportChanged || u.focusChanged ||
            u.geometryChanged) {
          this.decorations = livePreview(u.view, image);
        }
      }
    }, {decorations: (v) => v.decorations});
  }

  function safeDecode(text) {
    try { return decodeURIComponent(text); } catch (err) { return text; }
  }

  /* ── našeptávání [[odkazů]] ────────────────────────────────────────────── */

  function wikiComplete(getNotes) {
    return (ctx) => {
      const before = ctx.matchBefore(/\[\[[^\]\n]*/);
      if (!before) return null;
      const typed = before.text.slice(2).toLowerCase();
      const options = [];
      for (const path of getNotes()) {
        const name = path.replace(/\.md$/i, '');
        if (typed && !name.toLowerCase().includes(typed)) continue;
        options.push({label: name.split('/').pop(), detail: name.includes('/') ? name : '',
                      apply: name.split('/').pop()});
        if (options.length >= 60) break;
      }
      return options.length ? {from: before.from + 2, options, validFor: /^[^\]\n]*$/} : null;
    };
  }

  /* ── okno editoru ──────────────────────────────────────────────────────── */

  const theme = EditorView.theme({
    '&': {height: '100%', fontSize: '14px', color: 'var(--fg)', backgroundColor: 'transparent'},
    '.cm-content': {padding: '4px 0 40vh', fontFamily: 'inherit', lineHeight: '1.7',
                    caretColor: 'var(--accent)', maxWidth: '820px'},
    '.cm-scroller': {fontFamily: 'inherit', overflow: 'auto'},
    '&.cm-focused': {outline: 'none'},
    '.cm-line': {padding: '0 2px'},
    '.cm-activeLine': {backgroundColor: 'transparent'},
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection': {
      backgroundColor: 'color-mix(in srgb, var(--accent) 28%, transparent)'},
    '.cm-cursor, .cm-dropCursor': {borderLeftColor: 'var(--accent)', borderLeftWidth: '2px'},
    '.cm-tooltip': {background: 'var(--bg-card)', border: '1px solid var(--border)',
                    borderRadius: '8px', color: 'var(--fg)'},
    '.cm-tooltip-autocomplete ul li[aria-selected]': {
      background: 'color-mix(in srgb, var(--accent) 25%, transparent)', color: 'var(--fg-bright)'},
    '.cm-panels': {background: 'var(--bg-card)', color: 'var(--fg)', border: '1px solid var(--border)'},
    '.cm-panels input, .cm-panels button': {font: 'inherit', background: 'var(--bg)',
                                            color: 'var(--fg)', border: '1px solid var(--border)',
                                            borderRadius: '6px', padding: '2px 6px'},
  }, {dark: true});

  function create(opts) {
    const getNotes = opts.notes || (() => []);
    const save = opts.onSave || (() => {});
    const language = markdown({
      base: markdownLanguage,
      extensions: [GFM, WikiLink, Highlight, HashTag, {
        props: [styleTags({'WikiLink/...': tags.link, HashTag: tags.meta,
                           'Highlight/...': tags.special(tags.string)})],
      }],
    });
    const state = EditorState.create({
      doc: opts.doc || '',
      extensions: [
        history(), drawSelection(), rectangularSelection(), highlightActiveLine(),
        indentOnInput(), indentUnit.of('    '), closeBrackets(), highlightSelectionMatches(),
        search({top: true}),
        autocompletion({override: [wikiComplete(getNotes)], icons: false}),
        language, syntaxHighlighting(style), previewPlugin(opts.image || (() => '')), theme,
        EditorView.lineWrapping,
        placeholder(opts.placeholder || 'Piš poznámku…'),
        Prec.high(keymap.of([
          {key: 'Mod-s', run: () => { save(); return true; }, preventDefault: true},
          {key: 'Enter', run: insertNewlineContinueMarkup},
          {key: 'Mod-b', run: (v) => wrap(v, '**'), preventDefault: true},
          {key: 'Mod-i', run: (v) => wrap(v, '*'), preventDefault: true},
        ])),
        keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap,
                   ...markdownKeymap, ...completionKeymap, ...searchKeymap, indentWithTab]),
        EditorView.updateListener.of((u) => { if (u.docChanged && opts.onChange) opts.onChange(); }),
        EditorState.readOnly.of(!!opts.readOnly),
      ],
    });
    const view = new EditorView({state, parent: opts.parent});
    // Klik na [[odkaz]] otevře poznámku, jako v náhledu.
    view.dom.addEventListener('click', (ev) => {
      const el = ev.target.closest('.cm-wiki');
      if (!el || !opts.onOpenNote) return;
      const pos = view.posAtDOM(el);
      const line = view.state.doc.lineAt(pos).text;
      const m = /\[\[([^\]|#\n]+)/.exec(line.slice(Math.max(0, view.state.doc.lineAt(pos).from - pos)));
      const target = m ? m[1] : (el.textContent || '').replace(/^\[+|\]+$/g, '');
      if (target.trim()) opts.onOpenNote(target.trim());
    });
    return {
      view,
      focus: () => view.focus(),
      getDoc: () => view.state.doc.toString(),
      setDoc: (text) => view.dispatch({
        changes: {from: 0, to: view.state.doc.length, insert: text || ''},
        selection: EditorSelection.cursor(0), scrollIntoView: true,
      }),
      destroy: () => view.destroy(),
      undo: () => undo(view), redo: () => redo(view),
    };
  }

  function wrap(view, mark) {
    const changes = view.state.changeByRange((range) => {
      const text = view.state.sliceDoc(range.from, range.to);
      return {
        changes: {from: range.from, to: range.to, insert: mark + text + mark},
        range: EditorSelection.range(range.from + mark.length, range.to + mark.length),
      };
    });
    view.dispatch(changes);
    return true;
  }

  root.HubEditor = {create, available: true};
})(typeof window !== 'undefined' ? window : globalThis);
