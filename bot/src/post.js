// Recognising tagged video posts and building the reply.

export const TAG = 'consentvid'
export const MAX_GRAPHEMES = 300

// The video blob of a post record: app.bsky.embed.video, directly or as the
// media of app.bsky.embed.recordWithMedia. Null when the post has no video.
export function videoOf(record) {
  const e = record?.embed
  if (!e) return null
  if (e.$type === 'app.bsky.embed.video') return e.video || null
  if (e.$type === 'app.bsky.embed.recordWithMedia' && e.media?.$type === 'app.bsky.embed.video') {
    return e.media.video || null
  }
  return null
}

const norm = (t) => String(t || '').replace(/^#/, '').toLowerCase()

// Tagged through the record's `tags` field or a rich-text tag facet.
export function hasTag(record, tag = TAG) {
  if ((record?.tags || []).some((t) => norm(t) === tag)) return true
  for (const f of record?.facets || []) {
    for (const feat of f.features || []) {
      if (feat.$type === 'app.bsky.richtext.facet#tag' && norm(feat.tag) === tag) return true
    }
  }
  return false
}

export function mentions(record, did) {
  for (const f of record?.facets || []) {
    for (const feat of f.features || []) {
      if (feat.$type === 'app.bsky.richtext.facet#mention' && feat.did === did) return true
    }
  }
  return false
}

export function didOfUri(uri) {
  const m = /^at:\/\/(did:[a-z0-9]+:[A-Za-z0-9._:%-]+)\//.exec(uri || '')
  return m ? m[1] : null
}

// A self-label or labeler label `bot` (not negated) on the author.
export function isBotAuthor(author) {
  return (author?.labels || []).some((l) => l.val === 'bot' && !l.neg)
}

// Thread root for a reply to `target` (a post view or {uri, cid, record}).
export function rootFor(target) {
  const r = target.record?.reply?.root
  return r?.uri && r?.cid ? { uri: r.uri, cid: r.cid } : { uri: target.uri, cid: target.cid }
}

export function consentLink(appUrl, videoUri) {
  const u = new URL(appUrl)
  return `${u.origin}${u.pathname}?post=${encodeURIComponent(videoUri)}`
}

export function graphemes(text) {
  return [...new Intl.Segmenter('en', { granularity: 'grapheme' }).segment(text)].length
}

const enc = new TextEncoder()
const bytes = (s) => enc.encode(s).length

// Reply text with link facets (byte offsets are UTF-8, as atproto requires).
export function buildReply(appUrl, videoUri) {
  const app = new URL(appUrl)
  const parts = [
    { text: 'Hi! 🎬 Want this video in an open, consent-first video dataset? You can give consent (and withdraw it any time) here: ' },
    { text: app.host + app.pathname.replace(/\/$/, ''), link: consentLink(appUrl, videoUri) },
    { text: '\n\nYour consent is a record in your own Bluesky account. I’m a bot and only reply when tagged or mentioned. About: ' },
    { text: app.host, link: app.origin },
  ]
  let text = ''
  let offset = 0
  const facets = []
  for (const p of parts) {
    const n = bytes(p.text)
    if (p.link) {
      facets.push({
        index: { byteStart: offset, byteEnd: offset + n },
        features: [{ $type: 'app.bsky.richtext.facet#link', uri: p.link }],
      })
    }
    text += p.text
    offset += n
  }
  if (graphemes(text) > MAX_GRAPHEMES) throw new Error('reply text is too long')
  return { text, facets }
}
