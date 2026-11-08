// Pre-flight checks the app runs before offering to sign: the JS mirror of
// consentvid.signals (bot self-label, community.lexicon.preference.ai) and a
// heuristic scan of the video bytes for identifying container metadata.
//
// These are courtesy checks that explain a refusal up front. The binding
// checks are the readers' (resync, export, AppView): they read the same
// records with signature and Merkle proofs and drop the clip.

import { AI_PREF_NSID, SELF_LABELS_TYPE } from './records.js'

export const BOT_LABEL = 'bot'
export const OUR_COLLECTIONS = [
  'org.consentvid.video',
  'org.consentvid.videoV2',
  'org.consentvid.consent',
  'org.consentvid.consentV2',
]

/** Does an app.bsky.actor.profile record carry the 'bot' self-label? */
export function isBotProfile(profile) {
  const labels = profile && profile.labels
  if (!labels || labels.$type !== SELF_LABELS_TYPE || !Array.isArray(labels.values)) return false
  return labels.values.some((v) => v && v.val === BOT_LABEL)
}

function scopeOf(record) {
  if (!record || record.$type !== AI_PREF_NSID) return null
  const scope = record.scope
  const t = scope && scope.$type
  if (t === '#globalScope' || t === `${AI_PREF_NSID}#globalScope`) return ['global', null]
  if ((t === '#collectionScope' || t === `${AI_PREF_NSID}#collectionScope`) && typeof scope.collection === 'string')
    return ['collection', scope.collection]
  if ((t === '#entityScope' || t === `${AI_PREF_NSID}#entityScope`) && typeof scope.entity === 'string')
    return ['entity', scope.entity]
  return null
}

function trainingAllow(record) {
  const allow = record?.preferences?.training?.allow
  return typeof allow === 'boolean' ? allow : null
}

/** {denied, detail} from the 'self' preference record (or null) and the
 * other preference records (signals.decide_training): an override scoped to
 * a ConsentVid collection beats the account-wide default, and among those a
 * deny wins. Entity-scoped overrides are ignored. */
export function decideTraining(globalRecord, overrides) {
  const relevant = []
  for (const rec of overrides || []) {
    const scope = scopeOf(rec)
    if (scope && scope[0] === 'collection' && OUR_COLLECTIONS.includes(scope[1])) {
      const allow = trainingAllow(rec)
      if (allow !== null) relevant.push([allow, scope[1]])
    }
  }
  if (relevant.length) {
    const denied = relevant.filter(([allow]) => allow === false)
    if (denied.length) return { denied: true, detail: `override for ${denied[0][1]}` }
    return { denied: false, detail: 'override allows training' }
  }
  const scope = scopeOf(globalRecord)
  if (scope && scope[0] === 'global' && trainingAllow(globalRecord) === false)
    return { denied: true, detail: 'account-wide default' }
  return { denied: false, detail: '' }
}

// Byte patterns of container metadata that identifies a place, device or
// person (ISO 6709 location atoms, Apple QuickTime keys and the timed
// metadata track, 3GPP location, XMP packets). A heuristic: the CLI uses
// ffprobe; the browser can't. Found => the app refuses unless the user
// insists, as `contribute --from-post` does without --keep-metadata.
const MARKERS = [
  ['©xyz', 'GPS location (©xyz)'],
  ['com.apple.quicktime.location', 'GPS location (Apple)'],
  ['com.apple.quicktime.make', 'device make (Apple)'],
  ['com.apple.quicktime.model', 'device model (Apple)'],
  ['com.apple.quicktime.software', 'device software (Apple)'],
  ['mebx', 'Apple timed metadata track'],
  ['loci', 'location (3GPP)'],
  ['<x:xmpmeta', 'XMP metadata'],
  ['gpmd', 'GoPro telemetry track'],
]

function bytesOf(s) {
  // Latin-1: the markers are byte strings (© is 0xA9 in QuickTime atom names).
  return Uint8Array.from([...s].map((ch) => ch.charCodeAt(0) & 0xff))
}

const COMPILED = MARKERS.map(([m, label]) => [bytesOf(m), label])

/** Labels of identifying-metadata markers found in the bytes (deduplicated).
 * In an ISO-BMFF (MP4/MOV) file only the boxes outside 'mdat' are searched:
 * short atom names would otherwise match by chance inside compressed media.
 * Other containers are searched whole, for the long markers only. */
export function scanMetadata(bytes) {
  const regions = mp4MetadataRegions(bytes)
  const found = []
  for (const [needle, label] of COMPILED) {
    if (regions === null) {
      if (needle.length > 4 && indexOf(bytes, needle) >= 0) found.push(label)
    } else if (regions.some(([a, b]) => indexOf(bytes.subarray(a, b), needle) >= 0)) {
      found.push(label)
    }
  }
  return [...new Set(found)]
}

/** [start, end) of every top-level box except 'mdat', or null if the bytes
 * don't start with an ISO-BMFF 'ftyp' box or the box structure is broken. */
export function mp4MetadataRegions(bytes) {
  const type = (i) => String.fromCharCode(bytes[i + 4], bytes[i + 5], bytes[i + 6], bytes[i + 7])
  if (bytes.length < 16 || type(0) !== 'ftyp') return null
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const regions = []
  let i = 0
  while (i + 8 <= bytes.length) {
    let size = view.getUint32(i)
    if (size === 1) {
      if (i + 16 > bytes.length) return null
      const big = view.getBigUint64(i + 8)
      if (big > BigInt(bytes.length - i)) return null
      size = Number(big)
    } else if (size === 0) {
      size = bytes.length - i
    }
    if (size < 8 || i + size > bytes.length) return null
    if (type(i) !== 'mdat') regions.push([i, i + size])
    i += size
  }
  return regions
}

function indexOf(hay, needle) {
  const first = needle[0]
  const n = needle.length
  outer: for (let i = hay.indexOf(first); i !== -1 && i <= hay.length - n; i = hay.indexOf(first, i + 1)) {
    for (let j = 1; j < n; j++) if (hay[i + j] !== needle[j]) continue outer
    return i
  }
  return -1
}
