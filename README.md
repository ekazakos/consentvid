# consentvid

Consent-first video dataset platform on ATproto. Prototype.

## What this MVP does

- Defines two custom ATproto lexicons:
  - `org.consentvid.video` — a video record (title, content hash, duration, dimensions, tags, storage pointer).
  - `org.consentvid.consent` — a consent receipt (modalities, purposes, model classes, expiry, compensation, revocable).
- Provides a CLI to create both kinds of records on your existing Bluesky PDS, list them, and revoke them.

Storage of the bytes is intentionally decoupled. The `video` record commits to a SHA-256 hash and a pointer; the bytes can live anywhere (local, URL, IPFS, R2). This avoids fighting bsky.social's blob limits.

## Caveats

- `org.consentvid.*` is a placeholder NSID. Before any production use, replace with a domain you control (e.g. `org.consentvid.*`) and publish lexicons at a resolvable URL.
- Bluesky's PDS may not validate records against unresolvable lexicons. The CLI passes `validate: false` to be safe.
- Revoking a record removes it from your repo; copies already collected into downstream datasets are unaffected. A revocation feed that dataset consumers must re-sync is future work.

## Setup

```bash
cd /Users/ekazakos/Documents/consentvid
# NOTE: this repo lives under ~/Documents, which is iCloud-synced. iCloud
# offloads ("dataless") a virtualenv's small files non-deterministically, which
# silently breaks editable installs (ModuleNotFoundError from an evicted .pth).
# Keep the venv OUTSIDE iCloud:
python3.13 -m venv ~/.consentvid/venv && source ~/.consentvid/venv/bin/activate
pip install -e ".[c2pa]"   # add ,r2 and/or ,dev as needed
cp .env.example .env
```

Generate a Bluesky **app password** (not your account password) at <https://bsky.app/settings/app-passwords> and put it in `.env`:

```
CONSENTVID_HANDLE=ekazakos.bsky.social
CONSENTVID_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

## Usage

```bash
consentvid login
consentvid whoami

# Register a video (computes SHA-256, optionally extracts duration/dims via ffprobe)
consentvid register-video /path/to/clip.mp4 \
    --title "Demo clip" \
    --tag cooking --tag egocentric

# Returns an at:// URI for the video record. Use it to attach consent:
consentvid attach-consent at://did:plc:.../org.consentvid.video/<rkey> \
    --modality video --modality audio \
    --purpose training --purpose evaluation \
    --model-class academic --model-class openWeights \
    --compensation none

# Browse records
consentvid list --collection org.consentvid.consent
consentvid list --collection org.consentvid.video

# Export a Croissant-RAI dataset manifest from this user's consents
consentvid export ./manifest.json
consentvid export ./training-only.json --purpose training --tag cooking

# Revoke
consentvid revoke at://did:plc:.../org.consentvid.consent/<rkey>
```

## Revocation re-sync

Revocation = deleting the consent receipt from the contributor's repo. That
alone doesn't reach copies already pulled into datasets, so consumers re-sync.
Two paths:

```bash
# Consumer side: re-check every consent receipt embedded in a manifest against
# the live network; report (and optionally drop) revoked or expired clips.
consentvid resync ./manifest.json
consentvid resync ./manifest.json --write ./manifest.clean.json

# Producer side (requires a running AppView, below): emit the revoked-consent
# feed. Poll with --since <last-seen> for incremental re-sync.
consentvid appview revocations
consentvid appview revocations --since 2026-04-29T00:00:00Z --json
```

`resync` is authoritative against the contributor's repo (a `getRecord` →
`RecordNotFound` means revoked) and needs no AppView. Expiry is read from the
receipt's signed `expiresAt`. Clips whose status can't be determined (network
error) are reported `unknown` and never dropped — fail-open on transient
errors, never on a revocation.

## C2PA in-file provenance

ATproto records live in the contributor's repo; once a file leaves the platform
the link to its signed consent is lost. `embed-c2pa` writes a signed C2PA
manifest (ISO 22144) *into the video bytes*, carrying the consent terms plus the
at:// URIs of both the consent receipt and the video record. A downstream holder
can then recover provenance — and re-check revocation with `resync` — from the
file alone.

```bash
pip install -e ".[c2pa]"

consentvid embed-c2pa ./clip.mp4 \
    --consent at://did:plc:.../org.consentvid.consent/<rkey> \
    --output ./clip.c2pa.mp4

consentvid read-c2pa ./clip.c2pa.mp4   # read the embedded consent receipt back
```

Signing uses a self-signed ES256 credential generated and cached under
`~/.consentvid/c2pa/` (a root CA + leaf chain, since c2pa-rs rejects a lone
self-signed end-entity cert). Manifests embed and read back as `Valid`, but the
signer will not chain to the C2PA trust list — a reader flags it untrusted.
Production must swap in a CA-issued document-signing certificate at those paths;
nothing else changes.

## Storage backends

ConsentVid is **non-custodial**: the video record commits only to a content hash
and a `storageRef` pointer — the platform never has to host the bytes. That's the
economic core (kilobyte records, not petabytes of video) and it keeps liability
with the contributor, not you. Backends are pluggable; pass `--storage-kind` and
omit `--storage-value` to upload:

```bash
# Cloudflare R2 (S3-compatible, zero egress). Needs the CONSENTVID_R2_* env vars.
consentvid register-video clip.mp4 --title "…" --storage-kind r2

# IPFS. The CID is content-addressed, so it matches the record's SHA-256 by
# construction. Persistence needs a pin: set CONSENTVID_PINATA_JWT for a hosted
# pinning service, or run a local Kubo node (CONSENTVID_IPFS_API). Note: IPFS is
# not free-forever storage — unpinned content is garbage-collected.
consentvid register-video clip.mp4 --title "…" --storage-kind ipfs

# PeerTube (federated video host: transcoding + adaptive + P2P delivery). Needs
# CONSENTVID_PEERTUBE_URL / _USERNAME / _PASSWORD.
consentvid register-video clip.mp4 --title "…" --storage-kind peertube
```

Manifests dereference each backend automatically (an IPFS CID becomes a
`CONSENTVID_IPFS_GATEWAY` URL; r2/peertube/url pass through). See `.env.example`
for all backend variables.

## Publishing lexicons (schema validation)

Until the `org.consentvid.*` lexicons are *resolvable* on the network, records
are written with `validate:false` — the PDS can't find the schema to check
against. Publishing them makes `org.consentvid.*` a first-class, validated
namespace instead of a placeholder.

```bash
# 1. Write each lexicon as a com.atproto.lexicon.schema record (rkey = the NSID).
consentvid publish-lexicons

# 2. Add the DNS TXT record it prints, e.g.:
#      _lexicon.consentvid.org  IN TXT  "did=did:plc:…"

# 3. Verify resolution (DNS TXT -> DID -> schema record):
consentvid resolve-lexicon org.consentvid.consent
consentvid resolve-lexicon org.consentvid.consent --did did:plc:…  # skip DNS

# 4. Validate on write (see caveat below):
export CONSENTVID_VALIDATE=1        # or pass --validate to register-video/attach-consent
```

**Caveat (verified 2026-07-05):** even with the lexicons published and the DNS
record propagated globally, `validate:true` fails against bsky.social with
`Unknown lexicon type: org.consentvid.*`. This is **by design, not a gap**: the
[AT Protocol Lexicon spec](https://atproto.com/specs/lexicon) defines three PDS
validation modes — *explicit-required* (fail if the lexicon can't be resolved),
*optimistic* (the default: validate if known, otherwise allow), and *no
validation* — and makes **on-demand resolution of third-party lexicons optional**
for a PDS. bsky.social runs the optimistic default and does not resolve external
lexicons on write, so `validate:true` (explicit-required) fails there and may
never work on bsky.social regardless of our setup.

Keep `CONSENTVID_VALIDATE=0` — it corresponds to the spec's *optimistic* default
and is the correct setting. Publishing the lexicons is still worthwhile: they are
resolved and consumed by ecosystem tooling — `pdsls.dev`,
`@atproto/lexicon-resolver`, the Rust `atproto-lexicon` crate, and our own
`resolve-lexicon` — so `org.consentvid.*` is a real, discoverable namespace even
though bsky.social won't enforce it at write time.

ATproto resolves a lexicon by domain authority: `org.consentvid.consent` →
`consentvid.org` → the `_lexicon.consentvid.org` TXT record → a DID whose repo
holds the schema records. For production, point the authority at a dedicated DID
rather than a personal bsky.social account. (DNS-based lexicon resolution is a
young part of the protocol — verify the record shape against the current spec.)

The exported manifest conforms to [Croissant 1.0](http://mlcommons.org/croissant/)
with the [RAI extension](http://mlcommons.org/croissant/RAI/). Each `cr:FileObject`
embeds a `cr:consentReceipt` with the signed ATproto URI of the consent record,
so a researcher citing the dataset has end-to-end provenance from row → signed
receipt. Validate with `pip install mlcroissant && mlcroissant validate manifest.json`.

## AppView — federated indexing across users

The CLI also includes a Jetstream-based AppView that indexes `org.consentvid.*`
records from any contributor on the network into a local SQLite db at
`~/.consentvid/appview.db`.

```bash
# Backfill an existing user's records (run before live indexing).
consentvid appview backfill ekazakos.bsky.social

# Subscribe to the firehose and index in real time.
consentvid appview index               # forever
consentvid appview index --stop-after 5  # process 5 events then exit

# Query the index.
consentvid appview stats
consentvid appview query --purpose training --model-class academic
consentvid appview query --include-revoked

# A consent record deleted on its home PDS is broadcast on the firehose;
# the AppView marks it revoked in the local db. Default queries exclude
# revoked records; pass --include-revoked to see them.
```

Jetstream is a JSON-formatted firehose hosted by Bluesky at
`wss://jetstream2.us-east.bsky.network/subscribe`. Subscribing with
`?wantedCollections=org.consentvid.video&wantedCollections=org.consentvid.consent`
restricts the stream to our two record types.

### Hosting lexicons at consentvid.org

For the prototype, records are written with `validate: false` and the
`org.consentvid.*` lexicons are not yet resolvable on the network. To make them
resolvable, publish the lexicon JSON at the well-known location for the
appropriate NSID under consentvid.org. (See the AT Protocol lexicon-resolution
spec; this is a separate operational step that doesn't affect the CLI.)

`ffprobe` (from ffmpeg) is optional — used to extract `durationMs`, `width`, `height` if present.

### Python version

**Use Python 3.13 or earlier.** The `atproto` package (0.0.65 at time of
writing) has a circular import that surfaces under Python 3.14's stricter
import semantics. The error looks like:

```
ImportError: cannot import name 'string_formats' from partially initialized
module 'atproto_client.models'
```

The pyproject is set to require >=3.10. The development venv was built with
`python3.13 -m venv .venv`.

## What's next (after this MVP works)

Done: Croissant-RAI exporter, AppView indexer, R2 / IPFS / PeerTube storage
backends, revocation re-sync (`resync` + `appview revocations`), C2PA in-file
provenance (`embed-c2pa`), lexicon publishing (`publish-lexicons`).

Remaining:
- C2PA with a CA-issued (trust-list-chaining) certificate instead of the
  self-signed prototype signer.
- Add the `_lexicon.consentvid.org` DNS TXT record so lexicons resolve
  network-wide, then flip `CONSENTVID_VALIDATE=1` (records are published; only
  the DNS step and a dedicated authority DID remain).
