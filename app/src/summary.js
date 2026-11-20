// The "Review and sign" summary: plain sentences built from the chosen terms.
// Pure (no DOM), so it can be tested. The phrases are for the summary only;
// the checkbox labels live in labels.js.

import { LABELS } from './labels.js'

const MOD = {
  video: 'the picture',
  audio: 'the sound',
  transcript: 'a transcript of the speech',
  face: 'identifiable faces',
  voice: 'identifiable voices',
}
const PURPOSE = { training: 'training AI models', evaluation: 'evaluating models', benchmarking: 'public benchmarks' }
const WHO = { academic: 'academic research', 'open-weights': 'open-weights models', commercial: 'commercial products' }

function joinList(map, values) {
  const words = values.map((v) => map[v] || v)
  return words.length <= 1 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words.at(-1)}`
}

export function summaryLines(terms, n) {
  const expiry = terms.expiresAt ? `until ${terms.expiresAt.slice(0, 10)}` : 'until you withdraw it'
  return [
    `You consent, for ${n} video${n > 1 ? 's' : ''}, that ${joinList(MOD, terms.modalities)} may be used for ` +
      `${joinList(PURPOSE, terms.purposes)}, in ${joinList(WHO, terms.modelClasses)}, ${expiry}.`,
    `Who appears: ${LABELS.peoplePresent[terms.peoplePresent]}.`,
    'Anything you did not tick is not granted. You can withdraw on this page at any time: withdrawing (or ' +
      'deleting the post on Bluesky) takes the video out of datasets that re-check consent before use, and your ' +
      'server then frees an uploaded file. Copies made before that may persist. It does not reach models already trained.',
  ]
}
