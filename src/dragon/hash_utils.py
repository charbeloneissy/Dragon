"""Hash helpers used by Dragon and compatible with BigQuery hash output.

SHA-256 is the canonical cryptographic fingerprint for Dragon identifiers.
SHA-1 helpers remain available only for legacy compatibility with existing
BigQuery SHA1 pipelines.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Union

HashInput = Union[str, bytes]


def _bytes(value: HashInput) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("value must be str or bytes")


def sha256_bytes(value: HashInput) -> bytes:
    """Return the raw 32-byte SHA-256 digest."""
    return hashlib.sha256(_bytes(value)).digest()


def sha256_hex(value: HashInput) -> str:
    """Return SHA-256 as lowercase hexadecimal, matching TO_HEX(SHA256(...))."""
    return sha256_bytes(value).hex()


def sha256_base64(value: HashInput) -> str:
    """Return SHA-256 as Base64 for BigQuery BYTES-style display."""
    return base64.b64encode(sha256_bytes(value)).decode("ascii")


def sha1_bytes(value: HashInput) -> bytes:
    """Return the raw 20-byte SHA-1 digest for legacy compatibility."""
    return hashlib.sha1(_bytes(value)).digest()


def sha1_base64(value: HashInput) -> str:
    """Return SHA-1 digest encoded as Base64, matching BigQuery display."""
    return base64.b64encode(sha1_bytes(value)).decode("ascii")


def sha1_hex(value: HashInput) -> str:
    """Return SHA-1 digest as lowercase hexadecimal."""
    return sha1_bytes(value).hex()


def opportunity_hash(
    *,
    chain_id: int,
    buy_source: str,
    sell_source: str,
    base_token: str,
    quote_token: str,
    quote_amount: int,
    block_number: int | None = None,
    quote_version: str = "v1",
) -> str:
    """Create a deterministic SHA-256 ID for one Dragon opportunity.

    Fields are explicitly delimited so concatenation cannot ambiguously merge
    adjacent values. The same canonical fields produce the same ID in Python
    and in BigQuery when the same CONCAT expression is used.
    """
    fields = (
        str(chain_id),
        buy_source,
        sell_source,
        base_token,
        quote_token,
        str(quote_amount),
        "" if block_number is None else str(block_number),
        quote_version,
    )
    return sha256_hex("|".join(fields))
