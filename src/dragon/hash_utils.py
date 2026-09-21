"""Hash helpers with BigQuery-compatible SHA-1 semantics.

BigQuery SHA1(string) hashes the UTF-8 bytes of the string and returns BYTES.
When those bytes are displayed as Base64, the representation matches
base64.b64encode(hashlib.sha1(value.encode("utf-8")).digest()).
"""
from __future__ import annotations

import base64
import hashlib


def sha1_bytes(value: str) -> bytes:
    """Return the raw 20-byte SHA-1 digest of a UTF-8 string."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    return hashlib.sha1(value.encode("utf-8")).digest()


def sha1_base64(value: str) -> str:
    """Return SHA-1 digest encoded as Base64, matching BigQuery display."""
    return base64.b64encode(sha1_bytes(value)).decode("ascii")


def sha1_hex(value: str) -> str:
    """Return SHA-1 digest as lowercase hexadecimal."""
    return sha1_bytes(value).hex()
