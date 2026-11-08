import assert from 'node:assert/strict'
import { test } from 'node:test'

import { buildReply, consentLink, didOfUri, graphemes, hasTag, isBotAuthor, mentions, rootFor, videoOf } from '../src/post.js'
import { BOT, videoEmbed } from './helpers.js'

const enc = new TextEncoder()
const dec = new TextDecoder()
const slice = (text, { byteStart, byteEnd }) => dec.decode(enc.encode(text).slice(byteStart, byteEnd))

test('videoOf: direct video, recordWithMedia video, and non-video embeds', () => {
  assert.equal(videoOf({ embed: videoEmbed('bafkreia') }).ref.$link, 'bafkreia')
  const rwm = { embed: { $type: 'app.bsky.embed.recordWithMedia', record: {}, media: videoEmbed('bafkreib') } }
  assert.equal(videoOf(rwm).ref.$link, 'bafkreib')
  assert.equal(videoOf({ embed: { $type: 'app.bsky.embed.images', images: [] } }), null)
  assert.equal(videoOf({ embed: { $type: 'app.bsky.embed.recordWithMedia', media: { $type: 'app.bsky.embed.images' } } }), null)
  assert.equal(videoOf({}), null)
  assert.equal(videoOf(undefined), null)
})

test('hasTag: tags field and tag facets, case-insensitive, # optional', () => {
  assert.ok(hasTag({ tags: ['ConsentVid'] }))
  assert.ok(hasTag({ tags: ['#consentvid'] }))
  assert.ok(hasTag({ facets: [{ features: [{ $type: 'app.bsky.richtext.facet#tag', tag: 'CONSENTVID' }] }] }))
  assert.ok(!hasTag({ text: '#consentvid' }), 'plain text without a facet is not a tag')
  assert.ok(!hasTag({ tags: ['consentvids'] }))
  assert.ok(!hasTag({ facets: [{ features: [{ $type: 'app.bsky.richtext.facet#link', uri: 'https://x/consentvid' }] }] }))
})

test('mentions and didOfUri', () => {
  assert.ok(mentions({ facets: [{ features: [{ $type: 'app.bsky.richtext.facet#mention', did: BOT }] }] }, BOT))
  assert.ok(!mentions({ facets: [] }, BOT))
  assert.equal(didOfUri('at://did:plc:abc123/app.bsky.feed.post/3k'), 'did:plc:abc123')
  assert.equal(didOfUri('at://did:web:example.com/app.bsky.feed.post/3k'), 'did:web:example.com')
  assert.equal(didOfUri('at://alice.bsky.social/app.bsky.feed.post/3k'), null)
})

test('isBotAuthor honours negation', () => {
  assert.ok(isBotAuthor({ labels: [{ val: 'bot', src: 'did:plc:x' }] }))
  assert.ok(!isBotAuthor({ labels: [{ val: 'bot', neg: true }] }))
  assert.ok(!isBotAuthor({ labels: [{ val: 'porn' }] }))
  assert.ok(!isBotAuthor({}))
})

test('rootFor keeps the thread root of a reply', () => {
  const top = { uri: 'at://a/app.bsky.feed.post/1', cid: 'c1', record: {} }
  assert.deepEqual(rootFor(top), { uri: top.uri, cid: 'c1' })
  const reply = { uri: 'at://a/app.bsky.feed.post/2', cid: 'c2', record: { reply: { root: { uri: 'at://r/x/0', cid: 'c0' }, parent: {} } } }
  assert.deepEqual(rootFor(reply), { uri: 'at://r/x/0', cid: 'c0' })
})

test('consentLink percent-encodes the at-uri as the web app expects', () => {
  const u = 'at://did:plc:abc/app.bsky.feed.post/3k'
  const link = consentLink('https://consentvid.org/app/', u)
  assert.equal(link, 'https://consentvid.org/app/?post=at%3A%2F%2Fdid%3Aplc%3Aabc%2Fapp.bsky.feed.post%2F3k')
  assert.equal(new URL(link).searchParams.get('post'), u)
})

test('buildReply: link facets cover the right UTF-8 bytes despite emoji and curly quotes', () => {
  const u = 'at://did:plc:abc/app.bsky.feed.post/3k'
  const { text, facets } = buildReply('https://consentvid.org/app/', u)
  assert.match(text, /🎬/)
  assert.match(text, /’/)
  assert.equal(facets.length, 2)
  assert.equal(slice(text, facets[0].index), 'consentvid.org/app')
  assert.equal(new URL(facets[0].features[0].uri).searchParams.get('post'), u)
  assert.equal(slice(text, facets[1].index), 'consentvid.org')
  assert.equal(facets[1].features[0].uri, 'https://consentvid.org')
  // Byte offsets differ from UTF-16 indices here, which is the point.
  assert.notEqual(facets[0].index.byteStart, text.indexOf('consentvid.org/app'))
  assert.ok(graphemes(text) <= 300)
  assert.ok(/bot/i.test(text), 'says it is a bot')
})

test('graphemes counts clusters, not code units', () => {
  assert.equal(graphemes('👍🏽'), 1)
  assert.equal(graphemes('é'), 1)
  assert.equal(graphemes('abc'), 3)
})
