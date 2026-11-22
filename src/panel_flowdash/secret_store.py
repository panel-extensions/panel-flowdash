"""Secrets in the project file, such as model provider keys.

A secret is written in the project file either as a reference to an
environment variable, ``${OPENROUTER_API_KEY}``, or, opted into from the
admin view, encrypted with Fernet: ``{encrypted: <token>, key_id: <id>}``.
The resolved value is never written to the file, sent to the browser or
logged, and never put into ``os.environ``: it is handed to the client
that needs it, e.g. a model provider.

The encryption key is, in order, ``FLOWDASH_SECRET_KEY`` (several keys,
comma-separated, the first encrypting and all decrypting, for rotation),
the OS keyring (with ``keyring`` installed, for a local single-user setup)
or a key file outside the project, ``~/.config/flowdash/secret.key``
(``FLOWDASH_SECRET_KEY_FILE``), made with mode 0600 the first time a
secret is stored. Workers of one deployment share ``FLOWDASH_SECRET_KEY``.
Each stored value names the key it was encrypted with by ``key_id``, so a
value whose key is gone says so and asks to be entered again.

Encryption needs ``cryptography`` (``pip install 'panel-flowdash[secrets]'``).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
import typing as t
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from panel_flowdash.project import expand_env

logger = logging.getLogger(__name__)

__all__ = [
    "KEY_ENV",
    "KEY_FILE_ENV",
    "SecretError",
    "SecretInfo",
    "describe_secret",
    "encrypt_secret",
    "encryption_available",
    "env_reference",
    "is_secret",
    "resolve_secret",
]

KEY_ENV = "FLOWDASH_SECRET_KEY"

KEY_FILE_ENV = "FLOWDASH_SECRET_KEY_FILE"

KEYRING_SERVICE = "panel-flowdash"

KEYRING_NAME = "secret-key"

INSTALL_HINT = "Storing keys encrypted needs `pip install 'panel-flowdash[secrets]'`."

_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_KEY_LOCK = threading.Lock()


class SecretError(ValueError):
    """A secret that cannot be resolved; the message never holds its value."""


@dataclass(frozen=True)
class SecretInfo:
    """What the admin view may show about a secret: where it comes from, never its value.

    ``source`` is ``"env"``, ``"stored"`` or None (not set). ``env`` names
    the variable of a reference; ``last4`` is the value's last four
    characters when it resolves, and ``error`` why it does not.
    """

    source: str | None
    env: str | None = None
    last4: str | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        """Whether the secret resolves to a value."""
        return self.source is not None and self.error is None


def env_reference(name: str) -> str:
    """Return the project file's reference to the environment variable *name*."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise SecretError(f"{name!r} is not an environment variable name.")
    return f"${{{name}}}"


def is_secret(value: t.Any) -> bool:
    """Whether *value* is written the way a secret may be: a reference or an encrypted value."""
    if isinstance(value, str):
        return bool(_REFERENCE.match(value))
    return (
        isinstance(value, Mapping)
        and set(value) == {"encrypted", "key_id"}
        and all(isinstance(v, str) and v for v in value.values())
    )


def check_secret(value: t.Any, what: str) -> str | None:
    """Return why *value* may not stand for a secret in the project file, or None."""
    if value is None or is_secret(value):
        return None
    return (
        f"`{what}` is a reference to an environment variable, e.g. `${{OPENAI_API_KEY}}`, "
        "or a key stored encrypted from the admin view; a key is never written in the file."
    )


def encryption_available() -> bool:
    """Whether ``cryptography`` is installed, so secrets can be stored encrypted."""
    try:
        import cryptography.fernet  # noqa: F401
    except ImportError:
        return False
    return True


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def _key_file() -> Path:
    configured = os.environ.get(KEY_FILE_ENV)
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "flowdash" / "secret.key"


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return None, ()
    return keyring, (KeyringError, RuntimeError)


def _from_keyring() -> bytes | None:
    keyring, errors = _keyring()
    if keyring is None:
        return None
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_NAME)
    except errors:
        return None
    return value.encode() if value else None


def _to_keyring(key: bytes) -> bool:
    keyring, errors = _keyring()
    if keyring is None:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_NAME, key.decode())
        return _from_keyring() == key
    except errors:
        return False


def _read_key_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes().strip() or None
    except OSError:
        return None


def _write_key_file(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key + b"\n")


def _keys(create: bool = False) -> list[bytes]:
    """Return the encryption keys, the one that encrypts first; with *create*, make one if none."""
    configured = os.environ.get(KEY_ENV)
    if configured:
        return [key.strip().encode() for key in configured.split(",") if key.strip()]
    with _KEY_LOCK:
        key = _from_keyring() or _read_key_file(_key_file())
        if key is None and create:
            from cryptography.fernet import Fernet

            key = Fernet.generate_key()
            if not _to_keyring(key):
                _write_key_file(_key_file(), key)
            logger.info("Made a new key for the secrets stored in project files.")
        return [key] if key else []


def _fernet(keys: list[bytes]):
    from cryptography.fernet import Fernet, MultiFernet

    try:
        return MultiFernet([Fernet(key) for key in keys])
    except (ValueError, TypeError):
        raise SecretError(
            f"The secret key is not a Fernet key; check {KEY_ENV} or the key file."
        ) from None


def encrypt_secret(value: str) -> dict[str, str]:
    """Return *value* encrypted, as the project file stores it: ``{encrypted, key_id}``."""
    if not isinstance(value, str) or not value:
        raise SecretError("The secret is empty.")
    if not encryption_available():
        raise SecretError(INSTALL_HINT)
    keys = _keys(create=True)
    token = _fernet(keys[:1]).encrypt(value.encode()).decode()
    return {"encrypted": token, "key_id": _key_id(keys[0])}


def _decrypt(value: Mapping[str, str]) -> str:
    if not encryption_available():
        raise SecretError(INSTALL_HINT)
    keys = _keys()
    if not keys:
        raise SecretError("The key it was stored with is not available here; enter it again.")
    from cryptography.fernet import InvalidToken

    # The key it names first; the others still decrypt values made before a rotation.
    keys.sort(key=lambda key: _key_id(key) != value.get("key_id"))
    try:
        return _fernet(keys).decrypt(value["encrypted"].encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        raise SecretError(
            "It cannot be decrypted with the keys here (key "
            f"{value.get('key_id')}); enter it again."
        ) from None


def resolve_secret(value: t.Any, environ: Mapping[str, str] | None = None) -> str | None:
    """Return the secret *value* stands for, or None when it is not set.

    A reference reads its environment variable, an encrypted value is
    decrypted. Raises `SecretError` for a variable that is not set or a value
    that cannot be decrypted.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not _REFERENCE.match(value):
            raise SecretError("A secret is a ${VARIABLE} reference or an encrypted value.")
        try:
            resolved = expand_env(value, environ)
        except ValueError as exc:
            raise SecretError(str(exc)) from None
        if not resolved:
            raise SecretError(f"The environment variable {value[2:-1]} is empty.")
        return resolved
    if is_secret(value):
        return _decrypt(value)
    raise SecretError("A secret is a ${VARIABLE} reference or an encrypted value.")


def describe_secret(value: t.Any, environ: Mapping[str, str] | None = None) -> SecretInfo:
    """Return where *value* comes from and whether it resolves, without its value."""
    if value is None:
        return SecretInfo(None)
    match = _REFERENCE.match(value) if isinstance(value, str) else None
    source = "env" if match else "stored"
    env = match.group(1) if match else None
    try:
        resolved = resolve_secret(value, environ)
    except SecretError as exc:
        return SecretInfo(source, env, error=str(exc))
    return SecretInfo(
        source, env, last4=resolved[-4:] if resolved and len(resolved) >= 8 else None
    )


def fernet_key() -> str:
    """Return a new key for ``FLOWDASH_SECRET_KEY``."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()
