// Regenerate tests/fixtures/web_app_vectors.json from test/vector-cases.js.
import { writeFileSync } from 'node:fs'

import { makeVectors } from '../test/vector-cases.js'

const out = new URL('../../tests/fixtures/web_app_vectors.json', import.meta.url)
writeFileSync(out, JSON.stringify(await makeVectors(), null, 2) + '\n')
console.log('wrote tests/fixtures/web_app_vectors.json')
