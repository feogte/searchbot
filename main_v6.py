
import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient, events, types as tl_types
from telethon.errors import SessionPasswordNeededError


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"

API_ID = 12345678
API_HASH = "PASTE_YOUR_API_HASH_HERE"

# Personal Telegram account that receives Stars Gifts.
MTproto_SESSION = "fegote_stars"
MTproto_PHONE = "+77024728757"

# One test administrator for now.
ADMIN_ID = 8872934046

# Support / Stars recipient.
ADMIN_USERNAME = "@fegote"

# RUB payment details.
RUB_PAYMENT_DETAILS = "+79313716777 • Т-Банк • Наталья/Тимур"

DB_FILE = "shop.db"

# How often the bot rescans Gifts when a user presses
# "Отправить подтверждение".
STARS_SCAN_LIMIT = 100


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db_lock = asyncio.Lock()


def db_init():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            internal_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL,
            purchases_count INTEGER NOT NULL DEFAULT 0,
            rub_spent INTEGER NOT NULL DEFAULT 0,
            stars_spent INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            country_code TEXT NOT NULL,
            country_name TEXT NOT NULL,
            flag TEXT NOT NULL,
            item_name TEXT NOT NULL,
            price_rub INTEGER NOT NULL,
            price_stars INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_telegram_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            payment_type TEXT NOT NULL,
            price_rub INTEGER NOT NULL,
            price_stars INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            proof_file_id TEXT,
            proof_type TEXT,
            stars_total INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS used_star_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            payment_identifier TEXT UNIQUE NOT NULL,
            amount_stars INTEGER NOT NULL,
            gift_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    db.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def dt_from_iso(value):
    return datetime.fromisoformat(value)


def get_or_create_user(tg_user):
    row = db.execute(
        "SELECT * FROM users WHERE telegram_id = ?",
        (tg_user.id,),
    ).fetchone()

    if row:
        db.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?
            WHERE telegram_id = ?
            """,
            (tg_user.username, tg_user.first_name, tg_user.id),
        )
        db.commit()
        return row["internal_id"]

    max_id = db.execute(
        "SELECT COALESCE(MAX(internal_id), 0) AS n FROM users"
    ).fetchone()["n"]

    internal_id = max_id + 1

    db.execute(
        """
        INSERT INTO users
        (telegram_id, internal_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            tg_user.id,
            internal_id,
            tg_user.username,
            tg_user.first_name,
            now_iso(),
        ),
    )
    db.commit()

    return internal_id


def get_user(tg_id):
    return db.execute(
        "SELECT * FROM users WHERE telegram_id = ?",
        (tg_id,),
    ).fetchone()


def get_product(product_id):
    return db.execute(
        """
        SELECT *
        FROM products
        WHERE id = ? AND active = 1
        """,
        (product_id,),
    ).fetchone()


def get_order(order_id):
    return db.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Магазин")],
            [
                KeyboardButton(text="🆘 Поддержка"),
                KeyboardButton(text="⭐ Отзывы"),
            ],
            [KeyboardButton(text="👤 Мой профиль")],
        ],
        resize_keyboard=True,
    )


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📦 Товары",
                callback_data="admin_products",
            )],
            [InlineKeyboardButton(
                text="➕ Добавить товар",
                callback_data="admin_add",
            )],
            [InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="admin_stats",
            )],
        ]
    )


# ============================================================
# FSM
# ============================================================

class AddProduct(StatesGroup):
    country = State()
    product_data = State()


class RUBProof(StatesGroup):
    waiting = State()


class Prem(StatesGroup):
    waiting_emoji = State()


# ============================================================
# START / PROFILE
# ============================================================

async def start_handler(message: Message):
    get_or_create_user(message.from_user)

    nickname = message.from_user.first_name or "пользователь"

    await message.answer(
        f"Привет, {nickname}!\n\n"
        "Здесь ты можешь купить аккаунты 👋",
        reply_markup=main_menu(),
    )


async def profile_handler(message: Message):
    get_or_create_user(message.from_user)
    user = get_user(message.from_user.id)

    await message.answer(
        "👤 Мой профиль\n\n"
        f"🆔 Внутренний ID: {user['internal_id']}\n"
        f"🛒 Всего покупок: {user['purchases_count']}\n"
        f"💰 Сумма покупок: {user['rub_spent']} ₽\n"
        f"⭐ Потрачено Stars: {user['stars_spent']} ⭐"
    )


# ============================================================
# SHOP
# ============================================================

async def shop_handler(message: Message):
    countries = db.execute(
        """
        SELECT
            country_code,
            country_name,
            flag,
            MIN(price_rub) AS price_rub,
            SUM(quantity) AS quantity
        FROM products
        WHERE active = 1 AND quantity > 0
        GROUP BY country_code, country_name, flag
        ORDER BY country_name
        """
    ).fetchall()

    if not countries:
        await message.answer("🛒 Сейчас товаров нет.")
        return

    builder = InlineKeyboardBuilder()

    for country in countries:
        builder.row(
            InlineKeyboardButton(
                text=(
                    f"{country['flag']} {country['country_name']} "
                    f"[{country['price_rub']} ₽]"
                ),
                callback_data=f"country:{country['country_code']}",
            )
        )

    await message.answer(
        "🌍 ВЫБЕРИТЕ СТРАНУ",
        reply_markup=builder.as_markup(),
    )


async def country_handler(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]

    rows = db.execute(
        """
        SELECT *
        FROM products
        WHERE country_code = ?
          AND active = 1
          AND quantity > 0
        ORDER BY price_rub ASC
        """,
        (code,),
    ).fetchall()

    if not rows:
        await callback.answer("Товар закончился.", show_alert=True)
        return

    # Show the cheapest available offer for the country.
    row = rows[0]

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🛒 Купить",
            callback_data=f"buy:{row['id']}",
        )
    )

    await callback.message.edit_text(
        f"🌍 Страна: {row['flag']} {row['country_name']}\n\n"
        f"💰 Цена: {row['price_rub']} ₽ / {row['price_stars']} ⭐\n"
        f"📦 В наличии: {row['quantity']}",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


async def buy_handler(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    product = get_product(product_id)

    if not product or product["quantity"] <= 0:
        await callback.answer("Товар закончился.", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="₽ Рубли",
            callback_data=f"payrub:{product_id}",
        ),
        InlineKeyboardButton(
            text="⭐ Stars",
            callback_data=f"paystars:{product_id}",
        ),
    )

    await callback.message.edit_text(
        "💳 Выберите тип оплаты:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ============================================================
# ORDER CREATION
# ============================================================

def create_order(user_id, product, payment_type):
    cursor = db.execute(
        """
        INSERT INTO orders
        (user_telegram_id, seller_id, product_id, payment_type,
         price_rub, price_stars, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            product["seller_id"],
            product["id"],
            payment_type,
            product["price_rub"],
            product["price_stars"],
            "waiting_proof" if payment_type == "rub" else "waiting_stars",
            now_iso(),
        ),
    )
    db.commit()
    return cursor.lastrowid


async def pay_rub_handler(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    product = get_product(product_id)

    if not product or product["quantity"] <= 0:
        await callback.answer("Товар закончился.", show_alert=True)
        return

    order_id = create_order(callback.from_user.id, product, "rub")

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📤 Отправить подтверждение",
            callback_data=f"proof:{order_id}",
        )
    )

    await callback.message.edit_text(
        "💳 Оплата заказа\n\n"
        f"💰 Сумма: {product['price_rub']} ₽\n\n"
        f"Реквизиты:\n{RUB_PAYMENT_DETAILS}\n\n"
        "После оплаты отправьте скриншот/чек.",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


async def pay_stars_handler(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    product = get_product(product_id)

    if not product or product["quantity"] <= 0:
        await callback.answer("Товар закончился.", show_alert=True)
        return

    order_id = create_order(callback.from_user.id, product, "stars")

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Отправить подтверждение",
            callback_data=f"starproof:{order_id}",
        )
    )

    await callback.message.edit_text(
        "⭐ Оплата Stars\n\n"
        f"Отправьте подарок на аккаунт {ADMIN_USERNAME}.\n\n"
        f"💰 Стоимость заказа: {product['price_stars']} ⭐\n\n"
        "Можно отправить несколько подарков.\n"
        "Бот посчитает их общую стоимость.\n"
        "После отправки нажмите кнопку ниже.",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ============================================================
# RUB PROOF
# ============================================================

async def proof_button_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    order_id = int(callback.data.split(":", 1)[1])
    order = get_order(order_id)

    if not order or order["user_telegram_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if order["status"] != "waiting_proof":
        await callback.answer("Этот заказ уже обработан.", show_alert=True)
        return

    await state.set_state(RUBProof.waiting)
    await state.update_data(order_id=order_id)

    await callback.message.answer(
        "📎 Отправьте сюда скриншот или файл чека."
    )
    await callback.answer()


async def proof_message_handler(
    message: Message,
    state: FSMContext,
):
    data = await state.get_data()
    order_id = data.get("order_id")

    if not order_id:
        return

    file_id = None
    proof_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        proof_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        proof_type = "document"
    else:
        await message.answer("Отправьте именно скриншот или файл чека.")
        return

    order = get_order(order_id)
    if not order or order["user_telegram_id"] != message.from_user.id:
        await state.clear()
        return

    db.execute(
        """
        UPDATE orders
        SET status = 'waiting_admin',
            proof_file_id = ?,
            proof_type = ?
        WHERE id = ?
        """,
        (file_id, proof_type, order_id),
    )
    db.commit()

    await message.answer(
        "⏳ Подтверждение отправлено.\n"
        "Ждите выдачи заказа."
    )

    await notify_seller_about_order(
        await message.bot,
        order_id,
    )

    await state.clear()


async def notify_seller_about_order(bot: Bot, order_id: int):
    order = db.execute(
        """
        SELECT
            o.*,
            p.country_name,
            p.flag,
            p.item_name
        FROM orders o
        JOIN products p ON p.id = o.product_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()

    if not order:
        return

    user = get_user(order["user_telegram_id"])

    text = (
        "🔔 НОВАЯ ЗАЯВКА НА ОПЛАТУ\n\n"
        f"🆔 Заказ: #{order['id']}\n"
        f"👤 Покупатель: "
        f"@{user['username'] or 'без_username'}\n"
        f"🆔 Telegram ID: {user['telegram_id']}\n"
        f"🔢 Внутренний ID: {user['internal_id']}\n\n"
        f"🌍 Страна: {order['flag']} {order['country_name']}\n"
        f"💰 Сумма: {order['price_rub']} ₽\n"
        "💳 Способ: RUB"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Оплата подтверждена",
            callback_data=f"approve:{order_id}",
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"reject:{order_id}",
        ),
    )

    if order["proof_type"] == "photo":
        await bot.send_photo(
            ADMIN_ID,
            order["proof_file_id"],
            caption=text,
            reply_markup=builder.as_markup(),
        )
    else:
        await bot.send_document(
            ADMIN_ID,
            order["proof_file_id"],
            caption=text,
            reply_markup=builder.as_markup(),
        )


# ============================================================
# STARS
# ============================================================

async def stars_proof_handler(callback: CallbackQuery):
    order_id = int(callback.data.split(":", 1)[1])
    order = get_order(order_id)

    if not order or order["user_telegram_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if order["status"] not in ("waiting_stars", "waiting_stars_check"):
        await callback.answer("Этот заказ уже обработан.", show_alert=True)
        return

    if stars_client is None or not stars_client.is_connected():
        await callback.answer(
            "Проверка Stars временно недоступна.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        "🔎 Проверяем оплату Stars...\n\n"
        "Учитываются только подарки, отправленные "
        "после создания этого заказа."
    )

    approved, total, used_ids = await check_stars_order(order_id)

    if approved:
        await approve_stars_order(
            callback.bot,
            order_id,
            total,
            used_ids,
        )
        await callback.message.edit_text(
            "✅ Оплата подтверждена!\n\n"
            "⏳ Ждите выдачи заказа."
        )
    else:
        db.execute(
            """
            UPDATE orders
            SET status = 'waiting_stars_check',
                stars_total = ?
            WHERE id = ?
            """,
            (total, order_id),
        )
        db.commit()

        await callback.message.edit_text(
            "❌ Оплата пока не найдена или её недостаточно.\n\n"
            f"Найдено: {total} ⭐\n"
            f"Требуется: {order['price_stars']} ⭐\n\n"
            "Отправьте оставшуюся сумму и нажмите кнопку "
            "«Отправить подтверждение» снова."
        )

    await callback.answer()


async def check_stars_order(order_id: int):
    """
    Read recent incoming Star Gifts from the personal account.

    Telegram's current Gift API exposes received gifts with:
      - sender (from_id)
      - reception date
      - StarGift object, including its Stars price.

    We only count:
      - gifts from the buyer's Telegram ID;
      - gifts received after order.created_at;
      - gifts not already consumed by another order;
      - non-refunded/non-converted gifts.

    The order is paid when sum(gift.stars) >= order.price_stars.
    """
    if stars_client is None:
        return False, 0, []

    order = get_order(order_id)
    if not order:
        return False, 0, []

    buyer_id = order["user_telegram_id"]
    seller_id = order["seller_id"]
    created_at = dt_from_iso(order["created_at"])

    # Current single-admin implementation: the MTProto account is
    # the receiving Stars account for the seller.
    if seller_id != ADMIN_ID:
        return False, 0, []

    used_rows = db.execute(
        "SELECT payment_identifier FROM used_star_payments"
    ).fetchall()
    globally_used = {row["payment_identifier"] for row in used_rows}

    # Fetch received/saved/unsaved gifts of the currently authorized user.
    # Telegram documents getSavedStarGifts as the method for fetching
    # the full list of owned/received gifts.
    result = await stars_client(
        tl_functions.payments.GetSavedStarGiftsRequest(
            peer=await stars_client.get_input_entity("me"),
            offset="",
            limit=STARS_SCAN_LIMIT,
            exclude_unsaved=False,
            exclude_saved=False,
            exclude_unique=False,
            exclude_upgradable=False,
            exclude_unupgradable=False,
            exclude_hosted=True,
        )
    )

    total = 0
    candidates = []

    for saved in getattr(result, "gifts", []) or []:
        identifier = make_gift_identifier(saved)

        if not identifier or identifier in globally_used:
            continue

        gift_date = getattr(saved, "date", None)
        if gift_date is None:
            continue

        if gift_date.tzinfo is None:
            gift_date = gift_date.replace(tzinfo=timezone.utc)

        if gift_date < created_at:
            continue

        from_id = getattr(saved, "from_id", None)
        sender_id = peer_id_value(from_id)

        if sender_id != buyer_id:
            continue

        gift = getattr(saved, "gift", None)
        if gift is None:
            continue

        # We intentionally use the original Gift price (gift.stars),
        # not convert_stars, because the shop price is based on the
        # purchase price of the Gift.
        gift_price = int(getattr(gift, "stars", 0) or 0)
        if gift_price <= 0:
            continue

        # Do not count gifts already converted/refunded.
        if getattr(saved, "refunded", False):
            continue

        if getattr(saved, "convert_stars", None) is not None:
            # Presence alone is normal for convertible gifts and is NOT
            # a reason to reject. The original gift price remains used.
            pass

        candidates.append(
            (identifier, gift_price, gift_date)
        )

    # Oldest first gives deterministic allocation when several orders
    # are waiting for the same buyer.
    candidates.sort(key=lambda x: x[2])

    for identifier, amount, gift_date in candidates:
        total += amount
        if total >= order["price_stars"]:
            used = [
                item[0]
                for item in candidates
                if item[2] <= gift_date
            ]
            # Include only gifts actually needed up to this point.
            used = []
            running = 0
            for item in candidates:
                used.append(item[0])
                running += item[1]
                if running >= order["price_stars"]:
                    break

            return True, running, used

    return False, total, [item[0] for item in candidates]


def make_gift_identifier(saved):
    """
    Stable identifier for a received gift.

    For user gifts Telegram exposes msg_id. We combine it with the
    gift date so the same gift cannot be consumed twice.
    """
    msg_id = getattr(saved, "msg_id", None)
    gift_date = getattr(saved, "date", None)

    if msg_id is not None:
        return f"gift:{msg_id}:{gift_date.timestamp() if gift_date else 0}"

    saved_id = getattr(saved, "saved_id", None)
    if saved_id is not None:
        return f"saved:{saved_id}"

    return None


def peer_id_value(peer):
    if peer is None:
        return None

    # Telethon PeerUser has .user_id.
    user_id = getattr(peer, "user_id", None)
    if user_id is not None:
        return int(user_id)

    # Some returned Peer objects may expose .channel_id/.chat_id.
    channel_id = getattr(peer, "channel_id", None)
    if channel_id is not None:
        return int(channel_id)

    chat_id = getattr(peer, "chat_id", None)
    if chat_id is not None:
        return int(chat_id)

    return None


async def approve_stars_order(bot: Bot, order_id: int, total: int, used_ids):
    order = get_order(order_id)
    if not order:
        return

    if order["status"] == "paid":
        return

    # Atomically claim the order so a real-time Gift event and the
    # periodic scanner cannot approve the same order twice.
    claim = db.execute(
        """
        UPDATE orders
        SET status = 'paid',
            stars_total = ?
        WHERE id = ?
          AND status IN ('waiting_stars', 'waiting_stars_check')
        """,
        (total, order_id),
    )
    if claim.rowcount != 1:
        return

    # Re-check product availability.
    product = get_product(order["product_id"])
    if not product or product["quantity"] <= 0:
        await bot.send_message(
            order["user_telegram_id"],
            "⚠️ Оплата найдена, но товар уже закончился.\n"
            "Свяжитесь с поддержкой.",
        )
        return

    # Determine exact allocated amounts from the current gifts.
    allocated = await get_allocated_gifts(order_id, set(used_ids))

    for identifier, amount, gift_date in allocated:
        try:
            db.execute(
                """
                INSERT INTO used_star_payments
                (order_id, payment_identifier, amount_stars, gift_date, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    identifier,
                    amount,
                    gift_date.isoformat(),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            # Another order already consumed this gift.
            continue

    db.execute(
        """
        UPDATE products
        SET quantity = quantity - 1
        WHERE id = ? AND quantity > 0
        """,
        (order["product_id"],),
    )

    db.execute(
        """
        UPDATE users
        SET purchases_count = purchases_count + 1,
            stars_spent = stars_spent + ?
        WHERE telegram_id = ?
        """,
        (order["price_stars"], order["user_telegram_id"]),
    )

    db.commit()

    await bot.send_message(
        order["user_telegram_id"],
        "✅ Оплата подтверждена!\n\n"
        "⏳ Ждите выдачи заказа."
    )

    await bot.send_message(
        order["seller_id"],
        "⭐ Оплата Stars подтверждена автоматически.\n\n"
        f"🆔 Заказ: #{order_id}\n"
        f"👤 Покупатель ID: {order['user_telegram_id']}\n"
        f"⭐ Получено: {total} ⭐\n"
        f"⭐ Требовалось: {order['price_stars']} ⭐\n\n"
        "⏳ Выдайте заказ покупателю.",
    )


async def get_allocated_gifts(order_id: int, wanted_ids: set):
    order = get_order(order_id)
    if not order or stars_client is None:
        return []

    result = await stars_client(
        tl_functions.payments.GetSavedStarGiftsRequest(
            peer=await stars_client.get_input_entity("me"),
            offset="",
            limit=STARS_SCAN_LIMIT,
            exclude_unsaved=False,
            exclude_saved=False,
            exclude_unique=False,
            exclude_upgradable=False,
            exclude_unupgradable=False,
            exclude_hosted=True,
        )
    )

    result_rows = []

    for saved in getattr(result, "gifts", []) or []:
        identifier = make_gift_identifier(saved)
        if identifier not in wanted_ids:
            continue

        gift = getattr(saved, "gift", None)
        if gift is None:
            continue

        amount = int(getattr(gift, "stars", 0) or 0)
        gift_date = getattr(saved, "date", None)

        if gift_date is None:
            continue

        if gift_date.tzinfo is None:
            gift_date = gift_date.replace(tzinfo=timezone.utc)

        result_rows.append((identifier, amount, gift_date))

    return result_rows


async def auto_check_all_waiting_stars(bot: Bot):
    while True:
        try:
            if stars_client is not None and stars_client.is_connected():
                rows = db.execute(
                    """
                    SELECT id
                    FROM orders
                    WHERE payment_type = 'stars'
                      AND status IN ('waiting_stars', 'waiting_stars_check')
                    ORDER BY id ASC
                    """
                ).fetchall()

                for row in rows:
                    approved, total, used_ids = await check_stars_order(row["id"])

                    if approved:
                        await approve_stars_order(
                            bot,
                            row["id"],
                            total,
                            used_ids,
                        )
                    else:
                        db.execute(
                            """
                            UPDATE orders
                            SET stars_total = ?
                            WHERE id = ?
                              AND status IN ('waiting_stars', 'waiting_stars_check')
                            """,
                            (total, row["id"]),
                        )
                        db.commit()

        except Exception:
            logging.exception("Automatic Stars scan failed.")

        await asyncio.sleep(5)


async def handle_incoming_gift(event):
    """
    Real-time path: when the personal account receives a Gift,
    immediately try to match it to the oldest waiting order from
    that sender.

    Telegram emits a messageService containing messageActionStarGift
    when a user receives a Gift.
    """
    try:
        action = getattr(event.message, "action", None)

        if not isinstance(action, tl_types.MessageActionStarGift):
            return

        sender = getattr(action, "from_id", None)
        sender_id = peer_id_value(sender)

        if sender_id is None:
            # Anonymous gift: cannot safely attribute it to a buyer.
            return

        gift = getattr(action, "gift", None)
        if gift is None:
            return

        amount = int(getattr(gift, "stars", 0) or 0)
        if amount <= 0:
            return

        rows = db.execute(
            """
            SELECT id
            FROM orders
            WHERE payment_type = 'stars'
              AND status IN ('waiting_stars', 'waiting_stars_check')
              AND user_telegram_id = ?
            ORDER BY id ASC
            """,
            (sender_id,),
        ).fetchall()

        for row in rows:
            approved, total, used_ids = await check_stars_order(row["id"])

            if approved:
                await approve_stars_order(
                    bot,
                    row["id"],
                    total,
                    used_ids,
                )
                break

    except Exception:
        logging.exception("Incoming Gift handler failed.")


# ============================================================
# RUB / ORDER APPROVAL
# ============================================================

async def approve_handler(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split(":", 1)[1])
    order = get_order(order_id)

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if order["seller_id"] != callback.from_user.id:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    if order["status"] != "waiting_admin":
        await callback.answer(
            "Этот заказ уже обработан.",
            show_alert=True,
        )
        return

    product = get_product(order["product_id"])

    if not product or product["quantity"] <= 0:
        await callback.answer("Товар закончился.", show_alert=True)
        return

    db.execute(
        """
        UPDATE orders
        SET status = 'paid'
        WHERE id = ?
        """,
        (order_id,),
    )

    db.execute(
        """
        UPDATE products
        SET quantity = quantity - 1
        WHERE id = ? AND quantity > 0
        """,
        (order["product_id"],),
    )

    db.execute(
        """
        UPDATE users
        SET purchases_count = purchases_count + 1,
            rub_spent = rub_spent + ?
        WHERE telegram_id = ?
        """,
        (order["price_rub"], order["user_telegram_id"]),
    )

    db.commit()

    await callback.message.edit_reply_markup(reply_markup=None)

    await callback.bot.send_message(
        order["user_telegram_id"],
        "✅ Оплата подтверждена!\n\n"
        "⏳ Ждите выдачи заказа."
    )

    await callback.answer("Оплата подтверждена.")


async def reject_handler(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    order_id = int(callback.data.split(":", 1)[1])
    order = get_order(order_id)

    if not order or order["seller_id"] != callback.from_user.id:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    if order["status"] in ("paid", "rejected"):
        await callback.answer(
            "Этот заказ уже обработан.",
            show_alert=True,
        )
        return

    db.execute(
        "UPDATE orders SET status = 'rejected' WHERE id = ?",
        (order_id,),
    )
    db.commit()

    await callback.message.edit_reply_markup(reply_markup=None)

    await callback.bot.send_message(
        order["user_telegram_id"],
        "❌ Оплата не подтверждена.\n\n"
        "Обратитесь в поддержку, если считаете, что произошла ошибка."
    )

    await callback.answer("Заказ отклонён.")


# ============================================================
# ADMIN PANEL
# ============================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "👑 АДМИН-ПАНЕЛЬ",
        reply_markup=admin_menu(),
    )


async def admin_products_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    rows = db.execute(
        """
        SELECT *
        FROM products
        WHERE seller_id = ?
        ORDER BY id DESC
        """,
        (ADMIN_ID,),
    ).fetchall()

    if not rows:
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="➕ Добавить товар",
                callback_data="admin_add",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="admin_back",
            )
        )

        await callback.message.edit_text(
            "📦 У вас пока нет товаров.",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    text = "📦 МОИ ТОВАРЫ\n\n"

    for p in rows:
        text += (
            f"#{p['id']} {p['flag']} {p['country_name']}\n"
            f"{p['item_name']} — {p['price_rub']} ₽ / "
            f"{p['price_stars']} ⭐\n"
            f"📦 В наличии: {p['quantity']}\n\n"
        )

        builder.row(
            InlineKeyboardButton(
                text=f"🗑 Удалить #{p['id']}",
                callback_data=f"delete_product:{p['id']}",
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить товар",
            callback_data="admin_add",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="admin_back",
        )
    )

    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


async def admin_stats_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    users = db.execute(
        "SELECT COUNT(*) AS n FROM users"
    ).fetchone()["n"]

    sold = db.execute(
        """
        SELECT COUNT(*) AS n
        FROM orders
        WHERE seller_id = ? AND status = 'paid'
        """,
        (ADMIN_ID,),
    ).fetchone()["n"]

    rub = db.execute(
        """
        SELECT COALESCE(SUM(price_rub), 0) AS n
        FROM orders
        WHERE seller_id = ?
          AND status = 'paid'
          AND payment_type = 'rub'
        """,
        (ADMIN_ID,),
    ).fetchone()["n"]

    stars = db.execute(
        """
        SELECT COALESCE(SUM(price_stars), 0) AS n
        FROM orders
        WHERE seller_id = ?
          AND status = 'paid'
          AND payment_type = 'stars'
        """,
        (ADMIN_ID,),
    ).fetchone()["n"]

    await callback.message.edit_text(
        "📊 СТАТИСТИКА\n\n"
        f"👥 Пользователей: {users}\n\n"
        "👤 ВАША СТАТИСТИКА\n"
        f"📦 Продано аккаунтов: {sold}\n"
        f"💰 Выручка: {rub} ₽\n"
        f"⭐ Stars: {stars} ⭐",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="admin_back",
                )]
            ]
        ),
    )
    await callback.answer()


async def admin_back_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await callback.message.edit_text(
        "👑 АДМИН-ПАНЕЛЬ",
        reply_markup=admin_menu(),
    )
    await callback.answer()


async def admin_add_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(AddProduct.country)

    await callback.message.answer(
        "🌍 Отправьте страну в формате:\n\n"
        "🇺🇸 США\n\n"
        "После этого бот попросит данные товара."
    )
    await callback.answer()


async def admin_country_handler(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    if await state.get_state() != AddProduct.country.state:
        return

    value = message.text.strip()
    parts = value.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer("Пример: 🇺🇸 США")
        return

    flag, country_name = parts

    await state.update_data(
        flag=flag,
        country_name=country_name,
        country_code=country_name.lower(),
    )
    await state.set_state(AddProduct.product_data)

    await message.answer(
        "Теперь отправьте товар в формате:\n\n"
        "+1 50₽/60⭐ 3 шт\n\n"
        "Где:\n"
        "+1 — название/тип товара\n"
        "50 — цена в ₽\n"
        "60 — цена в Stars\n"
        "3 — количество"
    )


PRODUCT_RE = re.compile(
    r"^(.+?)\s+(\d+)\s*₽\s*/\s*(\d+)\s*"
    r"(?:⭐|звезд|звёзд)\s+(\d+)\s*(?:шт|штук)?$",
    re.IGNORECASE,
)


async def admin_product_data_handler(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    if await state.get_state() != AddProduct.product_data.state:
        return

    match = PRODUCT_RE.match(message.text.strip())

    if not match:
        await message.answer(
            "Не удалось распознать товар.\n\n"
            "Используйте:\n"
            "+1 50₽/60⭐ 3 шт"
        )
        return

    item_name = match.group(1).strip()
    price_rub = int(match.group(2))
    price_stars = int(match.group(3))
    quantity = int(match.group(4))

    if min(price_rub, price_stars, quantity) <= 0:
        await message.answer(
            "Цена и количество должны быть больше нуля."
        )
        return

    data = await state.get_data()

    # If an identical active product exists for this admin and country,
    # increase stock instead of creating a duplicate store card.
    existing = db.execute(
        """
        SELECT id
        FROM products
        WHERE seller_id = ?
          AND country_code = ?
          AND item_name = ?
          AND price_rub = ?
          AND price_stars = ?
          AND active = 1
        LIMIT 1
        """,
        (
            ADMIN_ID,
            data["country_code"],
            item_name,
            price_rub,
            price_stars,
        ),
    ).fetchone()

    if existing:
        db.execute(
            """
            UPDATE products
            SET quantity = quantity + ?
            WHERE id = ?
            """,
            (quantity, existing["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO products
            (seller_id, country_code, country_name, flag, item_name,
             price_rub, price_stars, quantity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ADMIN_ID,
                data["country_code"],
                data["country_name"],
                data["flag"],
                item_name,
                price_rub,
                price_stars,
                quantity,
                now_iso(),
            ),
        )

    db.commit()
    await state.clear()

    await message.answer(
        "✅ Товар добавлен.\n\n"
        f"{data['flag']} {data['country_name']}\n"
        f"{item_name}\n"
        f"💰 {price_rub} ₽ / {price_stars} ⭐\n"
        f"📦 {quantity} шт"
    )


async def delete_product_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    product_id = int(callback.data.split(":", 1)[1])
    product = db.execute(
        "SELECT * FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()

    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    if product["seller_id"] != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    db.execute(
        "UPDATE products SET active = 0 WHERE id = ?",
        (product_id,),
    )
    db.commit()

    await callback.answer("Товар удалён.")
    await admin_products_handler(callback)


# ============================================================
# /prem
# ============================================================

async def prem_command(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    await state.set_state(Prem.waiting_emoji)

    await message.answer(
        "⭐ Отправьте Premium Emoji отдельным сообщением.\n\n"
        "Я верну его custom_emoji_id."
    )


async def prem_emoji_handler(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    if await state.get_state() != Prem.waiting_emoji.state:
        return

    custom_id = None

    for entity in message.entities or []:
        if entity.type == "custom_emoji" and entity.custom_emoji_id:
            custom_id = entity.custom_emoji_id
            break

    if not custom_id:
        await message.answer(
            "Не найден Premium Emoji.\n"
            "Отправьте именно Premium Emoji отдельным сообщением."
        )
        return

    await message.answer(
        f"ID Premium Emoji:\n<code>{custom_id}</code>",
        parse_mode="HTML",
    )
    await state.clear()


# ============================================================
# SUPPORT / REVIEWS
# ============================================================

async def support_handler(message: Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="💬 Написать в поддержку",
            url="https://t.me/fegote",
        )
    )

    await message.answer(
        "🆘 Поддержка\n\n"
        "Если у вас возник вопрос по заказу, "
        "напишите в поддержку.",
        reply_markup=builder.as_markup(),
    )


async def reviews_handler(message: Message):
    await message.answer(
        "⭐ Отзывы\n\n"
        "Раздел пока пуст."
    )


# ============================================================
# MTProto AUTH
# ============================================================

def normalize_login_code(value: str) -> str:
    # Supports:
    # 8.7.6.5.4
    # 8-7-6-5-4
    # 8 7 6 5 4
    # and also preserves letters, e.g. p.a.2.k -> pa2k.
    return re.sub(r"[\s.\-_:]+", "", value.strip())


async def authorize_stars_account():
    client = TelegramClient(
        MTproto_SESSION,
        API_ID,
        API_HASH,
    )

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        logging.info(
            "MTProto session authorized as %s (%s).",
            getattr(me, "username", None),
            me.id,
        )
        return client

    print("\n=== Telegram Stars/Gifts authorization ===")
    print(f"Account: {MTproto_PHONE}")

    await client.send_code_request(MTproto_PHONE)

    while True:
        raw_code = input(
            "Введите код Telegram "
            "(например 8.7.6.5.4): "
        ).strip()

        code_value = normalize_login_code(raw_code)

        if not code_value:
            print("Код пустой.")
            continue

        try:
            await client.sign_in(
                phone=MTproto_PHONE,
                code=code_value,
            )
            break

        except SessionPasswordNeededError:
            password = input(
                "Введите пароль двухэтапной проверки Telegram: "
            )
            await client.sign_in(password=password)
            break

        except Exception as exc:
            print(f"Ошибка входа: {exc}")
            print("Попробуйте снова. Если код уже истёк, "
                  "перезапустите авторизацию.")

    me = await client.get_me()

    logging.info(
        "MTProto authorization complete: @%s (%s).",
        getattr(me, "username", None),
        me.id,
    )

    return client


# ============================================================
# STARTUP
# ============================================================

stars_client = None
bot = None


async def main():
    global stars_client, bot

    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Заполните BOT_TOKEN."
        )

    if API_ID == 12345678 or API_HASH == "PASTE_YOUR_API_HASH_HERE":
        raise RuntimeError(
            "Заполните API_ID и API_HASH из my.telegram.org."
        )

    db_init()

    stars_client = await authorize_stars_account()

    # Import here so the file fails clearly if Telethon is broken.
    from telethon import functions as tl_functions

    # Expose the module globally for the checker functions.
    globals()["tl_functions"] = tl_functions

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    # --------------------------------------------------------
    # Bot commands
    # --------------------------------------------------------
    dp.message.register(start_handler, CommandStart())
    dp.message.register(admin_command, Command("admin"))
    dp.message.register(prem_command, Command("prem"))

    # --------------------------------------------------------
    # Main menu
    # --------------------------------------------------------
    dp.message.register(
        shop_handler,
        F.text == "🛒 Магазин",
    )
    dp.message.register(
        support_handler,
        F.text == "🆘 Поддержка",
    )
    dp.message.register(
        reviews_handler,
        F.text == "⭐ Отзывы",
    )
    dp.message.register(
        profile_handler,
        F.text == "👤 Мой профиль",
    )

    # --------------------------------------------------------
    # User callbacks
    # --------------------------------------------------------
    dp.callback_query.register(
        country_handler,
        F.data.startswith("country:"),
    )
    dp.callback_query.register(
        buy_handler,
        F.data.startswith("buy:"),
    )
    dp.callback_query.register(
        pay_rub_handler,
        F.data.startswith("payrub:"),
    )
    dp.callback_query.register(
        pay_stars_handler,
        F.data.startswith("paystars:"),
    )
    dp.callback_query.register(
        proof_button_handler,
        F.data.startswith("proof:"),
    )
    dp.callback_query.register(
        stars_proof_handler,
        F.data.startswith("starproof:"),
    )

    # --------------------------------------------------------
    # FSM messages
    # --------------------------------------------------------
    dp.message.register(
        proof_message_handler,
        RUBProof.waiting,
    )
    dp.message.register(
        prem_emoji_handler,
        Prem.waiting_emoji,
    )
    dp.message.register(
        admin_country_handler,
        AddProduct.country,
    )
    dp.message.register(
        admin_product_data_handler,
        AddProduct.product_data,
    )

    # --------------------------------------------------------
    # Admin order callbacks
    # --------------------------------------------------------
    dp.callback_query.register(
        approve_handler,
        F.data.startswith("approve:"),
    )
    dp.callback_query.register(
        reject_handler,
        F.data.startswith("reject:"),
    )

    # --------------------------------------------------------
    # Admin panel
    # --------------------------------------------------------
    dp.callback_query.register(
        admin_products_handler,
        F.data == "admin_products",
    )
    dp.callback_query.register(
        admin_add_handler,
        F.data == "admin_add",
    )
    dp.callback_query.register(
        admin_stats_handler,
        F.data == "admin_stats",
    )
    dp.callback_query.register(
        admin_back_handler,
        F.data == "admin_back",
    )
    dp.callback_query.register(
        delete_product_handler,
        F.data.startswith("delete_product:"),
    )

    # --------------------------------------------------------
    # Real-time incoming Gift listener
    # --------------------------------------------------------
    @stars_client.on(events.NewMessage(incoming=True))
    async def incoming_gift_event(event):
        await handle_incoming_gift(event)

    await bot.delete_webhook(drop_pending_updates=True)

    logging.info("Bot started.")

    # Run the background scan and Bot API polling together.
    scan_task = asyncio.create_task(
        auto_check_all_waiting_stars(bot)
    )

    try:
        await dp.start_polling(bot)
    finally:
        scan_task.cancel()

        try:
            await scan_task
        except asyncio.CancelledError:
            pass

        if stars_client is not None:
            await stars_client.disconnect()

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
