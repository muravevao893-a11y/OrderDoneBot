import asyncio
import base64
import csv
import hashlib
import html
import imaplib
import inspect
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import message_from_bytes
from email.message import Message as EmailMessage
from email.policy import default
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

BUILD_VERSION = "saas-v2-clean-ui-2026-05-02"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "да", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            pass
    return result


def split_csv(raw: str, default: Optional[list[str]] = None) -> list[str]:
    if not raw:
        return default or []
    return [x.strip() for x in raw.split(",") if x.strip()]


def now_utc() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now_utc()).isoformat()


def parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def fmt_date(raw: Optional[str]) -> str:
    dt = parse_dt(raw)
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y")


def rub(amount: int, currency: str = "₽") -> str:
    return f"{amount:,}".replace(",", " ") + f" {currency}"


def money_to_int(raw: str) -> int:
    cleaned = re.sub(r"[^0-9]", "", raw or "")
    return int(cleaned) if cleaned else 0


def month_key(dt: Optional[datetime] = None) -> str:
    return (dt or now_utc()).strftime("%Y-%m")


OWNER_IDS = parse_ids(os.getenv("OWNER_IDS", "")) | parse_ids(os.getenv("ADMIN_IDS", ""))
PUBLIC_MODE = env_bool("PUBLIC_MODE", True)
DB_PATH = os.getenv("DB_PATH", "data/orders.db")
TRIAL_DAYS = env_int("TRIAL_DAYS", 7)
DEFAULT_PLAN = os.getenv("DEFAULT_PLAN", "trial")
PLANS = split_csv(os.getenv("PLANS", "trial,starter,pro,business"))
PLAN_PRICES_RAW = os.getenv("PLAN_PRICES", "starter=299,pro=599,business=999")
SUBSCRIPTION_CONTACT_URL = os.getenv("SUBSCRIPTION_CONTACT_URL", "")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "₽")
DEFAULT_CONTACT_BUTTON_TEXT = os.getenv("DEFAULT_CONTACT_BUTTON_TEXT", "Связаться")
DEFAULT_CATEGORIES = split_csv(
    os.getenv("DEFAULT_ORDER_CATEGORIES", "Telegram-боты,Парсеры,Автоматизация,GPT-боты,Сайты,Доработки,Другое")
)
EMAIL_CHECK_INTERVAL = env_int("EMAIL_CHECK_INTERVAL", 60)
EMAIL_MAX_PER_USER = env_int("EMAIL_MAX_PER_USER", 10)
EMAIL_SKIP_OLD_ON_FIRST_RUN = env_bool("EMAIL_SKIP_OLD_ON_FIRST_RUN", True)
DEFAULT_IMAP_HOST = os.getenv("DEFAULT_IMAP_HOST", "imap.gmail.com")
DEFAULT_IMAP_PORT = env_int("DEFAULT_IMAP_PORT", 993)
DEFAULT_IMAP_FOLDER = os.getenv("DEFAULT_IMAP_FOLDER", "INBOX")
DEFAULT_KWORK_SENDER_FILTER = os.getenv("DEFAULT_KWORK_SENDER_FILTER", "kwork")
SUCCESS_KEYWORDS = split_csv(os.getenv("KWORK_SUCCESS_KEYWORDS", "заказ выполнен,заказ завершен,работа принята"))
IGNORE_KEYWORDS = split_csv(os.getenv("KWORK_IGNORE_KEYWORDS", "новый заказ,заказ отменен,доработка"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Add BOT_TOKEN to Railway Variables or .env")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


# -------------------- encryption --------------------

def get_fernet() -> Fernet:
    key = os.getenv("DATA_SECRET_KEY", "").strip()
    if not key:
        key_path = Path("data/secret.key")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            key = key_path.read_text().strip()
        else:
            key = Fernet.generate_key().decode()
            key_path.write_text(key)
    return Fernet(key.encode() if isinstance(key, str) else key)


FERNET = get_fernet()


def encrypt_secret(value: str) -> str:
    return FERNET.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return FERNET.decrypt(value.encode()).decode()


# -------------------- database --------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_blocked INTEGER NOT NULL DEFAULT 0,
                plan TEXT NOT NULL DEFAULT 'trial',
                trial_until TEXT,
                paid_until TEXT,
                monthly_goal INTEGER NOT NULL DEFAULT 0,
                channel_id TEXT,
                contact_url TEXT,
                contact_button_text TEXT NOT NULL DEFAULT 'Связаться',
                currency TEXT NOT NULL DEFAULT '₽',
                auto_publish INTEGER NOT NULL DEFAULT 0,
                manual_preview INTEGER NOT NULL DEFAULT 1,
                categories TEXT,
                imap_host TEXT,
                imap_port INTEGER,
                imap_user TEXT,
                imap_password_enc TEXT,
                imap_folder TEXT,
                email_enabled INTEGER NOT NULL DEFAULT 0,
                email_skip_old INTEGER NOT NULL DEFAULT 1,
                mail_initialized INTEGER NOT NULL DEFAULT 0,
                sender_filter TEXT,
                success_keywords TEXT,
                ignore_keywords TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                public_number INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                amount INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT '₽',
                service TEXT NOT NULL DEFAULT 'Заказ',
                category TEXT NOT NULL DEFAULT 'Другое',
                comment TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                channel_id TEXT,
                channel_message_id INTEGER,
                email_uid TEXT,
                email_subject TEXT,
                dedupe_key TEXT,
                created_at TEXT NOT NULL,
                published_at TEXT,
                skipped_at TEXT,
                deleted_at TEXT,
                UNIQUE(user_id, dedupe_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_emails (
                user_id INTEGER NOT NULL,
                mailbox TEXT NOT NULL,
                uid TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, mailbox, uid)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER,
                action TEXT NOT NULL,
                days INTEGER,
                plan TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def plan_prices() -> dict[str, int]:
    result: dict[str, int] = {}
    for item in split_csv(PLAN_PRICES_RAW):
        if "=" not in item:
            continue
        name, price = item.split("=", 1)
        result[name.strip()] = money_to_int(price)
    return result


def ensure_user(message_or_user: Any) -> sqlite3.Row:
    user = getattr(message_or_user, "from_user", message_or_user)
    tg_id = int(user.id)
    username = getattr(user, "username", None)
    first_name = getattr(user, "first_name", None)
    is_owner = tg_id in OWNER_IDS
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if row is None:
            trial_until = None if is_owner else iso(now_utc() + timedelta(days=TRIAL_DAYS))
            conn.execute(
                """
                INSERT INTO users (
                    tg_id, username, first_name, created_at, role, plan, trial_until,
                    contact_button_text, currency, auto_publish, manual_preview, categories,
                    imap_host, imap_port, imap_folder, email_skip_old, sender_filter,
                    success_keywords, ignore_keywords, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tg_id,
                    username,
                    first_name,
                    iso(),
                    "owner" if is_owner else "user",
                    "owner" if is_owner else DEFAULT_PLAN,
                    trial_until,
                    DEFAULT_CONTACT_BUTTON_TEXT,
                    DEFAULT_CURRENCY,
                    1 if env_bool("DEFAULT_AUTO_PUBLISH", False) else 0,
                    1 if env_bool("DEFAULT_MANUAL_PREVIEW", True) else 0,
                    ",".join(DEFAULT_CATEGORIES),
                    DEFAULT_IMAP_HOST,
                    DEFAULT_IMAP_PORT,
                    DEFAULT_IMAP_FOLDER,
                    1 if EMAIL_SKIP_OLD_ON_FIRST_RUN else 0,
                    DEFAULT_KWORK_SENDER_FILTER,
                    ",".join(SUCCESS_KEYWORDS),
                    ",".join(IGNORE_KEYWORDS),
                    iso(),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        else:
            conn.execute(
                "UPDATE users SET username=?, first_name=?, updated_at=? WHERE tg_id=?",
                (username, first_name, iso(), tg_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    return row


def get_user(tg_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def user_is_owner(tg_id: int) -> bool:
    return tg_id in OWNER_IDS or (get_user(tg_id) and get_user(tg_id)["role"] == "owner")


def is_sub_active(row: sqlite3.Row) -> bool:
    if row["tg_id"] in OWNER_IDS or row["role"] == "owner":
        return True
    if row["is_blocked"]:
        return False
    current = now_utc()
    trial_until = parse_dt(row["trial_until"])
    paid_until = parse_dt(row["paid_until"])
    return bool((trial_until and trial_until >= current) or (paid_until and paid_until >= current))


def subscription_text(row: sqlite3.Row) -> str:
    active = is_sub_active(row)
    trial_until = parse_dt(row["trial_until"])
    paid_until = parse_dt(row["paid_until"])
    lines = [
        f"План: <b>{html.escape(row['plan'] or 'free')}</b>",
        f"Статус: {'✅ активна' if active else '❌ не активна'}",
    ]
    if trial_until:
        lines.append(f"Trial до: <b>{trial_until.strftime('%d.%m.%Y')}</b>")
    if paid_until:
        lines.append(f"Оплачено до: <b>{paid_until.strftime('%d.%m.%Y')}</b>")
    if row["tg_id"] in OWNER_IDS:
        lines.append("Ты владелец, подписка не требуется.")
    return "\n".join(lines)


def next_public_number(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT COALESCE(MAX(public_number), 0) + 1 AS n FROM orders WHERE user_id=?", (user_id,)).fetchone()
        return int(row["n"])


def create_order(
    user_id: int,
    amount: int,
    service: str,
    category: str = "Другое",
    comment: str = "",
    source: str = "manual",
    status: str = "draft",
    email_uid: Optional[str] = None,
    email_subject: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> sqlite3.Row:
    user = get_user(user_id)
    currency = user["currency"] if user else DEFAULT_CURRENCY
    if not dedupe_key:
        base = f"{user_id}:{amount}:{service}:{category}:{comment}:{source}:{email_uid}:{email_subject}"
        dedupe_key = hashlib.sha256(base.encode()).hexdigest()[:32]
    with db() as conn:
        n = next_public_number(user_id)
        try:
            conn.execute(
                """
                INSERT INTO orders (
                    user_id, public_number, source, amount, currency, service, category,
                    comment, status, email_uid, email_subject, dedupe_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, n, source, amount, currency, service, category, comment, status, email_uid, email_subject, dedupe_key, iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            existing = conn.execute("SELECT * FROM orders WHERE user_id=? AND dedupe_key=?", (user_id, dedupe_key)).fetchone()
            if existing:
                return existing
            raise
        return conn.execute("SELECT * FROM orders WHERE user_id=? AND dedupe_key=?", (user_id, dedupe_key)).fetchone()


def get_order(order_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def update_order(order_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = list(fields.keys())
    values = [fields[k] for k in keys]
    sql = ", ".join([f"{k}=?" for k in keys])
    with db() as conn:
        conn.execute(f"UPDATE orders SET {sql} WHERE id=?", (*values, order_id))
        conn.commit()


# -------------------- Telegram UI --------------------
router = Router()


class EditOrderState(StatesGroup):
    waiting_value = State()


class SetupState(StatesGroup):
    waiting_channel = State()
    waiting_contact = State()
    waiting_email = State()


def safe_handler(fn):
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        sig = inspect.signature(fn)
        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return await fn(*args, **filtered)
    return wrapper


def private_access(fn):
    @wraps(fn)
    async def wrapper(event: Message | CallbackQuery, *args, **kwargs):
        from_user = event.from_user
        row = ensure_user(from_user)
        if row["is_blocked"]:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ заблокирован", show_alert=True)
            else:
                await event.answer("🔒 Доступ заблокирован.")
            return None
        if not PUBLIC_MODE and from_user.id not in OWNER_IDS:
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            else:
                await event.answer("🔒 Нет доступа.")
            return None
        return await fn(event, *args, **kwargs)
    return safe_handler(wrapper)


def active_required(fn):
    @wraps(fn)
    async def wrapper(event: Message | CallbackQuery, *args, **kwargs):
        row = ensure_user(event.from_user)
        if not is_sub_active(row):
            text = (
                "❌ Подписка не активна.\n\n"
                f"{subscription_text(row)}\n\n"
                "Команда /plans покажет тарифы и способ подключения."
            )
            if isinstance(event, CallbackQuery):
                await event.answer("Подписка не активна", show_alert=True)
                await event.message.answer(text, parse_mode=ParseMode.HTML)
            else:
                await event.answer(text, parse_mode=ParseMode.HTML)
            return None
        return await fn(event, *args, **kwargs)
    return private_access(wrapper)


def owner_required(fn):
    @wraps(fn)
    async def wrapper(event: Message | CallbackQuery, *args, **kwargs):
        if event.from_user.id not in OWNER_IDS:
            if isinstance(event, CallbackQuery):
                await event.answer("Только владелец", show_alert=True)
            else:
                await event.answer("🔒 Команда только для владельца бота.")
            return None
        return await fn(event, *args, **kwargs)
    return private_access(wrapper)


def post_keyboard(row: sqlite3.Row) -> Optional[InlineKeyboardMarkup]:
    user = get_user(row["user_id"])
    url = user["contact_url"] if user else None
    if not url:
        return None
    text = user["contact_button_text"] if user else DEFAULT_CONTACT_BUTTON_TEXT
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def preview_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Опубликовать", callback_data=f"pub:{order_id}")],
            [
                InlineKeyboardButton(text="Сумма", callback_data=f"edit:amount:{order_id}"),
                InlineKeyboardButton(text="Услуга", callback_data=f"edit:service:{order_id}"),
            ],
            [
                InlineKeyboardButton(text="Категория", callback_data=f"edit:category:{order_id}"),
                InlineKeyboardButton(text="Комментарий", callback_data=f"edit:comment:{order_id}"),
            ],
            [InlineKeyboardButton(text="Пропустить", callback_data=f"skip:{order_id}")],
        ]
    )


def main_menu_keyboard(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Новый заказ", callback_data="menu:new_order"), InlineKeyboardButton(text="Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton(text="Настройки", callback_data="menu:settings"), InlineKeyboardButton(text="Подписка", callback_data="menu:subscription")],
        [InlineKeyboardButton(text="Помощь", callback_data="menu:help")],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton(text="Админ", callback_data="menu:owner")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def render_post(row: sqlite3.Row) -> str:
    service = html.escape(row["service"] or "Заказ")
    comment = html.escape(row["comment"] or "")
    date = datetime.fromisoformat(row["created_at"]).strftime("%d.%m.%Y") if row["created_at"] else now_utc().strftime("%d.%m.%Y")
    lines = [
        "✅ <b>Заказ выполнен</b>",
        "",
        f"💰 {rub(int(row['amount']), row['currency'])}",
        f"🛠 {service}",
        f"📅 {date}",
        f"№{int(row['public_number']):06d}",
    ]
    if comment:
        lines += ["", f"<i>{comment}</i>"]
    return "\n".join(lines)
def render_preview(row: sqlite3.Row) -> str:
    return "<b>Предпросмотр</b>\n\n" + render_post(row)

async def publish_order(bot: Bot, order_id: int, notify_user: bool = True) -> tuple[bool, str]:
    row = get_order(order_id)
    if not row:
        return False, "Заказ не найден."
    user = get_user(row["user_id"])
    if not user:
        return False, "Пользователь не найден."
    if int(row["amount"] or 0) <= 0:
        return False, "Сумма заказа не распознана. Нажми «Сумма» в предпросмотре и укажи её вручную."
    if not user["channel_id"]:
        return False, "Сначала укажи канал командой /set_channel @channel"
    if not is_sub_active(user):
        return False, "Подписка не активна."
    try:
        msg = await bot.send_message(
            chat_id=user["channel_id"],
            text=render_post(row),
            parse_mode=ParseMode.HTML,
            reply_markup=post_keyboard(row),
            disable_web_page_preview=True,
        )
        update_order(
            order_id,
            status="published",
            channel_id=str(user["channel_id"]),
            channel_message_id=msg.message_id,
            published_at=iso(),
        )
        if notify_user:
            await bot.send_message(user["tg_id"], f"✅ Опубликовано: заказ №{int(row['public_number']):06d}")
        return True, "Опубликовано."
    except Exception as e:
        return False, f"Не получилось опубликовать. Проверь, что бот админ канала. Ошибка: {e}"


def parse_done_args(text: str) -> tuple[int, str, str]:
    raw = text.strip()
    if not raw:
        return 0, "Заказ", ""
    # /done 3000 | service | comment
    parts = [p.strip() for p in raw.split("|")]
    first = parts[0]
    match = re.match(r"^(\d[\d\s.,]*)\s*(.*)$", first)
    if match:
        amount = money_to_int(match.group(1))
        service = match.group(2).strip() or (parts[1] if len(parts) > 1 else "Заказ")
    else:
        amount = 0
        service = first or "Заказ"
    comment = ""
    if len(parts) >= 2 and match and not match.group(2).strip():
        service = parts[1] or service
        comment = parts[2] if len(parts) >= 3 else ""
    elif len(parts) >= 2:
        comment = parts[1]
    if len(parts) >= 3:
        comment = parts[2]
    return amount, service, comment


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Меню"),
        BotCommand(command="menu", description="Меню"),
        BotCommand(command="done", description="Новый заказ"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="setup", description="Быстрая настройка"),
        BotCommand(command="subscription", description="Подписка"),
        BotCommand(command="version", description="Версия"),
    ]
    await bot.set_my_commands(commands)

@router.message(Command("start", "menu"))
@private_access
async def cmd_start(message: Message):
    row = ensure_user(message)
    text = (
        "<b>Kwork Proof</b>\n"
        f"v{BUILD_VERSION}\n\n"
        f"{subscription_text(row)}\n\n"
        "<code>/done 3000 | Услуга | Комментарий</code>\n"
        "<code>/setup</code> — настройка\n"
        "<code>/stats</code> — статистика"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=main_menu_keyboard(message.from_user.id in OWNER_IDS))
@router.message(Command("version"))
@private_access
async def cmd_version(message: Message):
    await message.answer(f"Версия: <b>{BUILD_VERSION}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("whoami"))
@private_access
async def cmd_whoami(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("menu:"))
@private_access
async def cb_menu(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    row = ensure_user(callback.from_user)
    if action == "new_order":
        text = "<b>Новый заказ</b>\n\n<code>/done 3000 | Услуга | Комментарий</code>"
    elif action == "stats":
        st = stats_for_user(callback.from_user.id)
        goal = int(row["monthly_goal"] or 0)
        progress = round(st["month_total"] / goal * 100, 1) if goal else 0
        text = (
            "<b>Статистика</b>\n\n"
            f"Всего: <b>{rub(st['total'], row['currency'])}</b>\n"
            f"Заказов: <b>{st['count']}</b>\n"
            f"Средний чек: <b>{rub(st['avg'], row['currency'])}</b>\n"
            f"Месяц: <b>{rub(st['month_total'], row['currency'])}</b> · {st['month_count']}"
        )
        if goal:
            text += f"\nЦель: <b>{rub(goal, row['currency'])}</b> · {progress}%"
    elif action == "settings":
        text = (
            "<b>Настройки</b>\n\n"
            f"Канал: <code>{html.escape(str(row['channel_id'] or '—'))}</code>\n"
            f"Кнопка: <b>{html.escape(row['contact_button_text'] or DEFAULT_CONTACT_BUTTON_TEXT)}</b>\n"
            f"Почта: <code>{html.escape(str(row['imap_user'] or '—'))}</code>\n"
            f"Проверка: <b>{'вкл' if row['email_enabled'] else 'выкл'}</b>\n"
            f"Автопубликация: <b>{'вкл' if row['auto_publish'] else 'выкл'}</b>\n\n"
            "<code>/setup</code>"
        )
    elif action == "subscription":
        text = "<b>Подписка</b>\n\n" + subscription_text(row)
    elif action == "owner" and callback.from_user.id in OWNER_IDS:
        text = "<b>Админ</b>\n\n<code>/users</code>\n<code>/app_stats</code>\n<code>/grant USER_ID 30 pro</code>\n<code>/revoke USER_ID</code>"
    else:
        text = (
            "<b>Помощь</b>\n\n"
            "<code>/done 3000 | Услуга | Комментарий</code>\n"
            "<code>/orders</code>\n"
            "<code>/stats</code>\n"
            "<code>/settings</code>\n"
            "<code>/setup</code>"
        )
    await callback.message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await callback.answer()


@router.message(Command("setup"))
@private_access
async def cmd_setup(message: Message):
    text = (
        "<b>Настройка</b>\n\n"
        "1. Добавь бота админом в канал.\n"
        "2. Укажи канал:\n<code>/set_channel @channel</code>\n\n"
        "3. Укажи кнопку:\n<code>/set_contact https://t.me/username Связаться</code>\n\n"
        "4. Подключи почту:\n<code>/set_email mail@gmail.com APP_PASSWORD</code>\n\n"
        "5. Включи проверку:\n<code>/enable_email</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
@router.message(Command("settings"))
@private_access
async def cmd_settings(message: Message):
    row = ensure_user(message)
    text = (
        "<b>Настройки</b>\n\n"
        f"Канал: <code>{html.escape(str(row['channel_id'] or '—'))}</code>\n"
        f"Кнопка: <b>{html.escape(row['contact_button_text'] or DEFAULT_CONTACT_BUTTON_TEXT)}</b>\n"
        f"Ссылка: <code>{html.escape(str(row['contact_url'] or '—'))}</code>\n"
        f"Валюта: <b>{html.escape(row['currency'] or DEFAULT_CURRENCY)}</b>\n"
        f"Почта: <code>{html.escape(str(row['imap_user'] or '—'))}</code>\n"
        f"Проверка: <b>{'вкл' if row['email_enabled'] else 'выкл'}</b>\n"
        f"Автопубликация: <b>{'вкл' if row['auto_publish'] else 'выкл'}</b>\n"
        f"Цель: <b>{rub(int(row['monthly_goal']), row['currency']) if row['monthly_goal'] else '—'}</b>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
@router.message(Command("set_channel"))
@private_access
async def cmd_set_channel(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Пример: <code>/set_channel @my_channel</code>", parse_mode=ParseMode.HTML)
        return
    with db() as conn:
        conn.execute("UPDATE users SET channel_id=?, updated_at=? WHERE tg_id=?", (arg, iso(), message.from_user.id))
        conn.commit()
    await message.answer("✅ Канал сохранён. Проверь, что бот добавлен админом в этот канал.")


@router.message(Command("test_channel"))
@active_required
async def cmd_test_channel(message: Message, bot: Bot):
    row = ensure_user(message)
    if not row["channel_id"]:
        await message.answer("Сначала укажи канал: /set_channel @channel")
        return
    try:
        await bot.send_message(row["channel_id"], "✅ Тест: бот успешно подключён к каналу.")
        await message.answer("✅ Тестовое сообщение отправлено в канал.")
    except Exception as e:
        await message.answer(f"❌ Не получилось отправить. Проверь права админа. Ошибка: {e}")


@router.message(Command("set_contact"))
@private_access
async def cmd_set_contact(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args:
        await message.answer("Пример: <code>/set_contact https://t.me/username Связаться</code>", parse_mode=ParseMode.HTML)
        return
    parts = args.split(maxsplit=1)
    url = parts[0].strip()
    button = parts[1].strip() if len(parts) > 1 else DEFAULT_CONTACT_BUTTON_TEXT
    if not (url.startswith("https://") or url.startswith("http://") or url.startswith("tg://")):
        await message.answer("Ссылка должна начинаться с https://, http:// или tg://")
        return
    with db() as conn:
        conn.execute(
            "UPDATE users SET contact_url=?, contact_button_text=?, updated_at=? WHERE tg_id=?",
            (url, button, iso(), message.from_user.id),
        )
        conn.commit()
    await message.answer("✅ Кнопка под постами сохранена.")


@router.message(Command("set_email"))
@private_access
async def cmd_set_email(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Пример для Gmail:\n"
            "<code>/set_email your@gmail.com APP_PASSWORD</code>\n\n"
            "Для кастомного IMAP:\n"
            "<code>/set_email imap.mail.ru 993 user@mail.ru PASSWORD</code>\n\n"
            "Пароль хранится в базе в зашифрованном виде. После отправки команды можешь удалить сообщение у себя.",
            parse_mode=ParseMode.HTML,
        )
        return
    parts = args.split()
    if len(parts) == 2:
        host, port, email_user, password = DEFAULT_IMAP_HOST, DEFAULT_IMAP_PORT, parts[0], parts[1]
    elif len(parts) >= 4:
        host, port_raw, email_user, password = parts[0], parts[1], parts[2], " ".join(parts[3:])
        try:
            port = int(port_raw)
        except ValueError:
            await message.answer("IMAP port должен быть числом, обычно 993.")
            return
    else:
        await message.answer("Не понял формат. Напиши /set_email без аргументов, покажу примеры.")
        return
    enc = encrypt_secret(password)
    with db() as conn:
        conn.execute(
            """
            UPDATE users SET imap_host=?, imap_port=?, imap_user=?, imap_password_enc=?,
            imap_folder=?, email_enabled=0, mail_initialized=0, updated_at=? WHERE tg_id=?
            """,
            (host, port, email_user, enc, DEFAULT_IMAP_FOLDER, iso(), message.from_user.id),
        )
        conn.commit()
    await message.answer("✅ Почта сохранена. Теперь напиши /enable_email, чтобы включить проверку.")


@router.message(Command("enable_email"))
@active_required
async def cmd_enable_email(message: Message):
    row = ensure_user(message)
    if not row["imap_user"] or not row["imap_password_enc"]:
        await message.answer("Сначала: <code>/set_email mail@gmail.com APP_PASSWORD</code>", parse_mode=ParseMode.HTML)
        return
    with db() as conn:
        conn.execute("UPDATE users SET email_enabled=1, updated_at=? WHERE tg_id=?", (iso(), message.from_user.id))
        conn.commit()
    await message.answer("✅ Проверка почты включена.")
@router.message(Command("disable_email"))
@private_access
async def cmd_disable_email(message: Message):
    with db() as conn:
        conn.execute("UPDATE users SET email_enabled=0, updated_at=? WHERE tg_id=?", (iso(), message.from_user.id))
        conn.commit()
    await message.answer("Проверка почты выключена.")
@router.message(Command("autopublish"))
@private_access
async def cmd_autopublish(message: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in {"on", "off", "true", "false", "1", "0"}:
        await message.answer("Пример: <code>/autopublish on</code> или <code>/autopublish off</code>", parse_mode=ParseMode.HTML)
        return
    value = arg in {"on", "true", "1"}
    with db() as conn:
        conn.execute("UPDATE users SET auto_publish=?, updated_at=? WHERE tg_id=?", (1 if value else 0, iso(), message.from_user.id))
        conn.commit()
    await message.answer(f"Автопубликация: <b>{'вкл' if value else 'выкл'}</b>", parse_mode=ParseMode.HTML)
@router.message(Command("done"))
@active_required
async def cmd_done(message: Message, command: CommandObject, bot: Bot):
    row = ensure_user(message)
    amount, service, comment = parse_done_args(command.args or "")
    if amount <= 0:
        await message.answer("Пример: <code>/done 3000 | Telegram-бот | Сделан бот и инструкция</code>", parse_mode=ParseMode.HTML)
        return
    category = split_csv(row["categories"] or "")[:1]
    order = create_order(
        user_id=message.from_user.id,
        amount=amount,
        service=service,
        category=category[0] if category else "Другое",
        comment=comment,
        source="manual",
        status="draft" if row["manual_preview"] else "published",
    )
    if row["manual_preview"]:
        await message.answer(render_preview(order), parse_mode=ParseMode.HTML, reply_markup=preview_keyboard(order["id"]))
    else:
        ok, result = await publish_order(bot, order["id"])
        if not ok:
            await message.answer(result)


@router.message(Command("quickdone"))
@active_required
async def cmd_quickdone(message: Message, command: CommandObject, bot: Bot):
    amount, service, comment = parse_done_args(command.args or "")
    if amount <= 0:
        await message.answer("Пример: <code>/quickdone 3000 | Telegram-бот | Комментарий</code>", parse_mode=ParseMode.HTML)
        return
    row = ensure_user(message)
    category = split_csv(row["categories"] or "")[:1]
    order = create_order(message.from_user.id, amount, service, category[0] if category else "Другое", comment, source="manual", status="draft")
    ok, result = await publish_order(bot, order["id"])
    if not ok:
        await message.answer(result)


@router.callback_query(F.data.startswith("pub:"))
@active_required
async def cb_publish(callback: CallbackQuery, bot: Bot):
    order_id = int(callback.data.split(":", 1)[1])
    row = get_order(order_id)
    if not row or row["user_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    ok, result = await publish_order(bot, order_id, notify_user=False)
    await callback.answer(result, show_alert=not ok)
    if ok:
        await callback.message.edit_text(f"✅ Опубликовано\n\n{render_post(get_order(order_id))}", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("skip:"))
@private_access
async def cb_skip(callback: CallbackQuery):
    order_id = int(callback.data.split(":", 1)[1])
    row = get_order(order_id)
    if not row or row["user_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    update_order(order_id, status="skipped", skipped_at=iso())
    await callback.message.edit_text("🚫 Заказ пропущен.")
    await callback.answer("Пропущено")


@router.callback_query(F.data.startswith("edit:"))
@active_required
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    _, field, order_id_raw = callback.data.split(":", 2)
    order_id = int(order_id_raw)
    row = get_order(order_id)
    if not row or row["user_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await state.set_state(EditOrderState.waiting_value)
    await state.update_data(order_id=order_id, field=field)
    prompts = {
        "amount": "Введи новую сумму числом, например 5000",
        "service": "Введи новое название услуги",
        "category": "Введи категорию, например Telegram-боты",
        "comment": "Введи комментарий. Чтобы очистить, напиши -",
    }
    await callback.message.answer(prompts.get(field, "Введи новое значение"))
    await callback.answer()


@router.message(EditOrderState.waiting_value)
@private_access
async def edit_order_value(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = int(data["order_id"])
    field = data["field"]
    row = get_order(order_id)
    if not row or row["user_id"] != message.from_user.id:
        await state.clear()
        await message.answer("Заказ не найден.")
        return
    value = message.text.strip()
    updates: dict[str, Any] = {}
    if field == "amount":
        amount = money_to_int(value)
        if amount <= 0:
            await message.answer("Сумма должна быть больше 0.")
            return
        updates["amount"] = amount
    elif field == "comment" and value == "-":
        updates["comment"] = ""
    elif field in {"service", "category", "comment"}:
        updates[field] = value
    else:
        await message.answer("Неизвестное поле.")
        await state.clear()
        return
    update_order(order_id, **updates)
    await state.clear()
    row2 = get_order(order_id)
    await message.answer(render_preview(row2), parse_mode=ParseMode.HTML, reply_markup=preview_keyboard(order_id))


@router.message(Command("orders"))
@private_access
async def cmd_orders(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id=? AND status!='deleted' ORDER BY id DESC LIMIT 10",
            (message.from_user.id,),
        ).fetchall()
    if not rows:
        await message.answer("Заказов нет.")
        return
    lines = ["<b>Заказы</b>"]
    for r in rows:
        lines.append(f"№{int(r['public_number']):06d} · {rub(int(r['amount']), r['currency'])} · {html.escape(r['service'])} · {r['status']}")
    lines.append("\n<code>/del 2</code> — удалить")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
@router.message(Command("delete_order", "del"))
@private_access
async def cmd_delete_order(message: Message, command: CommandObject, bot: Bot):
    num = money_to_int(command.args or "")
    if not num:
        await message.answer("Пример: /delete_order 2")
        return
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE user_id=? AND public_number=? AND status!='deleted'",
            (message.from_user.id, num),
        ).fetchone()
    if not row:
        await message.answer("Заказ не найден.")
        return
    # Try to delete channel post if exists
    if row["channel_id"] and row["channel_message_id"]:
        try:
            await bot.delete_message(row["channel_id"], int(row["channel_message_id"]))
        except Exception:
            pass
    update_order(row["id"], status="deleted", deleted_at=iso())
    await message.answer(f"🗑 Заказ №{num:06d} удалён из статистики.")


@router.message(Command("reset_orders", "reset"))
@private_access
async def cmd_reset_orders(message: Message):
    with db() as conn:
        conn.execute("DELETE FROM orders WHERE user_id=?", (message.from_user.id,))
        conn.commit()
    await message.answer("🧹 Все твои заказы удалены. Следующий будет №000001.")


@router.message(Command("goal"))
@private_access
async def cmd_goal(message: Message, command: CommandObject):
    amount = money_to_int(command.args or "")
    if amount <= 0:
        await message.answer("Пример: /goal 100000")
        return
    with db() as conn:
        conn.execute("UPDATE users SET monthly_goal=?, updated_at=? WHERE tg_id=?", (amount, iso(), message.from_user.id))
        conn.commit()
    await message.answer(f"🎯 Цель месяца сохранена: {rub(amount)}")


def stats_for_user(user_id: int) -> dict[str, Any]:
    today = now_utc().strftime("%Y-%m-%d")
    current_month = month_key()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='published'",
            (user_id,),
        ).fetchall()
    amounts = [int(r["amount"]) for r in rows]
    today_amounts = [int(r["amount"]) for r in rows if (r["published_at"] or r["created_at"]).startswith(today)]
    month_amounts = [int(r["amount"]) for r in rows if (r["published_at"] or r["created_at"]).startswith(current_month)]
    return {
        "count": len(rows),
        "total": sum(amounts),
        "today_count": len(today_amounts),
        "today_total": sum(today_amounts),
        "month_count": len(month_amounts),
        "month_total": sum(month_amounts),
        "avg": round(sum(amounts) / len(amounts)) if amounts else 0,
        "max": max(amounts) if amounts else 0,
    }


@router.message(Command("stats", "earnings"))
@private_access
async def cmd_stats(message: Message):
    user = ensure_user(message)
    s = stats_for_user(message.from_user.id)
    goal = int(user["monthly_goal"] or 0)
    progress = round(s["month_total"] / goal * 100, 1) if goal else 0
    text = (
        "<b>Статистика</b>\n\n"
        f"Всего: <b>{rub(s['total'], user['currency'])}</b>\n"
        f"Заказов: <b>{s['count']}</b>\n"
        f"Средний чек: <b>{rub(s['avg'], user['currency'])}</b>\n"
        f"Максимум: <b>{rub(s['max'], user['currency'])}</b>\n\n"
        f"Сегодня: <b>{rub(s['today_total'], user['currency'])}</b> · {s['today_count']}\n"
        f"Месяц: <b>{rub(s['month_total'], user['currency'])}</b> · {s['month_count']}"
    )
    if goal:
        text += f"\n\nЦель: <b>{rub(goal, user['currency'])}</b> · {progress}%"
    await message.answer(text, parse_mode=ParseMode.HTML)
@router.message(Command("months"))
@private_access
async def cmd_months(message: Message):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT substr(COALESCE(published_at, created_at), 1, 7) AS m, COUNT(*) AS c, SUM(amount) AS s
            FROM orders WHERE user_id=? AND status='published'
            GROUP BY m ORDER BY m DESC LIMIT 12
            """,
            (message.from_user.id,),
        ).fetchall()
    if not rows:
        await message.answer("Пока пусто.")
        return
    user = ensure_user(message)
    lines = ["<b>Месяцы</b>"]
    for r in rows:
        lines.append(f"{r['m']} · {rub(int(r['s'] or 0), user['currency'])} · {r['c']}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
@router.message(Command("categories"))
@private_access
async def cmd_categories(message: Message):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS c, SUM(amount) AS s
            FROM orders WHERE user_id=? AND status='published'
            GROUP BY category ORDER BY s DESC
            """,
            (message.from_user.id,),
        ).fetchall()
    if not rows:
        await message.answer("Пока пусто.")
        return
    user = ensure_user(message)
    lines = ["<b>Категории</b>"]
    for r in rows:
        lines.append(f"{html.escape(r['category'] or 'Другое')} · {rub(int(r['s'] or 0), user['currency'])} · {r['c']}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
@router.message(Command("export"))
@private_access
async def cmd_export(message: Message, bot: Bot):
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id", (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("Экспортировать нечего.")
        return
    fd, path = tempfile.mkstemp(prefix="orders_", suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["number", "status", "amount", "currency", "service", "category", "comment", "source", "created_at", "published_at"])
        for r in rows:
            writer.writerow([
                r["public_number"], r["status"], r["amount"], r["currency"], r["service"], r["category"], r["comment"], r["source"], r["created_at"], r["published_at"]
            ])
    await bot.send_document(message.chat.id, FSInputFile(path, filename="orders.csv"))
    try:
        os.remove(path)
    except OSError:
        pass


@router.message(Command("backup"))
@private_access
async def cmd_backup(message: Message, bot: Bot):
    # For public SaaS users give CSV, owners can get full DB.
    if message.from_user.id not in OWNER_IDS:
        await cmd_export(message, bot)
        return
    await bot.send_document(message.chat.id, FSInputFile(DB_PATH, filename="orders_backup.db"))


@router.message(Command("subscription"))
@private_access
async def cmd_subscription(message: Message):
    row = ensure_user(message)
    await message.answer("<b>Подписка</b>\n\n" + subscription_text(row), parse_mode=ParseMode.HTML)
@router.message(Command("plans"))
@private_access
async def cmd_plans(message: Message):
    prices = plan_prices()
    lines = ["<b>Тарифы</b>", ""]
    if TRIAL_DAYS:
        lines.append(f"trial · {TRIAL_DAYS} дней")
    for plan in PLANS:
        if plan == "trial":
            continue
        price = prices.get(plan)
        lines.append(f"{plan} · {price} ₽/мес" if price is not None else plan)
    if SUBSCRIPTION_CONTACT_URL:
        lines += ["", html.escape(SUBSCRIPTION_CONTACT_URL)]
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
@router.message(Command("grant"))
@owner_required
async def cmd_grant(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.answer("Пример: /grant 123456789 30 pro")
        return
    try:
        user_id = int(parts[0])
        days = int(parts[1])
    except ValueError:
        await message.answer("USER_ID и DAYS должны быть числами.")
        return
    plan = parts[2] if len(parts) >= 3 else "pro"
    row = get_user(user_id)
    if not row:
        await message.answer("Такой пользователь ещё не нажимал /start.")
        return
    current_paid = parse_dt(row["paid_until"])
    base = current_paid if current_paid and current_paid > now_utc() else now_utc()
    paid_until = base + timedelta(days=days)
    with db() as conn:
        conn.execute("UPDATE users SET plan=?, paid_until=?, updated_at=? WHERE tg_id=?", (plan, iso(paid_until), iso(), user_id))
        conn.execute(
            "INSERT INTO subscription_events (user_id, admin_id, action, days, plan, created_at) VALUES (?, ?, 'grant', ?, ?, ?)",
            (user_id, message.from_user.id, days, plan, iso()),
        )
        conn.commit()
    await message.answer(f"✅ Выдано {days} дней пользователю {user_id}. Оплачено до {paid_until.strftime('%d.%m.%Y')}")


@router.message(Command("revoke"))
@owner_required
async def cmd_revoke(message: Message, command: CommandObject):
    try:
        user_id = int((command.args or "").split()[0])
    except Exception:
        await message.answer("Пример: /revoke 123456789")
        return
    with db() as conn:
        conn.execute("UPDATE users SET paid_until=?, trial_until=?, plan='free', updated_at=? WHERE tg_id=?", (iso(now_utc() - timedelta(days=1)), iso(now_utc() - timedelta(days=1)), iso(), user_id))
        conn.execute("INSERT INTO subscription_events (user_id, admin_id, action, created_at) VALUES (?, ?, 'revoke', ?)", (user_id, message.from_user.id, iso()))
        conn.commit()
    await message.answer(f"✅ Доступ пользователя {user_id} отозван.")


@router.message(Command("users"))
@owner_required
async def cmd_users(message: Message):
    with db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
    lines = ["👥 <b>Последние пользователи</b>"]
    for r in rows:
        active = "✅" if is_sub_active(r) else "❌"
        uname = f"@{r['username']}" if r["username"] else r["first_name"] or "—"
        lines.append(f"{active} <code>{r['tg_id']}</code> {html.escape(uname)} / {html.escape(r['plan'])} / paid {fmt_date(r['paid_until'])}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("app_stats"))
@owner_required
async def cmd_app_stats(message: Message):
    with db() as conn:
        u = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        active = conn.execute("SELECT * FROM users").fetchall()
        orders = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM orders WHERE status='published'").fetchone()
        emails = conn.execute("SELECT COUNT(*) AS c FROM users WHERE email_enabled=1").fetchone()["c"]
    active_count = sum(1 for r in active if is_sub_active(r))
    text = (
        "<b>Приложение</b>\n\n"
        f"Пользователи: <b>{u}</b>\n"
        f"Активные: <b>{active_count}</b>\n"
        f"Почты: <b>{emails}</b>\n"
        f"Заказы: <b>{orders['c']}</b>\n"
        f"Сумма: <b>{rub(int(orders['s']))}</b>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)
# -------------------- email parsing --------------------

def mailbox_hash(row: sqlite3.Row) -> str:
    return hashlib.sha256(f"{row['imap_host']}:{row['imap_user']}:{row['imap_folder']}".encode()).hexdigest()[:16]


def extract_email_text(msg: EmailMessage) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disp:
                continue
            if ctype in {"text/plain", "text/html"}:
                try:
                    payload = part.get_content()
                    if ctype == "text/html":
                        payload = re.sub(r"<[^>]+>", " ", str(payload))
                    parts.append(str(payload))
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_content()
            if msg.get_content_type() == "text/html":
                payload = re.sub(r"<[^>]+>", " ", str(payload))
            parts.append(str(payload))
        except Exception:
            pass
    return "\n".join(parts)


def parse_kwork_order(subject: str, body: str, row: sqlite3.Row) -> Optional[dict[str, Any]]:
    sender_filter = (row["sender_filter"] or DEFAULT_KWORK_SENDER_FILTER).lower()
    combined = f"{subject}\n{body}".lower()
    # Sender itself is checked before this, but keep subject/body fallback.
    success = split_csv(row["success_keywords"] or ",".join(SUCCESS_KEYWORDS))
    ignore = split_csv(row["ignore_keywords"] or ",".join(IGNORE_KEYWORDS))
    if any(k.lower() in combined for k in ignore):
        return None
    if not any(k.lower() in combined for k in success):
        return None
    amount = 0
    for pattern in [
        r"(?:сумма|стоимость|оплата|зачислено|доход|к оплате)\D{0,30}(\d[\d\s.,]{1,12})\s*(?:₽|руб|рублей|р|RUB)",
        r"(\d[\d\s.,]{1,12})\s*(?:₽|руб|рублей|р|RUB)",
    ]:
        m = re.search(pattern, combined, flags=re.IGNORECASE)
        if m:
            amount = money_to_int(m.group(1))
            break
    service = subject.strip() or "Заказ Kwork"
    # Try extracting quoted or titled service from body.
    for pattern in [
        r"(?:заказ|кворк|проект)[:\s]+[«\"]?([^\n«»\"]{8,90})",
        r"(?:услуга|название)[:\s]+[«\"]?([^\n«»\"]{8,90})",
    ]:
        m = re.search(pattern, body, flags=re.IGNORECASE)
        if m:
            service = m.group(1).strip(" .:-—\n\r\t")[:100]
            break
    if amount <= 0:
        # Keep draft, but publishing will be blocked until the user edits the amount.
        amount = 0
    return {"amount": amount, "service": service[:120], "comment": "", "category": "Kwork"}


async def send_email_preview(bot: Bot, row: sqlite3.Row, order: sqlite3.Row) -> None:
    try:
        if row["auto_publish"]:
            ok, result = await publish_order(bot, order["id"], notify_user=False)
            if not ok:
                await bot.send_message(row["tg_id"], f"Письмо найдено, публикация не прошла:\n{html.escape(result)}", parse_mode=ParseMode.HTML)
            return
        await bot.send_message(
            row["tg_id"],
            "<b>Найден заказ</b>\n\n" + render_post(order),
            parse_mode=ParseMode.HTML,
            reply_markup=preview_keyboard(order["id"]),
        )
    except Exception as e:
        print(f"Failed to send email preview to {row['tg_id']}: {e}")
async def check_mailbox_for_user(bot: Bot, row: sqlite3.Row) -> None:
    if not is_sub_active(row):
        return
    if not row["imap_user"] or not row["imap_password_enc"]:
        return
    mailbox = mailbox_hash(row)
    try:
        password = decrypt_secret(row["imap_password_enc"])
        imap = imaplib.IMAP4_SSL(row["imap_host"] or DEFAULT_IMAP_HOST, int(row["imap_port"] or DEFAULT_IMAP_PORT), timeout=20)
        imap.login(row["imap_user"], password)
        imap.select(row["imap_folder"] or DEFAULT_IMAP_FOLDER)
        status, data = imap.uid("search", None, "ALL")
        if status != "OK":
            imap.logout()
            return
        uids = (data[0] or b"").decode().split()
        uids = uids[-EMAIL_MAX_PER_USER:]
        if not row["mail_initialized"] and row["email_skip_old"]:
            with db() as conn:
                for uid in uids:
                    conn.execute(
                        "INSERT OR IGNORE INTO processed_emails (user_id, mailbox, uid, created_at) VALUES (?, ?, ?, ?)",
                        (row["tg_id"], mailbox, uid, iso()),
                    )
                conn.execute("UPDATE users SET mail_initialized=1 WHERE tg_id=?", (row["tg_id"],))
                conn.commit()
            imap.logout()
            return
        with db() as conn:
            conn.execute("UPDATE users SET mail_initialized=1 WHERE tg_id=?", (row["tg_id"],))
            conn.commit()
        for uid in uids:
            with db() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM processed_emails WHERE user_id=? AND mailbox=? AND uid=?",
                    (row["tg_id"], mailbox, uid),
                ).fetchone()
            if exists:
                continue
            status, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            raw = msg_data[0][1]
            msg = message_from_bytes(raw, policy=default)
            subject = str(msg.get("Subject", ""))
            sender = str(msg.get("From", ""))
            body = extract_email_text(msg)
            sender_filter = (row["sender_filter"] or DEFAULT_KWORK_SENDER_FILTER).lower()
            if sender_filter and sender_filter not in sender.lower():
                with db() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO processed_emails (user_id, mailbox, uid, created_at) VALUES (?, ?, ?, ?)",
                        (row["tg_id"], mailbox, uid, iso()),
                    )
                    conn.commit()
                continue
            parsed = parse_kwork_order(subject, body, row)
            with db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO processed_emails (user_id, mailbox, uid, created_at) VALUES (?, ?, ?, ?)",
                    (row["tg_id"], mailbox, uid, iso()),
                )
                conn.commit()
            if not parsed:
                continue
            dedupe = hashlib.sha256(f"{row['tg_id']}:{mailbox}:{uid}:{subject}".encode()).hexdigest()[:32]
            order = create_order(
                user_id=row["tg_id"],
                amount=parsed["amount"],
                service=parsed["service"],
                category=parsed["category"],
                comment=parsed["comment"],
                source="email",
                status="draft",
                email_uid=uid,
                email_subject=subject,
                dedupe_key=dedupe,
            )
            await send_email_preview(bot, row, order)
        imap.logout()
    except Exception as e:
        print(f"Mailbox check failed for {row['tg_id']} {row['imap_user']}: {e}")


async def email_worker(bot: Bot):
    await asyncio.sleep(5)
    while True:
        try:
            with db() as conn:
                users = conn.execute("SELECT * FROM users WHERE email_enabled=1 AND is_blocked=0").fetchall()
            for row in users:
                await check_mailbox_for_user(bot, row)
                await asyncio.sleep(1)
        except Exception as e:
            print(f"Email worker error: {e}")
        await asyncio.sleep(max(20, EMAIL_CHECK_INTERVAL))


async def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await set_bot_commands(bot)
    print(f"Starting Kwork Proof Bot {BUILD_VERSION}")
    asyncio.create_task(email_worker(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
