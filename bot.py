import asyncio
import csv
import html
import imaplib
import inspect
import os
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from functools import wraps
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BUILD_VERSION = "crm-v4-2026-05-02"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "да", "on"}


def parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise RuntimeError(f"ADMIN_IDS содержит некорректный ID: {part}") from exc
    return ids


def split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    channel_id: str
    admin_ids: set[int]
    contact_url: str
    contact_button_text: str
    default_currency: str
    db_path: str
    auto_publish: bool
    manual_preview: bool
    email_enabled: bool
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_folder: str
    email_check_interval: int
    email_skip_old_on_first_run: bool
    kwork_sender_filter: str
    kwork_success_keywords: list[str]
    kwork_ignore_keywords: list[str]
    allow_all_users: bool
    categories: list[str]


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    channel_id = os.getenv("CHANNEL_ID", "").strip()
    if not bot_token:
        raise RuntimeError("Не указан BOT_TOKEN в .env / Railway Variables")
    if not channel_id:
        raise RuntimeError("Не указан CHANNEL_ID в .env / Railway Variables")

    categories = split_csv(
        os.getenv(
            "ORDER_CATEGORIES",
            "Telegram-боты,Парсеры,Автоматизация,GPT-боты,Сайты,Доработки,Другое",
        )
    )
    if not categories:
        categories = ["Другое"]

    return Settings(
        bot_token=bot_token,
        channel_id=channel_id,
        admin_ids=parse_admin_ids(os.getenv("ADMIN_IDS", "").strip()),
        contact_url=os.getenv("CONTACT_URL", "").strip(),
        contact_button_text=os.getenv("CONTACT_BUTTON_TEXT", "Заказать разработку").strip() or "Заказать разработку",
        default_currency=os.getenv("DEFAULT_CURRENCY", "₽").strip() or "₽",
        db_path=os.getenv("DB_PATH", "data/orders.db").strip() or "data/orders.db",
        auto_publish=env_bool("AUTO_PUBLISH", False),
        manual_preview=env_bool("MANUAL_PREVIEW", True),
        email_enabled=env_bool("EMAIL_ENABLED", False),
        imap_host=os.getenv("IMAP_HOST", "imap.gmail.com").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or "993"),
        imap_user=os.getenv("IMAP_USER", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        imap_folder=os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX",
        email_check_interval=max(15, int(os.getenv("EMAIL_CHECK_INTERVAL", "60").strip() or "60")),
        email_skip_old_on_first_run=env_bool("EMAIL_SKIP_OLD_ON_FIRST_RUN", True),
        kwork_sender_filter=os.getenv("KWORK_SENDER_FILTER", "kwork").strip().lower(),
        kwork_success_keywords=[x.lower() for x in split_csv(os.getenv(
            "KWORK_SUCCESS_KEYWORDS",
            "заказ выполнен,заказ закрыт,работа выполнена,заказ завершен,работа принята,оплата зачислена,order completed,completed",
        ))],
        kwork_ignore_keywords=[x.lower() for x in split_csv(os.getenv(
            "KWORK_IGNORE_KEYWORDS",
            "новый заказ,заказ отменен,доработка,арбитраж,просрочен,сообщение от покупателя",
        ))],
        allow_all_users=env_bool("ALLOW_ALL_USERS", False),
        categories=categories,
    )


settings = load_settings()
router = Router()


class ManualOrder(StatesGroup):
    waiting_amount = State()
    waiting_title = State()
    waiting_note = State()


class EditOrder(StatesGroup):
    waiting_amount = State()
    waiting_title = State()
    waiting_note = State()
    waiting_category = State()


# ========================
# Base helpers
# ========================

def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today_ru() -> str:
    return datetime.now().strftime("%d.%m.%Y")


def current_month_key() -> str:
    return datetime.now().strftime("%Y-%m")


def current_month_ru() -> str:
    months = {
        "01": "январь", "02": "февраль", "03": "март", "04": "апрель",
        "05": "май", "06": "июнь", "07": "июль", "08": "август",
        "09": "сентябрь", "10": "октябрь", "11": "ноябрь", "12": "декабрь",
    }
    return f"{months[datetime.now().strftime('%m')]} {datetime.now().strftime('%Y')}"


def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    return con


def ensure_column(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with db_connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'manual',
                source_uid TEXT UNIQUE,
                amount INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT '₽',
                title TEXT NOT NULL,
                category TEXT,
                note TEXT,
                raw_subject TEXT,
                raw_from TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                published_at TEXT,
                channel_message_id INTEGER
            )
            """
        )
        ensure_column(con, "orders", "category", "TEXT")
        ensure_column(con, "orders", "channel_message_id", "INTEGER")
        ensure_column(con, "orders", "published_at", "TEXT")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_published_at ON orders(published_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_category ON orders(category)")


def get_state(key: str) -> Optional[str]:
    with db_connect() as con:
        row = con.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None


def set_state(key: str, value: str) -> None:
    with db_connect() as con:
        con.execute(
            "INSERT INTO app_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def delete_state(key: str) -> None:
    with db_connect() as con:
        con.execute("DELETE FROM app_state WHERE key = ?", (key,))


def is_admin_user(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    if settings.allow_all_users:
        return True
    return user_id in settings.admin_ids


def access_denied_text(user_id: Optional[int]) -> str:
    if not settings.admin_ids and not settings.allow_all_users:
        return (
            "🔒 Доступ закрыт. Бот работает только для владельца.\n\n"
            "В Railway → Variables укажи свой Telegram ID:\n"
            f"<code>ADMIN_IDS={user_id or 'ТВОЙ_ID'}</code>\n\n"
            "После этого сделай redeploy/restart."
        )
    return "🔒 Нет доступа."


def only_admin(handler):
    handler_signature = inspect.signature(handler)
    allowed_kwargs = set(handler_signature.parameters.keys())

    @wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        user_id = message.from_user.id if message.from_user else None
        if not is_admin_user(user_id):
            await message.answer(access_denied_text(user_id), parse_mode=ParseMode.HTML)
            return
        filtered_kwargs = {key: value for key, value in kwargs.items() if key in allowed_kwargs}
        return await handler(message, *args, **filtered_kwargs)

    return wrapper


def format_money(amount: int, currency: str | None = None) -> str:
    currency = currency or settings.default_currency
    return f"{amount:,}".replace(",", " ") + f" {currency}"


def normalize_amount(raw: str) -> int:
    cleaned = re.sub(r"[^0-9]", "", raw or "")
    if not cleaned:
        return 0
    return int(cleaned)


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"^(re|fw|fwd):\s*", "", title, flags=re.I)
    title = re.sub(r"\b(kwork|кворк)\b", "", title, flags=re.I).strip(" -—|:")
    return title[:160] or "Заказ на Kwork"


def clean_category(category: str | None) -> str:
    category = re.sub(r"\s+", " ", category or "").strip()
    return category[:60] if category else "Другое"


def detect_category(title: str, body: str = "") -> str:
    text = f"{title}\n{body}".lower()
    rules = [
        ("Telegram-боты", ["telegram", "телеграм", "тг", "бот", "bot", "aiogram"]),
        ("GPT-боты", ["gpt", "chatgpt", "openai", "нейро", "ии", "ai бот", "ассистент"]),
        ("Парсеры", ["парсер", "парсинг", "parser", "scraping", "скрап"]),
        ("Автоматизация", ["автомат", "автоматизация", "интеграция", "api", "скрипт"]),
        ("Сайты", ["сайт", "лендинг", "web", "frontend", "backend", "веб"]),
        ("Доработки", ["доработ", "исправ", "фикс", "правк", "bug", "ошибк"]),
    ]
    configured_lower = {c.lower(): c for c in settings.categories}
    for category, keywords in rules:
        if category.lower() in configured_lower and any(k in text for k in keywords):
            return configured_lower[category.lower()]
    return settings.categories[0] if settings.categories else "Другое"


# ========================
# DB helpers
# ========================

def insert_order(
    *,
    source: str,
    source_uid: Optional[str],
    amount: int,
    title: str,
    category: Optional[str] = None,
    note: Optional[str] = None,
    currency: Optional[str] = None,
    raw_subject: Optional[str] = None,
    raw_from: Optional[str] = None,
    status: str = "draft",
) -> Optional[int]:
    try:
        with db_connect() as con:
            cur = con.execute(
                """
                INSERT INTO orders(source, source_uid, amount, currency, title, category, note, raw_subject, raw_from, status, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    source_uid,
                    int(amount or 0),
                    currency or settings.default_currency,
                    clean_title(title),
                    clean_category(category or detect_category(title or "")),
                    (note or "").strip() or None,
                    raw_subject,
                    raw_from,
                    status,
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def update_order(order_id: int, **fields) -> bool:
    allowed = {"amount", "currency", "title", "category", "note", "status", "published_at", "channel_message_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join([f"{k} = ?" for k in updates])
    values = list(updates.values()) + [order_id]
    with db_connect() as con:
        cur = con.execute(f"UPDATE orders SET {set_clause} WHERE id = ?", values)
        return cur.rowcount > 0


def get_order(order_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as con:
        return con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def get_recent_orders(limit: int = 10) -> list[sqlite3.Row]:
    with db_connect() as con:
        return con.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 30)),),
        ).fetchall()


def reset_orders_table() -> None:
    with db_connect() as con:
        con.execute("DELETE FROM orders")
        con.execute("DELETE FROM sqlite_sequence WHERE name = 'orders'")


def delete_order_record(order_id: int) -> bool:
    with db_connect() as con:
        cur = con.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        return cur.rowcount > 0


# ========================
# Rendering / keyboards
# ========================

def public_order_keyboard() -> InlineKeyboardMarkup | None:
    if not settings.contact_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=settings.contact_button_text, url=settings.contact_url)]]
    )


def draft_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{order_id}"),
                InlineKeyboardButton(text="🗑 Пропустить", callback_data=f"skip:{order_id}"),
            ],
            [
                InlineKeyboardButton(text="💰 Сумма", callback_data=f"edit_amount:{order_id}"),
                InlineKeyboardButton(text="🧩 Услуга", callback_data=f"edit_title:{order_id}"),
            ],
            [
                InlineKeyboardButton(text="🏷 Категория", callback_data=f"edit_category:{order_id}"),
                InlineKeyboardButton(text="💬 Комментарий", callback_data=f"edit_note:{order_id}"),
            ],
            [InlineKeyboardButton(text="❌ Удалить из базы", callback_data=f"delete:{order_id}")],
        ]
    )


def category_keyboard(order_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for idx, category in enumerate(settings.categories):
        current.append(InlineKeyboardButton(text=category, callback_data=f"set_category:{order_id}:{idx}"))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([InlineKeyboardButton(text="✍️ Ввести свою", callback_data=f"custom_category:{order_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"preview:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_post_text(order: sqlite3.Row | dict) -> str:
    title = html.escape(str(order["title"]))
    category = html.escape(str(order["category"] or "Другое"))
    note = html.escape(str(order["note"] or ""))
    amount = int(order["amount"] or 0)
    currency = str(order["currency"] or settings.default_currency)
    order_id = int(order["id"])

    # Лёгкая вариативность для канала: текст выглядит живее, но структура всегда понятная.
    headers = [
        "✅ <b>ЗАКАЗ ВЫПОЛНЕН</b>",
        "🚀 <b>НОВЫЙ ЗАВЕРШЁННЫЙ ПРОЕКТ</b>",
        "🔥 <b>РАБОТА СДАНА</b>",
    ]
    header = headers[(order_id - 1) % len(headers)]

    lines = [header, ""]
    if amount > 0:
        lines.append(f"💰 <b>Сумма:</b> {format_money(amount, currency)}")
    else:
        lines.append("💰 <b>Сумма:</b> не указана")
    lines.extend(
        [
            f"🧩 <b>Услуга:</b> {title}",
            f"🏷 <b>Категория:</b> {category}",
            f"📅 <b>Дата:</b> {today_ru()}",
            f"🔢 <b>Заказ №:</b> {order_id:06d}",
        ]
    )
    if note:
        lines.extend(["", f"💬 {note}"])
    lines.extend(["", "Спасибо за доверие 🙌"])
    return "\n".join(lines)


def build_draft_preview(row: sqlite3.Row) -> str:
    return (
        "📝 <b>Предпросмотр заказа</b>\n"
        "Можешь сразу опубликовать или поправить поля кнопками ниже.\n\n"
        + build_post_text(row)
    )


async def send_draft_preview(bot: Bot, chat_id: int, order_id: int, intro: str | None = None) -> None:
    row = get_order(order_id)
    if not row:
        await bot.send_message(chat_id, "Заказ не найден.")
        return
    text = (intro + "\n\n" if intro else "") + build_draft_preview(row)
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=draft_keyboard(order_id))


async def edit_or_send_preview(callback: CallbackQuery, order_id: int, intro: str | None = None) -> None:
    row = get_order(order_id)
    if not row:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    text = (intro + "\n\n" if intro else "") + build_draft_preview(row)
    if callback.message:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=draft_keyboard(order_id))
    await callback.answer()


async def try_delete_channel_message(bot: Bot, row: sqlite3.Row) -> None:
    channel_message_id = row["channel_message_id"]
    if not channel_message_id:
        return
    try:
        await bot.delete_message(settings.channel_id, int(channel_message_id))
    except Exception:
        pass


async def publish_order(bot: Bot, order_id: int) -> Optional[int]:
    row = get_order(order_id)
    if not row:
        return None
    if row["status"] == "published" and row["channel_message_id"]:
        return int(row["channel_message_id"])

    msg = await bot.send_message(
        chat_id=settings.channel_id,
        text=build_post_text(row),
        parse_mode=ParseMode.HTML,
        reply_markup=public_order_keyboard(),
        disable_web_page_preview=True,
    )
    update_order(order_id, status="published", published_at=now_iso(), channel_message_id=msg.message_id)
    return msg.message_id


async def notify_admins(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if not settings.admin_ids:
        return
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception:
            pass


def parse_done_args(args: str) -> tuple[int, str, str, str]:
    args = (args or "").strip()
    if not args:
        return 0, "", "", ""

    # /done 3000 | название | комментарий | категория
    if "|" in args:
        parts = [p.strip() for p in args.split("|")]
        amount = normalize_amount(parts[0]) if parts else 0
        title = parts[1] if len(parts) > 1 else "Заказ"
        note = parts[2] if len(parts) > 2 else ""
        category = parts[3] if len(parts) > 3 else detect_category(title, note)
        return amount, title, note, category

    # /done 3000 Разработка Telegram-бота
    match = re.match(r"^([\d\s.,]+)\s+(.+)$", args)
    if match:
        title = match.group(2).strip()
        return normalize_amount(match.group(1)), title, "", detect_category(title)

    return 0, args, "", detect_category(args)


# ========================
# Commands
# ========================

@router.message(Command("start", "help"))
@only_admin
async def cmd_start(message: Message) -> None:
    mode = "полный автомат" if settings.auto_publish else "предпросмотр с подтверждением"
    manual_mode = "предпросмотр" if settings.manual_preview else "сразу в канал"
    email_status = "включена" if settings.email_enabled else "выключена"
    await message.answer(
        f"Привет. Я бот для автопостинга выполненных заказов в канал.\nВерсия: <b>{BUILD_VERSION}</b>\n\n"
        f"Kwork-почта: <b>{html.escape(mode)}</b> · проверка: <b>{html.escape(email_status)}</b>\n"
        f"Ручные заказы: <b>{html.escape(manual_mode)}</b>\n\n"
        "Команды:\n"
        "<code>/done 3000 Разработка Telegram-бота</code> — добавить заказ\n"
        "<code>/done 3000 | Название | Комментарий | Категория</code> — добавить подробно\n"
        "<code>/quickdone 3000 Название</code> — сразу опубликовать без предпросмотра\n"
        "<code>/drafts</code> — черновики и предпросмотр\n"
        "<code>/orders</code> — последние заказы\n"
        "<code>/delete_order 2</code> или <code>/del 2</code> — удалить заказ\n"
        "<code>/reset</code> — удалить все заказы и сбросить нумерацию\n\n"
        "Статистика:\n"
        "<code>/stats</code> или <code>/earnings</code> — заработок и цель месяца\n"
        "<code>/months</code> — статистика по месяцам\n"
        "<code>/categories</code> — статистика по категориям\n"
        "<code>/goal 100000</code> — поставить цель на месяц\n"
        "<code>/export</code> — выгрузить заказы в CSV\n"
        "<code>/backup</code> — скачать базу SQLite\n\n"
        "Сервис:\n"
        "<code>/checkmail</code> — проверить почту сейчас\n"
        "<code>/whoami</code> — показать Telegram ID\n"
        "<code>/version</code> — проверить версию",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("version", "health"))
@only_admin
async def cmd_version(message: Message) -> None:
    await message.answer(
        f"✅ Бот живой. Версия: <b>{BUILD_VERSION}</b>\n"
        f"База: <code>{html.escape(settings.db_path)}</code>\n"
        f"Почта: <b>{'включена' if settings.email_enabled else 'выключена'}</b>\n"
        f"Защита: <b>{'выключена, доступ всем' if settings.allow_all_users else 'только ADMIN_IDS'}</b>\n"
        f"Категории: <b>{html.escape(', '.join(settings.categories))}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("whoami", "id"))
async def cmd_whoami(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    await message.answer(
        "Твой Telegram ID:\n"
        f"<code>{user_id}</code>\n\n"
        "Для защиты бота в Railway → Variables поставь:\n"
        f"<code>ADMIN_IDS={user_id}</code>\n"
        "<code>ALLOW_ALL_USERS=false</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("done"))
@only_admin
async def cmd_done(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    amount, title, note, category = parse_done_args(command.args or "")
    if not command.args:
        await state.set_state(ManualOrder.waiting_amount)
        await message.answer("Введи сумму заказа, например: <code>3000</code>", parse_mode=ParseMode.HTML)
        return

    if not title:
        await message.answer("Не понял название услуги. Пример: <code>/done 3000 Разработка Telegram-бота</code>", parse_mode=ParseMode.HTML)
        return

    order_id = insert_order(source="manual", source_uid=None, amount=amount, title=title, note=note, category=category, status="draft")
    if not order_id:
        await message.answer("Не смог создать заказ.")
        return

    if settings.manual_preview:
        await send_draft_preview(bot, message.chat.id, order_id, "Создал черновик ✅")
    else:
        await publish_order(bot, order_id)
        await message.answer("Готово, пост опубликован в канал ✅")


@router.message(Command("quickdone", "qdone"))
@only_admin
async def cmd_quick_done(message: Message, command: CommandObject, bot: Bot) -> None:
    amount, title, note, category = parse_done_args(command.args or "")
    if not command.args or not title:
        await message.answer("Пример: <code>/quickdone 3000 Разработка Telegram-бота</code>", parse_mode=ParseMode.HTML)
        return
    order_id = insert_order(source="manual", source_uid=None, amount=amount, title=title, note=note, category=category, status="draft")
    if not order_id:
        await message.answer("Не смог создать заказ.")
        return
    await publish_order(bot, order_id)
    await message.answer("Готово, пост сразу опубликован в канал ✅")


@router.message(ManualOrder.waiting_amount)
@only_admin
async def manual_amount(message: Message, state: FSMContext) -> None:
    amount = normalize_amount(message.text or "")
    if amount <= 0:
        await message.answer("Сумма должна быть числом. Например: <code>3000</code>", parse_mode=ParseMode.HTML)
        return
    await state.update_data(amount=amount)
    await state.set_state(ManualOrder.waiting_title)
    await message.answer("Теперь введи название услуги, например: <code>Разработка Telegram-бота</code>", parse_mode=ParseMode.HTML)


@router.message(ManualOrder.waiting_title)
@only_admin
async def manual_title(message: Message, state: FSMContext) -> None:
    title = clean_title(message.text or "")
    await state.update_data(title=title)
    await state.set_state(ManualOrder.waiting_note)
    await message.answer("Комментарий к заказу. Можно написать <code>-</code>, если без комментария.", parse_mode=ParseMode.HTML)


@router.message(ManualOrder.waiting_note)
@only_admin
async def manual_note(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    note = (message.text or "").strip()
    if note == "-":
        note = ""
    title = str(data["title"])
    order_id = insert_order(
        source="manual",
        source_uid=None,
        amount=int(data["amount"]),
        title=title,
        category=detect_category(title, note),
        note=note,
        status="draft",
    )
    await state.clear()
    if not order_id:
        await message.answer("Не смог создать заказ.")
        return
    await send_draft_preview(bot, message.chat.id, order_id, "Черновик готов ✅")


@router.message(Command("orders", "list"))
@only_admin
async def cmd_orders(message: Message) -> None:
    rows = get_recent_orders(10)
    if not rows:
        await message.answer("Заказов пока нет. Нумерация начнётся с <b>№000001</b>.", parse_mode=ParseMode.HTML)
        return

    await message.answer(
        "Последние заказы. Тестовый заказ можно удалить кнопкой ниже.\n\n"
        "Для полного обнуления: <code>/reset</code>",
        parse_mode=ParseMode.HTML,
    )
    for row in rows:
        status = html.escape(str(row["status"]))
        title = html.escape(str(row["title"]))
        category = html.escape(str(row["category"] or "Другое"))
        amount = format_money(int(row["amount"] or 0), str(row["currency"] or settings.default_currency))
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="👀 Открыть", callback_data=f"preview:{int(row['id'])}"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{int(row['id'])}"),
                ]
            ]
        )
        await message.answer(
            f"🔢 <b>№{int(row['id']):06d}</b>\n"
            f"Статус: <b>{status}</b>\n"
            f"Сумма: <b>{html.escape(amount)}</b>\n"
            f"Категория: <b>{category}</b>\n"
            f"Услуга: {title}",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


@router.message(Command("delete_order", "del", "delete"))
@only_admin
async def cmd_delete_order(message: Message, command: CommandObject, bot: Bot) -> None:
    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        await message.answer("Напиши номер заказа. Пример: <code>/delete_order 2</code>", parse_mode=ParseMode.HTML)
        return

    order_id = int(raw)
    row = get_order(order_id)
    if not row:
        await message.answer(f"Заказ №{order_id:06d} не найден.")
        return

    await try_delete_channel_message(bot, row)
    delete_order_record(order_id)
    await message.answer(f"Удалил заказ №{order_id:06d} из базы ✅")


@router.message(Command("reset_orders", "reset"))
@only_admin
async def cmd_reset_orders(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить всё и сбросить №", callback_data="reset_orders_confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="reset_orders_cancel")],
        ]
    )
    await message.answer(
        "⚠️ Это удалит <b>все заказы из базы</b> и сбросит нумерацию.\n\n"
        "После этого следующий пост будет <b>Заказ №000001</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@router.message(Command("drafts"))
@only_admin
async def cmd_drafts(message: Message) -> None:
    with db_connect() as con:
        rows = con.execute("SELECT * FROM orders WHERE status = 'draft' ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        await message.answer("Черновиков нет.")
        return
    for row in rows:
        await message.answer(build_draft_preview(row), parse_mode=ParseMode.HTML, reply_markup=draft_keyboard(int(row["id"])))


@router.message(Command("stats", "earnings", "money"))
@only_admin
async def cmd_stats(message: Message) -> None:
    with db_connect() as con:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS cnt,
                COALESCE(SUM(amount), 0) AS total,
                COALESCE(AVG(NULLIF(amount, 0)), 0) AS avg_amount,
                COALESCE(MAX(amount), 0) AS max_amount
            FROM orders
            WHERE status = 'published'
            """
        ).fetchone()
        today = con.execute(
            """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
            FROM orders
            WHERE status = 'published' AND substr(COALESCE(published_at, created_at), 1, 10) = date('now', 'localtime')
            """
        ).fetchone()
        month = con.execute(
            """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
            FROM orders
            WHERE status = 'published' AND substr(COALESCE(published_at, created_at), 1, 7) = strftime('%Y-%m', 'now', 'localtime')
            """
        ).fetchone()
        drafts = con.execute("SELECT COUNT(*) AS cnt FROM orders WHERE status = 'draft'").fetchone()["cnt"]
        best_cat = con.execute(
            """
            SELECT COALESCE(NULLIF(category, ''), 'Другое') AS category, COALESCE(SUM(amount), 0) AS total
            FROM orders
            WHERE status = 'published'
            GROUP BY COALESCE(NULLIF(category, ''), 'Другое')
            ORDER BY total DESC
            LIMIT 1
            """
        ).fetchone()

    avg_amount = int(float(row["avg_amount"] or 0))
    month_total = int(month["total"] or 0)
    goal_raw = get_state(f"goal:{current_month_key()}")
    goal_block = ""
    if goal_raw and goal_raw.isdigit() and int(goal_raw) > 0:
        goal = int(goal_raw)
        percent = min(100, int(month_total * 100 / goal)) if goal else 0
        left = max(0, goal - month_total)
        bar_fill = min(10, int(percent / 10))
        bar = "█" * bar_fill + "░" * (10 - bar_fill)
        goal_block = (
            f"\n🎯 <b>Цель на {html.escape(current_month_ru())}:</b> {format_money(goal)}\n"
            f"{bar} <b>{percent}%</b> · осталось {format_money(left)}\n"
        )

    best_category_text = "нет данных"
    if best_cat:
        best_category_text = f"{best_cat['category']} · {format_money(int(best_cat['total'] or 0))}"

    await message.answer(
        f"📊 <b>Статистика и заработок</b>\n\n"
        f"💰 <b>За всё время:</b> {format_money(int(row['total']))}\n"
        f"✅ Выполнено заказов: <b>{row['cnt']}</b>\n"
        f"📆 <b>За сегодня:</b> {format_money(int(today['total']))} · заказов: <b>{today['cnt']}</b>\n"
        f"🗓 <b>За этот месяц:</b> {format_money(month_total)} · заказов: <b>{month['cnt']}</b>\n"
        f"📈 Средний чек: <b>{format_money(avg_amount)}</b>\n"
        f"🏆 Самый крупный заказ: <b>{format_money(int(row['max_amount']))}</b>\n"
        f"🏷 Лучшая категория: <b>{html.escape(best_category_text)}</b>\n"
        f"📝 Черновиков на подтверждение: <b>{drafts}</b>"
        f"{goal_block}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("goal"))
@only_admin
async def cmd_goal(message: Message, command: CommandObject) -> None:
    key = f"goal:{current_month_key()}"
    raw = (command.args or "").strip()
    if not raw:
        saved = get_state(key)
        if saved and saved.isdigit() and int(saved) > 0:
            await message.answer(
                f"🎯 Текущая цель на {html.escape(current_month_ru())}: <b>{format_money(int(saved))}</b>\n\n"
                "Изменить: <code>/goal 100000</code>\n"
                "Убрать: <code>/goal 0</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer("Цель на месяц не задана. Пример: <code>/goal 100000</code>", parse_mode=ParseMode.HTML)
        return

    amount = normalize_amount(raw)
    if amount <= 0:
        delete_state(key)
        await message.answer("Цель на текущий месяц убрана ✅")
        return
    set_state(key, str(amount))
    await message.answer(f"🎯 Поставил цель на {html.escape(current_month_ru())}: <b>{format_money(amount)}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("months", "month_stats"))
@only_admin
async def cmd_months(message: Message) -> None:
    with db_connect() as con:
        rows = con.execute(
            """
            SELECT substr(COALESCE(published_at, created_at), 1, 7) AS ym,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(amount), 0) AS total,
                   COALESCE(AVG(NULLIF(amount, 0)), 0) AS avg_amount
            FROM orders
            WHERE status = 'published'
            GROUP BY ym
            ORDER BY ym DESC
            LIMIT 12
            """
        ).fetchall()
    if not rows:
        await message.answer("Пока нет опубликованных заказов для статистики по месяцам.")
        return
    lines = ["📅 <b>Статистика по месяцам</b>", ""]
    for row in rows:
        avg_amount = int(float(row["avg_amount"] or 0))
        lines.append(
            f"<b>{html.escape(row['ym'])}</b>: {format_money(int(row['total'] or 0))} · заказов {row['cnt']} · средний {format_money(avg_amount)}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("categories", "category_stats", "catstats"))
@only_admin
async def cmd_categories(message: Message) -> None:
    with db_connect() as con:
        rows = con.execute(
            """
            SELECT COALESCE(NULLIF(category, ''), 'Другое') AS category,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(amount), 0) AS total,
                   COALESCE(AVG(NULLIF(amount, 0)), 0) AS avg_amount
            FROM orders
            WHERE status = 'published'
            GROUP BY COALESCE(NULLIF(category, ''), 'Другое')
            ORDER BY total DESC
            """
        ).fetchall()
    if not rows:
        await message.answer("Пока нет опубликованных заказов для статистики по категориям.")
        return
    lines = ["🏷 <b>Категории заказов</b>", ""]
    for row in rows:
        avg_amount = int(float(row["avg_amount"] or 0))
        lines.append(
            f"<b>{html.escape(row['category'])}</b>: {format_money(int(row['total'] or 0))} · заказов {row['cnt']} · средний {format_money(avg_amount)}"
        )
    lines.extend(["", "Категории можно менять в Railway через <code>ORDER_CATEGORIES</code>."])
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("export"))
@only_admin
async def cmd_export(message: Message) -> None:
    os.makedirs("data", exist_ok=True)
    export_path = f"data/orders_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with db_connect() as con:
        rows = con.execute("SELECT * FROM orders ORDER BY id ASC").fetchall()
    with open(export_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["id", "status", "amount", "currency", "title", "category", "note", "source", "created_at", "published_at", "channel_message_id"])
        for row in rows:
            writer.writerow([
                row["id"], row["status"], row["amount"], row["currency"], row["title"], row["category"], row["note"],
                row["source"], row["created_at"], row["published_at"], row["channel_message_id"],
            ])
    await message.answer_document(FSInputFile(export_path), caption="Готово, выгрузка заказов в CSV ✅")


@router.message(Command("backup"))
@only_admin
async def cmd_backup(message: Message) -> None:
    if not os.path.exists(settings.db_path):
        await message.answer("База пока не создана.")
        return
    await message.answer_document(FSInputFile(settings.db_path), caption="Бэкап базы SQLite ✅")


# ========================
# Edit states
# ========================

@router.message(EditOrder.waiting_amount)
@only_admin
async def edit_wait_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    order_id = int(data["order_id"])
    amount = normalize_amount(message.text or "")
    if amount <= 0:
        await message.answer("Сумма должна быть числом. Например: <code>3000</code>", parse_mode=ParseMode.HTML)
        return
    update_order(order_id, amount=amount)
    await state.clear()
    await send_draft_preview(bot, message.chat.id, order_id, "Сумму обновил ✅")


@router.message(EditOrder.waiting_title)
@only_admin
async def edit_wait_title(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    order_id = int(data["order_id"])
    title = clean_title(message.text or "")
    update_order(order_id, title=title, category=detect_category(title))
    await state.clear()
    await send_draft_preview(bot, message.chat.id, order_id, "Название обновил ✅")


@router.message(EditOrder.waiting_note)
@only_admin
async def edit_wait_note(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    order_id = int(data["order_id"])
    note = (message.text or "").strip()
    if note == "-":
        note = ""
    update_order(order_id, note=note or None)
    await state.clear()
    await send_draft_preview(bot, message.chat.id, order_id, "Комментарий обновил ✅")


@router.message(EditOrder.waiting_category)
@only_admin
async def edit_wait_category(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    order_id = int(data["order_id"])
    category = clean_category(message.text or "Другое")
    update_order(order_id, category=category)
    await state.clear()
    await send_draft_preview(bot, message.chat.id, order_id, "Категорию обновил ✅")


# ========================
# Callbacks
# ========================

@router.callback_query(F.data.startswith("preview:"))
async def cb_preview(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await edit_or_send_preview(callback, order_id)


@router.callback_query(F.data.startswith("publish:"))
async def cb_publish(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    msg_id = await publish_order(bot, order_id)
    if not msg_id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await callback.answer("Опубликовано")
    row = get_order(order_id)
    if callback.message and row:
        await callback.message.edit_text(
            "✅ <b>Опубликовано в канал</b>\n\n" + build_post_text(row),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    update_order(order_id, status="skipped")
    await callback.answer("Пропущено")
    if callback.message:
        await callback.message.edit_text("🗑 Черновик пропущен", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("delete:"))
async def cb_delete_order(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return

    order_id = int(callback.data.split(":", 1)[1])
    row = get_order(order_id)
    if not row:
        await callback.answer("Заказ уже удалён")
        if callback.message:
            await callback.message.edit_text("🗑 Заказ уже удалён")
        return

    await try_delete_channel_message(bot, row)
    delete_order_record(order_id)
    await callback.answer("Удалено")
    if callback.message:
        await callback.message.edit_text(f"🗑 Заказ №{order_id:06d} удалён из базы", parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "reset_orders_cancel")
async def cb_reset_orders_cancel(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_text("Сброс заказов отменён.")


@router.callback_query(F.data == "reset_orders_confirm")
async def cb_reset_orders_confirm(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rows = get_recent_orders(30)
    for row in rows:
        await try_delete_channel_message(bot, row)

    reset_orders_table()
    await callback.answer("Сброшено")
    if callback.message:
        await callback.message.edit_text(
            "✅ Все заказы удалены из базы. Нумерация сброшена.\n\n"
            "Следующий заказ будет <b>№000001</b>.",
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data.startswith("edit_amount:"))
async def cb_edit_amount(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await state.set_state(EditOrder.waiting_amount)
    await state.update_data(order_id=order_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(f"Введи новую сумму для заказа №{order_id:06d}:", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("edit_title:"))
async def cb_edit_title(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await state.set_state(EditOrder.waiting_title)
    await state.update_data(order_id=order_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(f"Введи новое название услуги для заказа №{order_id:06d}:", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("edit_note:"))
async def cb_edit_note(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await state.set_state(EditOrder.waiting_note)
    await state.update_data(order_id=order_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введи новый комментарий. Чтобы убрать комментарий, отправь <code>-</code>.", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("edit_category:"))
async def cb_edit_category(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Выбери категорию для заказа №{order_id:06d}:",
            parse_mode=ParseMode.HTML,
            reply_markup=category_keyboard(order_id),
        )


@router.callback_query(F.data.startswith("set_category:"))
async def cb_set_category(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, order_id_raw, idx_raw = callback.data.split(":", 2)
    order_id = int(order_id_raw)
    idx = int(idx_raw)
    if idx < 0 or idx >= len(settings.categories):
        await callback.answer("Категория не найдена", show_alert=True)
        return
    update_order(order_id, category=settings.categories[idx])
    await edit_or_send_preview(callback, order_id, "Категорию обновил ✅")


@router.callback_query(F.data.startswith("custom_category:"))
async def cb_custom_category(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await state.set_state(EditOrder.waiting_category)
    await state.update_data(order_id=order_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Напиши свою категорию, например: <code>Боты для бизнеса</code>", parse_mode=ParseMode.HTML)


# ========================
# IMAP / Kwork parsing
# ========================

def email_body_to_text(msg: EmailMessage) -> str:
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        return ""
    content = body_part.get_content()
    if body_part.get_content_type() == "text/html":
        content = re.sub(r"<br\s*/?>", "\n", content, flags=re.I)
        content = re.sub(r"</p>", "\n", content, flags=re.I)
        content = re.sub(r"<[^>]+>", " ", content)
        content = html.unescape(content)
    content = re.sub(r"\r", "", content)
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def looks_like_completed_kwork(subject: str, sender: str, body: str) -> bool:
    haystack = f"{subject}\n{sender}\n{body}".lower()
    if settings.kwork_sender_filter and settings.kwork_sender_filter not in sender.lower() and settings.kwork_sender_filter not in haystack:
        return False
    if any(keyword in haystack for keyword in settings.kwork_ignore_keywords):
        return False
    return any(keyword in haystack for keyword in settings.kwork_success_keywords)


def extract_amount(text: str) -> int:
    patterns = [
        r"(?:сумма|стоимость|доход|оплата|заработок|итого)\D{0,40}([0-9][0-9\s.,]{1,15})\s*(?:₽|руб\.?|р\.?|rub)",
        r"([0-9][0-9\s.,]{1,15})\s*(?:₽|руб\.?|р\.?|rub)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = normalize_amount(match.group(1))
            if value > 0:
                return value
    return 0


def extract_title(subject: str, body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    patterns = [
        r"^(?:заказ|кворк|услуга|название заказа)\s*[:№-]\s*(.+)$",
        r"^(.{8,120})$",
    ]
    for line in lines[:30]:
        match = re.search(patterns[0], line, flags=re.I)
        if match:
            candidate = clean_title(match.group(1))
            if len(candidate) >= 5:
                return candidate
    return clean_title(subject) or "Заказ на Kwork"


def parse_email_order(uid: str, raw: bytes) -> Optional[dict]:
    msg = message_from_bytes(raw, policy=default)
    subject = str(msg.get("subject", "") or "")
    sender = str(msg.get("from", "") or "")
    body = email_body_to_text(msg)

    if not looks_like_completed_kwork(subject, sender, body):
        return None

    amount = extract_amount(f"{subject}\n{body}")
    title = extract_title(subject, body)
    note = "Автоматически найдено по уведомлению Kwork"
    return {
        "source": "kwork_email",
        "source_uid": f"imap:{settings.imap_user}:{uid}",
        "amount": amount,
        "title": title,
        "category": detect_category(title, body),
        "note": note,
        "raw_subject": subject,
        "raw_from": sender,
    }


def imap_fetch_new_emails() -> list[tuple[str, bytes]]:
    if not settings.imap_user or not settings.imap_password:
        raise RuntimeError("Не указаны IMAP_USER или IMAP_PASSWORD")

    with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as imap:
        imap.login(settings.imap_user, settings.imap_password)
        typ, _ = imap.select(settings.imap_folder)
        if typ != "OK":
            raise RuntimeError(f"Не удалось открыть папку IMAP: {settings.imap_folder}")

        typ, data = imap.uid("search", None, "ALL")
        if typ != "OK":
            raise RuntimeError("IMAP SEARCH вернул ошибку")
        uids = [uid.decode() for uid in data[0].split()] if data and data[0] else []
        if not uids:
            return []

        last_uid_raw = get_state("imap_last_uid")
        if last_uid_raw is None and settings.email_skip_old_on_first_run:
            set_state("imap_last_uid", uids[-1])
            return []

        last_uid = int(last_uid_raw or "0")
        new_uids = [uid for uid in uids if int(uid) > last_uid]
        if not new_uids:
            return []

        fetched: list[tuple[str, bytes]] = []
        for uid in new_uids[:50]:
            typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            for part in msg_data:
                if isinstance(part, tuple) and part[1]:
                    fetched.append((uid, part[1]))
                    break
        set_state("imap_last_uid", new_uids[-1])
        return fetched


async def check_email_once(bot: Bot, manual: bool = False) -> int:
    fetched = await asyncio.to_thread(imap_fetch_new_emails)
    found = 0
    for uid, raw in fetched:
        parsed = parse_email_order(uid, raw)
        if not parsed:
            continue
        order_id = insert_order(
            source=parsed["source"],
            source_uid=parsed["source_uid"],
            amount=parsed["amount"],
            title=parsed["title"],
            category=parsed["category"],
            note=parsed["note"],
            raw_subject=parsed["raw_subject"],
            raw_from=parsed["raw_from"],
            status="draft",
        )
        if not order_id:
            continue
        found += 1

        if settings.auto_publish:
            await publish_order(bot, order_id)
            await notify_admins(bot, f"✅ Заказ из Kwork автоматически опубликован. ID: <b>{order_id}</b>")
        else:
            row = get_order(order_id)
            if row:
                await notify_admins(bot, "🧾 <b>Найден выполненный заказ из Kwork</b>\n\n" + build_draft_preview(row), reply_markup=draft_keyboard(order_id))
    return found


async def email_watcher(bot: Bot) -> None:
    if not settings.email_enabled:
        return
    await notify_admins(bot, "📬 Проверка почты Kwork включена.")
    while True:
        try:
            await check_email_once(bot)
        except Exception as exc:
            await notify_admins(bot, f"⚠️ Ошибка проверки почты: <code>{html.escape(str(exc))}</code>")
        await asyncio.sleep(settings.email_check_interval)


@router.message(Command("checkmail"))
@only_admin
async def cmd_checkmail(message: Message, bot: Bot) -> None:
    if not settings.email_enabled:
        await message.answer("Проверка почты выключена. Поставь <code>EMAIL_ENABLED=true</code> в Railway Variables", parse_mode=ParseMode.HTML)
        return
    await message.answer("Проверяю почту…")
    try:
        count = await check_email_once(bot, manual=True)
    except Exception as exc:
        await message.answer(f"Ошибка проверки почты: <code>{html.escape(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return
    await message.answer(f"Готово. Новых выполненных заказов найдено: <b>{count}</b>", parse_mode=ParseMode.HTML)


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="помощь и список команд"),
        BotCommand(command="done", description="добавить выполненный заказ"),
        BotCommand(command="quickdone", description="сразу опубликовать заказ"),
        BotCommand(command="drafts", description="черновики и предпросмотр"),
        BotCommand(command="orders", description="последние заказы"),
        BotCommand(command="stats", description="статистика и заработок"),
        BotCommand(command="months", description="статистика по месяцам"),
        BotCommand(command="categories", description="статистика по категориям"),
        BotCommand(command="goal", description="цель на месяц"),
        BotCommand(command="export", description="выгрузить CSV"),
        BotCommand(command="backup", description="скачать базу"),
        BotCommand(command="checkmail", description="проверить почту сейчас"),
        BotCommand(command="reset", description="сбросить заказы и нумерацию"),
        BotCommand(command="whoami", description="показать мой Telegram ID"),
        BotCommand(command="version", description="проверка версии"),
    ])


async def main() -> None:
    init_db()
    bot = Bot(settings.bot_token)
    await setup_bot_commands(bot)
    print(
        f"Starting OrderDone Bot {BUILD_VERSION}, db={settings.db_path}, email_enabled={settings.email_enabled}, "
        f"admins={sorted(settings.admin_ids)}, allow_all={settings.allow_all_users}",
        flush=True,
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    if settings.email_enabled:
        asyncio.create_task(email_watcher(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
