// One bot run (called every minute by the cron trigger).
//
// 1. Mentions of the bot (listNotifications) and posts tagged #consentvid
//    (searchPosts) since the last run become candidates, but only when they
//    point at a video post by the same author.
// 2. Each video gets at most one reply, ever (D1 `replied`), and none when the
//    author is a labeled bot, already has a ConsentVid record for that post,
//    or hit the per-author daily limit.
// 3. DRY_RUN=true logs the reply instead of posting it.

import { BotAgent, RateLimited, resolvePds, xrpc } from './xrpc.js'
import { buildReply, didOfUri, hasTag, isBotAuthor, rootFor, TAG, videoOf } from './post.js'
import { Store } from './store.js'

const SEARCH_OVERLAP_MS = 15 * 60_000 // re-search a window: the index can lag
const FIRST_RUN_LOOKBACK_MS = 24 * 3600_000
const MAX_ATTEMPTS = 3
const PAGE = 25
const MAX_PAGES = 2
const VIDEO_V2 = 'org.consentvid.videoV2'
const POST_REF = 'org.consentvid.videoV2#bskyPostRef'

const iso = (ms) => new Date(ms).toISOString()
const intEnv = (v, d) => (Number.isFinite(Number(v)) && Number(v) >= 0 ? Number(v) : d)

export async function runOnce(env, { fetchFn = fetch, now = Date.now(), log = console.log } = {}) {
  const store = new Store(env.DB)
  const dryRun = String(env.DRY_RUN ?? 'true').toLowerCase() !== 'false'
  const limits = {
    run: intEnv(env.MAX_PER_RUN, 20),
    day: intEnv(env.MAX_PER_DAY, 300),
    author: intEnv(env.MAX_PER_AUTHOR_PER_DAY, 5),
  }
  const appUrl = env.APP_URL || 'https://consentvid.org/app/'
  const summary = { dryRun, candidates: 0, replied: 0, skipped: 0, failed: 0, paused: false }

  const pausedUntil = Number((await store.getState('paused_until')) || 0)
  if (pausedUntil > now) {
    summary.paused = true
    log(`paused by rate limit until ${iso(pausedUntil)}`)
    return summary
  }

  const agent = new BotAgent({ env, store, fetchFn, now })
  try {
    await agent.ensure()
    const candidates = [...(await fromRetries(store)), ...(await fromMentions(agent, store, now)), ...(await fromSearch(agent, store, now))]
    const seen = new Set()
    const unique = candidates.filter((c) => !seen.has(c.video.uri) && seen.add(c.video.uri))
    summary.candidates = unique.length
    await handle(unique, { agent, store, fetchFn, now, dryRun, limits, appUrl, log, summary })
  } catch (e) {
    if (e instanceof RateLimited) {
      await store.setState('paused_until', e.resetAt)
      summary.paused = true
      log(`rate limited: ${e.message}`)
    } else {
      summary.error = String(e?.message || e)
      log(`run failed: ${summary.error}`)
    }
  }
  log(`run: ${JSON.stringify(summary)}`)
  return summary
}

// Candidate: { video: {uri, cid}, target: {uri, cid}, root: {uri, cid}, author: {did, labels}, retry? }
function candidate(videoPost, target, author) {
  return {
    video: { uri: videoPost.uri, cid: videoPost.cid },
    target: { uri: target.uri, cid: target.cid },
    root: rootFor(target),
    author: { did: author.did, labels: author.labels || [] },
  }
}

async function fromRetries(store) {
  const rows = await store.retryable(MAX_ATTEMPTS, 10)
  return rows.map((r) => ({
    video: { uri: r.video_uri, cid: r.video_cid },
    target: { uri: r.target_uri, cid: r.target_cid },
    root: { uri: r.root_uri, cid: r.root_cid },
    author: { did: r.author_did, labels: null }, // labels re-read below
    retry: true,
  }))
}

async function fromMentions(agent, store, now) {
  const seenAt = (await store.getState('notif_seen')) || iso(now - FIRST_RUN_LOOKBACK_MS)
  const fresh = []
  let cursor
  for (let page = 0; page < MAX_PAGES; page++) {
    const r = await agent.call('app.bsky.notification.listNotifications', {
      params: { reasons: ['mention'], limit: PAGE, cursor },
      appview: true,
    })
    const items = r?.notifications || []
    const newer = items.filter((n) => n.indexedAt > seenAt)
    fresh.push(...newer)
    cursor = r?.cursor
    if (!cursor || newer.length < items.length || items.length === 0) break
  }
  const newest = fresh.reduce((m, n) => (n.indexedAt > m ? n.indexedAt : m), seenAt)

  const out = []
  const parents = new Map() // uri -> mention needing its parent/root looked up
  for (const n of fresh) {
    if (n.reason !== 'mention' || n.author?.did === agent.did) continue
    if (videoOf(n.record)) {
      out.push(candidate(n, n, n.author))
      continue
    }
    // "@bot" in a reply under the author's own video post: link that post.
    for (const ref of [n.record?.reply?.parent, n.record?.reply?.root]) {
      if (ref?.uri && didOfUri(ref.uri) === n.author.did) {
        if (!parents.has(ref.uri)) parents.set(ref.uri, [])
        parents.get(ref.uri).push(n)
        break
      }
    }
  }
  if (parents.size) {
    const uris = [...parents.keys()].slice(0, PAGE)
    const r = await agent.call('app.bsky.feed.getPosts', { params: { uris }, appview: true })
    for (const p of r?.posts || []) {
      if (!videoOf(p.record)) continue
      for (const n of parents.get(p.uri) || []) {
        if (p.author?.did === n.author.did) out.push(candidate(p, n, n.author))
      }
    }
  }
  await store.setState('notif_seen', newest)
  return out
}

async function fromSearch(agent, store, now) {
  const last = Number((await store.getState('search_at')) || now - FIRST_RUN_LOOKBACK_MS)
  const since = iso(last - SEARCH_OVERLAP_MS)
  const out = []
  let cursor
  for (let page = 0; page < MAX_PAGES; page++) {
    const r = await agent.call('app.bsky.feed.searchPosts', {
      params: { q: `#${TAG}`, tag: [TAG], sort: 'latest', since, limit: PAGE, cursor },
      appview: true,
    })
    for (const p of r?.posts || []) {
      if (p.author?.did === agent.did) continue
      if (videoOf(p.record) && hasTag(p.record)) out.push(candidate(p, p, p.author))
    }
    cursor = r?.cursor
    if (!cursor || (r?.posts || []).length < PAGE) break
  }
  await store.setState('search_at', now)
  return out
}

// URIs of the posts this author already contributed through ConsentVid.
async function consentedPosts(fetchFn, did, cache) {
  if (cache.has(did)) return cache.get(did)
  const pds = await resolvePds(fetchFn, did)
  if (!pds) throw new Error(`no PDS in the DID document of ${did}`)
  const posts = new Set()
  let cursor
  for (let page = 0; page < 4; page++) {
    const r = await xrpc(fetchFn, pds, 'com.atproto.repo.listRecords', {
      params: { repo: did, collection: VIDEO_V2, limit: 100, cursor },
    })
    for (const rec of r?.records || []) {
      const s = rec.value?.storage
      if (s?.$type === POST_REF && typeof s.post?.uri === 'string') posts.add(s.post.uri)
    }
    cursor = r?.cursor
    if (!cursor || (r?.records || []).length < 100) break
  }
  cache.set(did, posts)
  return posts
}

async function handle(cands, ctx) {
  const { agent, store, fetchFn, now, dryRun, limits, appUrl, log, summary } = ctx
  const day = iso(now).slice(0, 10)
  const dayKey = `day:${day}`
  let dayCount = Number((await store.getState(dayKey)) || 0)
  const consentCache = new Map()
  const labelCache = new Map()

  const nowIso = iso(now)
  // Candidates not reached this run (cap or rate limit) are kept for the
  // retry pass: the mention cursor has already moved past them.
  const defer = async (rest, why) => {
    for (const c of rest) {
      if (c.retry || c.author.did === agent.did) continue
      const prev = await store.getReplied(c.video.uri)
      if (!prev || (dryRun ? false : prev.status === 'dry-run')) {
        await store.putReplied(c, { status: 'failed', error: `deferred: ${why}`, now: nowIso })
      }
    }
  }

  for (let i = 0; i < cands.length; i++) {
    const c = cands[i]
    if (summary.replied >= limits.run || dayCount >= limits.day) {
      await defer(cands.slice(i), summary.replied >= limits.run ? 'run cap' : 'daily cap')
      break
    }
    if (c.author.did === agent.did) continue
    const prev = await store.getReplied(c.video.uri)
    if (prev) {
      const final = prev.status === 'done' || prev.status.startsWith('skipped')
      const exhausted = prev.status === 'failed' && prev.attempts >= MAX_ATTEMPTS
      if (final || exhausted || (dryRun && prev.status === 'dry-run')) continue
      if (prev.status === 'failed' && !c.retry) continue // the retry pass owns it
    }
    try {
      if (c.author.labels === null) c.author.labels = await authorLabels(agent, c.author.did, labelCache)
      if (isBotAuthor(c.author)) {
        await store.putReplied(c, { status: 'skipped-bot', now: nowIso })
        summary.skipped++
        continue
      }
      if ((await store.authorCount(c.author.did, day)) >= limits.author) {
        summary.skipped++
        continue
      }
      if ((await consentedPosts(fetchFn, c.author.did, consentCache)).has(c.video.uri)) {
        await store.putReplied(c, { status: 'skipped-consented', now: nowIso })
        summary.skipped++
        continue
      }
      const reply = buildReply(appUrl, c.video.uri)
      if (dryRun) {
        log(`DRY_RUN would reply to ${c.target.uri} (video ${c.video.uri}): ${reply.text}`)
        await store.putReplied(c, { status: 'dry-run', now: nowIso })
        summary.replied++
        continue
      }
      const r = await agent.call('com.atproto.repo.createRecord', {
        body: {
          repo: agent.did,
          collection: 'app.bsky.feed.post',
          record: {
            $type: 'app.bsky.feed.post',
            text: reply.text,
            facets: reply.facets,
            langs: ['en'],
            reply: { root: c.root, parent: c.target },
            createdAt: nowIso,
          },
        },
      })
      await store.putReplied(c, { status: 'done', replyUri: r?.uri || null, now: nowIso })
      await store.bumpAuthor(c.author.did, day)
      dayCount++
      await store.setState(dayKey, dayCount)
      summary.replied++
      log(`replied to ${c.target.uri}: ${r?.uri}`)
    } catch (e) {
      if (e instanceof RateLimited) {
        // Not this post's fault: keep it for later without spending an attempt.
        await store.putReplied(c, { status: 'failed', error: e.message, now: nowIso })
        await defer(cands.slice(i + 1), 'rate limited')
        throw e
      }
      await store.putReplied(c, { status: 'failed', error: String(e?.message || e).slice(0, 500), bumpAttempts: true, now: nowIso })
      summary.failed++
      log(`failed on ${c.video.uri}: ${e?.message || e}`)
    }
  }
  await store.pruneAuthorDays(iso(now - 3 * 86400_000).slice(0, 10))
}

async function authorLabels(agent, did, cache) {
  if (!cache.has(did)) {
    const p = await agent.call('app.bsky.actor.getProfile', { params: { actor: did }, appview: true })
    cache.set(did, p?.labels || [])
  }
  return cache.get(did)
}
