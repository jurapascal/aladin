// Pure helpers of the bridge (no sockets), kept apart so they can be tested.

// Digits of a JID user part: "420777123456:12@s.whatsapp.net" -> "420777123456".
export const digitsOf = (jid) => String(jid || '').split('@')[0].split(':')[0].replace(/\D/g, '')

// Only a real phone JID carries a phone number. A @lid identity is an opaque id
// whose digits must never be mistaken for a number.
export const phoneOf = (jid) => (String(jid || '').endsWith('@s.whatsapp.net') ? digitsOf(jid) : '')

// First phone number found among the identifiers a message carries for its sender.
export function senderPhone(key, extra = []) {
  const candidates = [
    key.participantPn, key.senderPn, key.participantAlt, key.remoteJidAlt,
    key.participant, key.remoteJid, ...extra,
  ]
  for (const c of candidates) {
    const p = phoneOf(c)
    if (p) return p
  }
  return ''
}

export function textOf(m) {
  const msg = m.message || {}
  return (
    msg.conversation ||
    msg.extendedTextMessage?.text ||
    msg.imageMessage?.caption ||
    msg.videoMessage?.caption ||
    msg.documentMessage?.caption ||
    msg.documentWithCaptionMessage?.message?.documentMessage?.caption ||
    ''
  ).trim()
}

function contextOf(m) {
  const msg = m.message || {}
  return (
    msg.extendedTextMessage?.contextInfo ||
    msg.imageMessage?.contextInfo ||
    msg.videoMessage?.contextInfo ||
    msg.documentMessage?.contextInfo ||
    {}
  )
}

// Is the bot mentioned / replied to? `botIds` = every JID the bot answers to.
export function addressedToBot(m, botIds) {
  const ctx = contextOf(m)
  const mine = new Set(botIds.map(digitsOf).filter(Boolean))
  const oznacen = (ctx.mentionedJid || []).some((j) => mine.has(digitsOf(j)))
  const odpoved = !!ctx.participant && mine.has(digitsOf(ctx.participant))
  return { oznacen, odpoved_botovi: odpoved }
}

// Removes the leading @mention token of the bot ("@420777123456 ahoj" -> "ahoj").
export const stripMention = (text) => text.replace(/^(@\d{6,}\s*)+/, '').trim()

// WhatsApp caps a message near 4096 chars; split on paragraph, then line, then word.
export function chunk(text, size = 3500) {
  const parts = []
  let rest = String(text || '')
  while (rest.length > size) {
    let cut = rest.lastIndexOf('\n\n', size)
    if (cut < size * 0.4) cut = rest.lastIndexOf('\n', size)
    if (cut < size * 0.4) cut = rest.lastIndexOf(' ', size)
    if (cut < size * 0.4) cut = size
    parts.push(rest.slice(0, cut))
    rest = rest.slice(cut).replace(/^\n+/, '')
  }
  if (rest.trim()) parts.push(rest)
  return parts
}

// Event for the gateway from one incoming message. Returns null for what is never relayed.
export function buildEvent(m, botIds) {
  if (!m.message || m.key.fromMe) return null
  const chat = m.key.remoteJid
  if (!chat || chat === 'status@broadcast' || chat.endsWith('@newsletter') || chat.endsWith('@broadcast')) return null
  const isGroup = chat.endsWith('@g.us')
  let text = textOf(m)
  if (!text) return null
  const od = senderPhone(isGroup ? { ...m.key, remoteJid: '' } : m.key)
  const ev = { typ: isGroup ? 'skupina' : 'dm', od, chat, text, jmeno: m.pushName || '' }
  if (isGroup) {
    ev.skupina = chat
    Object.assign(ev, addressedToBot(m, botIds))
    ev.text = stripMention(text)
  }
  return ev
}
