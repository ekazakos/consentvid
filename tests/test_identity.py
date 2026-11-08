"""Identity resolution: SSRF guards (R1-18) and deleted DIDs (R1-19). Offline."""

from types import SimpleNamespace

import httpx
import pytest
from atproto_core.did_doc import DidDocument
from atproto_identity.exceptions import DidPlcResolverError

from conftest import ALICE, K1
from consentvid import identity
from consentvid import resync as rs
from consentvid.errors import RepoUnavailable

KEY = "zQ3shXjHeiBuRCKmM36cuYnm7YEMzhGnCmCyW92sRJ9pribSF"
PUBLIC_IP = "93.184.215.14"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Offline DNS: every name resolves to a public address unless a test
    says otherwise (R2-11: names resolving to private addresses)."""
    import socket

    table = {}

    def getaddrinfo(host, port, *a, **k):
        ip = table.get(host, PUBLIC_IP)
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return table


def doc(pds="https://pds.example.com", handle="alice.example.com", did=ALICE):
    d = {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": did,
        "verificationMethod": [
            {"id": f"{did}#atproto", "type": "Multikey", "controller": did, "publicKeyMultibase": KEY}
        ],
        "service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": pds}],
    }
    if handle:
        d["alsoKnownAs"] = [f"at://{handle}"]
    return DidDocument.from_dict(d)


@pytest.fixture
def resolver(monkeypatch):
    state = SimpleNamespace(doc=None, exc=None, handle_calls=[])

    def resolve(did):
        if state.exc:
            raise state.exc
        return state.doc

    def resolve_handle(h):
        state.handle_calls.append(h)
        return ALICE

    fake = SimpleNamespace(did=SimpleNamespace(resolve=resolve), handle=SimpleNamespace(resolve=resolve_handle))
    monkeypatch.setattr(identity, "_resolver", lambda: fake)
    identity.resolve_identity.cache_clear()
    yield state
    identity.resolve_identity.cache_clear()
    identity.public_client.cache_clear()


@pytest.mark.parametrize(
    "did",
    [
        "did:web:127.0.0.1",
        "did:web:localhost",
        "did:web:169.254.169.254",
        "did:web:metadata.google.internal",
        "did:web:printer.local",
        "did:web:example.com%3A8080",
        "did:web:example.com:user:alice",
        "did:web:%5B%3A%3A1%5D",
        # R2-11: shorthand IPv4 and names that resolve to private addresses
        "did:web:127.1",
        "did:web:0x7f.0.0.1",
        "did:web:10.1",
        "did:web:2130706433",
        "did:web:a.0x7f",
        "did:web:localtest.me",
        "did:web:metadata.example.com",
        # R3-01: characters that end the host once the name is put in a URL
        "did:web:169.254.169.254%3Fx",
        "did:web:localtest.me%3Fa",
        "did:web:10.0.0.1%23",
        "did:web:127.0.0.1%40x",
        "did:web:example.com%20x",
        "did:web:169.254.169%EF%BC%8E254",
    ],
)
def test_did_web_private_hosts_refused(resolver, dns, did, monkeypatch):
    dns["localtest.me"] = "127.0.0.1"
    dns["metadata.example.com"] = "fd00:ec2::254"
    resolver.doc = doc(did=did)
    sent = []
    monkeypatch.setattr(httpx.Client, "send", lambda self, req, **k: sent.append(req) or pytest.fail("sent"))
    with pytest.raises(RepoUnavailable) as ei:
        identity.resolve_identity(did)
    assert ei.value.status == "invalid" and sent == []


def test_did_web_url_host_must_match_checked_host(monkeypatch):
    """R3-01 defence in depth: the host httpx would contact is re-checked."""
    with pytest.raises(ValueError):
        identity._check_url_host("https://169.254.169.254?x/.well-known/did.json", "169.254.169.254?x")
    identity._check_url_host("https://example.com/.well-known/did.json", "example.com")


@pytest.mark.parametrize(
    "pds",
    [
        "http://169.254.169.254",
        "https://127.0.0.1",
        "https://localhost:2583",
        "https://pds.internal",
        "https://user:pw@pds.example.com",
        "file:///etc/passwd",
        # R2-11
        "https://127.1",
        "https://0x7f.0.0.1",
        "https://10.1",
        "https://localtest.me",
        "https://rebind.example.com",
        "https://pds.example.com:9",
        "https://pds.example.com:8443",
    ],
)
def test_private_or_insecure_pds_refused(resolver, dns, pds):
    dns["localtest.me"] = "127.0.0.1"
    dns["rebind.example.com"] = "169.254.169.254"
    resolver.doc = doc(pds=pds)
    with pytest.raises(RepoUnavailable) as e:
        identity.resolve_identity(ALICE)
    assert e.value.status == "invalid"


def test_private_handle_is_never_contacted(resolver):
    resolver.doc = doc(handle="admin.internal")
    ident = identity.resolve_identity(ALICE)
    assert ident.handle is None and resolver.handle_calls == []


def test_public_identity_resolves(resolver):
    resolver.doc = doc()
    ident = identity.resolve_identity(ALICE)
    assert ident.pds == "https://pds.example.com" and ident.handle == "alice.example.com"
    assert ident.signing_key.startswith("did:key:")


def test_doc_without_handle_resolves(resolver):
    resolver.doc = doc(handle=None)
    assert identity.resolve_identity(ALICE).handle is None


def test_missing_doc_is_deleted_and_revokes(resolver):
    resolver.doc = None
    with pytest.raises(RepoUnavailable) as e:
        identity.resolve_identity(ALICE)
    assert e.value.status == "deleted"


@pytest.mark.parametrize("code,status", [(410, "deleted"), (404, "deleted")])
def test_tombstoned_plc_did_is_deleted(resolver, code, status):
    req = httpx.Request("GET", f"https://plc.directory/{ALICE}")
    cause = httpx.HTTPStatusError("gone", request=req, response=httpx.Response(code, request=req))
    err = DidPlcResolverError("Error resolving DID")
    err.__cause__ = cause
    resolver.exc = err
    with pytest.raises(RepoUnavailable) as e:
        identity.resolve_identity(ALICE)
    assert e.value.status == status


def test_plc_outage_is_not_deletion(resolver):
    req = httpx.Request("GET", f"https://plc.directory/{ALICE}")
    err = DidPlcResolverError("Error resolving DID")
    err.__cause__ = httpx.ConnectError("down", request=req)
    resolver.exc = err
    with pytest.raises(DidPlcResolverError):
        identity.resolve_identity(ALICE)


def test_deleted_did_makes_resync_revoked(resolver):
    """End to end through verify.fetch_verified: a tombstoned DID is REVOKED,
    not UNKNOWN (which --lenient would keep)."""
    from consentvid.verify import fetch_verified

    resolver.doc = None
    it = rs.Item(
        id="x", name="x", consent_uri=f"at://{ALICE}/org.consentvid.consent/{K1}", sha256="ab" * 32,
        content_url="https://example.org/v", receipt={},
    )
    r = rs.check(it, fetch=fetch_verified)
    assert r.status == rs.REVOKED


def test_public_client_does_not_follow_redirects(resolver):
    resolver.doc = doc()
    client = identity.public_client(ALICE)
    assert client.request._client.follow_redirects is False


def test_pds_port_allowed_only_with_insecure_escape_hatch(resolver, monkeypatch):
    resolver.doc = doc(pds="https://pds.example.com:2583")
    monkeypatch.setenv("CONSENTVID_ALLOW_INSECURE", "1")
    assert identity.resolve_identity(ALICE).pds == "https://pds.example.com:2583"


def test_handle_txt_verifies_even_if_the_handle_host_is_private(resolver, dns):
    """R2-11: the DNS TXT method never contacts the handle host, so its A
    record doesn't matter (the HTTPS method checks every host it contacts)."""
    dns["alice.example.com"] = "100.64.0.1"
    resolver.doc = doc()
    assert identity.verified_handle(ALICE) == "alice.example.com"
    assert resolver.handle_calls == ["alice.example.com"]


def test_resolve_identity_makes_no_handle_lookups(resolver, monkeypatch):
    """R2-11: reading a repo needs the PDS and key only."""
    resolver.doc = doc()
    monkeypatch.setattr(identity, "_resolve_handle", lambda h: pytest.fail("handle lookup"))
    ident = identity.resolve_identity(ALICE)
    assert ident.pds == "https://pds.example.com" and ident.claimed_handle == "alice.example.com"
    assert identity.display_name  # (display_name verifies; see below)


def test_display_name_uses_only_verified_handles(resolver, monkeypatch):
    resolver.doc = doc()
    monkeypatch.setattr(identity, "_resolve_handle", lambda h: "did:plc:someoneelse0000000000000")
    assert identity.display_name(ALICE) == ALICE
    identity.verified_handle.cache_clear()
    monkeypatch.setattr(identity, "_resolve_handle", lambda h: ALICE)
    assert identity.display_name(ALICE) == "alice.example.com"


@pytest.mark.parametrize("did", ["did:web:pds.example", "did:web:x.test", "did:web:x.alt", "did:web:x.onion"])
def test_did_web_reserved_tlds_refused(resolver, did, monkeypatch):
    """R2-12: the handle TLD restrictions apply to did:web domains."""
    monkeypatch.setattr(httpx.Client, "send", lambda self, req, **k: pytest.fail("sent"))
    with pytest.raises(RepoUnavailable) as ei:
        identity.resolve_identity(did)
    assert ei.value.status == "invalid"


@pytest.mark.parametrize(
    "pds, ok",
    [
        ("https://pds.example.com/some/path", False),
        ("https://pds.example.com/?x=1", False),
        ("https://pds.example.com/#frag", False),
        ("https://pds.example.com?", False),
        ("https://pds.example.com/", True),
        ("https://pds.example.com", True),
    ],
)
def test_pds_endpoint_must_be_an_origin(monkeypatch, pds, ok):
    monkeypatch.setattr(identity, "_check_public_host", lambda host: None)
    if ok:
        identity._check_pds(pds)
    else:
        with pytest.raises(ValueError, match="origin"):
            identity._check_pds(pds)


# ------------------------------------- R2-04: bounded answers from a PDS


def _client(handler):
    from atproto import Client

    return Client(
        base_url="https://pds.example.com",
        request=identity._NoRedirectRequest(transport=httpx.MockTransport(handler)),
    )


def _gzip_bomb(n=identity.MAX_BODY + 1024):
    import gzip
    import io

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        g.write(b'{"records": [], "pad": "')
        chunk = b"a" * (1 << 20)
        for _ in range(n // len(chunk) + 1):
            g.write(chunk)
        g.write(b'"}')
    return buf.getvalue()


def test_gzip_bomb_is_refused():
    bomb = _gzip_bomb()
    assert len(bomb) < 100_000

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json", "content-encoding": "gzip"}, stream=httpx.ByteStream(bomb)
        )

    with pytest.raises(identity.ResponseTooLarge):
        _client(handler).com.atproto.repo.list_records({"repo": ALICE, "collection": "x.y.z"})


@pytest.mark.parametrize("coding", ["br", "zstd", "gzip, gzip", "gzip,deflate", "compress"])
def test_unbounded_or_stacked_encodings_are_refused(coding):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/json", "content-encoding": coding}, stream=httpx.ByteStream(b"{}"))

    with pytest.raises(identity.ResponseTooLarge):
        _client(handler).com.atproto.repo.list_records({"repo": ALICE, "collection": "x.y.z"})


def test_slow_drip_hits_the_deadline(monkeypatch):
    clock = iter(range(0, 10_000, 7))  # every read "takes" 7 s
    monkeypatch.setattr(identity.time, "monotonic", lambda: next(clock))

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                yield b" "

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Drip())

    with pytest.raises(identity.ResponseTooLarge, match="longer than"):
        _client(handler).com.atproto.repo.list_records({"repo": ALICE, "collection": "x.y.z"})


def test_normal_response_still_parses():
    import gzip
    import json

    body = {"records": [{"uri": f"at://{ALICE}/x.y.z/a", "cid": "bafyx", "value": {"a": 1}}], "cursor": None}

    def handler(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=httpx.ByteStream(gzip.compress(json.dumps(body).encode())),
        )

    resp = _client(handler).com.atproto.repo.list_records({"repo": ALICE, "collection": "x.y.z"})
    assert [r.uri for r in resp.records] == [f"at://{ALICE}/x.y.z/a"]


def test_redirects_and_errors_still_map_to_atproto_errors():
    from atproto_client.exceptions import BadRequestError, RequestException

    def handler(request):
        if "listRecords" in request.url.path:
            return httpx.Response(302, headers={"location": "https://169.254.169.254/"})
        return httpx.Response(400, json={"error": "RecordNotFound", "message": "nope"})

    c = _client(handler)
    with pytest.raises(RequestException):
        c.com.atproto.repo.list_records({"repo": ALICE, "collection": "x.y.z"})
    with pytest.raises(BadRequestError):
        c.com.atproto.repo.get_record({"repo": ALICE, "collection": "x.y.z", "rkey": "a"})


def test_oversized_answer_makes_resync_invalid_not_unknown():
    bomb = _gzip_bomb()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/vnd.ipld.car", "content-encoding": "gzip"}, stream=httpx.ByteStream(bomb))

    client = _client(handler)

    def fetch(at):
        return client.com.atproto.sync.get_record({"did": at.did, "collection": at.collection, "rkey": at.rkey})

    it = rs.Item(
        id="x", name="x", consent_uri=f"at://{ALICE}/org.consentvid.consent/{K1}", sha256="ab" * 32,
        content_url="https://example.org/v", receipt={},
    )
    r = rs.check(it, fetch=fetch)
    assert r.status == rs.INVALID and "ResponseTooLarge" in r.reason


class _Pages:
    """listRecords double: ``pages`` is a list of (records, cursor)."""

    def __init__(self, pages):
        self.pages, self.calls = pages, 0
        self.com = SimpleNamespace(atproto=SimpleNamespace(repo=self))

    def list_records(self, params):
        recs, cursor = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return SimpleNamespace(records=recs, cursor=cursor)


def _rec(i):
    return SimpleNamespace(uri=f"at://{ALICE}/org.consentvid.consent/r{i}", cid="bafyx", value={})


def test_repeated_cursor_is_an_error_after_one_repeat():
    from consentvid.errors import ConsentVidError

    pds = _Pages([([_rec(1)], "same")])
    with pytest.raises(ConsentVidError, match="repeated"):
        identity.list_all(pds, ALICE, "org.consentvid.consent")
    assert pds.calls == 2


def test_page_cap_and_oversized_pages():
    from consentvid.errors import ConsentVidError

    class Counting(_Pages):
        def list_records(self, params):
            self.calls += 1
            return SimpleNamespace(records=[_rec(self.calls)], cursor=f"c{self.calls}")

    pds = Counting([])
    with pytest.raises(ConsentVidError, match="more than 3 pages"):
        identity.list_all(pds, ALICE, "org.consentvid.consent", max_pages=3)
    big = _Pages([([_rec(i) for i in range(identity.PAGE_LIMIT + 1)], None)])
    with pytest.raises(ConsentVidError, match="records for a page"):
        identity.list_all(big, ALICE, "org.consentvid.consent")
    ok = _Pages([([_rec(1)], "a"), ([_rec(2)], "b"), ([], None)])
    assert [r.uri[-2:] for r in identity.list_all(ok, ALICE, "org.consentvid.consent")] == ["r1", "r2"]


def test_did_web_document_is_fetched_with_the_capped_client(monkeypatch):
    import json

    did = "did:web:example.com"
    body = json.dumps(
        {
            "@context": ["https://www.w3.org/ns/did/v1"],
            "id": did,
            "verificationMethod": [
                {"id": f"{did}#atproto", "type": "Multikey", "controller": did, "publicKeyMultibase": KEY}
            ],
            "service": [
                {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://pds.example.com"}
            ],
        }
    ).encode()
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(identity, "_http", lambda: client)
    identity.resolve_identity.cache_clear()
    try:
        ident = identity.resolve_identity(did)
    finally:
        identity.resolve_identity.cache_clear()
    assert seen == ["https://example.com/.well-known/did.json"] and ident.pds == "https://pds.example.com"

    def bomb(request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(_gzip_bomb()))

    monkeypatch.setattr(identity, "_http", lambda: httpx.Client(transport=httpx.MockTransport(bomb)))
    with pytest.raises(identity.ResponseTooLarge):
        identity.resolve_identity("did:web:example.org")
    identity.resolve_identity.cache_clear()


# ------------------------- R3-02: absolute deadline over connect/TLS/headers


def test_deadline_backend_hooks_into_httpx_internals():
    """Fails loudly if httpx/httpcore move the private attribute we patch."""
    c = identity._install_deadline(httpx.Client())
    assert isinstance(c._transport._pool._network_backend, identity._DeadlineBackend)
    assert isinstance(identity._http()._transport._pool._network_backend, identity._DeadlineBackend)
    r = identity._NoRedirectRequest()
    assert isinstance(r._client._transport._pool._network_backend, identity._DeadlineBackend)


def _slow_server(mode):
    """A local HTTP server that drips its status line and headers one byte
    every 0.2 s ('drip') or accepts and never answers ('silent')."""
    import socket
    import threading
    import time as _t

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        conns = []
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            conns.append(conn)
            try:
                conn.recv(65536)
                if mode == "drip":
                    head = b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"a" * 400 + b"\r\n\r\n"
                    for b in head:
                        if stop.is_set():
                            break
                        conn.sendall(bytes([b]))
                        _t.sleep(0.2)
                else:
                    stop.wait(30)
            except OSError:
                pass
        for c in conns:
            c.close()
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv.getsockname()[1], stop


@pytest.mark.parametrize("mode", ["drip", "silent"])
def test_slow_headers_hit_the_absolute_deadline(monkeypatch, dns, mode):
    import time as _t

    dns["127.0.0.1"] = "127.0.0.1"

    monkeypatch.setattr(identity, "DEADLINE", 1.0)
    monkeypatch.setattr(identity, "_TIMEOUT", 0.5)
    port, stop = _slow_server(mode)
    # 'silent': a per-read timeout longer than the deadline, so only the
    # absolute deadline can end it.
    client = identity._install_deadline(httpx.Client(timeout=0.5 if mode == "drip" else 5.0))
    try:
        t0 = _t.monotonic()
        with pytest.raises(identity.ResponseTooLarge, match="longer than"):
            identity.capped_send(client, client.build_request("GET", f"http://127.0.0.1:{port}/x"))
        assert _t.monotonic() - t0 < identity.DEADLINE + 0.5
    finally:
        stop.set()
        client.close()


def test_slow_headers_make_resync_invalid(monkeypatch, dns):
    from atproto import Client

    dns["127.0.0.1"] = "127.0.0.1"

    monkeypatch.setattr(identity, "DEADLINE", 1.0)
    monkeypatch.setattr(identity, "_TIMEOUT", 0.5)
    port, stop = _slow_server("drip")
    client = Client(base_url=f"http://127.0.0.1:{port}", request=identity._NoRedirectRequest())
    client.request._client.timeout = httpx.Timeout(0.5)

    def fetch(at):
        return client.com.atproto.sync.get_record({"did": at.did, "collection": at.collection, "rkey": at.rkey})

    it = rs.Item(
        id="x", name="x", consent_uri=f"at://{ALICE}/org.consentvid.consent/{K1}", sha256="ab" * 32,
        content_url="https://example.org/v", receipt={},
    )
    try:
        r = rs.check(it, fetch=fetch)
    finally:
        stop.set()
    assert r.status == rs.INVALID and "ResponseTooLarge" in r.reason


# ------------------------------- R1-05: handle resolution per the handle spec


def test_dns_txt_needs_exactly_one_valid_did(monkeypatch):
    txt = {}
    monkeypatch.setattr(identity, "_txt_records", lambda name: txt.get(name, []))
    txt["_atproto.alice.example.com"] = [f"did={ALICE}", "did=did:plc:someoneelseentirely00"]
    assert identity._resolve_handle_dns("alice.example.com") is None  # conflicting
    txt["_atproto.alice.example.com"] = ["did=junk", "v=spf1"]
    assert identity._resolve_handle_dns("alice.example.com") is None
    txt["_atproto.alice.example.com"] = [f"did={ALICE}", f"did={ALICE}", "other"]
    assert identity._resolve_handle_dns("alice.example.com") == ALICE


def test_split_txt_strings_are_joined(monkeypatch):
    import dns.resolver

    rdata = SimpleNamespace(strings=[b"did=did:plc:alice", b"aaaaaaaaaaaaaaaaaaa"])
    monkeypatch.setattr(dns.resolver, "resolve", lambda name, rtype, lifetime=None: [rdata])
    assert identity._txt_records("_atproto.alice.example.com") == ["did=did:plc:aliceaaaaaaaaaaaaaaaaaaa"]


def _https(monkeypatch, routes):
    """identity._http() answers from ``routes``: url -> (status, headers, body)."""
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        status, headers, body = routes.get(url, (404, {}, b""))
        return httpx.Response(status, headers=headers, content=body)

    monkeypatch.setattr(identity, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


WK = "/.well-known/atproto-did"


def test_https_redirect_is_followed(monkeypatch):
    seen = _https(
        monkeypatch,
        {
            f"https://alice.example.com{WK}": (301, {"location": f"https://www.alice.example.com{WK}"}, b""),
            f"https://www.alice.example.com{WK}": (200, {}, f"{ALICE}\n".encode()),
        },
    )
    assert identity._resolve_handle_https("alice.example.com") == ALICE and len(seen) == 2


@pytest.mark.parametrize(
    "location",
    [
        f"http://www.alice.example.com{WK}",  # downgrade
        f"https://internal.example.com{WK}",  # resolves to a private address (below)
        f"https://user@www.alice.example.com{WK}",
        f"https://www.alice.example.com:8443{WK}",
        f"https://10.0.0.1{WK}",
    ],
)
def test_unsafe_redirects_are_refused(monkeypatch, dns, location):
    dns["internal.example.com"] = "10.1.2.3"
    seen = _https(
        monkeypatch,
        {
            f"https://alice.example.com{WK}": (302, {"location": location}, b""),
            location: (200, {}, ALICE.encode()),
        },
    )
    assert identity._resolve_handle_https("alice.example.com") is None
    assert seen == [f"https://alice.example.com{WK}"]


def test_too_many_redirects_are_refused(monkeypatch):
    hosts = [f"h{i}.alice.example.com" for i in range(5)]
    routes = {f"https://{a}{WK}": (307, {"location": f"https://{b}{WK}"}, b"") for a, b in zip(hosts, hosts[1:], strict=False)}
    routes[f"https://alice.example.com{WK}"] = (307, {"location": f"https://{hosts[0]}{WK}"}, b"")
    routes[f"https://{hosts[-1]}{WK}"] = (200, {}, ALICE.encode())
    _https(monkeypatch, routes)
    assert identity._resolve_handle_https("alice.example.com") is None


def test_first_valid_also_known_as_handle_is_used_lowercased(resolver):
    raw = {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": ALICE,
        "alsoKnownAs": ["at://did:plc:x", "at://Alice.Example.COM", "at://bob.example.com"],
        "verificationMethod": [
            {"id": f"{ALICE}#atproto", "type": "Multikey", "controller": ALICE, "publicKeyMultibase": KEY}
        ],
        "service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://pds.example.com"}],
    }
    resolver.doc = DidDocument.from_dict(raw)
    ident = identity.resolve_identity(ALICE)
    assert ident.handle == "alice.example.com" and resolver.handle_calls == ["alice.example.com"]


def test_reserved_tld_handle_claim_is_never_verified(resolver):
    resolver.doc = doc(handle="alice.local")
    assert identity.resolve_identity(ALICE).handle is None and resolver.handle_calls == []


def test_conflicting_lexicon_txt_records(monkeypatch):
    from consentvid import lexicons as lx

    monkeypatch.setattr(lx, "lexicon_txt_values", lambda nsid: [f"did={ALICE}", "did=did:plc:other0000000000000000000"])
    assert lx.resolve_did_via_dns("org.consentvid.consent") is None
    with pytest.raises(lx.ConflictingLexiconRecords):
        lx.resolve_did_via_dns("org.consentvid.consent", strict=True)
    monkeypatch.setattr(lx, "lexicon_txt_values", lambda nsid: ["did=junk", f"did={ALICE}"])
    assert lx.resolve_did_via_dns("org.consentvid.consent") == ALICE


def test_identity_cache_expires_and_can_be_invalidated(resolver, monkeypatch):
    resolver.doc = doc()
    calls = []
    real = identity._resolve_identity
    monkeypatch.setattr(identity, "_resolve_identity", lambda did: calls.append(did) or real(did))
    identity.resolve_identity(ALICE)
    identity.resolve_identity(ALICE)
    assert len(calls) == 1
    identity.invalidate_identity(ALICE)
    identity.resolve_identity(ALICE)
    assert len(calls) == 2
    monkeypatch.setattr(identity, "IDENTITY_TTL", -1)
    identity.resolve_identity(ALICE)
    assert len(calls) == 3


# ------------------------- R3-09: DID resolution is retried like other reads


def _plc_error(status):
    req = httpx.Request("GET", f"https://plc.directory/{ALICE}")
    err = DidPlcResolverError("Error resolving DID")
    err.__cause__ = httpx.HTTPStatusError(str(status), request=req, response=httpx.Response(status, request=req))
    return err


def test_plc_503_then_200_resolves(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(identity, "_sleep", sleeps.append)

    def resolve(did):
        calls.append(did)
        if len(calls) == 1:
            raise _plc_error(503)
        return "the document"

    monkeypatch.setattr(identity, "_resolver", lambda: SimpleNamespace(did=SimpleNamespace(resolve=resolve)))
    assert identity._resolve_doc(ALICE) == "the document"
    assert len(calls) == 2 and len(sleeps) == 1


def test_plc_410_is_deleted_without_retrying(monkeypatch):
    calls = []

    def resolve(did):
        calls.append(did)
        raise _plc_error(410)

    monkeypatch.setattr(identity, "_resolver", lambda: SimpleNamespace(did=SimpleNamespace(resolve=resolve)))
    with pytest.raises(RepoUnavailable) as ei:
        identity._resolve_doc(ALICE)
    assert ei.value.status == "deleted" and len(calls) == 1


def test_wrapped_http_statuses_decide_transience():
    assert identity.status_of(_plc_error(503)) == 503
    assert identity._is_transient_error(_plc_error(503))
    assert not identity._is_transient_error(_plc_error(404))
    assert not identity._is_transient_error(_plc_error(410))
    err = DidPlcResolverError("x")
    err.__cause__ = httpx.ConnectError("down", request=httpx.Request("GET", "https://plc.directory/x"))
    assert identity._is_transient_error(err)


# ------------------------- R3-08: a failed handle lookup is not "unverified"


def _real_handle_methods(monkeypatch, resolver):
    def resolve(did):
        return resolver.doc

    fake = SimpleNamespace(did=SimpleNamespace(resolve=resolve), handle=SimpleNamespace(resolve_dns=None))
    monkeypatch.setattr(identity, "_resolver", lambda: fake)
    identity.verified_handle.cache_clear()


def test_dns_timeout_in_verified_handle_is_not_cached(monkeypatch, resolver):
    import dns.exception
    import dns.resolver

    resolver.doc = doc()
    _real_handle_methods(monkeypatch, resolver)
    _https(monkeypatch, {})  # the well-known method answers 404
    calls = []

    def resolve(name, rtype, lifetime=None):
        calls.append(name)
        if len(calls) == 1:
            raise dns.exception.Timeout()
        return [SimpleNamespace(strings=[f"did={ALICE}".encode()])]

    monkeypatch.setattr(dns.resolver, "resolve", resolve)
    assert identity.verified_handle(ALICE) is None  # unknown this time ...
    assert identity.verified_handle(ALICE) == "alice.example.com"  # ... retried, not cached
    with pytest.raises(identity.HandleLookupError):
        calls.clear()
        identity.verified_handle.cache_clear()
        identity.verified_handle(ALICE, raise_errors=True)
    identity.verified_handle.cache_clear()


def test_nxdomain_and_https_404_is_a_cached_negative(monkeypatch, resolver):
    import dns.resolver

    resolver.doc = doc()
    _real_handle_methods(monkeypatch, resolver)
    seen = _https(monkeypatch, {})
    calls = []

    def resolve(name, rtype, lifetime=None):
        calls.append(name)
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", resolve)
    assert identity.verified_handle(ALICE) is None
    assert identity.verified_handle(ALICE) is None
    assert len(calls) == 1 and len(seen) == 1  # the definite answer was cached
    identity.verified_handle.cache_clear()


@pytest.mark.parametrize("answer", ["servfail", "timeout"])
def test_dns_errors_raise_instead_of_answering_none(monkeypatch, answer):
    import dns.exception
    import dns.resolver

    exc = dns.resolver.NoNameservers() if answer == "servfail" else dns.exception.Timeout()
    monkeypatch.setattr(dns.resolver, "resolve", lambda *a, **k: (_ for _ in ()).throw(exc))
    with pytest.raises(identity.HandleLookupError):
        identity._txt_records("_atproto.alice.example.com")


def test_https_5xx_and_transport_errors_raise_but_4xx_is_an_answer(monkeypatch):
    _https(monkeypatch, {f"https://alice.example.com{WK}": (503, {}, b"")})
    with pytest.raises(identity.HandleLookupError):
        identity._resolve_handle_https("alice.example.com")
    _https(monkeypatch, {f"https://alice.example.com{WK}": (404, {}, b"")})
    assert identity._resolve_handle_https("alice.example.com") is None

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(identity, "_http", lambda: httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(identity.HandleLookupError):
        identity._resolve_handle_https("alice.example.com")


def test_embed_warns_when_the_handle_lookup_failed(capsys):
    from consentvid import _core, cli

    _core.set_reporter(cli._click_reporter)  # as when the CLI runs

    def failing(did):
        raise identity.HandleLookupError("DNS lookup of _atproto.alice.example.com failed (Timeout)")

    assert cli._verified_handle(ALICE, failing) is None
    assert "handle not included: lookup failed (DNS lookup" in capsys.readouterr().err


# ------------------------- R3-04: the client always matches the identity


def test_public_client_follows_the_identity_after_it_expires(resolver):
    resolver.doc = doc(pds="https://old.example.com")
    assert identity.public_client(ALICE)._base_url.startswith("https://old.example.com")
    resolver.doc = doc(pds="https://new.example.com")
    identity._identities.pop(ALICE)  # what IDENTITY_TTL expiry does
    assert identity.resolve_identity(ALICE).pds == "https://new.example.com"
    assert identity.public_client(ALICE)._base_url.startswith("https://new.example.com")
    # Invalidating an identity keeps per-PDS clients (they stay correct).
    identity.invalidate_identity(ALICE)
    assert identity.client_for("https://old.example.com") is identity.client_for("https://old.example.com")
