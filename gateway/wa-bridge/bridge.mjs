#!/usr/bin/env node
// WhatsApp bridge for Claude Code Hub.
//
// One Baileys session per company that has a bot number registered in the
// gateway. The bridge decides NOTHING about permissions: it forwards each event
// to the gateway (loopback, shared secret) and sends back whatever the gateway
// says. Texts are never written to disk here either.
//
// Gateway API (POST /gw/whatsapp/bridge, header X-WA-Token):
//   {op:"seznam"}                          -> {firmy:[{firma, telefon}]}
//   {op:"stav", firma, pripojeno, qr, jid} -> {ok}
//   {op:"udalost", firma, udalost}         -> {odpovedi:[{komu,text,citovat}], opustit}

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import pino from 'pino'
import QRCode from 'qrcode'
import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  useMultiFileAuthState,
} from 'baileys'

import { buildEvent, chunk, digitsOf, senderPhone } from './lib.mjs'

const ROOT = path.dirname(fileURLToPath(import.meta.url))
const GATEWAY = process.env.WA_GATEWAY_URL || 'http://127.0.0.1:8800'
const TOKEN_FILE = process.env.WA_TOKEN_FILE || ''
const AUTH_ROOT = process.env.WA_AUTH_DIR || path.join(ROOT, 'auth')
const POLL_MS = 5000
const HEARTBEAT_MS = 30000
const BOOTED_AT = Math.floor(Date.now() / 1000)

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a)
const logger = pino({ level: 'silent' })

function token() {
  if (process.env.WA_TOKEN) return process.env.WA_TOKEN.trim()
  return fs.readFileSync(TOKEN_FILE, 'utf8').trim()
}

async function gateway(body) {
  const res = await fetch(`${GATEWAY}/gw/whatsapp/bridge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-WA-Token': token() },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(180000),
  })
  if (!res.ok) throw new Error(`gateway ${res.status}`)
  return res.json()
}

// slug -> { sock, phone, stop, state }
const sessions = new Map()

const authDir = (slug) => path.join(AUTH_ROOT, slug.replace(/[^a-z0-9-]/gi, '_'))

async function pushState(slug, s) {
  try {
    await gateway({ op: 'stav', firma: slug, pripojeno: s.connected, qr: s.qr || '', jid: s.jid || '', chyba: s.error || '' })
  } catch (e) {
    log(`[${slug}] stav se nepodařilo poslat: ${e.message}`)
  }
}

async function deliver(slug, sock, replies, incoming) {
  for (const r of replies) {
    const parts = chunk(r.text)
    for (let i = 0; i < parts.length; i++) {
      const opts = i === 0 && r.citovat && incoming ? { quoted: incoming } : {}
      await sock.sendMessage(r.komu, { text: parts[i] }, opts)
    }
  }
}

async function onMessage(slug, s, m) {
  if (Number(m.messageTimestamp) < BOOTED_AT - 60) return // backlog replayed on connect
  const ev = buildEvent(m, s.botIds)
  if (!ev) return
  // Typing indicator only when we may answer; the gateway decides, so show it briefly.
  const typing = setInterval(() => s.sock.sendPresenceUpdate('composing', ev.chat).catch(() => {}), 8000)
  try {
    const out = await gateway({ op: 'udalost', firma: slug, udalost: ev })
    if (out.odpovedi?.length) {
      await s.sock.readMessages([m.key]).catch(() => {})
      await deliver(slug, s.sock, out.odpovedi, m)
    }
    if (out.opustit) {
      log(`[${slug}] opouštím skupinu ${out.opustit}`)
      await s.sock.groupLeave(out.opustit).catch((e) => log('groupLeave', e.message))
    }
  } catch (e) {
    log(`[${slug}] událost selhala: ${e.message}`)
  } finally {
    clearInterval(typing)
    s.sock.sendPresenceUpdate('paused', ev.chat).catch(() => {})
  }
}

async function onGroupUpdate(slug, s, u) {
  if (u.action !== 'add') return
  const mine = new Set(s.botIds.map(digitsOf))
  if (!u.participants.some((p) => mine.has(digitsOf(p.id || p)))) return
  // Who added the bot? WhatsApp does not always say; then the gateway keeps the group ignored.
  const pridavatel = senderPhone({ participantPn: u.authorPn, participant: u.author })
  try {
    const out = await gateway({ op: 'udalost', firma: slug,
      udalost: { druh: 'pridan', skupina: u.id, pridavatel } })
    if (out.opustit) {
      log(`[${slug}] přidal cizí, odcházím ze skupiny ${u.id}`)
      await s.sock.groupLeave(out.opustit).catch((e) => log('groupLeave', e.message))
    }
  } catch (e) {
    log(`[${slug}] přidání do skupiny: ${e.message}`)
  }
}

async function connect(slug, expectedPhone) {
  const s = sessions.get(slug)
  if (!s || s.stop) return
  const { state, saveCreds } = await useMultiFileAuthState(authDir(slug))
  const { version } = await fetchLatestBaileysVersion()
  const sock = makeWASocket({
    version, auth: state, logger,
    browser: ['Claude Code Hub', 'Chrome', '1.0.0'],
    markOnlineOnConnect: false, syncFullHistory: false,
  })
  s.sock = sock
  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', async (u) => {
    if (s.stop) return
    const { connection, lastDisconnect, qr } = u
    if (qr) {
      s.qr = await QRCode.toDataURL(qr, { margin: 1, width: 280 }).catch(() => '')
      s.connected = false
      await pushState(slug, s)
    }
    if (connection === 'open') {
      s.connected = true
      s.qr = ''
      s.error = ''
      s.jid = jidNormalizedUser(sock.user.id)
      s.botIds = [s.jid, sock.user.lid ? jidNormalizedUser(sock.user.lid) : null].filter(Boolean)
      const paired = digitsOf(s.jid)
      if (expectedPhone && paired && paired !== expectedPhone) {
        // Paired with a different number than the one registered for the company.
        s.error = `Spárováno jiné číslo (+${paired}) než zapsané (+${expectedPhone}).`
        log(`[${slug}] ${s.error}`)
        s.connected = false
        await pushState(slug, s)
        await sock.logout().catch(() => {})
        return
      }
      log(`[${slug}] připojeno jako ${s.jid}`)
      await pushState(slug, s)
    }
    if (connection === 'close') {
      s.connected = false
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        log(`[${slug}] odhlášeno z telefonu, mažu přihlášení`)
        fs.rmSync(authDir(slug), { recursive: true, force: true })
        s.error = s.error || 'Spojení bylo odpojeno v telefonu. Spáruj číslo znovu.'
      }
      await pushState(slug, s)
      if (!s.stop) setTimeout(() => connect(slug, expectedPhone).catch((e) => log(e)), code === DisconnectReason.loggedOut ? 1000 : 3000)
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify' || s.stop) return
    for (const m of messages) await onMessage(slug, s, m).catch((e) => log(`[${slug}]`, e.message))
  })
  sock.ev.on('group-participants.update', (u) => onGroupUpdate(slug, s, u).catch((e) => log(e.message)))
}

async function startSession(slug, phone) {
  fs.mkdirSync(authDir(slug), { recursive: true, mode: 0o700 })
  sessions.set(slug, { sock: null, phone, stop: false, connected: false, qr: '', jid: '', error: '', botIds: [] })
  log(`[${slug}] startuji spojení (+${phone})`)
  await connect(slug, phone)
}

async function stopSession(slug, wipe) {
  const s = sessions.get(slug)
  if (!s) return
  s.stop = true
  sessions.delete(slug)
  try { if (wipe) await s.sock?.logout(); else s.sock?.end(undefined) } catch { /* already closed */ }
  if (wipe) fs.rmSync(authDir(slug), { recursive: true, force: true })
  log(`[${slug}] spojení ukončeno${wipe ? ' a přihlášení smazáno' : ''}`)
}

async function reconcile() {
  let firmy
  try {
    firmy = (await gateway({ op: 'seznam' })).firmy || []
  } catch (e) {
    log(`gateway nedostupná: ${e.message}`)
    return
  }
  const wanted = new Map(firmy.map((f) => [f.firma, String(f.telefon || '')]))
  for (const [slug, s] of [...sessions]) {
    // Number removed or changed in the hub: drop the old pairing.
    if (!wanted.has(slug) || wanted.get(slug) !== s.phone) await stopSession(slug, true)
  }
  for (const [slug, phone] of wanted) {
    if (!sessions.has(slug)) await startSession(slug, phone).catch((e) => log(`[${slug}] start selhal: ${e.message}`))
  }
}

setInterval(() => reconcile().catch((e) => log(e.message)), POLL_MS)
setInterval(() => { for (const [slug, s] of sessions) pushState(slug, s) }, HEARTBEAT_MS)
process.on('unhandledRejection', (e) => log('unhandledRejection', e?.message || e))
process.on('SIGTERM', async () => { for (const slug of [...sessions.keys()]) await stopSession(slug, false); process.exit(0) })
log(`wa-bridge běží, brána ${GATEWAY}`)
reconcile().catch((e) => log(e.message))
