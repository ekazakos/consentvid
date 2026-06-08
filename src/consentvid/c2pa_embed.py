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
flags the signer as untrusted. Production must use a CA-issued document-signing
certificate; swap the PEMs at the paths below and nothing else changes.
"""
from __future__ import annotations

import json
from pathlib import Path

CONSENT_ASSERTION_LABEL = "org.consentvid.consent"
_SIGNER_DIR = Path.home() / ".consentvid" / "c2pa"
_CERT_PATH = _SIGNER_DIR / "signer.cert.pem"
_KEY_PATH = _SIGNER_DIR / "signer.key.pem"

_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}


def _require_c2pa():
    try:
        import c2pa  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            'c2pa-python is required. Install with: pip install -e ".[c2pa]"'
        ) from e
    return c2pa


def mime_for(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


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
        raise RuntimeError(
            'cryptography is required to generate a signer. Install with: pip install -e ".[c2pa]"'
        ) from e
    import datetime

    # Fixed validity window: keeps the prototype cert deterministic.
    not_before = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    not_after = datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc)

    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ConsentVid Prototype Root CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ConsentVid"),
    ])
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
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False
        )
        .sign(root_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ConsentVid Prototype Signer"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ConsentVid"),
    ])
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
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )

    _SIGNER_DIR.mkdir(parents=True, exist_ok=True)
    chain_pem = leaf_cert.public_bytes(serialization.Encoding.PEM) + root_cert.public_bytes(
        serialization.Encoding.PEM
    )
    _CERT_PATH.write_bytes(chain_pem)
    _KEY_PATH.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    _KEY_PATH.chmod(0o600)
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

    return c2pa.Signer.from_callback(
        _sign, c2pa.C2paSigningAlg.ES256, certs_pem, tsa_url=None
    )


def build_manifest_definition(
    *,
    video_path: Path,
    consent_uri: str,
    consent: dict,
    video_uri: str,
    video: dict,
    contributor_handle: str,
    contributor_did: str,
) -> dict:
    """Assemble the C2PA manifest definition with the consent assertion."""
    receipt = {
        "consentUri": consent_uri,
        "videoUri": video_uri,
        "contentHash": video.get("contentHash"),
        "contributor": {"handle": contributor_handle, "did": contributor_did},
        "purposes": consent.get("purposes", []),
        "modelClasses": consent.get("modelClasses", []),
        "modalities": consent.get("modalities", []),
        "compensation": consent.get("compensation", "none"),
        "revocable": consent.get("revocable", False),
        "expiresAt": consent.get("expiresAt"),
        "createdAt": consent.get("createdAt"),
        "revocationNote": (
            "Authoritative consent is the ATproto record at consentUri. If that "
            "record is deleted, consent is revoked; re-check before any use."
        ),
    }
    return {
        "claim_generator_info": [{"name": "consentvid", "version": "0.0.1"}],
        "title": video.get("title") or video_path.name,
        "format": mime_for(video_path),
        "assertions": [
            {"label": CONSENT_ASSERTION_LABEL, "data": receipt},
            {
                "label": "stds.schema-org.CreativeWork",
                "data": {
                    "@context": "https://schema.org",
                    "@type": "CreativeWork",
                    "author": [
                        {
                            "@type": "Person",
                            "name": contributor_handle,
                            "identifier": contributor_did,
                        }
                    ],
                },
            },
            {
                "label": "c2pa.actions",
                "data": {
                    "actions": [
                        {"action": "c2pa.created", "softwareAgent": "consentvid/0.0.1"}
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
    consent: dict,
    video_uri: str,
    video: dict,
    contributor_handle: str,
    contributor_did: str,
) -> Path:
    """Embed the consent receipt as a signed C2PA manifest, writing out_path."""
    c2pa = _require_c2pa()
    manifest = build_manifest_definition(
        video_path=video_path,
        consent_uri=consent_uri,
        consent=consent,
        video_uri=video_uri,
        video=video,
        contributor_handle=contributor_handle,
        contributor_did=contributor_did,
    )
    signer = build_signer()
    builder = c2pa.Builder.from_json(json.dumps(manifest))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()  # c2pa won't overwrite an existing destination
    builder.sign_file(str(video_path), str(out_path), signer)
    return out_path


def read_manifest(path: Path) -> dict:
    """Read the embedded C2PA manifest back out as a dict (for verification)."""
    c2pa = _require_c2pa()
    reader = c2pa.Reader(str(path))
    return json.loads(reader.json())


def extract_consent_receipt(path: Path) -> dict | None:
    """Return the org.consentvid.consent assertion data from an embedded file,
    or None if the file carries a C2PA manifest without that assertion."""
    man = read_manifest(path)
    active = man.get("manifests", {}).get(man.get("active_manifest"), {})
    for assertion in active.get("assertions", []):
        if assertion.get("label") == CONSENT_ASSERTION_LABEL:
            return assertion.get("data")
    return None
