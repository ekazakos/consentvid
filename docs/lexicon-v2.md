# Checklist for a future `org.consentvid.*V2` lexicon

**Where things stand (2026-11-07).** No `org.consentvid.*` records exist on
the network: the relay's `com.atproto.sync.listReposByCollection` lists only
the lexicon authority's repo for both NSIDs, and `listRecords` there returns
no records. The V1 schemas are nevertheless frozen, by this project's own rule,
because they are *published* (as `com.atproto.lexicon.schema` records,
resolvable through `_lexicon.consentvid.org`) and other software may already
validate against them. The
[lexicon evolution rules](https://atproto.com/specs/lexicon#lexicon-evolution)
allow only compatible changes to a published NSID: new optional fields, new
`knownValues`, better descriptions. Whether the items below are worth a V2 is
an open decision (deferred as R3-16).

**The numbered items below can't be applied to the existing NSIDs.** Each one
adds, tightens or retypes a constraint, so it can only land in a new NSID (for
example `org.consentvid.videoV2`), with readers accepting both during a
transition. `consentvid publish-lexicons` refuses them on the current NSIDs
(see `lexicons.compat_errors`). Compatible additions are listed separately at
the end.

They follow the [lexicon style guide](https://atproto.com/guides/lexicon-style-guide)
and Bluesky's own schemas.

1. **Limit strings in graphemes as well as bytes.** `title` has only
   `maxLength: 300` (UTF-8 bytes), so a CJK title gets about 100 characters,
   and `description` (3000 bytes) the same. Bluesky's `app.bsky.feed.post#text`
   uses `maxGraphemes: 300` with `maxLength: 3000`; a profile description uses
   `maxGraphemes: 256` with `maxLength: 2560`. Use the same 1:10 pattern.
   Adopting `maxGraphemes` needs a real grapheme-cluster counter in
   `validate.py` first (e.g. the `regex` package's `\X`); today a test refuses
   any lexicon that uses it.
2. **Bound every string.** `storageRef.value`, `contentHash`, `language` and the
   consent's open-vocabulary strings (`compensation`, `rightsBasis`,
   `captureProvenance`, `peoplePresent`) have no or loose `maxLength`.
   Unbounded strings are a style-guide anti-pattern.
3. **Tags like Bluesky's.** `app.bsky.feed.post#tags`: items with
   `maxGraphemes: 64` and `maxLength: 640`, array `maxLength: 8`.
4. **`format: "language"`** for the spoken language, and an array like
   `app.bsky.feed.post#langs` (`maxLength: 3`) if several are allowed. Today
   `language` is a plain string whose BCP-47 syntax only our readers enforce.
5. **A `com.atproto.repo.strongRef` subject** instead of the flat
   `videoUri` + `videoCid` pair (as `app.bsky.feed.like#subject` does), and
   make it required in the schema: a consent must always pin the video
   version (readers already reject a consent without `videoCid`).
6. **An open union for storage** instead of the stringly-typed
   `storageRef {kind, value}`: one object type per backend
   (`#r2Ref {url}`, `#ipfsRef {cid}`, `#peertubeRef {url}`, `#urlRef {url}`,
   `#localRef {name}`), each with its own formats (`format: "uri"`,
   `format: "cid"` where it applies). A union lets readers skip kinds they
   don't know, and lets the schema state each kind's syntax. (A PDS-blob
   kind does not need to wait for this: see "Compatible additions" below.)
7. **Kebab-case tokens.** The style guide's convention for `knownValues` is
   kebab-case (`only-me`, `all-consented`, `open-weights`), not lowerCamelCase
   (`onlyMe`, `allConsented`, `openWeights`). Better still, declare them as
   `token` defs so they are namespaced and documented
   (`org.consentvid.defs#onlyMe`).
8. **`minimum: 0` and `maximum: 9007199254740991`** (2^53-1, the data
   model's limit for JavaScript readers) on `durationMs`, `width` and
   `height` (both bounds are reader rules today, stated in the descriptions).
9. **No always-true required booleans.** `revocable` is required but must
   always be `true` (consent must be withdrawable), so it carries no
   information. Drop it and say so in the description. Likewise make the
   attestations that readers require (`adultAttested`, `peoplePresent`)
   required in the schema instead of "optional in the schema but required by
   readers".
10. **`contentHash` as a CID.** `{type: "string", format: "cid"}` holding the
    CIDv1 raw-codec sha-256 of the bytes (`bafkrei...`, exactly what a PDS
    blob ref carries) instead of `sha256-<hex>`, which looks like SRI (that
    is base64) and is checked only by ConsentVid code. Generic validators
    check `format: cid`; no `maxLength` alongside the format.
    `records.raw_cid_from_sha256()` converts today's value.
11. **Keep the sidecar rule** (consent record key = video record key) and keep
    `key: "tid"` for both; state it in the description, as today.

When a V2 lands, readers must accept both versions, `export` should write the
new one, and the old NSIDs stay published (never delete a published lexicon).

## Compatible additions to the current NSIDs

These fit the evolution rules (`compat_errors` accepts them), so they can be
added to `org.consentvid.video` without a new NSID. Not scheduled.

- **An optional PDS-blob storage kind** (R1-27, deferred): an optional
  top-level `blob` property (`{type: "blob", accept: ["video/*"], maxSize:
  ...}`) plus a new `knownValue` `"pds"` for `storageRef.kind`.
  `storage_ref_errors` already treats unknown kinds as opaque, so today's
  readers ignore it safely. Readers that support it must check:
  - `blob.ref` equals the raw-codec CID of `contentHash`
    (`records.raw_cid_from_sha256`), and `blob.size` equals the file size, or
    the blob doesn't bind to the consent;
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
