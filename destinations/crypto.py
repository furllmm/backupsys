"""
destinations.crypto
====================

Per-destination, at-rest encryption for remote objects, using AES-256-GCM
(authenticated encryption -- tampering or corruption is detected on
decrypt, not just silently accepted) with a key derived from a user
passphrase via PBKDF2-HMAC-SHA256.

Security note on where the passphrase lives
--------------------------------------------
The passphrase itself is **never** written to a destination config file on
disk. Only the PBKDF2 *salt* and iteration count are persisted (they are
not secret -- they just need to be reproducible so the same passphrase
re-derives the same key later). The passphrase must be supplied at
push/restore time via the ``BACKUPSYS_DESTINATION_PASSPHRASE`` environment
variable or an interactive prompt.
Storing an encryption passphrase in plaintext next to the ciphertext it
protects would defeat the purpose of encrypting at all.

Wire format
-----------
Each encrypted object is stored as ``salt_marker + nonce(12) + ciphertext``
where ciphertext includes the GCM tag (cryptography's AESGCM appends it
automatically). The salt is NOT stored per-object -- it lives once in the
repository's ``repository.json`` manifest (see
:mod:`destinations.repository`), since re-deriving the same key for every
object with a per-object salt would be wasteful and isn't needed: GCM's
random 96-bit nonce alone is sufficient to keep every object's ciphertext
unique even under a single fixed key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

PBKDF2_ITERATIONS = 480_000  # OWASP-recommended minimum as of 2023 for PBKDF2-HMAC-SHA256
KEY_LENGTH = 32  # AES-256
NONCE_LENGTH = 12  # 96-bit GCM nonce, the standard/recommended size
SALT_LENGTH = 16


class DecryptionError(RuntimeError):
    """Raised when ciphertext fails authentication (wrong passphrase, or
    the object was corrupted/tampered with in transit or at rest)."""


@dataclass(frozen=True)
class KeyDerivationParams:
    salt: bytes
    iterations: int = PBKDF2_ITERATIONS

    def to_dict(self) -> dict:
        return {"salt_hex": self.salt.hex(), "iterations": self.iterations}

    @classmethod
    def from_dict(cls, d: dict) -> "KeyDerivationParams":
        return cls(salt=bytes.fromhex(d["salt_hex"]), iterations=d["iterations"])

    @classmethod
    def generate(cls) -> "KeyDerivationParams":
        return cls(salt=os.urandom(SALT_LENGTH))


def derive_key(passphrase: str, params: KeyDerivationParams) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=params.salt,
        iterations=params.iterations,
    )
    return kdf.derive(passphrase.encode("utf-8"))


class ObjectCipher:
    """Encrypts/decrypts individual repository objects with a key derived
    once (not per-call) from the repository's passphrase + stored salt."""

    def __init__(self, passphrase: str, params: KeyDerivationParams):
        self._aesgcm = AESGCM(derive_key(passphrase, params))

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(NONCE_LENGTH)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, associated_data=None)
        return nonce + ciphertext

    def decrypt(self, blob: bytes) -> bytes:
        if len(blob) < NONCE_LENGTH:
            raise DecryptionError("Ciphertext too short to contain a nonce")
        nonce, ciphertext = blob[:NONCE_LENGTH], blob[NONCE_LENGTH:]
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, associated_data=None)
        except Exception as exc:  # cryptography raises InvalidTag
            raise DecryptionError(
                "Decryption failed -- wrong passphrase, or the object is corrupted/tampered with"
            ) from exc
