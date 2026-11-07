"""C2PA in-file provenance: embed a ConsentVid consent receipt into the video bytes.

ATproto records live in the contributor's repo; once a video file leaves the
platform the link to its signed consent is lost. C2PA (Content Provenance and
Authenticity, ISO 22144) fixes that by embedding a signed manifest *inside* the
media file. We write a custom ``org.consentvid.consent`` assertion carrying the
consent terms plus the at:// URIs of both the consent receipt and the video
record, so a downstream holder can recover provenance — and re-sync revocation
via :mod:`consentvid.resync` — from the file alone.

C2PA and cryptography are optional; install with ``pip install -e ".[c2pa]"``.

Signing requires an X.509 certificate. For the prototype we generate a
self-signed ES256 signer cached under ~/.consentvid/c2pa/. Self-signed manifests
embed and read back fine but will NOT chain to the C2PA trust list — a reader
flags the signer as untrusted.

What an embedded receipt proves: nothing about the file. It is a *pointer* to
the consent record, not evidence that this file is the consented bytes. Any
C2PA signer (self-signed or CA-trusted) can stamp anyone's public receipt onto
other video, the signer is not the contributor's DID key, and embedding changes
the bytes so the file can never match the signed contentHash. ``consentvid
resync`` therefore reports a C2PA file as UNBOUND (dropped) even when the
consent stands, and points at the registered bytes (by sha256) to use instead.
Binding a file to a consent would need the contributor's own key in the
signature (e.g. CAWG identity assertions tied to the DID); a CA-issued
certificate alone does not provide it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from consentvid import __version__
from consentvid.errors import ConfigError, ConsentVidError
from consentvid.paths import secure_dir, write_private
from consentvid.records import mime_for as _mime
from consentvid.records import normalize_dt, now_iso

CONSENT_ASSERTION_LABEL = "org.consentvid.consent"
_SIGNER_DIR = Path(os.environ.get("CONSENTVID_HOME", Path.home() / ".consentvid")) / "c2pa"
_CERT_PATH = _SIGNER_DIR / "signer.cert.pem"
_KEY_PATH = _SIGNER_DIR / "signer.key.pem"


# Bundled c2pa-rs releases before 0.85.1 are affected by CVE-2026-34712
# (Adobe APSB26-61, DoS on crafted input); we parse untrusted files.
MIN_SDK_VERSION = (0, 85, 1)


def _sdk_version(c2pa) -> tuple[int, ...] | None:
    try:
        v = c2pa.sdk_version()
    except Exception:  # noqa: BLE001
        return None
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", str(v))
    return tuple(int(x) for x in m.groups()) if m else None


def _require_c2pa():
    try:
        import c2pa  # noqa: F401
    except ImportError as e:
        raise ConfigError('c2pa-python is required: pip install "consentvid[c2pa]"') from e
    v = _sdk_version(c2pa)
    if v is None or v < MIN_SDK_VERSION:
        need = ".".join(map(str, MIN_SDK_VERSION))
        raise ConfigError(
            f"c2pa-python bundles c2pa-rs {'.'.join(map(str, v)) if v else 'of unknown version'}, older than "
            f"{need} (CVE-2026-34712, APSB26-61): upgrade with pip install -U \"c2pa-python>=0.32.11\""
        )
    return c2pa


def mime_for(path: Path) -> str:
    return _mime(path) or "application/octet-stream"


def ensure_signer_cert() -> tuple[Path, Path]:
    """Generate a cached ES256 signing credential if absent.

    Returns (cert_chain_path, key_path). c2pa-rs rejects a lone self-signed
    end-entity cert ("certificate is invalid"); it needs a chain. We mint a
    self-signed root CA and a leaf signing cert issued by it, and write the
    chain (leaf then root) to cert_chain_path — mirroring the c2pa test
    fixtures. The leaf carries keyUsage=digitalSignature + an emailProtection
    EKU, as C2PA requires of an end-entity signer.
    """
    if _CERT_PATH.exists() and _KEY_PATH.exists():
        return _CERT_PATH, _KEY_PATH
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as e:
        raise ConfigError('cryptography is required: pip install "consentvid[c2pa]"') from e
    import datetime

    # Fixed validity window: keeps the prototype cert deterministic.
    not_before = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
    not_after = datetime.datetime(2035, 1, 1, tzinfo=datetime.UTC)

    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "ConsentVid Prototype Root CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ConsentVid"),
        ]
    )
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "ConsentVid Prototype Signer"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ConsentVid"),
        ]
    )
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )

    # Owner-only from the start: CONSENTVID_HOME may not exist yet (no login).
    secure_dir(_SIGNER_DIR.parent)
    secure_dir(_SIGNER_DIR)
    chain_pem = leaf_cert.public_bytes(serialization.Encoding.PEM) + root_cert.public_bytes(
        serialization.Encoding.PEM
    )
    _CERT_PATH.write_bytes(chain_pem)
    write_private(
        _KEY_PATH,
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return _CERT_PATH, _KEY_PATH


def build_signer():
    """Build an offline c2pa Signer backed by the cached cert chain.

    Uses ``Signer.from_callback`` (not ``from_info``) so we can sign without a
    timestamp-authority URL — ``from_info`` rejects an empty ``ta_url``. The
    callback signs with ECDSA/P-256/SHA-256 and returns the raw r‖s pair that
    COSE ES256 expects (cryptography emits DER, so we convert).
    """
    c2pa = _require_c2pa()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    cert_path, key_path = ensure_signer_cert()
    private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    certs_pem = cert_path.read_text()

    def _sign(data: bytes) -> bytes:
        der = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return c2pa.Signer.from_callback(_sign, c2pa.C2paSigningAlg.ES256, certs_pem, tsa_url=None)


SOURCE_INGREDIENT_LABEL = "consentvid-source"

def build_manifest_definition(
    *,
    video_path: Path,
    consent_uri: str,
    consent_cid: str,
    consent: dict,
    video_uri: str,
    video_cid: str,
    video: dict,
    contributor_handle: str | None,
    contributor_did: str,
) -> dict:
    """Assemble the C2PA manifest definition with the consent assertion.

    ``contributor_handle`` must be the DID's bidirectionally verified handle,
    or None: handles are mutable display names, the DID is the identifier,
    and a DID is never put in a field called 'handle'."""
    contributor = {"did": contributor_did}
    author = {"@type": "Person", "name": contributor_handle or contributor_did, "identifier": contributor_did}
    if contributor_handle:
        contributor["handle"] = contributor_handle
        contributor["handleVerifiedAt"] = now_iso()
        author["alternateName"] = contributor_handle
    receipt = {
        "consentUri": consent_uri,
        "consentCid": consent_cid,
        "videoUri": video_uri,
        "videoCid": video_cid,
        "contentHash": video.get("contentHash"),
        "contributor": contributor,
        "purposes": consent.get("purposes", []),
        "modelClasses": consent.get("modelClasses", []),
        "modalities": consent.get("modalities", []),
        "compensation": consent.get("compensation", "none"),
        "revocable": consent.get("revocable", False),
        "authorshipAttested": consent.get("authorshipAttested", False),
        "rightsBasis": consent.get("rightsBasis"),
        "captureProvenance": consent.get("captureProvenance", "unverified"),
        "peoplePresent": consent.get("peoplePresent"),
        "adultAttested": consent.get("adultAttested", False),
        "expiresAt": normalize_dt(consent.get("expiresAt")),
        "createdAt": normalize_dt(consent.get("createdAt")),
        "revocationNote": (
            "Authoritative consent is the ATproto record at consentUri. If that "
            "record is deleted, consent is revoked; re-check before any use."
        ),
    }
    return {
        "claim_generator_info": [{"name": "consentvid", "version": __version__}],
        "title": video.get("title") or video_path.name,
        "format": mime_for(video_path),
        "assertions": [
            {"label": CONSENT_ASSERTION_LABEL, "data": receipt},
            {
                "label": "stds.schema-org.CreativeWork",
                "data": {
                    "@context": "https://schema.org",
                    "@type": "CreativeWork",
                    "author": [author],
                },
            },
            {
                "label": "c2pa.actions",
                "data": {
                    # We stamp an existing file, so the honest action is
                    # "opened" with that file as the parent ingredient, not
                    # "created" (which would need a digitalSourceType claim
                    # about how the footage was made that we can't back).
                    "actions": [
                        {
                            "action": "c2pa.opened",
                            "softwareAgent": f"consentvid/{__version__}",
                            "parameters": {"ingredientIds": [SOURCE_INGREDIENT_LABEL]},
                        }
                    ]
                },
            },
        ],
    }


def embed_consent(
    *,
    video_path: Path,
    out_path: Path,
    consent_uri: str,
    consent_cid: str,
    consent: dict,
    video_uri: str,
    video_cid: str,
    video: dict,
    contributor_handle: str | None,
    contributor_did: str,
) -> Path:
    """Embed the consent receipt as a signed C2PA manifest, writing out_path."""
    c2pa = _require_c2pa()
    manifest = build_manifest_definition(
        video_path=video_path,
        consent_uri=consent_uri,
        consent_cid=consent_cid,
        consent=consent,
        video_uri=video_uri,
        video_cid=video_cid,
        video=video,
        contributor_handle=contributor_handle,
        contributor_did=contributor_did,
    )
    signer = build_signer()
    builder = c2pa.Builder.from_json(json.dumps(manifest))
    from consentvid.records import mime_for

    with open(video_path, "rb") as src:
        builder.add_ingredient(
            {"title": "source video", "relationship": "parentOf", "label": SOURCE_INGREDIENT_LABEL},
            mime_for(video_path) or "video/mp4",
            src,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Never write through or replace anything at the (predictable) output
    # path, including a dangling symlink: sign into a private directory and
    # publish exclusively.
    if os.path.lexists(out_path):
        raise ConsentVidError(f"{out_path} already exists; remove it first")
    from consentvid.media import publish_file
    from consentvid.paths import secure_dir

    home = secure_dir(Path(os.environ.get("CONSENTVID_HOME", Path.home() / ".consentvid")))
    with tempfile.TemporaryDirectory(dir=home, prefix="c2pa-") as wd:
        tmp = Path(wd) / ("out" + out_path.suffix)
        builder.sign_file(str(video_path), str(tmp), signer)
        publish_file(tmp, out_path)
    return out_path


def read_manifest(path: Path) -> dict:
    """Read the embedded C2PA manifest back out as a dict (for verification)."""
    c2pa = _require_c2pa()
    reader = c2pa.Reader(str(path))
    return json.loads(reader.json())


def extract_consent_receipt(path: Path) -> tuple[dict | None, str]:
    """Return (org.consentvid.consent assertion data or None, validation state).

    The state is c2pa's verdict on the file: "Trusted" (signer chains to the
    trust list), "Valid" (intact, but signer untrusted) or "Invalid" (bytes or
    manifest altered after signing). Callers must not trust a receipt from an
    Invalid file.
    """
    man = read_manifest(path)
    state = man.get("validation_state") or ("Invalid" if man.get("validation_status") else "unknown")
    active = man.get("manifests", {}).get(man.get("active_manifest"), {})
    for assertion in active.get("assertions", []):
        if assertion.get("label") == CONSENT_ASSERTION_LABEL:
            return assertion.get("data"), state
    return None, state
