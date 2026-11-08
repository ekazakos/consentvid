import assert from 'node:assert/strict'
import { test } from 'node:test'

import { decideTraining, isBotProfile, mp4MetadataRegions, scanMetadata } from '../src/gates.js'

const PREF = 'community.lexicon.preference.ai'
const pref = (scope, allow) => ({ $type: PREF, scope, preferences: { training: { allow } } })
const GLOBAL = { $type: '#globalScope' }
const ours = { $type: PREF + '#collectionScope', collection: 'org.consentvid.videoV2' }
const other = { $type: '#collectionScope', collection: 'app.bsky.feed.post' }

test('bot self-label', () => {
  assert.ok(isBotProfile({ labels: { $type: 'com.atproto.label.defs#selfLabels', values: [{ val: 'bot' }] } }))
  assert.ok(!isBotProfile({ labels: { $type: 'com.atproto.label.defs#selfLabels', values: [{ val: 'nudity' }] } }))
  assert.ok(!isBotProfile({ labels: { values: [{ val: 'bot' }] } }))
  assert.ok(!isBotProfile(null))
})

test('AI-usage preference precedence (signals.decide_training)', () => {
  assert.equal(decideTraining(null, []).denied, false)
  assert.equal(decideTraining(pref(GLOBAL, false), []).denied, true)
  assert.equal(decideTraining(pref(GLOBAL, true), []).denied, false)
  // A ConsentVid-scoped override beats the global default, in both directions.
  assert.equal(decideTraining(pref(GLOBAL, false), [pref(ours, true)]).denied, false)
  assert.equal(decideTraining(pref(GLOBAL, true), [pref(ours, false)]).denied, true)
  // Among overrides a deny wins; other collections and entities don't count.
  assert.equal(decideTraining(null, [pref(ours, true), pref(ours, false)]).denied, true)
  assert.equal(decideTraining(pref(GLOBAL, true), [pref(other, false)]).denied, false)
  assert.equal(decideTraining(null, [pref({ $type: '#entityScope', entity: 'x' }, false)]).denied, false)
  const notAPref = { $type: 'other', scope: GLOBAL, preferences: { training: { allow: false } } }
  assert.equal(decideTraining(notAPref, []).denied, false)
})

function box(type, payload) {
  const body = typeof payload === 'string' ? new TextEncoder().encode(payload) : payload
  const out = new Uint8Array(8 + body.length)
  new DataView(out.buffer).setUint32(0, out.length)
  out.set(new TextEncoder().encode(type), 4)
  out.set(body, 8)
  return out
}

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let i = 0
  for (const p of parts) {
    out.set(p, i)
    i += p.length
  }
  return out
}

const FTYP = box('ftyp', 'isom\u0000\u0000\u0002\u0000isomiso2')

test('metadata scan looks only outside mdat in MP4', () => {
  const clean = concat(FTYP, box('moov', 'trak mvhd stuff'), box('mdat', 'random loci mebx bytes'))
  assert.equal(mp4MetadataRegions(clean).length, 2)
  assert.deepEqual(scanMetadata(clean), [])
  const loc = new Uint8Array([0xa9, ...new TextEncoder().encode('xyz+50.08+014.42/')])
  const dirty = concat(FTYP, box('moov', box('udta', loc)), box('mdat', 'x'))
  assert.deepEqual(scanMetadata(dirty), ['GPS location (©xyz)'])
  const apple = concat(FTYP, box('moov', 'keys com.apple.quicktime.location.ISO6709 mebx'), box('mdat', 'x'))
  assert.deepEqual(scanMetadata(apple), ['GPS location (Apple)', 'Apple timed metadata track'])
})

test('other containers: whole file, long markers only', () => {
  const webm = new TextEncoder().encode('\u001aEß£ loci mebx <x:xmpmeta ')
  assert.equal(mp4MetadataRegions(webm), null)
  assert.deepEqual(scanMetadata(webm), ['XMP metadata'])
})

test('a broken box structure falls back to the whole-file scan', () => {
  const broken = concat(box('ftyp', 'isom'), new Uint8Array([0, 0, 0, 3, 0x6d, 0x6f, 0x6f, 0x76]))
  assert.equal(mp4MetadataRegions(broken), null)
})
