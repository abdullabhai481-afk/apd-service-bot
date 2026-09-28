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


def _get_last_msg(uid: int):
    snap = db.collection(USERS).document(str(uid)).get()
    if snap.exists:
        return (snap.to_dict() or {}).get("last_msg_id")
    return None


def _save_user(user, msg_id, is_start: bool):
    ref = db.collection(USERS).document(str(user.id))
    data = {
        "name": user.full_name,
        "username": user.username,
        "last_seen": firestore.SERVER_TIMESTAMP,
        "last_msg_id": msg_id,
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
# ক্লিন চ্যাট + কম ঝিলিক:
# - বাটনের লেখা (ইউজারের মেসেজ) মুছে যায়
# - ফিচারের উত্তর আগের বটের মেসেজটাই এডিট করে দেখায় (নতুন মেসেজ নয়),
#   তাই কীবোর্ড আর মেনু যেমন ছিল তেমনই থাকে
# - শুধু পেজ বদলালে নতুন কীবোর্ড পাঠাতে হয়
# - আগের মেসেজের আইডি Firebase এ থাকে, রিস্টার্টেও কাজ করে
# ---------------------------------------------------------------
async def safe_delete(bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def sweep_old(bot, chat_id: int, new_id: int, old_id):
    """নতুন মেনুর আগের মেসেজগুলো (আগের মেনু/ফিচার মেসেজ) মুছে ফেলে।
    আগের মেসেজের আইডি হারিয়ে গেলেও কাজ করে।"""
    ids = {new_id - i for i in range(1, 13)}
    if old_id:
        ids.add(old_id)
    ids.discard(new_id)
    await asyncio.gather(*(safe_delete(bot, chat_id, i) for i in ids if i > 0))


async def get_old_id(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    cache = context.bot_data.setdefault("last_msg", {})
    old_id = cache.get(chat_id)
    if old_id is None:
        try:
            old_id = await run(_get_last_msg, user_id)
        except Exception as e:
            logging.warning("firebase read error: %s", e)
    return old_id


async def finish(context, user, chat_id: int, msg_id: int, is_start: bool):
    context.bot_data.setdefault("last_msg", {})[chat_id] = msg_id
    try:
        await run(_save_user, user, msg_id, is_start)
    except Exception as e:
        logging.warning("firebase write error: %s", e)


async def show_page(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    page: int, is_start: bool = False):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if update.message:
        await safe_delete(context.bot, chat_id, update.message.message_id)

    old_id = await get_old_id(context, user.id, chat_id)

    sent = await context.bot.send_message(
        chat_id, menu_text(page), reply_markup=menu_keyboard(page)
    )
    await sweep_old(context.bot, chat_id, sent.message_id, old_id)

    await finish(context, user, chat_id, sent.message_id, is_start)


async def show_feature(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if update.message:
        await safe_delete(context.bot, chat_id, update.message.message_id)

    old_id = await get_old_id(context, user.id, chat_id)
    new_id = None

    if old_id:
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=old_id)
            new_id = old_id
        except BadRequest as e:
            if "not modified" in str(e).lower():
                new_id = old_id
        except TelegramError:
            pass

    if new_id is None:
        sent = await context.bot.send_message(chat_id, text)
        new_id = sent.message_id
        if old_id:
            await safe_delete(context.bot, chat_id, old_id)

    await finish(context, user, chat_id, new_id, False)


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
