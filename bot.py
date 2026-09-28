import os
import re
import io
import math
import time
import random
import shutil
import asyncio
import logging
import functools
import threading

import httpx
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    MenuButtonDefault,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))

# ---------------------------------------------------------------
# Cartesia AI (ভয়েস API) সেটিংস
# Render Environment এ CARTESIA_API_KEY দিন (sk_car_...)
# ---------------------------------------------------------------
CARTESIA_KEY = os.environ.get("CARTESIA_API_KEY", "")
CARTESIA_URL = "https://api.cartesia.ai"
CARTESIA_VERSION = os.environ.get("CARTESIA_VERSION", "2026-08-14")
TTS_MODEL = os.environ.get("TTS_MODEL", "sonic-3.5")
CLONE_LANG = os.environ.get("CLONE_LANG", "en")   # ক্লোন ভয়েসের ভাষা

CLONE_MIN_SEC = 30      # ক্লোনের জন্য সর্বনিম্ন ভয়েস দৈর্ঘ্য (সেকেন্ড)
CLONE_MAX_SEC = 120     # সর্বোচ্চ দৈর্ঘ্য (সেকেন্ড)
MAX_TEXT = 2000         # একবারে সর্বোচ্চ কত অক্ষর থেকে ভয়েস বানানো যাবে
PER_PAGE = 10           # ভয়েস লিস্টে প্রতি পেজে কয়টা ভয়েস
DEFAULT_PER_GENDER = 50  # ডিফল্ট ভয়েস: মেয়ে 50 + ছেলে 50
CLONE_EXTS = {"flac", "mp3", "mpeg", "mpga", "oga", "ogg", "wav", "webm"}
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
FFMPEG = shutil.which("ffmpeg")  # থাকলে OGG/Opus এ কনভার্ট করে, না থাকলে MP3

# ---------------------------------------------------------------
# Firebase (Firestore) - ডাটা স্থায়ীভাবে সেভ থাকবে
# Render এ Secret File হিসেবে firebase.json রাখুন (পাথ: /etc/secrets/firebase.json)
# চাইলে Environment এ FIREBASE_KEY_PATH দিয়ে অন্য পাথ দিতে পারেন।
# ---------------------------------------------------------------
KEY_PATH = os.environ.get("FIREBASE_KEY_PATH", "/etc/secrets/firebase.json")
if not os.path.exists(KEY_PATH) and os.path.exists("firebase.json"):
    KEY_PATH = "firebase.json"  # লোকাল টেস্টের জন্য
cred = credentials.Certificate(KEY_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()
USERS = "users"
USER_VOICES = "user_voices"   # প্রতি ইউজারের সেভ করা ভয়েস লিস্ট (চিরস্থায়ী)


async def run(fn, *args):
    """Firestore sync কল বটকে আটকে না রেখে আলাদা থ্রেডে চালায়"""
    return await asyncio.to_thread(fn, *args)


# ---------------------------------------------------------------
# ভিউ (মেনু পেজ) - {"n": নাম, ...}
#   main(p) | voice | allv | vlist(g, p) | create(p) | prompt
# ---------------------------------------------------------------
def V(n, **kw):
    d = {"n": n}
    d.update(kw)
    return d


MAIN1 = V("main", p=1)


def _get_state(uid: int):
    snap = db.collection(USERS).document(str(uid)).get()
    view, msg, keep = MAIN1, None, []
    if snap.exists:
        d = snap.to_dict() or {}
        msg = d.get("last_msg_id")
        view = d.get("view")
        if not isinstance(view, dict) or "n" not in view:
            pg = d.get("page")  # আগের ভার্সনের ডাটা
            view = V("voice") if pg == "voice" else V("main", p=pg if pg in (1, 2) else 1)
        keep = [int(x) for x in (d.get("keep_ids") or [])]
    return msg, view, keep


def _save_user(user, msg_id, view, keep, is_start: bool):
    ref = db.collection(USERS).document(str(user.id))
    data = {
        "name": user.full_name,
        "username": user.username,
        "last_seen": firestore.SERVER_TIMESTAMP,
        "last_msg_id": msg_id,
        "view": view,
        "keep_ids": keep,
        "actions": firestore.Increment(1),
    }
    if is_start and not ref.get().exists:
        data["joined_at"] = firestore.SERVER_TIMESTAMP
    ref.set(data, merge=True)


def _get_voices(uid: int):
    snap = db.collection(USER_VOICES).document(str(uid)).get()
    if snap.exists:
        return list((snap.to_dict() or {}).get("list") or [])
    return []


def _save_voices(uid: int, lst):
    db.collection(USER_VOICES).document(str(uid)).set({"list": lst})


def _find_uid(username: str):
    for name in (username, username.lower()):
        docs = db.collection(USERS).where("username", "==", name).limit(1).stream()
        for d in docs:
            return int(d.id)
    return None


# ---------------------------------------------------------------
# বাটনের রঙ (টেলিগ্রাম শুধু 3টা রঙ সাপোর্ট করে)
#   "primary" = নীল | "success" = সবুজ | "danger" = লাল
# ---------------------------------------------------------------
DEFAULT_STYLE = "primary"
NAV_STYLE = "primary"
COLORS = {
    # "refer": "success",
    # "profile": "danger",
}

BUTTONS = {
    "smm": "📈 SMM Service",
    "voice": "🎙 Voice Generate",
    "tgbot": "🤖 TG Bot Py",
    "linkprot": "🔗 Link Protect",
    "vassist": "🗣 Voice Assistant",
    "reply": "💬 Smart Reply",
    "janai": "🧠 My Jan AI",
    "poll": "📊 Poll Maker",
    "guard": "🛡 Message Guard",
    "userinfo": "👤 User Info",
    "fftour": "🏆 FF Tournament File",
    "fftopup": "💎 FF Topup File",
    "refer": "🎁 Refer & Earn",
    "profile": "🙋 My Profile",
}
LABEL_TO_KEY = {label: key for key, label in BUTTONS.items()}

PAGES = {
    1: ["smm", "voice", "tgbot", "vassist", "reply", "refer"],
    2: ["linkprot", "janai", "poll", "guard", "userinfo", "fftour", "fftopup", "profile"],
}
TOTAL_PAGES = len(PAGES)

NEXT = "পরের পেজ ➡️"
PREV = "⬅️ আগের পেজ"

# Voice Generate সাব-মেনুর বাটন
BTN_ALL = "🎭 All Voices"
BTN_CREATE = "✨ Create Voice"
BTN_CLONE = "🧬 Clone Voice"
BTN_ADDID = "🆔 Add by Voice ID"
BTN_HOME = "🏠 Home"
BTN_BACK = "⬅️ Back"
BTN_FEMALE = "👩 Female Voices"
BTN_MALE = "👨 Male Voices"
BTN_RANDOM = "🎲 Select Random Voice"
BTN_VPREV = "⬅️ Prev"
BTN_VNEXT = "Next ➡️"

# ভয়েসের নিচের ইনলাইন বাটন
SEND_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("👥 Send Group/Channel", callback_data="send_group")],
        [InlineKeyboardButton("👤 Send User", callback_data="send_user")],
    ]
)


def B(text, style=DEFAULT_STYLE):
    return KeyboardButton(text, style=style)


def kb(rows) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,   # বাটন চাপলে মেনু বন্ধ হবে না
        is_persistent=False,       # শুধু ⊞ আইকনে খোলা/বন্ধ হবে
        input_field_placeholder="মেনু থেকে বেছে নিন 👇",
    )


def pair(btns):
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def main_keyboard(page: int) -> ReplyKeyboardMarkup:
    keys = PAGES[page]
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                B(BUTTONS[k], COLORS.get(k, DEFAULT_STYLE))
                for k in keys[i:i + 2]
            ]
        )
    nav_label = NEXT if page < TOTAL_PAGES else PREV
    rows.append([B(nav_label, NAV_STYLE)])
    return kb(rows)


PAGE_NAMES = {1: "প্রথম পেজ", 2: "দ্বিতীয় পেজ"}


def menu_text(page: int) -> str:
    return f"{PAGE_NAMES[page]}\nআপনার পছন্দের সার্ভিসটি বেছে নিন"


def uniq_label(prefix: str, name: str, used) -> str:
    base = f"{prefix} {name}".strip()[:30]
    label, i = base, 2
    while label in used:
        label = f"{base} {i}"
        i += 1
    return label


# ---------------------------------------------------------------
# Cartesia API
# ---------------------------------------------------------------
HTTP = None  # httpx.AsyncClient (post_init এ তৈরি হয়)
_dv = {"lock": None, "ts": 0.0, "f": [], "m": []}  # ডিফল্ট ভয়েস ক্যাশ


def _auth():
    if not CARTESIA_KEY:
        raise RuntimeError("CARTESIA_API_KEY সেট করা নেই")
    return {"Authorization": f"Bearer {CARTESIA_KEY}", "Cartesia-Version": CARTESIA_VERSION}


def detect_lang(text: str) -> str:
    """সঠিক উচ্চারণের জন্য লেখার ভাষা ঠিক করে (বাংলা/হিন্দি/আরবি/ইংরেজি)"""
    bn = hi = ar = total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        total += 1
        o = ord(ch)
        if 0x0980 <= o <= 0x09FF:
            bn += 1
        elif 0x0900 <= o <= 0x097F:
            hi += 1
        elif 0x0600 <= o <= 0x06FF:
            ar += 1
    if total:
        best = max((bn, "bn"), (hi, "hi"), (ar, "ar"))
        if best[0] / total >= 0.3:
            return best[1]
    return "en"


async def cartesia_tts(text: str, voice_id: str) -> bytes:
    r = await HTTP.post(
        f"{CARTESIA_URL}/tts/bytes",
        headers={**_auth(), "Content-Type": "application/json"},
        json={
            "model_id": TTS_MODEL,
            "transcript": text,
            "voice": {"id": voice_id},
            "language": detect_lang(text),
            "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
        },
        timeout=90,
    )
    r.raise_for_status()
    return r.content


async def to_voice(mp3: bytes):
    """টেলিগ্রামের ভয়েস মেসেজের জন্য অডিও রেডি করে (ffmpeg থাকলে OGG/Opus)"""
    if FFMPEG:
        try:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-loglevel", "error", "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
                "-f", "ogg", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate(mp3)
            if proc.returncode == 0 and out:
                return out, "voice.ogg"
        except Exception as e:
            logging.warning("ffmpeg error: %s", e)
    return mp3, "voice.mp3"


async def _fetch_gender(gender: str):
    out, after = [], None
    for _ in range(4):
        params = {"limit": 100, "gender": gender}
        if after:
            params["starting_after"] = after
        r = await HTTP.get(f"{CARTESIA_URL}/voices", headers=_auth(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        for v in j.get("data", []):
            if v.get("is_owner") or v.get("access") != "public":
                continue
            if v.get("status", "active") != "active":
                continue
            out.append(
                {
                    "id": v["id"],
                    "name": v.get("name") or "Voice",
                    "desc": f"{v.get('tagline') or ''} {v.get('description') or ''}",
                }
            )
        if len(out) >= DEFAULT_PER_GENDER or not j.get("has_more"):
            break
        after = j.get("next_page")
        if not after:
            break
    return out[:DEFAULT_PER_GENDER]


async def get_default_voices(g: str):
    """Cartesia এর ডিফল্ট ভয়েস (g = 'f' মেয়ে / 'm' ছেলে), ৬ ঘণ্টা ক্যাশ থাকে"""
    stale = time.time() - _dv["ts"] > 6 * 3600 or not _dv["f"] or not _dv["m"]
    if stale:
        async with _dv["lock"]:
            if time.time() - _dv["ts"] > 6 * 3600 or not _dv["f"] or not _dv["m"]:
                f, m = await asyncio.gather(
                    _fetch_gender("feminine"), _fetch_gender("masculine")
                )
                if not f and not m:
                    raise RuntimeError("কোনো ডিফল্ট ভয়েস পাওয়া যায়নি")
                _dv.update(f=f, m=m, ts=time.time())
    return _dv[g]


# র‍্যান্ডম ভয়েস: অল্প বয়সী শোনায় এমন, ডিপ/ভারী নয়
YOUNG_RE = re.compile(
    r"\b(young|youth\w*|teen\w*|kid\w*|boy\w*|girl\w*|energetic|bright|cheerful|"
    r"playful|upbeat|bubbly|lively|fresh|sweet|light|friendly|casual)\b", re.I)
DEEP_RE = re.compile(
    r"\b(deep|low|bass|baritone|gravel\w*|rasp\w*|husky|mature|older|elderly|old|"
    r"senior|authoritative|gruff|resonant|commanding|booming|smoky)\b", re.I)


async def random_voice():
    allv = (await get_default_voices("f")) + (await get_default_voices("m"))
    pool = (
        [v for v in allv if YOUNG_RE.search(v["desc"]) and not DEEP_RE.search(v["desc"])]
        or [v for v in allv if not DEEP_RE.search(v["desc"])]
        or allv
    )
    return random.choice(pool)


def api_error_text(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return "API কী/পারমিশন সমস্যা"
        if code in (402, 429):
            return "লিমিট শেষ, একটু পরে চেষ্টা করুন"
        return f"Cartesia error {code}"
    if isinstance(e, RuntimeError):
        return str(e)
    return "নেটওয়ার্ক সমস্যা"


# ---------------------------------------------------------------
# ইউজারের সেভ করা ভয়েস (Firestore এ চিরস্থায়ী)
# ---------------------------------------------------------------
async def get_user_voices(st):
    if st["voices"] is None:
        st["voices"] = await run(_get_voices, st["uid"])
    return st["voices"]


async def add_voice(st, v) -> bool:
    lst = await get_user_voices(st)
    if any(x["id"] == v["id"] for x in lst):
        return False
    lst.append({"id": v["id"], "name": v["name"], "kind": v.get("kind", "lib")})
    try:
        await run(_save_voices, st["uid"], lst)
    except Exception:
        lst.pop()
        raise
    return True


# ---------------------------------------------------------------
# মেনু রেন্ডার: (টেক্সট, কীবোর্ড, লেবেল→ভয়েস, ঠিক করা ভিউ)
# ---------------------------------------------------------------
async def render(view, st=None):
    n = view.get("n")
    labels = {}

    if n == "voice":
        rows = [
            [B(BTN_ALL), B(BTN_CREATE)],
            [B(BTN_CLONE), B(BTN_ADDID)],
            [B(BTN_HOME, NAV_STYLE)],
        ]
        return "🎙 Voice Generate\nআপনার পছন্দের অপশনটি বেছে নিন", kb(rows), labels, V("voice")

    if n == "allv":
        rows = [[B(BTN_FEMALE), B(BTN_MALE)], [B(BTN_BACK, NAV_STYLE), B(BTN_HOME, NAV_STYLE)]]
        return "🎭 All Voices\nকোন ধরনের ভয়েস দেখতে চান?", kb(rows), labels, V("allv")

    if n == "vlist":
        g = "m" if view.get("g") == "m" else "f"
        voices = await get_default_voices(g)
        pages = max(1, math.ceil(len(voices) / PER_PAGE))
        pg = min(max(int(view.get("p", 0)), 0), pages - 1)
        emoji = "👨" if g == "m" else "👩"
        btns = []
        for v in voices[pg * PER_PAGE:(pg + 1) * PER_PAGE]:
            label = uniq_label(emoji, v["name"], labels)
            labels[label] = v
            btns.append(B(label))
        rows = pair(btns)
        nav = []
        if pg > 0:
            nav.append(B(BTN_VPREV, NAV_STYLE))
        if pg < pages - 1:
            nav.append(B(BTN_VNEXT, NAV_STYLE))
        if nav:
            rows.append(nav)
        rows.append([B(BTN_BACK, NAV_STYLE), B(BTN_HOME, NAV_STYLE)])
        title = "Male" if g == "m" else "Female"
        text = (
            f"{emoji} {title} Voices ({pg + 1}/{pages})\n"
            "যে ভয়েসে ক্লিক করবেন সেটা আপনার Create Voice লিস্টে যোগ হবে"
        )
        return text, kb(rows), labels, V("vlist", g=g, p=pg)

    if n == "create":
        if st is None:
            raise RuntimeError("state নেই")
        voices = await get_user_voices(st)
        pages = max(1, math.ceil(len(voices) / PER_PAGE))
        pg = min(max(int(view.get("p", 0)), 0), pages - 1)
        btns = []
        for v in voices[pg * PER_PAGE:(pg + 1) * PER_PAGE]:
            label = uniq_label("🎤", v["name"], labels)
            labels[label] = v
            btns.append(B(label))
        rows = [[B(BTN_RANDOM)]] + pair(btns)
        nav = []
        if pg > 0:
            nav.append(B(BTN_VPREV, NAV_STYLE))
        if pg < pages - 1:
            nav.append(B(BTN_VNEXT, NAV_STYLE))
        if nav:
            rows.append(nav)
        rows.append([B(BTN_BACK, NAV_STYLE), B(BTN_HOME, NAV_STYLE)])
        text = "✨ Create Voice\nর‍্যান্ডম ভয়েস অথবা আপনার সেভ করা ভয়েস বেছে নিন"
        if not voices:
            text += "\n\n(এখনো কোনো সেভ ভয়েস নেই — All Voices, Clone Voice বা Add by Voice ID থেকে যোগ করুন)"
        elif pages > 1:
            text += f" ({pg + 1}/{pages})"
        return text, kb(rows), labels, V("create", p=pg)

    if n == "prompt":
        return "🏠 Home চাপুন অথবা /start দিন", kb([[B(BTN_HOME, NAV_STYLE)]]), labels, V("prompt")

    p = view.get("p", 1)
    if p not in PAGES:
        p = 1
    return menu_text(p), main_keyboard(p), labels, V("main", p=p)


# ---------------------------------------------------------------
# ক্লিন চ্যাট + দ্রুত রেসপন্স:
# - মেসেজ আগে পাঠানো হয়, মোছা/Firebase সেভ ব্যাকগ্রাউন্ডে হয় (তাই দেরি হয় না)
# - প্রতিটা উত্তরের সাথে কীবোর্ড আবার পাঠানো হয়, পুরনো মেসেজ মোছা হয় নতুনটা
#   যাওয়ার পরে - তাই ⊞ মেনু আইকন কখনো সরে যায় না
# - বানানো ভয়েস মেসেজ কখনো মোছা হয় না (keep লিস্ট)
# ---------------------------------------------------------------
_bg_tasks = set()


def spawn(coro):
    """ব্যাকগ্রাউন্ডে কাজ চালায়, হ্যান্ডলার আটকে থাকে না"""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def safe_delete(bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def sweep_old(bot, chat_id: int, new_id: int, old_id, extra_id=None, keep=()):
    """নতুন মেসেজের আগের মেসেজগুলো (আগের মেনু/ইউজারের মেসেজ) মুছে ফেলে।
    keep এ থাকা মেসেজ (বানানো ভয়েস) মোছা হয় না।"""
    ids = {new_id - i for i in range(1, 13)}
    if old_id:
        ids.add(old_id)
    if extra_id:
        ids.add(extra_id)
    ids.discard(new_id)
    ids -= set(keep)
    await asyncio.gather(*(safe_delete(bot, chat_id, i) for i in ids if i > 0))


async def load_state(user_id: int):
    msg, view, keep = None, MAIN1, []
    try:
        msg, view, keep = await run(_get_state, user_id)
    except Exception as e:
        logging.warning("firebase read error: %s", e)
    return {
        "uid": user_id, "msg": msg, "view": view, "keep": list(keep),
        "mode": None, "labels": None, "voices": None,
    }


async def get_state(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    cache = context.bot_data.setdefault("state", {})
    st = cache.get(chat_id)
    if st is None:
        st = await load_state(user_id)
        cache[chat_id] = st
    return st


async def save_bg(user, msg_id: int, view, keep, is_start: bool):
    try:
        await run(_save_user, user, msg_id, view, keep, is_start)
    except Exception as e:
        logging.warning("firebase write error: %s", e)


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    text, markup, view=None, is_start: bool = False):
    chat_id = update.effective_chat.id
    user = update.effective_user
    cache = context.bot_data.setdefault("state", {})

    async def _send():
        return await context.bot.send_message(chat_id, text, reply_markup=markup)

    st = cache.get(chat_id)
    if st is None:
        # ক্যাশ নেই (রিস্টার্টের পর প্রথমবার): মেসেজ পাঠানো আর Firebase রিড একসাথে
        sent, st = await asyncio.gather(_send(), load_state(user.id))
        cache[chat_id] = st
    else:
        sent = await _send()

    old_id = st["msg"]
    st["msg"] = sent.message_id
    if view is not None:
        st["view"] = view

    user_msg_id = update.message.message_id if update.message else None
    keep = st["keep"][-60:]
    st["keep"] = keep
    spawn(sweep_old(context.bot, chat_id, sent.message_id, old_id, user_msg_id, keep))
    spawn(save_bg(user, sent.message_id, st["view"], list(keep), is_start))
    return st


async def goto(update: Update, context: ContextTypes.DEFAULT_TYPE, view,
               is_start: bool = False, text=None, extra=None, mode=None):
    """কোনো মেনু পেজ দেখায়। text = পুরো লেখা বদলাতে, extra = ওপরে এক লাইন যোগ করতে"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    st = context.bot_data.setdefault("state", {}).get(chat_id)
    if st is None and view.get("n") == "create":
        st = await get_state(context, user.id, chat_id)
    try:
        rtext, markup, labels, view = await render(view, st)
    except Exception as e:
        logging.warning("render error: %s", e)
        rtext, markup, labels, view = await render(V("voice"))
        text = None
        extra = f"❌ ভয়েস লোড হয়নি ({api_error_text(e)}), আবার চেষ্টা করুন"
    final = text if text is not None else rtext
    if extra:
        final = f"{extra}\n\n{final}"
    st = await send_menu(update, context, final, markup, view, is_start)
    st["labels"] = labels
    st["mode"] = mode
    return st


def get_lock(context, chat_id):
    return context.bot_data.setdefault("locks", {}).setdefault(chat_id, asyncio.Lock())


def guarded(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await fn(update, context)
        except Exception:
            logging.exception("handler error")
            try:
                await update.effective_chat.send_message("⚠️ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")
            except Exception:
                pass
    return wrapper


# ---------------------------------------------------------------
# প্রম্পট (মেনু সরে গিয়ে ওপরে লেখা আসে, নিচে শুধু Home বাটন থাকে)
# ---------------------------------------------------------------
PROMPT_GEN = "আপনি যে ভয়েসটি বানাতে চান সেটি এখানে লিখুন"
PROMPT_ADDID = "আপনার Cartesia AI এর ভয়েস আইডি দিন"
PROMPT_CLONE = "৬০ সেকেন্ডের একটি স্পষ্ট ও ক্লিন ভয়েস পাঠান"
PROMPT_GROUP = "গ্রুপ/চ্যানেলের ID বা @username দিন\n(বটকে ওই গ্রুপ/চ্যানেলে অ্যাড থাকতে হবে)"
PROMPT_USER = "ইউজারের ID বা @username দিন\n(ইউজারকে আগে এই বটে /start দিতে হবে)"


async def prompt(update, context, text, mode, extra=None):
    return await goto(update, context, V("prompt"), text=text, extra=extra, mode=mode)


async def start_gen(update, context, v):
    return await prompt(
        update, context,
        f"{PROMPT_GEN}\n\n🎤 {v['name']}",
        {"t": "gen", "vid": v["id"], "vname": v["name"]},
    )


# ---------------------------------------------------------------
# মোড হ্যান্ডলার
# ---------------------------------------------------------------
async def do_generate(update, context, st, text: str):
    mode = st["mode"]
    chat_id = update.effective_chat.id
    if len(text) > MAX_TEXT:
        return await prompt(
            update, context, f"{PROMPT_GEN}\n\n🎤 {mode['vname']}", mode,
            extra=f"⚠️ লেখা অনেক বড় (সর্বোচ্চ {MAX_TEXT} অক্ষর)",
        )
    await context.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
    try:
        mp3 = await cartesia_tts(text, mode["vid"])
        audio, fname = await to_voice(mp3)
        sent = await context.bot.send_voice(
            chat_id, voice=InputFile(audio, filename=fname), reply_markup=SEND_KB
        )
    except Exception as e:
        logging.warning("tts error: %s", e)
        return await prompt(
            update, context, f"{PROMPT_GEN}\n\n🎤 {mode['vname']}", mode,
            extra=f"❌ ভয়েস তৈরি হয়নি ({api_error_text(e)}). আবার লিখুন",
        )
    st["keep"].append(sent.message_id)  # বানানো ভয়েস কখনো মোছা হবে না
    await goto(update, context, V("create", p=0), extra=f"✅ ভয়েস তৈরি হয়েছে — {mode['vname']}")


async def do_add_id(update, context, st, text: str):
    vid = text.strip()
    mode = st["mode"]
    if not UUID_RE.match(vid):
        return await prompt(update, context, PROMPT_ADDID, mode, extra="❌ ভয়েস আইডি সঠিক নয়")
    try:
        r = await HTTP.get(f"{CARTESIA_URL}/voices/{vid}", headers=_auth(), timeout=30)
        if r.status_code in (400, 403, 404, 422):
            return await prompt(
                update, context, PROMPT_ADDID, mode,
                extra="❌ ভয়েসটি পাওয়া যায়নি (ভয়েসটি Public হতে হবে)",
            )
        r.raise_for_status()
        j = r.json()
        if j.get("status") == "archived":
            return await prompt(update, context, PROMPT_ADDID, mode, extra="❌ ভয়েসটি আর্কাইভ করা")
        added = await add_voice(
            st, {"id": j.get("id") or vid, "name": j.get("name") or "Voice", "kind": "id"}
        )
    except Exception as e:
        logging.warning("add id error: %s", e)
        return await prompt(
            update, context, PROMPT_ADDID, mode, extra=f"❌ যোগ করা যায়নি ({api_error_text(e)})"
        )
    name = j.get("name") or "Voice"
    msg = f"✅ ভয়েস যোগ হয়েছে — {name}" if added else f"ℹ️ ভয়েসটি আগেই যোগ করা আছে — {name}"
    await goto(update, context, V("voice"), extra=msg)


async def do_send(update, context, st, text: str):
    mode = st["mode"]
    kind = mode["kind"]
    ptxt = PROMPT_GROUP if kind == "group" else PROMPT_USER
    target = text.strip()
    chat = None
    if re.fullmatch(r"-?\d+", target):
        chat = int(target)
    elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", target):
        uname = target.lstrip("@")
        if kind == "group":
            chat = "@" + uname
        else:
            chat = await run(_find_uid, uname)
            if chat is None:
                return await prompt(
                    update, context, ptxt, mode,
                    extra="❌ এই username এর ইউজার বটে নেই (তাকে আগে /start দিতে হবে), ID দিন",
                )
    if chat is None:
        return await prompt(update, context, ptxt, mode, extra="❌ ID বা @username সঠিক নয়")
    try:
        if mode.get("k") == "audio":
            await context.bot.send_audio(chat, audio=mode["file_id"])
        else:
            await context.bot.send_voice(chat, voice=mode["file_id"])
    except Forbidden:
        return await prompt(
            update, context, ptxt, mode,
            extra="❌ পাঠানো যায়নি (ইউজার বটকে স্টার্ট করেনি / বট গ্রুপে নেই)",
        )
    except TelegramError as e:
        logging.warning("send error: %s", e)
        return await prompt(update, context, ptxt, mode, extra="❌ পাঠানো যায়নি, ID/username ঠিক আছে কিনা দেখুন")
    await goto(update, context, V("create", p=0), extra=f"✅ ভয়েস পাঠানো হয়েছে — {target}")


# ---------------------------------------------------------------
# হ্যান্ডলার
# ---------------------------------------------------------------
@guarded
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_lock(context, update.effective_chat.id):
        await goto(update, context, MAIN1, is_start=True)


@guarded
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    user = update.effective_user

    async with get_lock(context, chat_id):
        st = await get_state(context, user.id, chat_id)
        view = st["view"]

        # ---------- নেভিগেশন বাটন ----------
        if text == NEXT:
            return await goto(update, context, V("main", p=2))
        if text == PREV or text == BTN_HOME:
            return await goto(update, context, MAIN1)
        if text == BTN_BACK:
            return await goto(update, context, V("allv") if view.get("n") == "vlist" else V("voice"))
        if text in (BTN_VPREV, BTN_VNEXT) and view.get("n") in ("vlist", "create"):
            nv = dict(view)
            nv["p"] = max(0, int(view.get("p", 0)) + (-1 if text == BTN_VPREV else 1))
            return await goto(update, context, nv)
        if text == BUTTONS["voice"]:
            return await goto(update, context, V("voice"))
        if text == BTN_ALL:
            return await goto(update, context, V("allv"))
        if text == BTN_FEMALE:
            return await goto(update, context, V("vlist", g="f", p=0))
        if text == BTN_MALE:
            return await goto(update, context, V("vlist", g="m", p=0))
        if text == BTN_CREATE:
            return await goto(update, context, V("create", p=0))
        if text == BTN_CLONE:
            return await prompt(update, context, PROMPT_CLONE, {"t": "clone"})
        if text == BTN_ADDID:
            return await prompt(update, context, PROMPT_ADDID, {"t": "addid"})
        if text == BTN_RANDOM:
            try:
                v = await random_voice()
            except Exception as e:
                logging.warning("random voice error: %s", e)
                return await goto(update, context, V("create", p=0),
                                  extra=f"❌ ভয়েস লোড হয়নি ({api_error_text(e)})")
            return await start_gen(update, context, v)

        # ---------- ইউজার কিছু লিখছে (মোড চালু) ----------
        mode = st["mode"]
        if mode:
            t = mode["t"]
            if t == "gen":
                return await do_generate(update, context, st, text)
            if t == "addid":
                return await do_add_id(update, context, st, text)
            if t == "send":
                return await do_send(update, context, st, text)
            if t == "clone":
                return await prompt(update, context, PROMPT_CLONE, mode,
                                    extra="⚠️ লেখা নয়, ভয়েস মেসেজ পাঠান")

        # ---------- লিস্টের ভয়েস বাটন ----------
        labels = st["labels"]
        if labels is None:
            try:
                _, _, labels, _ = await render(view, st)
            except Exception as e:
                logging.warning("labels rebuild error: %s", e)
                labels = {}
            st["labels"] = labels
        if text in labels:
            v = labels[text]
            n = view.get("n")
            if n == "vlist":
                added = await add_voice(st, {"id": v["id"], "name": v["name"], "kind": "lib"})
                msg = f"✅ যোগ হয়েছে — {v['name']}" if added else f"ℹ️ আগেই যোগ করা আছে — {v['name']}"
                return await goto(update, context, view, extra=msg)
            if n == "create":
                return await start_gen(update, context, v)

        # ---------- বাকি সার্ভিস বাটন ----------
        if text in LABEL_TO_KEY:
            mv = view if view.get("n") == "main" else MAIN1
            return await goto(update, context, mv, text=f"{text}\n\n🚧 এই ফিচারটি শীঘ্রই আসছে...")
        # অন্য কোনো লেখা এলে কিছু মুছবে না


@guarded
async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ভয়েস ক্লোন: ইউজারের পাঠানো ভয়েস/অডিও দিয়ে Cartesia তে ক্লোন করে"""
    msg = update.message
    m = msg.voice or msg.audio
    chat_id = update.effective_chat.id
    user = update.effective_user

    async with get_lock(context, chat_id):
        st = await get_state(context, user.id, chat_id)
        mode = st["mode"]
        if not mode or mode.get("t") != "clone":
            return

        def retry(err):
            return prompt(update, context, PROMPT_CLONE, mode, extra=err)

        dur = m.duration or 0
        if dur < CLONE_MIN_SEC:
            return await retry(f"❌ ভয়েস খুব ছোট ({dur}s), কমপক্ষে {CLONE_MIN_SEC} সেকেন্ড দরকার")
        if dur > CLONE_MAX_SEC:
            return await retry(f"❌ ভয়েস অনেক বড় ({dur}s), সর্বোচ্চ {CLONE_MAX_SEC} সেকেন্ড")
        if m.file_size and m.file_size > 15 * 1024 * 1024:
            return await retry("❌ ফাইল অনেক বড় (সর্বোচ্চ 15MB)")

        if msg.voice:
            fname, mime = "clip.ogg", "audio/ogg"
        else:
            fname = m.file_name or "clip.mp3"
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "mp3"
            if ext not in CLONE_EXTS:
                return await retry("❌ ফরম্যাট সাপোর্ট নেই (mp3, wav, ogg, flac দিন)")
            mime = m.mime_type or "audio/mpeg"

        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            tg_file = await context.bot.get_file(m.file_id)
            data = bytes(await tg_file.download_as_bytearray())
            voices = await get_user_voices(st)
            n = sum(1 for x in voices if x.get("kind") == "clone") + 1
            name = f"My Clone {n}"
            r = await HTTP.post(
                f"{CARTESIA_URL}/voices/clone",
                headers=_auth(),
                data={"name": name, "language": CLONE_LANG, "access": "private"},
                files={"clip": (fname, data, mime)},
                timeout=180,
            )
            if r.status_code in (400, 422):
                logging.warning("clone rejected: %s", r.text[:300])
                return await retry("❌ ভয়েস ক্লোন হয়নি, স্পষ্ট ও নয়েজ-মুক্ত ভয়েস পাঠান")
            r.raise_for_status()
            j = r.json()
            await add_voice(st, {"id": j["id"], "name": j.get("name") or name, "kind": "clone"})
        except Exception as e:
            logging.warning("clone error: %s", e)
            return await retry(f"❌ ক্লোন করা যায়নি ({api_error_text(e)})")
        await goto(
            update, context, V("voice"),
            extra=f"✅ ভয়েস ক্লোন হয়েছে — {name}\nCreate Voice এ গিয়ে ব্যবহার করুন",
        )


@guarded
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ভয়েসের নিচের Send Group/Channel ও Send User বাটন"""
    q = update.callback_query
    await q.answer()
    msg = q.message
    if msg is None:
        return
    if msg.voice:
        fid, k = msg.voice.file_id, "voice"
    elif msg.audio:
        fid, k = msg.audio.file_id, "audio"
    else:
        return
    kind = "group" if q.data == "send_group" else "user"
    chat_id = update.effective_chat.id
    user = update.effective_user

    async with get_lock(context, chat_id):
        await get_state(context, user.id, chat_id)
        await prompt(
            update, context,
            PROMPT_GROUP if kind == "group" else PROMPT_USER,
            {"t": "send", "kind": kind, "file_id": fid, "k": k},
        )


async def post_init(app: Application):
    global HTTP
    HTTP = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    _dv["lock"] = asyncio.Lock()
    # বাম পাশের 3 লাইনের "Menu" বাটন সরানো
    try:
        await app.bot.delete_my_commands()
        await app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except TelegramError as e:
        logging.warning("menu button reset error: %s", e)


# ---------------------------------------------------------------
# Render + UptimeRobot এর জন্য ছোট ওয়েব সার্ভার
# ---------------------------------------------------------------
web = Flask(__name__)


@web.route("/")
def home():
    return "Bot is running ✅"


@web.route("/health")
def health():
    return "OK", 200


def run_web():
    web.run(host="0.0.0.0", port=PORT)


def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)   # একজনের ভয়েস বানানো অন্যদের আটকে রাখবে না
        .build()
    )
    app.add_handler(CommandHandler(["start", "menu"], cmd_menu))
    app.add_handler(CallbackQueryHandler(on_callback, pattern="^send_(group|user)$"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
