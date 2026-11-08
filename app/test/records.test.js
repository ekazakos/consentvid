import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  PDS_BLOB_MAX_SIZE,
  TID_RE,
  buildContribution,
  buildUploadContribution,
  cleanLine,
  consentRecord,
  defaultTitle,
  encodeTid,
  expiryFrom,
  isRawSha256Cid,
  mimeForName,
  newTid,
  parsePostParam,
  postLangs,
  rawCid,
  recordCid,
  sameContainer,
  termsErrors,
  toIpld,
  uploadVideoRecord,
  videoBlobOf,
  videoErrors,
  videoRecord,
  withdrawBody,
} from '../src/records.js'

const DID = 'did:plc:aliceaaaaaaaaaaaaaaaaaaa'
const BLOB = 'bafkreifwkq2hesfskgekpkjbmih2ityaslss3rvd5wsmdytyl2pkdjxu7m'
const POST_URI = 'at://' + DID + '/app.bsky.feed.post/3mzrrcdrdgs2f'
const POST_CID = 'bafyreicamp74xmd5e5riqch6ocgwqb2yfs63zi4ukovfukazbawioueuci'
const videoEmbed = {
  $type: 'app.bsky.embed.video',
  video: { $type: 'blob', ref: { $link: BLOB }, mimeType: 'video/mp4', size: 5 },
}
const TERMS = {
  modalities: ['video'],
  purposes: ['training'],
  modelClasses: ['academic'],
  peoplePresent: 'only-me',
  authorshipAttested: true,
  adultAttested: true,
}

test('TIDs are well-formed, sortable and monotonic', () => {
  const a = newTid(1_800_000_000_000, 5)
  const b = newTid(1_800_000_000_000, 5) // same instant: still strictly later
  assert.match(a, TID_RE)
  assert.match(b, TID_RE)
  assert.ok(b > a)
  assert.equal(encodeTid(0), '2222222222222')
})

test('CIDs match the Python implementation (known values)', async () => {
  // consentvid.records.compute_cid({"$type": "x", "a": 1}) and raw_cid_from_sha256(sha256(b"hello"))
  assert.equal(await recordCid({ $type: 'x', a: 1 }), 'bafyreifhkvxoj7i3cvhtcaye7r7lgne7kgznjw5myvqjxs6hamzpnwwppm')
  assert.equal(await rawCid(new TextEncoder().encode('hello')), 'bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq')
  assert.ok(isRawSha256Cid(BLOB))
  assert.ok(!isRawSha256Cid(POST_CID))
  assert.ok(!isRawSha256Cid('not a cid'))
})

test('video blob of a post', () => {
  assert.deepEqual(videoBlobOf({ embed: videoEmbed }), { cid: BLOB, mimeType: 'video/mp4', size: 5 })
  assert.equal(videoBlobOf({ embed: { $type: 'app.bsky.embed.recordWithMedia', media: videoEmbed } }).cid, BLOB)
  assert.equal(videoBlobOf({ embed: { $type: 'app.bsky.embed.images', images: [] } }), null)
  assert.equal(videoBlobOf({}), null)
  assert.equal(videoBlobOf(null), null)
})

test('deep link parameter (?post=<at-uri>)', () => {
  assert.deepEqual(parsePostParam(POST_URI), { did: DID, collection: 'app.bsky.feed.post', rkey: '3mzrrcdrdgs2f' })
  assert.equal(parsePostParam('at://' + DID + '/app.bsky.feed.like/3mzrrcdrdgs2f'), null)
  assert.equal(parsePostParam('https://bsky.app/profile/x/post/y'), null)
  assert.equal(parsePostParam('at://alice.example.com/app.bsky.feed.post/3mzrrcdrdgs2f'), null) // DID form only
  assert.equal(parsePostParam(null), null)
})

test('titles and text cleaning', () => {
  assert.equal(defaultTitle({ text: '\n  First   line ‮\nsecond' }), 'First line')
  assert.equal(defaultTitle({ text: '' }), 'Bluesky video')
  assert.equal(defaultTitle({ text: 'x'.repeat(400) }).length, 300)
  assert.ok(new TextEncoder().encode(defaultTitle({ text: '👩‍👩‍👧‍👦'.repeat(300) })).length <= 3000)
  assert.equal(cleanLine(' a ​ b\t'), 'a b')
  assert.deepEqual(postLangs({ langs: ['en', 'EN', 'pt-BR', 'xx_yy', 'cs', 'de'] }), ['en', 'pt-BR', 'cs'])
})

test('video record', () => {
  const rec = videoRecord({
    postUri: POST_URI,
    postCid: POST_CID,
    post: { text: 'Hi', embed: videoEmbed },
    labels: ['porn', 'bogus'],
    tags: ['a', 'a', ' '],
    probe: { size: 5, width: 0, height: -1, durationMs: 1.5 },
    createdAt: '2026-11-08T07:00:00.000Z',
  })
  assert.equal(rec.contentHash, BLOB)
  assert.equal(rec.storage.blob, BLOB)
  assert.deepEqual(rec.storage.post, { uri: POST_URI, cid: POST_CID })
  assert.equal(rec.title, 'Hi')
  assert.deepEqual(rec.labels, { $type: 'com.atproto.label.defs#selfLabels', values: [{ val: 'porn' }] })
  assert.deepEqual(rec.tags, ['a'])
  assert.equal(rec.size, 5)
  assert.equal(rec.width, 0)
  assert.ok(!('height' in rec) && !('durationMs' in rec))
  assert.throws(() => videoRecord({ post: { text: 'no video' } }), /no video/)
})

test('video detail validation', () => {
  assert.deepEqual(videoErrors({ title: 'ok', tags: ['a'] }), [])
  assert.ok(videoErrors({ title: 'x'.repeat(301) }).length)
  assert.ok(videoErrors({ tags: Array.from({ length: 9 }, (_, i) => 't' + i) }).length)
  assert.ok(videoErrors({ tags: ['x'.repeat(65)] }).length)
  assert.ok(videoErrors({ description: 'a\u0007b' }).length)
})

test('terms validation mirrors the CLI', () => {
  assert.deepEqual(termsErrors(TERMS), [])
  assert.ok(termsErrors({ ...TERMS, authorshipAttested: false }).length)
  assert.ok(termsErrors({ ...TERMS, adultAttested: undefined }).length)
  assert.ok(termsErrors({ ...TERMS, purposes: [] }).length)
  assert.ok(termsErrors({ ...TERMS, peoplePresent: undefined }).length)
  assert.ok(termsErrors({ ...TERMS, modelClasses: ['academic', 'bogus'] }).length)
  assert.ok(termsErrors({ ...TERMS, captureProvenance: 'attested' }).length)
  assert.ok(termsErrors({ ...TERMS, peoplePresent: 'no-identifiable-people', modalities: ['video', 'face'] }).length)
  assert.ok(termsErrors({ ...TERMS, expiresAt: '2000-01-01T00:00:00Z' }).length)
})

test('consent record defaults', () => {
  const c = consentRecord({ videoUri: 'at://x/y/z', videoCid: 'c', terms: TERMS, createdAt: 't' })
  assert.equal(c.compensation, 'none')
  assert.equal(c.rightsBasis, 'own-recording')
  assert.equal(c.captureProvenance, 'self-attested')
  assert.equal(c.authorshipAttested, true)
  assert.ok(!('expiresAt' in c) && !('revocable' in c))
})

test('one applyWrites commit: video + sidecar consent pinned to its CID', async () => {
  const c = await buildContribution({
    did: DID,
    rkey: '3mzrrcdrdgs2a',
    postUri: POST_URI,
    postCid: POST_CID,
    post: { text: 'Hi', embed: videoEmbed },
    video: {},
    terms: TERMS,
    createdAt: '2026-11-08T07:00:00.000Z',
  })
  const [v, k] = c.body.writes
  assert.equal(v.rkey, k.rkey)
  assert.equal(v.collection, 'org.consentvid.videoV2')
  assert.equal(k.collection, 'org.consentvid.consentV2')
  assert.equal(k.value.subject.cid, await recordCid(v.value))
  assert.equal(k.value.subject.uri, c.videoUri)
  assert.deepEqual(
    withdrawBody(DID, 'r', { videoExists: true, consentExists: false }).writes.map((w) => w.collection),
    ['org.consentvid.videoV2'],
  )
})

test('expiry choices', () => {
  const now = new Date('2026-11-08T07:00:00.000Z')
  assert.equal(expiryFrom('', now), undefined)
  assert.equal(expiryFrom('2y', now), '2028-11-08T07:00:00.000Z')
})

// ---------------------------------------------------- uploads (PDS blob) --

const HELLO = 'bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq'
const upload = (extra = {}) => ({
  title: 'Kitchen',
  mimeType: 'video/mp4',
  blob: { cid: HELLO, mimeType: 'video/mp4', size: 5 },
  createdAt: '2026-11-08T07:00:00.000Z',
  ...extra,
})

test('upload record: a blob ref in the user’s own PDS', async () => {
  const rec = uploadVideoRecord(upload())
  assert.deepEqual(rec.storage, {
    $type: 'org.consentvid.videoV2#pdsBlobRef',
    blob: { $type: 'blob', ref: { $link: HELLO }, mimeType: 'video/mp4', size: 5 },
  })
  assert.equal(rec.contentHash, HELLO)
  assert.equal(rec.size, 5)
  assert.equal(await rawCid(new TextEncoder().encode('hello')), HELLO)
  assert.equal(PDS_BLOB_MAX_SIZE, 300_000_000)
})

test('upload record refusals', () => {
  assert.throws(() => uploadVideoRecord(upload({ title: ' ​ ' })), /title is required/)
  assert.throws(
    () => uploadVideoRecord(upload({ blob: { cid: HELLO, mimeType: 'video/webm', size: 5 } })),
    /identified the file/,
  )
  assert.throws(() => uploadVideoRecord(upload({ mimeType: 'application/x-foo' })), /unsupported video type/)
  const recordCidNotRaw = 'bafyreigh2akiscaildcqabsyg3dfr6chu3fgpregiymsck7e7aqa4s52zy'
  assert.throws(
    () => uploadVideoRecord(upload({ blob: { cid: recordCidNotRaw, mimeType: 'video/mp4', size: 5 } })),
    /raw sha-256/,
  )
  // The PDS's registered name for the same container is fine.
  const mkv = uploadVideoRecord(
    upload({ mimeType: 'video/x-matroska', blob: { cid: HELLO, mimeType: 'video/matroska', size: 5 } }),
  )
  assert.equal(mkv.storage.blob.mimeType, 'video/matroska')
})

test('mime helpers', () => {
  assert.equal(mimeForName('IMG_0001.MOV'), 'video/quicktime')
  assert.equal(mimeForName('a.mkv'), 'video/x-matroska')
  assert.equal(mimeForName('a.txt'), null)
  assert.ok(sameContainer('video/x-msvideo', 'video/vnd.avi'))
  assert.ok(!sameContainer('video/mp4', 'video/quicktime'))
})

test('blob refs are CID links in the record CID', async () => {
  const rec = uploadVideoRecord(upload())
  assert.equal(toIpld(rec).storage.blob.ref.toString(), HELLO)
  const asString = structuredClone(rec)
  asString.storage.blob.ref = HELLO // a string is not a link: a different CID
  assert.notEqual(await recordCid(rec), await recordCid(asString))
  const built = await buildUploadContribution({
    did: 'did:plc:aliceaaaaaaaaaaaaaaaaaaa',
    rkey: '3mzrrcdrdgs2d',
    video: upload(),
    terms: {
      modalities: ['video'],
      purposes: ['training'],
      modelClasses: ['academic'],
      peoplePresent: 'only-me',
      authorshipAttested: true,
      adultAttested: true,
    },
    createdAt: '2026-11-08T07:00:00.000Z',
  })
  assert.equal(built.consent.subject.cid, await recordCid(built.video))
})
