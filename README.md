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
- **Private by default:** container metadata (GPS location, device, titles,
  comments, XMP, vendor boxes), cover art, in-band H.264/HEVC SEI and AV1
  metadata are stripped before anything is published, whatever the storage kind.

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
consentvid login        # handle + app password (bsky.app/settings/app-passwords)

# Register a clip and consent in one step:
consentvid contribute clip.mp4 --title "Making pasta" --tag cooking --language en \
    --attest-authorship --attest-adult --people onlyMe \
    --purpose training --purpose evaluation \
    --model-class academic --model-class openWeights \
    --modality video --modality audio \
    --expires-at 2y

consentvid list                                   # your videos + their consent
consentvid attach-consent <video-uri> --replace … # change terms
consentvid revoke <consent-uri>                   # withdraw consent (--all: every consent for that video)
consentvid revoke <video-uri> --delete-bytes      # withdraw everything + delete bytes
```

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
3. **Validates** the record locally, *then* uploads. A record that fails
   validation never leaves orphaned bytes, and a failed record write deletes
   the upload.
4. Writes an `org.consentvid.video` record, then an `org.consentvid.consent`
   receipt **pinned to that exact video record version** (`videoCid`).

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

**Vocabulary.** `--purpose` says what the data may be used *for* (training,
evaluation, benchmarking). `--model-class` says *who* may use it (academic,
openWeights, commercial). `researchOnly` and `foundationModel` are deprecated,
because they mixed the two questions: existing records using them stay valid,
but the CLI no longer offers them. `--compensation` records what you expect in
return. ConsentVid doesn't pay or enforce it.

Consent is always revocable: consent to process personal data must be
withdrawable (GDPR Art. 7(3)). Each video has one consent receipt, and
`--replace` updates it in place, which downstream `resync` reports as *modified*.

## Researchers

No account needed.

```bash
# One contributor's clips (filters are ALL-of: the clip must grant every one)
consentvid export ds.json --repo alice.bsky.social --purpose training --model-class commercial

# Or across the network, via a local AppView index
consentvid appview backfill alice.bsky.social bob.example.com
consentvid appview index                       # follow the firehose (resumable)
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
| `modified` | the consent or video changed since export, a newer consent supersedes it (even an invalid one: the contributor's newest record governs, and an invalid one grants nothing), or the manifest's terms, URL, `encodingFormat` or hash disagree with the signed records | yes |
| `invalid` | fails validation or proof verification, or points at someone else's video | yes |
| `unattested` | below `--min-provenance` | yes |
| `unavailable` | account deactivated, suspended or taken down | yes |
| `unbound` | a C2PA-stamped file: the consent stands, but nothing binds *this file's* bytes to it | yes |
| `unknown` | network error (only transport errors and 5xx/429; a malformed or rejected answer, or a TLS failure, is `invalid`) | yes (default `--strict`); `--lenient` keeps it unverified, so only use `--lenient` on manifests you built yourself (a contributor's PDS can make its clips uncheckable) |

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
| `local` | n/a | `<contentHash>.<ext>` (never your file name, which can carry the capture time and device); never dereferenceable, so excluded from datasets |

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
  deletion. Detecting that needs an independent witness, such as a relay.
- **Hosts named by contributors.** DID documents, handles and PDS endpoints
  are chosen by contributors; `resync`/`verify` only contact public `https://`
  hosts (no IP literals, `localhost`, `.local`, `.internal`) and don't follow
  redirects.
- **Models trained before a revocation** stay legitimate, provided they were
  trained in good faith on a manifest that `resync` showed as valid at the time
  (GDPR Art. 7(3): withdrawal doesn't affect earlier lawful processing).
  Revocation stops all *new* use from that point, including retraining,
  fine-tuning and new evaluations. Re-sync before every run.
- **Copies:** revocation can't reach copies that were already downloaded. The
  terms of use oblige consumers to resync and delete.
- **Jetstream** keeps roughly a day of replay. After a longer AppView outage,
  re-run `appview backfill`, which also detects deletions it missed. A busy
  database never skips an event: the indexer waits, reconnects and replays it.
- **Clocks:** a consent whose `createdAt` is more than a day in the future is
  invalid (it could never be superseded). Because the newest record governs,
  such a record makes the video's clips unusable until it is deleted.
- **DNS rebinding:** host names are checked against private address ranges
  when resolved, but a name could resolve differently by the time we connect.

## Lexicons

`org.consentvid.video` and `org.consentvid.consent` ship in
`src/consentvid/schemas/` (mirrored in `web/lexicons/`, and a test keeps the two
identical). They are published as `com.atproto.lexicon.schema` records and
resolvable through `_lexicon.consentvid.org`:

```bash
consentvid publish-lexicons             # after changing a schema
consentvid resolve-lexicon org.consentvid.consent   # compares published vs local
```

Changes follow the [lexicon evolution rules](https://atproto.com/specs/lexicon):
add only optional fields, never tighten or repurpose existing ones; a breaking
change needs a new NSID. bsky.social doesn't resolve third-party lexicons on
write, so by default records are written with the PDS's own ("optimistic")
validation policy. ConsentVid always validates locally before writing and on
every read path. `CONSENTVID_VALIDATE=1` forces strict PDS validation, which
fails on bsky.social.

## Development

```bash
pip install -e ".[dev]"
pytest                    # offline; real CAR proofs are fixtures in tests/fixtures
pytest -m network         # also hits the live network
ruff check src tests && ruff format src tests
```

Layout: `records` (URIs, datetimes, hashing) · `validate` (lexicon + domain
rules) · `identity` (DID/handle → PDS, key) · `verify` (CAR/MST/signature
proofs) · `media` (probe + metadata stripping) · `storage` · `policy` ·
`croissant` · `resync` · `appview` · `c2pa_embed` · `cli`.

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
