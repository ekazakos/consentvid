// Lossless, in-browser removal of identifying metadata from MP4 / MOV
// (ISO-BMFF / QuickTime) files: the browser counterpart of the CLI's
// `ffmpeg -map 0:V -map 0:a? -map_metadata -1 -c copy -fflags +bitexact`.
// No re-encoding: the media samples are copied byte for byte.
//
// What is removed:
//   * every top-level box except ftyp, moov and mdat (udta, meta, XMP and
//     vendor 'uuid' boxes, free/skip/wide padding that can hold stale data);
//   * in moov, everything but mvhd and the video/sound tracks: udta (©xyz
//     location, make, model, …), meta (Apple keys/ilst: com.apple.quicktime.*),
//     uuid, iods, and every other track (timed metadata 'mebx', GoPro 'gpmd'
//     telemetry, timecode, subtitles, chapters). The samples of a dropped
//     track are overwritten with zeros in mdat, so e.g. a GPS track's data
//     does not survive as unreferenced bytes;
//   * in kept tracks: tref, udta, meta, uuid;
//   * creation and modification times in mvhd, tkhd and mdhd (set to 0);
//   * handler names (hdlr) and the video compressorname, which often name
//     the device or its software.
// Chunk offsets (stco / co64) of the kept tracks are rewritten for the new
// layout. Fragmented files (moof / mvex) are refused: their offsets live in
// every fragment.
//
// Not covered (the CLI handles it with ffmpeg bitstream filters): user-data
// SEI messages inside H.264 / HEVC frames, which some cameras use.

const KEEP_TOP = new Set(['ftyp', 'moov', 'mdat'])
const KEEP_HANDLERS = new Set(['vide', 'soun'])
// Containers whose children are rebuilt (descended into), per level.
const KEEP_IN = {
  moov: new Set(['mvhd', 'trak']),
  trak: new Set(['tkhd', 'edts', 'mdia']),
  mdia: new Set(['mdhd', 'hdlr', 'minf']),
}
const DROP_ANYWHERE = new Set(['udta', 'meta', 'uuid', 'free', 'skip', 'wide', 'tref'])

export class Mp4Error extends Error {}

// --------------------------------------------------------------- parsing --

function fourcc(bytes, i) {
  return String.fromCharCode(bytes[i], bytes[i + 1], bytes[i + 2], bytes[i + 3])
}

/** The boxes in bytes[start, end): {type, start, end, header}. Throws on a
 * broken structure. */
export function readBoxes(bytes, start = 0, end = bytes.length) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const boxes = []
  let i = start
  while (i < end) {
    if (i + 8 > end) throw new Mp4Error('truncated box header')
    let size = view.getUint32(i)
    let header = 8
    if (size === 1) {
      if (i + 16 > end) throw new Mp4Error('truncated box header')
      const big = view.getBigUint64(i + 8)
      if (big > BigInt(end - i)) throw new Mp4Error('box larger than its container')
      size = Number(big)
      header = 16
    } else if (size === 0) {
      size = end - i
    }
    const type = fourcc(bytes, i + 4)
    if (type === 'uuid') header += 16
    if (size < header || i + size > end) throw new Mp4Error(`bad size for box '${type}'`)
    boxes.push({ type, start: i, end: i + size, header })
    i += size
  }
  return boxes
}

function child(bytes, box, type) {
  return readBoxes(bytes, box.start + box.header, box.end).find((b) => b.type === type) || null
}

function path(bytes, box, ...types) {
  let cur = box
  for (const t of types) {
    cur = cur && child(bytes, cur, t)
  }
  return cur
}

function handlerType(bytes, trak) {
  const hdlr = path(bytes, trak, 'mdia', 'hdlr')
  if (!hdlr) return null
  const p = hdlr.start + hdlr.header + 8 // version/flags, pre_defined (QuickTime: component type)
  return p + 4 <= hdlr.end ? fourcc(bytes, p) : null
}

/** Chunk offsets of a track (stco or co64), as Numbers. */
function chunkOffsets(bytes, trak) {
  const stbl = path(bytes, trak, 'mdia', 'minf', 'stbl')
  if (!stbl) throw new Mp4Error('track without a sample table')
  const kids = readBoxes(bytes, stbl.start + stbl.header, stbl.end)
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const stco = kids.find((b) => b.type === 'stco' || b.type === 'co64')
  if (!stco) throw new Mp4Error('track without chunk offsets')
  const p = stco.start + stco.header + 4
  const n = view.getUint32(p)
  const wide = stco.type === 'co64'
  if (p + 4 + n * (wide ? 8 : 4) > stco.end) throw new Mp4Error('truncated chunk offset table')
  const out = []
  for (let k = 0; k < n; k++) {
    out.push(wide ? Number(view.getBigUint64(p + 4 + 8 * k)) : view.getUint32(p + 4 + 4 * k))
  }
  return { offsets: out, kids, view }
}

/** [start, end) byte ranges of every sample of a track (for zeroing). */
function sampleRanges(bytes, trak) {
  const { offsets, kids, view } = chunkOffsets(bytes, trak)
  const stsc = kids.find((b) => b.type === 'stsc')
  const stsz = kids.find((b) => b.type === 'stsz')
  if (!stsc || !stsz) throw new Mp4Error('track without sample-to-chunk or sample-size table')
  const runs = []
  const nr = view.getUint32(stsc.start + stsc.header + 4)
  for (let k = 0; k < nr; k++) {
    const q = stsc.start + stsc.header + 8 + 12 * k
    if (q + 12 > stsc.end) throw new Mp4Error('truncated sample-to-chunk table')
    runs.push([view.getUint32(q), view.getUint32(q + 4)]) // first_chunk (1-based), samples_per_chunk
  }
  const zs = stsz.start + stsz.header
  const constant = view.getUint32(zs + 4)
  const count = view.getUint32(zs + 8)
  if (!constant && zs + 12 + 4 * count > stsz.end) throw new Mp4Error('truncated sample-size table')
  const sizeOf = (s) => (constant ? constant : view.getUint32(zs + 12 + 4 * s))
  const ranges = []
  let sample = 0
  for (let c = 0; c < offsets.length && sample < count; c++) {
    let perChunk = 0
    for (const [first, n] of runs) if (c + 1 >= first) perChunk = n
    let at = offsets[c]
    for (let s = 0; s < perChunk && sample < count; s++, sample++) {
      const size = sizeOf(sample)
      ranges.push([at, at + size])
      at += size
    }
  }
  return ranges
}

// -------------------------------------------------------------- building --

function u32(n) {
  const b = new Uint8Array(4)
  new DataView(b.buffer).setUint32(0, n)
  return b
}

function concat(parts) {
  const total = parts.reduce((n, p) => n + p.length, 0)
  const out = new Uint8Array(total)
  let i = 0
  for (const p of parts) {
    out.set(p, i)
    i += p.length
  }
  return out
}

/** A box with new content, keeping its type (and uuid usertype). */
function box(bytes, b, content) {
  const head = bytes.subarray(b.start + 4, b.start + 8)
  const extra = b.type === 'uuid' ? bytes.subarray(b.start + b.header - 16, b.start + b.header) : new Uint8Array(0)
  return concat([u32(8 + extra.length + content.length), head, extra, content])
}

function zeroTimes(content) {
  // Full box: version (1), flags (3), then creation_time and
  // modification_time, 32-bit (version 0) or 64-bit (version 1).
  const out = content.slice()
  const width = out[0] === 1 ? 8 : 4
  out.fill(0, 4, 4 + 2 * width)
  return out
}

function blankHandlerName(content) {
  // version/flags (4), pre_defined (4), handler_type (4), reserved (12), name.
  // One zero byte is an empty name both as an MP4 C string and as a
  // QuickTime counted string.
  if (content.length < 24) return content
  return concat([content.subarray(0, 24), new Uint8Array(1)])
}

// VisualSampleEntry (ISO 14496-12 §12.1.3): after the 8-byte box header,
// 42 bytes of fixed fields, then a 32-byte compressorname, which cameras and
// apps fill with their own name. Zeroing it keeps every size and offset.
const COMPRESSOR_NAME_AT = 8 + 42
// QuickTime's 'vendor' code (e.g. 'appl') sits in the pre_defined bytes
// that ISO-BMFF requires to be zero anyway.
const VENDOR_AT = 8 + 12
function blankCompressorNames(bytes, b) {
  const content = bytes.slice(b.start + b.header, b.end)
  for (const e of readBoxes(content, 8, content.length)) {
    if (e.end - e.start < COMPRESSOR_NAME_AT + 32) continue
    content.fill(0, e.start + VENDOR_AT, e.start + VENDOR_AT + 4)
    content.fill(0, e.start + COMPRESSOR_NAME_AT, e.start + COMPRESSOR_NAME_AT + 32)
  }
  return box(bytes, b, content)
}

function rewriteOffsets(bytes, b, mapOffset) {
  const content = bytes.slice(b.start + b.header, b.end)
  const view = new DataView(content.buffer)
  const n = view.getUint32(4)
  const wide = b.type === 'co64'
  for (let k = 0; k < n; k++) {
    const p = 8 + k * (wide ? 8 : 4)
    if (wide) view.setBigUint64(p, BigInt(mapOffset(Number(view.getBigUint64(p)))))
    else view.setUint32(p, mapOffset(view.getUint32(p)))
  }
  return box(bytes, b, content)
}

function rebuild(bytes, b, mapOffset, handler = null) {
  const keep = KEEP_IN[b.type]
  const parts = []
  for (const c of readBoxes(bytes, b.start + b.header, b.end)) {
    if (DROP_ANYWHERE.has(c.type)) continue
    if (keep && !keep.has(c.type)) continue
    const h = c.type === 'trak' ? handlerType(bytes, c) : handler
    if (c.type === 'trak' && !KEEP_HANDLERS.has(h)) continue
    const raw = () => bytes.subarray(c.start, c.end)
    const content = () => bytes.subarray(c.start + c.header, c.end)
    if (c.type === 'mvhd' || c.type === 'tkhd' || c.type === 'mdhd') parts.push(box(bytes, c, zeroTimes(content())))
    else if (c.type === 'hdlr') parts.push(box(bytes, c, blankHandlerName(content())))
    else if (c.type === 'stco' || c.type === 'co64') parts.push(rewriteOffsets(bytes, c, mapOffset))
    else if (c.type === 'stsd' && h === 'vide') parts.push(blankCompressorNames(bytes, c))
    else if (['trak', 'mdia', 'minf', 'stbl'].includes(c.type)) parts.push(rebuild(bytes, c, mapOffset, h))
    else parts.push(raw())
  }
  return box(bytes, b, concat(parts))
}

// ------------------------------------------------------------------ main --

/** Strip identifying metadata from an MP4 / MOV file (see the top of this
 * file). Returns {bytes, removed} with `removed` a list of what was taken
 * out (for the user), or throws Mp4Error when the file is not an ISO-BMFF
 * file this code can rewrite safely. */
export function stripMp4(input) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input)
  const top = readBoxes(bytes)
  if (!top.length || top[0].type !== 'ftyp') throw new Mp4Error('not an MP4/MOV file')
  if (top.some((b) => b.type === 'moof')) throw new Mp4Error('fragmented MP4 is not supported in the browser')
  const moovs = top.filter((b) => b.type === 'moov')
  const mdats = top.filter((b) => b.type === 'mdat')
  if (moovs.length !== 1 || !mdats.length) throw new Mp4Error('expected one moov box and media data')
  const moov = moovs[0]
  if (child(bytes, moov, 'mvex')) throw new Mp4Error('fragmented MP4 is not supported in the browser')

  const removed = []
  for (const b of top) if (!KEEP_TOP.has(b.type)) removed.push(`'${b.type}' box`)
  const traks = readBoxes(bytes, moov.start + moov.header, moov.end)
  for (const c of traks) {
    if (c.type === 'trak') {
      const h = handlerType(bytes, c)
      if (!KEEP_HANDLERS.has(h)) removed.push(`'${h || '?'}' track`)
    } else if (!KEEP_IN.moov.has(c.type)) {
      removed.push(`movie '${c.type}' box`)
    }
  }
  if (!traks.some((c) => c.type === 'trak' && handlerType(bytes, c) === 'vide')) {
    throw new Mp4Error('no video track')
  }

  // Zero the samples of the tracks that go, in a copy of the input.
  const work = bytes.slice()
  const inMdat = (a, z) => mdats.some((m) => a >= m.start + m.header && z <= m.end)
  for (const c of traks) {
    if (c.type !== 'trak' || KEEP_HANDLERS.has(handlerType(bytes, c))) continue
    for (const [a, z] of sampleRanges(bytes, c)) {
      if (!inMdat(a, z)) throw new Mp4Error('a dropped track has data outside the media data')
      work.fill(0, a, z)
    }
  }

  // New layout: ftyp, moov, mdat(s) in their original order. moov's new
  // size is independent of the offsets it holds (same field widths), so
  // build it once with a provisional mapping to learn its size, then again.
  const layout = top.filter((b) => KEEP_TOP.has(b.type))
  const place = (moovSize) => {
    const starts = new Map()
    let at = 0
    for (const b of layout) {
      starts.set(b, at)
      at += b === moov ? moovSize : b.end - b.start
    }
    return starts
  }
  const mapper = (starts) => (o) => {
    const m = mdats.find((d) => o >= d.start + d.header && o < d.end)
    if (!m) throw new Mp4Error('a chunk offset points outside the media data')
    return o - m.start + starts.get(m)
  }
  const size = rebuild(work, moov, (o) => o).length
  const starts = place(size)
  const newMoov = rebuild(work, moov, mapper(starts))
  if (newMoov.length !== size) throw new Mp4Error('internal: moov size changed')
  const out = concat(layout.map((b) => (b === moov ? newMoov : work.subarray(b.start, b.end))))
  removed.push('creation times, handler and encoder names')
  check(out)
  return { bytes: out, removed: [...new Set(removed)] }
}

/** Re-parse the result: every box well-formed, every chunk offset inside
 * the media data. */
export function check(bytes) {
  const top = readBoxes(bytes)
  const moov = top.find((b) => b.type === 'moov')
  const mdats = top.filter((b) => b.type === 'mdat')
  for (const t of readBoxes(bytes, moov.start + moov.header, moov.end)) {
    if (t.type !== 'trak') continue
    for (const o of chunkOffsets(bytes, t).offsets) {
      if (!mdats.some((m) => o >= m.start + m.header && o < m.end)) throw new Mp4Error('bad chunk offset after rewrite')
    }
  }
  return true
}

/** Is this an ISO-BMFF (MP4 / MOV) file? */
export function isMp4(bytes) {
  return bytes.length >= 12 && fourcc(bytes, 4) === 'ftyp'
}

/** For tests: the tracks of a file, their handler types and sample ranges. */
export const _internal = {
  tracks(bytes) {
    const moov = readBoxes(bytes).find((b) => b.type === 'moov')
    return moov ? readBoxes(bytes, moov.start + moov.header, moov.end).filter((b) => b.type === 'trak') : []
  },
  handlerType,
  sampleRanges,
}
