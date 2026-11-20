import assert from 'node:assert/strict'
import { test } from 'node:test'

import { summaryLines } from '../src/summary.js'

const terms = {
  modalities: ['video', 'audio', 'face'],
  purposes: ['training', 'evaluation'],
  modelClasses: ['academic', 'open-weights', 'commercial'],
  peoplePresent: 'only-me',
  expiresAt: undefined,
}

test('summary sentence', () => {
  const lines = summaryLines(terms, 1)
  assert.equal(
    lines[0],
    'You consent, for 1 video, that the picture, the sound and identifiable faces may be used for training AI ' +
      'models and evaluating models, in academic research, open-weights models and commercial products, until you ' +
      'withdraw it.',
  )
  assert.equal(lines[1], 'Who appears: Only me.')
  const all = lines.join(' ')
  assert.ok(all.includes('AI'))
  assert.ok(!all.includes(' by '))
  assert.ok(lines[2].endsWith('It does not reach models already trained.'))
})

test('summary with an end date and several videos', () => {
  const lines = summaryLines({ ...terms, modalities: ['video'], expiresAt: '2027-11-09T10:00:00.000Z' }, 3)
  assert.match(lines[0], /^You consent, for 3 videos, that the picture may be used for .*, until 2027-11-09\.$/)
})
