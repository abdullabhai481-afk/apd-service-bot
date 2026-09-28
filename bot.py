import os
import asyncio
import logging
import threading

import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask
from telegram import (
    KeyboardButton,
    MenuButtonDefault,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))

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


async def run(fn, *args):
    """Firestore sync কল বটকে আটকে না রেখে আলাদা থ্রেডে চালায়"""
    return await asyncio.to_thread(fn, *args)


def _get_state(uid: int):
    snap = db.collection(USERS).document(str(uid)).get()
    if snap.exists:
        d = snap.to_dict() or {}
        return d.get("last_msg_id"), d.get("page") or 1
    return None, 1


def _save_user(user, msg_id, page: int, is_start: bool):
    ref = db.collection(USERS).document(str(user.id))
    data = {
        "name": user.full_name,
        "username": user.username,
        "last_seen": firestore.SERVER_TIMESTAMP,
        "last_msg_id": msg_id,
        "page": page,
        "actions": firestore.Increment(1),
    }
    if is_start and not ref.get().exists:
        data["joined_at"] = firestore.SERVER_TIMESTAMP
    ref.set(data, merge=True)


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


def menu_keyboard(page: int) -> ReplyKeyboardMarkup:
    keys = PAGES[page]
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                KeyboardButton(BUTTONS[k], style=COLORS.get(k, DEFAULT_STYLE))
                for k in keys[i:i + 2]
            ]
        )
    nav_label = NEXT if page < TOTAL_PAGES else PREV
    rows.append([KeyboardButton(nav_label, style=NAV_STYLE)])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,   # বাটন চাপলে মেনু বন্ধ হবে না
        is_persistent=False,       # শুধু ⊞ আইকনে খোলা/বন্ধ হবে
        input_field_placeholder="মেনু থেকে বেছে নিন 👇",
    )


PAGE_NAMES = {1: "প্রথম পেজ", 2: "দ্বিতীয় পেজ"}


def menu_text(page: int) -> str:
    return f"{PAGE_NAMES[page]}\nআপনার পছন্দের সার্ভিসটি বেছে নিন"


# ---------------------------------------------------------------
# ক্লিন চ্যাট + দ্রুত রেসপন্স:
# - মেসেজ আগে পাঠানো হয়, মোছা/Firebase সেভ ব্যাকগ্রাউন্ডে হয় (তাই দেরি হয় না)
# - ফিচারের উত্তরেও বর্তমান পেজের কীবোর্ড সাথে পাঠানো হয়, আর পুরনো মেসেজ
#   মোছা হয় নতুনটা যাওয়ার পরে - তাই ⊞ মেনু আইকন কখনো সরে যায় না
#   (মেসেজ এডিট করলে টেলিগ্রাম ক্লায়েন্ট কীবোর্ড সরিয়ে দিত, সেটাই সমস্যা ছিল)
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


async def sweep_old(bot, chat_id: int, new_id: int, old_id, extra_id=None):
    """নতুন মেসেজের আগের মেসেজগুলো (আগের মেনু/ফিচার মেসেজ) মুছে ফেলে।
    আগের মেসেজের আইডি হারিয়ে গেলেও কাজ করে।"""
    ids = {new_id - i for i in range(1, 13)}
    if old_id:
        ids.add(old_id)
    if extra_id:
        ids.add(extra_id)
    ids.discard(new_id)
    await asyncio.gather(*(safe_delete(bot, chat_id, i) for i in ids if i > 0))


async def get_state(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    """(আগের মেসেজ আইডি, বর্তমান পেজ) - আগে মেমরি ক্যাশ, না থাকলে Firebase"""
    cache = context.bot_data.setdefault("state", {})
    st = cache.get(chat_id)
    if st is None:
        old_id, page = None, 1
        try:
            old_id, page = await run(_get_state, user_id)
        except Exception as e:
            logging.warning("firebase read error: %s", e)
        st = {"msg": old_id, "page": page if page in PAGES else 1}
        cache[chat_id] = st
    return st["msg"], st["page"]


async def save_bg(user, msg_id: int, page: int, is_start: bool):
    try:
        await run(_save_user, user, msg_id, page, is_start)
    except Exception as e:
        logging.warning("firebase write error: %s", e)


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    page, text_fn, is_start: bool = False):
    """page=None হলে বর্তমান পেজ ধরে নেয় (ফিচার বাটনের জন্য)"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    cache = context.bot_data.setdefault("state", {})

    async def _send(p):
        return await context.bot.send_message(
            chat_id, text_fn(p), reply_markup=menu_keyboard(p)
        )

    if chat_id in cache:
        old_id, cur_page = cache[chat_id]["msg"], cache[chat_id]["page"]
        use_page = cur_page if page is None else page
        sent = await _send(use_page)
    elif page is not None:
        # ক্যাশ নেই (রিস্টার্টের পর প্রথমবার): মেসেজ পাঠানো আর Firebase রিড একসাথে
        sent, st = await asyncio.gather(
            _send(page), get_state(context, user.id, chat_id)
        )
        old_id, use_page = st[0], page
    else:
        old_id, use_page = await get_state(context, user.id, chat_id)
        sent = await _send(use_page)

    cache[chat_id] = {"msg": sent.message_id, "page": use_page}

    user_msg_id = update.message.message_id if update.message else None
    spawn(sweep_old(context.bot, chat_id, sent.message_id, old_id, user_msg_id))
    spawn(save_bg(user, sent.message_id, use_page, is_start))


async def show_page(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    page: int, is_start: bool = False):
    await send_menu(update, context, page, menu_text, is_start)


async def show_feature(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await send_menu(update, context, None, lambda p: text)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_page(update, context, 1, is_start=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == NEXT:
        await show_page(update, context, 2)
    elif text == PREV:
        await show_page(update, context, 1)
    elif text in LABEL_TO_KEY:
        await show_feature(update, context, f"{text}\n\n🚧 এই ফিচারটি শীঘ্রই আসছে...")
    # অন্য কোনো লেখা এলে কিছু মুছবে না


async def post_init(app: Application):
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

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
