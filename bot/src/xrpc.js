// Minimal XRPC client: plain fetch, a cached session with refresh, and
// rate-limit awareness. No SDK, to stay small and cheap on CPU.

export const APPVIEW_PROXY = 'did:web:api.bsky.app#bsky_appview'

export class XrpcError extends Error {
  constructor(nsid, status, error, message) {
    super(`${nsid}: HTTP ${status} ${error || ''} ${message || ''}`.trim())
    this.status = status
    this.error = error
  }
}

export class RateLimited extends Error {
  constructor(nsid, resetAt) {
    super(`${nsid}: rate limited until ${new Date(resetAt).toISOString()}`)
    this.resetAt = resetAt // epoch ms
  }
}

// redirect: 'manual' — Workers reject 'error'; a 3xx is then !res.ok and fails below.
export async function xrpc(fetchFn, base, nsid, { params, body, post = false, token, proxy, now = Date.now() } = {}) {
  const url = new URL(`/xrpc/${nsid}`, base)
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null) continue
    for (const item of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(item))
  }
  const headers = { accept: 'application/json' }
  if (token) headers.authorization = `Bearer ${token}`
  if (proxy) headers['atproto-proxy'] = proxy
  const init = { method: body === undefined && !post ? 'GET' : 'POST', headers, redirect: 'manual' }
  if (body !== undefined) {
    headers['content-type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  const res = await fetchFn(url.toString(), init)
  if (res.status === 429) {
    const reset = Number(res.headers.get('ratelimit-reset'))
    throw new RateLimited(nsid, Number.isFinite(reset) && reset > 0 ? reset * 1000 : now + 60_000)
  }
  let data = null
  const text = await res.text()
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!res.ok) throw new XrpcError(nsid, res.status, data?.error, data?.message)
  return data
}

// The account's PDS from its DID document (plc.directory or did:web).
export async function resolvePds(fetchFn, did) {
  let docUrl
  if (did.startsWith('did:plc:')) docUrl = `https://plc.directory/${did}`
  else if (did.startsWith('did:web:')) {
    const host = did.slice('did:web:'.length)
    if (!/^[a-z0-9.-]+$/i.test(host)) throw new Error(`unsupported did:web ${did}`)
    docUrl = `https://${host}/.well-known/did.json`
  } else throw new Error(`unsupported DID method: ${did}`)
  const res = await fetchFn(docUrl, { headers: { accept: 'application/json' }, redirect: 'manual' })
  if (!res.ok) throw new Error(`DID document for ${did}: HTTP ${res.status}`)
  return pdsFromDidDoc(await res.json())
}

export function pdsFromDidDoc(doc) {
  const svc = (doc?.service || []).find(
    (s) => (s.id === '#atproto_pds' || s.id === `${doc.id}#atproto_pds`) && s.type === 'AtprotoPersonalDataServer',
  )
  const ep = svc?.serviceEndpoint
  if (typeof ep !== 'string') return null
  const u = new URL(ep)
  if (u.protocol !== 'https:') return null
  return u.origin
}

const CREATE_SESSION_BACKOFF_MS = 10 * 60_000

// Authenticated calls as the bot. Keeps the session in D1 so the bot logs in
// rarely: access token -> refreshSession -> (at most every 10 min) createSession.
export class BotAgent {
  constructor({ env, store, fetchFn, now }) {
    this.env = env
    this.store = store
    this.fetch = fetchFn
    this.now = now
    this.session = null
  }

  get did() {
    return this.session?.did
  }

  async ensure() {
    if (this.session) return this.session
    this.session = await this.store.getSession()
    if (!this.session) this.session = await this.#create()
    return this.session
  }

  async #create() {
    const last = Number((await this.store.getState('last_create_session')) || 0)
    if (this.now - last < CREATE_SESSION_BACKOFF_MS) {
      throw new Error('createSession backoff: last attempt was under 10 minutes ago')
    }
    await this.store.setState('last_create_session', this.now)
    if (!this.env.BOT_HANDLE || !this.env.BOT_APP_PASSWORD) {
      throw new Error('BOT_HANDLE / BOT_APP_PASSWORD secrets are not set')
    }
    const entry = this.env.BOT_ENTRYWAY || 'https://bsky.social'
    const r = await xrpc(this.fetch, entry, 'com.atproto.server.createSession', {
      body: { identifier: this.env.BOT_HANDLE, password: this.env.BOT_APP_PASSWORD },
      now: this.now,
    })
    return this.#save(r, pdsFromDidDoc(r.didDoc) || new URL(entry).origin)
  }

  async #save(r, pds) {
    const s = { did: r.did, handle: r.handle, pds, access_jwt: r.accessJwt, refresh_jwt: r.refreshJwt }
    await this.store.putSession(s, new Date(this.now).toISOString())
    return s
  }

  async #refresh() {
    try {
      const r = await xrpc(this.fetch, this.session.pds, 'com.atproto.server.refreshSession', {
        post: true,
        token: this.session.refresh_jwt,
        now: this.now,
      })
      this.session = await this.#save(r, pdsFromDidDoc(r.didDoc) || this.session.pds)
    } catch (e) {
      if (e instanceof RateLimited) throw e
      await this.store.clearSession()
      this.session = await this.#create()
    }
  }

  // One authenticated call; on an expired/invalid token refresh once and retry.
  async call(nsid, { params, body, appview = false } = {}) {
    await this.ensure()
    const opts = () => ({
      params,
      body,
      token: this.session.access_jwt,
      proxy: appview ? APPVIEW_PROXY : undefined,
      now: this.now,
    })
    try {
      return await xrpc(this.fetch, this.session.pds, nsid, opts())
    } catch (e) {
      const expired =
        e instanceof XrpcError &&
        (e.status === 401 || (e.status === 400 && ['ExpiredToken', 'InvalidToken'].includes(e.error)))
      if (!expired) throw e
      await this.#refresh()
      return xrpc(this.fetch, this.session.pds, nsid, opts())
    }
  }
}
