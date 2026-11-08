// Deterministic inputs for the JS record builder. scripts/vectors.mjs writes
// their outputs to tests/fixtures/web_app_vectors.json; the Python suite
// (tests/test_web_app.py) validates them with the Python validators and CID
// code, and test/vectors.test.js checks the file is current.
import { buildContribution, buildUploadContribution, withdrawBody } from '../src/records.js'

export const DID = 'did:plc:aliceaaaaaaaaaaaaaaaaaaa'
const BLOB = 'bafkreifwkq2hesfskgekpkjbmih2ityaslss3rvd5wsmdytyl2pkdjxu7m'
const POST_CID = 'bafyreicamp74xmd5e5riqch6ocgwqb2yfs63zi4ukovfukazbawioueuci'

function post(extra = {}) {
  return {
    $type: 'app.bsky.feed.post',
    text: 'Making pasta 🍝\nwith friends',
    langs: ['en', 'EN', 'cs'],
    createdAt: '2026-11-08T06:18:33.580Z',
    embed: {
      $type: 'app.bsky.embed.video',
      aspectRatio: { height: 1080, width: 1920 },
      video: { $type: 'blob', mimeType: 'video/mp4', ref: { $link: BLOB }, size: 12262169 },
    },
    ...extra,
  }
}

const BASE_TERMS = {
  modalities: ['video', 'audio'],
  purposes: ['training'],
  modelClasses: ['academic'],
  peoplePresent: 'only-me',
  compensation: 'none',
  rightsBasis: 'own-recording',
  captureProvenance: 'self-attested',
  authorshipAttested: true,
  adultAttested: true,
}

export const CASES = [
  { name: 'minimal', rkey: '3mzrrcdrdgs2a', post: post(), video: {}, terms: BASE_TERMS },
  {
    name: 'everything',
    rkey: '3mzrrcdrdgs2b',
    post: post(),
    video: {
      title: '  Pasta‮ night  ',
      description: 'A cooking clip.\nShot at home.',
      tags: ['cooking', ' food ', 'cooking'],
      labels: ['nudity', 'not-a-label'],
      probe: { size: 12262169, width: 1920, height: 1080, durationMs: 41500 },
    },
    terms: {
      ...BASE_TERMS,
      modalities: ['video', 'audio', 'transcript', 'face', 'voice'],
      purposes: ['training', 'evaluation', 'benchmarking'],
      modelClasses: ['academic', 'open-weights', 'commercial'],
      peoplePresent: 'all-consented',
      compensation: 'credit-only',
      rightsBasis: 'licensed',
      expiresAt: '2028-11-08T06:20:00.000Z',
    },
  },
  {
    name: 'record-with-media',
    rkey: '3mzrrcdrdgs2c',
    post: post({
      text: '',
      langs: [],
      embed: {
        $type: 'app.bsky.embed.recordWithMedia',
        record: {
          $type: 'app.bsky.embed.record',
          record: { uri: 'at://' + DID + '/app.bsky.feed.post/3mzrrcaaaaa2a', cid: POST_CID },
        },
        media: post().embed,
      },
    }),
    video: {},
    terms: { ...BASE_TERMS, modalities: ['video'], peoplePresent: 'no-identifiable-people', compensation: 'monetary' },
  },
]

// "Upload from my computer": a blob in the user's own PDS (#pdsBlobRef).
const UPLOAD_BLOB = 'bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq' // sha-256 of 'hello'
export const UPLOAD_CASES = [
  {
    name: 'upload-minimal',
    rkey: '3mzrrcdrdgs2d',
    video: { title: 'Kitchen', mimeType: 'video/mp4', blob: { cid: UPLOAD_BLOB, mimeType: 'video/mp4', size: 5 } },
    terms: BASE_TERMS,
  },
  {
    name: 'upload-everything',
    rkey: '3mzrrcdrdgs2e',
    video: {
      title: ' Garden\u200b walk ',
      description: 'Filmed on a walk.',
      tags: ['outdoors'],
      labels: ['graphic-media'],
      mimeType: 'video/x-matroska',
      blob: { cid: UPLOAD_BLOB, mimeType: 'video/matroska', size: 5 },
      probe: { width: 1280, height: 720, durationMs: 5000 },
    },
    terms: { ...BASE_TERMS, purposes: ['evaluation'], modelClasses: ['academic', 'open-weights'] },
  },
]

export async function makeVectors() {
  const out = []
  for (const c of CASES) {
    const built = await buildContribution({
      did: DID,
      rkey: c.rkey,
      postUri: 'at://' + DID + '/app.bsky.feed.post/3mzrrcdrdgs2f',
      postCid: POST_CID,
      post: c.post,
      video: c.video,
      terms: c.terms,
      createdAt: '2026-11-08T07:00:00.000Z',
    })
    out.push({
      name: c.name,
      did: DID,
      rkey: c.rkey,
      post: c.post,
      videoUri: built.videoUri,
      videoCid: built.videoCid,
      video: built.video,
      consent: built.consent,
      body: built.body,
      withdraw: withdrawBody(DID, c.rkey),
    })
  }
  for (const c of UPLOAD_CASES) {
    const built = await buildUploadContribution({
      did: DID,
      rkey: c.rkey,
      video: c.video,
      terms: c.terms,
      createdAt: '2026-11-08T07:00:00.000Z',
    })
    out.push({
      name: c.name,
      did: DID,
      rkey: c.rkey,
      videoUri: built.videoUri,
      videoCid: built.videoCid,
      video: built.video,
      consent: built.consent,
      body: built.body,
      withdraw: withdrawBody(DID, c.rkey),
    })
  }
  return out
}
