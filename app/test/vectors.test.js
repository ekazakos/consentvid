import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import { FALLBACK_SCOPE, SCOPE } from '../src/scopes.js'
import { makeVectors } from './vector-cases.js'

const read = (rel) => JSON.parse(readFileSync(new URL(rel, import.meta.url), 'utf8'))

test('tests/fixtures/web_app_vectors.json is current (npm run vectors)', async () => {
  assert.deepEqual(read('../../tests/fixtures/web_app_vectors.json'), JSON.parse(JSON.stringify(await makeVectors())))
})

test('client metadata matches the app', () => {
  const meta = read('../../web/app/client-metadata.json')
  assert.equal(meta.client_id, 'https://consentvid.org/app/client-metadata.json')
  assert.deepEqual(meta.redirect_uris, ['https://consentvid.org/app/'])
  const listed = new Set(meta.scope.split(' '))
  for (const s of (SCOPE + ' ' + FALLBACK_SCOPE).split(' ')) assert.ok(listed.has(s), s)
  assert.equal(meta.dpop_bound_access_tokens, true)
  assert.equal(meta.token_endpoint_auth_method, 'none')
  assert.equal(meta.application_type, 'web')
  assert.deepEqual(meta.grant_types, ['authorization_code', 'refresh_token'])
  assert.equal(new URL(meta.client_uri).hostname, new URL(meta.client_id).hostname)
})
