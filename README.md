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
python -m venv .venv && source .venv/bin/activate
pip install -e .
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

- Croissant-RAI manifest exporter: turn a query over consent records into a dataset card researchers can cite.
- AppView: index the firehose for `org.consentvid.*` records across users.
- Revocation feed.
- C2PA manifest embedded in the file at register-video time.
- Pluggable storage backends actually wired up (R2 upload, IPFS pin, PeerTube push).
