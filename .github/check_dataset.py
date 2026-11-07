"""Fail if a sample manifest leaks local filesystem paths or points at
anything but https URLs. Used by deploy-pages.yml before and after deploying.
Standard library only."""

import json
import sys


def problems(text: str) -> list[str]:
    out = [f"contains {needle!r}" for needle in ("/Users/", "/home/", "file:", "C:\\") if needle in text]
    try:
        manifest = json.loads(text)
    except ValueError as e:
        return out + [f"not JSON: {e}"]
    distribution = manifest.get("distribution") if isinstance(manifest, dict) else None
    if not isinstance(distribution, list):
        return out + ["no distribution list"]
    for fo in distribution:
        url = fo.get("contentUrl") if isinstance(fo, dict) else None
        if not (isinstance(url, str) and url.startswith("https://")):
            out.append(f"non-https contentUrl {url!r}")
    return out


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        found = problems(f.read())
    for p in found:
        print(f"{path}: {p}")
    sys.exit(1 if found else 0)
