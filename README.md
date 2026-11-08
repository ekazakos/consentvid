# ConsentVid

A consent-first video dataset platform on [ATproto](https://atproto.com): think
"Common Voice for video". Contributors publish videos with **signed, granular,
revocable consent receipts** in their own ATproto repository. Researchers build
[Croissant-RAI](http://mlcommons.org/croissant/) datasets from them and
**cryptographically re-verify** every clip's consent before each use.

- **Granular consent:** per clip × purpose × model class × modality × expiry.
- **Signed by the contributor:** records live in their own repo, signed by their key.
- **Revocable:** withdrawing consent means deleting the receipt. `resync` proves the
  deletion from the contributor's signed repo commit, so it trusts no relay or
  mirror (see the trust model for what that means for the hosting PDS).
- **Non-custodial:** records commit to a SHA-256 of the bytes, and the bytes live
  on R2, IPFS, PeerTube or any URL.
- **File metadata stripped by default:** container metadata (GPS location,
  device, titles, comments, XMP, vendor boxes), cover art, in-band H.264/HEVC
  SEI and AV1 metadata are stripped before anything is published, whatever the
  storage kind. The *records* themselves are public (see the trust model).

## Install

Python 3.11–3.13 (the `atproto` SDK breaks on 3.14), plus `ffmpeg` for metadata
stripping (`brew install ffmpeg`).

```bash
python3.13 -m venv ~/.consentvid/venv && source ~/.consentvid/venv/bin/activate
pip install -e ".[r2,c2pa]"          # extras: r2, ipfs, peertube, c2pa, dev
cp .env.example ~/.consentvid/config.env && chmod 600 ~/.consentvid/config.env  # fill in what you need
consentvid doctor                     # checks login, tools, backends, lexicons
```

> **iCloud warning (this checkout):** `~/Documents` is iCloud-synced, and iCloud
> evicts small files from virtualenvs, which breaks editable installs at random
> (`ModuleNotFoundError`). Keep the venv outside iCloud, as shown above.

## Contributors

```bash
consentvid login        # handle (or DID) + app password (bsky.app/settings/app-passwords)

# Register a clip and consent in one step:
consentvid contribute clip.mp4 --title "Making pasta" --tag cooking --language en \
    --attest-authorship --attest-adult --people onlyMe \
    --purpose training --purpose evaluation \
    --model-class academic --model-class openWeights \
    --modality video --modality audio \
    --expires-at 2y

consentvid list                                   # your videos + their consent
consentvid attach-consent <video-uri> --replace … # change terms
consentvid revoke <consent-uri>                   # withdraw consent
consentvid revoke <video-uri> --delete-bytes      # withdraw everything (one commit) + delete bytes
```

### Videos you already posted on Bluesky

Open **[consentvid.org/app](https://consentvid.org/app/)**, sign in with your
Bluesky account (atproto OAuth: you approve on your own server, and the app
may only create and delete `org.consentvid.videoV2` / `consentV2` records and
upload video blobs, `blob:video/*`), tick your video posts, choose your terms
and sign. Or from the CLI:

```bash
consentvid contribute --from-post https://bsky.app/profile/you.bsky.social/post/3abc... \
    --attest-authorship --attest-adult --people onlyMe \
    --modality video --purpose training --model-class academic
```

Nothing is re-uploaded: the video record points at your post
(`#bskyPostRef`) and its bytes are the video blob your PDS already serves
(`com.atproto.sync.getBlob`). They stay tied to the post: **delete the post on
Bluesky and the clip leaves every dataset** (readers report it
`unavailable`), or withdraw the consent in the app or with `consentvid
revoke`. Only your own posts can be registered (readers reject a post in
another repo). Both the app and the CLI download the video once to check it
against the post's CID and look for identifying metadata (GPS, device): such
metadata can't be stripped without changing the post, so they refuse unless
you insist (`--keep-metadata`, or the app's "include anyway"); delete the post
and post a stripped copy instead. The title defaults to the post's first
line, the languages to the post's. The same account checks apply as for
`contribute`: no consent from accounts with the `bot` self-label, and no
`training` if your `community.lexicon.preference.ai` denies it.

### A video file from your computer (without posting it)

In the app, **Upload from my computer**: choose or drop files (up to 300 MB
each; some PDSes accept less), give each a title (published; file names are
never published), choose your terms and sign. Your browser removes
identifying metadata first, without re-encoding: for MP4 and MOV it keeps
only the video and sound tracks, drops `udta`/`meta`/XMP/vendor boxes and
every other track (GPS and telemetry tracks' samples are zeroed), zeroes
creation times and blanks handler and encoder names, then checks the result
still plays. WebM, MKV and AVI can't be rewritten in the browser: convert to
MP4, use the CLI (ffmpeg strips them), or tick "include anyway". The file is
then uploaded as a blob to **your own** PDS (`com.atproto.repo.uploadBlob`;
nothing is posted) and referenced by the video record (`#pdsBlobRef`) in the
same one-commit write as its consent. Withdrawing deletes the record, and the
PDS deletes the blob. From the CLI: `consentvid contribute clip.mp4 --title
"…" --storage-kind pds …` (ffmpeg strips it first).

`login` finds your PDS from your handle (handle → DID → DID document → PDS
endpoint, as goat and the reference clients do), so your app password is only
ever sent to the server that hosts your account, on any PDS, not just
bsky.social. A handle must verify both ways (DNS/HTTPS → DID, DID document →
handle); otherwise log in with your DID. `CONSENTVID_SERVICE` overrides the
discovered PDS (an email address as the identifier falls back to
bsky.social). Logging in does not depend on the Bluesky AppView, and a cached
session is only replaced by a new login when the PDS says it is dead
(`ExpiredToken`, `InvalidToken`): network errors are reported, not retried
into the `createSession` rate limit.

Before anything is uploaded or written, the session is checked: an account
the PDS reports as not active (`getSession` `active: false`, e.g. the copy
left behind after a PDS migration, or a deactivated or taken-down account) is
refused, and a session on a PDS other than the one your DID document names is
discarded ("your account moved; signing in again"). `login` and `whoami`
warn if the credential was your **main** password (the reference PDS marks
the session's scope) or a privileged app password; ConsentVid only needs a
normal app password. Accounts that require an email code for password logins
are told to use an app password.

What `contribute` does:

1. **Strips identifying metadata** with a lossless stream copy: every container
   and stream tag (GPS, device make/model/serial, titles, comments, creation
   time, XMP, vendor boxes such as GoPro `udta`), data/subtitle tracks (Apple's
   location track, GoPro telemetry), cover art, every H.264/HEVC NAL unit and
   AV1 OBU that is not picture data or a parameter set (an allowlist: SEI,
   filler, reserved and unspecified units, AV1 metadata/padding OBUs; where
   dashcams and action cams put GPS and serials; this also drops HDR
   mastering metadata), and SEI stored in the HEVC decoder configuration
   (overwritten with filler). Every clip is stripped, whatever
   the storage kind and whatever ffprobe reports (it can't see every vendor
   box), and stripping is repeated until it changes nothing; ffmpeg is
   required. Only codecs that can be cleaned this way are copied: video
   H.264, HEVC, VP8, VP9, AV1; audio AAC, Opus, MP3, PCM. Anything else
   (Vorbis/FLAC comment headers, MJPEG EXIF, ProRes, MPEG-4 Part 2, Dolby
   Vision, AAC with a program config element, MP4 tracks with several sample
   descriptions, a file whose name doesn't match its container) is refused:
   transcode first. For `--storage-kind local` the record commits to
   a `<name>.clean.<ext>` copy, published under the name `<contentHash>.<ext>`. For a pointer you host yourself (`url`, or
   `--storage-value`), a file that still needs stripping is refused and a
   clean copy is written next to it: host that copy and re-run on it.
   `--keep-metadata` opts out of all of this. Not covered: anything in the
   picture itself (faces, plates, burnt-in timestamps), in-band AAC data
   stream / fill elements (DSE/FIL extension payloads) or MP3 ancillary bits.
2. **Hashes** the clean bytes and refuses to register the same bytes twice.
3. **Validates** the records locally, *then* uploads. A record that fails
   validation never leaves orphaned bytes, and a failed record write deletes
   the upload. If your PDS rate-limits the write (HTTP 429), the uploaded
   bytes are kept and the error says when to re-run.
4. Writes the `org.consentvid.videoV2` record and its
   `org.consentvid.consentV2` receipt in **one atomic commit**
   (`com.atproto.repo.applyWrites`): both exist or neither does. The receipt's
   `subject` (a `com.atproto.repo.strongRef`) **pins that exact video record
   version** (its CID, computed locally exactly as the PDS does), and it
   shares the video's record key (see "one consent per video" below). The
   video record carries the byte `size` and, with `--self-label`, your own
   content labels (`porn`, `sexual`, `nudity`, `graphic-media`, Bluesky's
   global self-label values): dataset tools leave labeled clips out unless
   asked (`--include-labeled`).

Nothing is granted by default. `--modality`, `--purpose` and `--model-class`
must each be given at least once. Run interactively, `contribute` and
`attach-consent` ask for any you left out, with nothing pre-selected.
Without a terminal, they refuse to run. Three attestations are also required,
each signed by your key and enforced by every reader:

- `--attest-authorship`: you recorded the video, or hold the rights to it
  (`--rights-basis licensed`). Consenting on behalf of someone else's video
  is unlawful, and every reader rejects a consent that points at a video in
  another account's repo.
- `--people onlyMe | allConsented | noIdentifiablePeople`: who identifiably
  appears or is heard in the video. `allConsented` means every identifiable
  person agreed to this use. `noIdentifiablePeople` (for example hands-only
  footage) can't be combined with the `face` or `voice` modalities. This covers
  bystanders, which the authorship attestation does not.
- `--attest-adult`: you are 18 or older (or the age of majority where you
  live) and able to consent.

These are self-declared. They deter misuse, make it detectable and put
liability on the contributor; they are not verified.

**Account signals.** Two things you publish elsewhere in your repo override a
consent, and `contribute`/`attach-consent` refuse to write one they contradict
(every reader enforces the same):

- **Bots can't consent.** If your Bluesky profile (`app.bsky.actor.profile`)
  carries the `bot` self-label (as the
  [bot tutorial](https://atproto.com/guides/bot-tutorial) recommends for
  automated accounts), the account is not a person who recorded a video.
- **Your AI-usage preferences win.** If your
  [`community.lexicon.preference.ai`](https://github.com/lexicon-community/lexicon)
  record denies AI training, a consent that grants `training` is not honoured.
  A preference scoped to a ConsentVid collection (`#collectionScope`) beats
  your account-wide default (`self`, `#globalScope`), and among such overrides
  a deny wins. Entity-scoped preferences name an AI consumer, which ConsentVid
  can't know, so they aren't applied. Only `training` maps onto a ConsentVid
  purpose.

Both are read from your own PDS with proofs (signature + MST, including proof
that a record is absent). If they can't be read, nothing is written.

**Vocabulary.** `--purpose` says what the data may be used *for* (training,
evaluation, benchmarking). `--model-class` says *who* may use it (academic,
openWeights, commercial). `researchOnly` and `foundationModel` are deprecated,
because they mixed the two questions: existing version-1 records using them
stay valid, but the CLI no longer offers them and version 2 doesn't have them.
The CLI and manifests spell values in lowerCamelCase (`openWeights`); version-2
records store the kebab-case form the lexicon style guide asks for
(`open-weights`), and filters accept either. `--compensation` records what you expect in
return. ConsentVid doesn't pay or enforce it.

Consent is always revocable: consent to process personal data must be
withdrawable (GDPR Art. 7(3)).

**One consent per video.** A consent's record key equals its video's record
key: the consent is the video's *sidecar* record, as `app.bsky.feed.threadgate`
is a post's. The PDS itself therefore refuses a second consent for a video, and
no reader ever has to choose between several. `attach-consent --replace`
rewrites that one record (with `swapRecord`, so a concurrent change is refused
instead of overwritten), which downstream `resync` reports as *modified*.
Deleting it withdraws consent. Every reader ignores a consent stored under any
other key. Revoking a video deletes it and all its consents in one commit.
That commit is guarded with `swapCommit`, which covers your *whole* repo (your
everyday Bluesky repo too), so `revoke` asks you once what it will delete, then
re-reads the repo head right before committing and retries a few times if a
like or post landed meanwhile. It only asks again if what it would delete
changed (a consent appeared, or the video record was rewritten).

Records are written under a client-chosen record key (a TID), and the CLI
waits up to 60 s for the PDS to answer a write. If the answer is lost anyway
(a timeout, a dropped connection), it looks the records up under that key: if
they are there as written, the write counts as done (and the uploaded bytes
stay); if it can't tell, it says so and keeps the bytes (`consentvid list`
shows what exists).

### Python API (many videos, scripts, notebooks)

Everything above is also a Python API; the CLI is a thin layer over it, so
the same checks apply (local validation, the bot-label and AI-preference
gates, metadata stripping, one atomic commit per video and consent). Nothing
prompts, and the consent terms and attestations have no defaults.

```python
from consentvid import Session, Terms

s = Session.login()  # the cached `consentvid login` session, or CONSENTVID_HANDLE / _APP_PASSWORD
terms = Terms(purposes=["training"], modalities=["video", "audio"], model_classes=["academic"],
              people="onlyMe", adult=True, authorship=True, expires="2y")

clip = s.contribute("clip.mp4", terms, title="Making pasta", tags=["cooking"], storage="r2")
s.contribute_from_post("https://bsky.app/profile/you.bsky.social/post/3abc...", terms)
s.list()                # your videos with their consent status
s.check(clip.video_uri) # the verdict `resync` would give
s.revoke(clip.video_uri, delete_bytes=True)

# Many videos: sequential, paced under your PDS's write budget, rate limits
# waited out, and resumable: re-run the same call after an interruption.
results = s.contribute_many(paths, terms=terms, title_for=lambda item: titles[item.path],
                            storage="r2", resume="run.jsonl")
```

See [docs/python-api.md](docs/python-api.md) for the reference and a bulk
example.

## Researchers

No account needed.

```bash
# One contributor's clips (filters are ALL-of: the clip must grant every one)
consentvid export ds.json --repo alice.bsky.social --purpose training --model-class commercial

# Or across the network, via a local AppView index
consentvid appview index                       # follow the firehose first (resumable)
consentvid appview backfill --all              # then discover and read every contributor
consentvid appview export ds.json --purpose training --model-class academic

# BEFORE EVERY USE: verify every clip, write a cleaned manifest
consentvid resync ds.json --write ds.clean.json
consentvid resync ds.json --check             # exit 1 if anything must be dropped (CI)
consentvid verify at://did:plc:…/org.consentvid.consent/…   # inspect one record
```

`resync` checks each clip against the contributor's own PDS using
`com.atproto.sync.getRecord`. It verifies the repo commit's **signature against
the DID's signing key** and the **Merkle Search Tree proof** of the record's
presence or absence, so no relay, mirror or cache is trusted (the hosting PDS
usually holds the signing key: see the trust model). Outcomes:

| status | meaning | dropped |
|---|---|---|
| `valid` | consent and video verified, unchanged since export | no |
| `revoked` | consent or video deleted (proven), or account deleted | yes |
| `expired` | past `expiresAt` | yes |
| `modified` | the consent or video changed since export (a rewritten consent has a new CID), or the manifest's terms, URL, `encodingFormat` or hash disagree with the signed records | yes |
| `invalid` | fails validation or proof verification, points at someone else's video, or is not keyed to its video (the sidecar rule) | yes |
| `unattested` | below `--min-provenance` | yes |
| `unavailable` | account deactivated, suspended or taken down, on the PDS or at the relay (`getRepoStatus` from `$CONSENTVID_RELAY`, default relay1.us-east.bsky.network: a relay-level takedown counts even if the PDS still serves the records; `--no-relay-status` skips this for offline use). Also when the PDS can't be reached (5xx, network) and the relay reports the account deleted, taken down, suspended or deactivated (never `revoked`: only the PDS's signed answer proves a withdrawal) | yes |
| `unbound` | a C2PA-stamped file: the consent stands, but nothing binds *this file's* bytes to it | yes |
| `bot-account` | the contributor's profile carries the `bot` self-label | yes |
| `conflicting-preference` | the consent grants `training`, but the same account's `community.lexicon.preference.ai` denies it | yes |
| `labeled` | the contributor self-labeled the video `porn`, `sexual`, `nudity` or `graphic-media` (`--include-labeled` keeps it) | yes |
| `stale` | the PDS served an older repo revision than the relay has seen (`com.atproto.sync.getLatestCommit`, asked *before* the PDS): a rolled-back repo could hide a withdrawal | yes |
| `unknown` | network error (only transport errors and 5xx/429; a malformed or rejected answer, or a TLS failure, is `invalid`) | yes (default `--strict`); `--lenient` keeps it unverified, so only use `--lenient` on manifests you built yourself (a contributor's PDS can make its clips uncheckable) |

The relay's `getRepoStatus` mixes two kinds of status. **Account statuses**
(`takendown`, `suspended`, `deactivated`, `deleted`, and any value ConsentVid
doesn't know) describe the account and make a clip `unavailable`. **Relay
processing statuses** (`throttled`, `host-throttled`, `desynchronized`)
describe only the relay's own handling of the repo (the account spec: for
these "active may be true"); a relay reports `host-throttled` for every
account of a PDS above its per-host account limit, which a community or
university PDS easily is. They never drop a clip the PDS has just proven;
`resync` adds the note "relay reports throttled; live index may be stale".

The manifest itself is untrusted. `resync` refuses manifests with duplicate
JSON keys (readers disagree on which copy wins), whose JSON-LD `@context`
differs from the one ConsentVid writes, whose `license` isn't
consentvid.org/license.html (a manifest claiming a blanket licence), or that
carry top-level keys or FileObject fields it doesn't know (`md5`,
`sc:contentUrl`, `@graph`, ...), because those could make a Croissant loader
fetch different bytes than resync checked. Every `distribution` entry counts:
anything that isn't a well-formed `cr:FileObject` whose `@id` is exactly the
exporter's `<contributor DID>/<video record key>` (unique) is `invalid`.
`encodingFormat` must be what the exporter derives from the signed record
(loaders act on it: `git+https` makes mlcroissant clone a repo and skip the
hash check), and a video record's `mimeType` must be a video container type.
Every receipt field (terms, attestations, `peoplePresent`,
`captureProvenance`, dates) must equal the signed record. `--write` builds a
new manifest from the verified records only: every kept entry and every
dataset-level field (creator, licence, `rai:*`, description) is regenerated;
only the name and version are carried over. Under `--lenient`, unchecked
entries are appended as they were and `rai:dataLimitations` says so.

Reads from contributors' hosts are bounded (8 MiB and 60 s per answer, 100
pages per listing, no redirects, public hosts on port 443 only), so one
hostile PDS can't stall or exhaust a whole re-sync: its clips come back
`invalid`.

Reads are also polite and resilient, as the sync spec asks. At most 4 run at
once against one PDS host. A 429 waits for the server's `RateLimit-Reset` /
`Retry-After` (at most 60 s per wait, 3 waits per call, 5 minutes per run),
and network errors and 5xx answers are retried twice. Bluesky's PDSes allow
about 3000 reads per 5 minutes (`ratelimit-policy: 3000;w=300`), so expect
roughly 10 requests/s per PDS host, two per clip. A commit signature that
doesn't match is retried once with a freshly resolved DID document (the key
may have rotated or the account moved). A PDS whose `sync.getRecord` is broken
(answers 5xx, as Cocoon does) is read with `sync.getLatestCommit` +
`sync.getBlocks` instead, walking the same Merkle path block by block; every
block is still checked against its CID.

Manifests are Croissant 1.0 + RAI with `isLiveDataset: true`. There is no
blanket licence (see [consentvid.org/license.html](https://consentvid.org/license.html)):
each `cr:FileObject` carries a `cv:consentReceipt` with the receipt's AT-URI,
**CID**, terms and the pinned video CID. ConsentVid's own properties use the
`cv:` namespace (`https://consentvid.org/ns#`). Readers also accept the old
`cr:consentReceipt`.

### Provenance inside the file (C2PA)

```bash
consentvid embed-c2pa clip.mp4 --consent at://…/org.consentvid.consent/…   # needs [c2pa]
consentvid read-c2pa clip.c2pa.mp4
consentvid resync clip.c2pa.mp4      # checks the consent the file points at
```

The file must be exactly the registered bytes (the hash is checked). Signing
uses a self-signed prototype certificate under `~/.consentvid/c2pa/`, so readers
show the manifest as valid but the signer as untrusted.

An embedded receipt is a **pointer to the consent, not proof that this file is
the consented video**: any C2PA signer can stamp someone's public receipt onto
other footage, a CA-issued certificate is still not the contributor's DID key,
and stamping changes the bytes so they never match the signed `contentHash`.
`resync` on a stamped file therefore checks the consent (revoked, expired and
modified still show as such) but reports at best `unbound`, which is dropped,
and names the registered bytes (by sha256) to train on instead.

## Storage backends

| kind | revocable? | notes |
|---|---|---|
| `r2` | yes (`revoke --delete-bytes`) | content-addressed key, correct `Content-Type`, idempotent |
| `ipfs` | **no**: unpinning doesn't remove copies elsewhere | Pinata (`CONSENTVID_PINATA_JWT`) or a Kubo node at `CONSENTVID_IPFS_API` (set explicitly). The CID is a UnixFS DAG root, *not* the file's SHA-256 |
| `peertube` | yes | uploaded **unlisted** by default; served renditions are transcodes, so they won't match `contentHash` |
| `url` | your responsibility | must be `https://` |
| `bsky` (`#bskyPostRef`, V2 only) | yes: delete the post (or withdraw) | the video blob of **your own** Bluesky post, served by your PDS; readers check the post (with a proof) still exists at the pinned CID and embeds the blob, and the blob CID equals `contentHash`. `contentUrl` is the getBlob URL; any public host serving getBlob for the same DID and CID is accepted (PDS migration). Written by the web app and `contribute --from-post` |
| `pds` (`#pdsBlobRef`, V2 only) | yes: withdrawing deletes the record, and the PDS then frees the blob | a blob in **your own** PDS, uploaded with `com.atproto.repo.uploadBlob` and referenced by the video record only (no post, nothing in your feed). Up to 300 MB (`maxSize`; Bluesky's video limit), but a PDS may accept less (the reference PDS defaults to 5 MiB). Readers require `blob.ref` = `contentHash`, `blob.size` = `size` and the same container type (`video/matroska` = `video/x-matroska`, …). `contentUrl` is the getBlob URL. Written by the web app's "Upload from my computer" and `--storage-kind pds` / `storage="pds"`. Unavailable while the account is deactivated, deleted with it |
| `local` | n/a | `<contentHash>.<ext>` (never your file name, which can carry the capture time and device); never dereferenceable, so excluded from datasets |

**Why not PDS blobs for everything?** (`pds` exists since 2026-11-08 for
everyday contributors without other hosting; for research-scale files prefer
R2/IPFS/PeerTube, for the reasons below.) A PDS blob puts the bytes in the contributor's
own repo. Size is not the obstacle: most PDSes accept 100-300 MB blobs (the
official installer sets `PDS_BLOB_UPLOAD_LIMIT` to 300 MB, and
`app.bsky.embed.video` allows 300 MB), although a bare deployment defaults to
5 MB and operators set per-account storage quotas. Nor is the hash: a blob
uploaded with `com.atproto.repo.uploadBlob` is stored byte-exact, and its CID
is the raw-codec form of the same SHA-256 that `contentHash` records (only
Bluesky's video service re-encodes, for its own playback copies). The real
reasons are that the [blob lifecycle guide](https://atproto.com/guides/blob-lifecycle)
says serving media to end users directly from a PDS is not a recommended
pattern; `com.atproto.sync.getBlob` is rate-limited (about 3000 requests per
5 minutes on bsky.social); the bytes are tied to one account's PDS and its
quota; the PDS sniffs the content and may override the declared `mimeType`;
and there is no CDN, and HTTP `Range` support is not guaranteed, for
multi-GB research files. (`getBlob`'s sandboxing CSP and attachment headers
matter for rendering in a browser, not for dataset downloads.) Above all,
blob storage ties the bytes' lifetime to the record and the account: a blob
is freed when the last record referencing it is deleted, deleted with the
account, and unavailable while the account is deactivated or taken down
([blob lifecycle](https://atproto.com/guides/blob-lifecycle)): for
consent that is a feature (withdraw, and the bytes go), for a long-lived
research mirror it is not. [docs/lexicon-v2.md](docs/lexicon-v2.md),
"Shipped compatible additions", lists what readers check for `pds`. The `bsky` kind fits too:
the bytes are a Bluesky post's video that the contributor already published,
so their lifetime is the post's (deliberately: deleting the post withdraws
the clip), and nothing new is put on the PDS. The AppView does not follow
post deletions (that would mean subscribing to every post on the network);
`resync` and `export` check the post, so run `resync` on AppView exports as
always.

> **Deleting or deactivating your ATproto account does NOT delete bytes on
> R2/IPFS/PeerTube.** Run `consentvid revoke <video-uri> --delete-bytes` for
> each video first.

Every reader rejects storage pointers that aren't plain `https://` URLs (for
`url`, `r2`, `peertube`) or a bare CID (for `ipfs`). `revoke --delete-bytes`
only deletes the object this clip's own upload would have created (R2 key
`videos/<sha256>` under your public URL, a CID, a watch URL on your PeerTube
instance), and keeps it when another of your videos uses the same bytes. A
PeerTube video is only deleted if it belongs to your account and one of your
channels and carries the `consentvid:<contentHash>` marker that `contribute`
writes into its support field (videos uploaded before that marker existed
must be deleted by hand). A failed record write only removes bytes this run
provably created: an existing R2 object, a Pinata pin reported as a
duplicate, or a CID your Kubo node had already pinned is kept. `revoke
--delete-bytes` still assumes the bucket / pinning account is yours alone: on
storage shared by several contributors, identical bytes share one object.

Settings are read from your real environment, `~/.consentvid/config.env`, or
(for editable installs) the source checkout's `.env`, and only `CONSENTVID_*`
keys are taken from files. A `.env` in the current directory is ignored, since
that is where downloaded datasets are opened. Keep these files `chmod 600`
(`consentvid doctor` checks), and prefer `consentvid login`'s cached session
over storing the app password at all. `consentvid logout` also ends the session
on the server.

## Trust model and limitations

- **Signed ≠ true.** A signature proves *who* attested, not that they filmed it.
  Uploads only get deterrence, detection and liability. The `attested`
  provenance tier is reserved for capture-time C2PA/CAWG signing that binds the
  recording to the contributor's DID, and is **not verified yet**: the CLI
  refuses to write it or filter the AppView on it, and `resync
  --min-provenance attested` reports every clip UNATTESTED (a signed
  `attested` counts as `selfAttested`).
- **The AppView is for discovery, not proof.** Jetstream strips signatures, so
  verify with `resync` before use.
- **The hosting PDS holds the key.** `valid`/`revoked` mean "the holder of the
  DID's current `#atproto` signing key vouches for this". On a standard PDS
  (e.g. bsky.social) the PDS operator holds that key, so the guarantee is
  independence from transport, relays and mirrors, not from the hosting PDS: a
  malicious or compromised PDS could sign a commit that re-adds a deleted
  consent or forges a new one. Always use manifests that pin the consent and
  video CIDs (the default export); `resync` notes when a manifest doesn't, and
  pins limit a forging PDS to replaying exactly the records already referenced.
- **Rollback:** a malicious PDS could serve an *older* signed commit to hide a
  deletion. `resync` and `verify` ask the relay for the newest revision it
  has seen (`com.atproto.sync.getLatestCommit`) *before* reading the PDS, and
  report `stale` if the PDS's signed commit is older. If the relay can't
  answer, the clip keeps its verdict with a note ("rollback not checked").
  Commits dated more than five minutes in the future are refused, so a
  forged far-future revision can't outrank honest ones. A PDS that rolls back
  before the relay has seen the newer commit is not detected.
- **Records are public.** Video and consent records are public ATproto
  records: relays, Jetstream and every indexer copy them as they are written.
  Deleting a record withdraws consent and well-behaved indexes drop it (the
  ConsentVid AppView keeps only the URI, time and reason of a withdrawal), but
  deletion is not erasure: copies made before it can persist. Only the
  metadata you publish is in them (title, description, tags, hashes, storage
  pointer, consent terms). They are public on purpose (anyone can verify a
  consent with an MST proof, and relays make it discoverable), so ConsentVid
  doesn't use atproto's permissioned-data "Spaces": they are alpha, have no
  relay, give access control rather than confidentiality, and must not hold
  consent data. Keep personal data out of public records.
- **Hosts named by contributors.** DID documents, handles and PDS endpoints
  are chosen by contributors; `resync`/`verify` only contact public `https://`
  hosts (no IP literals, `localhost`, `.local`, `.internal`) and don't follow
  redirects (except handle resolution's `/.well-known/atproto-did`, which the
  handle spec allows to redirect: up to 3 hops, each checked the same way).
  Handles are lowercased, a DNS name with conflicting `_atproto` TXT records
  doesn't resolve, and handles under reserved TLDs (`.local`, `.onion`,
  `.invalid`, `.test`, ...) never resolve; neither do `did:web` domains under
  them. A PDS endpoint must be a bare origin (no path). Reading a repo needs
  only the DID document (PDS and key); handles are verified separately, and
  only where one is shown (login, `export` names, C2PA).
- **Models trained before a revocation** stay legitimate, provided they were
  trained in good faith on a manifest that `resync` showed as valid at the time
  (GDPR Art. 7(3): withdrawal doesn't affect earlier lawful processing).
  Revocation stops all *new* use from that point, including retraining,
  fine-tuning and new evaluations. Re-sync before every run.
- **Copies:** revocation can't reach copies that were already downloaded. The
  terms of use oblige consumers to resync and delete.
- **Jetstream** keeps 36 h of replay; beyond that the indexer records a gap
  (shown by `appview stats` and `doctor`) and `appview backfill --all`
  repairs it. The indexer uses the Jetstream v2 endpoint
  (`network.bsky.jetstream.subscribeEvents`) and asks only for its
  collections (both record versions, plus `community.lexicon.preference.ai`
  and `app.bsky.actor.profile` for account signals) and the `commit`,
  `account` and `sync` kinds. `CONSENTVID_JETSTREAM_URL` may list several
  instances, comma-separated (default: the public v2 instances in us-east
  and us-west); after repeated failed connections the indexer moves to the
  next. It resumes by
  sequence number on the same instance (by time on another) and skips events
  it already applied (seq cursors are inclusive). If the seq is past
  retention (`CursorTooOld`) or unknown to the archive (`#info FutureCursor`),
  it reconnects from the stored timestamp, which Jetstream clamps to its
  retention floor (`#info OutdatedCursor`): everything still replayable is
  replayed, and the gap is recorded. It never asks Jetstream to drop large
  messages. A `#sync` event means the repo's commit chain broke, not that
  anything was deleted: the repo is hidden from queries (`desynced_repos` in
  `appview stats`), without any revocation, until the indexer re-reads it
  (normally within a minute; `appview backfill --pending` forces it); records
  missing from that listing are then revoked like any delete. The reference
  PDS emits a `#sync` on every account activation and migration, so `appview
  index` re-reads queued repos itself: a background task takes one due repo
  at a time (in a worker thread, with its own database connection), and a
  failed re-read is retried with backoff (1 min, doubling, at most 6 h).
  `appview stats` lists queued repos with their failed attempts;
  `appview index --no-drain` leaves them to `backfill --pending`. A replayed
  `#sync` is ignored. A busy database never skips an event: the indexer
  waits, reconnects and replays it.
- **Relay processing statuses are not account statuses.** An `#account`
  event or `getRepoStatus` answer of `throttled`, `host-throttled` or
  `desynchronized` comes from the relay's own bookkeeping, not from the
  account: the index never marks the account inactive for it. Because the
  relay may not be forwarding that repo's events, the repo is hidden (like
  after a `#sync`) and queued; the re-read lists it from its PDS but keeps it
  hidden, re-checking hourly, until the relay reports it active again (an
  `#account` event with `active: true` queues an immediate re-read).
- **Account signals in the index** are read with proofs whenever a repo is
  backfilled. Fail closed: a contributor whose signals were never checked is
  left out of queries and queued for a re-read. Live, a profile or preference
  record that sets the `bot` label or denies training hides the account at
  once (unverified, but only ever in the restrictive direction); any change
  queues a verified re-read. Self-labeled videos are left out unless
  `--include-labeled`.
- **Ordering:** `appview query` sorts by `min(createdAt, first time the index
  saw the record)`, as Bluesky does with untrusted `createdAt`, so a
  future-dated record can't push others past `--limit`.
- **Discovery:** `appview backfill --all` asks a relay which repos hold
  ConsentVid records (`com.atproto.sync.listReposByCollection`, default
  `relay1.us-east.bsky.network`, override with `--relay` or
  `CONSENTVID_RELAY`), adds every repo the index already knows, and reads each
  from its own PDS. Start `appview index` first, then backfill, so nothing
  written in between is missed. A backfill takes each account's hosting status
  from both the PDS and the relay (`com.atproto.sync.getRepoStatus`), the more
  restrictive one winning, so a takedown is never undone. (A relay that has
  never crawled a repo answers `RepoNotFound`; that only counts as a deletion
  when the PDS doesn't host the repo either. A relay processing status casts
  no vote; see above.) The status is decided first: a
  deactivated, suspended, taken-down or deleted (tombstoned DID) repo is
  recorded as such and not listed, so a gap that hid its `#account` event can
  still be repaired. A gap recorded while `backfill --all` runs is kept for
  the next run.
- **Deleting your account does not delete your bytes.** A PDS deletes its own
  blobs with an account, but ConsentVid's bytes live on R2, IPFS or PeerTube:
  run `consentvid revoke <video-uri> --delete-bytes` for each video *before*
  deleting or deactivating your ATproto account.
- **DNS rebinding:** host names are checked against private address ranges
  when resolved, but a name could resolve differently by the time we connect.

## Lexicons

Two record versions ship in `src/consentvid/schemas/` (mirrored in
`web/lexicons/`, and a test keeps the two identical):

- `org.consentvid.videoV2` and `org.consentvid.consentV2`: what the CLI
  writes (see [docs/lexicon-v2.md](docs/lexicon-v2.md) for what changed);
- `org.consentvid.video` and `org.consentvid.consent`: version 1, frozen.
  Every reader still accepts them.

A consent only binds a video of its own version (a V2 consent's subject must
be an `org.consentvid.videoV2` record), and `attach-consent` refuses a
version-1 video: register it again. All four are published as
`com.atproto.lexicon.schema` records and resolvable through
`_lexicon.consentvid.org`:

```bash
consentvid publish-lexicons             # after changing a schema (lexicon authority only)
consentvid resolve-lexicon org.consentvid.consent   # verifies the published record, compares with local
```

`resolve-lexicon` (and `doctor`) read the schema record with a verified
`sync.getRecord` (commit signature + MST proof) and check its `$type` and that
its `id` equals the NSID. A DNS failure (timeout, SERVFAIL) is reported as a
DNS error, never as "no record".

Changes follow the [lexicon evolution rules](https://atproto.com/specs/lexicon):
add only optional fields, never tighten or repurpose existing ones; a breaking
change needs a new NSID. `publish-lexicons` enforces both halves of that:

- it only writes as the **lexicon authority**, the account named by the
  `_lexicon.consentvid.org` DNS TXT record (`--bootstrap` for the very first
  publication, before that record exists), and refuses if several TXT records
  name different DIDs, or if the DNS lookup itself fails (it never guesses
  "no record");
- it compares each schema with the **published** one and refuses changes that
  break the evolution rules (removed or retyped properties, added or changed
  constraints, new required fields, removed `knownValues`, a different record
  key type). `--allow-breaking` is only for a brand-new NSID. A test also
  checks the local schemas against snapshots of the published ones
  (`tests/fixtures/published_lexicons/`). For an independent check, Bluesky's
  [goat](https://github.com/bluesky-social/goat) has `goat lex lint` and
  `goat lex breaking`. With `--bootstrap`, a schema already in your own repo
  is checked the same way. Open unions may gain refs; closed ones are frozen.
- it writes with compare-and-swap: `putRecord` with `swapRecord` set to the
  published record's CID, or `createRecord` for a first publication, so a
  schema changed by someone else meanwhile is never overwritten ("re-run").
- the PDS validates the `com.atproto.lexicon.schema` record itself
  (`--skip-pds-validation` if yours rejects valid lexicons).

The records' contract, beyond what the schema language can say, is in the
lexicon descriptions, and every reader enforces it. For version 2:

- **Record keys** are TIDs, and a consent's key MUST equal its video's (the
  sidecar rule); consents under other keys are ignored.
- `subject` is a `com.atproto.repo.strongRef`: `uri` is the DID-form
  `at://<did>/org.consentvid.videoV2/<rkey>` in the same repo, `cid` that
  record's CID (dag-cbor).
- `authorshipAttested` and `adultAttested` are required and `const: true`;
  `peoplePresent` is required and one of `only-me`, `all-consented`,
  `no-identifiable-people`. There is no `revocable` field: every consent is
  withdrawable.
- `contentHash` is the bytes' CIDv1 with the raw codec and sha-256
  (`bafkrei...`, what a PDS blob ref would carry); `storage` is an open union
  (`#r2Ref`, `#ipfsRef`, `#peertubeRef`, `#urlRef`, `#localRef`), and a member
  type a reader doesn't know makes the bytes unreachable for it; `langs` holds
  up to three BCP-47 tags; titles, descriptions and tags are limited in
  characters (grapheme clusters) as well as bytes, as Bluesky's posts are.
- Vocabulary is kebab-case, holds no duplicates and no control or format
  characters; `expiresAt` is later than `createdAt`; `mimeType` is one of
  its `knownValues`.

For version 1:

- **Record keys** are TIDs (`key: tid`), and an `org.consentvid.consent`
  record's key MUST equal the key of the `org.consentvid.video` it references
  (the sidecar rule above); consents under other keys are ignored.
- `videoUri` is the DID-form `at://<did>/org.consentvid.video/<rkey>` in the
  same repo; `videoCid` is a canonical CIDv1 (dag-cbor, sha-256, base32) and
  is required by every reader (optional in the schema only for evolution
  compatibility): a consent that doesn't pin its video version would follow
  any later rewrite of the video record, so it is invalid.
- `revocable`, `authorshipAttested` and `adultAttested` must be `true` and
  `peoplePresent` must be one of its `knownValues` (readers reject other
  values); `modalities`, `purposes` and `modelClasses` hold no duplicate
  values; `expiresAt` is later than `createdAt`; vocabulary values
  (`modalities`, `purposes`, `modelClasses`, `compensation`, `rightsBasis`,
  `captureProvenance`, `peoplePresent`) contain no control or format
  characters.
- `contentHash` is `sha256-` followed by 64 **lowercase** hex characters;
  `language` is a BCP-47 tag with a lowercase primary subtag (`en`, `pt-BR`;
  `EN` is rejected, as in the atproto interop vectors); `mimeType` is one of
  its `knownValues` (it becomes the manifest's `encodingFormat`, which loaders
  act on; `video/matroska` and `video/vnd.avi`, the names a PDS's file-type
  sniffing reports, are accepted alongside the `x-` names ConsentVid writes);
  `durationMs`, `width`, `height` are non-negative and at most 2^53-1 (the
  data model's limit for JavaScript readers); `storageRef.value` must match
  its `kind` (see the storage table).

### Releasing lexicon changes

1. Commit the change to `src/consentvid/schemas/` and `web/lexicons/` (a test
   keeps them identical and checks them against
   `tests/fixtures/published_lexicons/` with the evolution rules).
2. As the lexicon authority, run `consentvid publish-lexicons`.
3. Check each NSID with `consentvid resolve-lexicon <nsid>`: it must report
   "verified" and "matches the local lexicon".
4. Refresh `tests/fixtures/published_lexicons/` from the live records (for
   example `consentvid resolve-lexicon` plus `goat get`, or
   `com.atproto.repo.getRecord` on `at://<authority>/com.atproto.lexicon.schema/<nsid>`
   with `$type` removed) and commit.

Things the evolution rules forbid changing in place (string limits in
graphemes, a `strongRef` subject, an open union for storage, kebab-case
values, ...) shipped as new NSIDs, version 2: see
[docs/lexicon-v2.md](docs/lexicon-v2.md).

After changing a description, the lexicon authority must run
`consentvid publish-lexicons` so the published schemas match. No PDS resolves
third-party lexicons on write yet (the reference implementation validates only
against its bundled lexicons), so by default records are written with the
PDS's own ("optimistic") validation policy and come back with
`validationStatus: unknown`, which the CLI prints. ConsentVid always validates
locally before writing and on every read path. `--validate` /
`CONSENTVID_VALIDATE=1` forces strict PDS validation, which fails on any
reference-implementation PDS today ("Unknown lexicon type"); leave it unset.
`contribute` and `register-video` warn and ask before uploading when it is
set.

## Development

```bash
pip install -e ".[dev]"
pytest                    # offline; real CAR proofs are fixtures in tests/fixtures
pytest -m network         # also hits the live network
ruff check src tests && ruff format src tests
```

Layout: `records` (URIs, datetimes, hashing) · `versions` (V1/V2 records
and their common internal view) · `validate` (lexicon + domain rules) ·
`signals` (bot label, AI-usage preferences) · `bsky` (Bluesky-post storage) · `identity` (DID/handle → PDS, key) · `verify` (CAR/MST/signature
proofs) · `media` (probe + metadata stripping) · `storage` · `policy` ·
`croissant` · `resync` · `appview` · `c2pa_embed` · `cli`.

### The consent web app

`app/` holds the source of [consentvid.org/app](https://consentvid.org/app/)
(plain ES modules, no framework); `web/app/` holds its static page, styles and
OAuth client metadata (`client-metadata.json`, the `client_id`). The bundle
`web/app/app.js` is not committed: the Pages workflow builds it with
`npm ci --ignore-scripts && npm test && npm run build` from the exact
versions in `app/package-lock.json` (esbuild, deterministic output).

```bash
cd app && npm ci && npm test && npm run build
cd ../web && python3 -m http.server 8000 --bind 127.0.0.1   # http://127.0.0.1:8000/app/
npm run vectors   # after changing the record builder: refresh tests/fixtures/web_app_vectors.json
```

- Locally (http://127.0.0.1) the app is an atproto **loopback** OAuth client
  (no metadata to host; only `127.0.0.1` / `[::1]` origins, not `localhost`).
- OAuth: `@atproto/oauth-client-browser`, DPoP-bound tokens kept in the
  browser's IndexedDB. Scopes (`app/src/scopes.js`): `atproto` plus
  `repo:org.consentvid.videoV2?action=create&action=delete`, the same for
  `consentV2`, and `blob:video/*` (uploads). A PDS that rejects granular scopes (`invalid_scope`) is offered
  `transition:generic` instead, only after the user clicks to accept it; both
  are listed in the client metadata, as the spec requires.
- Handles are resolved through `public.api.bsky.app` (this tells Bluesky the
  handle and the user's IP; the authorization server is the user's own PDS).
- Writes: one `applyWrites` per video creating the videoV2 record and its
  sidecar consentV2 under a client-generated TID, the consent pinned to the
  locally computed video CID (if the PDS stores a different CID, both are
  deleted again). No `swapCommit`: creates under a fresh key can't overwrite
  anything. Withdraw = one `applyWrites` deleting both.
- Pre-flight checks mirror `signals.py` (bot self-label; AI-usage preference
  denying training) and look for identifying metadata in the downloaded
  video (a byte scan of the MP4 boxes outside `mdat`: a heuristic; the CLI
  uses ffprobe). They explain refusals early; the binding checks are the
  readers'.
- Uploads: `app/src/mp4strip.js` rewrites MP4/MOV boxes losslessly (see the
  contributor section), rewriting `stco`/`co64` chunk offsets when `moov`
  precedes `mdat`; fragmented MP4 is refused. Its tests run on ffmpeg-made
  fixtures (`app/test/fixtures/`) and, where ffmpeg is installed, compare
  decoded frame hashes before and after. Not covered: user-data SEI inside
  H.264/HEVC frames (the CLI's ffmpeg bitstream filters remove those). The
  blob CID and size returned by `uploadBlob` must equal the local ones, and
  the PDS's sniffed `mimeType` must name the same container.
- Strict CSP (meta tag): scripts only from the site, images only from
  Bluesky's video CDN (thumbnails), `connect-src https:` (any user's PDS).
  No analytics, no third-party scripts.
- `tests/fixtures/web_app_vectors.json` is produced by the JS record builder
  (`npm run vectors`); `tests/test_web_app.py` validates it with the Python
  validators and CIDs, and the JS tests fail if the file is stale.
- Known: `npm audit` reports zod 3.x (GHSA-mrhx-c7xc-m3w5, memory exhaustion
  on oversized collections, fixed only in zod 4) through the atproto OAuth
  libraries; in the browser it can at worst hang the user's own tab on a
  hostile server response. Revisit when the libraries move to zod 4.

### Maintainer notes

- `consentvid.org` is served by GitHub Pages and embedded in every manifest
  (licence URL, `cv:` namespace). Keep the domain **verified** under GitHub
  Settings → Pages → Verified domains (`_github-pages-challenge-<user>` TXT
  record), and remove the DNS records before ever unpublishing the site, so
  nobody else can claim it.
- `deploy-pages.yml` checks `web/dataset.json` before deploying and the file
  actually served afterwards (no local paths, https `contentUrl`s only).
- Workflow actions are pinned to commit SHAs; Dependabot proposes updates.

Licensed under Apache-2.0.
