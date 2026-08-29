"""NoveraSearch Telegram bot.

Install dependencies:
    pip install python-telegram-bot==21.* telethon aiohttp

Fill BOT_TOKEN, API_ID, API_HASH, PHONE and OWNER_ID before running.
"""

import asyncio
import logging
import random
import re
import sqlite3
import string
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:
    from telethon import TelegramClient
    from telethon.errors import RPCError, UsernameNotOccupiedError, UsernamePurchaseAvailableError
    from telethon.tl.functions.account import CheckUsernameRequest
    from telethon.tl.functions.fragment import GetCollectibleInfoRequest
    from telethon.tl.types import InputCollectibleUsername
except ImportError:  # Lets the bot start when Telegram username checks are not configured.
    TelegramClient = None
    RPCError = UsernameNotOccupiedError = UsernamePurchaseAvailableError = Exception
    CheckUsernameRequest = GetCollectibleInfoRequest = InputCollectibleUsername = None

BOT_TOKEN = "8205193243:AAG9fjSnrP3wSvxDpHepVcW0ya-h0jRQk50"
API_ID = 31799721
API_HASH = "eb2181220b3b8a0b6a7f93cd8075a559"
PHONE = "+77024728757"
OWNER_ID = 8872934046

BRAND = "NoveraSearch"
DATABASE = Path(__file__).with_name("novera_search.db")
FREE_DAILY_LIMIT = 5
PAYMENT_PROOF = 1

PREMIUM_PLANS = {
    "1d": (1, 15, 20),
    "5d": (5, 50, 60),
    "7d": (7, 65, 70),
    "30d": (30, 150, 170),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(db()) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TEXT NOT NULL,
                found_count INTEGER NOT NULL DEFAULT 0,
                daily_date TEXT,
                daily_count INTEGER NOT NULL DEFAULT 0,
                premium_until TEXT
            );
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_key TEXT NOT NULL,
                payment_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
        """)
        con.commit()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_owner(user_id: int) -> bool:
    return bool(OWNER_ID) and user_id == OWNER_ID


def register_user(tg_user) -> bool:
    """Create/update a user; return True only on first registration."""
    now = utcnow().isoformat()
    with closing(db()) as con:
        exists = con.execute("SELECT 1 FROM users WHERE user_id = ?", (tg_user.id,)).fetchone()
        con.execute(
            """INSERT INTO users (user_id, username, first_name, registered_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (tg_user.id, tg_user.username, tg_user.first_name, now),
        )
        con.commit()
    return exists is None


def user_row(user_id: int):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def premium_active(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    row = user_row(user_id)
    if not row or not row["premium_until"]:
        return False
    try:
        return datetime.fromisoformat(row["premium_until"]) > utcnow()
    except ValueError:
        return False


def can_search(user_id: int) -> tuple[bool, str]:
    if premium_active(user_id):
        return True, ""
    today = utcnow().date().isoformat()
    with closing(db()) as con:
        row = con.execute("SELECT daily_date, daily_count FROM users WHERE user_id = ?", (user_id,)).fetchone()
        used = row["daily_count"] if row and row["daily_date"] == today else 0
    if used >= FREE_DAILY_LIMIT:
        return False, f"Бесплатный лимит: {FREE_DAILY_LIMIT} найденных юзернеймов в день."
    return True, ""


def record_found(user_id: int) -> None:
    today = utcnow().date().isoformat()
    with closing(db()) as con:
        row = con.execute("SELECT daily_date FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row and row["daily_date"] == today:
            con.execute("UPDATE users SET found_count=found_count+1, daily_count=daily_count+1 WHERE user_id=?", (user_id,))
        else:
            con.execute("UPDATE users SET found_count=found_count+1, daily_date=?, daily_count=1 WHERE user_id=?", (today, user_id))
        con.commit()


def statistics() -> tuple[int, int, int, int]:
    with closing(db()) as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        found = con.execute("SELECT COALESCE(SUM(found_count), 0) FROM users").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM payment_requests WHERE status='pending'").fetchone()[0]
        rows = con.execute("SELECT user_id FROM users").fetchall()
    premium = sum(premium_active(row["user_id"]) for row in rows)
    return total, premium, found, pending


async def update_bot_description(application: Application) -> None:
    """Telegram displays the short description on the bot profile, below its title."""
    total, _, _, _ = statistics()
    description = f"{BRAND} • Пользователей: {total:,}".replace(",", " ")
    try:
        await application.bot.set_my_short_description(short_description=description)
    except Exception:
        log.exception("Could not update the bot short description")


def main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔎 Искать юзернейм", callback_data="search")],
        [InlineKeyboardButton("💎 Премиум", callback_data="premium")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
    ]
    if is_owner(user_id):
        rows.append([InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def back_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="menu")]])


def rank(found: int) -> str:
    if found < 50:
        return "1 ранг — Разведчик"
    if found < 120:
        return "2 ранг — Коллекционер"
    if found < 222:
        return "3 ранг — Скаут"
    if found < 700:
        return "4 ранг — Охотник"
    return "5 ранг — Novera Prime"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    newly_registered = register_user(user)
    if newly_registered:
        await update_bot_description(context.application)
    await update.effective_message.reply_text(
        f"Добро пожаловать в {BRAND}!\nЗдесь вы можете найти свободные юзернеймы.",
        reply_markup=main_menu(user.id),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(f"{BRAND}: выберите действие.", reply_markup=main_menu(query.from_user.id))


async def show_profile(query) -> None:
    row = user_row(query.from_user.id)
    found = row["found_count"] if row else 0
    premium = "Premium владельца — навсегда" if is_owner(query.from_user.id) else (
        "Premium активен" if premium_active(query.from_user.id) else "Без Premium"
    )
    await query.edit_message_text(
        f"👤 Профиль\n\nID: {query.from_user.id}\nВаш ранг: {rank(found)}\n"
        f"Найдено юзернеймов: {found}\nСтатус: {premium}",
        reply_markup=back_button(),
    )


class FragmentStatus(str, Enum):
    """Statuses obtained from Telegram's Fragment MTProto methods, never page HTML."""

    NOT_FOUND = "Not Found"
    SOLD = "Sold"
    PURCHASE_AVAILABLE = "Available / On Auction"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class UsernameCheck:
    telegram_free: bool
    fragment: FragmentStatus

    @property
    def is_safe_basic_username(self) -> bool:
        return self.telegram_free and self.fragment is FragmentStatus.NOT_FOUND


def rpc_message(error: Exception) -> str:
    return str(getattr(error, "message", "") or error).upper()


async def telegram_basic_username_free(candidate: str, client) -> bool:
    """Return True only for an ordinary Telegram username, never a Fragment collectible."""
    try:
        return bool(await client(CheckUsernameRequest(candidate)))
    except UsernamePurchaseAvailableError:
        # Telegram has explicitly identified a collectible that can be bought.
        return False
    except RPCError as error:
        if "USERNAME_PURCHASE_AVAILABLE" in rpc_message(error):
            return False
        log.info("Telegram rejected @%s: %s", candidate, error)
        return False


async def fragment_username_status(candidate: str, client) -> FragmentStatus:
    """Use Telegram's official fragment.getCollectibleInfo MTProto method.

    Fragment has no stable public username-status API.  This method is an official
    server-side collectible lookup, so it is deliberately fail-closed: an error or
    a non-visible collectible is never treated as a free Fragment username.
    """
    try:
        await client(GetCollectibleInfoRequest(InputCollectibleUsername(candidate)))
        # A visible collectible has a recorded Fragment purchase and is not free.
        return FragmentStatus.SOLD
    except UsernamePurchaseAvailableError:
        return FragmentStatus.PURCHASE_AVAILABLE
    except UsernameNotOccupiedError:
        return FragmentStatus.NOT_FOUND
    except RPCError as error:
        message = rpc_message(error)
        if "USERNAME_PURCHASE_AVAILABLE" in message:
            return FragmentStatus.PURCHASE_AVAILABLE
        if "USERNAME_NOT_OCCUPIED" in message or "USERNAME_INVALID" in message:
            return FragmentStatus.NOT_FOUND
        log.warning("Fragment lookup is inconclusive for @%s: %s", candidate, error)
        return FragmentStatus.UNKNOWN
    except Exception:
        log.exception("Fragment lookup failed for @%s", candidate)
        return FragmentStatus.UNKNOWN


async def check_username(candidate: str, context: ContextTypes.DEFAULT_TYPE) -> UsernameCheck:
    """Verify Telegram first, then Fragment; only a dual-confirmed result is usable."""
    client = context.application.bot_data.get("telethon")
    if not client:
        return UsernameCheck(False, FragmentStatus.UNKNOWN)
    telegram_free = await telegram_basic_username_free(candidate, client)
    # Fragment is always queried after the Telegram check, including occupied names.
    fragment = await fragment_username_status(candidate, client)
    return UsernameCheck(telegram_free, fragment)


async def find_username(length: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    alphabet = string.ascii_lowercase
    for _ in range(20):
        candidate = "".join(random.choices(alphabet, k=length))
        result = await check_username(candidate, context)
        if result.is_safe_basic_username:
            return candidate
        await asyncio.sleep(0.25)
    return None


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data
    if action == "menu":
        await menu(update, context)
    elif action == "profile":
        await show_profile(query)
    elif action == "search":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("5 букв", callback_data="length:5"), InlineKeyboardButton("6 букв", callback_data="length:6")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
        ])
        await query.edit_message_text("Выберите длину юзернейма:", reply_markup=keyboard)
    elif action.startswith("length:"):
        length = int(action.split(":")[1])
        if length == 5 and not premium_active(query.from_user.id):
            await query.answer("Для поиска 5-значных юзернеймов требуется Premium", show_alert=True)
            return None
        allowed, reason = can_search(query.from_user.id)
        if not allowed:
            await query.answer(reason, show_alert=True)
            return None
        await query.edit_message_text("Ищу свободный юзернейм в Telegram…")
        candidate = await find_username(length, context)
        if candidate:
            record_found(query.from_user.id)
            await query.edit_message_text(
                f"✅ Возможный свободный юзернейм: @{candidate}\n\n"
                "Перед использованием дополнительно проверьте его на Fragment.com.",
                reply_markup=back_button(),
            )
        else:
            await query.edit_message_text("За эту попытку свободный вариант не найден. Попробуйте ещё раз.", reply_markup=back_button())
    elif action == "premium":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 день — 15⭐ / 20₽", callback_data="plan:1d")],
            [InlineKeyboardButton("5 дней — 50⭐ / 60₽", callback_data="plan:5d")],
            [InlineKeyboardButton("7 дней — 65⭐ / 70₽", callback_data="plan:7d")],
            [InlineKeyboardButton("Месяц — 150⭐ / 170₽", callback_data="plan:30d")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
        ])
        await query.edit_message_text("Выберите срок Premium:", reply_markup=keyboard)
    elif action.startswith("plan:"):
        plan = action.split(":")[1]
        context.user_data["plan"] = plan
        await query.edit_message_text(
            "Выберите тип оплаты:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Звёзды", callback_data="pay:stars"), InlineKeyboardButton("💳 Рубли", callback_data="pay:rub")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
            ]),
        )
    elif action.startswith("pay:"):
        payment_type = action.split(":")[1]
        plan = context.user_data.get("plan", "1d")
        _, stars, rubles = PREMIUM_PLANS[plan]
        details = f"Отправьте {stars} звёзд на аккаунт @fegote." if payment_type == "stars" else (
            f"Переведите {rubles} рублей на реквизиты:\n+79313716777\nТ-Банк\nТимур/Наталья"
        )
        context.user_data["payment_type"] = payment_type
        await query.edit_message_text(
            f"{details}\n\nПосле оплаты отправьте фото подтверждения следующим сообщением.",
            reply_markup=back_button(),
        )
        return PAYMENT_PROOF
    elif action == "admin" and is_owner(query.from_user.id):
        total, premium, found, pending = statistics()
        await query.edit_message_text(
            f"⚙️ Админ-панель\n\n👥 Пользователей: {total}\n💎 Premium: {premium}\n"
            f"🔎 Найдено: {found}\n📨 Ожидают заявки: {pending}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="admin")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
            ]),
        )
    return None


async def receive_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправьте фото подтверждения оплаты.")
        return PAYMENT_PROOF
    plan = context.user_data.get("plan")
    payment_type = context.user_data.get("payment_type")
    if not plan or not payment_type:
        await update.message.reply_text("Сначала выберите тариф и способ оплаты.", reply_markup=main_menu(update.effective_user.id))
        return ConversationHandler.END
    with closing(db()) as con:
        cursor = con.execute(
            "INSERT INTO payment_requests (user_id, plan_key, payment_type, created_at) VALUES (?, ?, ?, ?)",
            (update.effective_user.id, plan, payment_type, utcnow().isoformat()),
        )
        request_id = cursor.lastrowid
        con.commit()
    if OWNER_ID:
        sender = update.effective_user
        caption = f"📨 Заявка на Premium #{request_id}\nПользователь: @{sender.username or 'нет'}\nID: {sender.id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{request_id}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{request_id}")]])
        await context.bot.send_photo(OWNER_ID, update.message.photo[-1].file_id, caption=caption, reply_markup=keyboard)
    await update.message.reply_text("Заявка отправлена владельцу. Мы сообщим о результате.", reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END


async def moderation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    decision, raw_id = query.data.split(":")
    request_id = int(raw_id)
    with closing(db()) as con:
        request = con.execute("SELECT * FROM payment_requests WHERE id=? AND status='pending'", (request_id,)).fetchone()
        if not request:
            await query.edit_message_caption(caption="Эта заявка уже обработана.")
            return
        status = "approved" if decision == "approve" else "rejected"
        con.execute("UPDATE payment_requests SET status=? WHERE id=?", (status, request_id))
        if status == "approved":
            days = PREMIUM_PLANS[request["plan_key"]][0]
            current = user_row(request["user_id"])
            base = utcnow()
            if current and current["premium_until"]:
                try:
                    base = max(base, datetime.fromisoformat(current["premium_until"]))
                except ValueError:
                    pass
            con.execute("UPDATE users SET premium_until=? WHERE user_id=?", ((base + timedelta(days=days)).isoformat(), request["user_id"]))
        con.commit()
    await context.bot.send_message(request["user_id"], "✅ Premium активирован!" if decision == "approve" else "❌ Заявка на Premium отклонена.")
    await query.edit_message_caption(caption=f"Заявка #{request_id}: {'одобрена' if decision == 'approve' else 'отклонена'}.")


def telethon_code_callback() -> str:
    """Read exactly five Telegram code digits, written as 1.2.3.4.5."""
    while True:
        formatted = input("Введите код Telegram в формате 1.2.3.4.5: ").strip()
        if re.fullmatch(r"\d(?:\.\d){4}", formatted):
            return formatted.replace(".", "")
        print("Неверный формат. Нужны ровно 5 цифр с точками: 1.2.3.4.5")


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([BotCommand("start", "Запустить NoveraSearch")])
    await update_bot_description(application)
    if TelegramClient and API_ID and API_HASH and PHONE:
        client = TelegramClient("novera_telethon", API_ID, API_HASH)
        await client.start(phone=PHONE, code_callback=telethon_code_callback)
        application.bot_data["telethon"] = client
    else:
        log.warning("Telethon is not configured; username lookup will be unavailable.")


async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("telethon")
    if client:
        await client.disconnect()


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN before starting NoveraSearch.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    payment = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callback, pattern=r"^pay:")],
        states={PAYMENT_PROOF: [MessageHandler(filters.PHOTO, receive_proof)]},
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )
    app.add_handler(payment)
    app.add_handler(CallbackQueryHandler(moderation, pattern=r"^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app


if __name__ == "__main__":
    init_db()
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
