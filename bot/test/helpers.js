// Test doubles: a D1-compatible database on node:sqlite (so the real
// migration and SQL run), and a routing fetch stub.
import { readFileSync } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'

export function makeD1() {
  const db = new DatabaseSync(':memory:')
  db.exec(readFileSync(new URL('../migrations/0001_init.sql', import.meta.url), 'utf8'))
  const norm = (args) => args.map((a) => (typeof a === 'boolean' ? Number(a) : a))
  return {
    raw: db,
    prepare(sql) {
      const stmt = db.prepare(sql)
      let args = []
      const api = {
        bind(...a) {
          args = norm(a)
          return api
        },
        async first() {
          const row = stmt.get(...args)
          return row ? { ...row } : null
        },
        async run() {
          stmt.run(...args)
          return { success: true }
        },
        async all() {
          return { results: stmt.all(...args).map((r) => ({ ...r })) }
        },
      }
      return api
    },
  }
}

export function json(status, body, headers = {}) {
  return new Response(body === undefined ? '' : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  })
}

// routes: { 'nsid-or-host-path': (url, init) => Response }. Records calls.
export function makeFetch(routes) {
  const calls = []
  const fn = async (input, init = {}) => {
    const url = new URL(input)
    const key = url.pathname.startsWith('/xrpc/') ? url.pathname.slice(6) : `${url.host}${url.pathname}`
    calls.push({ key, url, init, body: init.body ? JSON.parse(init.body) : undefined })
    const h = routes[key]
    if (!h) return json(404, { error: 'NotFound', message: `no stub for ${key}` })
    return h(url, init)
  }
  fn.calls = calls
  fn.count = (key) => calls.filter((c) => c.key === key).length
  return fn
}

export const BOT = 'did:plc:botbotbotbotbotbotbotbotb'
export const ALICE = 'did:plc:aliceaaaaaaaaaaaaaaaaaaa'
export const BOB = 'did:plc:bobbbbbbbbbbbbbbbbbbbbbb'

export const videoEmbed = (cid = 'bafkreivideo') => ({
  $type: 'app.bsky.embed.video',
  video: { $type: 'blob', ref: { $link: cid }, mimeType: 'video/mp4', size: 1000 },
})

export const tagged = (extra = {}) => ({
  $type: 'app.bsky.feed.post',
  text: 'my clip #consentvid',
  facets: [{ index: { byteStart: 8, byteEnd: 19 }, features: [{ $type: 'app.bsky.richtext.facet#tag', tag: 'consentvid' }] }],
  embed: videoEmbed(),
  createdAt: '2026-11-08T10:00:00Z',
  ...extra,
})

export function postView(did, rkey, record, { labels = [], indexedAt = '2026-11-08T10:00:00Z' } = {}) {
  return {
    uri: `at://${did}/app.bsky.feed.post/${rkey}`,
    cid: `bafyrei${rkey}`,
    author: { did, handle: `${rkey}.test`, labels },
    record,
    indexedAt,
  }
}

export const didDoc = (did, pds = 'https://pds.example') => ({
  id: did,
  service: [{ id: '#atproto_pds', type: 'AtprotoPersonalDataServer', serviceEndpoint: pds }],
})

// A world with the bot logged in and empty search/notifications/consents.
export function world(over = {}) {
  const state = { search: [], notifications: [], consents: {}, profiles: {}, getPosts: [], created: [] }
  Object.assign(state, over)
  const routes = {
    'com.atproto.server.createSession': () =>
      json(200, { did: BOT, handle: 'consentvid.org', accessJwt: 'A1', refreshJwt: 'R1', didDoc: didDoc(BOT) }),
    'com.atproto.server.refreshSession': () =>
      json(200, { did: BOT, handle: 'consentvid.org', accessJwt: 'A2', refreshJwt: 'R2', didDoc: didDoc(BOT) }),
    'app.bsky.feed.searchPosts': () => json(200, { posts: state.search }),
    'app.bsky.notification.listNotifications': () => json(200, { notifications: state.notifications }),
    'app.bsky.feed.getPosts': () => json(200, { posts: state.getPosts }),
    'app.bsky.actor.getProfile': (url) => json(200, { did: url.searchParams.get('actor'), labels: state.profiles[url.searchParams.get('actor')] || [] }),
    'com.atproto.repo.listRecords': (url) => json(200, { records: state.consents[url.searchParams.get('repo')] || [] }),
    'com.atproto.repo.createRecord': (url, init) => {
      const n = state.created.push(JSON.parse(init.body))
      return json(200, { uri: `at://${BOT}/app.bsky.feed.post/r${n}`, cid: `bafyreply${n}` })
    },
  }
  for (const d of [ALICE, BOB]) routes[`plc.directory/${d}`] = () => json(200, didDoc(d, 'https://pds.example'))
  return { state, routes }
}

export const ENV = (db, extra = {}) => ({
  DB: db,
  BOT_HANDLE: 'consentvid.org',
  BOT_APP_PASSWORD: 'test-app-password',
  DRY_RUN: 'false',
  APP_URL: 'https://consentvid.org/app/',
  ...extra,
})

export const NOW = Date.parse('2026-11-08T10:05:00Z')
export const quiet = () => {}
