# ConsentVid

A consent-first video dataset platform on [ATproto](https://atproto.com): think
"Common Voice for video". Contributors publish videos with **signed, granular,
revocable consent receipts** in their own ATproto repository. Researchers build
[Croissant-RAI](http://mlcommons.org/croissant/) datasets from them and
**cryptographically re-verify** every clip's consent before each use.

- **Granular consent:** per clip × purpose × model class × modality × expiry.
- **Signed by the contributor:** records live in their own repo, signed by their key.
- **Revocable:** withdrawing consent means deleting the receipt. `resync` proves the
  deletion from the contributor's signed repo commit, so it trusts no server.
- **Non-custodial:** records commit to a SHA-256 of the bytes, and the bytes live
  on R2, IPFS, PeerTube or any URL.
- **Private by default:** GPS location and device metadata are stripped before
  anything is published.

## Install

Python 3.11–3.13 (the `atproto` SDK breaks on 3.14), plus `ffmpeg` for metadata
stripping (`brew install ffmpeg`).

```bash
python3.13 -m venv ~/.consentvid/venv && source ~/.consentvid/venv/bin/activate
pip install -e ".[r2,c2pa]"          # extras: r2, ipfs, peertube, c2pa, dev
cp .env.example .env                  # fill in what you need
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
    --attest-authorship \
    --purpose training --purpose evaluation \
    --model-class academic --model-class openWeights \
    --modality video --modality audio \
    --expires-at 2y

consentvid list                                   # your videos + their consent
consentvid attach-consent <video-uri> --replace … # change terms
consentvid revoke <consent-uri>                   # withdraw consent
consentvid revoke <video-uri> --delete-bytes      # withdraw everything + delete bytes
```

What `contribute` does:

1. **Strips identifying metadata** (GPS, device make/model/serial, Apple's
   location track) with a lossless stream copy. `--keep-metadata` opts out.
2. **Hashes** the clean bytes and refuses to register the same bytes twice.
3. **Validates** the record locally, *then* uploads. A record that fails
   validation never leaves orphaned bytes, and a failed record write deletes
   the upload.
4. Writes an `org.consentvid.video` record, then an `org.consentvid.consent`
   receipt **pinned to that exact video record version** (`videoCid`).

`--attest-authorship` is required. You affirm, under your signing key, that you
recorded the video (or hold the rights, `--rights-basis licensed`). Consenting
on behalf of someone else's video is unlawful, and every reader rejects a
consent that points at a video in another account's repo.

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
presence or absence, so neither the PDS nor any relay is trusted. Outcomes:

| status | meaning | dropped |
|---|---|---|
| `valid` | consent and video verified, unchanged since export | no |
| `revoked` | consent or video deleted (proven), or account deleted | yes |
| `expired` | past `expiresAt` | yes |
| `modified` | the consent or video changed since export, a newer consent supersedes it, or the manifest's terms, URL or hash disagree with the signed records | yes |
| `invalid` | fails validation or proof verification, or points at someone else's video | yes |
| `unattested` | below `--min-provenance` | yes |
| `unavailable` | account deactivated, suspended or taken down | yes |
| `unknown` | network error | only with `--strict` |

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
consentvid resync clip.c2pa.mp4      # re-verify from the file alone
```

The file must be exactly the registered bytes (the hash is checked). Signing
uses a self-signed prototype certificate under `~/.consentvid/c2pa/`, so readers
show the manifest as valid but the signer as untrusted. Production needs a
CA-issued certificate at the same paths. The stamped file's hash differs from
the registered hash: re-check it with `resync` (it carries the consent URI and
CIDs), not by its hash.

## Storage backends

| kind | revocable? | notes |
|---|---|---|
| `r2` | yes (`revoke --delete-bytes`) | content-addressed key, correct `Content-Type`, idempotent |
| `ipfs` | **no**: unpinning doesn't remove copies elsewhere | Pinata (`CONSENTVID_PINATA_JWT`) or local Kubo. The CID is a UnixFS DAG root, *not* the file's SHA-256 |
| `peertube` | yes | uploaded **unlisted** by default; served renditions are transcodes, so they won't match `contentHash` |
| `url` | your responsibility | must be `https://` |
| `local` | n/a | a bare file name only; never dereferenceable, so excluded from datasets |

## Trust model and limitations

- **Signed ≠ true.** A signature proves *who* attested, not that they filmed it.
  Uploads only get deterrence, detection and liability. The `attested`
  provenance tier is reserved for capture-time C2PA/CAWG signing that binds the
  recording to the contributor's DID.
- **The AppView is for discovery, not proof.** Jetstream strips signatures, so
  verify with `resync` before use.
- **Rollback:** a malicious PDS could serve an *older* signed commit to hide a
  deletion. Detecting that needs an independent witness, such as a relay.
- **Copies:** revocation can't reach copies that were already downloaded. The
  terms of use oblige consumers to resync and delete.
- **Jetstream** keeps roughly a day of replay. After a longer AppView outage,
  re-run `appview backfill`, which also detects deletions it missed.

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

Licensed under Apache-2.0.
