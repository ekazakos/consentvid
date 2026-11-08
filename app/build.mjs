// Bundles src/main.js into web/app/app.js (served at https://consentvid.org/app/).
// Reproducible: exact dependency versions from package-lock.json (`npm ci`),
// a pinned esbuild, no timestamps or absolute paths in the output.
import { build } from 'esbuild'

await build({
  entryPoints: ['src/main.js'],
  outfile: '../web/app/app.js',
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: ['es2022'],
  minify: true,
  sourcemap: false,
  legalComments: 'eof',
  charset: 'utf8',
  logLevel: 'info',
})
