"""
User registry for the admin panel — a small JSON store (the bot is a single
process, so a file with atomic writes is enough).

Each user record:
  {
    "status": "pending" | "active" | "blocked",
    "tier":   "free" | "pro" | "diamond",
    "numbers": ["994557778899", ...],   # bina.az numbers they've connected
    "note": "",
    "created_at": "..."
  }

Tiers cap how many numbers a user may connect and how many price updates/day.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("SESSIONS_DIR", "sessions")).parent / "data"
USERS_FILE = DATA_DIR / "users.json"

TIERS = {
    "free":    {"max_numbers": 1, "max_daily_updates": 1},
    "pro":     {"max_numbers": 2, "max_daily_updates": 3},
    "diamond": {"max_numbers": 5, "max_daily_updates": 10},
}
STATUSES = ("pending", "active", "blocked")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _load() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ users
def ensure_user(user_id: int, default_status: str = "pending") -> dict:
    """Return the user record, creating a pending one on first sight."""
    data = _load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"status": default_status, "tier": "free",
                     "numbers": [], "note": "", "created_at": _now()}
        _save(data)
    return data[uid]


def get_user(user_id: int) -> dict | None:
    return _load().get(str(user_id))


def all_users() -> dict:
    return _load()


def set_status(user_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    data = _load(); uid = str(user_id)
    data.setdefault(uid, ensure_user(user_id))
    data[uid]["status"] = status
    _save(data)


def set_tier(user_id: int, tier: str) -> None:
    if tier not in TIERS:
        raise ValueError(tier)
    data = _load(); uid = str(user_id)
    data.setdefault(uid, ensure_user(user_id))
    data[uid]["tier"] = tier
    # if downgrading below current number count, mark the extras (kept, not deleted)
    _save(data)


def set_note(user_id: int, note: str) -> None:
    data = _load(); uid = str(user_id)
    data.setdefault(uid, ensure_user(user_id))
    data[uid]["note"] = note[:200]
    _save(data)


def is_active(user_id: int) -> bool:
    u = get_user(user_id)
    return bool(u and u["status"] == "active")


def tier_of(user_id: int) -> str:
    u = get_user(user_id)
    return u["tier"] if u else "free"


def max_numbers(user_id: int) -> int:
    return TIERS[tier_of(user_id)]["max_numbers"]


def numbers(user_id: int) -> list[str]:
    u = get_user(user_id)
    return list(u["numbers"]) if u else []


def add_number(user_id: int, phone: str) -> tuple[bool, str]:
    """Add a number if under the tier cap. Returns (ok, message)."""
    d = _digits(phone)
    data = _load(); uid = str(user_id)
    data.setdefault(uid, ensure_user(user_id))
    nums = data[uid]["numbers"]
    if d in nums:
        return True, "already connected"
    cap = TIERS[data[uid]["tier"]]["max_numbers"]
    if len(nums) >= cap:
        return False, (f"Your tier ({data[uid]['tier']}) allows {cap} number(s). "
                       f"Upgrade to connect more.")
    nums.append(d)
    _save(data)
    return True, "added"


def remove_number(user_id: int, phone: str) -> None:
    d = _digits(phone)
    data = _load(); uid = str(user_id)
    if uid in data and d in data[uid]["numbers"]:
        data[uid]["numbers"].remove(d)
        _save(data)
