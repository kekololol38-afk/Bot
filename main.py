import asyncio
import logging
import os
from pathlib import Path
from decimal import Decimal, InvalidOperation

import aiosqlite
import httpx
from dotenv import load_dotenv

from ton_core import Address, NetworkGlobalID
from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV4R2

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# НАСТРОЙКИ
# ============================================================

load_dotenv()

TOKEN = os.getenv("8088504112:AAG5QZkujdmucKicJ9bogz5PyO5LQ0ss7bQ", "")

ADMIN_ID = 7411827400

CHANNEL_URL = "https://t.me/moonlllluu"
SUPPORT_URL = "https://t.me/moonlllu"

DB_FILE = "starsmoonlllu.db"
BANNER_FILE = "banner.png"

# ============================================================
# АВТОМАТИЧЕСКАЯ ВЫДАЧА ЧЕРЕЗ MYSTARS FAAS + TON
# ============================================================
MYSTARS_API_KEY = os.getenv("MYSTARS_API_KEY", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
FULFILLMENT_MNEMONIC = os.getenv("FULFILLMENT_MNEMONIC", "").strip()
MYSTARS_API_BASE = os.getenv("MYSTARS_API_BASE", "https://api.mystars.tg").rstrip("/")
FULFILLMENT_POLL_SECONDS = int(os.getenv("FULFILLMENT_POLL_SECONDS", "5"))
FULFILLMENT_TIMEOUT_SECONDS = int(os.getenv("FULFILLMENT_TIMEOUT_SECONDS", "600"))


# Здесь позже укажем твои реальные реквизиты.
PAYMENT_DETAILS = """
💳 <b>Реквизиты для оплаты</b>

🏦 <b>ЮMoney</b>
Номер счёта: <code>4100119592830773</code>

💰 Переведите точную сумму, указанную в заказе.

После оплаты нажмите
«📸 Отправить чек» и отправьте подтверждение оплаты.
"""


# ============================================================
# BOT
# ============================================================

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Ожидаемые чеки: telegram_id -> order_id
pending_receipts = {}

# Временное состояние администратора.
admin_states = {}


# ============================================================
# ЦЕНЫ ПО УМОЛЧАНИЮ
# ============================================================

DEFAULT_PRICES = {
    50: 66,
    100: 133,
    150: 199,
    250: 332,
}

CUSTOM_STAR_RATE = 1.33
MIN_CUSTOM_STARS = 1
MAX_CUSTOM_STARS = 1_000_000


# ============================================================
# DATABASE
# ============================================================

async def init_db():

    async with aiosqlite.connect(DB_FILE) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                balance REAL DEFAULT 0,
                purchases INTEGER DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                product TEXT NOT NULL,
                amount INTEGER DEFAULT 0,
                price REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграция старой базы: новые поля добавляются без удаления истории.
        cursor = await db.execute("PRAGMA table_info(orders)")
        order_columns = {row[1] for row in await cursor.fetchall()}

        if "delivery_status" not in order_columns:
            await db.execute(
                "ALTER TABLE orders ADD COLUMN delivery_status TEXT DEFAULT 'not_ready'"
            )

        if "delivered_at" not in order_columns:
            await db.execute(
                "ALTER TABLE orders ADD COLUMN delivered_at TIMESTAMP"
            )

        if "receipt_file_id" not in order_columns:
            await db.execute(
                "ALTER TABLE orders ADD COLUMN receipt_file_id TEXT"
            )

        new_columns = {
            "recipient_username": "TEXT",
            "provider_order_id": "TEXT",
            "provider_status": "TEXT",
            "provider_payment_amount": "TEXT",
            "provider_payment_address": "TEXT",
            "provider_payment_memo": "TEXT",
            "provider_payment_tx": "TEXT",
            "provider_error": "TEXT",
        }
        for column, column_type in new_columns.items():
            if column not in order_columns:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {column} {column_type}")

        await db.execute("""
            UPDATE orders
            SET delivery_status = 'not_ready'
            WHERE delivery_status IS NULL
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        for amount, price in DEFAULT_PRICES.items():

            await db.execute("""
                INSERT OR IGNORE INTO settings
                (key, value)
                VALUES (?, ?)
            """, (
                f"stars_price_{amount}",
                str(price)
            ))

        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES (?, ?)
        """, ("payment_details", PAYMENT_DETAILS))

        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES (?, ?)
        """, (
            "welcome_text",
            "👋 <b>Добро пожаловать!</b>\n\n⭐ У нас Вы можете приобрести Telegram Stars на свой аккаунт за рубли 💳\n\n🛍️ <b>Хороших покупок!</b>"
        ))

        await db.commit()


# ============================================================
# SETTINGS
# ============================================================

async def get_setting(key, default=None):

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute(
            """
            SELECT value
            FROM settings
            WHERE key = ?
            """,
            (key,)
        )

        row = await cursor.fetchone()

    if row is None:
        return default

    return row[0]


async def set_setting(key, value):

    async with aiosqlite.connect(DB_FILE) as db:

        await db.execute("""
            INSERT INTO settings
            (key, value)
            VALUES (?, ?)

            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, (
            key,
            str(value)
        ))

        await db.commit()


async def get_stars_price(amount):

    price = await get_setting(
        f"stars_price_{amount}"
    )

    if price is None:
        return DEFAULT_PRICES.get(amount, 0)

    return float(price)



async def get_custom_stars_price(amount):
    if amount in DEFAULT_PRICES:
        return await get_stars_price(amount)
    return round(amount * CUSTOM_STAR_RATE, 2)

# ============================================================
# MYSTARS FaaS / TON
# ============================================================

_fulfillment_lock = asyncio.Lock()


def fulfillment_configured():
    return bool(MYSTARS_API_KEY and FULFILLMENT_MNEMONIC)


async def mystars_request(method, path, *, json=None, headers=None):
    if not MYSTARS_API_KEY:
        raise RuntimeError("MYSTARS_API_KEY не задан.")
    request_headers = {"X-Api-Key": MYSTARS_API_KEY, "Accept": "application/json"}
    if json is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(method, f"{MYSTARS_API_BASE}{path}", headers=request_headers, json=json)
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if response.status_code >= 400:
        detail = data.get("message") or data.get("detail") or data.get("error") or response.text
        raise RuntimeError(f"MyStars HTTP {response.status_code}: {detail}")
    return data


async def check_mystars_recipient(username):
    username = username.lstrip("@").strip()
    data = await mystars_request("POST", "/v1/recipients/check", json={
        "type": "stars", "recipient": {"username": username}
    })
    if data.get("eligible") is False:
        raise RuntimeError(data.get("telegram_message") or data.get("reason") or "Получатель не может принять Stars.")
    return data


async def create_mystars_order(local_order_id, username, amount):
    await check_mystars_recipient(username)
    return await mystars_request(
        "POST", "/v1/orders",
        headers={"Idempotency-Key": f"starsmoonllu-order-{local_order_id}"},
        json={
            "type": "stars",
            "recipient": {"username": username.lstrip("@").strip()},
            "quantity": int(amount),
            "payment_currency": "ton",
        },
    )


async def pay_mystars_order(payment):
    if not FULFILLMENT_MNEMONIC:
        raise RuntimeError("FULFILLMENT_MNEMONIC не задан.")
    address_text = payment.get("pay_to_address")
    memo = str(payment.get("memo") or "")
    amount_text = str(payment.get("amount") or "")
    currency = str(payment.get("currency") or "").lower()
    if currency != "ton":
        raise RuntimeError("Автооплата сейчас настроена только для GRAM/TON.")
    if not address_text or not memo or not amount_text:
        raise RuntimeError("MyStars payment block не содержит address/amount/memo.")
    try:
        amount_nano = int(Decimal(amount_text) * Decimal(1_000_000_000))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Некорректная сумма MyStars: {amount_text}") from exc
    client_kwargs = {"network": NetworkGlobalID.MAINNET}
    if TONCENTER_API_KEY:
        client_kwargs["api_key"] = TONCENTER_API_KEY
    client = ToncenterClient(**client_kwargs)
    await client.connect()
    try:
        wallet, _, _, _ = WalletV4R2.from_mnemonic(client, FULFILLMENT_MNEMONIC)
        await wallet.refresh()
        balance = int(wallet.balance or 0)
        if balance < amount_nano:
            human = Decimal(balance) / Decimal(1_000_000_000)
            raise RuntimeError(f"Недостаточно GRAM/TON на hot-wallet: нужно {amount_text}, доступно ~{human}.")
        msg = await wallet.transfer(destination=Address(address_text), amount=amount_nano, body=memo)
        return msg.normalized_hash
    finally:
        await client.close()


async def get_mystars_order(provider_order_id):
    return await mystars_request("GET", f"/v1/orders/{provider_order_id}")


async def update_order_provider(order_id, **fields):
    allowed = {
        "provider_order_id", "provider_status", "provider_payment_amount",
        "provider_payment_address", "provider_payment_memo", "provider_payment_tx",
        "provider_error", "delivery_status", "status", "delivered_at",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [order_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(f"UPDATE orders SET {assignments} WHERE id = ?", values)
        await db.commit()


async def fulfill_order(order_id, notify_user=True):
    async with _fulfillment_lock:
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute("""
                SELECT telegram_id, amount, price, status, delivery_status,
                       recipient_username, provider_order_id
                FROM orders WHERE id = ?
            """, (order_id,))
            row = await cursor.fetchone()
        if not row:
            return False
        telegram_id, amount, price, status, delivery_status, username, provider_order_id = row
        if status != "paid" or delivery_status == "completed":
            return False
        if not username:
            await update_order_provider(order_id, delivery_status="problem", provider_error="У пользователя нет Telegram username.")
            await bot.send_message(ADMIN_ID, f"⚠️ Заказ #{order_id}: у покупателя нет @username. Автовыдача невозможна.")
            return False
        try:
            await update_order_provider(order_id, delivery_status="processing", provider_error=None)
            if provider_order_id:
                raise RuntimeError("У заказа уже есть provider_order_id. Повторная оплата заблокирована во избежание двойной выдачи.")
            created = await create_mystars_order(order_id, username, amount)
            provider_order_id = created.get("order_id")
            payment = created.get("payment") or {}
            if not provider_order_id:
                raise RuntimeError(f"MyStars не вернул order_id: {created}")
            await update_order_provider(
                order_id,
                provider_order_id=provider_order_id,
                provider_status=created.get("status", "awaiting_payment"),
                provider_payment_amount=str(payment.get("amount", "")),
                provider_payment_address=payment.get("pay_to_address"),
                provider_payment_memo=payment.get("memo"),
            )
            tx_hash = await pay_mystars_order(payment)
            await update_order_provider(order_id, provider_payment_tx=tx_hash, provider_status="payment_sent")
            await bot.send_message(
                ADMIN_ID,
                f"⚙️ <b>Заказ #{order_id}</b> оплачен поставщику.\n"
                f"⭐ {amount} Stars\n👤 @{username.lstrip('@')}\n"
                f"🧾 MyStars: <code>{provider_order_id}</code>\n"
                f"🔗 TX: <code>{tx_hash}</code>",
                parse_mode="HTML",
            )
            deadline = asyncio.get_running_loop().time() + FULFILLMENT_TIMEOUT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(FULFILLMENT_POLL_SECONDS)
                current = await get_mystars_order(provider_order_id)
                current_status = str(current.get("status", "")).lower()
                await update_order_provider(order_id, provider_status=current_status)
                if current_status in {"delivered", "completed"}:
                    await update_order_provider(order_id, status="completed", delivery_status="completed", provider_status=current_status)
                    await bot.send_message(
                        telegram_id,
                        f"🎉 <b>Заказ #{order_id} выдан!</b>\n\n⭐ Вам отправлено: <b>{amount} Stars</b>\nСпасибо за покупку!",
                        parse_mode="HTML",
                    )
                    await bot.send_message(ADMIN_ID, f"✅ <b>Заказ #{order_id} выдан.</b> ⭐ {amount} → @{username.lstrip('@')}", parse_mode="HTML")
                    return True
                if current_status in {"failed", "cancelled", "refunded", "rejected"}:
                    raise RuntimeError(f"MyStars завершил заказ со статусом: {current_status}")
            raise RuntimeError("Истёк таймаут ожидания выдачи MyStars.")
        except Exception as exc:
            logging.exception("Fulfillment failed for order #%s", order_id)
            await update_order_provider(order_id, delivery_status="problem", provider_error=str(exc)[:1000])
            await bot.send_message(ADMIN_ID, f"❌ <b>Ошибка автовыдачи #{order_id}</b>\n\n<code>{str(exc)[:1500]}</code>", parse_mode="HTML")
            if notify_user:
                await bot.send_message(telegram_id, f"⚠️ <b>Выдача заказа #{order_id} задерживается.</b>\n\nОплата подтверждена. Мы уже занимаемся проблемой.", parse_mode="HTML")
            return False


async def fulfillment_reconcile_loop():
    while True:
        await asyncio.sleep(30)
        if not fulfillment_configured():
            continue
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute("""
                SELECT id, telegram_id, amount, provider_order_id
                FROM orders
                WHERE status = 'paid' AND delivery_status = 'processing' AND provider_order_id IS NOT NULL
                ORDER BY id ASC LIMIT 5
            """)
            rows = await cursor.fetchall()
        for order_id, telegram_id, amount, provider_order_id in rows:
            try:
                current = await get_mystars_order(provider_order_id)
                status = str(current.get("status", "")).lower()
                await update_order_provider(order_id, provider_status=status)
                if status in {"delivered", "completed"}:
                    await update_order_provider(order_id, status="completed", delivery_status="completed")
                    await bot.send_message(telegram_id, f"🎉 <b>Заказ #{order_id} выдан!</b>\n\n⭐ Stars: <b>{amount}</b>", parse_mode="HTML")
                elif status in {"failed", "cancelled", "refunded", "rejected"}:
                    await update_order_provider(order_id, delivery_status="problem", provider_error=f"MyStars status: {status}")
            except Exception:
                logging.exception("Reconcile failed for order #%s", order_id)


# ============================================================
# USERS
# ============================================================

async def add_user(user):

    async with aiosqlite.connect(DB_FILE) as db:

        await db.execute("""
            INSERT INTO users
            (
                telegram_id,
                username,
                first_name
            )
            VALUES (?, ?, ?)

            ON CONFLICT(telegram_id)
            DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (
            user.id,
            user.username,
            user.first_name
        ))

        await db.commit()


# ============================================================
# MAIN MENU
# ============================================================

def main_menu(user_id=None):

    kb = InlineKeyboardBuilder()

    kb.button(
        text="⭐ Купить Stars",
        callback_data="buy_stars"
    )

    kb.button(
        text="💎 Premium",
        callback_data="premium"
    )

    kb.button(
        text="🎁 Удаленные подарки",
        callback_data="gifts"
    )

    kb.button(
        text="🎧 Поддержка",
        url=SUPPORT_URL
    )

    kb.button(
        text="👤 Профиль",
        callback_data="profile"
    )

    kb.button(
        text="📋 Мои заказы",
        callback_data="my_orders"
    )

    kb.button(
        text="📢 Наш канал",
        url=CHANNEL_URL
    )

    kb.button(
        text="🏆 Топ покупателей",
        callback_data="top_buyers"
    )

    kb.button(
        text="🎰 КАЗИНО",
        callback_data="casino"
    )

    if user_id == ADMIN_ID:
        kb.button(
            text="🛡️ Админ панель",
            callback_data="admin"
        )

    kb.adjust(1)

    return kb.as_markup()


# ============================================================
# TEXT
# ============================================================

async def get_welcome_text():
    return await get_setting(
        "welcome_text",
        "👋 <b>Добро пожаловать!</b>\n\n"
        "⭐ У нас Вы можете приобрести Telegram Stars "
        "на свой аккаунт за рубли 💳\n\n"
        "🛍️ <b>Хороших покупок!</b>"
    )


# ============================================================
# BACK
# ============================================================

def back_button():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="◀️ Назад",
        callback_data="back"
    )

    return kb.as_markup()


def admin_back_button():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="◀️ В админку",
        callback_data="admin"
    )

    return kb.as_markup()


# ============================================================
# SEND MAIN MENU
# ============================================================

async def send_main_menu(message, user_id=None):

    if user_id is None:
        user_id = getattr(getattr(message, "from_user", None), "id", None)

    banner_file_id = await get_setting("banner_file_id")
    welcome = await get_welcome_text()

    if banner_file_id:
        await message.answer_photo(
            photo=banner_file_id,
            caption=welcome,
            reply_markup=main_menu(user_id),
            parse_mode="HTML"
        )
    elif Path(BANNER_FILE).exists():
        photo = FSInputFile(BANNER_FILE)
        await message.answer_photo(
            photo=photo,
            caption=welcome,
            reply_markup=main_menu(user_id),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            welcome,
            reply_markup=main_menu(user_id),
            parse_mode="HTML"
        )




# ============================================================
# SAFE SCREEN EDIT
# ============================================================

async def edit_screen(message, caption=None, reply_markup=None, parse_mode="HTML"):
    """Редактирует экран независимо от того, фото это или обычный текст."""
    if getattr(message, "photo", None):
        return await message.edit_caption(
            caption=caption or "",
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    return await message.edit_text(
        text=caption or "",
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message):

    await add_user(message.from_user)

    await send_main_menu(message, message.from_user.id)


# ============================================================
# ID
# ============================================================

@dp.message(Command("id"))
async def get_id(message: Message):

    await message.answer(
        f"🆔 Ваш Telegram ID:\n\n"
        f"<code>{message.from_user.id}</code>",
        parse_mode="HTML"
    )


custom_stars_pending = set()

# ============================================================
# BUY STARS
# ============================================================

@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: CallbackQuery):

    kb = InlineKeyboardBuilder()

    for amount in [50, 100, 150, 250]:

        price = await get_stars_price(amount)

        kb.button(
            text=f"⭐ {amount} Stars — {price:g} ₽",
            callback_data=f"stars_{amount}"
        )

    kb.button(
        text="✏️ Свое количество Stars",
        callback_data="custom_stars"
    )

    kb.button(
        text="◀️ Назад",
        callback_data="back"
    )

    kb.adjust(1)

    await edit_screen(callback.message, 
        caption=(
            "⭐ <b>Покупка Telegram Stars</b>\n\n"
            "Выберите необходимое количество:"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# CUSTOM STARS
# ============================================================

@dp.callback_query(F.data == "custom_stars")
async def custom_stars(callback: CallbackQuery):
    custom_stars_pending.add(callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="buy_stars")
    await edit_screen(callback.message, caption=(
        "✏️ <b>Свое количество Stars</b>\n\n"
        f"Введите количество Stars от <b>{MIN_CUSTOM_STARS}</b> до <b>{MAX_CUSTOM_STARS:,}</b>.\n\n"
        f"Цена: <b>{CUSTOM_STAR_RATE:.2f} ₽ за 1 Star</b>\n"
        "Например: <code>37</code>"
    ).replace(',', ' '), reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


# ============================================================
# STAR PRODUCT
# ============================================================

@dp.callback_query(F.data.startswith("stars_"))
async def stars_selected(callback: CallbackQuery):

    amount = int(
        callback.data.split("_")[1]
    )

    price = await get_stars_price(amount)

    kb = InlineKeyboardBuilder()

    kb.button(
        text=f"💳 Оплатить {price:g} ₽",
        callback_data=f"create_order_{amount}"
    )

    kb.button(
        text="◀️ Назад",
        callback_data="buy_stars"
    )

    kb.adjust(1)

    await edit_screen(callback.message, 
        caption=(
            f"⭐ <b>{amount} Telegram Stars</b>\n\n"
            f"💰 Стоимость: <b>{price:g} ₽</b>\n\n"
            "Нажмите кнопку ниже, чтобы создать заказ."
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# CREATE ORDER
# ============================================================

@dp.callback_query(F.data.startswith("create_order_"))
async def create_order(callback: CallbackQuery):

    amount = int(
        callback.data.split("_")[2]
    )

    price = await get_custom_stars_price(amount)

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute("""
            INSERT INTO orders
            (
                telegram_id, product, amount, price, status, delivery_status, recipient_username
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            callback.from_user.id, "Telegram Stars", amount, price,
            "waiting_payment", "not_ready", callback.from_user.username
        ))

        order_id = cursor.lastrowid

        await db.commit()

    kb = InlineKeyboardBuilder()

    kb.button(
        text="💳 Перейти к оплате",
        callback_data=f"payment_details_{order_id}"
    )

    kb.button(
        text="◀️ Назад",
        callback_data="buy_stars"
    )

    kb.adjust(1)

    await edit_screen(callback.message, 
        caption=(
            "🧾 <b>Заказ создан</b>\n\n"
            f"🔢 Заказ: <code>#{order_id}</code>\n"
            f"⭐ Stars: <b>{amount}</b>\n"
            f"💰 Сумма: <b>{price:g} ₽</b>\n\n"
            "Нажмите «Реквизиты для оплаты»."
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# PAYMENT DETAILS
# ============================================================

@dp.callback_query(F.data.startswith("payment_details_"))
async def payment_details(callback: CallbackQuery):

    order_id = int(
        callback.data.split("_")[2]
    )

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute("""
            SELECT
                amount,
                price,
                status
            FROM orders
            WHERE id = ?
              AND telegram_id = ?
        """, (
            order_id,
            callback.from_user.id
        ))

        order = await cursor.fetchone()

    if not order:

        await callback.answer(
            "Заказ не найден.",
            show_alert=True
        )

        return

    amount, price, status = order

    if status != "waiting_payment":

        await callback.answer(
            "Этот заказ уже обработан.",
            show_alert=True
        )

        return

    kb = InlineKeyboardBuilder()

    kb.button(
        text="📸 Отправить чек",
        callback_data=f"send_receipt_{order_id}"
    )

    kb.button(
        text="❌ Отменить заказ",
        callback_data=f"cancel_{order_id}"
    )

    kb.adjust(1)

    text = (
        f"💳 <b>Оплата заказа #{order_id}</b>\n\n"
        f"⭐ Количество: <b>{amount} Stars</b>\n"
        f"💰 К оплате: <b>{price:g} ₽</b>\n\n"
        f"{await get_setting("payment_details", PAYMENT_DETAILS)}\n\n"
        "После оплаты нажмите:\n"
        "📸 <b>Отправить чек</b>"
    )

    await edit_screen(callback.message, 
        caption=text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# SEND RECEIPT
# ============================================================

@dp.callback_query(F.data.startswith("send_receipt_"))
async def send_receipt(callback: CallbackQuery):

    order_id = int(callback.data.split("_")[2])

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT amount, price, status
            FROM orders
            WHERE id = ? AND telegram_id = ?
        """, (order_id, callback.from_user.id))
        order = await cursor.fetchone()

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    amount, price, status = order

    if status != "waiting_payment":
        await callback.answer("Этот заказ уже обрабатывается.", show_alert=True)
        return

    pending_receipts[callback.from_user.id] = order_id

    await edit_screen(callback.message, 
        caption=(
            f"📸 <b>Отправка чека — заказ #{order_id}</b>\n\n"
            f"⭐ Stars: <b>{amount}</b>\n"
            f"💰 Сумма: <b>{price:g} ₽</b>\n\n"
            "Теперь отправьте сюда <b>фотографию чека</b> одним сообщением.\n\n"
            "⚠️ Отправляйте только чек, относящийся к этому заказу."
        ),
        reply_markup=back_button(),
        parse_mode="HTML"
    )

    await callback.answer("Отправьте фотографию чека.")


@dp.message(F.photo)
async def receive_receipt(message: Message):

    if message.from_user.id == ADMIN_ID and admin_states.get(ADMIN_ID) == "banner":
        await set_setting("banner_file_id", message.photo[-1].file_id)
        admin_states.pop(ADMIN_ID, None)
        await message.answer("✅ Баннер сохранён.")
        return

    order_id = pending_receipts.get(message.from_user.id)

    if not order_id:
        return

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT telegram_id, amount, price, status
            FROM orders
            WHERE id = ?
        """, (order_id,))
        order = await cursor.fetchone()

        if not order:
            pending_receipts.pop(message.from_user.id, None)
            await message.answer("❌ Заказ не найден.")
            return

        telegram_id, amount, price, status = order

        if telegram_id != message.from_user.id or status != "waiting_payment":
            pending_receipts.pop(message.from_user.id, None)
            await message.answer("⚠️ Этот заказ уже обрабатывается.")
            return

        receipt_file_id = message.photo[-1].file_id

        await db.execute("""
            UPDATE orders
            SET status = 'payment_check', receipt_file_id = ?
            WHERE id = ?
        """, (receipt_file_id, order_id))
        await db.commit()

    pending_receipts.pop(message.from_user.id, None)

    username = f"@{message.from_user.username}" if message.from_user.username else "без username"

    admin_kb = InlineKeyboardBuilder()
    admin_kb.button(text="✅ Подтвердить", callback_data=f"approve_{order_id}")
    admin_kb.button(text="❌ Отклонить", callback_data=f"reject_{order_id}")
    admin_kb.adjust(2)

    admin_text = (
        "🔔 <b>Новый чек на проверку</b>\n\n"
        f"🧾 Заказ: <code>#{order_id}</code>\n"
        f"👤 {username}\n"
        f"🆔 <code>{telegram_id}</code>\n"
        f"⭐ Stars: <b>{amount}</b>\n"
        f"💰 Сумма: <b>{price:g} ₽</b>\n\n"
        "Проверьте чек и выберите действие."
    )

    await bot.send_photo(
        ADMIN_ID,
        receipt_file_id,
        caption=admin_text,
        reply_markup=admin_kb.as_markup(),
        parse_mode="HTML"
    )

    await message.answer(
        f"⏳ <b>Чек получен!</b>\n\n"
        f"🧾 Заказ: <code>#{order_id}</code>\n"
        "Чек отправлен администратору на проверку.\n\n"
        "После проверки вы получите уведомление.",
        parse_mode="HTML"
    )


# ============================================================
# CANCEL ORDER
# ============================================================

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_order(callback: CallbackQuery):

    order_id = int(
        callback.data.split("_")[1]
    )

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute("""
            SELECT telegram_id, status
            FROM orders
            WHERE id = ?
        """, (
            order_id,
        ))

        order = await cursor.fetchone()

        if not order:

            await callback.answer(
                "Заказ не найден.",
                show_alert=True
            )

            return

        telegram_id, status = order

        if telegram_id != callback.from_user.id:

            await callback.answer(
                "⛔ Это не ваш заказ.",
                show_alert=True
            )

            return

        if status not in (
            "waiting_payment",
            "payment_check"
        ):

            await callback.answer(
                "Этот заказ нельзя отменить.",
                show_alert=True
            )

            return

        await db.execute("""
            UPDATE orders
            SET status = 'cancelled'
            WHERE id = ?
        """, (
            order_id,
        ))

        await db.commit()

    await edit_screen(callback.message, 
        caption=(
            f"❌ <b>Заказ #{order_id} отменён.</b>\n\n"
            "Если хотите сделать новую покупку, "
            "вернитесь в магазин."
        ),
        reply_markup=back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# APPROVE PAYMENT
# ============================================================

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT telegram_id, amount, price, status, delivery_status, recipient_username
            FROM orders WHERE id = ?
        """, (order_id,))
        order = await cursor.fetchone()
        if not order:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        telegram_id, amount, price, status, delivery_status, username = order
        if status != "payment_check":
            await callback.answer("Этот заказ уже обработан.", show_alert=True)
            return
        if not username:
            await callback.answer("У пользователя нет @username — автоматическая выдача невозможна.", show_alert=True)
            return
        await db.execute("""
            UPDATE orders SET status = 'paid', delivery_status = 'processing'
            WHERE id = ? AND status = 'payment_check'
        """, (order_id,))
        await db.execute("UPDATE users SET purchases = purchases + 1 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
    await edit_screen(
        callback.message,
        caption=(
            "💰 <b>Оплата подтверждена</b>\n\n"
            f"🧾 Заказ: <code>#{order_id}</code>\n"
            f"⭐ Stars: <b>{amount}</b>\n"
            f"💰 Сумма: <b>{price:g} ₽</b>\n"
            f"👤 Получатель: <b>@{username}</b>\n\n"
            "⚙️ <b>Автоматическая выдача запущена.</b>\n"
            "Бот создаст заказ у поставщика, оплатит его из hot-wallet и дождётся выдачи."
        ),
        reply_markup=admin_back_button(),
        parse_mode="HTML",
    )
    await bot.send_message(
        telegram_id,
        f"✅ <b>Оплата подтверждена!</b>\n\n🧾 Заказ: <code>#{order_id}</code>\n⭐ Stars: <b>{amount}</b>\n\n⚙️ Автоматическая выдача запущена.",
        parse_mode="HTML",
    )
    if not fulfillment_configured():
        await bot.send_message(ADMIN_ID, "⚠️ Автовыдача не настроена: проверь MYSTARS_API_KEY и FULFILLMENT_MNEMONIC.")
        await update_order_provider(order_id, delivery_status="problem", provider_error="MYSTARS_API_KEY/FULFILLMENT_MNEMONIC not configured")
    else:
        asyncio.create_task(fulfill_order(order_id))
    await callback.answer("Оплата подтверждена, автовыдача запущена.")


# ============================================================
# REJECT PAYMENT
# ============================================================

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    order_id = int(
        callback.data.split("_")[1]
    )

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute("""
            SELECT
                telegram_id,
                amount,
                price,
                status
            FROM orders
            WHERE id = ?
        """, (
            order_id,
        ))

        order = await cursor.fetchone()

        if not order:

            await callback.answer(
                "Заказ не найден.",
                show_alert=True
            )

            return

        telegram_id, amount, price, status = order

        if status != "payment_check":

            await callback.answer(
                "Этот заказ уже обработан.",
                show_alert=True
            )

            return

        await db.execute("""
            UPDATE orders
            SET status = 'rejected'
            WHERE id = ?
        """, (
            order_id,
        ))

        await db.commit()

    await edit_screen(callback.message, 
        caption=(
            "❌ <b>Оплата отклонена</b>\n\n"
            f"🧾 Заказ: <code>#{order_id}</code>\n"
            f"⭐ Stars: <b>{amount}</b>\n"
            f"💰 Сумма: <b>{price:g} ₽</b>"
        ),
        reply_markup=None,
        parse_mode="HTML"
    )

    await bot.send_message(
        telegram_id,
        (
            "❌ <b>Оплата не подтверждена</b>\n\n"
            f"🧾 Заказ: <code>#{order_id}</code>\n\n"
            "Проверьте оплату или обратитесь "
            "в поддержку."
        ),
        parse_mode="HTML"
    )

    await callback.answer(
        "Оплата отклонена."
    )


# ============================================================
# DELIVER ORDER
# ============================================================

@dp.callback_query(F.data.startswith("deliver_"))
async def deliver_order(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    await callback.answer("Ручная выдача отключена: используй автоматическую выдачу MyStars.", show_alert=True)


# ============================================================
# FRAGMENT DELIVERY PROBLEM
# ============================================================

@dp.callback_query(F.data.startswith("delivery_problem_"))
async def delivery_problem(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT telegram_id, amount, price, status, delivery_status
            FROM orders WHERE id = ?
        """, (order_id,))
        order = await cursor.fetchone()

        if not order:
            await callback.answer("Заказ не найден.", show_alert=True)
            return

        telegram_id, amount, price, status, delivery_status = order
        if status != "paid":
            await callback.answer("Заказ должен быть в статусе paid.", show_alert=True)
            return

        await db.execute("""
            UPDATE orders SET delivery_status = 'problem'
            WHERE id = ? AND status = 'paid'
        """, (order_id,))
        await db.commit()

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Повторить выдачу", callback_data=f"delivery_retry_{order_id}")
    kb.button(text="📦 К заказам", callback_data="admin_orders")
    kb.adjust(1)

    await edit_screen(
        callback.message,
        caption=(
            "⚠️ <b>Проблема с автоматической выдачей</b>\n\n"
            f"🧾 Заказ: <code>#{order_id}</code>\n"
            f"⭐ Stars: <b>{amount}</b>\n"
            f"💰 Сумма: <b>{price:g} ₽</b>\n\n"
            "📌 Заказ остаётся <b>paid</b>.\n"
            "❗ Он НЕ считается выполненным."
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await bot.send_message(
        telegram_id,
        (
            "⚠️ <b>Выдача заказа задерживается</b>\n\n"
            f"🧾 Заказ: <code>#{order_id}</code>\n"
            "Оплата подтверждена. Мы занимаемся выдачей Stars."
        ),
        parse_mode="HTML"
    )
    await callback.answer("Проблема отмечена.")


@dp.callback_query(F.data.startswith("delivery_retry_"))
async def delivery_retry(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT amount, price, status, delivery_status, provider_order_id FROM orders WHERE id = ?", (order_id,))
        order = await cursor.fetchone()
        if not order:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        amount, price, status, delivery_status, provider_order_id = order
        if status != "paid":
            await callback.answer("Заказ больше не находится в paid.", show_alert=True)
            return
        if provider_order_id:
            await callback.answer("У заказа уже есть MyStars order ID. Используй «Проверить выдачу».", show_alert=True)
            return
        await db.execute("UPDATE orders SET delivery_status = 'processing', provider_error = NULL WHERE id = ?", (order_id,))
        await db.commit()
    await edit_screen(callback.message, caption=(
        "🔄 <b>Автовыдача перезапущена</b>\n\n"
        f"🧾 Заказ: <code>#{order_id}</code>\n⭐ Stars: <b>{amount}</b>\n💰 Сумма: <b>{price:g} ₽</b>\n\n"
        "⚙️ Бот снова запускает выдачу через MyStars."
    ), reply_markup=admin_back_button(), parse_mode="HTML")
    if not fulfillment_configured():
        await bot.send_message(ADMIN_ID, "⚠️ Автовыдача не настроена.")
    else:
        asyncio.create_task(fulfill_order(order_id))
    await callback.answer("Автовыдача запущена.")


# ============================================================
# MY ORDERS
# ============================================================

@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT id, amount, price, status, delivery_status, created_at
            FROM orders
            WHERE telegram_id = ?
            ORDER BY id DESC
            LIMIT 20
        """, (callback.from_user.id,))
        orders = await cursor.fetchall()

    text = "📋 <b>Мои заказы</b>\n\n"
    if not orders:
        text += "У вас пока нет заказов."
    else:
        for order_id, amount, price, status, delivery_status, created_at in orders:
            status_text = {
                "waiting_payment": "⏱ Ожидает оплаты",
                "payment_check": "⏳ Проверяется",
                "paid": "💰 Оплачен",
                "completed": "📦 Выдан",
                "rejected": "❌ Отклонён",
            }.get(status, status)
            text += (
                f"🧾 <b>#{order_id}</b> · ⭐ {amount} · {price:g} ₽\n"
                f"📌 {status_text}\n\n"
            )

    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Купить Stars", callback_data="buy_stars")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1)
    await edit_screen(
        callback.message,
        caption=text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================
# PREMIUM
# ============================================================

@dp.callback_query(F.data == "premium")
async def premium(callback: CallbackQuery):

    kb = InlineKeyboardBuilder()

    kb.button(
        text="💎 3 месяца",
        callback_data="premium_3"
    )

    kb.button(
        text="💎 6 месяцев",
        callback_data="premium_6"
    )

    kb.button(
        text="💎 12 месяцев",
        callback_data="premium_12"
    )

    kb.button(
        text="◀️ Назад",
        callback_data="back"
    )

    kb.adjust(1)

    await edit_screen(callback.message, 
        caption=(
            "💎 <b>Telegram Premium</b>\n\n"
            "Выберите срок подписки:"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("premium_"))
async def premium_selected(callback: CallbackQuery):

    months = callback.data.split("_")[1]

    await edit_screen(callback.message, 
        caption=(
            f"💎 <b>Telegram Premium</b>\n\n"
            f"📅 Срок: <b>{months} месяцев</b>\n\n"
            "Раздел оплаты Premium пока настраивается."
        ),
        reply_markup=back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# GIFTS
# ============================================================

@dp.callback_query(F.data == "gifts")
async def gifts(callback: CallbackQuery):

    await edit_screen(callback.message, 
        caption=(
            "🎁 <b>Удаленные подарки</b>\n\n"
            "Раздел находится в разработке."
        ),
        reply_markup=back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# PROFILE
# ============================================================

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute("""
            SELECT
                balance,
                purchases
            FROM users
            WHERE telegram_id = ?
        """, (
            callback.from_user.id,
        ))

        user = await cursor.fetchone()

    if user:

        balance, purchases = user

    else:

        balance = 0
        purchases = 0

    username = (
        f"@{callback.from_user.username}"
        if callback.from_user.username
        else "не указан"
    )

    await edit_screen(callback.message, 
        caption=(
            "👤 <b>Ваш профиль</b>\n\n"
            f"🆔 ID: <code>{callback.from_user.id}</code>\n"
            f"👤 Username: {username}\n\n"
            f"💰 Баланс: <b>{balance:g} ₽</b>\n"
            f"🛒 Покупок: <b>{purchases}</b>"
        ),
        reply_markup=back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# TOP BUYERS
# ============================================================

@dp.callback_query(F.data == "top_buyers")
async def top_buyers(callback: CallbackQuery):

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute("""
            SELECT
                username,
                first_name,
                purchases
            FROM users
            ORDER BY purchases DESC
            LIMIT 10
        """)

        users = await cursor.fetchall()

    text = "🏆 <b>Топ покупателей</b>\n\n"

    if not users:

        text += "Пока здесь никого нет."

    else:

        for i, row in enumerate(users, 1):

            username, first_name, purchases = row

            name = (
                f"@{username}"
                if username
                else first_name or "Пользователь"
            )

            text += (
                f"{i}. {name} — "
                f"{purchases} покупок\n"
            )

    await edit_screen(callback.message, 
        caption=text,
        reply_markup=back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# TOP REFERRALS
# ============================================================

# ============================================================
# CASINO
# ============================================================

@dp.callback_query(F.data == "casino")
async def casino(callback: CallbackQuery):

    await edit_screen(callback.message, 
        caption=(
            "🎰 <b>КАЗИНО</b>\n\n"
            "Раздел находится в разработке."
        ),
        reply_markup=back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# ADMIN MENU
# ============================================================

@dp.callback_query(F.data == "admin")
async def admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Цены", callback_data="admin_prices")
    kb.button(text="📦 Заказы", callback_data="admin_orders")
    kb.button(text="👥 Пользователи", callback_data="admin_users")
    kb.button(text="📊 Статистика", callback_data="admin_stats")
    kb.button(text="🎨 Оформление", callback_data="admin_design")
    kb.button(text="💳 Реквизиты", callback_data="admin_payment")
    kb.button(text="📢 Рассылка", callback_data="admin_broadcast")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1)

    await edit_screen(
        callback.message,
        caption=(
            "🛡️ <b>Админ панель</b>\n\n"
            "Управление магазином: цены, заказы, оформление,\n"
            "реквизиты и рассылка."
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================
# ADMIN PRICES
# ============================================================

@dp.callback_query(F.data == "admin_prices")
async def admin_prices(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    kb = InlineKeyboardBuilder()

    for amount in [50, 100, 150, 250]:

        price = await get_stars_price(amount)

        kb.button(
            text=f"⭐ {amount} — {price:g} ₽",
            callback_data=f"editprice_{amount}"
        )

    kb.button(
        text="◀️ В админку",
        callback_data="admin"
    )

    kb.adjust(1)

    await edit_screen(callback.message, 
        caption=(
            "💰 <b>Цены Stars</b>\n\n"
            "Выберите тариф:"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# EDIT PRICE
# ============================================================

@dp.callback_query(F.data.startswith("editprice_"))
async def edit_price(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    amount = int(
        callback.data.split("_")[1]
    )

    await edit_screen(callback.message, 
        caption=(
            f"💰 <b>Изменение цены</b>\n\n"
            f"⭐ Тариф: <b>{amount} Stars</b>\n\n"
            f"Используй команду:\n"
            f"<code>/price {amount} НОВАЯ_ЦЕНА</code>\n\n"
            f"Например:\n"
            f"<code>/price {amount} 150</code>"
        ),
        reply_markup=admin_back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# PRICE COMMAND
# ============================================================

@dp.message(Command("price"))
async def change_price(message: Message):

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "⛔ Доступ запрещён."
        )

        return

    parts = message.text.split()

    if len(parts) != 3:

        await message.answer(
            "❌ Формат:\n"
            "<code>/price 100 150</code>",
            parse_mode="HTML"
        )

        return

    try:

        amount = int(parts[1])
        price = float(parts[2])

    except ValueError:

        await message.answer(
            "❌ Используй числа."
        )

        return

    if amount not in DEFAULT_PRICES:

        await message.answer(
            "❌ Такого тарифа нет."
        )

        return

    if price <= 0:

        await message.answer(
            "❌ Цена должна быть больше 0."
        )

        return

    await set_setting(
        f"stars_price_{amount}",
        price
    )

    await message.answer(
        f"✅ Цена изменена!\n\n"
        f"⭐ {amount} Stars\n"
        f"💰 {price:g} ₽",
        parse_mode="HTML"
    )


# ============================================================
# ADMIN ORDERS
# ============================================================

async def render_admin_orders(callback: CallbackQuery, status_filter="all"):
    if status_filter == "all":
        query = """
            SELECT id, telegram_id, amount, price, status, delivery_status
            FROM orders
            ORDER BY id DESC
            LIMIT 20
        """
        params = ()
    else:
        query = """
            SELECT id, telegram_id, amount, price, status, delivery_status
            FROM orders
            WHERE status = ?
            ORDER BY id DESC
            LIMIT 20
        """
        params = (status_filter,)

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(query, params)
        orders = await cursor.fetchall()

        cursor = await db.execute("""
            SELECT status, COUNT(*)
            FROM orders
            GROUP BY status
        """)
        counts = {row[0]: row[1] for row in await cursor.fetchall()}

    text = (
        "📦 <b>Заказы</b>\n\n"
        f"⏳ Проверка: <b>{counts.get('payment_check', 0)}</b>\n"
        f"💰 Оплачены: <b>{counts.get('paid', 0)}</b>\n"
        f"📦 Выданы: <b>{counts.get('completed', 0)}</b>\n"
        f"❌ Отклонены: <b>{counts.get('rejected', 0)}</b>\n\n"
    )

    if not orders:
        text += "Заказов в этом разделе нет."

    kb = InlineKeyboardBuilder()
    for order_id, telegram_id, amount, price, status, delivery_status in orders:
        icon = {
            "payment_check": "⏳",
            "paid": "💰",
            "completed": "📦",
            "rejected": "❌",
        }.get(status, "🧾")
        kb.button(
            text=f"{icon} #{order_id} — {amount} ⭐",
            callback_data=f"order_view_{order_id}"
        )
        text += (
            f"🧾 <b>#{order_id}</b> · ⭐ {amount} · {price:g} ₽\n"
            f"👤 <code>{telegram_id}</code> · {status}\n"
            f"📦 {delivery_status or 'not_ready'}\n\n"
        )

    kb.button(text="📋 Все", callback_data="orders_filter_all")
    kb.button(text="⏳ Проверка", callback_data="orders_filter_payment_check")
    kb.button(text="💰 Оплачены", callback_data="orders_filter_paid")
    kb.button(text="📦 Выданы", callback_data="orders_filter_completed")
    kb.button(text="❌ Отклонены", callback_data="orders_filter_rejected")
    kb.button(text="◀️ В админку", callback_data="admin")
    kb.adjust(1)

    await edit_screen(
        callback.message,
        caption=text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    await render_admin_orders(callback, "all")


@dp.callback_query(F.data.startswith("orders_filter_"))
async def orders_filter(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    await render_admin_orders(
        callback,
        callback.data.replace("orders_filter_", "", 1)
    )


@dp.callback_query(F.data.startswith("order_view_"))
async def order_view(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT telegram_id, product, amount, price, status,
                   delivery_status, delivered_at, receipt_file_id, created_at,
                   recipient_username, provider_order_id, provider_status, provider_error
            FROM orders
            WHERE id = ?
        """, (order_id,))
        order = await cursor.fetchone()

    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    (telegram_id, product, amount, price, status, delivery_status, delivered_at,
     receipt_file_id, created_at, recipient_username, provider_order_id,
     provider_status, provider_error) = order
    status_text = {
        "waiting_payment": "⏱ Ожидает оплаты",
        "payment_check": "⏳ На проверке",
        "paid": "💰 Оплачен",
        "completed": "📦 Выдан",
        "rejected": "❌ Отклонён",
    }.get(status, status)

    caption = (
        f"🧾 <b>Заказ #{order_id}</b>\n\n"
        f"👤 ID: <code>{telegram_id}</code>\n"
        f"⭐ Stars: <b>{amount}</b>\n"
        f"💰 Сумма: <b>{price:g} ₽</b>\n"
        f"📌 Статус: <b>{status_text}</b>\n"
        f"📦 Выдача: <b>{delivery_status or 'not_ready'}</b>\n"
        f"🕐 Создан: <code>{created_at or '—'}</code>"
    )
    if recipient_username:
        caption += f"\n👤 Username: <b>@{recipient_username}</b>"
    if provider_order_id:
        caption += f"\n🧾 MyStars: <code>{provider_order_id}</code>"
    if provider_status:
        caption += f"\n⚙️ Provider: <b>{provider_status}</b>"
    if provider_error:
        caption += f"\n⚠️ Ошибка: <code>{provider_error[:500]}</code>"
    if delivered_at:
        caption += f"\n📦 Выдан: <code>{delivered_at}</code>"
    if receipt_file_id:
        caption += "\n📸 Чек сохранён."

    kb = InlineKeyboardBuilder()
    if status == "payment_check":
        kb.button(text="✅ Подтвердить", callback_data=f"approve_{order_id}")
        kb.button(text="❌ Отклонить", callback_data=f"reject_{order_id}")
    elif status == "paid":
        if delivery_status == "problem":
            kb.button(text="🔄 Повторить автовыдачу", callback_data=f"delivery_retry_{order_id}")
        else:
            kb.button(text="⚙️ Проверить выдачу", callback_data=f"provider_check_{order_id}")
    elif status == "completed":
        kb.button(text="✅ Уже выдан", callback_data="noop")
    kb.button(text="◀️ К заказам", callback_data="admin_orders")
    kb.adjust(1)

    if receipt_file_id:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await bot.send_photo(
            ADMIN_ID,
            receipt_file_id,
            caption=caption,
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
    else:
        await edit_screen(
            callback.message,
            caption=caption,
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer("Изменений нет.", show_alert=True)


# ============================================================
# ADMIN SETTINGS
# ============================================================

@dp.callback_query(F.data == "admin_design")
async def admin_design(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Изменить баннер", callback_data="admin_set_banner")
    kb.button(text="📝 Изменить приветствие", callback_data="admin_set_welcome")
    kb.button(text="◀️ В админку", callback_data="admin")
    kb.adjust(1)
    await edit_screen(callback.message, caption="🎨 <b>Оформление</b>\n\nВыбери, что изменить.", reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_set_banner")
async def admin_set_banner(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    admin_states[ADMIN_ID] = "banner"
    await edit_screen(callback.message, caption="🎨 <b>Новый баннер</b>\n\nОтправь фотографию следующим сообщением.", reply_markup=admin_back_button(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_set_welcome")
async def admin_set_welcome(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    admin_states[ADMIN_ID] = "welcome"
    await edit_screen(callback.message, caption="📝 <b>Новое приветствие</b>\n\nОтправь текст следующим сообщением.", reply_markup=admin_back_button(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_payment")
async def admin_payment(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    details = await get_setting("payment_details", PAYMENT_DETAILS)
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить реквизиты", callback_data="admin_set_payment")
    kb.button(text="◀️ В админку", callback_data="admin")
    kb.adjust(1)
    await edit_screen(callback.message, caption=f"💳 <b>Реквизиты оплаты</b>\n\n{details}", reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_set_payment")
async def admin_set_payment(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    admin_states[ADMIN_ID] = "payment"
    await edit_screen(callback.message, caption="💳 <b>Новые реквизиты</b>\n\nОтправь текст следующим сообщением.", reply_markup=admin_back_button(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    admin_states[ADMIN_ID] = "broadcast"
    await edit_screen(callback.message, caption="📢 <b>Рассылка</b>\n\nОтправь текст следующим сообщением.", reply_markup=admin_back_button(), parse_mode="HTML")
    await callback.answer()


@dp.message(F.text)
async def custom_stars_amount_message(message: Message):
    user_id = message.from_user.id
    if user_id not in custom_stars_pending:
        return
    raw = message.text.strip().replace(" ", "").replace("_", "")
    try:
        amount = int(raw)
    except ValueError:
        await message.answer("❌ Введите целое число, например <code>37</code>.", parse_mode="HTML")
        return
    if not (MIN_CUSTOM_STARS <= amount <= MAX_CUSTOM_STARS):
        await message.answer(f"❌ Количество должно быть от <b>{MIN_CUSTOM_STARS}</b> до <b>{MAX_CUSTOM_STARS:,}</b> Stars.".replace(',', ' '), parse_mode="HTML")
        return
    custom_stars_pending.discard(user_id)
    price=await get_custom_stars_price(amount)
    kb=InlineKeyboardBuilder()
    kb.button(text=f"💳 Оплатить {price:g} ₽", callback_data=f"create_order_{amount}")
    kb.button(text="✏️ Изменить количество", callback_data="custom_stars")
    kb.button(text="◀️ К тарифам", callback_data="buy_stars")
    kb.adjust(1)
    await message.answer((f"⭐ <b>{amount} Telegram Stars</b>\n\n💰 Стоимость: <b>{price:g} ₽</b>\n\nНажмите кнопку ниже, чтобы создать заказ."),reply_markup=kb.as_markup(),parse_mode="HTML")


@dp.message(F.text)
async def admin_text_state(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    state = admin_states.get(ADMIN_ID)
    if state == "welcome":
        await set_setting("welcome_text", message.text)
        admin_states.pop(ADMIN_ID, None)
        await message.answer("✅ Приветствие сохранено.")
    elif state == "payment":
        await set_setting("payment_details", message.text)
        admin_states.pop(ADMIN_ID, None)
        await message.answer("✅ Реквизиты сохранены.")
    elif state == "broadcast":
        admin_states.pop(ADMIN_ID, None)
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute("SELECT telegram_id FROM users")
            user_ids = [row[0] for row in await cursor.fetchall()]
        sent = failed = 0
        for user_id in user_ids:
            try:
                await bot.send_message(user_id, message.text, parse_mode="HTML")
                sent += 1
            except Exception:
                failed += 1
        await message.answer(f"📢 <b>Рассылка завершена</b>\n\n✅ Отправлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>", parse_mode="HTML")


# ============================================================
# ADMIN USERS
# ============================================================

@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute("""
            SELECT
                username,
                first_name,
                telegram_id
            FROM users
            ORDER BY id DESC
            LIMIT 15
        """)

        users = await cursor.fetchall()

    text = "👥 <b>Последние пользователи</b>\n\n"

    if not users:

        text += "Пользователей пока нет."

    else:

        for username, first_name, telegram_id in users:

            name = (
                f"@{username}"
                if username
                else first_name or "Без имени"
            )

            text += (
                f"• {name}\n"
                f"  🆔 <code>{telegram_id}</code>\n\n"
            )

    await edit_screen(callback.message, 
        caption=text,
        reply_markup=admin_back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# ADMIN STATS
# ============================================================

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    async with aiosqlite.connect(DB_FILE) as db:

        cursor = await db.execute(
            "SELECT COUNT(*) FROM users"
        )

        users = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM orders"
        )

        orders = (await cursor.fetchone())[0]

        cursor = await db.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status = 'paid'
        """)

        paid = (await cursor.fetchone())[0]

        cursor = await db.execute("""
            SELECT COALESCE(SUM(price), 0)
            FROM orders
            WHERE status IN ('paid', 'completed')
        """)

        revenue = (await cursor.fetchone())[0]

        cursor = await db.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status = 'payment_check'
        """)

        checks = (await cursor.fetchone())[0]

        cursor = await db.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status = 'completed'
        """)

        delivered = (await cursor.fetchone())[0]

    await edit_screen(callback.message, 
        caption=(
            "📊 <b>Статистика</b>\n\n"
            f"👥 Пользователей: <b>{users}</b>\n"
            f"📦 Заказов: <b>{orders}</b>\n"
            f"⏳ На проверке: <b>{checks}</b>\n"
            f"💰 Оплачено: <b>{paid}</b>\n"
            f"📦 Выдано: <b>{delivered}</b>\n"
            f"💵 Выручка: <b>{revenue:g} ₽</b>"
        ),
        reply_markup=admin_back_button(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# BACK
# ============================================================

@dp.callback_query(F.data == "back")
async def back(callback: CallbackQuery):

    try:
        await callback.message.delete()
    except Exception:
        pass

    await send_main_menu(
        callback.message,
        callback.from_user.id
    )

    await callback.answer()


# ============================================================
# UNKNOWN CALLBACK
# ============================================================

@dp.callback_query()
async def unknown_callback(callback: CallbackQuery):

    await callback.answer(
        "⚠️ Раздел пока недоступен.",
        show_alert=True
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logging.basicConfig(
        level=logging.INFO
    )

    print("================================")
    print("🚀 STARSMOONLLU")
    print("================================")

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в окружении (.env).")

    await init_db()

    print("✅ База данных готова")
    print(f"⚙️ MyStars API: {'ON' if MYSTARS_API_KEY else 'OFF'}")
    print(f"💳 TON hot-wallet: {'ON' if FULFILLMENT_MNEMONIC else 'OFF'}")

    print("✅ Бот запускается...")

    try:

        reconcile_task = asyncio.create_task(fulfillment_reconcile_loop())
        try:
            await dp.start_polling(bot)
        finally:
            reconcile_task.cancel()
            try:
                await reconcile_task
            except asyncio.CancelledError:
                pass

    finally:

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())