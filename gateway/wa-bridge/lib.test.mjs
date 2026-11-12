import test from 'node:test'
import assert from 'node:assert/strict'
import { buildEvent, chunk, phoneOf, senderPhone, stripMention } from './lib.mjs'

const BOT = ['420111222333@s.whatsapp.net', '99887766@lid']

test('phoneOf ignores LID identities', () => {
  assert.equal(phoneOf('420777123456:12@s.whatsapp.net'), '420777123456')
  assert.equal(phoneOf('99887766@lid'), '')
})

test('senderPhone prefers the real phone JID over a LID', () => {
  const key = { participant: '5551@lid', participantPn: '420777123456@s.whatsapp.net' }
  assert.equal(senderPhone(key), '420777123456')
  assert.equal(senderPhone({ participant: '5551@lid' }), '')
})

test('direct message becomes a dm event', () => {
  const m = { key: { remoteJid: '420777123456@s.whatsapp.net' }, message: { conversation: ' ahoj ' }, pushName: 'Eva' }
  assert.deepEqual(buildEvent(m, BOT), { typ: 'dm', od: '420777123456', chat: m.key.remoteJid, text: 'ahoj', jmeno: 'Eva' })
})

test('own messages, status and empty text are dropped', () => {
  assert.equal(buildEvent({ key: { remoteJid: 'a@s.whatsapp.net', fromMe: true }, message: { conversation: 'x' } }, BOT), null)
  assert.equal(buildEvent({ key: { remoteJid: 'status@broadcast' }, message: { conversation: 'x' } }, BOT), null)
  assert.equal(buildEvent({ key: { remoteJid: 'a@s.whatsapp.net' }, message: { imageMessage: {} } }, BOT), null)
})

test('group mention and reply to the bot are detected, mention token stripped', () => {
  const key = { remoteJid: '1203@g.us', participant: '420777123456@s.whatsapp.net' }
  const mention = { key, message: { extendedTextMessage: { text: '@420111222333 kolik je 2+2', contextInfo: { mentionedJid: [BOT[0]] } } } }
  const ev = buildEvent(mention, BOT)
  assert.equal(ev.typ, 'skupina')
  assert.equal(ev.skupina, '1203@g.us')
  assert.equal(ev.oznacen, true)
  assert.equal(ev.odpoved_botovi, false)
  assert.equal(ev.text, 'kolik je 2+2')
  assert.equal(ev.od, '420777123456')

  const reply = { key, message: { extendedTextMessage: { text: 'díky', contextInfo: { participant: BOT[1] } } } }
  const ev2 = buildEvent(reply, BOT)
  assert.equal(ev2.oznacen, false)
  assert.equal(ev2.odpoved_botovi, true)
})

test('chunk splits long text on paragraphs and keeps everything', () => {
  const text = Array.from({ length: 40 }, (_, i) => `Odstavec ${i} ${'x'.repeat(200)}`).join('\n\n')
  const parts = chunk(text, 1000)
  assert.ok(parts.every((p) => p.length <= 1000))
  assert.equal(parts.join('\n\n').replace(/\s+/g, ''), text.replace(/\s+/g, ''))
  assert.deepEqual(chunk('krátké'), ['krátké'])
})

test('stripMention removes only leading mentions', () => {
  assert.equal(stripMention('@420111222333   ahoj @420999'), 'ahoj @420999')
})
