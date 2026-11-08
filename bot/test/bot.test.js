import assert from 'node:assert/strict'
import { test } from 'node:test'

import { runOnce } from '../src/bot.js'
import { Store } from '../src/store.js'
import { ALICE, BOB, BOT, ENV, json, makeD1, makeFetch, NOW, postView, quiet, tagged, videoEmbed, world } from './helpers.js'

const enc = new TextEncoder()
const dec = new TextDecoder()

async function run(db, w, { env = {}, now = NOW, log = quiet } = {}) {
  const fetchFn = makeFetch(w.routes)
  const summary = await runOnce(ENV(db, env), { fetchFn, now, log })
  return { summary, fetchFn }
}

test('replies once to a tagged video post, as a proper reply with the consent link', async () => {
  const db = makeD1()
  const w = world()
  const p = postView(ALICE, 'v1', tagged())
  w.state.search = [p]
  const { summary, fetchFn } = await run(db, w)
  assert.equal(summary.replied, 1)
  assert.equal(w.state.created.length, 1)
  const body = w.state.created[0]
  assert.equal(body.repo, BOT)
  assert.equal(body.collection, 'app.bsky.feed.post')
  const rec = body.record
  assert.deepEqual(rec.reply, { root: { uri: p.uri, cid: p.cid }, parent: { uri: p.uri, cid: p.cid } })
  assert.deepEqual(rec.langs, ['en'])
  const link = rec.facets[0]
  assert.equal(dec.decode(enc.encode(rec.text).slice(link.index.byteStart, link.index.byteEnd)), 'consentvid.org/app')
  assert.equal(new URL(link.features[0].uri).searchParams.get('post'), p.uri)
  // App-view reads are proxied; the search asked for the tag.
  const search = fetchFn.calls.find((c) => c.key === 'app.bsky.feed.searchPosts')
  assert.equal(search.init.headers['atproto-proxy'], 'did:web:api.bsky.app#bsky_appview')
  assert.deepEqual(search.url.searchParams.getAll('tag'), ['consentvid'])
  assert.equal(search.url.searchParams.get('sort'), 'latest')

  // Next minute: the same post comes back in the overlap window: no 2nd reply.
  const again = await run(db, w, { now: NOW + 60_000 })
  assert.equal(again.summary.replied, 0)
  assert.equal(w.state.created.length, 1)
  const row = await new Store(db).getReplied(p.uri)
  assert.equal(row.status, 'done')
  assert.equal(row.reply_uri, `at://${BOT}/app.bsky.feed.post/r1`)
})

test('ignores search hits without video, without the tag, or by the bot itself', async () => {
  const db = makeD1()
  const w = world()
  w.state.search = [
    postView(ALICE, 'noVideo', tagged({ embed: undefined })),
    postView(ALICE, 'noTag', { ...tagged(), facets: [], text: 'consentvid without tag' }),
    postView(BOT, 'mine', tagged()),
  ]
  const { summary } = await run(db, w)
  assert.equal(summary.candidates, 0)
  assert.equal(w.state.created.length, 0)
})

test('tag via the record tags field and video inside recordWithMedia also count', async () => {
  const db = makeD1()
  const w = world()
  const rec = {
    text: 'clip',
    tags: ['ConsentVid'],
    embed: { $type: 'app.bsky.embed.recordWithMedia', record: { record: {} }, media: videoEmbed() },
  }
  w.state.search = [postView(ALICE, 'rwm', rec)]
  const { summary } = await run(db, w)
  assert.equal(summary.replied, 1)
})

test('skips authors labeled bot (and remembers it)', async () => {
  const db = makeD1()
  const w = world()
  const p = postView(ALICE, 'v1', tagged(), { labels: [{ src: ALICE, uri: `at://${ALICE}/app.bsky.actor.profile/self`, val: 'bot' }] })
  w.state.search = [p]
  const { summary } = await run(db, w)
  assert.equal(summary.replied, 0)
  assert.equal(summary.skipped, 1)
  assert.equal(w.state.created.length, 0)
  assert.equal((await new Store(db).getReplied(p.uri)).status, 'skipped-bot')
})

test('skips videos that already have a ConsentVid consent (bskyPostRef to that post)', async () => {
  const db = makeD1()
  const w = world()
  const p = postView(ALICE, 'v1', tagged())
  w.state.search = [p]
  w.state.consents[ALICE] = [
    {
      uri: `at://${ALICE}/org.consentvid.videoV2/3l`,
      value: { storage: { $type: 'org.consentvid.videoV2#bskyPostRef', post: { uri: p.uri, cid: p.cid }, blob: 'bafkreivideo' } },
    },
  ]
  const { summary, fetchFn } = await run(db, w)
  assert.equal(summary.replied, 0)
  assert.equal(w.state.created.length, 0)
  assert.equal((await new Store(db).getReplied(p.uri)).status, 'skipped-consented')
  // The consent lookup went to the author's own PDS (from the DID document).
  const lr = fetchFn.calls.find((c) => c.key === 'com.atproto.repo.listRecords')
  assert.equal(lr.url.origin, 'https://pds.example')
  assert.equal(lr.url.searchParams.get('collection'), 'org.consentvid.videoV2')
})

test('a mention on a video post links that post', async () => {
  const db = makeD1()
  const w = world()
  const n = { ...postView(BOB, 'm1', { text: '@consentvid.org', embed: videoEmbed(), createdAt: 'x' }), reason: 'mention', indexedAt: '2026-11-08T10:01:00Z' }
  w.state.notifications = [n]
  const { summary } = await run(db, w)
  assert.equal(summary.replied, 1)
  assert.equal(w.state.created[0].record.reply.parent.uri, n.uri)
})

test('a mention in a reply under the author’s own video post links the video post', async () => {
  const db = makeD1()
  const w = world()
  const video = postView(ALICE, 'vid', { text: 'my dance', embed: videoEmbed(), createdAt: 'x' })
  const mention = {
    ...postView(ALICE, 'm2', {
      text: '@consentvid.org',
      reply: { root: { uri: video.uri, cid: video.cid }, parent: { uri: video.uri, cid: video.cid } },
      createdAt: 'x',
    }),
    reason: 'mention',
    indexedAt: '2026-11-08T10:02:00Z',
  }
  w.state.notifications = [mention]
  w.state.getPosts = [video]
  const { summary } = await run(db, w)
  assert.equal(summary.replied, 1)
  const rec = w.state.created[0].record
  assert.equal(rec.reply.parent.uri, mention.uri, 'answers under the mention')
  assert.equal(rec.reply.root.uri, video.uri, 'keeps the thread root')
  assert.equal(new URL(rec.facets[0].features[0].uri).searchParams.get('post'), video.uri, 'links the video')
})

test('a mention under someone else’s video does not link their video', async () => {
  const db = makeD1()
  const w = world()
  const video = postView(ALICE, 'vid', { text: 'alice video', embed: videoEmbed(), createdAt: 'x' })
  const mention = {
    ...postView(BOB, 'm3', { text: '@consentvid.org look', reply: { root: { uri: video.uri, cid: video.cid }, parent: { uri: video.uri, cid: video.cid } }, createdAt: 'x' }),
    reason: 'mention',
    indexedAt: '2026-11-08T10:02:00Z',
  }
  w.state.notifications = [mention]
  w.state.getPosts = [video]
  const { summary, fetchFn } = await run(db, w)
  assert.equal(summary.candidates, 0)
  assert.equal(fetchFn.count('app.bsky.feed.getPosts'), 0)
})

test('notifications already seen are not processed again', async () => {
  const db = makeD1()
  const w = world()
  const n = { ...postView(BOB, 'm1', { text: 'x', embed: videoEmbed(), createdAt: 'x' }), reason: 'mention', indexedAt: '2026-11-08T10:01:00Z' }
  w.state.notifications = [n]
  await run(db, w)
  assert.equal(await new Store(db).getState('notif_seen'), '2026-11-08T10:01:00Z')
  await new Store(db).putReplied(
    { video: { uri: n.uri, cid: n.cid }, target: n, root: n, author: { did: BOB } },
    { status: 'failed', bumpAttempts: true, now: 'x' },
  ) // even if its row changed, the cursor keeps it out of the mention path
  const again = await run(db, w, { now: NOW + 60_000 })
  assert.equal(again.summary.candidates, 1, 'only the retry pass sees it')
})

test('per-author daily limit and per-run cap; unreached candidates are deferred for retry', async () => {
  const db = makeD1()
  const w = world()
  w.state.search = Array.from({ length: 4 }, (_, i) => postView(ALICE, `a${i}`, tagged()))
  w.state.search.push(postView(BOB, 'b0', tagged()), postView(BOB, 'b1', tagged()))
  const { summary } = await run(db, w, { env: { MAX_PER_AUTHOR_PER_DAY: '2', MAX_PER_RUN: '3' } })
  assert.equal(summary.replied, 3)
  const byAuthor = w.state.created.map((c) => c.record.reply.parent.uri.split('/')[2])
  assert.equal(byAuthor.filter((d) => d === ALICE).length, 2)
  const s = new Store(db)
  const deferred = await s.retryable(3, 10)
  assert.ok(deferred.some((r) => r.last_error === 'deferred: run cap'))
})

test('daily global cap stops replies', async () => {
  const db = makeD1()
  const w = world()
  await new Store(db).setState('day:2026-11-08', 300)
  w.state.search = [postView(ALICE, 'v1', tagged())]
  const { summary } = await run(db, w)
  assert.equal(summary.replied, 0)
  assert.equal(w.state.created.length, 0)
})

test('DRY_RUN logs instead of posting, and live mode later still replies', async () => {
  const db = makeD1()
  const w = world()
  const p = postView(ALICE, 'v1', tagged())
  w.state.search = [p]
  const logs = []
  const { summary, fetchFn } = await run(db, w, { env: { DRY_RUN: 'true' }, log: (m) => logs.push(m) })
  assert.equal(summary.dryRun, true)
  assert.equal(fetchFn.count('com.atproto.repo.createRecord'), 0)
  assert.ok(logs.some((l) => l.startsWith('DRY_RUN would reply to')))
  // Dry run again: not logged twice.
  logs.length = 0
  await run(db, w, { env: { DRY_RUN: 'true' }, now: NOW + 60_000, log: (m) => logs.push(m) })
  assert.ok(!logs.some((l) => l.startsWith('DRY_RUN would reply')))
  // Going live: the post is still answered.
  const live = await run(db, w, { now: NOW + 120_000 })
  assert.equal(live.summary.replied, 1)
})

test('DRY_RUN is the default when unset', async () => {
  const db = makeD1()
  const w = world()
  w.state.search = [postView(ALICE, 'v1', tagged())]
  const fetchFn = makeFetch(w.routes)
  const env = ENV(db)
  delete env.DRY_RUN
  const summary = await runOnce(env, { fetchFn, now: NOW, log: quiet })
  assert.equal(summary.dryRun, true)
  assert.equal(w.state.created.length, 0)
})

test('session is cached: createSession once, then reused across runs', async () => {
  const db = makeD1()
  const w = world()
  const a = await run(db, w)
  const b = await run(db, w, { now: NOW + 60_000 })
  assert.equal(a.fetchFn.count('com.atproto.server.createSession'), 1)
  assert.equal(b.fetchFn.count('com.atproto.server.createSession'), 0)
  const call = b.fetchFn.calls.find((c) => c.key === 'app.bsky.feed.searchPosts')
  assert.equal(call.init.headers.authorization, 'Bearer A1')
  assert.equal(call.url.origin, 'https://pds.example', 'calls go to the PDS from the DID document')
})

test('expired access token: refreshSession once, retry, persist the new tokens', async () => {
  const db = makeD1()
  const w = world()
  await run(db, w) // logs in, caches A1/R1
  let first = true
  w.routes['app.bsky.notification.listNotifications'] = (url, init) => {
    if (init.headers.authorization === 'Bearer A1' && first) {
      first = false
      return json(400, { error: 'ExpiredToken', message: 'Token has expired' })
    }
    return json(200, { notifications: [] })
  }
  const { fetchFn, summary } = await run(db, w, { now: NOW + 60_000 })
  assert.equal(summary.error, undefined)
  assert.equal(fetchFn.count('com.atproto.server.refreshSession'), 1)
  const refresh = fetchFn.calls.find((c) => c.key === 'com.atproto.server.refreshSession')
  assert.equal(refresh.init.headers.authorization, 'Bearer R1')
  assert.equal(refresh.init.method, 'POST')
  assert.equal((await new Store(db).getSession()).access_jwt, 'A2')
})

test('refresh failure falls back to createSession, but at most every 10 minutes', async () => {
  const db = makeD1()
  const w = world()
  await run(db, w) // createSession at NOW
  w.routes['app.bsky.notification.listNotifications'] = () => json(401, { error: 'AuthMissing' })
  w.routes['com.atproto.server.refreshSession'] = () => json(400, { error: 'ExpiredToken' })
  const r = await run(db, w, { now: NOW + 60_000 })
  assert.match(r.summary.error, /createSession backoff/)
  assert.equal(r.fetchFn.count('com.atproto.server.createSession'), 0)
})

test('missing secrets are reported, not thrown', async () => {
  const db = makeD1()
  const w = world()
  const summary = await runOnce({ DB: db, DRY_RUN: 'false' }, { fetchFn: makeFetch(w.routes), now: NOW, log: quiet })
  assert.match(summary.error, /BOT_HANDLE/)
})

test('429 pauses the bot until ratelimit-reset and keeps the post for later', async () => {
  const db = makeD1()
  const w = world()
  const p = postView(ALICE, 'v1', tagged())
  w.state.search = [p]
  const reset = Math.floor((NOW + 30 * 60_000) / 1000)
  w.routes['com.atproto.repo.createRecord'] = () => json(429, { error: 'RateLimitExceeded' }, { 'ratelimit-reset': String(reset) })
  const { summary } = await run(db, w)
  assert.equal(summary.paused, true)
  const s = new Store(db)
  assert.equal(Number(await s.getState('paused_until')), reset * 1000)
  const row = await s.getReplied(p.uri)
  assert.equal(row.status, 'failed')
  assert.equal(row.attempts, 0, 'a rate limit does not spend an attempt')
  // While paused: nothing is called at all.
  const paused = await run(db, w, { now: NOW + 60_000 })
  assert.equal(paused.summary.paused, true)
  assert.equal(paused.fetchFn.calls.length, 0)
  // After the reset: the retry pass answers it.
  w.routes['com.atproto.repo.createRecord'] = world({ created: w.state.created }).routes['com.atproto.repo.createRecord']
  const after = await run(db, w, { now: NOW + 31 * 60_000 })
  assert.equal(after.summary.replied, 1)
})

test('one failing post does not stop the run; it is retried up to 3 times', async () => {
  const db = makeD1()
  const w = world()
  const bad = postView(ALICE, 'bad', tagged())
  const good = postView(BOB, 'good', tagged())
  w.state.search = [bad, good]
  w.routes[`plc.directory/${ALICE}`] = () => json(500, { error: 'InternalServerError' })
  const r1 = await run(db, w)
  assert.equal(r1.summary.replied, 1)
  assert.equal(r1.summary.failed, 1)
  const s = new Store(db)
  assert.equal((await s.getReplied(bad.uri)).attempts, 1)
  await run(db, w, { now: NOW + 60_000 })
  await run(db, w, { now: NOW + 120_000 })
  assert.equal((await s.getReplied(bad.uri)).attempts, 3)
  const r4 = await run(db, w, { now: NOW + 180_000 })
  assert.equal(r4.fetchFn.calls.filter((c) => c.key === `plc.directory/${ALICE}`).length, 0, 'gives up after 3 attempts')
})

test('search window overlaps the previous run and the first run looks back a day', async () => {
  const db = makeD1()
  const w = world()
  const a = await run(db, w)
  const since1 = a.fetchFn.calls.find((c) => c.key === 'app.bsky.feed.searchPosts').url.searchParams.get('since')
  assert.equal(since1, new Date(NOW - 24 * 3600_000 - 15 * 60_000).toISOString())
  const b = await run(db, w, { now: NOW + 60_000 })
  const since2 = b.fetchFn.calls.find((c) => c.key === 'app.bsky.feed.searchPosts').url.searchParams.get('since')
  assert.equal(since2, new Date(NOW - 15 * 60_000).toISOString())
})
