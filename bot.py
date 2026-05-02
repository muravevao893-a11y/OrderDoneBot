import asyncio
import html
import imaplib
import os
import re
import sqlite3
import inspect
from functools import wraps
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from typing import Iterable, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()


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
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


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


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    channel_id = os.getenv("CHANNEL_ID", "").strip()
    if not bot_token:
        raise RuntimeError("Не указан BOT_TOKEN в .env")
    if not channel_id:
        raise RuntimeError("Не указан CHANNEL_ID в .env")

    return Settings(
        bot_token=bot_token,
        channel_id=channel_id,
        admin_ids=parse_admin_ids(os.getenv("ADMIN_IDS", "").strip()),
        contact_url=os.getenv("CONTACT_URL", "").strip(),
        contact_button_text=os.getenv("CONTACT_BUTTON_TEXT", "Заказать разработку").strip() or "Заказать разработку",
        default_currency=os.getenv("DEFAULT_CURRENCY", "₽").strip() or "₽",
        db_path=os.getenv("DB_PATH", "data/orders.db").strip() or "data/orders.db",
        auto_publish=env_bool("AUTO_PUBLISH", False),
        email_enabled=env_bool("EMAIL_ENABLED", False),
        imap_host=os.getenv("IMAP_HOST", "imap.gmail.com").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or "993"),
        imap_user=os.getenv("IMAP_USER", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        imap_folder=os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX",
        email_check_interval=max(15, int(os.getenv("EMAIL_CHECK_INTERVAL", "60").strip() or "60")),
        email_skip_old_on_first_run=env_bool("EMAIL_SKIP_OLD_ON_FIRST_RUN", True),
        kwork_sender_filter=os.getenv("KWORK_SENDER_FILTER", "kwork").strip().lower(),
        kwork_success_keywords=split_csv(
            os.getenv(
                "KWORK_SUCCESS_KEYWORDS",
                "заказ выполнен,заказ закрыт,работа выполнена,заказ завершен,order completed,completed",
            )
        ),
    )


settings = load_settings()
router = Router()


class ManualOrder(StatesGroup):
    waiting_amount = State()
    waiting_title = State()
    waiting_note = State()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today_ru() -> str:
    return datetime.now().strftime("%d.%m.%Y")


def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    return con


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
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")


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


def only_admin(handler):
    # Aiogram 3 прокидывает в обработчики служебные аргументы через DI
    # (например dispatcher, bot, state, command). Передаём в исходный
    # handler только те аргументы, которые он реально принимает.
    handler_signature = inspect.signature(handler)
    allowed_kwargs = set(handler_signature.parameters.keys())

    @wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if settings.admin_ids and message.from_user and message.from_user.id not in settings.admin_ids:
            await message.answer("Нет доступа.")
            return
        filtered_kwargs = {key: value for key, value in kwargs.items() if key in allowed_kwargs}
        return await handler(message, *args, **filtered_kwargs)

    return wrapper


def is_admin_user(user_id: Optional[int]) -> bool:
    return bool(user_id and (not settings.admin_ids or user_id in settings.admin_ids))


def format_money(amount: int, currency: str | None = None) -> str:
    currency = currency or settings.default_currency
    return f"{amount:,}".replace(",", " ") + f" {currency}"


def normalize_amount(raw: str) -> int:
    cleaned = re.sub(r"[^0-9]", "", raw)
    if not cleaned:
        return 0
    return int(cleaned)


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"^(re|fw|fwd):\s*", "", title, flags=re.I)
    # Убираем частые служебные хвосты, если они есть в теме письма.
    title = re.sub(r"\b(kwork|кворк)\b", "", title, flags=re.I).strip(" -—|:")
    return title[:160] or "Заказ на Kwork"


def build_post_text(order: sqlite3.Row | dict) -> str:
    title = html.escape(str(order["title"]))
    note = html.escape(str(order["note"] or ""))
    amount = int(order["amount"] or 0)
    currency = str(order["currency"] or settings.default_currency)
    order_id = int(order["id"])

    lines = [
        "✅ <b>ЗАКАЗ ВЫПОЛНЕН</b>",
        "",
    ]
    if amount > 0:
        lines.append(f"💰 <b>Сумма:</b> {format_money(amount, currency)}")
    else:
        lines.append("💰 <b>Сумма:</b> не указана")
    lines.extend(
        [
            f"🧩 <b>Услуга:</b> {title}",
            f"📅 <b>Дата:</b> {today_ru()}",
            f"🔢 <b>Заказ №:</b> {order_id:06d}",
        ]
    )
    if note:
        lines.extend(["", f"💬 {note}"])
    lines.extend(["", "Спасибо за доверие 🙌"])
    return "\n".join(lines)


def order_keyboard(order_id: Optional[int] = None, for_admin: bool = False) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    if settings.contact_url and not for_admin:
        buttons.append([InlineKeyboardButton(text=settings.contact_button_text, url=settings.contact_url)])
    if for_admin and order_id is not None:
        buttons.append(
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{order_id}"),
                InlineKeyboardButton(text="🗑 Пропустить", callback_data=f"skip:{order_id}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def insert_order(
    *,
    source: str,
    source_uid: Optional[str],
    amount: int,
    title: str,
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
                INSERT INTO orders(source, source_uid, amount, currency, title, note, raw_subject, raw_from, status, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    source_uid,
                    int(amount or 0),
                    currency or settings.default_currency,
                    clean_title(title),
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


def get_order(order_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as con:
        return con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


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
        reply_markup=order_keyboard(for_admin=False),
        disable_web_page_preview=True,
    )
    with db_connect() as con:
        con.execute(
            "UPDATE orders SET status = 'published', published_at = ?, channel_message_id = ? WHERE id = ?",
            (now_iso(), msg.message_id, order_id),
        )
    return msg.message_id


async def notify_admins(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if not settings.admin_ids:
        return
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception:
            pass


def parse_done_args(args: str) -> tuple[int, str, str]:
    args = (args or "").strip()
    if not args:
        return 0, "", ""

    # /done 3000 | название | комментарий
    if "|" in args:
        parts = [p.strip() for p in args.split("|")]
        amount = normalize_amount(parts[0]) if parts else 0
        title = parts[1] if len(parts) > 1 else "Заказ"
        note = parts[2] if len(parts) > 2 else ""
        return amount, title, note

    # /done 3000 Разработка Telegram-бота
    match = re.match(r"^([\d\s.,]+)\s+(.+)$", args)
    if match:
        return normalize_amount(match.group(1)), match.group(2).strip(), ""

    return 0, args, ""


@router.message(Command("start", "help"))
@only_admin
async def cmd_start(message: Message) -> None:
    mode = "полный автомат" if settings.auto_publish else "предпросмотр с подтверждением"
    email_status = "включена" if settings.email_enabled else "выключена"
    await message.answer(
        "Привет. Я бот для автопостинга выполненных заказов в канал.\n\n"
        f"Режим публикации: <b>{html.escape(mode)}</b>\n"
        f"Проверка почты: <b>{html.escape(email_status)}</b>\n\n"
        "Команды:\n"
        "<code>/done 3000 Разработка Telegram-бота</code> — вручную опубликовать заказ\n"
        "<code>/done 3000 | Название | Комментарий</code> — с комментарием\n"
        "<code>/done</code> — пошаговое добавление\n"
        "<code>/checkmail</code> — проверить почту сейчас\n"
        "<code>/drafts</code> — черновики из почты\n"
        "<code>/stats</code> — статистика",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("done"))
@only_admin
async def cmd_done(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    amount, title, note = parse_done_args(command.args or "")
    if not command.args:
        await state.set_state(ManualOrder.waiting_amount)
        await message.answer("Введи сумму заказа, например: <code>3000</code>", parse_mode=ParseMode.HTML)
        return

    if not title:
        await message.answer("Не понял название услуги. Пример: <code>/done 3000 Разработка Telegram-бота</code>", parse_mode=ParseMode.HTML)
        return

    order_id = insert_order(source="manual", source_uid=None, amount=amount, title=title, note=note, status="draft")
    if not order_id:
        await message.answer("Не смог создать заказ.")
        return

    await publish_order(bot, order_id)
    await message.answer("Готово, пост опубликован в канал ✅")


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
    order_id = insert_order(
        source="manual",
        source_uid=None,
        amount=int(data["amount"]),
        title=str(data["title"]),
        note=note,
        status="draft",
    )
    await state.clear()
    if not order_id:
        await message.answer("Не смог создать заказ.")
        return
    await publish_order(bot, order_id)
    await message.answer("Готово, пост опубликован в канал ✅")


@router.message(Command("stats"))
@only_admin
async def cmd_stats(message: Message) -> None:
    with db_connect() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
            FROM orders
            WHERE status = 'published'
            """
        ).fetchone()
        drafts = con.execute("SELECT COUNT(*) AS cnt FROM orders WHERE status = 'draft'").fetchone()["cnt"]
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Опубликовано заказов: <b>{row['cnt']}</b>\n"
        f"Сумма опубликованных: <b>{format_money(int(row['total']))}</b>\n"
        f"Черновиков на подтверждение: <b>{drafts}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("drafts"))
@only_admin
async def cmd_drafts(message: Message) -> None:
    with db_connect() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE status = 'draft' ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await message.answer("Черновиков нет.")
        return
    for row in rows:
        await message.answer(
            "📝 <b>Черновик из почты</b>\n\n" + build_post_text(row),
            parse_mode=ParseMode.HTML,
            reply_markup=order_keyboard(int(row["id"]), for_admin=True),
        )


@router.message(Command("checkmail"))
@only_admin
async def cmd_checkmail(message: Message, bot: Bot) -> None:
    if not settings.email_enabled:
        await message.answer("Проверка почты выключена. Поставь <code>EMAIL_ENABLED=true</code> в .env", parse_mode=ParseMode.HTML)
        return
    await message.answer("Проверяю почту…")
    try:
        count = await check_email_once(bot, manual=True)
    except Exception as exc:
        await message.answer(f"Ошибка проверки почты: <code>{html.escape(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return
    await message.answer(f"Готово. Новых выполненных заказов найдено: <b>{count}</b>", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("publish:"))
async def cb_publish(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    await publish_order(bot, order_id)
    await callback.answer("Опубликовано")
    if callback.message:
        await callback.message.edit_text("✅ Опубликовано в канал\n\n" + callback.message.html_text, parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    with db_connect() as con:
        con.execute("UPDATE orders SET status = 'skipped' WHERE id = ?", (order_id,))
    await callback.answer("Пропущено")
    if callback.message:
        await callback.message.edit_text("🗑 Черновик пропущен", parse_mode=ParseMode.HTML)


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
    return any(keyword in haystack for keyword in settings.kwork_success_keywords)


def extract_amount(text: str) -> int:
    patterns = [
        r"(?:сумма|стоимость|доход|оплата|заработок)\D{0,40}([0-9][0-9\s.,]{1,15})\s*(?:₽|руб\.?|р\.?|rub)",
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
        for pattern in patterns[:1]:
            match = re.search(pattern, line, flags=re.I)
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
                await notify_admins(
                    bot,
                    "🧾 <b>Найден выполненный заказ из Kwork</b>\n\n"
                    "Проверь, всё ли нормально, и нажми «Опубликовать».\n\n"
                    + build_post_text(row),
                    reply_markup=order_keyboard(order_id, for_admin=True),
                )
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


async def main() -> None:
    init_db()
    bot = Bot(settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    if settings.email_enabled:
        asyncio.create_task(email_watcher(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
