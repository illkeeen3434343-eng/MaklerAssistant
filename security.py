"""
Security: bind every bina.az number to the Telegram user who first connected
it, and encrypt each saved session with a key derived from that user's id.

Rules enforced here:
  • A phone number is OWNED by the first Telegram id that connects it.
  • Another id may NOT use, read, or log in with that number.
  • One id may own several numbers (no limit here).
  • Each session file is encrypted with a per-user key, so even on-disk the
    file is useless to anyone but its owner (and only with the master key).

The ownership registry is a small JSON file. The bot is a single asyncio
process, so a plain file with atomic writes is safe.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path

SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "sessions"))
OWNERS_FILE = SESSIONS_DIR / "owners.json"
MASTER = os.getenv("SESSION_ENCRYPTION_KEY", "").strip()

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False
    InvalidToken = Exception


class OwnershipError(Exception):
    """Raised when a user tries to use a number owned by someone else."""
    def __init__(self, owner_id):
        self.owner_id = owner_id
        super().__init__(f"number is owned by another user ({owner_id})")


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _master_bytes() -> bytes | None:
    return MASTER.encode() if MASTER else None


def phone_hash(phone: str) -> str:
    """Stable, non-reversible id for a number (peppered with the master key)."""
    pepper = _master_bytes() or b"no-master-pepper"
    return hashlib.sha256(pepper + _digits(phone).encode()).hexdigest()


# ----------------------------------------------------------------- registry
def _load_owners() -> dict:
    if OWNERS_FILE.exists():
        try:
            return json.loads(OWNERS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_owners(data: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OWNERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, OWNERS_FILE)
    try:
        os.chmod(OWNERS_FILE, 0o600)
    except OSError:
        pass


def owner_of(phone: str):
    """Return the Telegram id that owns this number, or None if unclaimed."""
    return _load_owners().get(phone_hash(phone))


def claim(phone: str, owner_id: int) -> None:
    """Claim a number for owner_id. Raise OwnershipError if someone else owns it."""
    owners = _load_owners()
    h = phone_hash(phone)
    existing = owners.get(h)
    if existing is not None and str(existing) != str(owner_id):
        raise OwnershipError(existing)
    if existing is None:
        owners[h] = owner_id
        _save_owners(owners)


def release(phone: str, owner_id: int) -> bool:
    """Give up ownership of a number (only the owner may). Returns True if released."""
    owners = _load_owners()
    h = phone_hash(phone)
    if str(owners.get(h)) == str(owner_id):
        del owners[h]
        _save_owners(owners)
        return True
    return False


def numbers_of(owner_id: int) -> int:
    """How many numbers this user owns (count only; hashes aren't reversible)."""
    return sum(1 for v in _load_owners().values() if str(v) == str(owner_id))


# --------------------------------------------------------------- encryption
def _user_key(owner_id: int) -> bytes | None:
    mb = _master_bytes()
    if not mb or not _HAVE_CRYPTO:
        return None
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=str(owner_id).encode(), info=b"bina-session-v1")
    return base64.urlsafe_b64encode(hkdf.derive(mb))


def encrypt(owner_id: int, data: bytes) -> bytes:
    key = _user_key(owner_id)
    if not key:
        return data          # no master key set → stored as-is (still owner-scoped)
    return Fernet(key).encrypt(data)


def decrypt(owner_id: int, data: bytes) -> bytes | None:
    key = _user_key(owner_id)
    if not key:
        return data
    try:
        return Fernet(key).decrypt(data)
    except InvalidToken:
        return None          # wrong owner / wrong key / corrupt → treat as no session


def encryption_enabled() -> bool:
    return bool(_master_bytes()) and _HAVE_CRYPTO
