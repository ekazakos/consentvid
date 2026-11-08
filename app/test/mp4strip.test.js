import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { scanMetadata } from '../src/gates.js'
import { _internal, Mp4Error, isMp4, readBoxes, stripMp4 } from '../src/mp4strip.js'

// Fixtures made with ffmpeg (64x48, 1 s, H.264 + AAC): a mov_text subtitle
// track whose sample says "GPS 50.0755N 14.4378E", ©xyz location and Apple
// make/model keys; moov after mdat (identifying.mp4, identifying.mov) and
// before it (identifying-faststart.mp4, which needs offset rewriting).
const FIXTURES = ['identifying.mp4', 'identifying-faststart.mp4', 'identifying.mov']
const load = (name) => new Uint8Array(readFileSync(new URL(`fixtures/${name}`, import.meta.url)))
const has = (bytes, s) => Buffer.from(bytes).includes(s)

/** Concatenated sample bytes per kept handler, in track order. */
function samples(bytes) {
  const out = {}
  for (const t of _internal.tracks(bytes)) {
    const h = _internal.handlerType(bytes, t)
    if (h !== 'vide' && h !== 'soun') continue
    out[h] = Buffer.concat(_internal.sampleRanges(bytes, t).map(([a, z]) => Buffer.from(bytes.subarray(a, z))))
  }
  return out
}

for (const name of FIXTURES) {
  test(`${name}: identifying metadata is removed, media samples are untouched`, () => {
    const input = load(name)
    assert.ok(scanMetadata(input).length > 0, 'the fixture carries metadata')
    const { bytes, removed } = stripMp4(input)
    assert.deepEqual(scanMetadata(bytes), [])
    for (const s of ['GPS 50', 'iPhone', 'secret', '+50.0755', 'libx264', 'Handler', 'FFMP']) {
      assert.ok(!has(bytes, s), s)
    }
    assert.ok(removed.some((r) => r.includes('udta')))
    assert.deepEqual(readBoxes(bytes).map((b) => b.type).filter((t) => t !== 'mdat'), ['ftyp', 'moov'].filter(
      (t) => readBoxes(bytes).some((b) => b.type === t),
    ))
    const before = samples(input)
    const after = samples(bytes)
    assert.ok(before.vide.length > 0 && before.soun.length > 0)
    assert.ok(after.vide.equals(before.vide) && after.soun.equals(before.soun))
    assert.deepEqual(Object.keys(after).sort(), ['soun', 'vide'])
    // A fixed point: stripping again changes nothing.
    assert.deepEqual(stripMp4(bytes).bytes, bytes)
  })
}

test('the dropped track’s samples are zeroed, not left behind', () => {
  const input = load('identifying.mp4')
  assert.ok(has(input, 'GPS 50'))
  assert.ok(!has(stripMp4(input).bytes, 'GPS 50'))
})

const ffmpeg = spawnSync('ffmpeg', ['-version']).status === 0
test('decodes to the same frames (ffmpeg framemd5)', { skip: !ffmpeg && 'ffmpeg not installed' }, () => {
  const dir = mkdtempSync(join(tmpdir(), 'mp4strip-'))
  try {
    for (const name of FIXTURES) {
      const src = new URL(`fixtures/${name}`, import.meta.url).pathname
      const out = join(dir, name)
      writeFileSync(out, stripMp4(load(name)).bytes)
      const md5 = (f, maps) =>
        spawnSync('ffmpeg', ['-v', 'error', '-i', f, ...maps, '-f', 'framemd5', '-'], { encoding: 'utf8' })
          .stdout.split('\n')
          .filter((l) => l && !l.startsWith('#'))
          .join('\n')
      const want = md5(src, ['-map', '0:v', '-map', '0:a'])
      assert.ok(want.length > 0)
      assert.equal(md5(out, []), want, name)
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

// ------------------------------------------------------- crafted files --

function bx(type, ...parts) {
  const body = Buffer.concat(parts.map((p) => Buffer.from(p)))
  const head = Buffer.alloc(8)
  head.writeUInt32BE(8 + body.length)
  head.write(type, 4, 'latin1')
  return Buffer.concat([head, body])
}
const full = (v = 0) => Buffer.from([v, 0, 0, 0])
const u32 = (...ns) => Buffer.concat(ns.map((n) => { const b = Buffer.alloc(4); b.writeUInt32BE(n); return b }))
const u64 = (n) => { const b = Buffer.alloc(8); b.writeBigUInt64BE(BigInt(n)); return b }

function trak(handler, offsetBox, sizes) {
  return bx('trak',
    bx('tkhd', full(), u32(111, 222), Buffer.alloc(72)),
    bx('mdia',
      bx('mdhd', full(), u32(111, 222), u32(1000, 1), u32(0)),
      bx('hdlr', full(), u32(0), Buffer.from(handler, 'latin1'), Buffer.alloc(12), Buffer.from('Acme Cam 3000\0')),
      bx('minf', bx('stbl',
        bx('stsd', full(), u32(0)),
        bx('stsc', full(), u32(1), u32(1, sizes.length, 1)),
        bx('stsz', full(), u32(0, sizes.length), u32(...sizes)),
        offsetBox,
      ))),
    bx('udta', bx('©xyz', Buffer.from('+50+14/'))))
}

function crafted({ wide = false } = {}) {
  // ftyp, udta, moov (video track + a 'meta' track), mdat with both tracks'
  // samples. Offsets are filled in once the layout is known.
  const ftyp = bx('ftyp', 'isom', u32(0), 'isom')
  const junk = bx('udta', bx('©xyz', Buffer.from('+50.1+14.4/')))
  const video = Buffer.from('VIDEO-SAMPLE-1VIDEO-SAMPLE-2')
  const gps = Buffer.from('LAT=50.0755')
  const build = (vOff, mOff) =>
    bx('moov',
      bx('mvhd', full(), u32(111, 222), Buffer.alloc(88)),
      trak('vide', wide ? bx('co64', full(), u32(1), u64(vOff)) : bx('stco', full(), u32(1, vOff)), [14, 14]),
      trak('meta', bx('stco', full(), u32(1, mOff)), [gps.length]),
      bx('meta', full(), bx('keys', full(), u32(0))))
  const moovLen = build(0, 0).length
  const mdatStart = ftyp.length + junk.length + moovLen
  const moov = build(mdatStart + 8, mdatStart + 8 + video.length)
  return { file: new Uint8Array(Buffer.concat([ftyp, junk, moov, bx('mdat', video, gps)])), video, gps }
}

for (const wide of [false, true]) {
  test(`crafted moov-before-mdat (${wide ? 'co64' : 'stco'}): offsets follow the moved mdat`, () => {
    const { file, video, gps } = crafted({ wide })
    const { bytes, removed } = stripMp4(file)
    assert.ok(bytes.length < file.length)
    assert.deepEqual(samples(bytes), { vide: video })
    assert.ok(!has(bytes, 'LAT=') && !has(bytes, 'Acme') && !has(bytes, '+50'))
    assert.ok(removed.includes("'udta' box") && removed.includes("'meta' track") && removed.includes("movie 'meta' box"))
    // Creation/modification times zeroed.
    assert.ok(!has(bytes, Buffer.from(u32(111, 222))))
    assert.ok(has(file, gps))
  })
}

test('refuses what it can’t rewrite safely', () => {
  assert.throws(() => stripMp4(new Uint8Array(Buffer.from('\x1aE\xdf\xa3 webm/matroska'))), Mp4Error)
  const ftyp = bx('ftyp', 'isom', u32(0), 'isom')
  assert.throws(() => stripMp4(new Uint8Array(Buffer.concat([ftyp, bx('moov'), bx('moof'), bx('mdat')]))), /fragmented/)
  assert.throws(() => stripMp4(new Uint8Array(Buffer.concat([ftyp, bx('moov', bx('mvex')), bx('mdat')]))), /fragmented/)
  const { file } = crafted()
  assert.throws(() => stripMp4(file.subarray(0, file.length - 5)), Mp4Error) // truncated
  const noVideo = Buffer.concat([ftyp, bx('moov', bx('mvhd', full(), Buffer.alloc(96))), bx('mdat', 'x')])
  assert.throws(() => stripMp4(new Uint8Array(noVideo)), /no video track/)
  assert.ok(isMp4(file) && !isMp4(new Uint8Array(Buffer.from('RIFF....AVI '))))
})
