"""Encrypt-at-rest helper for a secret an admin types into a Rule Designer
config form (today: a reference file's xlsx password) instead of naming a
server-side environment variable that already holds it. The master key
itself still lives only in server environment config — there's no
secrets-vault/KMS integration in this app's stack to lean on instead —
but this protects the secret from anyone who can read the stored config
(the reference-file index, a JSON blob on S3) or the audit log, which is
a broader audience than "has server/env access".
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_ENV_VAR = "REFERENCE_SECRETS_KEY"  # a Fernet key, generated once at deploy time


def _fernet() -> Fernet:
    key = os.getenv(_ENV_VAR)
    if not key:
        raise ValueError(
            f"'{_ENV_VAR}' is not set — cannot store or read an encrypted "
            f"reference-file password. Generate one with "
            f'`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` '
            f"and set it as a server environment variable."
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            f"could not decrypt the stored password — '{_ENV_VAR}' may have "
            f"changed since it was encrypted"
        ) from exc
