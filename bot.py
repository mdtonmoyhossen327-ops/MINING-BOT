# -*- coding: utf-8 -*-
"""
Mining Mini-App Bot (Telegram WebApp + Bot)
============================================
AUTO mining bot (tap-to-earn NOT). User starts mining and tokens accrue
over time. Free plan gives a set number of tokens per day.

IMPORTANT (Bangla warning):
- BOT_TOKEN / ADMIN_ID kokhono ei file-e likhben NA.
  Shudhu Render env var-e diben. Token leak hole bot churi hoye jabe!
- Eta points-only bot: kono real crypto / real taka / payment / withdraw NAI.

Run:  python bot.py   (Flask web server + Telegram polling, one process)
Env:  BOT_TOKEN, ADMIN_ID, WEBAPP_URL (optional), PORT (Render sets it)
"""

import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
from urllib.parse import parse_qsl

from flask import Flask, jsonify, request, send_from_directory
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = os.environ.get("ADMIN_ID", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
PORT = int(os.environ.get("PORT", "8080"))

DEFAULT_SETTINGS = {
    "token_name": "MINE",
    "token_symbol": "MINE",
    "free_daily": 10.0,        # free plan: tokens per day
    "base_hashrate": 1.0,      # TH/s
    "force_channel": "",       # e.g. "TonmoyDeveloperBD24" (without @). Empty = off.
}

MINERS = [
    {"id": "pickaxe", "name": "Rusty Pickaxe", "bonus": 0.5, "price": 50},
    {"id": "drill", "name": "Drill Bot", "bonus": 2.0, "price": 200},
    {"id": "rig", "name": "Lunar Rig", "bonus": 10.0, "price": 1000},
]

TASKS = [
    {"id": "join_channel", "name": "Join our channel", "reward": 5},
    {"id": "daily_checkin", "name": "Daily check-in", "reward": 2},
    {"id": "share_bot", "name": "Share the bot", "reward": 3},
]

DAY = 86400
LEVEL_STEP = 50.0  # tokens of total earned per level (1..100)

_lock = threading.Lock()
_store = {"settings": dict(DEFAULT_SETTINGS), "users": {}}


# ---------------------------------------------------------------- storage
def load_store():
    global _store
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _store["settings"] = {**DEFAULT_SETTINGS, **data.get("settings", {})}
            _store["users"] = data.get("users", {})
        except Exception:
            pass


def save_store():
    with _lock:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)


def get_settings():
    return _store["settings"]


def get_user(uid, first_name="", username=""):
    uid = str(uid)
    users = _store["users"]
    u = users.get(uid)
    if u is None:
        u = {
            "first_name": first_name,
            "username": username,
            "balance": 0.0,
            "total_earned": 0.0,
            "mining_on": False,
            "mining_since": 0,
            "miners": {},          # miner_id -> count
            "tasks_done": [],      # one-time task ids
            "last_checkin": 0,
        }
        users[uid] = u
        save_store()
    else:
        if first_name and u.get("first_name") != first_name:
            u["first_name"] = first_name
        if username and u.get("username") != username:
            u["username"] = username
    return u


def user_hashrate(u):
    s = get_settings()
    bonus = sum(
        next((m["bonus"] for m in MINERS if m["id"] == mid), 0.0) * n
        for mid, n in u.get("miners", {}).items()
    )
    return round(s["base_hashrate"] + bonus, 2)


def rate_per_sec(u):
    s = get_settings()
    base = s["base_hashrate"] or 1.0
    return (s["free_daily"] / DAY) * (user_hashrate(u) / base)


def pending_amount(u):
    if not u.get("mining_on") or not u.get("mining_since"):
        return 0.0
    elapsed = min(time.time() - u["mining_since"], DAY)
    return round(rate_per_sec(u) * max(elapsed, 0), 4)


def user_level(u):
    return min(100, 1 + int(u.get("total_earned", 0.0) // LEVEL_STEP))


# ---------------------------------------------------------------- initData
def verify_init_data(init_data, bot_token):
    """Verify Telegram WebApp initData. Returns user dict or None."""
    if not bot_token or not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data))
    except Exception:
        return None
    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


def _clear_proxy_env():
    """PTB/httpx mis-parses bracketed IPv6 entries in NO_PROXY on some hosts.
    The bot talks to Telegram directly, so drop proxy vars before building it."""
    for k in ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        os.environ.pop(k, None)


# ---------------------------------------------------------------- force join
_api_bot = None


def _get_api_bot():
    global _api_bot
    if _api_bot is None and BOT_TOKEN:
        from telegram import Bot

        _clear_proxy_env()
        _api_bot = Bot(BOT_TOKEN)
    return _api_bot


def is_channel_member(uid):
    """True if user joined the force-join channel (or none is set)."""
    channel = get_settings().get("force_channel", "").strip().lstrip("@")
    if not channel:
        return True
    bot = _get_api_bot()
    if bot is None:
        return True
    try:
        member = asyncio.run(bot.get_chat_member("@" + channel, int(uid)))
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


# ---------------------------------------------------------------- Flask app
app = Flask(__name__)


def _api_user():
    data = request.get_json(silent=True) or {}
    user = verify_init_data(data.get("initData", ""), BOT_TOKEN)
    if not user or not user.get("id"):
        return None, jsonify({"ok": False, "error": "invalid initData"}), 401
    u = get_user(user["id"], user.get("first_name", ""), user.get("username", ""))
    return (user, u), None, None


def public_state(u, uid):
    s = get_settings()
    pending = pending_amount(u)
    elapsed = 0
    if u.get("mining_on") and u.get("mining_since"):
        elapsed = min(time.time() - u["mining_since"], DAY)
    lvl = user_level(u)
    return {
        "ok": True,
        "token_name": s["token_name"],
        "token_symbol": s["token_symbol"],
        "free_daily": s["free_daily"],
        "balance": round(u["balance"], 4),
        "pending": pending,
        "hashrate": user_hashrate(u),
        "level": lvl,
        "level_progress": round((u["total_earned"] % LEVEL_STEP) / LEVEL_STEP, 3),
        "total_earned": round(u["total_earned"], 4),
        "mining_on": bool(u.get("mining_on")),
        "timer_left": int(DAY - elapsed) if u.get("mining_on") else DAY,
        "timer_progress": round(elapsed / DAY, 3),
        "joined": is_channel_member(uid),
        "force_channel": s.get("force_channel", "").lstrip("@"),
        "miners_owned": u.get("miners", {}),
    }


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return "ok", 200


@app.route("/api/state", methods=["POST"])
def api_state():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    return jsonify(public_state(u, user["id"]))


@app.route("/api/start_mining", methods=["POST"])
def api_start_mining():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    if not is_channel_member(user["id"]):
        return jsonify({"ok": False, "error": "join channel first"}), 403
    if not u.get("mining_on"):
        u["mining_on"] = True
        u["mining_since"] = time.time()
        save_store()
    return jsonify(public_state(u, user["id"]))


@app.route("/api/collect", methods=["POST"])
def api_collect():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    amount = pending_amount(u)
    if amount > 0:
        u["balance"] = round(u["balance"] + amount, 4)
        u["total_earned"] = round(u["total_earned"] + amount, 4)
    u["mining_since"] = time.time() if u.get("mining_on") else 0
    save_store()
    out = public_state(u, user["id"])
    out["collected"] = amount
    return jsonify(out)


@app.route("/api/miners", methods=["POST"])
def api_miners():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    return jsonify(
        {
            "ok": True,
            "token_symbol": get_settings()["token_symbol"],
            "balance": round(u["balance"], 4),
            "miners": MINERS,
            "owned": u.get("miners", {}),
        }
    )


@app.route("/api/buy_miner", methods=["POST"])
def api_buy_miner():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    data = request.get_json(silent=True) or {}
    miner = next((m for m in MINERS if m["id"] == data.get("miner_id")), None)
    if not miner:
        return jsonify({"ok": False, "error": "unknown miner"}), 400
    if u["balance"] < miner["price"]:
        return jsonify({"ok": False, "error": "not enough balance"}), 400
    u["balance"] = round(u["balance"] - miner["price"], 4)
    owned = u.setdefault("miners", {})
    owned[miner["id"]] = owned.get(miner["id"], 0) + 1
    save_store()
    return jsonify(public_state(u, user["id"]))


@app.route("/api/tasks", methods=["POST"])
def api_tasks():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    now = time.time()
    out = []
    for t in TASKS:
        if t["id"] == "join_channel":
            done = is_channel_member(user["id"]) and ("join_channel" in u.get("tasks_done", []))
            # joining alone marks it claimable; reward once
            available = is_channel_member(user["id"]) and ("join_channel" not in u.get("tasks_done", []))
            out.append({**t, "done": done, "available": available})
        elif t["id"] == "daily_checkin":
            available = (now - u.get("last_checkin", 0)) >= DAY
            out.append({**t, "done": not available, "available": available})
        else:
            done = t["id"] in u.get("tasks_done", [])
            out.append({**t, "done": done, "available": not done})
    return jsonify({"ok": True, "tasks": out, "balance": round(u["balance"], 4)})


@app.route("/api/claim_task", methods=["POST"])
def api_claim_task():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    data = request.get_json(silent=True) or {}
    task = next((t for t in TASKS if t["id"] == data.get("task_id")), None)
    if not task:
        return jsonify({"ok": False, "error": "unknown task"}), 400
    now = time.time()
    if task["id"] == "join_channel":
        if not is_channel_member(user["id"]):
            return jsonify({"ok": False, "error": "join channel first"}), 403
        if "join_channel" in u.get("tasks_done", []):
            return jsonify({"ok": False, "error": "already claimed"}), 400
        u.setdefault("tasks_done", []).append("join_channel")
    elif task["id"] == "daily_checkin":
        if (now - u.get("last_checkin", 0)) < DAY:
            return jsonify({"ok": False, "error": "come back tomorrow"}), 400
        u["last_checkin"] = now
    else:
        if task["id"] in u.get("tasks_done", []):
            return jsonify({"ok": False, "error": "already claimed"}), 400
        u.setdefault("tasks_done", []).append(task["id"])
    u["balance"] = round(u["balance"] + task["reward"], 4)
    u["total_earned"] = round(u["total_earned"] + task["reward"], 4)
    save_store()
    return jsonify({"ok": True, "reward": task["reward"], "balance": u["balance"]})


@app.route("/api/profile", methods=["POST"])
def api_profile():
    res, err, code = _api_user()
    if err:
        return err, code
    user, u = res
    return jsonify(
        {
            "ok": True,
            "name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "user_id": user["id"],
            "balance": round(u["balance"], 4),
            "total_earned": round(u["total_earned"], 4),
            "hashrate": user_hashrate(u),
            "level": user_level(u),
            "token_symbol": get_settings()["token_symbol"],
            "mining_on": bool(u.get("mining_on")),
        }
    )


@app.route("/")
def index():
    return send_from_directory(WEBAPP_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(WEBAPP_DIR, path)


# ---------------------------------------------------------------- Telegram bot
PENDING_ADMIN = {}  # admin chat id -> field name waiting for a value


def is_admin(uid):
    return bool(ADMIN_ID) and str(uid) == str(ADMIN_ID)


def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Stats", callback_data="admin:stats")],
            [InlineKeyboardButton("🪙 Token name", callback_data="admin:set:token_name"),
             InlineKeyboardButton("🔣 Symbol", callback_data="admin:set:token_symbol")],
            [InlineKeyboardButton("📅 Daily tokens (free)", callback_data="admin:set:free_daily"),
             InlineKeyboardButton("⚡ Base hashrate", callback_data="admin:set:base_hashrate")],
            [InlineKeyboardButton("📣 Force-join channel", callback_data="admin:set:force_channel")],
        ]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    get_user(u.id, u.first_name or "", u.username or "")
    s = get_settings()
    text = (
        f"⛏️ Welcome to {s['token_name']} Miner!\n\n"
        "Auto mining: press Start Mining and leave it — "
        f"you earn up to {s['free_daily']} {s['token_symbol']} per day on the free plan.\n"
        "Open the Mini App below to mine, collect and upgrade."
    )
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⛏️ Open Miner", web_app=WebAppInfo(url=WEBAPP_URL))]]
        )
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(
            text + "\n\n(Admin: set WEBAPP_URL env var to enable the Mini App button.)"
        )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Shudhu admin-er jonno.")
        return
    await update.message.reply_text("🛠️ Admin Panel", reply_markup=admin_keyboard())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Shudhu admin-er jonno.")
        return
    parts = query.data.split(":")
    if len(parts) == 2 and parts[1] == "stats":
        s = get_settings()
        users = _store["users"]
        total_mined = round(sum(u.get("total_earned", 0.0) for u in users.values()), 2)
        text = (
            "📊 Stats\n\n"
            f"👥 Total users: {len(users)}\n"
            f"⛏️ Total mined: {total_mined} {s['token_symbol']}\n"
            f"🪙 Token: {s['token_name']} ({s['token_symbol']})\n"
            f"📅 Free daily: {s['free_daily']}\n"
            f"⚡ Base hashrate: {s['base_hashrate']} TH/s\n"
            f"📣 Force channel: @{s['force_channel'] or '(off)'}"
        )
        await query.edit_message_text(text, reply_markup=admin_keyboard())
        return
    if len(parts) == 3 and parts[1] == "set":
        field = parts[2]
        PENDING_ADMIN[query.from_user.id] = field
        labels = {
            "token_name": "Notun token name likhun (jemon: MINE)",
            "token_symbol": "Notun token symbol likhun (jemon: MINE)",
            "free_daily": "Free plan-e protidin koto token dibe? (number likhun, jemon: 10)",
            "base_hashrate": "Base hashrate koto TH/s? (number likhun, jemon: 1)",
            "force_channel": "Force-join channel username likhun (jemon: TonmoyDeveloperBD24). Bondho korte 'off' likhun.",
        }
        await query.edit_message_text("✏️ " + labels.get(field, "Notun value likhun:"))
        return


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in PENDING_ADMIN or not is_admin(uid):
        return
    field = PENDING_ADMIN.pop(uid)
    value = (update.message.text or "").strip()
    s = get_settings()
    try:
        if field == "token_name":
            if not (1 <= len(value) <= 20):
                raise ValueError("name 1-20 okkhor hote hobe")
            s["token_name"] = value
        elif field == "token_symbol":
            if not (1 <= len(value) <= 10):
                raise ValueError("symbol 1-10 okkhor hote hobe")
            s["token_symbol"] = value.upper()
        elif field == "free_daily":
            v = float(value)
            if v <= 0:
                raise ValueError("0-er beshi hote hobe")
            s["free_daily"] = v
        elif field == "base_hashrate":
            v = float(value)
            if v <= 0:
                raise ValueError("0-er beshi hote hobe")
            s["base_hashrate"] = v
        elif field == "force_channel":
            if value.lower() == "off":
                s["force_channel"] = ""
            else:
                s["force_channel"] = value.lstrip("@").strip()
        else:
            return
    except ValueError as e:
        await update.message.reply_text(f"❌ Vul value: {e}")
        return
    save_store()
    await update.message.reply_text(
        f"✅ Saved: {field} = {s[field]}", reply_markup=admin_keyboard()
    )


def build_application():
    _clear_proxy_env()
    app_tg = Application.builder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CommandHandler("admin", cmd_admin))
    app_tg.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))
    return app_tg


async def _run_bot(app_tg):
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling()
    while True:
        await asyncio.sleep(3600)


def start_bot_thread():
    if not BOT_TOKEN:
        print("[warn] BOT_TOKEN not set - Telegram bot disabled, web only.")
        return
    app_tg = build_application()
    t = threading.Thread(target=lambda: asyncio.run(_run_bot(app_tg)), daemon=True)
    t.start()
    print("[info] Telegram bot polling started.")


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    load_store()
    start_bot_thread()
    print(f"[info] Web server on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
