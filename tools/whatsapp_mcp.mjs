#!/usr/bin/env node
// WhatsApp for Claude Code: Claude can message the user and ask a question,
// then wait for the reply. Two roles in one file:
//
//   node whatsapp_mcp.mjs mcp            stdio MCP server — one per Claude session
//   node whatsapp_mcp.mjs daemon [--pair] the single WhatsApp connection
//
// WhatsApp allows one connection per linked device, and the hub runs many
// Claude sessions at once (tabs, conversations). If every MCP process opened
// its own connection they would keep kicking each other off ("replaced"). So
// the connection lives in one daemon per user; MCP processes start it when it
// is not running and talk to it over 127.0.0.1 with a secret token. On the
// gateway all workspaces share one network namespace, so the token (in a 0600
// file in the user's own home, which other workspaces cannot see) is what
// keeps other users out.
//
// Library: Baileys (MIT), installed by the hub into the state directory, not
// shipped in the repo. State: ~/.claude/whatsapp/ (config.json, auth/,
// node_modules/, media/, inbox.json, daemon.json, daemon.log).
//
// Tool design borrowed from @mhrj/whatsapp-mcp (ISC): ask blocks until the
// reply, concurrent questions are tagged Q1, Q2…, only allowed numbers count.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import readline from 'node:readline';
import { spawn } from 'node:child_process';
import { pathToFileURL, fileURLToPath } from 'node:url';

const VERSION = '1.0.0';
const STATE = process.env.HUB_WHATSAPP_DIR
  || path.join(process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude'), 'whatsapp');
const SELF = fileURLToPath(import.meta.url);
const F = {
  config: path.join(STATE, 'config.json'),
  auth: path.join(STATE, 'auth'),
  media: path.join(STATE, 'media'),
  inbox: path.join(STATE, 'inbox.json'),
  daemon: path.join(STATE, 'daemon.json'),
  log: path.join(STATE, 'daemon.log'),
  lock: path.join(STATE, 'daemon.lock'),
};
const MAX_WAIT_MIN = 60;
const INBOX_KEEP = 200;

const readJson = (file, fallback) => {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
};
const writeJson = (file, data, mode = 0o600) => {
  const tmp = file + '.' + process.pid + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { mode });
  fs.renameSync(tmp, file);
};
const log = (...args) => {
  try {
    fs.appendFileSync(F.log, new Date().toISOString() + ' ' + args.join(' ') + '\n');
  } catch { /* the log is a convenience */ }
};
const alive = (pid) => {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
};
const digits = (s) => String(s || '').replace(/\D/g, '');
const jidOf = (number) => digits(number) + '@s.whatsapp.net';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── daemon ──────────────────────────────────────────────────────────────────
async function daemon(pair) {
  fs.mkdirSync(STATE, { recursive: true, mode: 0o700 });
  // One daemon per state dir. A live one that answers wins; a stale lock
  // (crashed daemon) is taken over.
  try {
    const fd = fs.openSync(F.lock, 'wx', 0o600);
    fs.writeSync(fd, String(process.pid));
    fs.closeSync(fd);
  } catch {
    const other = Number(fs.readFileSync(F.lock, 'utf8')) || 0;
    if (other && other !== process.pid && alive(other)) {
      log('daemon already running as', other);
      process.exit(0);
    }
    fs.writeFileSync(F.lock, String(process.pid), { mode: 0o600 });
  }
  const releaseLock = () => {
    try { if (Number(fs.readFileSync(F.lock, 'utf8')) === process.pid) fs.unlinkSync(F.lock); } catch { /* gone */ }
  };
  process.on('exit', releaseLock);
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(sig, () => process.exit(0));

  const lib = path.join(STATE, 'node_modules', 'baileys', 'lib', 'index.js');
  if (!fs.existsSync(lib)) {
    log('baileys is not installed in', STATE);
    process.exit(3);
  }
  const B = await import(pathToFileURL(lib).href);
  const makeWASocket = B.default || B.makeWASocket;
  const silent = {
    level: 'silent', child() { return silent; },
    trace() {}, debug() {}, info() {}, warn() {}, fatal() {},
    error(obj, msg) { log('baileys:', msg || '', obj && obj.err ? obj.err.message : ''); },
  };

  const st = {
    state: 'connecting',   // connecting | qr | open | logged_out | not_linked
    qr: '',
    me: '',
    detail: '',
    sock: null,
    ownerLid: '',
  };
  const inbox = readJson(F.inbox, []);
  let seq = inbox.reduce((m, x) => Math.max(m, x.seq || 0), 0);
  const pending = [];      // open questions: {ref, label, msgId, to, resolve, timer}
  let labelCounter = 0;
  const waiters = new Set();
  const config = () => readJson(F.config, {});

  if (!pair && !fs.existsSync(path.join(F.auth, 'creds.json'))) {
    st.state = 'not_linked';
    st.detail = 'WhatsApp is not linked yet — link it in Hub: Settings → Connections → WhatsApp.';
  }

  const allowed = () => {
    const c = config();
    return new Set([c.owner, ...(c.allow || [])].map(digits).filter(Boolean));
  };
  // Baileys 7: senders may come as hidden IDs (…@lid) instead of numbers;
  // the phone number is then in remoteJidAlt / participantAlt, or in the
  // LID mapping the library keeps.
  async function senderNumber(key) {
    for (const j of [key.remoteJidAlt, key.participantAlt, key.remoteJid, key.participant]) {
      if (j && /@s\.whatsapp\.net$/.test(j)) return digits(j.split('@')[0].split(':')[0]);
    }
    const lid = key.participant || key.remoteJid || '';
    if (/@lid$/.test(lid) && st.sock?.signalRepository?.lidMapping) {
      try {
        const pn = await st.sock.signalRepository.lidMapping.getPNForLID(lid);
        if (pn) return digits(pn.split('@')[0].split(':')[0]);
      } catch { /* unknown */ }
    }
    return '';
  }

  function textOf(m) {
    const msg = m.message || {};
    const inner = msg.ephemeralMessage?.message || msg.viewOnceMessage?.message || msg;
    return inner.conversation || inner.extendedTextMessage?.text
      || inner.imageMessage?.caption || inner.videoMessage?.caption
      || inner.documentMessage?.caption || inner.buttonsResponseMessage?.selectedDisplayText
      || inner.listResponseMessage?.title || inner.reactionMessage?.text || '';
  }
  function quotedId(m) {
    const msg = m.message || {};
    for (const v of Object.values(msg)) {
      const id = v && v.contextInfo && v.contextInfo.stanzaId;
      if (id) return id;
    }
    return '';
  }
  async function saveMedia(m) {
    const msg = m.message || {};
    const kind = ['imageMessage', 'documentMessage', 'audioMessage', 'videoMessage']
      .find((k) => msg[k]);
    if (!kind) return '';
    try {
      const buf = await B.downloadMediaMessage(m, 'buffer', {}, { logger: silent,
        reuploadRequest: st.sock.updateMediaMessage });
      const ext = msg[kind].fileName ? path.extname(msg[kind].fileName)
        : ({ imageMessage: '.jpg', audioMessage: '.ogg', videoMessage: '.mp4' }[kind] || '');
      fs.mkdirSync(F.media, { recursive: true, mode: 0o700 });
      const file = path.join(F.media, `${Date.now()}-${m.key.id}${ext}`);
      fs.writeFileSync(file, buf, { mode: 0o600 });
      return file;
    } catch (e) {
      log('media download failed:', e.message);
      return '';
    }
  }

  async function onMessage(m) {
    if (!m.message || m.key.fromMe) return;
    const from = await senderNumber(m.key);
    if (!from || !allowed().has(from)) return;    // only the user (and allowed numbers)
    if (/@g\.us$/.test(m.key.remoteJid || '')) return;
    const text = textOf(m).trim();
    const file = await saveMedia(m);
    const item = { seq: ++seq, at: new Date((Number(m.messageTimestamp) || Date.now() / 1000) * 1000).toISOString(),
      from: '+' + from, text, file: file || undefined, id: m.key.id };
    // Which open question does it answer? A quoted question, then a leading
    // "Q2", then the oldest open one to this person.
    const mine = pending.filter((p) => digits(p.to) === from);
    let q = mine.find((p) => p.msgId && p.msgId === quotedId(m));
    if (!q && mine.length > 1) {
      const tag = /^\s*\[?(Q\d+)\]?[\s:.,-]*/i.exec(text);
      if (tag) {
        q = mine.find((p) => p.label.toUpperCase() === tag[1].toUpperCase());
        if (q) item.text = text.slice(tag[0].length).trim();
      }
    }
    if (!q) q = mine[0];
    if (q) {
      pending.splice(pending.indexOf(q), 1);
      clearTimeout(q.timer);
      item.answers = q.label || 'question';
      q.resolve({ answered: true, answer: item.text, file: item.file, from: item.from, at: item.at });
      try { await st.sock.readMessages([m.key]); } catch { /* cosmetic */ }
      return;
    }
    inbox.push(item);
    while (inbox.length > INBOX_KEEP) inbox.shift();
    writeJson(F.inbox, inbox);
    for (const w of waiters) w();
  }

  let retry = 0;
  async function connect() {
    const { state: auth, saveCreds } = await B.useMultiFileAuthState(F.auth);
    let version;
    try { ({ version } = await B.fetchLatestBaileysVersion()); } catch { /* bundled default */ }
    const sock = makeWASocket({
      auth, logger: silent, version, markOnlineOnConnect: false,
      browser: B.Browsers ? B.Browsers.appropriate('Aladin') : undefined,
      syncFullHistory: false, shouldSyncHistoryMessage: () => false,
    });
    st.sock = sock;
    sock.ev.on('creds.update', saveCreds);
    sock.ev.on('connection.update', async (u) => {
      if (u.qr) {
        if (!pair) {
          // The saved login stopped working; only Hub may start a new pairing.
          st.state = 'logged_out';
          st.detail = 'The WhatsApp login expired — link it again in Hub.';
          try { sock.end(undefined); } catch { /* closing anyway */ }
          return;
        }
        st.qr = u.qr;
        st.state = 'qr';
      }
      if (u.connection === 'open') {
        retry = 0;
        st.state = 'open';
        st.qr = '';
        st.detail = '';
        st.me = digits((sock.user?.id || '').split(':')[0].split('@')[0]);
        log('connected as +' + st.me);
      }
      if (u.connection === 'close') {
        const code = u.lastDisconnect?.error?.output?.statusCode;
        log('connection closed', code || '', u.lastDisconnect?.error?.message || '');
        if (code === B.DisconnectReason.loggedOut) {
          st.state = 'logged_out';
          st.detail = 'WhatsApp unlinked this device — link it again in Hub.';
          fs.rmSync(F.auth, { recursive: true, force: true });
          return;
        }
        if (code === B.DisconnectReason.connectionReplaced) {
          st.state = 'logged_out';
          st.detail = 'Another connection took over this WhatsApp login.';
          return;
        }
        if (st.state === 'qr' && !pair) return;
        st.state = 'connecting';
        retry += 1;
        setTimeout(() => connect().catch((e) => log('reconnect failed:', e.message)),
          Math.min(60000, 1000 * 2 ** Math.min(retry, 6)));
      }
    });
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
      if (type !== 'notify' && type !== 'append') return;
      for (const m of messages) {
        try { await onMessage(m); } catch (e) { log('message handling failed:', e.message); }
      }
    });
  }
  if (st.state !== 'not_linked') await connect();

  // ── local API ──
  const token = crypto.randomBytes(24).toString('hex');
  const ready = async () => {
    for (let i = 0; i < 60 && st.state === 'connecting'; i++) await sleep(500);
    if (st.state !== 'open') {
      throw new Error({ qr: 'WhatsApp is waiting for the QR code scan in Hub (Settings → Connections → WhatsApp).',
        logged_out: st.detail, not_linked: st.detail }[st.state]
        || 'WhatsApp is not connected right now — try again in a minute.');
    }
  };
  const target = (to) => {
    const c = config();
    const number = digits(to || c.owner);
    if (!number) throw new Error('No recipient: the user\'s number is not set in Hub.');
    if (!allowed().has(number)) throw new Error(`+${number} is not an allowed recipient. Allowed: `
      + [...allowed()].map((n) => '+' + n).join(', '));
    return number;
  };
  async function send(number, text, file) {
    const jid = jidOf(number);
    let content;
    if (file) {
      const abs = path.resolve(file);
      if (!fs.existsSync(abs)) throw new Error('File not found: ' + abs);
      const ext = path.extname(abs).toLowerCase();
      const data = fs.readFileSync(abs);
      if (['.jpg', '.jpeg', '.png', '.webp', '.gif'].includes(ext)) {
        content = { image: data, caption: text || undefined };
      } else {
        content = { document: data, fileName: path.basename(abs), caption: text || undefined,
          mimetype: { '.pdf': 'application/pdf', '.zip': 'application/zip', '.txt': 'text/plain',
            '.csv': 'text/csv', '.json': 'application/json', '.html': 'text/html',
            '.mp4': 'video/mp4', '.mp3': 'audio/mpeg' }[ext] || 'application/octet-stream' };
      }
    } else {
      if (!text) throw new Error('Nothing to send.');
      content = { text };
    }
    const sent = await st.sock.sendMessage(jid, content);
    return sent?.key?.id || '';
  }

  const api = {
    async status() {
      const c = config();
      const readSeq = readJson(F.inbox + '.read', { seq: 0 }).seq || 0;
      return { ok: true, version: VERSION, state: st.state, qr: st.qr, me: st.me ? '+' + st.me : '',
        owner: c.owner ? '+' + digits(c.owner) : '', detail: st.detail, pending: pending.length,
        unread: inbox.filter((x) => x.seq > readSeq).length };
    },
    async send({ to, text, file }) {
      await ready();
      const number = target(to);
      const id = await send(number, text, file);
      return { ok: true, sent_to: '+' + number, id };
    },
    async ask({ to, question, options, wait_minutes }) {
      await ready();
      const number = target(to);
      if (!question) throw new Error('Missing question.');
      const open = pending.filter((p) => digits(p.to) === number);
      if (!open.length) labelCounter = 0;
      // The first question went out untagged; now that there are two it is Q1.
      if (open.length && !open[0].label) open[0].label = 'Q' + (++labelCounter);
      const label = open.length ? 'Q' + (++labelCounter) : '';
      let body = (label ? `[${label}] ` : '') + question;
      if (Array.isArray(options) && options.length) {
        body += '\n\n' + options.map((o, i) => `${i + 1}) ${o}`).join('\n');
      }
      if (label) {
        const others = open.map((p) => p.label).filter(Boolean).join(', ');
        body += `\n\n(Mám víc otázek naráz — na tuhle odpověz „${label} …“, na dřívější ${others} `
          + 'stejně, nebo přes Odpovědět na konkrétní zprávu.)';
      }
      const msgId = await send(number, body);
      const minutes = Math.max(0.1, Math.min(MAX_WAIT_MIN, Number(wait_minutes) || 10));
      return await new Promise((resolve) => {
        const q = { ref: crypto.randomBytes(4).toString('hex'), label, msgId, to: number, resolve };
        q.timer = setTimeout(() => {
          const i = pending.indexOf(q);
          if (i >= 0) pending.splice(i, 1);
          resolve({ answered: false, detail: `No reply within ${minutes} min. A later reply `
            + 'will show up in read_messages.' });
        }, minutes * 60000);
        pending.push(q);
      });
    },
    async read({ wait_minutes, all }) {
      const readMark = readJson(F.inbox + '.read', { seq: 0 });
      const fresh = () => inbox.filter((x) => all || x.seq > (readMark.seq || 0));
      let out = fresh();
      const minutes = Math.max(0, Math.min(MAX_WAIT_MIN, Number(wait_minutes) || 0));
      if (!out.length && minutes > 0) {
        await new Promise((resolve) => {
          const done = () => { waiters.delete(done); clearTimeout(t); resolve(); };
          const t = setTimeout(done, minutes * 60000);
          waiters.add(done);
        });
        out = fresh();
      }
      if (out.length) writeJson(F.inbox + '.read', { seq: Math.max(...out.map((x) => x.seq)) });
      return { ok: true, state: st.state, messages: out.slice(-50) };
    },
    async logout() {
      try { if (st.sock && st.state === 'open') await st.sock.logout(); } catch (e) { log('logout:', e.message); }
      fs.rmSync(F.auth, { recursive: true, force: true });
      setTimeout(() => process.exit(0), 200);
      return { ok: true };
    },
    async stop() {
      setTimeout(() => process.exit(0), 200);
      return { ok: true };
    },
  };

  const server = http.createServer((req, res) => {
    const reply = (code, data) => {
      res.writeHead(code, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(data));
    };
    const given = String(req.headers['x-token'] || '');
    if (given.length !== token.length || !crypto.timingSafeEqual(Buffer.from(given), Buffer.from(token))) {
      return reply(403, { ok: false, error: 'forbidden' });
    }
    const name = (req.url || '').replace(/^\/+/, '');
    if (req.method !== 'POST' || !Object.hasOwn(api, name)) return reply(404, { ok: false, error: 'unknown' });
    let raw = '';
    req.on('data', (c) => { raw += c; if (raw.length > 1e6) req.destroy(); });
    req.on('end', async () => {
      try {
        reply(200, await api[name](raw ? JSON.parse(raw) : {}));
      } catch (e) {
        reply(200, { ok: false, error: e.message });
      }
    });
  });
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  server.listen(0, '127.0.0.1', () => {
    writeJson(F.daemon, { pid: process.pid, port: server.address().port, token, version: VERSION });
    log(`daemon ${VERSION} listening (pair=${pair})`);
  });
  process.on('exit', () => {
    try { if (readJson(F.daemon, {}).pid === process.pid) fs.unlinkSync(F.daemon); } catch { /* gone */ }
  });
  // A daemon started just for pairing gives up when nobody scans.
  if (pair) {
    setTimeout(() => {
      if (st.state === 'qr' || st.state === 'connecting') { log('pairing timed out'); process.exit(0); }
    }, 10 * 60000);
  }
}

// ── client side (MCP process, hub) ──────────────────────────────────────────
function call(name, body = {}, timeoutMs = 30000) {
  const info = readJson(F.daemon, null);
  if (!info || !alive(info.pid)) return Promise.reject(new Error('daemon not running'));
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request({ host: '127.0.0.1', port: info.port, path: '/' + name, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data),
        'X-Token': info.token } }, (res) => {
      let raw = '';
      res.on('data', (c) => { raw += c; });
      res.on('end', () => { try { resolve(JSON.parse(raw)); } catch (e) { reject(e); } });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error('daemon timed out')));
    req.on('error', reject);
    req.end(data);
  });
}

async function ensureDaemon() {
  try { await call('status', {}, 5000); return; } catch { /* start it */ }
  if (!fs.existsSync(path.join(F.auth, 'creds.json'))) {
    throw new Error('WhatsApp is not linked yet. Ask the user to link it in Hub: '
      + 'Settings → Connections → WhatsApp.');
  }
  const child = spawn(process.execPath, [SELF, 'daemon'], { detached: true, stdio: 'ignore',
    windowsHide: true });
  child.unref();
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    try { await call('status', {}, 3000); return; } catch { /* not yet */ }
  }
  throw new Error('The WhatsApp connection did not start (see ' + F.log + ').');
}

const TOOLS = [
  {
    name: 'send_message',
    description: 'Send the user a WhatsApp message (a notification, a result, a link) or a file. '
      + 'Returns immediately. Write in the user\'s language, short and phone-friendly; WhatsApp '
      + 'formatting: *bold*, _italic_. Use `ask` instead when you need an answer.',
    inputSchema: { type: 'object', properties: {
      text: { type: 'string', description: 'Message text (caption when a file is attached).' },
      file: { type: 'string', description: 'Optional absolute path of a file to send (image or document).' },
      to: { type: 'string', description: 'Optional recipient number; defaults to the user. Only numbers allowed in Hub.' },
    } },
  },
  {
    name: 'ask',
    description: 'Ask the user a question on WhatsApp and WAIT for the reply (blocks up to wait_minutes, '
      + 'default 10, max 60). Use when you need a decision or information and the user may be away '
      + 'from the computer. Returns {answered, answer} — or answered=false after the timeout; a later '
      + 'reply then appears in read_messages. Several open questions are tagged Q1, Q2 automatically.',
    inputSchema: { type: 'object', required: ['question'], properties: {
      question: { type: 'string', description: 'The question, self-contained (the user reads it on a phone without context).' },
      options: { type: 'array', items: { type: 'string' }, description: 'Optional numbered choices; the user may answer with the number.' },
      wait_minutes: { type: 'number', description: 'How long to wait for the reply (default 10, max 60).' },
      to: { type: 'string', description: 'Optional recipient number; defaults to the user.' },
    } },
  },
  {
    name: 'read_messages',
    description: 'Messages the user sent on WhatsApp that did not answer an `ask` (new instructions, '
      + 'late replies, photos — files are saved locally and returned as paths). Marks them read. '
      + 'wait_minutes > 0 waits for a new message when there is none.',
    inputSchema: { type: 'object', properties: {
      wait_minutes: { type: 'number', description: 'Wait this long for a new message if there is none (default 0, max 60).' },
      all: { type: 'boolean', description: 'Also return messages already read (last 50).' },
    } },
  },
  {
    name: 'status',
    description: 'WhatsApp connection state, the linked number and who messages go to.',
    inputSchema: { type: 'object', properties: {} },
  },
];

async function runTool(name, args) {
  await ensureDaemon();
  const long = (Number(args.wait_minutes) || (name === 'ask' ? 10 : 0)) * 60000 + 60000;
  const map = { send_message: 'send', ask: 'ask', read_messages: 'read', status: 'status' };
  if (!map[name]) throw new Error('Unknown tool ' + name);
  const res = await call(map[name], args, Math.max(60000, long));
  if (res.ok === false) throw new Error(res.error || 'failed');
  if (name === 'status') delete res.qr;
  return res;
}

function mcp() {
  const out = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');
  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    const { id, method, params } = msg;
    if (id === undefined) return;               // notifications
    try {
      if (method === 'initialize') {
        // Starting the connection early means replies sent before the first
        // tool call are not missed.
        ensureDaemon().catch(() => {});
        return out({ jsonrpc: '2.0', id, result: {
          protocolVersion: params?.protocolVersion || '2025-06-18',
          capabilities: { tools: {} },
          serverInfo: { name: 'whatsapp', version: VERSION },
          instructions: 'WhatsApp channel to the user (via Aladin). Use `ask` when you need '
            + 'the user\'s decision and they might be away from the computer, `send_message` for '
            + 'results and notifications. Do not spam: one message per meaningful event.',
        } });
      }
      if (method === 'ping') return out({ jsonrpc: '2.0', id, result: {} });
      if (method === 'tools/list') return out({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
      if (method === 'tools/call') {
        try {
          const res = await runTool(params.name, params.arguments || {});
          return out({ jsonrpc: '2.0', id, result: {
            content: [{ type: 'text', text: JSON.stringify(res, null, 2) }] } });
        } catch (e) {
          return out({ jsonrpc: '2.0', id, result: { isError: true,
            content: [{ type: 'text', text: e.message }] } });
        }
      }
      return out({ jsonrpc: '2.0', id, error: { code: -32601, message: 'Method not found' } });
    } catch (e) {
      return out({ jsonrpc: '2.0', id, error: { code: -32603, message: e.message } });
    }
  });
}

const mode = process.argv[2];
if (mode === 'daemon') {
  daemon(process.argv.includes('--pair')).catch((e) => { log('daemon crashed:', e.stack || e.message); process.exit(1); });
} else if (mode === 'mcp') {
  mcp();
} else {
  process.stderr.write('usage: whatsapp_mcp.mjs mcp | daemon [--pair]\n');
  process.exit(2);
}
