// The consent web app (https://consentvid.org/app/). Signs in with atproto
// OAuth, lists the user's Bluesky video posts, and writes videoV2 (pointing
// at the post: #bskyPostRef) + consentV2 records to the user's own repo in
// one applyWrites commit per video. "Upload from my computer" strips the
// file's identifying metadata in the browser (MP4/MOV), uploads it as a blob
// to the user's own PDS (no post) and writes videoV2 (#pdsBlobRef) +
// consentV2 the same way. Everything runs in the browser: no ConsentVid
// server sees a token or a video.

import { BrowserOAuthClient, buildAtprotoLoopbackClientMetadata } from '@atproto/oauth-client-browser'

import clientMetadata from '../../web/app/client-metadata.json' with { type: 'json' }
import { decideTraining, isBotProfile, scanMetadata } from './gates.js'
import { Mp4Error, isMp4, stripMp4 } from './mp4strip.js'
import {
  AI_PREF_NSID,
  CONSENT_NSID,
  OFFERED_PROVENANCE,
  PDS_BLOB_MAX_SIZE,
  POST_NSID,
  PROFILE_NSID,
  SENSITIVE_SELF_LABELS,
  VIDEO_MIME_TYPES,
  VIDEO_NSID,
  VOCAB,
  buildContribution,
  buildUploadContribution,
  cleanLine,
  defaultTitle,
  expiryFrom,
  mimeForName,
  newTid,
  parseAtUri,
  parsePostParam,
  postSelfLabels,
  rawCid,
  termsErrors,
  videoBlobOf,
  videoErrors,
  withdrawBody,
} from './records.js'
import { FALLBACK_SCOPE, SCOPE } from './scopes.js'

const HANDLE_RESOLVER = 'https://public.api.bsky.app'
const POST_PAGES_FIRST = 3 // listRecords pages read before showing the list
const MAX_BLOB_BYTES = 1024 * 1024 * 1024

const LABELS = {
  modalities: {
    video: 'The picture',
    audio: 'The sound',
    transcript: 'A transcript of the speech',
    face: 'Identifiable faces (e.g. face recognition)',
    voice: 'Identifiable voices (e.g. voice cloning)',
  },
  purposes: { training: 'Training AI models', evaluation: 'Evaluating models', benchmarking: 'Public benchmarks' },
  modelClasses: { academic: 'Academic research', 'open-weights': 'Open-weights models', commercial: 'Commercial use' },
  peoplePresent: {
    'only-me': 'Only me',
    'all-consented': 'Other people too, and every one of them agreed to this use',
    'no-identifiable-people': 'No identifiable face or voice (e.g. hands only, scenery)',
  },
  compensation: { none: 'None', 'credit-only': 'Credit only', monetary: 'Payment' },
  rightsBasis: {
    'own-recording': 'I recorded it',
    licensed: 'Licensed from the person who recorded it',
    'other-rights-holder': 'Other documented rights',
  },
  labels: { porn: 'Porn', sexual: 'Sexual', nudity: 'Nudity', 'graphic-media': 'Graphic media' },
}

// ------------------------------------------------------------- helpers ----

const $ = (id) => document.getElementById(id)

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue
    if (k === 'class') node.className = v
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v)
    else node.setAttribute(k, v === true ? '' : v)
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c)
  return node
}

function notice(text, kind = '') {
  const n = $('notice')
  n.textContent = text
  n.className = `notice ${kind}`
  n.hidden = !text
}

function log(text, kind = '') {
  $('log').append(el('li', { class: kind }, text))
}

class XrpcError extends Error {
  constructor(status, error, message) {
    super(`${error || `HTTP ${status}`}${message ? `: ${message}` : ''}`)
    this.status = status
    this.error = error
  }
}

// ---------------------------------------------------------------- state ----

const state = {
  client: null,
  session: null,
  did: null,
  posts: [], // {uri, cid, value, blob}
  postCursor: null,
  postsDone: false,
  contributions: new Map(), // post at-uri -> {rkey, video, consent}
  others: [], // contributions not tied to a post (uploads, CLI): {rkey, video, consent}
  files: [], // queued uploads: {id, file, title, status}
  selected: new Set(),
  trainingDenied: null,
  deepLink: null,
  pendingMeta: false,
}

async function xrpc(nsid, { params, body } = {}) {
  const qs = params ? `?${new URLSearchParams(params)}` : ''
  const init = body
    ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
    : { method: 'GET' }
  const res = await state.session.fetchHandler(`/xrpc/${nsid}${qs}`, init)
  if (!res.ok) {
    let err = {}
    try {
      err = await res.json()
    } catch {
      /* not JSON */
    }
    throw new XrpcError(res.status, err.error, err.message)
  }
  return res
}

async function getRecord(collection, rkey) {
  try {
    const res = await xrpc('com.atproto.repo.getRecord', { params: { repo: state.did, collection, rkey } })
    return await res.json()
  } catch (e) {
    if (e instanceof XrpcError && (e.error === 'RecordNotFound' || e.status === 404)) return null
    throw e
  }
}

async function listPage(collection, cursor, limit = 100) {
  const params = { repo: state.did, collection, limit: String(limit) }
  if (cursor) params.cursor = cursor
  const res = await xrpc('com.atproto.repo.listRecords', { params })
  return await res.json()
}

async function listAll(collection, maxPages = 20) {
  const out = []
  const seen = new Set()
  let cursor
  for (let i = 0; i < maxPages; i++) {
    const page = await listPage(collection, cursor)
    out.push(...(page.records || []))
    cursor = page.cursor
    if (!cursor || !(page.records || []).length || seen.has(cursor)) return out
    seen.add(cursor)
  }
  throw new Error(`more than ${maxPages} pages of ${collection}`)
}

// ---------------------------------------------------------------- OAuth ----

function isLoopback() {
  return ['127.0.0.1', '[::1]'].includes(location.hostname)
}

async function makeClient() {
  if (location.hostname === 'localhost') {
    // Loopback OAuth clients must use an IP origin (atproto OAuth spec).
    location.replace(location.href.replace('//localhost', '//127.0.0.1'))
    return null
  }
  const metadata = isLoopback()
    ? buildAtprotoLoopbackClientMetadata({
        scope: `${SCOPE} transition:generic`,
        redirect_uris: [`${location.origin}${location.pathname}`],
      })
    : clientMetadata
  return new BrowserOAuthClient({ clientMetadata: metadata, handleResolver: HANDLE_RESOLVER, responseMode: 'fragment' })
}

async function signIn(handle, scope = SCOPE) {
  const st = JSON.stringify({ post: state.deepLink ? state.deepLink.uri : null })
  try {
    await state.client.signIn(handle, { scope, state: st })
  } catch (e) {
    if (scope === SCOPE && /invalid_scope/i.test(String(e && e.message))) {
      // The user decides: never widen the permission silently.
      notice(
        'Your server does not support fine-grained permissions yet. You can sign in with its broader ' +
          '“generic” permission instead; ConsentVid will still only write org.consentvid.* records.',
      )
      $('notice').append(
        ' ',
        el('button', { class: 'btn ghost small', type: 'button', onclick: () => signIn(handle, FALLBACK_SCOPE) },
          'Continue with the broader permission'),
      )
      return
    }
    throw e
  }
}

// ---------------------------------------------------------------- load ----

async function loadAccount() {
  // Gates first (signals.py): a bot can't consent; a public "no training"
  // preference rules out the training purpose. A failed lookup fails closed.
  const profile = await getRecord(PROFILE_NSID, 'self')
  if (profile && isBotProfile(profile.value)) {
    $('blocked-why').textContent =
      "Your profile carries the 'bot' self-label. Bots can't give consent: only a person who recorded a video " +
      'can. If this is a person’s account, remove the bot label in your profile settings and reload.'
    $('blocked').hidden = false
    return false
  }
  const prefSelf = await getRecord(AI_PREF_NSID, 'self')
  const overrides = (await listAll(AI_PREF_NSID, 5))
    .filter((r) => !r.uri.endsWith('/self'))
    .map((r) => r.value)
  const decision = decideTraining(prefSelf ? prefSelf.value : null, overrides)
  state.trainingDenied = decision.denied ? decision.detail : null

  const [videos, consents] = await Promise.all([listAll(VIDEO_NSID), listAll(CONSENT_NSID)])
  const consentByKey = new Map(consents.map((r) => [parseAtUri(r.uri)?.rkey, r]))
  state.contributions.clear()
  state.others = []
  for (const v of videos) {
    const storage = v.value && v.value.storage
    const postUri = storage && storage.post && storage.post.uri
    const rkey = parseAtUri(v.uri)?.rkey
    if (!rkey) continue
    const entry = { rkey, video: v, consent: consentByKey.get(rkey) || null }
    if (postUri) state.contributions.set(postUri, entry)
    else state.others.push(entry)
  }
  return true
}

async function loadPosts(pages) {
  for (let i = 0; i < pages && !state.postsDone; i++) {
    const page = await listPage(POST_NSID, state.postCursor)
    for (const r of page.records || []) {
      const blob = videoBlobOf(r.value)
      if (blob && !state.posts.some((p) => p.uri === r.uri)) state.posts.push({ uri: r.uri, cid: r.cid, value: r.value, blob })
    }
    state.postCursor = page.cursor
    if (!page.cursor || !(page.records || []).length) state.postsDone = true
  }
}

async function loadDeepLinkedPost() {
  const at = state.deepLink
  if (!at) return
  if (at.did !== state.did) {
    notice('The link you followed is for a post by another account. Sign in as its author to contribute it.', 'error')
    return
  }
  const rec = await getRecord(POST_NSID, at.rkey)
  if (!rec) {
    notice('The linked post no longer exists.', 'error')
    return
  }
  const blob = videoBlobOf(rec.value)
  if (!blob) {
    notice('The linked post has no video.', 'error')
    return
  }
  if (!state.posts.some((p) => p.uri === rec.uri)) state.posts.unshift({ uri: rec.uri, cid: rec.cid, value: rec.value, blob })
  if (!state.contributions.has(rec.uri)) state.selected.add(rec.uri)
}

// --------------------------------------------------------------- render ----

function renderChoices() {
  for (const box of document.querySelectorAll('.choices')) {
    const field = box.dataset.field
    const single = box.hasAttribute('data-single')
    const values = field === 'labels' ? SENSITIVE_SELF_LABELS : VOCAB[field]
    box.replaceChildren(
      ...values.map((value) => {
        const disabled = field === 'purposes' && value === 'training' && state.trainingDenied
        return el(
          'label',
          { title: disabled ? 'Your AI-usage preference denies training' : null },
          el('input', {
            type: single ? 'radio' : 'checkbox',
            name: field,
            value,
            disabled: Boolean(disabled),
            onchange: update,
          }),
          (LABELS[field] && LABELS[field][value]) || value,
        )
      }),
    )
    if (field === 'purposes' && state.trainingDenied) {
      box.append(
        el(
          'span',
          { class: 'hint' },
          `Training is unavailable: your public AI-usage preference (community.lexicon.preference.ai, ${state.trainingDenied}) denies it.`,
        ),
      )
    }
  }
  for (const [id, field, dflt] of [
    ['compensation', 'compensation', 'none'],
    ['rightsBasis', 'rightsBasis', 'own-recording'],
  ]) {
    $(id).replaceChildren(
      ...VOCAB[field].map((v) => el('option', { value: v, selected: v === dflt }, LABELS[field][v] || v)),
    )
  }
}

function thumbFor(post) {
  const img = el('img', {
    alt: '',
    loading: 'lazy',
    src: `https://video.bsky.app/watch/${encodeURIComponent(state.did)}/${encodeURIComponent(post.blob.cid)}/thumbnail.jpg`,
  })
  img.addEventListener('error', () => img.replaceWith(el('div', { class: 'thumb', 'aria-hidden': 'true' })))
  return img
}

function renderPosts() {
  const list = $('posts')
  list.replaceChildren(
    ...state.posts.map((post) => {
      const contribution = state.contributions.get(post.uri)
      const text = (post.value.text || '').trim() || '(no text)'
      const date = (post.value.createdAt || '').slice(0, 10)
      let control
      if (contribution) {
        control = el(
          'button',
          { class: 'btn danger small', type: 'button', onclick: (e) => withdraw(post, contribution, e.currentTarget) },
          'Withdraw',
        )
      } else {
        control = el('input', {
          type: 'checkbox',
          'aria-label': `Contribute: ${text.slice(0, 80)}`,
          checked: state.selected.has(post.uri),
          onchange: (e) => {
            if (e.target.checked) state.selected.add(post.uri)
            else state.selected.delete(post.uri)
            renderPosts()
            update()
          },
        })
      }
      return el(
        'li',
        { class: `post${state.selected.has(post.uri) ? ' selected' : ''}` },
        control,
        thumbFor(post),
        el(
          'div',
          { class: 'meta' },
          el('span', { class: 'text' }, text),
          el('span', { class: 'date' }, date),
          contribution
            ? el('span', { class: 'state' }, contribution.consent ? ' · in ConsentVid' : ' · registered, no consent')
            : null,
        ),
      )
    }),
  )
  $('posts-empty').hidden = state.posts.length > 0
  $('more').hidden = state.postsDone
}

function renderOthers() {
  const list = $('others')
  list.replaceChildren(
    ...state.others.map((entry) => {
      const v = entry.video.value || {}
      const kind = (v.storage && v.storage.$type ? v.storage.$type.split('#')[1] : '') || ''
      return el(
        'li',
        { class: 'post' },
        el(
          'button',
          { class: 'btn danger small', type: 'button', onclick: (e) => withdrawOther(entry, e.currentTarget) },
          'Withdraw',
        ),
        el('div', { class: 'thumb', 'aria-hidden': 'true' }),
        el(
          'div',
          { class: 'meta' },
          el('span', { class: 'text' }, v.title || '(untitled)'),
          el('span', { class: 'date' }, (v.createdAt || '').slice(0, 10)),
          el(
            'span',
            { class: 'state' },
            `${kind === 'pdsBlobRef' ? ' · uploaded' : ''}${entry.consent ? ' · in ConsentVid' : ' · registered, no consent'}`,
          ),
        ),
      )
    }),
  )
  $('others-card').hidden = state.others.length === 0
}

function mb(n) {
  return `${(n / 1e6).toFixed(n < 1e7 ? 1 : 0)} MB`
}

function fileProblem(file) {
  if (file.size > PDS_BLOB_MAX_SIZE) return `larger than ${mb(PDS_BLOB_MAX_SIZE)}`
  if (!uploadMime(file)) return 'not a video file this page can upload (MP4, MOV, WebM, MKV, AVI, MPEG)'
  return null
}

function uploadMime(file) {
  const byName = mimeForName(file.name)
  if (byName) return byName
  return VIDEO_MIME_TYPES.includes(file.type) ? file.type : null
}

let fileIds = 0
function addFiles(files) {
  for (const file of files) {
    const problem = fileProblem(file)
    state.files.push({ id: ++fileIds, file, title: '', status: problem ? `Can't upload: ${problem}.` : '', bad: Boolean(problem) })
  }
  renderFiles()
  update()
}

function renderFiles() {
  const list = $('files')
  list.replaceChildren(
    ...state.files.map((f) =>
      el(
        'li',
        { class: `file${f.bad ? ' bad' : ''}` },
        el('div', { class: 'meta' },
          // The file name is shown here only: it is never published (camera
          // file names leak the capture time and device).
          el('span', { class: 'text' }, `${f.file.name} · ${mb(f.file.size)}`),
          f.bad
            ? null
            : el('label', {}, 'Title (published) ',
                el('input', {
                  type: 'text',
                  value: f.title,
                  maxlength: '300',
                  required: true,
                  placeholder: 'e.g. Making pasta at home',
                  oninput: (e) => {
                    f.title = e.target.value
                    update()
                  },
                })),
          f.status ? el('span', { class: 'status', role: 'status' }, f.status) : null,
          f.busy ? el('progress', { 'aria-label': `Working on ${f.file.name}` }) : null,
        ),
        f.busy
          ? null
          : el('button', {
              class: 'btn ghost small',
              type: 'button',
              onclick: () => {
                state.files = state.files.filter((x) => x !== f)
                renderFiles()
                update()
              },
            }, 'Remove'),
      ),
    ),
  )
}

function setFile(f, status, busy = false) {
  f.status = status
  f.busy = busy
  renderFiles()
}

function readyFiles() {
  return state.files.filter((f) => !f.bad && !f.done)
}

function readTerms() {
  const checked = (field) => [...document.querySelectorAll(`input[name="${field}"]:checked`)].map((i) => i.value)
  return {
    modalities: checked('modalities'),
    purposes: checked('purposes'),
    modelClasses: checked('modelClasses'),
    peoplePresent: checked('peoplePresent')[0],
    compensation: $('compensation').value,
    rightsBasis: $('rightsBasis').value,
    captureProvenance: OFFERED_PROVENANCE.includes('self-attested') ? 'self-attested' : undefined,
    expiresAt: expiryFrom($('expiry').value),
    authorshipAttested: $('attest-authorship').checked,
    adultAttested: $('attest-adult').checked,
  }
}

function readVideoDetails() {
  return {
    tags: $('tags')
      .value.split(',')
      .map((t) => t.trim())
      .filter(Boolean),
    labels: [...document.querySelectorAll('input[name="labels"]:checked')].map((i) => i.value),
  }
}

function listWords(field, values) {
  const words = values.map((v) => (LABELS[field][v] || v).toLowerCase())
  return words.length <= 1 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words.at(-1)}`
}

function update() {
  const terms = readTerms()
  const details = readVideoDetails()
  const errors = [...termsErrors(terms), ...videoErrors(details)]
  if (state.trainingDenied && terms.purposes.includes('training'))
    errors.push('Your AI-usage preference denies training.')
  const files = readyFiles()
  if (files.some((f) => !cleanLine(f.title))) errors.push('Give every uploaded video a title.')
  if (!state.selected.size && !files.length) errors.unshift('Choose at least one video.')
  const n = state.selected.size + files.length
  const summary = $('summary')
  if (n && terms.modalities.length && terms.purposes.length && terms.modelClasses.length && terms.peoplePresent) {
    const expiry = terms.expiresAt ? `until ${terms.expiresAt.slice(0, 10)}` : 'until you withdraw it'
    summary.replaceChildren(
      el(
        'p',
        {},
        `You consent, for ${n} video${n > 1 ? 's' : ''}, that ${listWords('modalities', terms.modalities)} may be used for ` +
          `${listWords('purposes', terms.purposes)} by ${listWords('modelClasses', terms.modelClasses)}, ${expiry}.`,
      ),
      el('p', {}, `Who appears: ${LABELS.peoplePresent[terms.peoplePresent].toLowerCase()}.`),
      el(
        'p',
        {},
        'Anything you did not tick is not granted. You can withdraw on this page at any time: withdrawing (or ' +
          'deleting the post on Bluesky) removes the video from every dataset, and an uploaded file is deleted from ' +
          'your server.',
      ),
    )
  } else {
    summary.textContent = 'Choose at least one video and your terms.'
  }
  const list = $('errors')
  list.replaceChildren(...errors.map((e) => el('li', {}, e)))
  list.hidden = errors.length === 0
  $('sign').disabled = errors.length > 0
}

// -------------------------------------------------------------- actions ----

async function probe(bytes, mimeType) {
  return await new Promise((resolve) => {
    const url = URL.createObjectURL(new Blob([bytes], { type: mimeType || 'video/mp4' }))
    const video = document.createElement('video')
    const done = (value) => {
      URL.revokeObjectURL(url)
      resolve(value)
    }
    const timer = setTimeout(() => done({}), 10000)
    video.preload = 'metadata'
    video.muted = true
    video.addEventListener('loadedmetadata', () => {
      clearTimeout(timer)
      const durationMs = Number.isFinite(video.duration) ? Math.round(video.duration * 1000) : undefined
      done({ width: video.videoWidth || undefined, height: video.videoHeight || undefined, durationMs })
    })
    video.addEventListener('error', () => {
      clearTimeout(timer)
      done({})
    })
    video.src = url
  })
}

async function contributeOne(post, terms, details) {
  const label = (post.value.text || '').trim().slice(0, 40) || post.uri
  log(`“${label}”: checking the video…`)
  const res = await xrpc('com.atproto.sync.getBlob', { params: { did: state.did, cid: post.blob.cid } })
  const bytes = new Uint8Array(await res.arrayBuffer())
  if (bytes.length > MAX_BLOB_BYTES) throw new Error('the video is too large')
  if ((await rawCid(bytes)) !== post.blob.cid) throw new Error("your server returned bytes that don't match the post")
  const found = scanMetadata(bytes)
  if (found.length && !$('keep-metadata').checked) {
    $('keep-metadata-row').hidden = false
    throw new Error(
      `the video carries identifying metadata (${found.join(', ')}). It can't be removed without changing the ` +
        'post: delete it and post a stripped copy, or tick “include anyway”.',
    )
  }
  const measured = await probe(bytes, post.blob.mimeType)
  const rkey = newTid()
  const createdAt = new Date().toISOString()
  const labels = [...new Set([...details.labels, ...postSelfLabels(post.value).filter((l) => SENSITIVE_SELF_LABELS.includes(l))])]
  const c = await buildContribution({
    did: state.did,
    rkey,
    postUri: post.uri,
    postCid: post.cid,
    post: post.value,
    video: { title: defaultTitle(post.value), tags: details.tags, labels, probe: { size: bytes.length, ...measured } },
    terms,
    createdAt,
  })
  const out = await (await xrpc('com.atproto.repo.applyWrites', { body: c.body })).json()
  const stored = out && out.results && out.results[0] && out.results[0].cid
  if (stored && stored !== c.videoCid) {
    // The consent pins the video CID we computed; if the server stored the
    // record differently, the pair would be invalid: take both back.
    await xrpc('com.atproto.repo.applyWrites', { body: withdrawBody(state.did, rkey) })
    throw new Error('your server stored the record differently than expected; nothing was kept')
  }
  state.contributions.set(post.uri, {
    rkey,
    video: { uri: c.videoUri, value: c.video },
    consent: { uri: `at://${state.did}/${CONSENT_NSID}/${rkey}`, value: c.consent },
  })
  state.selected.delete(post.uri)
  log(`“${label}”: consent signed.`, 'ok')
}

async function uploadBlob(bytes, mimeType) {
  const res = await state.session.fetchHandler('/xrpc/com.atproto.repo.uploadBlob', {
    method: 'POST',
    headers: { 'content-type': mimeType },
    body: bytes,
  })
  if (!res.ok) {
    let err = {}
    try {
      err = await res.json()
    } catch {
      /* not JSON */
    }
    if (res.status === 413 || /too ?large/i.test(`${err.error} ${err.message}`)) {
      throw new Error('your server refused the file as too large (some servers accept only a few MB)')
    }
    throw new XrpcError(res.status, err.error, err.message)
  }
  const out = await res.json()
  const b = out && out.blob
  const cid = b && b.ref && b.ref.$link
  if (!cid || !Number.isInteger(b.size)) throw new Error('your server answered the upload without a blob reference')
  return { cid, mimeType: b.mimeType, size: b.size }
}

async function contributeFile(f, terms, details) {
  const mime = uploadMime(f.file)
  setFile(f, 'Reading the file…', true)
  let bytes = new Uint8Array(await f.file.arrayBuffer())
  const original = await probe(bytes, mime)
  if (isMp4(bytes)) {
    setFile(f, 'Removing identifying metadata (no re-encoding)…', true)
    try {
      const stripped = stripMp4(bytes)
      bytes = stripped.bytes
      log(`“${cleanLine(f.title)}”: removed ${stripped.removed.join(', ')}.`)
    } catch (e) {
      if (!(e instanceof Mp4Error)) throw e
      if (!$('keep-metadata').checked) {
        $('keep-metadata-row').hidden = false
        throw new Error(`its metadata can't be removed in the browser (${e.message}); use the consentvid command-line tool, or tick “include anyway”`)
      }
    }
  } else if (!$('keep-metadata').checked) {
    // WebM, Matroska, AVI, MPEG: the browser can't rewrite these containers.
    $('keep-metadata-row').hidden = false
    throw new Error(
      'only MP4 and MOV files can be cleaned of identifying metadata in the browser. Convert it to MP4, use the ' +
        'consentvid command-line tool, or tick “include anyway”',
    )
  }
  const found = scanMetadata(bytes)
  if (found.length && !$('keep-metadata').checked) {
    $('keep-metadata-row').hidden = false
    throw new Error(`the file still carries identifying metadata (${found.join(', ')}); tick “include anyway” to upload it`)
  }
  const measured = await probe(bytes, mime)
  if (original.width && !measured.width) throw new Error('the cleaned file no longer plays; nothing was uploaded')
  const cid = await rawCid(bytes)
  setFile(f, `Uploading ${mb(bytes.length)} to your server…`, true)
  const blob = await uploadBlob(bytes, mime)
  if (blob.cid !== cid || blob.size !== bytes.length) throw new Error('your server stored different bytes than were sent')
  setFile(f, 'Signing…', true)
  const rkey = newTid()
  const createdAt = new Date().toISOString()
  const c = await buildUploadContribution({
    did: state.did,
    rkey,
    video: { title: f.title, mimeType: mime, blob, tags: details.tags, labels: details.labels, probe: measured },
    terms,
    createdAt,
  })
  // The blob is referenced from here on; an unreferenced blob is deleted by
  // the server after a while, so a failure below leaves nothing behind.
  const out = await (await xrpc('com.atproto.repo.applyWrites', { body: c.body })).json()
  const stored = out && out.results && out.results[0] && out.results[0].cid
  if (stored && stored !== c.videoCid) {
    await xrpc('com.atproto.repo.applyWrites', { body: withdrawBody(state.did, rkey) })
    throw new Error('your server stored the record differently than expected; nothing was kept')
  }
  state.others.unshift({
    rkey,
    video: { uri: c.videoUri, value: c.video },
    consent: { uri: `at://${state.did}/${CONSENT_NSID}/${rkey}`, value: c.consent },
  })
  f.done = true
  setFile(f, 'Uploaded and consent signed.')
  log(`“${c.video.title}”: uploaded and consent signed.`, 'ok')
}

async function withdrawOther(entry, button) {
  if (button.dataset.confirm !== 'yes') {
    button.dataset.confirm = 'yes'
    button.textContent = 'Confirm withdraw'
    return
  }
  button.disabled = true
  try {
    await xrpc('com.atproto.repo.applyWrites', {
      body: withdrawBody(state.did, entry.rkey, { consentExists: Boolean(entry.consent) }),
    })
    state.others = state.others.filter((x) => x !== entry)
    log('Consent withdrawn: the video is out of ConsentVid, and your server deletes an uploaded file.', 'ok')
  } catch (e) {
    log(`Withdrawing failed — ${e.message}`, 'bad')
  }
  renderOthers()
  update()
}

async function signSelected() {
  const terms = readTerms()
  const details = readVideoDetails()
  $('sign').disabled = true
  $('log').replaceChildren()
  const posts = state.posts.filter((p) => state.selected.has(p.uri))
  for (const post of posts) {
    try {
      await contributeOne(post, terms, details)
    } catch (e) {
      log(`“${(post.value.text || '').trim().slice(0, 40) || post.uri}”: not signed — ${e.message}`, 'bad')
    }
  }
  for (const f of readyFiles()) {
    try {
      await contributeFile(f, terms, details)
    } catch (e) {
      setFile(f, `Not uploaded: ${e.message}.`)
      log(`“${cleanLine(f.title) || 'upload'}”: not uploaded — ${e.message}`, 'bad')
    }
  }
  state.files = state.files.filter((f) => !f.done)
  renderPosts()
  renderOthers()
  renderFiles()
  update()
}

async function withdraw(post, contribution, button) {
  if (button.dataset.confirm !== 'yes') {
    button.dataset.confirm = 'yes'
    button.textContent = 'Confirm withdraw'
    return
  }
  button.disabled = true
  try {
    await xrpc('com.atproto.repo.applyWrites', {
      body: withdrawBody(state.did, contribution.rkey, { consentExists: Boolean(contribution.consent) }),
    })
    state.contributions.delete(post.uri)
    log('Consent withdrawn: the video is out of ConsentVid. Datasets drop it at their next re-sync.', 'ok')
  } catch (e) {
    log(`Withdrawing failed — ${e.message}`, 'bad')
  }
  renderPosts()
  update()
}

// ----------------------------------------------------------------- main ----

async function main() {
  const params = new URLSearchParams(location.search)
  const postParam = params.get('post')
  state.deepLink = postParam ? parsePostParam(postParam) : null
  if (postParam && !state.deepLink) notice('The post link is not valid.', 'error')

  state.client = await makeClient()
  if (!state.client) return
  let result
  try {
    result = await state.client.init()
  } catch (e) {
    notice(`Sign-in did not complete: ${e.message}`, 'error')
  }
  if (result && result.state) {
    try {
      const saved = JSON.parse(result.state)
      if (saved && saved.post && !state.deepLink) state.deepLink = parsePostParam(saved.post)
    } catch {
      /* not ours */
    }
  }
  if (state.deepLink && !postParam) {
    history.replaceState(null, '', `${location.pathname}?post=${encodeURIComponent(state.deepLink.uri)}`)
  }

  if (!result) {
    $('signin').hidden = false
    if (state.deepLink) {
      $('deeplink-note').textContent = 'Sign in as the author of the linked post to contribute it.'
      $('deeplink-note').hidden = false
    }
    $('signin-form').addEventListener('submit', async (e) => {
      e.preventDefault()
      const handle = $('handle').value.trim().replace(/^@/, '')
      if (!handle) return
      notice('Redirecting to your server…')
      try {
        await signIn(handle)
      } catch (err) {
        notice(`Could not start sign-in: ${err.message}`, 'error')
      }
    })
    return
  }

  state.session = result.session
  state.did = result.session.did
  $('who').textContent = state.did
  $('who').hidden = false
  $('signout').hidden = false
  $('signout').addEventListener('click', async () => {
    try {
      await state.session.signOut()
    } finally {
      location.assign(location.pathname)
    }
  })

  try {
    notice('Loading your account…')
    if (!(await loadAccount())) {
      notice('')
      return
    }
    await loadPosts(POST_PAGES_FIRST)
    await loadDeepLinkedPost()
    if (!$('notice').classList.contains('error')) notice('')
  } catch (e) {
    notice(
      `Couldn't read your account (${e.message}). Nothing can be signed until it can be checked; reload to retry.`,
      'error',
    )
    return
  }
  renderChoices()
  renderPosts()
  renderOthers()
  renderFiles()
  $('contribute').hidden = false
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener('click', () => {
      for (const t of document.querySelectorAll('[role="tab"]')) {
        const on = t === tab
        t.setAttribute('aria-selected', String(on))
        $(t.getAttribute('aria-controls')).hidden = !on
      }
    })
  }
  $('file').addEventListener('change', (e) => {
    addFiles([...e.target.files])
    e.target.value = ''
  })
  const drop = $('drop')
  drop.addEventListener('dragover', (e) => {
    e.preventDefault()
    drop.classList.add('over')
  })
  drop.addEventListener('dragleave', () => drop.classList.remove('over'))
  drop.addEventListener('drop', (e) => {
    e.preventDefault()
    drop.classList.remove('over')
    addFiles([...(e.dataTransfer?.files || [])])
  })
  $('terms').addEventListener('input', update)
  $('more').addEventListener('click', async () => {
    $('more').disabled = true
    try {
      await loadPosts(POST_PAGES_FIRST)
    } catch (e) {
      notice(`Couldn't load more posts: ${e.message}`, 'error')
    }
    $('more').disabled = false
    renderPosts()
  })
  $('sign').addEventListener('click', signSelected)
  update()
}

main().catch((e) => notice(`Something went wrong: ${e.message}`, 'error'))
