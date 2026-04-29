"""Smoke test: export a Croissant manifest and validate it.

Usage:
  pip install -e ".[dev]"
  PYTHONPATH=src python scripts/validate_manifest.py

Exits non-zero if export or validation fails.
"""
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        manifest_path = Path(f.name)

    print(f"Exporting manifest to {manifest_path} ...")
    subprocess.check_call(
        [sys.executable, "-m", "consentvid.cli", "export", str(manifest_path)]
    )

    print("Validating against Croissant 1.0 ...")
    try:
        from mlcroissant import Dataset
    except ImportError:
        print(
            'mlcroissant not installed. Install with: pip install -e ".[dev]"',
            file=sys.stderr,
        )
        return 2

    Dataset(jsonld=str(manifest_path))  # raises ValidationError on failure
    print(f"OK: {manifest_path} validates against Croissant 1.0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
