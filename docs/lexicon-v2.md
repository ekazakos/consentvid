# Version 2: `org.consentvid.videoV2` and `org.consentvid.consentV2`

**Shipped (2026-11-07).** Version 2 exists because no `org.consentvid.*`
records existed on the network yet: the relay's
`com.atproto.sync.listReposByCollection` listed only the lexicon authority's
repo for both NSIDs, and `listRecords` there returned none. A clean break
cost nothing then. The version-1 schemas stay published and frozen (never
delete a published lexicon), and every reader still accepts V1 records:

- the CLI writes only V2 (`contribute`, `register-video`, `attach-consent`);
  `attach-consent` refuses a V1 video, because
- a consent binds only a video of its own version (a V2 consent's subject
  must be an `org.consentvid.videoV2` record; a V1 consent's `videoUri` an
  `org.consentvid.video` one);
- `verify`, `resync`, `export`, `embed-c2pa`, `revoke` and the AppView
  (Jetstream collections, `listReposByCollection`, backfill) read both;
- past validation, both versions become one internal view
  (`consentvid/versions.py`) in V1's field names and lowerCamelCase
  vocabulary, which is also what manifests and C2PA receipts contain, so a
  receipt reads the same whichever version was signed.

These items could not be applied to the V1 NSIDs, because each adds,
tightens or retypes a constraint (`lexicons.compat_errors` refuses them).
They follow the [lexicon style guide](https://atproto.com/guides/lexicon-style-guide)
and Bluesky's own schemas. How each landed:

1. **Strings limited in graphemes as well as bytes.** `title`:
   `maxGraphemes: 300`, `maxLength: 3000`; `description`: 3000 / 30000 (the
   1:10 pattern of `app.bsky.feed.post#text`). `validate.py` counts extended
   grapheme clusters with the `regex` package (`\X`).
2. **Every string bounded.** Storage values, local names, vocabulary strings
   (64 bytes), label values (128). Strings with a `format` (`cid`,
   `datetime`, `at-uri`, `language`) are bounded by it; a test checks that
   nothing else is unbounded.
3. **Tags like Bluesky's.** Items `maxGraphemes: 64`, `maxLength: 640`; at
   most 8.
4. **`langs`**: up to three `format: "language"` tags, primary first (like
   `app.bsky.feed.post#langs`), instead of one `language` string.
5. **`subject`: a `com.atproto.repo.strongRef`**, required, instead of
   `videoUri` + optional `videoCid`.
6. **`storage`: an open union** (`#r2Ref {url}`, `#ipfsRef {cid}`,
   `#peertubeRef {url}`, `#urlRef {url}`, `#localRef {name}`), URLs as
   `format: "uri"`. `#ipfsRef.cid` is *not* `format: "cid"`: IPFS CIDs are
   often CIDv0 or dag-pb, which atproto's `cid` format doesn't cover. A reader
   that doesn't know a member type treats the bytes as unreachable.
7. **Kebab-case values** (`only-me`, `open-weights`, `self-attested`, ...).
   Plain `knownValues` strings, not `token` defs: the style guide asks for
   kebab-case fixed strings and reserves tokens for values with subjective
   definitions that third parties extend, which these are not. The
   deprecated `researchOnly` / `foundationModel` are not in V2.
8. **`minimum: 0`, `maximum: 2^53-1`** on `durationMs`, `width`, `height`.
9. **No `revocable`** (every consent is withdrawable, so it carried no
   information). `authorshipAttested`, `adultAttested` and `peoplePresent`
   are required; the two attestations are `const: true`: they are signed
   affirmations, the reason the record exists, not configuration.
10. **`contentHash` is a CID**: the bytes' CIDv1, raw codec, sha-256
    (`bafkrei...`), exactly what a PDS blob ref carries. Readers reject other
    codecs.
11. **The sidecar rule and `key: "tid"` are kept** for both records.

Also new in `videoV2`, both optional:

- **`size`**: the byte size (as blob refs and Streamplace's tracks carry).
- **`labels`**: an open union of `com.atproto.label.defs#selfLabels`, the
  contributor's own labels (as on `app.bsky.feed.post`). `export`, `resync`
  and the AppView leave out clips labeled `porn`, `sexual`, `nudity` or
  `graphic-media` (Bluesky's global values) unless `--include-labeled`.

Publishing: `consentvid publish-lexicons` creates the two new schema records
(a new NSID has no published version to be compatible with) and updates the
V1 ones only within the evolution rules.

## Shipped compatible additions

- **`#bskyPostRef`** (storage union member, added 2026-11-08): `{post:
  com.atproto.repo.strongRef, blob: cid}`, the video of the contributor's own
  Bluesky post. An open union may gain members (`compat_errors` accepts it).
  Not a blob reference: the bytes' lifetime stays the post's. Readers
  require `post.uri` to be an `app.bsky.feed.post` in the same repo, `blob` to
  equal `contentHash`, and the post, read with a proof at `post.cid`, to embed
  that blob (`consentvid.bsky`; README, "Storage backends"). Readers that
  predate it treat such clips as unreachable, as for any unknown member.

## Possible compatible additions

These fit the evolution rules (`compat_errors` accepts them), so they can be
added to `org.consentvid.videoV2` without a new NSID. Not scheduled.

- **An optional PDS-blob storage kind** (R1-27, deferred): a new storage
  union member (e.g. `#blobRef {blob: {type: "blob", accept: ["video/*"],
  maxSize: ...}}`). Readers treat unknown members as unreachable, so today's
  readers ignore it safely. Readers that support it must check:
  - `blob.ref` equals `contentHash` (in V2 already the raw-codec CID), and
    `blob.size` equals `size`, or the blob doesn't bind to the consent;
  - `blob.mimeType` is equivalent to `mimeType`, through an alias table, not
    string equality: the PDS stores the type its file-type sniffing reports
    and requires blob refs to match it exactly, so a `.mkv` upload comes back
    as `video/matroska`, an `.avi` as `video/vnd.avi`. Equivalent pairs:
    {`video/matroska`, `video/x-matroska`}, {`video/vnd.avi`,
    `video/x-msvideo`}, {`video/x-m4v`, `video/mp4`}, {`video/MP1S`,
    `video/MP2P`, `video/mpeg`}. (Readers already accept `video/matroska` and
    `video/vnd.avi` as `mimeType`.) The writer copies `mimeType` from the
    `uploadBlob` response, not from the file name;
  - `accept` and `maxSize`: the PDS does **not** enforce them for lexicons it
    doesn't know (it validates only its bundled lexicons), so ConsentVid
    readers must;
  - the `applyWrites` that references the blob follows the upload promptly:
    the PDS garbage-collects unreferenced blobs after a grace period (at
    least an hour on the reference PDS);
  - deleting a record frees the blob only when no other record in the repo
    references the same CID (the PDS tracks references from records of any
    lexicon), so `revoke` should warn when another record still references
    it. Blob lifetime is tied to the account (deleted with it, unavailable
    while it is deactivated or taken down): see the README's "Why not PDS
    blobs?".
- **Permissioned data (atproto Spaces).** Consent records stay public on
  purpose: anyone can verify them with MST proofs, and relays make them
  discoverable. Spaces are alpha
  ([announcement](https://atproto.com/blog/atproto-spaces-alpha)), have no
  relay, provide access control rather than confidentiality, and the
  announcement says not to put sensitive information in them, so they must
  not hold consent data. Revisit after general availability, and only for
  genuinely private side channels (e.g. a contributor-to-researcher
  message), never for the consent itself. Either way, keep personal data out
  of public records.
