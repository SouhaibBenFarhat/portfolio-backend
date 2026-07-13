"""A transparently-encrypted text field.

Stores ciphertext in the database and returns plaintext to the app. The key is
derived from SECRET_KEY (see settings.FIELD_ENCRYPTION_KEY), so anyone who reads
the database directly sees only ciphertext.
"""

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models


def _fernet() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY)


class EncryptedTextField(models.TextField):
    """TextField whose value is Fernet-encrypted at rest, decrypted on load."""

    def from_db_value(self, value, expression, connection):
        if value in (None, ""):
            return value
        return _fernet().decrypt(value.encode()).decode()

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        return _fernet().encrypt(value.encode()).decode()
