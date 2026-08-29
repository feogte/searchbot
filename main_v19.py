# NoveraSearch v19
"""
NoveraSearch — Telegram username finder bot
================================================

Target host: bothost.ru

Python:
    3.10+

Install (if your host does not auto-install dependencies):
    pip install -r requirements.txt

Fill only these values before launch:
    BOT_TOKEN = "8205193243:AAG9fjSnrP3wSvxDpHepVcW0ya-h0jRQk50"
    API_ID = 31799721
    API_HASH = "eb2181220b3b8a0b6a7f93cd8075a559"
    PHONE = "+77024728757"
    OWNER_ID = 8872934046

The rest of the bot is already implemented.

IMPORTANT ABOUT TELEGRAM CHECKING
---------------------------------
The Bot API cannot perform the same username-availability check as a
Telegram user account. Therefore Telethon is used with a USER account.
On the first launch Telethon will request the login code and, if enabled,
the 2FA password.

IMPORTANT ABOUT FRAGMENT
------------------------
Fragment does not provide a simple permanent public "is available" API.
The function check_fragment_username() checks the public Fragment page and
accepts only explicit availability signals. If Fragment changes its page,
only that function needs to be updated.

PAYMENTS
--------
Payments are manual:
1. User chooses a subscription period.
2. User chooses Stars or RUB.
3. Bot shows the payment instructions.
4. User presses "Отправить подтверждение".
5. Bot asks for a payment screenshot.
6. Owner receives the screenshot + username + Telegram ID + selected plan.
7. Owner can approve or reject.
8. On approval premium is activated.

The bot does NOT automatically mark a payment as paid.
"""

import asyncio
import logging
import random
import re
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramBadRequest

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

# ============================================================
# CONFIG — INTENTIONALLY EMPTY
# ============================================================

BOT_TOKEN = "8205193243:AAG9fjSnrP3wSvxDpHepVcW0ya-h0jRQk50"
API_ID = 31799721
API_HASH = "eb2181220b3b8a0b6a7f93cd8075a559"
PHONE = "+77024728757"

# Telegram ID of the owner who receives premium applications.
OWNER_ID = 8872934046

# Premium payment recipient.
STARS_ACCOUNT = "@fegote"

# RUB payment details.
TBANK_PHONE = "+79313716777"
TBANK_BANK = "Т-Банк"
TBANK_RECIPIENT = "Тимур/Наталья"

# Runtime data is created automatically on first launch.
# Only main.py and requirements.txt are required in the project.
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Telethon session file.
SESSION_NAME = str(DATA_DIR / "noverasearch_user")

# SQLite database.
DB_PATH = DATA_DIR / "noverasearch.db"

# How many available usernames the bot tries to return per request.
RESULTS_PER_SEARCH = 10

# ============================================================
# PREMIUM PLANS
# ============================================================

PLANS = {
    "1d": {
        "title": "1 день",
        "days": 1,
        "stars": 15,
        "rub": 20,
    },
    "5d": {
        "title": "5 дней",
        "days": 5,
        "stars": 50,
        "rub": 60,
    },
    "7d": {
        "title": "7 дней",
        "days": 7,
        "stars": 65,
        "rub": 70,
    },
    "30d": {
        "title": "Месяц",
        "days": 30,
        "stars": 150,
        "rub": 170,
    },
}

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("noverasearch")

logger.info("NoveraSearch v19 Python process started.")

# ============================================================
# GLOBALS
# ============================================================

bot: Bot | None = None
dp = Dispatcher()
tg_client: TelegramClient | None = None

# In-memory temporary state for payment confirmations.
# It is intentionally small; actual user/premium data lives in SQLite.
payment_state: dict[int, dict] = {}

# Protect Telethon from simultaneous username checks.
telegram_check_lock = asyncio.Lock()


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            premium_until TEXT,
            found_count INTEGER NOT NULL DEFAULT 0,
            daily_found_count INTEGER NOT NULL DEFAULT 0,
            daily_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS premium_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            plan_code TEXT NOT NULL,
            payment_type TEXT NOT NULL,
            screenshot_file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT,
            processed_by INTEGER
        )
    """)

    conn.commit()
    conn.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def get_user(user_id: int):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def ensure_user(user: object):
    user_id = user.id
    now = iso_now()

    conn = db()
    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                now,
                user_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO users
            (user_id, username, first_name, premium_until,
             found_count, daily_found_count, daily_date,
             created_at, updated_at)
            VALUES (?, ?, ?, NULL, 0, 0, ?, ?, ?)
            """,
            (
                user_id,
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                utc_now().date().isoformat(),
                now,
                now,
            ),
        )

    conn.commit()
    conn.close()


def get_premium_until(user_id: int) -> datetime | None:
    row = get_user(user_id)
    if not row or not row["premium_until"]:
        return None

    try:
        dt = datetime.fromisoformat(row["premium_until"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_premium(user_id: int) -> bool:
    until = get_premium_until(user_id)
    return bool(until and until > utc_now())


def activate_premium(user_id: int, days: int):
    ensure_user(
        type(
            "UserObj",
            (),
            {"id": user_id, "username": None, "first_name": None},
        )()
    )

    current = get_premium_until(user_id)
    start = current if current and current > utc_now() else utc_now()
    new_until = start + timedelta(days=days)

    conn = db()
    conn.execute(
        """
        UPDATE users
        SET premium_until = ?, updated_at = ?
        WHERE user_id = ?
        """,
        (new_until.isoformat(), iso_now(), user_id),
    )
    conn.commit()
    conn.close()

    return new_until


def reset_daily_counter_if_needed(user_id: int):
    today = utc_now().date().isoformat()
    conn = db()
    row = conn.execute(
        "SELECT daily_date FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if row and row["daily_date"] != today:
        conn.execute(
            """
            UPDATE users
            SET daily_found_count = 0, daily_date = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (today, iso_now(), user_id),
        )
        conn.commit()

    conn.close()


def get_daily_found_count(user_id: int) -> int:
    reset_daily_counter_if_needed(user_id)
    row = get_user(user_id)
    return int(row["daily_found_count"]) if row else 0


def increment_found_count(user_id: int):
    reset_daily_counter_if_needed(user_id)

    conn = db()
    conn.execute(
        """
        UPDATE users
        SET found_count = found_count + 1,
            daily_found_count = daily_found_count + 1,
            updated_at = ?
        WHERE user_id = ?
        """,
        (iso_now(), user_id),
    )
    conn.commit()
    conn.close()


def get_found_count(user_id: int) -> int:
    row = get_user(user_id)
    return int(row["found_count"]) if row else 0


# ============================================================
# RANKS
# ============================================================

def get_rank(found_count: int) -> tuple[int, str]:
    if found_count < 50:
        return 1, "Разведчик"
    if found_count < 120:
        return 2, "Коллекционер"
    if found_count < 222:
        return 3, "Скаут"
    if found_count < 700:
        return 4, "Охотник"
    return 5, "Novera Prime"


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Искать юзернейм",
                    callback_data="search",
                ),
                InlineKeyboardButton(
                    text="💎 Премиум",
                    callback_data="premium",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👤 Профиль",
                    callback_data="profile",
                ),
            ],
        ]
    )


def search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="5 Букв",
                    callback_data="search_5",
                ),
                InlineKeyboardButton(
                    text="6 Букв",
                    callback_data="search_6",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="back",
                )
            ],
        ]
    )


def premium_plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1 день — 15 ⭐ / 20 ₽",
                    callback_data="plan_1d",
                )
            ],
            [
                InlineKeyboardButton(
                    text="5 дней — 50 ⭐ / 60 ₽",
                    callback_data="plan_5d",
                )
            ],
            [
                InlineKeyboardButton(
                    text="7 дней — 65 ⭐ / 70 ₽",
                    callback_data="plan_7d",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Месяц — 150 ⭐ / 170 ₽",
                    callback_data="plan_30d",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="back",
                )
            ],
        ]
    )


def payment_type_keyboard(plan_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Звезды",
                    callback_data=f"pay_stars:{plan_code}",
                ),
                InlineKeyboardButton(
                    text="₽ Рубли",
                    callback_data=f"pay_rub:{plan_code}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="premium",
                )
            ],
        ]
    )


def confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📸 Отправить подтверждение",
                    callback_data="payment_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="premium",
                )
            ],
        ]
    )


def owner_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{request_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{request_id}",
                ),
            ]
        ]
    )


# ============================================================
# TEXT HELPERS
# ============================================================

def start_text() -> str:
    return (
        "Добро пожаловать в NoveraSearch\n"
        "здесь вы можете найти свободные юзернеймы."
    )


def premium_text() -> str:
    return (
        "💎 <b>Премиум NoveraSearch</b>\n\n"
        "Премиум открывает поиск 5-значных юзернеймов "
        "и снимает дневной лимит поиска.\n\n"
        "Выберите срок подписки:"
    )


def search_text() -> str:
    return (
        "🔎 <b>Поиск юзернеймов</b>\n\n"
        "Выберите длину юзернейма:\n\n"
        "6 букв — доступно всем.\n"
        "5 букв — только для Premium."
    )


# ============================================================
# BOT-DRIVEN TELETHON AUTH
# ============================================================

auth_state: dict[str, object] = {
    "waiting_for_code": False,
    "waiting_for_password": False,
    "phone_code_hash": None,
}


def normalize_login_code(value: str) -> str:
    """Accept 1.2.3.4.6 and convert it to 12346."""
    value = value.strip()
    if re.fullmatch(r"\d(?:\.\d){4}", value):
        return value.replace(".", "")
    if re.fullmatch(r"\d{5}", value):
        return value
    raise ValueError("Введите код в формате 1.2.3.4.6")


async def authorize_telethon_from_bot() -> str:
    """Send the Telegram login code. The actual code is received in /auth_code."""
    global tg_client

    if not API_ID or not API_HASH or not PHONE:
        return "Не заполнены API_ID, API_HASH или PHONE."

    if tg_client is None:
        tg_client = TelegramClient(
            SESSION_NAME,
            API_ID,
            API_HASH,
        )

    await tg_client.connect()

    if await tg_client.is_user_authorized():
        return "already_authorized"

    sent = await tg_client.send_code_request(PHONE)
    auth_state["waiting_for_code"] = True
    auth_state["waiting_for_password"] = False
    auth_state["phone_code_hash"] = sent.phone_code_hash

    return "code_sent"


# ============================================================
# TELEGRAM USERNAME CHECK
# ============================================================


USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


async def check_telegram_username(username: str) -> bool:
    """
    Returns True only when Telegram explicitly reports that the
    username is not occupied.

    Telethon must be logged in as a USER account.
    """
    if tg_client is None:
        logger.error("Telethon client is not initialized.")
        return False

    username = username.lstrip("@")

    async with telegram_check_lock:
        try:
            await tg_client.get_entity(username)

            # get_entity succeeded -> username is occupied.
            return False

        except UsernameNotOccupiedError:
            # Telegram explicitly says the username is not occupied.
            return True

        except UsernameInvalidError:
            return False

        except FloodWaitError as e:
            logger.warning("Telegram FloodWait: %s seconds", e.seconds)
            await asyncio.sleep(e.seconds)
            return False

        except ValueError:
            # Some Telethon versions use ValueError for unresolved
            # usernames. Treating it as available would be unsafe,
            # therefore return False.
            return False

        except Exception as e:
            logger.warning(
                "Telegram check failed for @%s: %s",
                username,
                e,
            )
            return False


# ============================================================
# FRAGMENT CHECK
# ============================================================

async def check_fragment_username(
    session: aiohttp.ClientSession,
    username: str,
) -> bool:
    """
    Conservative Fragment availability check.

    Fragment changes its public page frequently. We request the public
    username page and look for explicit availability markers.

    If the page does not contain an unambiguous positive marker, this
    function returns False rather than claiming that a username is free.
    """
    username = username.lstrip("@")
    url = f"https://fragment.com/username/{username}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/151.0 Safari/537.36"
        )
    }

    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=12),
            allow_redirects=True,
        ) as response:
            if response.status != 200:
                return False

            html = await response.text(errors="ignore")
            text = BeautifulSoup(html, "html.parser").get_text(
                " ",
                strip=True,
            )
            text_lower = text.lower()

            # Explicit positive markers seen on Fragment-style pages.
            positive_patterns = [
                r"\bavailable\b",
                r"\bfor sale\b",
                r"\bbuy username\b",
                r"\bplace bid\b",
            ]

            # Explicit negative markers.
            negative_patterns = [
                r"\bnot available\b",
                r"\bunavailable\b",
                r"\balready taken\b",
                r"\bnot for sale\b",
            ]

            if any(re.search(p, text_lower) for p in negative_patterns):
                return False

            if any(re.search(p, text_lower) for p in positive_patterns):
                return True

            return False

    except Exception as e:
        logger.warning(
            "Fragment check failed for @%s: %s",
            username,
            e,
        )
        return False


# ============================================================
# USERNAME GENERATOR
# ============================================================

def generate_username(length: int) -> str:
    """
    Generates letter-only usernames.

    Telegram usernames have to start with a Latin letter.
    User requested 5/6 "букв", therefore this generator uses
    Latin letters only.
    """
    letters = string.ascii_lowercase
    return "".join(random.choice(letters) for _ in range(length))


async def find_available_usernames(
    length: int,
    limit: int,
    user_id: int,
) -> list[str]:
    """
    Finds usernames that are explicitly free in Telegram AND
    explicitly considered available by Fragment.

    The search is conservative: if either source cannot confirm
    availability, the username is not returned.
    """
    results: list[str] = []
    checked: set[str] = set()

    if not 5 <= length <= 32:
        return results

    connector = aiohttp.TCPConnector(limit=10)
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
    ) as session:

        # Limit attempts to prevent an accidental infinite loop.
        max_attempts = 1000

        for _ in range(max_attempts):
            if len(results) >= limit:
                break

            candidate = generate_username(length)

            if candidate in checked:
                continue

            checked.add(candidate)

            # Telegram first — this is the authoritative availability
            # check for Telegram itself.
            tg_free = await check_telegram_username(candidate)

            if not tg_free:
                continue

            # Then Fragment.
            fragment_free = await check_fragment_username(
                session,
                candidate,
            )

            if not fragment_free:
                continue

            results.append(candidate)

            # Count only usernames that were actually returned as found.
            increment_found_count(user_id)

    return results


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user)

    await message.answer(
        start_text(),
        reply_markup=main_keyboard(),
    )


# ============================================================
# MAIN MENU
# ============================================================

@dp.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery):
    await callback.answer()

    try:
        await callback.message.edit_text(
            start_text(),
            reply_markup=main_keyboard(),
        )
    except TelegramBadRequest:
        pass


# ============================================================
# OWNER CHECK
# ============================================================

def is_owner(user_id: int) -> bool:
    return int(user_id) == int(OWNER_ID)


# ============================================================
# OWNER TELETHON AUTH COMMANDS
# ============================================================

@dp.message(F.text == "/auth")
async def cmd_auth(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer("У вас нет доступа.")
        return

    if tg_client is None:
        await message.answer(
            "❌ Telethon не инициализирован. "
            "Проверьте API_ID, API_HASH и PHONE."
        )
        return

    try:
        result = await authorize_telethon_from_bot()

        if result == "already_authorized":
            await message.answer(
                "✅ Telegram-аккаунт уже авторизован.\n"
                "Поиск юзернеймов доступен."
            )
            return

        if result != "code_sent":
            await message.answer(f"❌ {result}")
            return

        await message.answer(
            "📲 Код отправлен в Telegram.\n\n"
            "Введите код следующим сообщением в формате:\n"
            "<code>1.2.3.4.6</code>"
        )

    except Exception as exc:
        logger.exception("Auth request failed: %s", exc)
        await message.answer(
            f"❌ Не удалось отправить код:\n<code>{exc}</code>"
        )


@dp.message(F.text)
async def owner_auth_input(message: Message):
    user_id = message.from_user.id

    if not is_owner(user_id):
        return

    if not auth_state.get("waiting_for_code") and not auth_state.get(
        "waiting_for_password"
    ):
        return

    if tg_client is None:
        await message.answer("❌ Telethon не инициализирован.")
        return

    # Password stage.
    if auth_state.get("waiting_for_password"):
        password = message.text.strip()

        try:
            await tg_client.sign_in(password=password)

            auth_state["waiting_for_password"] = False
            auth_state["waiting_for_code"] = False
            auth_state["phone_code_hash"] = None

            me = await tg_client.get_me()

            await message.answer(
                "✅ <b>Авторизация успешна!</b>\n\n"
                f"Аккаунт: "
                f"<b>{getattr(me, 'username', None) or me.id}</b>\n\n"
                "Теперь можно использовать поиск."
            )

        except Exception as exc:
            logger.exception("2FA auth failed: %s", exc)
            await message.answer(
                "❌ Неверный пароль 2FA или ошибка авторизации.\n"
                "Попробуйте ещё раз."
            )
        return

    # Code stage.
    try:
        code = normalize_login_code(message.text)

    except ValueError:
        await message.answer(
            "❌ Неверный формат.\n\n"
            "Введите код так:\n"
            "<code>1.2.3.4.6</code>"
        )
        return

    try:
        await tg_client.sign_in(
            phone=PHONE,
            code=code,
            phone_code_hash=auth_state.get("phone_code_hash"),
        )

        auth_state["waiting_for_code"] = False
        auth_state["phone_code_hash"] = None

        me = await tg_client.get_me()

        await message.answer(
            "✅ <b>Авторизация успешна!</b>\n\n"
            f"Аккаунт: "
            f"<b>{getattr(me, 'username', None) or me.id}</b>\n\n"
            "Теперь можно использовать поиск."
        )

    except Exception as exc:
        # Avoid importing Telethon's version-specific 2FA exception.
        if exc.__class__.__name__ == "SessionPasswordNeededError":
            auth_state["waiting_for_code"] = False
            auth_state["waiting_for_password"] = True

            await message.answer(
                "🔐 На аккаунте включена двухэтапная аутентификация.\n\n"
                "Введите пароль 2FA."
            )
            return

        logger.exception("Code auth failed: %s", exc)
        await message.answer(
            "❌ Не удалось авторизовать аккаунт.\n"
            "Проверьте код и попробуйте /auth ещё раз."
        )


# ============================================================
# SEARCH
# ============================================================

@dp.callback_query(F.data == "search")
async def cb_search(callback: CallbackQuery):
    await callback.answer()

    try:
        await callback.message.edit_text(
            search_text(),
            reply_markup=search_keyboard(),
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.in_({"search_5", "search_6"}))
async def cb_search_length(callback: CallbackQuery):
    user_id = callback.from_user.id

    if tg_client is None or not await tg_client.is_user_authorized():
        await callback.answer(
            "Поиск временно недоступен: владелец ещё не авторизовал Telegram-аккаунт.",
            show_alert=True,
        )
        return
    ensure_user(callback.from_user)

    length = int(callback.data.split("_")[1])

    if length == 5 and not is_premium(user_id):
        await callback.answer(
            "Для поиска 5 значных юзернеймов требуется премиум",
            show_alert=True,
        )
        return

    # Free users: max 5 found usernames per UTC day.
    if not is_premium(user_id):
        remaining = 5 - get_daily_found_count(user_id)

        if remaining <= 0:
            await callback.answer(
                "Вы достигли дневного лимита: 5 найденных юзернеймов.",
                show_alert=True,
            )
            return

        result_limit = min(RESULTS_PER_SEARCH, remaining)
    else:
        result_limit = RESULTS_PER_SEARCH

    await callback.answer()

    try:
        await callback.message.edit_text(
            (
                f"🔎 Ищу свободные {length}-значные юзернеймы...\n\n"
                "Проверяю Telegram и Fragment."
            )
        )
    except TelegramBadRequest:
        pass

    try:
        results = await find_available_usernames(
            length=length,
            limit=result_limit,
            user_id=user_id,
        )
    except Exception as e:
        logger.exception("Search error: %s", e)
        results = []

    if not results:
        text = (
            "❌ Не удалось найти подтверждённые свободные "
            f"{length}-значные юзернеймы.\n\n"
            "Попробуйте ещё раз."
        )
    else:
        lines = [
            f"✅ <b>Найдено: {len(results)}</b>",
            "",
        ]
        lines.extend(f"@{username}" for username in results)

        if not is_premium(user_id):
            left = max(0, 5 - get_daily_found_count(user_id))
            lines.extend(
                [
                    "",
                    f"Осталось бесплатных находок сегодня: {left}",
                ]
            )

        text = "\n".join(lines)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔎 Искать ещё",
                            callback_data="search",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="◀️ Главное меню",
                            callback_data="back",
                        )
                    ],
                ]
            ),
        )
    except TelegramBadRequest:
        await callback.message.answer(
            text,
            reply_markup=main_keyboard(),
        )


# ============================================================
# PREMIUM MENU
# ============================================================

@dp.callback_query(F.data == "premium")
async def cb_premium(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()

    until = get_premium_until(callback.from_user.id)

    if until and until > utc_now():
        text = (
            "💎 <b>У вас уже есть Premium</b>\n\n"
            f"Действует до: <code>{until.astimezone().strftime('%d.%m.%Y %H:%M')}</code>\n\n"
            "Можно продлить подписку:"
        )
    else:
        text = premium_text()

    try:
        await callback.message.edit_text(
            text,
            reply_markup=premium_plans_keyboard(),
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.regexp(r"^plan_(1d|5d|7d|30d)$"))
async def cb_plan(callback: CallbackQuery):
    plan_code = callback.data.split("_", 1)[1]
    plan = PLANS[plan_code]

    await callback.answer()

    text = (
        f"💎 <b>Premium — {plan['title']}</b>\n\n"
        f"⭐ {plan['stars']} Stars\n"
        f"₽ {plan['rub']}\n\n"
        "Выберите тип оплаты:"
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=payment_type_keyboard(plan_code),
        )
    except TelegramBadRequest:
        pass


# ============================================================
# PAYMENT TYPE
# ============================================================

@dp.callback_query(F.data.regexp(r"^pay_(stars|rub):(1d|5d|7d|30d)$"))
async def cb_payment_type(callback: CallbackQuery):
    match = re.match(
        r"^pay_(stars|rub):(1d|5d|7d|30d)$",
        callback.data,
    )

    if not match:
        await callback.answer("Ошибка.", show_alert=True)
        return

    payment_type = match.group(1)
    plan_code = match.group(2)
    plan = PLANS[plan_code]

    payment_state[callback.from_user.id] = {
        "plan_code": plan_code,
        "payment_type": payment_type,
    }

    await callback.answer()

    if payment_type == "stars":
        text = (
            f"⭐ <b>Оплата Stars — {plan['title']}</b>\n\n"
            f"Отправьте <b>{plan['stars']} Stars</b> на аккаунт "
            f"<b>{STARS_ACCOUNT}</b>.\n\n"
            "После оплаты нажмите кнопку ниже и отправьте "
            "скриншот подтверждения."
        )
    else:
        text = (
            f"₽ <b>Оплата рублями — {plan['title']}</b>\n\n"
            f"Переведите <b>{plan['rub']} рублей</b> на реквизиты:\n\n"
            f"<b>{TBANK_PHONE}</b>\n"
            f"{TBANK_BANK}\n"
            f"{TBANK_RECIPIENT}\n\n"
            "После оплаты нажмите кнопку ниже и отправьте "
            "скриншот подтверждения."
        )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=confirmation_keyboard(),
        )
    except TelegramBadRequest:
        pass


# ============================================================
# PAYMENT CONFIRMATION
# ============================================================

@dp.callback_query(F.data == "payment_confirm")
async def cb_payment_confirm(callback: CallbackQuery):
    state = payment_state.get(callback.from_user.id)

    if not state:
        await callback.answer(
            "Сначала выберите срок и тип оплаты.",
            show_alert=True,
        )
        return

    await callback.answer()

    await callback.message.answer(
        "📸 Отправьте одним сообщением <b>фото/скриншот оплаты</b>.\n\n"
        "После получения его проверит владелец."
    )


@dp.message(F.photo)
async def payment_photo(message: Message):
    user_id = message.from_user.id
    state = payment_state.get(user_id)

    if not state:
        # Photo is not associated with a payment request.
        return

    plan_code = state["plan_code"]
    payment_type = state["payment_type"]
    plan = PLANS[plan_code]

    photo = message.photo[-1]
    file_id = photo.file_id

    ensure_user(message.from_user)

    conn = db()
    cursor = conn.execute(
        """
        INSERT INTO premium_requests
        (user_id, username, plan_code, payment_type,
         screenshot_file_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            user_id,
            message.from_user.username,
            plan_code,
            payment_type,
            file_id,
            iso_now(),
        ),
    )
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Keep state only until the request is created.
    payment_state.pop(user_id, None)

    if OWNER_ID:
        username = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else "нет username"
        )

        owner_caption = (
            "💎 <b>Заявка на премиум!</b>\n\n"
            f"Заявка № <code>{request_id}</code>\n"
            f"Тариф: <b>{plan['title']}</b>\n"
            f"Оплата: <b>{'Stars' if payment_type == 'stars' else 'Рубли'}</b>\n\n"
            f"Юзернейм пользователя: <b>{username}</b>\n"
            f"ID пользователя: <code>{user_id}</code>"
        )

        try:
            await bot.send_photo(
                OWNER_ID,
                photo=file_id,
                caption=owner_caption,
                reply_markup=owner_request_keyboard(request_id),
            )
        except Exception as e:
            logger.exception(
                "Could not send premium request to owner: %s",
                e,
            )

    await message.answer(
        "✅ Заявка отправлена владельцу.\n"
        "После проверки оплаты Premium будет активирован.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# OWNER: APPROVE
# ============================================================

@dp.callback_query(F.data.regexp(r"^approve:\d+$"))
async def cb_approve(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "У вас нет доступа.",
            show_alert=True,
        )
        return

    request_id = int(callback.data.split(":")[1])

    conn = db()
    request = conn.execute(
        "SELECT * FROM premium_requests WHERE id = ?",
        (request_id,),
    ).fetchone()

    if not request:
        conn.close()
        await callback.answer(
            "Заявка не найдена.",
            show_alert=True,
        )
        return

    if request["status"] != "pending":
        conn.close()
        await callback.answer(
            "Заявка уже обработана.",
            show_alert=True,
        )
        return

    plan = PLANS.get(request["plan_code"])
    if not plan:
        conn.close()
        await callback.answer(
            "Неизвестный тариф.",
            show_alert=True,
        )
        return

    new_until = activate_premium(
        request["user_id"],
        plan["days"],
    )

    conn.execute(
        """
        UPDATE premium_requests
        SET status = 'approved',
            processed_at = ?,
            processed_by = ?
        WHERE id = ?
        """,
        (iso_now(), callback.from_user.id, request_id),
    )
    conn.commit()
    conn.close()

    await callback.answer("Premium активирован.")

    try:
        await callback.message.edit_caption(
            caption=(
                f"{callback.message.caption or ''}\n\n"
                "✅ <b>ОДОБРЕНО</b>\n"
                f"Premium до: <code>{new_until.strftime('%d.%m.%Y %H:%M UTC')}</code>"
            ),
            reply_markup=None,
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            request["user_id"],
            (
                "🎉 <b>Premium активирован!</b>\n\n"
                f"Тариф: {plan['title']}\n"
                f"Действует до: "
                f"<code>{new_until.astimezone().strftime('%d.%m.%Y %H:%M')}</code>\n\n"
                "Теперь вам доступен поиск 5-значных юзернеймов "
                "и снят дневной лимит."
            ),
        )
    except Exception as e:
        logger.warning(
            "Could not notify user %s: %s",
            request["user_id"],
            e,
        )


# ============================================================
# OWNER: REJECT
# ============================================================

@dp.callback_query(F.data.regexp(r"^reject:\d+$"))
async def cb_reject(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "У вас нет доступа.",
            show_alert=True,
        )
        return

    request_id = int(callback.data.split(":")[1])

    conn = db()
    request = conn.execute(
        "SELECT * FROM premium_requests WHERE id = ?",
        (request_id,),
    ).fetchone()

    if not request:
        conn.close()
        await callback.answer(
            "Заявка не найдена.",
            show_alert=True,
        )
        return

    if request["status"] != "pending":
        conn.close()
        await callback.answer(
            "Заявка уже обработана.",
            show_alert=True,
        )
        return

    conn.execute(
        """
        UPDATE premium_requests
        SET status = 'rejected',
            processed_at = ?,
            processed_by = ?
        WHERE id = ?
        """,
        (iso_now(), callback.from_user.id, request_id),
    )
    conn.commit()
    conn.close()

    await callback.answer("Заявка отклонена.")

    try:
        await callback.message.edit_caption(
            caption=(
                f"{callback.message.caption or ''}\n\n"
                "❌ <b>ОТКЛОНЕНО</b>"
            ),
            reply_markup=None,
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            request["user_id"],
            (
                "❌ <b>Заявка на Premium отклонена.</b>\n\n"
                "Если вы уверены, что оплатили подписку, "
                "проверьте скриншот и отправьте новую заявку."
            ),
        )
    except Exception as e:
        logger.warning(
            "Could not notify user %s: %s",
            request["user_id"],
            e,
        )


# ============================================================
# PROFILE
# ============================================================

@dp.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    ensure_user(callback.from_user)

    user_id = callback.from_user.id
    found = get_found_count(user_id)
    rank_number, rank_name = get_rank(found)
    premium_until = get_premium_until(user_id)

    if premium_until and premium_until > utc_now():
        premium_status = (
            "💎 Premium активен до "
            f"<code>{premium_until.astimezone().strftime('%d.%m.%Y %H:%M')}</code>"
        )
    else:
        premium_status = "❌ Premium отсутствует"

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"Айди — <code>{user_id}</code>\n\n"
        f"Ваш ранг: <b>{rank_number} ранг — {rank_name}</b>\n"
        f"Найдено юзернеймов: <b>{found}</b>\n\n"
        f"{premium_status}\n\n"
        "1 ранг — Разведчик: 0–50 найденных юзернеймов\n"
        "2 ранг — Коллекционер: 50–120\n"
        "3 ранг — Скаут: 120–222\n"
        "4 ранг — Охотник: 222–700\n"
        "5 ранг — Novera Prime: 700+"
    )

    await callback.answer()

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💎 Премиум",
                            callback_data="premium",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="◀️ Главное меню",
                            callback_data="back",
                        )
                    ],
                ]
            ),
        )
    except TelegramBadRequest:
        pass


# ============================================================
# TEXT FALLBACK
# ============================================================

@dp.message()
async def text_fallback(message: Message):
    ensure_user(message.from_user)

    await message.answer(
        "Используйте кнопки меню:",
        reply_markup=main_keyboard(),
    )


# ============================================================
# TELETHON STARTUP
# ============================================================


def normalize_login_code(value: str) -> str:
    """Convert Telegram code 1.2.3.4.6 to 12346."""
    value = value.strip()
    if re.fullmatch(r"\d(?:\.\d){4}", value):
        return value.replace(".", "")
    if re.fullmatch(r"\d{5}", value):
        return value
    raise ValueError("Код должен быть в формате 1.2.3.4.6")


async def start_telethon():
    """
    Initialize Telethon without blocking bot polling.
    Owner starts actual authorization with /auth.
    """
    global tg_client

    if not API_ID or not API_HASH or not PHONE:
        logger.warning(
            "API_ID / API_HASH / PHONE are empty. "
            "Telegram checks are disabled."
        )
        return

    tg_client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
    )

    await tg_client.connect()

    if await tg_client.is_user_authorized():
        me = await tg_client.get_me()
        logger.info(
            "Telethon already authorized as %s",
            getattr(me, "username", None) or me.id,
        )
    else:
        logger.info(
            "Telethon is not authorized. "
            "Owner must use /auth in the bot."
        )


# ============================================================
# MAIN
# ============================================================

async def main():
    global bot

    # Generate runtime files/directories automatically.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is empty. Fill BOT_TOKEN at the top of the file."
        )

    bot = Bot(BOT_TOKEN)

    try:
        try:
            await start_telethon()
        except Exception as exc:
            # Telethon authorization/network problems must not prevent
            # the Bot API itself from answering /start and other commands.
            logger.exception("Telethon startup failed: %s", exc)
            tg_client = None

        # Remove a possible old webhook before switching to long polling.
        # This fixes the common case where the bot is running but receives
        # no updates because another webhook configuration is active.
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            logger.exception("Could not remove Telegram webhook: %s", exc)

        try:
            me = await bot.get_me()
            logger.info(
                "Bot API connected: @%s (id=%s)",
                me.username,
                me.id,
            )
        except Exception as exc:
            logger.exception("Bot API connection failed: %s", exc)
            raise

        logger.info("NoveraSearch started. Polling Telegram updates.")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        if tg_client is not None:
            await tg_client.disconnect()

        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped.")
    except Exception as exc:
        logger.exception("FATAL STARTUP ERROR: %s", exc)
        raise
