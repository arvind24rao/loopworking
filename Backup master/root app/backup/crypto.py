from typing import Tuple

# MVP placeholder: do NOT use in production.
# Real version will AES-GCM encrypt with a per-message DEK wrapped by your master key/KMS.

def encrypt_plaintext(plaintext: str) -> Tuple[str, bytes, bytes, bytes]:
    """
    Returns tuple shaped like the real thing:
    (content_ciphertext, dek_wrapped, nonce, aead_tag)
    For MVP dev, we just prefix the plaintext.
    """
    return f"cipher:{plaintext}", b"", b"", b""

def decrypt_ciphertext(ciphertext: str) -> str:
    if ciphertext.startswith("cipher:"):
        return ciphertext[len("cipher:"):]
    return ciphertext
