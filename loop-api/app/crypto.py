# app/crypto.py
"""
Placeholder crypto shim used across the app.

Current behaviour:
- "Encrypt" = prefix with "cipher:" so we never store raw text by accident.
- "Decrypt" = strip the prefix back to plaintext for APIs that must return text.

Notes:
- This is intentionally trivial. See the handbook §13.4 for migrating to real AEAD.
- Keep function names stable; other modules import `seal_plaintext`.
"""

from __future__ import annotations


_PREFIX = "cipher:"


def seal_plaintext(plaintext: str) -> str:
    """
    Returns a ciphertext placeholder by prefixing with 'cipher:'.
    Ensures input is a string and strips leading/trailing whitespace.
    """
    if plaintext is None:
        plaintext = ""
    # Normalise to str and trim outer whitespace only (preserve inner spaces/newlines)
    text = str(plaintext).strip()
    return f"{_PREFIX}{text}"


def reveal_plaintext(ciphertext: str) -> str:
    """
    Strips the 'cipher:' prefix and returns the original plaintext.
    If the prefix is missing, returns the input as-is.
    """
    if not isinstance(ciphertext, str):
        return ""
    if ciphertext.startswith(_PREFIX):
        return ciphertext[len(_PREFIX) :].strip()
    return ciphertext.strip()


__all__ = ["seal_plaintext", "reveal_plaintext"]