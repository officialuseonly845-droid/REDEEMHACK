import os
import re
import json
import html
import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta
from collections import OrderedDict

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("gplay-bot")

# ---------------------------------------------------------------- config
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", ".").strip() or "."
PORT = int(os.environ.get("PORT", "10000"))

if not BOT_TOKEN or not OWNER_ID_RAW.lstrip("-").isdigit():
    raise SystemExit("BOT_TOKEN and numeric OWNER_ID environment variables are required.")
OWNER_ID = int(OWNER_ID_RAW)

os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- storage
_lock = threading.RLock()


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def fmt_time(ts) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %I:%M %p IST")


def new_group(title, ctype, admin=False) -> dict:
    return {
        "title": title, "type": ctype, "admin": bool(admin), "active": True,
        "targets": [], "codes": 0, "last_code": None, "last_time": None, "added": now_ts(),
    }


def _load() -> dict:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"groups": {}, "users": []}
    if not isinstance(d, dict):
        return {"groups": {}, "users": []}
    groups = d.get("groups", {})
    old_targets = d.pop("targets", [])  # migrate old global targets
    for gid, g in list(groups.items()):
        base = new_group(g.get("title", gid), g.get("type", "group"), g.get("admin", False))
        base.update(g)
        if "targets" not in g:
            base["targets"] = list(old_targets)
        groups[gid] = base
    users = [int(u) for u in d.get("users", []) if str(u).lstrip("-").isdigit()]
    return {"groups": groups, "users": users}


def _save() -> None:
    with _lock:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)


DATA = _load()
GROUPS: dict = DATA["groups"]
USERS: list = DATA["users"]            # extra users added with /add
AUTH_IDS: set = {OWNER_ID, *USERS}     # everyone allowed to use the bot


def refresh_auth() -> None:
    AUTH_IDS.clear()
    AUTH_IDS.add(OWNER_ID)
    AUTH_IDS.update(USERS)


def remember_group(chat_id: int, title: str, ctype: str, admin=None) -> None:
    key = str(chat_id)
    with _lock:
        g = GROUPS.get(key)
        if g is None:
            GROUPS[key] = new_group(title or key, ctype, bool(admin))
            _save()
            return
        changed = False
        if title and g["title"] != title:
            g["title"], changed = title, True
        if g["type"] != ctype:
            g["type"], changed = ctype, True
        if admin is not None and g["admin"] != bool(admin):
            g["admin"], changed = bool(admin), True
        if changed:
            _save()


def forget_group(chat_id: int) -> None:
    with _lock:
        if GROUPS.pop(str(chat_id), None) is not None:
            _save()


# ---------------------------------------------------------------- filters
class AuthFilter(filters.MessageFilter):
    """Passes only messages from the owner or users added with /add."""
    def filter(self, message):
        u = message.from_user
        return bool(u and u.id in AUTH_IDS)


class OwnerFilter(filters.MessageFilter):
    def filter(self, message):
        u = message.from_user
        return bool(u and u.id == OWNER_ID)


PRIVATE = filters.ChatType.PRIVATE
AUTH_DM = PRIVATE & AuthFilter()
OWNER_DM = PRIVATE & OwnerFilter()

# ---------------------------------------------------------------- targets (per group)
def parse_target(token: str):
    t = token.strip()
    if t.lstrip("-").isdigit():
        return {"username": None, "id": int(t)}
    t = t.lstrip("@").lower()
    if re.fullmatch(r"[a-z0-9_]{4,32}", t):
        return {"username": t, "id": None}
    return None


def label(t: dict) -> str:
    if t.get("username"):
        return f"@{t['username']}" + (f" (ID {t['id']})" if t.get("id") else "")
    return f"ID {t['id']}"


def same_target(a: dict, b: dict) -> bool:
    return bool((a.get("username") and a["username"] == b.get("username")) or
                (a.get("id") and a["id"] == b.get("id")))


def match_target(user, targets: list) -> bool:
    if user is None:
        return False
    uname = (user.username or "").lower()
    for t in targets:
        if t.get("id") and t["id"] == user.id:
            if uname and t.get("username") != uname:
                t["username"] = uname
                threading.Thread(target=_save, daemon=True).start()
            return True
        if t.get("username") and uname and t["username"] == uname:
            if not t.get("id"):
                t["id"] = user.id
                threading.Thread(target=_save, daemon=True).start()
            return True
    return False


def add_targets(gid: str, tokens: list):
    added, bad = [], []
    with _lock:
        tl = GROUPS[gid]["targets"]
        for tok in tokens:
            t = parse_target(tok)
            if not t:
                bad.append(tok)
                continue
            if not any(same_target(t, x) for x in tl):
                tl.append(t)
            added.append(label(t))
        _save()
    return added, bad


# ---------------------------------------------------------------- detection
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)

REDEEM_URL_RE = re.compile(r"play\.google\.com/redeem\?[^\s]*?code=([A-Z0-9\-]{12,30})", re.I)
ANY_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
DASHED_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4}(?:-[A-Z0-9]{4}){3,5})(?![A-Z0-9])")
PLAIN_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{16,24})(?![A-Z0-9])")
HAS_LETTER = re.compile(r"[A-Z]")
HAS_DIGIT = re.compile(r"\d")

GP_LENGTHS = {16, 20, 23, 24}
GP_HINTS = ("google play", "googleplay", "gplay", "g play", "play.google", "play store", "playstore")
OTHER_HINTS = ("swiggy", "phonepe", "phone pe", "paytm", "zomato", "amazon pay", "flipkart", "myntra")


def extract_google_play_codes(text: str) -> list:
    if not text or len(text) < 12:      # fast exit: too short to hold a code
        return []
    text = text.translate(ZERO_WIDTH)
    low = text.lower()
    if any(h in low for h in OTHER_HINTS) and not any(h in low for h in GP_HINTS):
        return []

    found = []

    def add(code: str):
        code = code.replace("-", "").upper()
        if len(code) in GP_LENGTHS and code not in found:
            found.append(code)

    for m in REDEEM_URL_RE.finditer(text):
        add(m.group(1))
    stripped = ANY_URL_RE.sub(" ", text).upper()
    for m in DASHED_RE.finditer(stripped):
        add(m.group(1))
    for m in PLAIN_RE.finditer(stripped):
        c = m.group(1)
        if HAS_LETTER.search(c) and HAS_DIGIT.search(c):
            add(c)
    return found


SEEN = OrderedDict()
SEEN_MAX = 3000


def is_new(code: str) -> bool:
    if code in SEEN:
        return False
    SEEN[code] = True
    while len(SEEN) > SEEN_MAX:
        SEEN.popitem(last=False)
    return True


# ---------------------------------------------------------------- UI helpers
def short(title: str, n: int = 40) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"


def is_member_status(m) -> bool:
    return m.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER) or (
        m.status == ChatMember.RESTRICTED and getattr(m, "is_member", True)
    )


def list_text(mode: str) -> str:
    if not GROUPS:
        return "No remembered groups yet.\nAdd the bot to a group, or use /g GROUP_ID."
    if mode == "g":
        return "📋 Your groups (⭐ admin, ⏸ paused).\nTap one to manage it:"
    return "🚪 Tap a group to leave it:"


def list_markup(mode: str):
    if not GROUPS:
        return None
    rows = []
    for gid, g in GROUPS.items():
        tag = ("⭐ " if g.get("admin") else "") + ("⏸ " if not g.get("active", True) else "")
        if mode == "g":
            rows.append([InlineKeyboardButton(f"👥 {tag}{short(g['title'])}", callback_data=f"g:{gid}")])
        else:
            rows.append([InlineKeyboardButton(f"🚪 {short(g['title'], 32)}", callback_data=f"ask:{gid}:r")])
    return InlineKeyboardMarkup(rows)


def panel_text(gid: str) -> str:
    g = GROUPS[gid]
    targets = ", ".join(label(t) for t in g["targets"]) or "Everyone (no target set)"
    last = f"{g['last_code']} · {fmt_time(g['last_time'])}" if g.get("last_code") else "none yet"
    return (
        f"👥 {g['title']}\n"
        f"🆔 {gid}\n"
        f"👤 Bot role: {'Admin ⭐' if g['admin'] else 'Member'}\n"
        f"📡 Monitoring: {'ON ✅' if g['active'] else 'PAUSED ⏸'}\n"
        f"🎯 Tracking: {targets}\n"
        f"📊 Codes found: {g['codes']}\n"
        f"🕒 Last code: {last}\n"
        f"📅 Added: {fmt_time(g['added'])}\n\n"
        f"You get a DM only when a Google Play code is found here."
    )


def panel_markup(gid: str) -> InlineKeyboardMarkup:
    g = GROUPS[gid]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Set Target", callback_data=f"tgt:{gid}"),
         InlineKeyboardButton("🗑 Clear Targets", callback_data=f"clr:{gid}")],
        [InlineKeyboardButton("⏸ Pause" if g["active"] else "▶ Resume", callback_data=f"tog:{gid}")],
        [InlineKeyboardButton("🚪 Leave Group", callback_data=f"ask:{gid}:g")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


START_BASE = (
    "🤖 Google Play Code Watcher\n\n"
    "I silently watch the groups I'm in. When a Google Play code appears, "
    "I DM you a REDEEM NOW button. I never post in groups.\n\n"
    "📌 Commands\n"
    "/start – this guide\n"
    "/groups – list groups; tap one to see its panel (targets, stats, pause, leave)\n"
    "/rgroup – list groups with quick Leave buttons\n"
    "/g GROUP_ID – register a group I'm already in (e.g. /g -828292991)\n"
    "/target @user – track only this user in the opened group\n"
    "/untarget @user – stop tracking that user in the opened group\n"
    "/targets – show targets of the opened group\n"
    "/target off – clear targets (track everyone) in the opened group\n"
)
START_OWNER = (
    "\n👑 Owner only\n"
    "/add USER_ID – give someone access to this bot\n"
    "/remove USER_ID – remove their access\n"
    "/users – list everyone with access\n"
)
START_FOOT = "\n🔁 Workflow: /groups → tap a group → use the buttons. Targets are saved per group."


# ---------------------------------------------------------------- monitoring
async def send_alert(bot, uid: int, text: str, markup):
    try:
        await bot.send_message(
            chat_id=uid, text=text, parse_mode=ParseMode.HTML,
            reply_markup=markup, disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Alert to %s failed: %s", uid, e)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return

    key = str(chat.id)
    g = GROUPS.get(key)
    if g is None:
        remember_group(chat.id, chat.title or key, chat.type)
        g = GROUPS[key]

    if not g["active"]:
        return
    if g["targets"] and not match_target(msg.from_user, g["targets"]):
        return

    text = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities
    if ents:
        extra = [e.url for e in ents if e.url]
        if extra:
            text = text + "\n" + "\n".join(extra)

    codes = extract_google_play_codes(text)
    if not codes:
        return

    for code in codes:
        if not is_new(code):
            continue
        url = f"https://play.google.com/redeem?code={code}"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 REDEEM NOW", url=url)]])
        body = (f"🚨 GOOGLE PLAY CODE FOUND\n🎟 <code>{code}</code>\n"
                f"📍 {html.escape(short(g['title'], 40))}")

        # 1) send to everyone at the same time
        await asyncio.gather(*(send_alert(context.bot, uid, body, markup) for uid in list(AUTH_IDS)))

        # 2) save stats after the alert is out, off the event loop
        g["codes"] += 1
        g["last_code"], g["last_time"] = code, now_ts()
        asyncio.get_running_loop().run_in_executor(None, _save)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ev = update.my_chat_member
    if not ev or ev.chat.type == ChatType.PRIVATE:
        return
    chat, st = ev.chat, ev.new_chat_member.status
    if st in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR):
        remember_group(chat.id, chat.title or str(chat.id), chat.type, admin=(st == ChatMember.ADMINISTRATOR))
    elif st in (ChatMember.LEFT, ChatMember.BANNED):
        forget_group(chat.id)


# ---------------------------------------------------------------- commands (owner + added users, DM only)
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_owner = update.effective_user.id == OWNER_ID
    text = START_BASE + (START_OWNER if is_owner else "") + START_FOOT
    await update.effective_message.reply_text(text)


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(list_text("g"), reply_markup=list_markup("g"))


async def cmd_rgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(list_text("r"), reply_markup=list_markup("r"))


async def cmd_g(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await msg.reply_text("Usage: /g GROUP_ID")
        return
    gid = int(context.args[0])
    try:
        chat = await context.bot.get_chat(gid)
        me = await context.bot.get_chat_member(gid, context.bot.id)
    except Exception as e:
        await msg.reply_text(f"❌ Can't access that chat: {e}")
        return
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("❌ That is not a group.")
        return
    if not is_member_status(me):
        await msg.reply_text("❌ The bot is not a member of that group.")
        return
    admin = me.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    remember_group(chat.id, chat.title or str(chat.id), chat.type, admin=admin)
    context.user_data["sel"] = str(chat.id)
    await msg.reply_text(panel_text(str(chat.id)), reply_markup=panel_markup(str(chat.id)))


def selected(context):
    sel = context.user_data.get("sel")
    return sel if sel in GROUPS else None


async def need_group(update, context):
    gid = selected(context)
    if not gid:
        await update.effective_message.reply_text("Open a group first: /groups → tap a group.")
    return gid


async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    gid = await need_group(update, context)
    if not gid:
        return
    if not context.args:
        await msg.reply_text("Usage: /target @username (or numeric ID)\n/target off → track everyone")
        return
    if context.args[0].lower() in ("off", "clear", "all"):
        with _lock:
            GROUPS[gid]["targets"].clear()
            _save()
    else:
        added, bad = add_targets(gid, context.args)
        if bad:
            await msg.reply_text("⚠️ Invalid: " + ", ".join(bad))
        if not added:
            return
    await msg.reply_text(panel_text(gid), reply_markup=panel_markup(gid))


async def cmd_untarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    gid = await need_group(update, context)
    if not gid:
        return
    t = parse_target(context.args[0]) if context.args else None
    if not t:
        await msg.reply_text("Usage: /untarget @username")
        return
    with _lock:
        GROUPS[gid]["targets"][:] = [x for x in GROUPS[gid]["targets"] if not same_target(t, x)]
        _save()
    await msg.reply_text(panel_text(gid), reply_markup=panel_markup(gid))


async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = await need_group(update, context)
    if not gid:
        return
    tl = GROUPS[gid]["targets"]
    body = "\n".join(label(t) for t in tl) if tl else "Everyone (no target set)"
    await update.effective_message.reply_text(f"🎯 {GROUPS[gid]['title']}\n{body}")


async def on_owner_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text: only used right after tapping 🎯 Set Target."""
    gid = context.user_data.pop("pending", None)
    if not gid or gid not in GROUPS:
        return
    added, bad = add_targets(gid, (update.effective_message.text or "").split())
    if bad:
        await update.effective_message.reply_text("⚠️ Invalid: " + ", ".join(bad))
    context.user_data["sel"] = gid
    await update.effective_message.reply_text(panel_text(gid), reply_markup=panel_markup(gid))


# ---------------------------------------------------------------- owner-only: user access
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("Usage: /add USER_ID\nExample: /add 123456789")
        return
    uid = int(context.args[0])
    if uid == OWNER_ID:
        await msg.reply_text("That's you, you already have full access.")
        return
    if uid in USERS:
        await msg.reply_text(f"ℹ️ {uid} already has access.")
        return
    with _lock:
        USERS.append(uid)
        refresh_auth()
        _save()
    note = f"✅ Access granted to {uid}."
    try:
        await context.bot.send_message(
            uid, "✅ You've been given access to this bot.\nSend /start to see the commands."
        )
    except Exception:
        note += "\n⚠️ I couldn't message them yet. Ask them to open this bot and press Start once, then they'll receive alerts."
    await msg.reply_text(note)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("Usage: /remove USER_ID")
        return
    uid = int(context.args[0])
    if uid not in USERS:
        await msg.reply_text("That ID isn't in the access list.")
        return
    with _lock:
        USERS.remove(uid)
        refresh_auth()
        _save()
    await msg.reply_text(f"🗑 Access removed for {uid}.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"👑 {OWNER_ID} (owner)"] + [f"👤 {u}" for u in USERS]
    await update.effective_message.reply_text("🔐 Users with access:\n" + "\n".join(lines))


# ---------------------------------------------------------------- buttons
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user or q.from_user.id not in AUTH_IDS:
        return
    if not q.message or q.message.chat.type != ChatType.PRIVATE:
        return

    await q.answer()
    data = q.data or ""
    parts = data.split(":")
    action = parts[0]

    if action == "back":
        context.user_data.pop("pending", None)
        await safe_edit(q, list_text("g"), list_markup("g"))
        return

    gid = parts[1] if len(parts) > 1 else ""
    if gid not in GROUPS:
        await safe_edit(q, "Group not found. Use /groups.")
        return

    if action == "g":
        context.user_data.pop("pending", None)
        context.user_data["sel"] = gid
        await safe_edit(q, panel_text(gid), panel_markup(gid))

    elif action == "tgt":
        context.user_data["pending"] = gid
        context.user_data["sel"] = gid
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"g:{gid}")]])
        await safe_edit(
            q,
            f"🎯 Set target for:\n{GROUPS[gid]['title']}\n\n"
            "Send the @username or numeric ID now.\nSeparate several with spaces.",
            kb,
        )

    elif action == "clr":
        with _lock:
            GROUPS[gid]["targets"].clear()
            _save()
        await safe_edit(q, panel_text(gid), panel_markup(gid))

    elif action == "tog":
        with _lock:
            GROUPS[gid]["active"] = not GROUPS[gid]["active"]
            _save()
        await safe_edit(q, panel_text(gid), panel_markup(gid))

    elif action == "ask":
        mode = parts[2] if len(parts) > 2 else "g"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, leave", callback_data=f"leave:{gid}:{mode}"),
            InlineKeyboardButton("❌ No", callback_data=f"g:{gid}"),
        ]])
        await safe_edit(q, f"Leave this group?\n\n{GROUPS[gid]['title']}\n🆔 {gid}", kb)

    elif action == "leave":
        mode = parts[2] if len(parts) > 2 else "g"
        title = GROUPS[gid]["title"]
        try:
            await context.bot.leave_chat(int(gid))
            forget_group(int(gid))
            context.user_data.pop("sel", None)
            note = f"✅ Left: {title}"
        except Exception as e:
            note = f"❌ Could not leave {title}: {e}"
        await safe_edit(q, f"{note}\n\n{list_text(mode)}", list_markup(mode))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Handler error: %s", context.error, exc_info=context.error)


# ---------------------------------------------------------------- startup refresh
async def post_init(app: Application):
    """After every restart: re-verify remembered groups and refresh admin status."""
    async def verify(gid: str):
        try:
            me = await app.bot.get_chat_member(int(gid), app.bot.id)
            if not is_member_status(me):
                forget_group(int(gid))
                return
            chat = await app.bot.get_chat(int(gid))
            remember_group(
                chat.id, chat.title or gid, chat.type,
                admin=me.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER),
            )
        except (Forbidden, BadRequest):
            forget_group(int(gid))
        except Exception as e:
            log.warning("Could not verify group %s: %s", gid, e)

    await asyncio.gather(*(verify(gid) for gid in list(GROUPS)))   # all groups checked in parallel
    log.info("Groups loaded: %d | Users with access: %d", len(GROUPS), len(AUTH_IDS))
    try:
        await app.bot.send_message(
            OWNER_ID, f"✅ Bot online. Remembered groups: {len(GROUPS)}\nSend /start for the guide."
        )
    except Exception as e:
        log.warning("Could not DM owner on startup (press Start on the bot first): %s", e)


# ---------------------------------------------------------------- flask health
web = Flask(__name__)


@web.route("/")
def health():
    return "OK", 200


def run_web():
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


# ---------------------------------------------------------------- main
def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .connection_pool_size(32)
        .pool_timeout(5.0)
        .connect_timeout(5.0)
        .read_timeout(10.0)
        .write_timeout(10.0)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start, filters=AUTH_DM))
    app.add_handler(CommandHandler("groups", cmd_groups, filters=AUTH_DM))
    app.add_handler(CommandHandler("rgroup", cmd_rgroup, filters=AUTH_DM))
    app.add_handler(CommandHandler("g", cmd_g, filters=AUTH_DM))
    app.add_handler(CommandHandler("target", cmd_target, filters=AUTH_DM))
    app.add_handler(CommandHandler("untarget", cmd_untarget, filters=AUTH_DM))
    app.add_handler(CommandHandler("targets", cmd_targets, filters=AUTH_DM))

    app.add_handler(CommandHandler("add", cmd_add, filters=OWNER_DM))
    app.add_handler(CommandHandler("remove", cmd_remove, filters=OWNER_DM))
    app.add_handler(CommandHandler("users", cmd_users, filters=OWNER_DM))

    app.add_handler(MessageHandler(AUTH_DM & filters.TEXT & ~filters.COMMAND, on_owner_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION), on_group_message)
    )
    app.add_error_handler(on_error)

    # Python 3.14 fix: polling needs an event loop in the main thread
    asyncio.set_event_loop(asyncio.new_event_loop())

    log.info("Bot started.")
    app.run_polling(
        allowed_updates=["message", "my_chat_member", "callback_query"],
        drop_pending_updates=True,
        poll_interval=0.0,
        timeout=30,
    )


if __name__ == "__main__":
    main()
