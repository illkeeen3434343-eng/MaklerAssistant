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
def ensure_user(user_id: int, default_status: str = "pending",
                username: str = "", name: str = "") -> dict:
    """Return the user record, creating a pending one on first sight.

    username/name are refreshed each time we see the user so the admin panel
    and reports always show current values.
    """
    data = _load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"status": default_status, "tier": "free",
                     "numbers": [], "note": "", "created_at": _now(),
                     "username": username, "name": name}
        _save(data)
    else:
        changed = False
        if username and data[uid].get("username") != username:
            data[uid]["username"] = username; changed = True
        if name and data[uid].get("name") != name:
            data[uid]["name"] = name; changed = True
        if changed:
            _save(data)
    return data[uid]


def label_for(user_id: int) -> str:
    """A clickable '/id @username (Name)' label for admin lists."""
    u = _load().get(str(user_id)) or {}
    un = u.get("username")
    nm = u.get("name")
    parts = [f"/{user_id}"]
    if un:
        parts.append(f"@{un}")
    if nm:
        parts.append(f"({nm})")
    return " ".join(parts)


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


# ---------------------------------------------------------------- usage stats
STATS_FILE = DATA_DIR / "stats.json"


def _load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_stats(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, STATS_FILE)


def track(user_id: int, action: str) -> None:
    """Record one user action (button press / command) for statistics."""
    if not action:
        return
    data = _load_stats()
    uid = str(user_id)
    rec = data.setdefault(uid, {"actions": {}, "first": _now(), "last": _now(),
                                "days": {}, "total": 0})
    rec["actions"][action] = rec["actions"].get(action, 0) + 1
    rec["total"] = rec.get("total", 0) + 1
    rec["last"] = _now()
    day = _now()[:10]
    rec["days"][day] = rec["days"].get(day, 0) + 1
    # keep the day map small
    if len(rec["days"]) > 90:
        for k in sorted(rec["days"])[:-90]:
            rec["days"].pop(k, None)
    _save_stats(data)


def stats_summary(top_n: int = 8) -> dict:
    """Aggregate statistics across all users."""
    data = _load_stats()
    actions: dict[str, int] = {}
    per_user = []
    active_today = 0
    today = _now()[:10]
    for uid, rec in data.items():
        for a, c in rec.get("actions", {}).items():
            actions[a] = actions.get(a, 0) + c
        per_user.append((uid, rec.get("total", 0), rec.get("last", "")))
        if rec.get("days", {}).get(today):
            active_today += 1
    per_user.sort(key=lambda x: -x[1])
    return {
        "users": len(data),
        "active_today": active_today,
        "total_actions": sum(actions.values()),
        "top_actions": sorted(actions.items(), key=lambda x: -x[1])[:top_n],
        "top_users": per_user[:top_n],
    }


def user_stats(user_id: int) -> dict | None:
    return _load_stats().get(str(user_id))
