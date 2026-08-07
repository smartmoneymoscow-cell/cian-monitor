"""
Data store — JSON-based persistence for users, listings, favorites, monitoring.
"""

import json
import time
from pathlib import Path
from threading import Lock

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

_lock = Lock()


def _load(filename, default=None):
    path = DATA_DIR / filename
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return default if default is not None else {}


def _save(filename, data):
    path = DATA_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  USERS
# ═══════════════════════════════════════════════════════════════

DEFAULT_SETTINGS = {
    "district": "Южное Бутово",
    "rooms": [1, 2],
    "price_min": 0,
    "price_max": 15_000_000,
    "area_min": 30,
    "sources": ["cian"],
    "chat_id": None,         # Telegram chat_id for notifications
    "monitoring_active": False,
    "last_check": 0,
}


def get_user(uid):
    """Get user settings. Creates default if not exists."""
    with _lock:
        users = _load("users.json", {})
        if uid not in users:
            users[uid] = {**DEFAULT_SETTINGS}
            _save("users.json", users)
        return users[uid]


def save_user(uid, settings):
    with _lock:
        users = _load("users.json", {})
        users[uid] = settings
        _save("users.json", users)


def set_chat_id(uid, chat_id):
    """Link Telegram chat_id to user."""
    with _lock:
        users = _load("users.json", {})
        if uid not in users:
            users[uid] = {**DEFAULT_SETTINGS}
        users[uid]["chat_id"] = chat_id
        _save("users.json", users)


def get_all_active_users():
    """Get all users with monitoring enabled and chat_id set."""
    with _lock:
        users = _load("users.json", {})
        return [
            (uid, u) for uid, u in users.items()
            if u.get("monitoring_active") and u.get("chat_id")
        ]


# ═══════════════════════════════════════════════════════════════
#  LISTINGS (global store)
# ═══════════════════════════════════════════════════════════════

def get_all_listings():
    with _lock:
        return _load("listings.json", {})


def save_all_listings(listings):
    with _lock:
        _save("listings.json", listings)


def get_listing(listing_id):
    with _lock:
        listings = _load("listings.json", {})
        return listings.get(listing_id)


def upsert_listing(listing):
    """Insert or update a listing. Returns (listing, is_new, price_changed, old_price)."""
    with _lock:
        listings = _load("listings.json", {})
        lid = listing["id"]
        now = time.time()

        if lid in listings:
            existing = listings[lid]
            old_price = existing.get("price_num", 0)
            new_price = listing.get("price_num", 0)
            price_changed = old_price and new_price and old_price != new_price

            # Update fields
            existing.update(listing)
            existing["last_seen"] = now
            existing["active"] = True

            if price_changed:
                if "price_history" not in existing:
                    existing["price_history"] = []
                existing["price_history"].append({
                    "price": old_price,
                    "changed_at": now,
                })
                existing["price_changed"] = True
                existing["old_price"] = old_price
                existing["price_changed_at"] = now

            listings[lid] = existing
            _save("listings.json", listings)
            return existing, False, price_changed, old_price
        else:
            listing["first_seen"] = now
            listing["last_seen"] = now
            listing["active"] = True
            listings[lid] = listing
            _save("listings.json", listings)
            return listing, True, False, None


def mark_inactive(listing_id):
    with _lock:
        listings = _load("listings.json", {})
        if listing_id in listings:
            listings[listing_id]["active"] = False
            listings[listing_id]["removed_at"] = time.time()
            _save("listings.json", listings)


# ═══════════════════════════════════════════════════════════════
#  USER SEEN LISTINGS
# ═══════════════════════════════════════════════════════════════

def get_seen(uid):
    with _lock:
        seen = _load("seen.json", {})
        return set(seen.get(uid, []))


def add_seen(uid, listing_id):
    with _lock:
        seen = _load("seen.json", {})
        if uid not in seen:
            seen[uid] = []
        if listing_id not in seen[uid]:
            seen[uid].append(listing_id)
            seen[uid] = seen[uid][-10000:]
        _save("seen.json", seen)


def mark_price_seen(uid, listing_id):
    """Mark that user was notified about price change."""
    with _lock:
        key = f"{uid}_price_seen"
        data = _load("price_seen.json", {})
        if key not in data:
            data[key] = []
        if listing_id not in data[key]:
            data[key].append(listing_id)
            data[key] = data[key][-5000:]
        _save("price_seen.json", data)


def is_price_seen(uid, listing_id):
    with _lock:
        data = _load("price_seen.json", {})
        return listing_id in data.get(f"{uid}_price_seen", [])


# ═══════════════════════════════════════════════════════════════
#  FAVORITES
# ═══════════════════════════════════════════════════════════════

def get_favorites(uid):
    with _lock:
        favs = _load("favorites.json", {})
        return favs.get(uid, [])


def add_favorite(uid, listing):
    with _lock:
        favs = _load("favorites.json", {})
        if uid not in favs:
            favs[uid] = []
        # Remove if exists (update)
        favs[uid] = [f for f in favs[uid] if f.get("id") != listing.get("id")]
        listing["is_favorite"] = True
        favs[uid].insert(0, listing)
        _save("favorites.json", favs)
        return favs[uid]


def remove_favorite(uid, listing_id):
    with _lock:
        favs = _load("favorites.json", {})
        if uid in favs:
            favs[uid] = [f for f in favs[uid] if f.get("id") != listing_id]
        _save("favorites.json", favs)
        return favs.get(uid, [])


def is_favorite(uid, listing_id):
    favs = get_favorites(uid)
    return any(f.get("id") == listing_id for f in favs)


# ═══════════════════════════════════════════════════════════════
#  MONITORING STATE
# ═══════════════════════════════════════════════════════════════

def get_monitor_state():
    with _lock:
        return _load("monitor_state.json", {
            "last_global_check": 0,
            "check_count": 0,
            "errors": 0,
        })


def save_monitor_state(state):
    with _lock:
        _save("monitor_state.json", state)
