"""Against the live network. Run with: pytest -m network"""

import pytest

from consentvid.verify import fetch_verified

pytestmark = pytest.mark.network
DID = "did:plc:u73ld57pmwsta45y45uitdfi"


def test_live_absence_proof():
    rec = fetch_verified(f"at://{DID}/org.consentvid.consent/3mkm6zmigrx2v")
    assert not rec.present and rec.proven


def test_live_schema_record_verifies():
    rec = fetch_verified(f"at://{DID}/com.atproto.lexicon.schema/org.consentvid.video")
    assert rec.present and rec.value["id"] == "org.consentvid.video"
