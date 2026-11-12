# app/core/crypto.py
#
# Field-level encryption at rest for sensitive per-tenant columns.
# Currently used for Company.vobiz_auth_id / Company.vobiz_auth_token
# (see app/models/models.py) — these are third-party telephony
# credentials that customers enter in Settings, stored in a shared
# Postgres database, and previously saved as plain VARCHAR. If the
# database itself (or a backup/export of it) leaked, every customer's
# Vobiz credentials would leak with it.
#
# This is transparent to the rest of the app: anywhere the code reads
# `company.vobiz_auth_token`, SQLAlchemy decrypts on load and the value
# is a normal plaintext string — nothing in vobiz_service.py or the
# webhook flow needs to change. Encryption/decryption only happens at
# the DB boundary via the EncryptedString TypeDecorator below.
#
# Uses Fernet (AES-128-CBC + HMAC, from the `cryptography` package) —
# symmetric, authenticated, battle-tested for exactly this
# "encrypt small secrets at rest" use case. Not meant for huge blobs.

import logging
from typing import Optional

from sqlalchemy.types import TypeDecorator, Text

from app.core.config import settings

logger = logging.getLogger(__name__)

_fernet = None
_fernet_init_attempted = False


def _get_fernet():
    """Lazily builds the Fernet instance from settings.ENCRYPTION_KEY.
    Lazy on purpose: importing this module must never crash app startup
    just because ENCRYPTION_KEY isn't set yet (e.g. a fresh install where
    no company has entered Vobiz credentials yet) — it only raises the
    moment something actually tries to encrypt/decrypt a value."""
    global _fernet, _fernet_init_attempted
    if _fernet is not None:
        return _fernet
    if _fernet_init_attempted:
        return None
    _fernet_init_attempted = True

    if not settings.ENCRYPTION_KEY:
        logger.warning(
            "ENCRYPTION_KEY is not set — encrypted columns (Company.vobiz_auth_id/"
            "vobiz_auth_token) cannot be read or written until it is. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and set ENCRYPTION_KEY in .env."
        )
        return None

    from cryptography.fernet import Fernet
    try:
        _fernet = Fernet(settings.ENCRYPTION_KEY.encode())
    except Exception as e:
        logger.error(f"ENCRYPTION_KEY is set but invalid ({e}) — must be a 32-byte urlsafe-base64 Fernet key")
        return None
    return _fernet


class EncryptedString(TypeDecorator):
    """A String/Text column that's encrypted at rest with Fernet.

    Storing NULL still stores NULL (no encryption of "nothing set" —
    keeps `company.vobiz_auth_token is None` checks working unchanged).
    Empty string is stored as empty string for the same reason.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[str], dialect) -> Optional[str]:
        if value is None or value == "":
            return value
        fernet = _get_fernet()
        if fernet is None:
            raise RuntimeError(
                "Cannot save an encrypted field: ENCRYPTION_KEY is unset or invalid. "
                "Set ENCRYPTION_KEY in .env before saving Vobiz credentials."
            )
        return fernet.encrypt(value.encode()).decode()

    def process_result_value(self, value: Optional[str], dialect) -> Optional[str]:
        if value is None or value == "":
            return value
        fernet = _get_fernet()
        if fernet is None:
            # Same "unset key" case, but on read. Fail loudly rather than
            # silently returning ciphertext, which would look like a
            # valid-but-wrong auth token and fail Vobiz calls confusingly.
            raise RuntimeError(
                "Cannot read an encrypted field: ENCRYPTION_KEY is unset or invalid."
            )
        try:
            return fernet.decrypt(value.encode()).decode()
        except Exception:
            # Most likely cause: the value was written before this column
            # was encrypted (plaintext already in the DB from before this
            # change), or ENCRYPTION_KEY was rotated without re-encrypting
            # existing rows. Log and surface the raw value rather than
            # crashing every request that touches this company — an
            # operator needs to see this and re-save the credential.
            logger.error(
                "Failed to decrypt an encrypted field — value may predate encryption "
                "being enabled, or ENCRYPTION_KEY was rotated. Re-save this field via "
                "Settings to fix it."
            )
            return value
