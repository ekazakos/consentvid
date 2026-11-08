// Record construction for the consent web app: the JS mirror of the Python
// writer (consentvid.versions.video_record_v2 / consent_record_v2 and
// consentvid.cli._register_post / _consent_record). Pure functions, no DOM,
// so they run under `node --test` and produce the shared test vectors that
// tests/test_web_app.py validates with the Python validators.

import * as dagCbor from '@ipld/dag-cbor'
import { CID } from 'multiformats/cid'
import { sha256 } from 'multiformats/hashes/sha2'

import videoLexicon from '../../src/consentvid/schemas/org.consentvid.videoV2.json' with { type: 'json' }
import consentLexicon from '../../src/consentvid/schemas/org.consentvid.consentV2.json' with { type: 'json' }

export const VIDEO_NSID = 'org.consentvid.videoV2'
export const CONSENT_NSID = 'org.consentvid.consentV2'
export const POST_NSID = 'app.bsky.feed.post'
export const PROFILE_NSID = 'app.bsky.actor.profile'
export const AI_PREF_NSID = 'community.lexicon.preference.ai'
export const SELF_LABELS_TYPE = 'com.atproto.label.defs#selfLabels'

// Vocabulary, straight from the lexicons (single source of truth).
const cprops = consentLexicon.defs.main.record.properties
const vprops = videoLexicon.defs.main.record.properties
export const VOCAB = {
  modalities: cprops.modalities.items.knownValues,
  purposes: cprops.purposes.items.knownValues,
  modelClasses: cprops.modelClasses.items.knownValues,
  compensation: cprops.compensation.knownValues,
  rightsBasis: cprops.rightsBasis.knownValues,
  captureProvenance: cprops.captureProvenance.knownValues,
  peoplePresent: cprops.peoplePresent.knownValues,
}
export const VIDEO_MIME_TYPES = vprops.mimeType.knownValues
// Bluesky's global self-labels for adult and graphic content (vocab.py).
export const SENSITIVE_SELF_LABELS = ['porn', 'sexual', 'nudity', 'graphic-media']
// 'attested' needs capture-time cryptographic provenance, which nothing
// verifies yet: the CLI refuses it, so does the app.
export const OFFERED_PROVENANCE = VOCAB.captureProvenance.filter((v) => v !== 'attested')
export const IDENTITY_MODALITIES = ['face', 'voice']

// ------------------------------------------------------------------ TIDs --

const TID_ALPHABET = '234567abcdefghijklmnopqrstuvwxyz'
let lastTid = 0n

export function encodeTid(value) {
  let v = BigInt(value)
  let out = ''
  for (let i = 0; i < 13; i++) {
    out = TID_ALPHABET[Number(v & 31n)] + out
    v >>= 5n
  }
  return out
}

/** A record key of type tid (atproto.com/specs/tid): 53 bits of microseconds
 * since the epoch, a random 10-bit clock id. Monotonic within the page. */
export function newTid(nowMs = Date.now(), clockId = randomClockId()) {
  const micros = BigInt(Math.floor(nowMs * 1000)) & ((1n << 53n) - 1n)
  let value = (micros << 10n) | BigInt(clockId & 1023)
  if (value <= lastTid) value = lastTid + 1n
  lastTid = value
  return encodeTid(value)
}

function randomClockId() {
  const a = new Uint16Array(1)
  globalThis.crypto.getRandomValues(a)
  return a[0] & 1023
}

export const TID_RE = /^[234567abcdefghij][234567abcdefghijklmnopqrstuvwxyz]{12}$/

// ------------------------------------------------------------------ CIDs --

/** The CID a PDS assigns to a record (CIDv1, dag-cbor, sha-256, base32).
 * Our records hold only strings, integers, booleans, arrays and objects. */
export async function recordCid(record) {
  const bytes = dagCbor.encode(record)
  const digest = await sha256.digest(bytes)
  return CID.createV1(dagCbor.code, digest).toString()
}

/** The raw-codec sha-256 CID of some bytes (what a blob ref carries). */
export async function rawCid(bytes) {
  const digest = await sha256.digest(bytes)
  return CID.createV1(0x55, digest).toString()
}

export function isRawSha256Cid(s) {
  try {
    const cid = CID.parse(s)
    return cid.version === 1 && cid.code === 0x55 && cid.multihash.code === 0x12 && cid.multihash.size === 32
  } catch {
    return false
  }
}

// ----------------------------------------------------------------- posts --

export function parseAtUri(uri) {
  const m = /^at:\/\/(did:[a-z]+:[a-zA-Z0-9._:%-]*[a-zA-Z0-9._-])\/([a-zA-Z0-9.-]+)\/([a-zA-Z0-9._:~-]{1,512})$/.exec(
    typeof uri === 'string' ? uri : '',
  )
  if (!m) return null
  return { did: m[1], collection: m[2], rkey: m[3] }
}

/** The ?post= deep link: an at:// URI of an app.bsky.feed.post, or null. */
export function parsePostParam(value) {
  const at = parseAtUri(value)
  return at && at.collection === POST_NSID ? at : null
}

/** The video blob a post embeds (directly or as recordWithMedia's media). */
export function videoBlobOf(post) {
  let embed = post && typeof post === 'object' ? post.embed : null
  if (!embed || typeof embed !== 'object') return null
  if (embed.$type === 'app.bsky.embed.recordWithMedia') embed = embed.media
  if (!embed || embed.$type !== 'app.bsky.embed.video') return null
  const blob = embed.video
  if (!blob || typeof blob !== 'object') return null
  const ref = blob.ref
  const cid = typeof ref === 'string' ? ref : ref && typeof ref.$link === 'string' ? ref.$link : null
  if (!cid) return null
  return {
    cid,
    mimeType: typeof blob.mimeType === 'string' ? blob.mimeType : null,
    size: Number.isInteger(blob.size) ? blob.size : null,
  }
}

export function postSelfLabels(post) {
  const labels = post && post.labels
  if (!labels || labels.$type !== SELF_LABELS_TYPE || !Array.isArray(labels.values)) return []
  return labels.values.map((v) => v && v.val).filter((v) => typeof v === 'string')
}

const UNSAFE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu

/** Title from the post text: first non-empty line, control/format characters
 * dropped, whitespace collapsed, <= 300 graphemes and 3000 UTF-8 bytes
 * (bsky.default_title). */
export function defaultTitle(post, limit = 300, maxBytes = 3000) {
  const text = post && typeof post.text === 'string' ? post.text : ''
  for (const raw of text.split(/\r\n|\r|\n/)) {
    const line = raw.replace(UNSAFE, '').split(/\s+/).filter(Boolean).join(' ')
    if (!line) continue
    return truncateGraphemes(line, limit, maxBytes)
  }
  return 'Bluesky video'
}

/** One line of user text: control/format characters dropped, whitespace collapsed. */
export function cleanLine(s) {
  return String(s).replace(UNSAFE, '').split(/\s+/).filter(Boolean).join(' ')
}

/** Why the user's video details can't be written (videoV2 limits), or []. */
export function videoErrors({ title, description, tags }) {
  const errors = []
  const enc = new TextEncoder()
  const t = title ? cleanLine(title) : ''
  if (title && !t) errors.push('The title is empty.')
  if (graphemeLength(t) > 300 || enc.encode(t).length > 3000) errors.push('The title is too long (300 characters).')
  const d = description || ''
  if (graphemeLength(d) > 3000 || enc.encode(d).length > 30000) errors.push('The description is too long (3000 characters).')
  if (/[\p{Cc}\p{Cf}]/u.test(d.replace(/[\n\t]/g, ''))) errors.push('The description contains control characters.')
  const tagList = [...new Set((tags || []).map((x) => x.trim()).filter(Boolean))]
  if (tagList.length > 8) errors.push('At most 8 tags.')
  for (const tag of tagList) {
    if (graphemeLength(tag) > 64 || enc.encode(tag).length > 640) errors.push(`Tag “${tag.slice(0, 20)}…” is too long.`)
    if (UNSAFE_ONE.test(tag)) errors.push('Tags must not contain control characters.')
  }
  return errors
}

const UNSAFE_ONE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u

export function truncateGraphemes(s, limit, maxBytes) {
  const seg = new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  const enc = new TextEncoder()
  let out = ''
  let n = 0
  let size = 0
  for (const { segment } of seg.segment(s)) {
    if (n >= limit) break
    size += enc.encode(segment).length
    if (size > maxBytes) break
    out += segment
    n++
  }
  return out
}

export function graphemeLength(s) {
  return [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(s)].length
}

// A conservative subset of BCP-47 that the Python validator accepts:
// lowercase primary subtag, then subtags of 2-8 alphanumerics.
const LANG_RE = /^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$/

export function postLangs(post) {
  const langs = Array.isArray(post && post.langs) ? post.langs : []
  return [...new Set(langs.filter((l) => typeof l === 'string' && LANG_RE.test(l)))].slice(0, 3)
}

// --------------------------------------------------------------- records --

/** The org.consentvid.videoV2 record for a Bluesky video post.
 * `probe` = {size, width, height, durationMs} measured from the bytes. */
export function videoRecord({ postUri, postCid, post, title, description, tags, labels, probe, createdAt }) {
  const blob = videoBlobOf(post)
  if (!blob) throw new Error('the post has no video')
  const rec = {
    $type: VIDEO_NSID,
    title: (title && cleanLine(title)) || defaultTitle(post),
    contentHash: blob.cid,
    storage: { $type: `${VIDEO_NSID}#bskyPostRef`, post: { uri: postUri, cid: postCid }, blob: blob.cid },
    createdAt,
  }
  if (description) rec.description = description
  if (blob.mimeType && VIDEO_MIME_TYPES.includes(blob.mimeType)) rec.mimeType = blob.mimeType
  for (const [k, v] of Object.entries({
    size: probe?.size,
    durationMs: probe?.durationMs,
    width: probe?.width,
    height: probe?.height,
  })) {
    if (Number.isInteger(v) && v >= (k === 'size' ? 1 : 0)) rec[k] = v
  }
  const langs = postLangs(post)
  if (langs.length) rec.langs = langs
  const t = [...new Set((tags || []).map((x) => x.trim()).filter(Boolean))]
  if (t.length) rec.tags = t
  const l = [...new Set(labels || [])].filter((x) => SENSITIVE_SELF_LABELS.includes(x))
  if (l.length) rec.labels = { $type: SELF_LABELS_TYPE, values: l.map((val) => ({ val })) }
  return rec
}

/** The org.consentvid.consentV2 record (kebab-case vocabulary). */
export function consentRecord({ videoUri, videoCid, terms, createdAt }) {
  const rec = {
    $type: CONSENT_NSID,
    subject: { uri: videoUri, cid: videoCid },
    modalities: [...new Set(terms.modalities)],
    purposes: [...new Set(terms.purposes)],
    modelClasses: [...new Set(terms.modelClasses)],
    compensation: terms.compensation || 'none',
    rightsBasis: terms.rightsBasis || 'own-recording',
    captureProvenance: terms.captureProvenance || 'self-attested',
    peoplePresent: terms.peoplePresent,
    authorshipAttested: terms.authorshipAttested === true,
    adultAttested: terms.adultAttested === true,
    createdAt,
  }
  if (terms.expiresAt) rec.expiresAt = terms.expiresAt
  return rec
}

/** Why these terms can't be signed (the Python _require_terms rules), or []. */
export function termsErrors(terms) {
  const errors = []
  if (terms.authorshipAttested !== true) errors.push('Confirm that you recorded the video or hold the rights to it.')
  if (terms.adultAttested !== true) errors.push('Confirm that you are an adult able to consent.')
  if (!VOCAB.peoplePresent.includes(terms.peoplePresent)) errors.push('Say who appears in the video.')
  for (const [k, label] of [
    ['modalities', 'what may be used'],
    ['purposes', 'what it may be used for'],
    ['modelClasses', 'who may use it'],
  ]) {
    if (!Array.isArray(terms[k]) || terms[k].length === 0) errors.push(`Choose ${label}.`)
    else if (terms[k].some((v) => !VOCAB[k].includes(v))) errors.push(`Unknown value in ${label}.`)
  }
  if (terms.captureProvenance === 'attested') errors.push("'attested' provenance can't be verified yet.")
  if (
    terms.peoplePresent === 'no-identifiable-people' &&
    (terms.modalities || []).some((m) => IDENTITY_MODALITIES.includes(m))
  ) {
    errors.push('Face or voice use contradicts “no identifiable people”.')
  }
  if (terms.expiresAt && !(Date.parse(terms.expiresAt) > Date.now())) errors.push('The expiry must be in the future.')
  return errors
}

/** The two records and the applyWrites body that create them in one commit
 * (the consent is the video's sidecar: same record key). */
export async function buildContribution({ did, rkey, postUri, postCid, post, video, terms, createdAt }) {
  const videoUri = `at://${did}/${VIDEO_NSID}/${rkey}`
  const v = videoRecord({ postUri, postCid, post, createdAt, ...video })
  const videoCid = await recordCid(v)
  const c = consentRecord({ videoUri, videoCid, terms, createdAt })
  return {
    videoUri,
    videoCid,
    video: v,
    consent: c,
    body: {
      repo: did,
      writes: [
        { $type: 'com.atproto.repo.applyWrites#create', collection: VIDEO_NSID, rkey, value: v },
        { $type: 'com.atproto.repo.applyWrites#create', collection: CONSENT_NSID, rkey, value: c },
      ],
    },
  }
}

/** applyWrites body withdrawing a contribution: both records, one commit. */
export function withdrawBody(did, rkey, { videoExists = true, consentExists = true } = {}) {
  const writes = []
  if (consentExists) writes.push({ $type: 'com.atproto.repo.applyWrites#delete', collection: CONSENT_NSID, rkey })
  if (videoExists) writes.push({ $type: 'com.atproto.repo.applyWrites#delete', collection: VIDEO_NSID, rkey })
  return { repo: did, writes }
}

/** Expiry choice -> RFC 3339 datetime (or undefined for none). */
export function expiryFrom(choice, now = new Date()) {
  const years = { '1y': 1, '2y': 2, '5y': 5 }[choice]
  if (!years) return undefined
  const d = new Date(now.getTime())
  d.setUTCFullYear(d.getUTCFullYear() + years)
  return d.toISOString()
}
