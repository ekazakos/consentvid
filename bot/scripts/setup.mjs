#!/usr/bin/env node
// One-time profile setup for the bot account (run manually, locally):
//   BOT_HANDLE=consentvid.org BOT_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx npm run setup-profile
// Adds the `bot` self-label (atproto.com/guides/bot-tutorial) and an
// "automated account" description, keeping every other profile field.
// Pass --dry-run to print the new profile without writing it.

import { pdsFromDidDoc, xrpc } from '../src/xrpc.js'

export const DESCRIPTION =
  'Automated account (bot) for ConsentVid, a consent-first open video dataset. ' +
  'I only reply when you tag a video post #consentvid or mention me. https://consentvid.org'

export function withBotProfile(existing) {
  const profile = { ...(existing || {}), $type: 'app.bsky.actor.profile' }
  const values = (profile.labels?.values || []).filter((v) => v.val !== 'bot')
  profile.labels = { $type: 'com.atproto.label.defs#selfLabels', values: [...values, { val: 'bot' }] }
  if (!profile.description) profile.description = DESCRIPTION
  if (!profile.displayName) profile.displayName = 'ConsentVid bot'
  return profile
}

async function main() {
  const dry = process.argv.includes('--dry-run')
  const { BOT_HANDLE, BOT_APP_PASSWORD } = process.env
  const entry = process.env.BOT_ENTRYWAY || 'https://bsky.social'
  if (!BOT_HANDLE || !BOT_APP_PASSWORD) throw new Error('set BOT_HANDLE and BOT_APP_PASSWORD')
  const s = await xrpc(fetch, entry, 'com.atproto.server.createSession', {
    body: { identifier: BOT_HANDLE, password: BOT_APP_PASSWORD },
  })
  const pds = pdsFromDidDoc(s.didDoc) || entry
  let existing = null
  let swapRecord
  try {
    const r = await xrpc(fetch, pds, 'com.atproto.repo.getRecord', {
      params: { repo: s.did, collection: 'app.bsky.actor.profile', rkey: 'self' },
    })
    existing = r.value
    swapRecord = r.cid
  } catch (e) {
    if (e.status !== 400) throw e // 400 RecordNotFound: no profile yet
  }
  const profile = withBotProfile(existing)
  if (dry) {
    console.log(JSON.stringify(profile, null, 2))
    return
  }
  await xrpc(fetch, pds, 'com.atproto.repo.putRecord', {
    token: s.accessJwt,
    body: { repo: s.did, collection: 'app.bsky.actor.profile', rkey: 'self', record: profile, swapRecord: swapRecord ?? null },
  })
  console.log(`profile of ${s.handle} (${s.did}) now carries the bot self-label`)
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => {
    console.error(e.message)
    process.exit(1)
  })
}
