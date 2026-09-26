"""Pinned input bytes for the released suites; cache hits are verified too."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

BANKING77_COMMIT = "57ec275d8078af65b7731c2a98be812d844a6d6b"
BANKING77_BASE = (
    "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/" + BANKING77_COMMIT
)
SOURCES = {
    "banking77_csv": {
        "url": BANKING77_BASE + "/banking_data/train.csv",
        "sha256": "b06e26ac675513959a63135f11b94ea7786ed02da65db93a5650d8838cbc664b",
        "commit": BANKING77_COMMIT,
        "license": "CC BY 4.0",
    },
    "banking77_license": {
        "url": BANKING77_BASE + "/LICENSE",
        "sha256": "7e7170e3cebf88a9f60c7b8421418323c09304da1af4d5e90f4da1dc1c8a2661",
        "commit": BANKING77_COMMIT,
    },
    "sms_zip": {
        "url": "https://archive.ics.uci.edu/ml/machine-learning-databases/00228/smsspamcollection.zip",
        "sha256": "1587ea43e58e82b14ff1f5425c88e17f8496bfcdb67a583dbff9eefaf9963ce3",
        "license": "CC BY 4.0 (current UCI dataset page)",
        "license_url": "https://archive.ics.uci.edu/dataset/228/sms+spam+collection",
    },
    "sms_data": {
        "member": "SMSSpamCollection",
        "sha256": "7d039a24a6083ed9ef0f806ebad56bbb976e3aeb8de05669173bfdc4996c239d",
    },
}


def verify_bytes(payload: bytes, expected: str, name: str) -> bytes:
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected:
        raise ValueError(f"source checksum mismatch for {name}: expected {expected}, got {digest}")
    return payload


def verified_download(path: Path, source: dict[str, str]) -> Path:
    """Reject changed cached/downloaded inputs; never replace a mismatch silently."""
    if path.exists():
        verify_bytes(path.read_bytes(), source["sha256"], path.name)
        return path
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.get(source["url"])
        response.raise_for_status()
    payload = verify_bytes(response.content, source["sha256"], path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path
