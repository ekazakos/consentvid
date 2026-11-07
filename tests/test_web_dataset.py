"""The published sample manifest must never leak local paths (R1-23), must be
a current-format manifest (R2-17) and must pass resync's manifest shape
checks."""

from pathlib import Path

from consentvid import resync as rs

DATASET = Path(__file__).resolve().parents[1] / "web" / "dataset.json"


def _manifest():
    # Same strict reader resync uses: duplicate keys are refused (R2-08).
    return rs.load_manifest_json(DATASET.read_text(encoding="utf-8"))


def test_sample_dataset_has_no_local_paths():
    text = DATASET.read_text(encoding="utf-8")
    for needle in ("/Users/", "/home/", "C:\\\\", "C:\\", "file:"):
        assert needle not in text, needle
    for fo in _manifest()["distribution"]:
        assert fo["contentUrl"].startswith("https://"), fo["contentUrl"]


def test_sample_dataset_is_current_format():
    for fo in _manifest()["distribution"]:
        assert isinstance(fo.get("cv:consentReceipt"), dict), fo.get("@id")
        assert "cr:consentReceipt" not in fo and "cr:atUri" not in fo


def test_sample_dataset_passes_resync_shape_checks():
    items = rs.items_from_manifest(_manifest())
    assert items and all(i.problem is None for i in items), [i.problem for i in items]
    for i in items:
        assert rs._receipt_shape_error(i.receipt) is None
