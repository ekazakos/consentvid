import assert from 'node:assert/strict'
import { test } from 'node:test'

import { DESCRIPTION, withBotProfile } from '../scripts/setup.mjs'

test('adds the bot self-label and keeps every existing field', () => {
  const avatar = { $type: 'blob', ref: { $link: 'bafkreiavatar' }, mimeType: 'image/png', size: 10 }
  const existing = {
    $type: 'app.bsky.actor.profile',
    displayName: 'ConsentVid',
    description: 'custom text',
    avatar,
    labels: { $type: 'com.atproto.label.defs#selfLabels', values: [{ val: 'nudity' }] },
  }
  const p = withBotProfile(existing)
  assert.equal(p.displayName, 'ConsentVid')
  assert.equal(p.description, 'custom text')
  assert.deepEqual(p.avatar, avatar)
  assert.deepEqual(p.labels.values, [{ val: 'nudity' }, { val: 'bot' }])
  assert.equal(p.labels.$type, 'com.atproto.label.defs#selfLabels')
})

test('is idempotent and fills in a description for a new profile', () => {
  const p = withBotProfile(withBotProfile(null))
  assert.deepEqual(p.labels.values, [{ val: 'bot' }])
  assert.equal(p.description, DESCRIPTION)
  assert.match(p.description, /bot/i)
  assert.equal(p.$type, 'app.bsky.actor.profile')
})
