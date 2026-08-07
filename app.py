#!/usr/bin/env python3
"""
CIAN Monitor — Flask server + Telegram Bot + Mini App
Multi-user, auto-monitoring every 2h, price change detection.
"""

import json
import os
import sys
import time
import logging
import threading
import requests
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

import store

# ─── Config ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cian-app")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CF_WORKER = os.environ.get("CLOUDFLARE_WORKER_URL", "")
BOT_URL = os.environ.get("BOT_URL", "")
MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "7200"))  # 2 hours

# ─── Telegram API ───

def tg(method, data=None, timeout=30):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=data or {}, timeout=timeout)
        return r.json()
    except Exception as e:
        log.error(f"TG {method}: {e}")
        return {"ok": False}


# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════

DISTRICTS = {
    "Южное Бутово": {"id": 110, "lat": 55.538, "lon": 37.533},
    "Северное Бутово": {"id": 107, "lat": 55.570, "lon": 37.520},
    "Ясенево": {"id": 103, "lat": 55.606, "lon": 37.534},
    "Тёплый Стан": {"id": 108, "lat": 55.620, "lon": 37.498},
    "Коньково": {"id": 103, "lat": 55.637, "lon": 37.519},
    "Чертаново": {"id": None, "lat": 55.590, "lon": 37.595},
    "Бирюлёво": {"id": 9, "lat": 55.587, "lon": 37.647},
    "ЦАО": {"id": None, "lat": 55.755, "lon": 37.617},
    "Все районы": {"id": None, "lat": 55.755, "lon": 37.617},
}

DISTRICT_KEYWORDS = {
    "Южное Бутово": ["южное бутово", "южное-бутово", "южнобутовская", "бутово"],
    "Северное Бутово": ["северное бутово", "северное-бутово"],
    "Ясенево": ["ясенево"],
    "Тёплый Стан": ["тёплый стан", "теплый стан"],
    "Коньково": ["коньково"],
    "Чертаново": ["чертаново"],
    "Бирюлёво": ["бирюлёво", "бирюлево"],
    "ЦАО": ["центральный"],
}

PRICE_PRESETS = [
    {"label": "до 10 млн", "min": 0, "max": 10_000_000},
    {"label": "до 15 млн", "min": 0, "max": 15_000_000},
    {"label": "10–15 млн", "min": 10_000_000, "max": 15_000_000},
    {"label": "15–20 млн", "min": 15_000_000, "max": 20_000_000},
    {"label": "20–30 млн", "min": 20_000_000, "max": 30_000_000},
    {"label": "30–50 млн", "min": 30_000_000, "max": 50_000_000},
    {"label": "Без ограничений", "min": 0, "max": 0},
]

ROOMS_OPTIONS = [
    {"label": "Студия", "value": 0},
    {"label": "1-комн.", "value": 1},
    {"label": "2-комн.", "value": 2},
    {"label": "3-комн.", "value": 3},
    {"label": "4-комн.", "value": 4},
    {"label": "5-комн.", "value": 5},
]

SOURCES = [
    {"id": "cian", "name": "CIAN", "url": "https://www.cian.ru", "icon": "🏠"},
]


# ═══════════════════════════════════════════════════════════════
#  CIAN API
# ═══════════════════════════════════════════════════════════════

_geocode_cache = {}

def geocode_address(address):
    if not address or address == "Адрес не указан":
        return None, None
    key = address.lower().strip()
    if key in _geocode_cache:
        return _geocode_cache[key]
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
            params={"q": f"{address}, Москва", "format": "json", "limit": 1},
            headers={"User-Agent": "cian-monitor/1.0"}, timeout=10)
        data = r.json()
        if data:
            result = (float(data[0]["lat"]), float(data[0]["lon"]))
            _geocode_cache[key] = result
            return result
    except:
        pass
    _geocode_cache[key] = (None, None)
    return None, None


def build_json_query(settings, page=1):
    jq = {
        "_type": "flatsale",
        "engine_version": {"type": "term", "value": 2},
        "page": {"type": "term", "value": page},
        "region": {"type": "terms", "value": [1]},
    }
    rooms = settings.get("rooms", [1, 2])
    if rooms:
        jq["room"] = {"type": "terms", "value": rooms}
    pmin, pmax = settings.get("price_min", 0), settings.get("price_max", 0)
    if pmin or pmax:
        pv = {}
        if pmin: pv["gte"] = pmin
        if pmax: pv["lte"] = pmax
        jq["price"] = {"type": "range", "value": pv}
    amin = settings.get("area_min", 0)
    if amin:
        jq["total_area"] = {"type": "range", "value": {"gte": amin}}
    district = settings.get("district", "")
    did = DISTRICTS.get(district, {}).get("id")
    if did:
        jq["district"] = {"type": "terms", "value": [did]}
    return {"jsonQuery": jq}


def filter_by_district(offers, district_name):
    keywords = DISTRICT_KEYWORDS.get(district_name, [])
    if not keywords:
        return offers
    filtered = []
    for o in offers:
        geo = o.get("geo", {})
        parts = []
        for a in geo.get("address", []):
            parts.append(a.get("title", "") if isinstance(a, dict) else a)
        text = f"{' '.join(parts)} {o.get('description', '')}".lower()
        if any(kw in text for kw in keywords):
            filtered.append(o)
    return filtered


def fetch_from_cian(settings, max_pages=3):
    if not CF_WORKER:
        return []
    all_offers = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.post(f"{CF_WORKER.rstrip('/')}/search",
                json=build_json_query(settings, page), timeout=30)
            data = r.json()
            if data.get("error"):
                break
            offers = data.get("offers", [])
            if not offers:
                break
            district = settings.get("district", "")
            if district and district != "Все районы":
                offers = filter_by_district(offers, district)
            all_offers.extend(offers)
            time.sleep(1)
        except Exception as e:
            log.error(f"CIAN fetch p{page}: {e}")
            break
    return all_offers


def parse_offer(offer):
    try:
        oid = str(offer.get("id") or offer.get("cianId", ""))
        if not oid:
            return None

        bargain = offer.get("bargainTerms", {})
        price_raw = bargain.get("price", offer.get("formattedFullPrice", ""))
        if isinstance(price_raw, (int, float)) and price_raw:
            price_str = f"{price_raw:,.0f} ₽".replace(",", " ")
        elif isinstance(price_raw, str) and price_raw:
            price_str = price_raw
        else:
            price_str = "Цена не указана"

        price_num = price_raw if isinstance(price_raw, (int, float)) else 0

        price_per_m = ""
        area_val = 0
        try:
            area_val = float(offer.get("totalArea", 0) or 0)
        except:
            pass
        if price_num and area_val:
            price_per_m = f"{price_num/area_val:,.0f} ₽/м²".replace(",", " ")

        rooms = offer.get("roomsCount", "?")
        try: rooms = int(rooms)
        except: rooms = "?"

        area = offer.get("totalArea", "")
        try: area = float(area) if area else ""
        except: area = ""

        floor = offer.get("floorNumber", "")
        try: floor = int(floor) if floor else ""
        except: floor = ""
        ft = ""
        bld = offer.get("building")
        if isinstance(bld, dict):
            try: ft = int(bld.get("floorsTotal", "") or "")
            except: ft = ""

        geo = offer.get("geo", {})
        addr_parts = []
        for a in geo.get("address", []):
            addr_parts.append(a.get("title", "") if isinstance(a, dict) else a)
        address = ", ".join(filter(None, addr_parts)) or "Адрес не указан"

        metro = ""
        mi = geo.get("metro", [])
        if mi and isinstance(mi, list):
            m = mi[0]
            name = m.get("name", "")
            t = m.get("time", "")
            if name:
                icon = "🚶" if m.get("type", "foot") == "foot" else "🚗"
                metro = f"{icon} {name}" + (f", {t} мин" if t else "")

        lat, lon = None, None
        coords = geo.get("coordinates")
        if isinstance(coords, dict):
            lat, lon = coords.get("lat"), coords.get("lng")
        if not lat or not lon:
            lat, lon = geocode_address(address)

        desc = (offer.get("description", "") or "")[:500]

        photos = []
        for p in offer.get("photos", []):
            if isinstance(p, dict):
                url = p.get("url", p.get("full", p.get("thumbnail", "")))
                if url: photos.append(url)
            elif isinstance(p, str):
                photos.append(p)

        link = offer.get("fullUrl", "") or f"https://www.cian.ru/item/{oid}/"

        date_str = offer.get("createdAt", offer.get("addedAt", ""))
        if date_str:
            try:
                date_str = datetime.fromisoformat(date_str.replace("Z", "+00:00")).strftime("%d.%m.%Y")
            except:
                pass

        return {
            "id": oid, "price": price_str, "price_num": price_num,
            "price_per_m": price_per_m, "rooms": rooms, "area": area,
            "floor": floor, "floors_total": ft, "address": address,
            "metro": metro, "lat": lat, "lon": lon, "description": desc,
            "photos": photos[:10], "link": link, "date": date_str,
            "source": "cian", "active": True,
        }
    except Exception as e:
        log.warning(f"Parse: {e}")
        return None


def matches_user_filters(listing, settings):
    """Check if listing matches user's filters."""
    # Rooms
    rooms = settings.get("rooms", [])
    if rooms and listing.get("rooms") not in rooms and listing.get("rooms") != "?":
        return False
    # Price
    pmin = settings.get("price_min", 0)
    pmax = settings.get("price_max", 0)
    pnum = listing.get("price_num", 0)
    if pmin and pnum and pnum < pmin:
        return False
    if pmax and pnum and pnum > pmax:
        return False
    # Area
    amin = settings.get("area_min", 0)
    area = listing.get("area", 0)
    if amin and area and isinstance(area, (int, float)) and area < amin:
        return False
    # District
    district = settings.get("district", "")
    if district and district != "Все районы":
        keywords = DISTRICT_KEYWORDS.get(district, [])
        if keywords:
            text = f"{listing.get('address', '')} {listing.get('description', '')}".lower()
            if not any(kw in text for kw in keywords):
                return False
    return True


# ═══════════════════════════════════════════════════════════════
#  MONITORING (background thread)
# ═══════════════════════════════════════════════════════════════

def run_monitor_cycle():
    """One monitoring cycle: fetch, detect changes, notify users."""
    log.info("🔄 Monitor cycle start")
    state = store.get_monitor_state()

    # Use a "global" settings to fetch broad results
    # We'll filter per-user after
    global_settings = {
        "district": "Все районы",
        "rooms": [0, 1, 2, 3, 4, 5],
        "price_min": 0,
        "price_max": 0,
        "area_min": 0,
    }

    # Fetch from CIAN
    offers_raw = fetch_from_cian(global_settings, max_pages=5)
    log.info(f"  Fetched {len(offers_raw)} raw offers")

    new_count = 0
    changed_count = 0

    for offer in offers_raw:
        parsed = parse_offer(offer)
        if not parsed:
            continue

        listing, is_new, price_changed, old_price = store.upsert_listing(parsed)

        if is_new:
            new_count += 1
        if price_changed:
            changed_count += 1

    # Mark listings not seen in this cycle as potentially inactive
    all_listings = store.get_all_listings()
    now = time.time()
    inactive_count = 0
    fetched_ids = set()
    for offer in offers_raw:
        oid = str(offer.get("id") or offer.get("cianId", ""))
        if oid:
            fetched_ids.add(oid)

    for lid, ldata in all_listings.items():
        if ldata.get("active") and lid not in fetched_ids:
            # Don't mark inactive immediately — give it 3 cycles (6 hours)
            last_seen = ldata.get("last_seen", 0)
            if now - last_seen > 6 * 3600:
                store.mark_inactive(lid)
                inactive_count += 1

    # Notify each active user
    active_users = store.get_all_active_users()
    for uid, user_settings in active_users:
        chat_id = user_settings.get("chat_id")
        if not chat_id:
            continue

        seen = store.get_seen(uid)
        user_listings = [l for l in all_listings.values()
                         if l.get("active") and matches_user_filters(l, user_settings)]

        notify_new = 0
        notify_changed = 0

        for listing in user_listings:
            lid = listing["id"]

            # New listing for this user
            if lid not in seen:
                store.add_seen(uid, lid)
                notify_new += 1
                send_listing_notification(chat_id, listing, "new")
                time.sleep(0.5)
                continue

            # Price changed
            if listing.get("price_changed") and not store.is_price_seen(uid, lid):
                store.mark_price_seen(uid, lid)
                notify_changed += 1
                send_listing_notification(chat_id, listing, "price_changed")
                time.sleep(0.5)

        # Update user's last check
        user_settings["last_check"] = now
        store.save_user(uid, user_settings)

        if notify_new or notify_changed:
            log.info(f"  User {uid}: {notify_new} new, {notify_changed} price changes")

    state["last_global_check"] = now
    state["check_count"] = state.get("check_count", 0) + 1
    state["last_new_count"] = new_count
    state["last_changed_count"] = changed_count
    store.save_monitor_state(state)

    log.info(f"🔄 Monitor cycle done: {new_count} new, {changed_count} price changes, {inactive_count} removed")


def send_listing_notification(chat_id, listing, event_type="new"):
    """Send listing to Telegram with appropriate label."""
    text = format_listing_text(listing, event_type)

    # Inline keyboard
    fav_text = "⭐ В избранное" if not listing.get("is_favorite") else "✅ В избранном"
    keyboard = {"inline_keyboard": [[
        {"text": fav_text, "callback_data": f"fav_{listing['id']}"},
        {"text": "🏠 Открыть", "url": listing.get("link", "#")},
    ]]}

    photos = listing.get("photos", [])
    if photos:
        media = []
        for i, url in enumerate(photos[:10]):
            item = {"type": "photo", "media": url}
            if i == 0:
                item["caption"] = text
                item["parse_mode"] = "HTML"
            media.append(item)
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
                json={"chat_id": chat_id, "media": media}, timeout=30)
            if r.status_code == 200:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": "👇", "reply_markup": keyboard}, timeout=10)
                return True
        except:
            pass

    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "reply_markup": keyboard, "disable_web_page_preview": False}, timeout=30)
        return r.status_code == 200
    except:
        return False


def format_listing_text(listing, event_type="new"):
    lines = []

    if event_type == "new":
        lines.append("🏠 <b>Новое объявление</b>")
    elif event_type == "price_changed":
        old = listing.get("old_price", 0)
        new = listing.get("price_num", 0)
        old_str = f"{old:,.0f} ₽".replace(",", " ")
        new_str = f"{new:,.0f} ₽".replace(",", " ")
        direction = "📈" if new > old else "📉"
        lines.append(f"{direction} <b>Изменение цены</b>")
        lines.append(f"   Было: {old_str} → Стало: {new_str}")
        if listing.get("price_changed_at"):
            dt = datetime.fromtimestamp(listing["price_changed_at"]).strftime("%d.%m.%Y %H:%M")
            lines.append(f"   📅 Дата изменения: {dt}")

    lines.append(f"\n💰 <b>{listing['price']}</b>")
    if listing.get("price_per_m"):
        lines.append(f"   💰 {listing['price_per_m']}")

    params = []
    if listing.get("rooms") is not None:
        names = {0: "Студия", 1: "1-комн.", 2: "2-комн.", 3: "3-комн.", 4: "4-комн."}
        params.append(names.get(listing["rooms"], f"{listing['rooms']}-комн."))
    if listing.get("area"):
        params.append(f"{listing['area']} м²")
    if listing.get("floor") and listing.get("floors_total"):
        params.append(f"эт. {listing['floor']}/{listing['floors_total']}")
    if params:
        lines.append("   📐 " + " · ".join(str(p) for p in params))

    lines.append(f"   📍 {listing.get('address', '—')}")
    if listing.get("metro"):
        lines.append(f"   🚇 {listing['metro']}")
    if listing.get("date"):
        lines.append(f"   📅 {listing['date']}")
    lines.append(f"   🌐 CIAN")

    status = "🟢 Активно" if listing.get("active", True) else "🔴 Снято с продажи"
    lines.append(f"   {status}")

    if listing.get("description"):
        lines.append(f"\n{listing['description'][:200]}")
    lines.append(f'\n🔗 <a href="{listing.get("link", "#")}">Открыть на CIAN</a>')

    return "\n".join(lines)


def monitor_loop():
    """Background monitoring loop."""
    while True:
        try:
            run_monitor_cycle()
        except Exception as e:
            log.error(f"Monitor error: {e}")
        log.info(f"⏳ Next check in {MONITOR_INTERVAL}s")
        time.sleep(MONITOR_INTERVAL)


# ═══════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__, static_folder="webapp", static_url_path="")


@app.route("/")
@app.route("/miniapp")
def index():
    return send_from_directory("webapp", "index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": bool(BOT_TOKEN), "worker": bool(CF_WORKER)})


# ─── API: Config ───

@app.route("/api/config")
def api_config():
    return jsonify({
        "districts": {k: {"lat": v["lat"], "lon": v["lon"]} for k, v in DISTRICTS.items()},
        "price_presets": PRICE_PRESETS,
        "rooms_options": ROOMS_OPTIONS,
        "sources": SOURCES,
    })


# ─── API: Settings ───

@app.route("/api/settings")
def api_get_settings():
    uid = request.args.get("user_id", "default")
    return jsonify(store.get_user(uid))

@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.json or {}
    uid = str(data.pop("user_id", "default"))
    current = store.get_user(uid)
    current.update(data)
    store.save_user(uid, current)
    return jsonify({"success": True, "settings": current})


# ─── API: Search ───

@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.json or {}
    uid = str(data.get("user_id", "default"))
    settings = data.get("settings") or store.get_user(uid)

    offers_raw = fetch_from_cian(settings, max_pages=data.get("pages", 3))
    listings = []
    for offer in offers_raw:
        parsed = parse_offer(offer)
        if parsed:
            # Store globally
            store.upsert_listing(parsed)
            # Mark as seen by this user
            store.add_seen(uid, parsed["id"])
            parsed["is_favorite"] = store.is_favorite(uid, parsed["id"])
            listings.append(parsed)

    return jsonify({"success": True, "count": len(listings), "listings": listings})


# ─── API: Feed (cached + filtered per user) ───

@app.route("/api/feed")
def api_feed():
    uid = request.args.get("user_id", "default")
    settings = store.get_user(uid)
    all_listings = store.get_all_listings()

    # Filter by user settings and active status
    user_listings = []
    for lid, l in all_listings.items():
        if not l.get("active", True):
            continue
        if matches_user_filters(l, settings):
            l["is_favorite"] = store.is_favorite(uid, lid)
            user_listings.append(l)

    # Sort by first_seen desc
    user_listings.sort(key=lambda x: x.get("first_seen", 0), reverse=True)

    return jsonify({"listings": user_listings, "count": len(user_listings)})


# ─── API: Favorites ───

@app.route("/api/favorites")
def api_favorites():
    uid = request.args.get("user_id", "default")
    favs = store.get_favorites(uid)
    q = request.args.get("q", "").lower()
    if q:
        favs = [f for f in favs if q in json.dumps(f, ensure_ascii=False).lower()]
    # Enrich with current status from global store
    all_listings = store.get_all_listings()
    for f in favs:
        current = all_listings.get(f["id"])
        if current:
            f["active"] = current.get("active", True)
            f["price_num"] = current.get("price_num", 0)
            f["price_changed"] = current.get("price_changed", False)
    return jsonify({"favorites": favs, "count": len(favs)})

@app.route("/api/favorites/add", methods=["POST"])
def api_favorites_add():
    data = request.json or {}
    uid = str(data.get("user_id", "default"))
    listing = data.get("listing")
    if not listing or not listing.get("id"):
        return jsonify({"error": "Missing listing"}), 400
    favs = store.add_favorite(uid, listing)
    return jsonify({"success": True, "count": len(favs)})

@app.route("/api/favorites/remove", methods=["POST"])
def api_favorites_remove():
    data = request.json or {}
    uid = str(data.get("user_id", "default"))
    lid = data.get("listing_id")
    if not lid:
        return jsonify({"error": "Missing listing_id"}), 400
    favs = store.remove_favorite(uid, lid)
    return jsonify({"success": True, "count": len(favs)})


# ─── API: Map ───

@app.route("/api/map")
def api_map():
    uid = request.args.get("user_id", "default")
    mode = request.args.get("mode", "all")

    if mode == "favorites":
        listings = store.get_favorites(uid)
    else:
        settings = store.get_user(uid)
        all_listings = store.get_all_listings()
        listings = [l for l in all_listings.values()
                    if l.get("active", True) and matches_user_filters(l, settings)]

    with_coords = [l for l in listings if l.get("lat") and l.get("lon")]
    return jsonify({"listings": with_coords, "count": len(with_coords), "total": len(listings)})


# ─── API: Listing detail ───

@app.route("/api/listing/<listing_id>")
def api_listing_detail(listing_id):
    uid = request.args.get("user_id", "default")
    listing = store.get_listing(listing_id)
    if not listing:
        return jsonify({"error": "Not found"}), 404
    listing["is_favorite"] = store.is_favorite(uid, listing_id)
    return jsonify(listing)


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM BOT
# ═══════════════════════════════════════════════════════════════

def handle_update(update):
    if "message" in update:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        user_id = str(msg.get("from", {}).get("id", chat_id))
        text = msg.get("text", "")

        # Link chat_id to user
        store.set_chat_id(user_id, chat_id)

        if text == "/start":
            webapp_url = f"{BOT_URL}/miniapp" if BOT_URL else ""
            rows = []
            if webapp_url:
                rows.append([{"text": "📱 Открыть приложение", "web_app": {"url": webapp_url}}])
            rows.append([
                {"text": "🔍 Искать", "callback_data": "search_now"},
                {"text": "📋 Фильтры", "callback_data": "show_filters"},
            ])
            rows.append([
                {"text": "▶️ Мониторинг", "callback_data": "start_monitor"},
                {"text": "⏸ Стоп", "callback_data": "stop_monitor"},
            ])

            tg("sendMessage", {
                "chat_id": chat_id,
                "text": "🏠 <b>CIAN Monitor</b>\n\n"
                        "Мониторинг объявлений о продаже квартир.\n\n"
                        "📱 Откройте приложение для настройки\n"
                        "🔍 Или используйте кнопки ниже",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": rows},
            })

        elif text == "/search":
            do_user_search(chat_id, user_id)

        elif text == "/filters":
            show_user_filters(chat_id, user_id)

        elif text.startswith("/fav"):
            show_user_favorites(chat_id, user_id)

    elif "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        data = cb.get("data", "")
        chat_id = str(cb["message"]["chat"]["id"])
        user_id = str(cb.get("from", {}).get("id", chat_id))

        store.set_chat_id(user_id, chat_id)

        if data == "search_now":
            do_user_search(chat_id, user_id)
        elif data == "show_filters":
            show_user_filters(chat_id, user_id)
        elif data == "start_monitor":
            settings = store.get_user(user_id)
            settings["monitoring_active"] = True
            store.save_user(user_id, settings)
            tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ Мониторинг запущен!"})
            tg("sendMessage", {"chat_id": chat_id, "text": "▶️ <b>Мониторинг запущен!</b>\nБуду присылать новые объявления каждые 2 часа.", "parse_mode": "HTML"})
        elif data == "stop_monitor":
            settings = store.get_user(user_id)
            settings["monitoring_active"] = False
            store.save_user(user_id, settings)
            tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "⏸ Мониторинг остановлен"})
            tg("sendMessage", {"chat_id": chat_id, "text": "⏸ Мониторинг остановлен."})
        elif data.startswith("fav_"):
            lid = data[4:]
            handle_fav_toggle(chat_id, user_id, lid, cb_id)
        else:
            tg("answerCallbackQuery", {"callback_query_id": cb_id})


def do_user_search(chat_id, user_id):
    settings = store.get_user(user_id)
    tg("sendMessage", {"chat_id": chat_id, "text": "🔍 Ищу объявления..."})

    offers_raw = fetch_from_cian(settings, max_pages=3)
    seen = store.get_seen(user_id)
    new_count = 0

    for offer in offers_raw:
        parsed = parse_offer(offer)
        if not parsed:
            continue
        store.upsert_listing(parsed)
        if parsed["id"] in seen:
            continue
        store.add_seen(user_id, parsed["id"])
        parsed["is_favorite"] = store.is_favorite(user_id, parsed["id"])
        send_listing_notification(chat_id, parsed, "new")
        new_count += 1
        time.sleep(0.5)

    if new_count == 0:
        tg("sendMessage", {"chat_id": chat_id, "text": "😔 Новых объявлений не найдено.\nПопробуйте изменить фильтры."})
    else:
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ Найдено {new_count} новых объявлений!"})


def show_user_filters(chat_id, user_id):
    s = store.get_user(user_id)
    rooms_str = ", ".join(str(r) for r in s.get("rooms", []))
    price_str = f"{s.get('price_min',0):,}–{s.get('price_max',0):,} ₽".replace(",", " ")
    status = "▶️ Включён" if s.get("monitoring_active") else "⏸ Выключен"
    text = (
        f"📋 <b>Текущие фильтры</b>\n\n"
        f"📍 Район: {s.get('district', '—')}\n"
        f"🏠 Комнаты: {rooms_str}\n"
        f"💰 Бюджет: {price_str}\n"
        f"📐 Площадь: от {s.get('area_min', 0)} м²\n"
        f"🌐 Источник: CIAN\n"
        f"🔄 Мониторинг: {status}"
    )
    webapp_url = f"{BOT_URL}/miniapp" if BOT_URL else ""
    rows = []
    if webapp_url:
        rows.append([{"text": "⚙️ Настроить в приложении", "web_app": {"url": webapp_url}}])
    rows.append([{"text": "🔍 Искать сейчас", "callback_data": "search_now"}])
    tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": rows}})


def show_user_favorites(chat_id, user_id):
    favs = store.get_favorites(user_id)
    if not favs:
        tg("sendMessage", {"chat_id": chat_id, "text": "⭐ Избранное пусто.\n\nДобавляйте квартиры кнопкой «В избранное»."})
        return
    tg("sendMessage", {"chat_id": chat_id, "text": f"⭐ В избранном: {len(favs)} квартир"})
    for fav in favs[:10]:
        send_listing_notification(chat_id, fav, "new")


def handle_fav_toggle(chat_id, user_id, listing_id, cb_id):
    if store.is_favorite(user_id, listing_id):
        store.remove_favorite(user_id, listing_id)
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "❌ Удалено из избранного"})
    else:
        listing = store.get_listing(listing_id)
        if listing:
            store.add_favorite(user_id, listing)
            tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "⭐ Добавлено в избранное!"})
        else:
            tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Не найдено"})


def start_bot_polling():
    log.info("🤖 Bot polling started")
    offset = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30}, timeout=35)
            data = r.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        handle_update(update)
                    except Exception as e:
                        log.error(f"Update error: {e}")
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Start bot polling
    if BOT_TOKEN:
        t1 = threading.Thread(target=start_bot_polling, daemon=True)
        t1.start()

    # Start monitoring
    if BOT_TOKEN and CF_WORKER:
        t2 = threading.Thread(target=monitor_loop, daemon=True)
        t2.start()

    port = int(os.environ.get("PORT", 5000))
    log.info(f"🚀 Server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
