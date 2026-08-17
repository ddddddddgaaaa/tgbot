#!/usr/bin/env python3
"""
Wmilka Bot - Telegram bot with registration, giveaways, promo codes,
suggestions and a referral system.
"""

import asyncio
import hashlib
import logging
import os
import random
import sqlite3

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Setup

load_dotenv(override=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("Set BOT_TOKEN in .env file")

# Put your Telegram user id(s) here (or load from .env as MODERATOR_IDS=123,456)
MODERATOR_IDS = [
    int(x) for x in os.getenv('MODERATOR_IDS', '').split(',') if x.strip()
]

# Админы - полный доступ (в т.ч. добавление промокодов).
# Модеры - доступ к /admin, но без создания промокодов.
ADMIN_IDS = [
    int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()
]

# Все, у кого есть доступ к скрытой команде /admin
STAFF_IDS = set(ADMIN_IDS) | set(MODERATOR_IDS)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_staff(user_id: int) -> bool:
    return user_id in STAFF_IDS


def registered_only(handler):
    """Декоратор: не даёт выполнить команду, если пользователь ещё не прошёл
    регистрацию (не подтверждён модератором)."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_data = get_user(user.id)
        if not user_data or not user_data.get('is_registered'):
            await update.message.reply_text(
                "❌ Эта команда доступна только после регистрации.\n"
                "Пришлите боту скриншот своего Twitch-профиля, чтобы зарегистрироваться."
            )
            return
        return await handler(update, context)
    return wrapper


ITEMS_PER_PAGE = 1  # сколько заявок/предложений показываем за раз в /admin

TICKET_PRICE = 50
REFERRAL_BONUS_REFERRER = 100
REFERRAL_BONUS_REFERRED = 50

# Розыгрыши
GIVEAWAY_TICKET_MIN = 1
GIVEAWAY_TICKET_MAX = 10000
ADMIN_GIVEAWAYS_PER_PAGE = 5
GIVEAWAY_PARTICIPANTS_PER_PAGE = 10

# Картинка-инструкция, которая отправляется при /start.
# Можно использовать либо file_id из Telegram (быстрее, без файлов на диске),
# либо путь к локальному файлу как запасной вариант.
TWITCH_EXAMPLE_FILE_ID = os.getenv('TWITCH_EXAMPLE_FILE_ID', '').strip()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TWITCH_EXAMPLE_IMAGE_PATH = os.path.join(BASE_DIR, "twitch_example.png")

DB_NAME = "bot_database.db"

# Database

def get_db():
    """Get database connection and ensure schema exists."""
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_registered INTEGER DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
            referrals_count INTEGER DEFAULT 0,
            balance INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            screenshot_file_id TEXT,
            reg_status TEXT DEFAULT 'none',
            reviewed_by INTEGER
        )"""
    )
    # Миграция для уже существующих баз данных (созданных до добавления полей)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for col_def in (
        ("screenshot_file_id", "TEXT"),
        ("reg_status", "TEXT DEFAULT 'none'"),
        ("reviewed_by", "INTEGER"),
    ):
        col_name, col_type = col_def
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            value INTEGER,
            uses_limit INTEGER,
            uses_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS promo_activations (
            user_id INTEGER,
            code TEXT,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, code)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS giveaway_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ticket_number INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            image_file_id TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active',
            winner_id INTEGER,
            winner_ticket INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS giveaway_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            giveaway_id INTEGER,
            user_id INTEGER,
            ticket_number INTEGER,
            screenshot_file_id TEXT,
            reg_status TEXT DEFAULT 'pending',
            reviewed_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()
    return conn


db = get_db()


def get_user(user_id: int) -> dict | None:
    cursor = db.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))
    return None


def get_user_by_referral_code(code: str) -> dict | None:
    cursor = db.cursor()
    cursor.execute("SELECT * FROM users WHERE referral_code = ?", (code,))
    row = cursor.fetchone()
    if row:
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))
    return None


def create_user(user_id: int, username: str, referred_by: int | None = None) -> dict:
    referral_code = hashlib.md5(str(user_id).encode()).hexdigest()[:8].upper()

    cursor = db.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referral_code, referred_by) "
        "VALUES (?, ?, ?, ?)",
        (user_id, username, referral_code, referred_by)
    )
    db.commit()

    return get_user(user_id)


def register_user(user_id: int) -> None:
    cursor = db.cursor()
    cursor.execute("UPDATE users SET is_registered = 1 WHERE user_id = ?", (user_id,))
    db.commit()


def add_referral(referrer_id: int, referred_id: int) -> bool:
    """Give bonuses to referrer and referred user. Returns False if referrer missing."""
    if not get_user(referrer_id):
        return False

    cursor = db.cursor()
    cursor.execute(
        "UPDATE users SET referrals_count = referrals_count + 1, "
        "balance = balance + ? WHERE user_id = ?",
        (REFERRAL_BONUS_REFERRER, referrer_id)
    )
    cursor.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
        (REFERRAL_BONUS_REFERRED, referred_id)
    )
    db.commit()
    return True


def use_promo_code(user_id: int, code: str) -> str:
    """Try to activate a promo code. Returns a status string."""
    code = code.upper().strip()
    cursor = db.cursor()

    cursor.execute("SELECT code, value, uses_limit, uses_count FROM promo_codes WHERE code = ?", (code,))
    row = cursor.fetchone()
    if not row:
        return "not_found"

    db_code, value, uses_limit, uses_count = row

    if uses_limit and uses_count >= uses_limit:
        return "limit_reached"

    cursor.execute(
        "SELECT 1 FROM promo_activations WHERE user_id = ? AND code = ?",
        (user_id, db_code)
    )
    if cursor.fetchone():
        return "already_used"

    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (value, user_id))
    cursor.execute("UPDATE promo_codes SET uses_count = uses_count + 1 WHERE code = ?", (db_code,))
    cursor.execute(
        "INSERT INTO promo_activations (user_id, code) VALUES (?, ?)",
        (user_id, db_code)
    )
    db.commit()
    return f"ok:{value}"


def add_suggestion(user_id: int, username: str, text: str) -> int:
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO suggestions (user_id, username, text) VALUES (?, ?, ?)",
        (user_id, username, text)
    )
    db.commit()
    return cursor.lastrowid


# Регистрация по скриншоту

def set_registration_screenshot(user_id: int, file_id: str) -> None:
    cursor = db.cursor()
    cursor.execute(
        "UPDATE users SET screenshot_file_id = ?, reg_status = 'pending' "
        "WHERE user_id = ?",
        (file_id, user_id)
    )
    db.commit()


def get_pending_registrations(limit: int = 1, offset: int = 0) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM users WHERE reg_status = 'pending' "
        "ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (limit, offset)
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def count_pending_registrations() -> int:
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE reg_status = 'pending'")
    return cursor.fetchone()[0]


def review_registration(user_id: int, approved: bool, reviewer_id: int) -> None:
    cursor = db.cursor()
    if approved:
        cursor.execute(
            "UPDATE users SET is_registered = 1, reg_status = 'approved', "
            "reviewed_by = ? WHERE user_id = ?",
            (reviewer_id, user_id)
        )
    else:
        cursor.execute(
            "UPDATE users SET reg_status = 'rejected', reviewed_by = ? "
            "WHERE user_id = ?",
            (reviewer_id, user_id)
        )
    db.commit()


# Предложения (админ-очередь)

def get_pending_suggestions(limit: int = 1, offset: int = 0) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM suggestions WHERE status = 'pending' "
        "ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (limit, offset)
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def count_pending_suggestions() -> int:
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM suggestions WHERE status = 'pending'")
    return cursor.fetchone()[0]


def mark_suggestion_reviewed(suggestion_id: int) -> None:
    cursor = db.cursor()
    cursor.execute(
        "UPDATE suggestions SET status = 'reviewed' WHERE id = ?",
        (suggestion_id,)
    )
    db.commit()


# Промокоды (создание админом)

def create_promo_code(code: str, value: int, uses_limit: int) -> bool:
    code = code.upper().strip()
    cursor = db.cursor()
    try:
        cursor.execute(
            "INSERT INTO promo_codes (code, value, uses_limit) VALUES (?, ?, ?)",
            (code, value, uses_limit)
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# Розыгрыши

def create_giveaway(title: str, image_file_id: str | None, creator_id: int) -> int:
    """Create a new giveaway. Returns the new giveaway id."""
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO giveaways (title, image_file_id, created_by) VALUES (?, ?, ?)",
        (title, image_file_id, creator_id)
    )
    db.commit()
    return cursor.lastrowid


def get_active_giveaways() -> list[dict]:
    """Return all active giveaways, newest first."""
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaways WHERE status = 'active' ORDER BY created_at DESC"
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_giveaway(giveaway_id: int) -> dict | None:
    cursor = db.cursor()
    cursor.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
    row = cursor.fetchone()
    if row:
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))
    return None


def get_all_giveaways(limit: int = 20, offset: int = 0) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaways ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def count_all_giveaways() -> int:
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM giveaways")
    return cursor.fetchone()[0]


def count_active_giveaways() -> int:
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM giveaways WHERE status = 'active'")
    return cursor.fetchone()[0]


def update_giveaway_title(giveaway_id: int, title: str) -> bool:
    title = title.strip()
    if not title:
        return False
    cursor = db.cursor()
    cursor.execute("UPDATE giveaways SET title = ? WHERE id = ?", (title, giveaway_id))
    db.commit()
    return cursor.rowcount > 0


def update_giveaway_image(giveaway_id: int, image_file_id: str) -> bool:
    cursor = db.cursor()
    cursor.execute(
        "UPDATE giveaways SET image_file_id = ? WHERE id = ?",
        (image_file_id, giveaway_id)
    )
    db.commit()
    return cursor.rowcount > 0


def delete_giveaway(giveaway_id: int) -> bool:
    """Delete a giveaway and all of its participant applications."""
    cursor = db.cursor()
    cursor.execute("SELECT 1 FROM giveaways WHERE id = ?", (giveaway_id,))
    if not cursor.fetchone():
        return False

    cursor.execute(
        "DELETE FROM giveaway_participants WHERE giveaway_id = ?",
        (giveaway_id,)
    )
    cursor.execute("DELETE FROM giveaways WHERE id = ?", (giveaway_id,))
    db.commit()
    return cursor.rowcount > 0


def get_all_user_ids() -> list[int]:
    """Return every chat id known to the bot for result announcements."""
    cursor = db.cursor()
    cursor.execute("SELECT user_id FROM users")
    return [row[0] for row in cursor.fetchall()]


def get_giveaway_user_ids(giveaway_id: int) -> list[int]:
    """Return all users who submitted an application for a giveaway."""
    cursor = db.cursor()
    cursor.execute(
        "SELECT DISTINCT user_id FROM giveaway_participants WHERE giveaway_id = ?",
        (giveaway_id,)
    )
    return [row[0] for row in cursor.fetchall()]


def set_giveaway_finished(giveaway_id: int, winner_id: int | None = None,
                          winner_ticket: int | None = None) -> None:
    cursor = db.cursor()
    cursor.execute(
        "UPDATE giveaways SET status = 'finished', winner_id = ?, winner_ticket = ? "
        "WHERE id = ?",
        (winner_id, winner_ticket, giveaway_id)
    )
    db.commit()


def add_giveaway_participant(giveaway_id: int, user_id: int, ticket_number: int,
                             screenshot_file_id: str) -> int:
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO giveaway_participants "
        "(giveaway_id, user_id, ticket_number, screenshot_file_id, reg_status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (giveaway_id, user_id, ticket_number, screenshot_file_id)
    )
    db.commit()
    return cursor.lastrowid


def get_giveaway_participant(giveaway_id: int, user_id: int) -> dict | None:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaway_participants WHERE giveaway_id = ? AND user_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (giveaway_id, user_id)
    )
    row = cursor.fetchone()
    if row:
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))
    return None


def get_giveaway_participants(giveaway_id: int, status_filter: str = 'confirmed',
                              limit: int = 20, offset: int = 0) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaway_participants WHERE giveaway_id = ? AND reg_status = ? "
        "ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (giveaway_id, status_filter, limit, offset)
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def count_giveaway_participants(giveaway_id: int, status_filter: str = 'confirmed') -> int:
    cursor = db.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id = ? AND reg_status = ?",
        (giveaway_id, status_filter)
    )
    return cursor.fetchone()[0]


def get_all_giveaway_participants(giveaway_id: int, limit: int = 20,
                                  offset: int = 0) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaway_participants WHERE giveaway_id = ? "
        "ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (giveaway_id, limit, offset)
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def count_all_giveaway_participants(giveaway_id: int) -> int:
    cursor = db.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id = ?",
        (giveaway_id,)
    )
    return cursor.fetchone()[0]


def get_pending_giveaway_participants(giveaway_id: int, limit: int = 1,
                                      offset: int = 0) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaway_participants WHERE giveaway_id = ? AND reg_status = 'pending' "
        "ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (giveaway_id, limit, offset)
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def delete_rejected_giveaway_participant(giveaway_id: int, user_id: int) -> None:
    cursor = db.cursor()
    cursor.execute(
        "DELETE FROM giveaway_participants "
        "WHERE giveaway_id = ? AND user_id = ? AND reg_status = 'rejected'",
        (giveaway_id, user_id)
    )
    db.commit()


def count_pending_giveaway_participants(giveaway_id: int) -> int:
    cursor = db.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id = ? AND reg_status = 'pending'",
        (giveaway_id,)
    )
    return cursor.fetchone()[0]


def review_giveaway_participant(participant_id: int, approved: bool,
                                reviewer_id: int) -> dict | None:
    """Approve or reject a giveaway participant. Returns the participant dict."""
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaway_participants WHERE id = ?",
        (participant_id,)
    )
    row = cursor.fetchone()
    if not row:
        return None
    columns = [d[0] for d in cursor.description]
    participant = dict(zip(columns, row))

    # Не даём повторным нажатиям на старую кнопку изменить уже вынесенное решение.
    if participant['reg_status'] != 'pending':
        return participant

    new_status = 'confirmed' if approved else 'rejected'
    cursor.execute(
        "UPDATE giveaway_participants SET reg_status = ?, reviewed_by = ? WHERE id = ?",
        (new_status, reviewer_id, participant_id)
    )
    db.commit()
    return participant


def get_confirmed_participants(giveaway_id: int) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM giveaway_participants WHERE giveaway_id = ? AND reg_status = 'confirmed'",
        (giveaway_id,)
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def draw_giveaway_winner(giveaway_id: int) -> dict | None:
    """Pick a random winner from confirmed participants and finish the giveaway."""
    giveaway = get_giveaway(giveaway_id)
    if not giveaway or giveaway['status'] != 'active':
        return None

    participants = get_confirmed_participants(giveaway_id)
    if not participants:
        return None
    winner = random.choice(participants)
    set_giveaway_finished(
        giveaway_id,
        winner_id=winner['user_id'],
        winner_ticket=winner['ticket_number']
    )
    return winner


# Статистика

def get_stats() -> dict:
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_registered = 1")
    registered = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE reg_status = 'pending'")
    pending_reg = cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(balance), 0) FROM users")
    total_balance = cursor.fetchone()[0]
    cursor.execute(
        "SELECT COUNT(*) FROM giveaway_participants WHERE reg_status = 'confirmed'"
    )
    tickets_sold = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM suggestions WHERE status = 'pending'")
    pending_suggestions = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM giveaways")
    giveaways_total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM giveaways WHERE status = 'active'")
    giveaways_active = cursor.fetchone()[0]
    return {
        "total_users": total_users,
        "registered": registered,
        "pending_reg": pending_reg,
        "total_balance": total_balance,
        "tickets_sold": tickets_sold,
        "pending_suggestions": pending_suggestions,
        "giveaways_total": giveaways_total,
        "giveaways_active": giveaways_active,
    }


# Command handlers

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command, including referral deep links (/start CODE)."""
    user = update.effective_user
    existing = get_user(user.id)

    referred_by_id = None
    if not existing and context.args:
        ref_code = context.args[0]
        referrer = get_user_by_referral_code(ref_code)
        if referrer and referrer['user_id'] != user.id:
            referred_by_id = referrer['user_id']

    if not existing:
        create_user(user.id, user.username or user.first_name, referred_by=referred_by_id)
        if referred_by_id:
            add_referral(referred_by_id, user.id)

    # Актуальные данные пользователя (после возможного создания)
    user_data = get_user(user.id)
    is_registered = bool(user_data and user_data.get('is_registered'))

    if not is_registered:
        # Приветственная инструкция: как подтвердить Twitch-аккаунт.
        # Показываем только тем, кто ещё не зарегистрирован.
        caption = (
            " Чтобы начать пользоваться ботом, нужно зарегистрировать "
            "свой Twitch аккаунт.\n\n"
            " Пришлите скриншот своего профиля по примеру на картинке выше "
            "(имя аккаунта должно быть чётко видно).\n\n"
            "В течение некоторого времени модераторы проверят и подтвердят ваш профиль. "
            "После подтверждения вам станут доступны розыгрыши, промокоды и другие функции бота."
        )

        try:
            if TWITCH_EXAMPLE_FILE_ID:
                # Быстрый путь: картинка уже один раз загружена в Telegram, шлём по file_id
                await update.message.reply_photo(photo=TWITCH_EXAMPLE_FILE_ID, caption=caption)
            else:
                # Запасной путь: читаем локальный файл с диска
                with open(TWITCH_EXAMPLE_IMAGE_PATH, "rb") as photo:
                    await update.message.reply_photo(photo=photo, caption=caption)
        except FileNotFoundError:
            logger.warning(f"Instruction image not found at {TWITCH_EXAMPLE_IMAGE_PATH}")

        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\n"
            "Добро пожаловать в Wmilka Bot!\n\n"
            "Пока доступны:\n"
            "/help - справка\n"
            "/promocodes - промокоды и бонусы\n"
            "/social - наши социальные сети\n"
            "/myid - получить свой ID\n\n"
            "Розыгрыши и остальные функции откроются после подтверждения регистрации."
        )
    else:
        # Пользователь уже зарегистрирован - инструкцию больше не показываем
        await update.message.reply_text(
            f"👋 С возвращением, {user.first_name}!\n\n"
            "Доступные команды:\n"
            "/help - справка\n"
            "/giveaway - розыгрыши\n"
            "/promocodes - промокоды и бонусы\n"
            "/social - наши социальные сети\n"
            "/promo <код> - активировать промокод\n"
            "/suggest - предложения\n"
            "/referral - рефералы\n"
            "/balance - баланс\n"
            "/myid - получить свой ID"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Справка:\n\n"
        "Доступные команды:\n"
        "/start - начало\n"
        "/help - эта справка\n"
        "/giveaway - активные розыгрыши\n"
        "/promocodes - промокоды и бонусы\n"
        "/social - наши социальные сети\n"
        "/promo <код> - активировать промокод\n"
        "/suggest <текст> - отправить предложение модераторам\n"
        "/referral - ваша реферальная ссылка\n"
        "/balance - проверить баланс\n"
        "/myid - получить свой ID"
    )


async def promocodes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current partner promo codes."""
    await update.message.reply_text(
        "🎟 Промокоды партнёров:\n\n"
        "DINODROP — WOKOMILKA — скидка 30%/20%\n"
        "FREEMILKA — бесплатное открытие кейса\n"
        "FAMASJABKA — MILKA — +14% к пополнению\n"
        "MAGICDROP — «Волшебство случается здесь» — +25% к пополнению\n"
        "MILKASKINBRO — MILKA — +20% к пополнению\n"
        "SKINHOUSE — MILKA25 — +25% к пополнению\n"
        "WOKOMILKA — +40% к первому пополнению"
    )


async def social_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the project's social links."""
    await update.message.reply_text(
        "🌐 Наши социальные сети:\n\n"
        "CHAT — https://t.me/+GBeF0r5FRyUxMmEy\n"
        "YOUTUBE — https://www.youtube.com/@wokomilkaa\n"
        "TIKTOK — https://www.tiktok.com/@wokomilka0\n"
        "DISCORD — https://discord.gg/r2zKspdrA5\n"
        "VK — https://vk.com/pikaper_amil"
    )


@registered_only
async def giveaway_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /giveaway command - show active giveaways."""
    giveaways = get_active_giveaways()
    if not giveaways:
        await update.message.reply_text(
            "🎁 Активных розыгрышей пока нет. Следите за обновлениями!"
        )
        return

    keyboard = []
    for g in giveaways:
        title = g['title'][:50]
        keyboard.append([
            InlineKeyboardButton(f"🎁 {title}", callback_data=f"gw:{g['id']}:view")
        ])

    await update.message.reply_text(
        "🎁 Активные розыгрыши:\nНажмите на розыгрыш, чтобы посмотреть детали:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _gw_edit(query, text, markup):
    """Edit a giveaway message, handling photo and text messages."""
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=markup)
        else:
            await query.edit_message_text(text=text, reply_markup=markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=markup)


async def giveaway_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок в розыгрышах (пользовательская версия)."""
    query = update.callback_query
    user = query.from_user
    await query.answer()

    data = query.data
    parts = data.split(":")

    if len(parts) == 2 and parts[1] == "back":
        giveaways = get_active_giveaways()
        if not giveaways:
            await _gw_edit(query, "🎁 Активных розыгрышей пока нет.",
                           InlineKeyboardMarkup([[
                               InlineKeyboardButton("⬅️ В меню", callback_data="gw:back")
                           ]]))
            return
        keyboard = []
        for g in giveaways:
            keyboard.append([
                InlineKeyboardButton(f"🎁 {g['title'][:50]}", callback_data=f"gw:{g['id']}:view")
            ])
        await _gw_edit(query, "🎁 Активные розыгрыши:", InlineKeyboardMarkup(keyboard))
        return

    if len(parts) < 3:
        return

    giveaway_id = int(parts[1])
    action = parts[2]

    giveaway = get_giveaway(giveaway_id)
    if not giveaway or giveaway['status'] != 'active':
        await _gw_edit(query, "❌ Розыгрыш недоступен или завершён.",
                       InlineKeyboardMarkup([[
                           InlineKeyboardButton("⬅️ К списку", callback_data="gw:back")
                       ]]))
        return

    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data=f"gw:{giveaway_id}:view")
    ]])

    if action == "view":
        confirmed = count_giveaway_participants(giveaway_id, 'confirmed')
        pending = count_pending_giveaway_participants(giveaway_id)
        text = (
            f"🎁 {giveaway['title']}\n\n"
            f"👥 Участников подтверждено: {confirmed}\n"
            f"⏳ На проверке: {pending}\n\n"
            f"Чтобы участвовать, нажмите кнопку ниже."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎫 Участвовать", callback_data=f"gw:{giveaway_id}:join")],
            [InlineKeyboardButton("⬅️ К списку", callback_data="gw:back")],
        ])
        if giveaway.get('image_file_id'):
            await query.message.reply_photo(
                photo=giveaway['image_file_id'],
                caption=text,
                reply_markup=keyboard
            )
        else:
            await _gw_edit(query, text, keyboard)
        return

    if action == "join":
        user_data = get_user(user.id)
        if not user_data or not user_data.get('is_registered'):
            await _gw_edit(query,
                "❌ Сначала пройдите регистрацию - пришлите скриншот Twitch-профиля модераторам.",
                back_kb)
            return

        existing = get_giveaway_participant(giveaway_id, user.id)
        if existing and existing['reg_status'] == 'confirmed':
            await _gw_edit(query,
                           "✅ Вы уже участвуете! Ожидайте результатов.",
                           back_kb)
            return
        if existing and existing['reg_status'] == 'pending':
            await _gw_edit(query,
                           "⏳ Ваша заявка на проверке модераторов.",
                           back_kb)
            return
        if existing and existing['reg_status'] == 'rejected':
            # После отклонения можно отправить новый скриншот.
            delete_rejected_giveaway_participant(giveaway_id, user.id)

        context.user_data['gw_join_giveaway_id'] = giveaway_id
        await _gw_edit(query,
            "📸 Пришлите скриншот своего Twitch-профиля для подтверждения участия. Модераторы проверят заявку.",
            back_kb)
        return


@registered_only
async def promo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /promo <code> command."""
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("ℹ️ Использование: /promo <код>")
        return

    code = context.args[0]
    result = use_promo_code(user.id, code)

    if result == "not_found":
        await update.message.reply_text("❌ Промокод не найден")
    elif result == "limit_reached":
        await update.message.reply_text("❌ У этого промокода закончились активации")
    elif result == "already_used":
        await update.message.reply_text("❌ Вы уже активировали этот промокод")
    elif result.startswith("ok:"):
        value = result.split(":")[1]
        await update.message.reply_text(f"✅ Промокод активирован! Начислено: {value}")
    else:
        await update.message.reply_text("❌ Ошибка активации промокода")


@registered_only
async def suggest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /suggest <text> command."""
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("ℹ️ Использование: /suggest <текст предложения>")
        return

    text = " ".join(context.args)
    suggestion_id = add_suggestion(user.id, user.username or user.first_name, text)

    await update.message.reply_text(
        f"✅ Ваше предложение #{suggestion_id} отправлено модераторам. Спасибо!"
    )

    # Forward suggestion to moderators, if configured
    for mod_id in MODERATOR_IDS:
        try:
            await context.bot.send_message(
                chat_id=mod_id,
                text=(
                    f"💡 Новое предложение #{suggestion_id}\n"
                    f"От: @{user.username or user.first_name} (ID: {user.id})\n\n"
                    f"{text}"
                )
            )
        except Exception as e:
            logger.warning(f"Could not notify moderator {mod_id}: {e}")


@registered_only
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /referral command."""
    user = update.effective_user
    user_data = get_user(user.id)

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user_data['referral_code']}"

    await update.message.reply_text(
        "👥 Ваша реферальная ссылка:\n"
        f"{link}\n\n"
        f"Приглашено друзей: {user_data.get('referrals_count', 0)}\n"
        f"Бонус за друга: +{REFERRAL_BONUS_REFERRER} вам, "
        f"+{REFERRAL_BONUS_REFERRED} другу"
    )


@registered_only
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /balance command."""
    user = update.effective_user
    user_data = get_user(user.id)

    await update.message.reply_text(f"💰 Ваш баланс: {user_data.get('balance', 0)}")


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myid command."""
    user = update.effective_user
    await update.message.reply_text(f"🆔 Ваш ID: {user.id}")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for admin giveaway title editing."""
    edit_id = context.user_data.get('admin_giveaway_edit_title_id')
    if not edit_id:
        return

    user = update.effective_user
    if not is_admin(user.id):
        context.user_data.pop('admin_giveaway_edit_title_id', None)
        return

    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("❌ Текст розыгрыша не может быть пустым.")
        return
    if len(title) > 900:
        await update.message.reply_text(
            "❌ Текст слишком длинный. Ограничьте описание 900 символами."
        )
        return

    if update_giveaway_title(edit_id, title):
        context.user_data.pop('admin_giveaway_edit_title_id', None)
        await update.message.reply_text(
            f"✅ Текст розыгрыша #{edit_id} изменён.\n"
            "Откройте /admin, чтобы проверить результат."
        )
    else:
        context.user_data.pop('admin_giveaway_edit_title_id', None)
        await update.message.reply_text("❌ Розыгрыш не найден.")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Роутер входящих фото.

    - Если фото прислал админ/модер:
      * в режиме создания розыгрыша -> создаём розыгрыш с текстом и картинкой
      * иначе -> отдаём file_id (удобно, чтобы взять картинку-инструкцию)
    - Если фото прислал зарегистрированный пользователь в режиме участия
      в розыгрыше -> скриншот для участия в розыгрыше
    - Если фото прислал незарегистрированный пользователь -> скриншот на
      регистрацию, кладём в очередь модерации и уведомляем staff.
    """
    user = update.effective_user
    file_id = update.message.photo[-1].file_id

    # Если staff-пользователь нажал «Участвовать», его скриншот должен пройти
    # обычную проверку участия, а не попасть в служебный режим file_id.
    waiting_for_giveaway_screenshot = context.user_data.get('gw_join_giveaway_id')

    if is_staff(user.id) and not waiting_for_giveaway_screenshot:
        edit_image_id = context.user_data.get('admin_giveaway_edit_image_id')
        if edit_image_id:
            updated = update_giveaway_image(edit_image_id, file_id)
            context.user_data.pop('admin_giveaway_edit_image_id', None)
            await update.message.reply_text(
                f"✅ Изображение розыгрыша #{edit_image_id} изменено."
                if updated else "❌ Розыгрыш не найден."
            )
            return

        if context.user_data.get('admin_giveaway_edit_title_id'):
            await update.message.reply_text(
                "✏️ Сейчас ожидается текст розыгрыша, а не изображение. "
                "Отправьте новый текст или откройте /admin заново для отмены."
            )
            return

        # Режим создания розыгрыша: админ прислал фото после /addgiveaway <текст>
        if context.user_data.get('new_giveaway_waiting_photo'):
            text = context.user_data.pop('new_giveaway_text')
            context.user_data.pop('new_giveaway_waiting_photo', None)
            gw_id = create_giveaway(text, file_id, user.id)
            await update.message.reply_text(
                f"✅ Розыгрыш #{gw_id} создан!\n\n"
                f"Управляйте розыгрышами через /admin."
            )
            return
        await update.message.reply_text(
            f"📎 file_id этой картинки:\n\n`{file_id}`\n\n"
            "Скопируйте эту строку целиком и вставьте в .env как:\n"
            f"TWITCH_EXAMPLE_FILE_ID={file_id}",
            parse_mode="Markdown"
        )
        return

    user_data = get_user(user.id)
    if not user_data:
        user_data = create_user(user.id, user.username or user.first_name)

    # Проверяем, ждёт ли пользователь подтверждения скриншота для розыгрыша
    gw_id = context.user_data.get('gw_join_giveaway_id')
    if user_data.get('is_registered') and gw_id:
        giveaway = get_giveaway(gw_id)
        if not giveaway or giveaway['status'] != 'active':
            context.user_data.pop('gw_join_giveaway_id', None)
            await update.message.reply_text("❌ Розыгрыш недоступен.")
            return

        existing = get_giveaway_participant(gw_id, user.id)
        if existing:
            if existing['reg_status'] == 'confirmed':
                await update.message.reply_text("✅ Вы уже участвуете в этом розыгрыше!")
            elif existing['reg_status'] == 'pending':
                await update.message.reply_text("⏳ Ваша заявка на проверке.")
            else:
                context.user_data.pop('gw_join_giveaway_id', None)
                await update.message.reply_text("❌ Ваша заявка была отклонена.")
            return

        ticket_number = random.randint(GIVEAWAY_TICKET_MIN, GIVEAWAY_TICKET_MAX)
        participant_id = add_giveaway_participant(gw_id, user.id, ticket_number, file_id)
        context.user_data.pop('gw_join_giveaway_id', None)

        await update.message.reply_text(
            "✅ Фото подтверждения отправлено модераторам.\n"
            "Заявка на участие принята на проверку. После подтверждения вы "
            "будете добавлены в список участников."
        )

        caption = (
            f"🎟 Заявка на участие в розыгрыше #{gw_id}\n"
            f"От: @{user.username or user.first_name} (ID: {user.id})\n"
            f"Билет: {ticket_number}"
        )
        for admin_id in STAFF_IDS:
            try:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Одобрить", callback_data=f"adm:gw:part_ok:{participant_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"adm:gw:part_no:{participant_id}"),
                    ]])
                )
            except Exception as e:
                logger.warning(f"Could not notify admin {admin_id}: {e}")
        return

    # Регистрационный скриншот
    if user_data.get('is_registered'):
        await update.message.reply_text("✅ Вы уже зарегистрированы, скриншот не нужен.")
        return

    set_registration_screenshot(user.id, file_id)
    await update.message.reply_text(
        "📸 Скриншот получен! Заявка отправлена модераторам на проверку. "
        "Ожидайте подтверждения - это обычно занимает немного времени."
    )

    caption = (
        "📥 Новая заявка на регистрацию\n"
        f"От: @{user.username or user.first_name} (ID: {user.id})"
    )
    for staff_id in STAFF_IDS:
        try:
            await context.bot.send_photo(
                chat_id=staff_id,
                photo=file_id,
                caption=caption,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"adm:reg:ok:{user.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"adm:reg:no:{user.id}"),
                ]])
            )
        except Exception as e:
            logger.warning(f"Could not notify staff {staff_id}: {e}")


async def _admin_edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
    """Update an admin callback message regardless of text/photo message type."""
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=markup)
        else:
            await query.edit_message_text(text=text, reply_markup=markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=markup)


async def _announce_giveaway_result(bot, giveaway: dict, winner: dict) -> int:
    """Announce a finished giveaway to every bot user and every applicant."""
    winner_user = get_user(winner['user_id'])
    winner_username = (winner_user or {}).get('username')
    winner_label = (
        f"@{winner_username}"
        if winner_username and " " not in winner_username
        else f"ID {winner['user_id']}"
    )
    text = (
        f"🏁 Итоги розыгрыша #{giveaway['id']}\n\n"
        f"🎁 {giveaway['title']}\n"
        f"🏆 Победитель: {winner_label}\n"
        f"🎟 Выигрышный билет: {winner['ticket_number']}\n\n"
        "Спасибо всем за участие!"
    )

    recipient_ids = set(get_all_user_ids())
    recipient_ids.update(get_giveaway_user_ids(giveaway['id']))
    sent_count = 0
    for recipient_id in recipient_ids:
        try:
            await bot.send_message(chat_id=recipient_id, text=text)
            sent_count += 1
            # Не превышаем обычный лимит массовых сообщений Telegram.
            await asyncio.sleep(0.04)
        except Exception as e:
            logger.warning(
                f"Could not announce giveaway {giveaway['id']} to {recipient_id}: {e}"
            )
    return sent_count


async def _show_giveaway_participants(query, giveaway_id: int,
                                      offset: int = 0) -> None:
    giveaway = get_giveaway(giveaway_id)
    if not giveaway:
        await _admin_edit(
            query,
            "❌ Розыгрыш не найден.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")]])
        )
        return

    total = count_all_giveaway_participants(giveaway_id)
    participants = get_all_giveaway_participants(
        giveaway_id,
        limit=GIVEAWAY_PARTICIPANTS_PER_PAGE,
        offset=offset
    )
    status_labels = {
        'confirmed': '✅ подтверждён',
        'pending': '⏳ на проверке',
        'rejected': '❌ отклонён',
    }
    if not participants:
        text = "👥 В этом розыгрыше пока нет заявок."
    else:
        lines = [
            f"👥 Участники розыгрыша #{giveaway_id} "
            f"({offset + 1}-{min(offset + len(participants), total)} из {total})",
            "",
        ]
        for number, participant in enumerate(participants, start=offset + 1):
            participant_user = get_user(participant['user_id'])
            username = (participant_user or {}).get('username') or str(participant['user_id'])
            status = status_labels.get(participant['reg_status'], participant['reg_status'])
            lines.append(
                f"{number}. @{username} — билет {participant['ticket_number']} — {status}"
            )
        text = "\n".join(lines)

    keyboard = []
    navigation = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️", callback_data=f"adm:gw:participants:{giveaway_id}:{max(0, offset - GIVEAWAY_PARTICIPANTS_PER_PAGE)}"
            )
        )
    if offset + GIVEAWAY_PARTICIPANTS_PER_PAGE < total:
        navigation.append(
            InlineKeyboardButton(
                "➡️", callback_data=f"adm:gw:participants:{giveaway_id}:{offset + GIVEAWAY_PARTICIPANTS_PER_PAGE}"
            )
        )
    if navigation:
        keyboard.append(navigation)
    keyboard.append([
        InlineKeyboardButton("⬅️ К розыгрышу", callback_data=f"adm:gw:view:{giveaway_id}")
    ])
    await _admin_edit(query, text, InlineKeyboardMarkup(keyboard))


async def _show_pending_giveaway_request(query, giveaway_id: int,
                                         offset: int = 0) -> None:
    """Send the next pending giveaway screenshot to a staff member."""
    giveaway = get_giveaway(giveaway_id)
    if not giveaway:
        await _admin_edit(
            query,
            "❌ Розыгрыш не найден.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")]])
        )
        return

    total = count_pending_giveaway_participants(giveaway_id)
    pending = get_pending_giveaway_participants(giveaway_id, limit=1, offset=offset)
    if not pending:
        await _admin_edit(
            query,
            "📸 Новых заявок на участие нет.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ К розыгрышу", callback_data=f"adm:gw:view:{giveaway_id}"
                )
            ]])
        )
        return

    entry = pending[0]
    participant_user = get_user(entry['user_id'])
    username = (participant_user or {}).get('username') or str(entry['user_id'])
    caption = (
        f"📸 Заявка на участие {offset + 1}/{total}\n"
        f"Розыгрыш: #{giveaway_id} — {giveaway['title'][:60]}\n"
        f"От: @{username} (ID: {entry['user_id']})\n"
        f"Билет: {entry['ticket_number']}"
    )

    keyboard = [[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"adm:gw:part_ok:{entry['id']}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"adm:gw:part_no:{entry['id']}"),
    ]]
    navigation = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️", callback_data=f"adm:gw:pending:{giveaway_id}:{offset - 1}"
            )
        )
    if offset + 1 < total:
        navigation.append(
            InlineKeyboardButton(
                "➡️", callback_data=f"adm:gw:pending:{giveaway_id}:{offset + 1}"
            )
        )
    if navigation:
        keyboard.append(navigation)
    keyboard.append([
        InlineKeyboardButton("⬅️ К розыгрышу", callback_data=f"adm:gw:view:{giveaway_id}")
    ])

    try:
        await query.message.reply_photo(
            photo=entry['screenshot_file_id'],
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.warning(f"Could not show giveaway participant {entry['id']}: {e}")
        await _admin_edit(query, caption, InlineKeyboardMarkup(keyboard))


def _admin_menu_markup() -> InlineKeyboardMarkup:
    pending_reg = count_pending_registrations()
    pending_sugg = count_pending_suggestions()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📸 Заявки на регистрацию ({pending_reg})", callback_data="adm:reg:0")],
        [InlineKeyboardButton(f"💬 Предложения ({pending_sugg})", callback_data="adm:sugg:0")],
        [InlineKeyboardButton(f"🎁 Розыгрыши ({count_active_giveaways()})", callback_data="adm:gw:list:0")],
        [InlineKeyboardButton("📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton("🎟 Промокоды (справка)", callback_data="adm:promo")],
    ])


def _admin_giveaways_markup(offset: int = 0) -> InlineKeyboardMarkup:
    """Build the admin giveaway list with pagination and creation shortcut."""
    giveaways = get_all_giveaways(ADMIN_GIVEAWAYS_PER_PAGE, offset)
    total = count_all_giveaways()
    keyboard = []

    for giveaway in giveaways:
        status_icon = "🟢" if giveaway['status'] == 'active' else "🏁"
        keyboard.append([
            InlineKeyboardButton(
                f"{status_icon} #{giveaway['id']} {giveaway['title'][:42]}",
                callback_data=f"adm:gw:view:{giveaway['id']}"
            )
        ])

    navigation = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton("⬅️", callback_data=f"adm:gw:list:{offset - ADMIN_GIVEAWAYS_PER_PAGE}")
        )
    if offset + ADMIN_GIVEAWAYS_PER_PAGE < total:
        navigation.append(
            InlineKeyboardButton("➡️", callback_data=f"adm:gw:list:{offset + ADMIN_GIVEAWAYS_PER_PAGE}")
        )
    if navigation:
        keyboard.append(navigation)

    keyboard.append([
        InlineKeyboardButton("➕ Добавить розыгрыш", callback_data="adm:gw:add")
    ])
    keyboard.append([
        InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")
    ])
    return InlineKeyboardMarkup(keyboard)


def _admin_giveaway_view_markup(giveaway_id: int, status: str,
                                confirmed: int, pending: int) -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton(
            "👥 Список участников",
            callback_data=f"adm:gw:participants:{giveaway_id}:0"
        )
    ]]
    if status == 'active':
        if pending:
            keyboard.append([
                InlineKeyboardButton(
                    f"📸 Проверить заявки ({pending})",
                    callback_data=f"adm:gw:pending:{giveaway_id}:0"
                )
            ])
        elif confirmed:
            keyboard.append([
                InlineKeyboardButton(
                    "🏁 Подвести итоги",
                    callback_data=f"adm:gw:draw:{giveaway_id}"
                )
            ])
    elif status == 'finished':
        keyboard.append([
            InlineKeyboardButton(
                "📣 Повторить объявление",
                callback_data=f"adm:gw:announce:{giveaway_id}"
            )
        ])
    keyboard.append([
        InlineKeyboardButton("✏️ Изменить", callback_data=f"adm:gw:edit:{giveaway_id}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"adm:gw:delete:{giveaway_id}")
    ])
    keyboard.append([
        InlineKeyboardButton("⬅️ К списку розыгрышей", callback_data="adm:gw:list:0")
    ])
    keyboard.append([
        InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")
    ])
    return InlineKeyboardMarkup(keyboard)


def _giveaway_details_text(giveaway: dict, confirmed: int, pending: int,
                           rejected: int) -> str:
    text = (
        f"🎁 Розыгрыш #{giveaway['id']}\n"
        f"{giveaway['title']}\n\n"
        f"Статус: {'активен' if giveaway['status'] == 'active' else 'завершён'}\n"
        f"✅ Подтверждено участников: {confirmed}\n"
        f"⏳ Заявок на проверке: {pending}\n"
        f"❌ Отклонено заявок: {rejected}"
    )
    if giveaway['status'] == 'finished':
        winner_id = giveaway.get('winner_id')
        winner = get_user(winner_id) if winner_id else None
        winner_name = winner.get('username') if winner else None
        winner_label = f"@{winner_name}" if winner_name else f"ID {winner_id}"
        text += (
            f"\n\n🏆 Победитель: {winner_label}\n"
            f"🎟 Билет победителя: {giveaway.get('winner_ticket')}"
        )
    return text


async def _show_admin_giveaway(query, giveaway: dict) -> None:
    confirmed = count_giveaway_participants(giveaway['id'], 'confirmed')
    pending = count_pending_giveaway_participants(giveaway['id'])
    rejected = count_giveaway_participants(giveaway['id'], 'rejected')
    text = _giveaway_details_text(giveaway, confirmed, pending, rejected)
    markup = _admin_giveaway_view_markup(
        giveaway['id'], giveaway['status'], confirmed, pending
    )

    if query.message and query.message.photo:
        await query.edit_message_caption(caption=text, reply_markup=markup)
    elif giveaway.get('image_file_id'):
        await query.message.reply_photo(
            photo=giveaway['image_file_id'],
            caption=text,
            reply_markup=markup
        )
    else:
        await query.edit_message_text(text, reply_markup=markup)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрытая команда управления ботом. Доступна только STAFF_IDS.
    Для всех остальных ведёт себя как неизвестная команда, чтобы не
    палить факт своего существования."""
    user = update.effective_user
    if not is_staff(user.id):
        await unknown_command(update, context)
        return

    context.user_data.pop('admin_giveaway_edit_title_id', None)
    context.user_data.pop('admin_giveaway_edit_image_id', None)

    await update.message.reply_text(
        "🛠 Панель управления",
        reply_markup=_admin_menu_markup()
    )


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок в /admin."""
    query = update.callback_query
    user = query.from_user

    if not is_staff(user.id):
        await query.answer("Недоступно", show_alert=True)
        return

    await query.answer()
    data = query.data  # формат: "adm:<section>:<payload>"
    parts = data.split(":")
    section = parts[1] if len(parts) > 1 else ""

    # --- Главное меню ---
    if data == "adm:menu":
        await query.edit_message_text("🛠 Панель управления", reply_markup=_admin_menu_markup())
        return

    # --- Розыгрыши ---
    if section == "gw":
        action = parts[2] if len(parts) > 2 else "list"

        if action == "list":
            offset = max(0, int(parts[3]) if len(parts) > 3 else 0)
            total = count_all_giveaways()
            text = (
                "🎁 Управление розыгрышами\n\n"
                f"Всего: {total}\n"
                f"Активных: {count_active_giveaways()}\n\n"
                "Выберите розыгрыш или создайте новый:"
            )
            await _admin_edit(query, text, _admin_giveaways_markup(offset))
            return

        if action == "add":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Добавлять розыгрыши могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ Назад", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            await query.edit_message_text(
                "➕ Создание розыгрыша\n\n"
                "1. Выполните команду:\n"
                "/addgiveaway <текст розыгрыша>\n\n"
                "2. После ответа бота пришлите изображение.\n"
                "Оно будет сохранено в Telegram и показано участникам.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Назад", callback_data="adm:gw:list:0")
                ]])
            )
            return

        if action == "edit":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Изменять розыгрыши могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ Назад", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            giveaway_id = int(parts[3])
            giveaway = get_giveaway(giveaway_id)
            if not giveaway:
                await _admin_edit(
                    query,
                    "❌ Розыгрыш не найден.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            await _admin_edit(
                query,
                f"✏️ Изменение розыгрыша #{giveaway_id}\n\n"
                f"Текущий текст:\n{giveaway['title']}\n\n"
                "Выберите, что изменить:",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✏️ Текст",
                        callback_data=f"adm:gw:edit_title:{giveaway_id}"
                    )],
                    [InlineKeyboardButton(
                        "🖼 Изображение",
                        callback_data=f"adm:gw:edit_image:{giveaway_id}"
                    )],
                    [InlineKeyboardButton(
                        "⬅️ К розыгрышу",
                        callback_data=f"adm:gw:view:{giveaway_id}"
                    )],
                ])
            )
            return

        if action == "edit_title":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Изменять розыгрыши могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            giveaway_id = int(parts[3])
            if not get_giveaway(giveaway_id):
                await _admin_edit(
                    query,
                    "❌ Розыгрыш не найден.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            context.user_data['admin_giveaway_edit_title_id'] = giveaway_id
            context.user_data.pop('admin_giveaway_edit_image_id', None)
            await _admin_edit(
                query,
                f"✏️ Пришлите новый текст для розыгрыша #{giveaway_id}.\n"
                "Чтобы отменить, откройте /admin заново.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ К розыгрышу",
                        callback_data=f"adm:gw:view:{giveaway_id}"
                    )
                ]])
            )
            return

        if action == "edit_image":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Изменять розыгрыши могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            giveaway_id = int(parts[3])
            if not get_giveaway(giveaway_id):
                await _admin_edit(
                    query,
                    "❌ Розыгрыш не найден.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            context.user_data['admin_giveaway_edit_image_id'] = giveaway_id
            context.user_data.pop('admin_giveaway_edit_title_id', None)
            await _admin_edit(
                query,
                f"🖼 Пришлите новое изображение для розыгрыша #{giveaway_id}.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ К розыгрышу",
                        callback_data=f"adm:gw:view:{giveaway_id}"
                    )
                ]])
            )
            return

        if action == "delete":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Удалять розыгрыши могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            giveaway_id = int(parts[3])
            giveaway = get_giveaway(giveaway_id)
            if not giveaway:
                await _admin_edit(
                    query,
                    "❌ Розыгрыш уже удалён или не найден.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            await _admin_edit(
                query,
                f"🗑 Удалить розыгрыш #{giveaway_id}?\n\n"
                f"{giveaway['title']}\n\n"
                "Будут удалены также все заявки и список участников. Действие нельзя отменить.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🗑 Да, удалить",
                        callback_data=f"adm:gw:delete_confirm:{giveaway_id}"
                    )],
                    [InlineKeyboardButton(
                        "↩️ Отмена",
                        callback_data=f"adm:gw:view:{giveaway_id}"
                    )],
                ])
            )
            return

        if action == "delete_confirm":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Удалять розыгрыши могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            giveaway_id = int(parts[3])
            deleted = delete_giveaway(giveaway_id)
            if context.user_data.get('admin_giveaway_edit_title_id') == giveaway_id:
                context.user_data.pop('admin_giveaway_edit_title_id', None)
            if context.user_data.get('admin_giveaway_edit_image_id') == giveaway_id:
                context.user_data.pop('admin_giveaway_edit_image_id', None)
            await _admin_edit(
                query,
                "✅ Розыгрыш удалён." if deleted else "❌ Розыгрыш уже удалён или не найден.",
                _admin_giveaways_markup(0)
            )
            return

        if action == "view":
            giveaway_id = int(parts[3])
            giveaway = get_giveaway(giveaway_id)
            if not giveaway:
                await _admin_edit(
                    query,
                    "❌ Розыгрыш не найден.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            await _show_admin_giveaway(query, giveaway)
            return

        if action == "pending":
            giveaway_id = int(parts[3])
            offset = max(0, int(parts[4]) if len(parts) > 4 else 0)
            await _show_pending_giveaway_request(query, giveaway_id, offset)
            return

        if action == "participants":
            giveaway_id = int(parts[3])
            offset = max(0, int(parts[4]) if len(parts) > 4 else 0)
            await _show_giveaway_participants(query, giveaway_id, offset)
            return

        if action in ("part_ok", "part_no"):
            participant_id = int(parts[3])
            approved = action == "part_ok"
            participant = review_giveaway_participant(participant_id, approved, user.id)
            if not participant:
                await _admin_edit(
                    query,
                    "❌ Заявка не найдена.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return
            if participant['reg_status'] != 'pending':
                await _admin_edit(
                    query,
                    "ℹ️ Эта заявка уже обработана.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "⬅️ К розыгрышу",
                            callback_data=f"adm:gw:view:{participant['giveaway_id']}"
                        )
                    ]])
                )
                return

            target_id = participant['user_id']
            verdict = "подтверждена" if approved else "отклонена"
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        f"{'✅' if approved else '❌'} Заявка на участие в розыгрыше "
                        f"#{participant['giveaway_id']} {verdict}."
                        + (" Ваш билет: " + str(participant['ticket_number']) if approved else "")
                    )
                )
            except Exception as e:
                logger.warning(f"Could not notify giveaway participant {target_id}: {e}")

            giveaway = get_giveaway(participant['giveaway_id'])
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ К розыгрышу",
                    callback_data=f"adm:gw:view:{participant['giveaway_id']}"
                )
            ]])
            await _admin_edit(
                query,
                f"{'✅ Заявка одобрена.' if approved else '❌ Заявка отклонена.'}\n"
                f"Участник: ID {target_id}",
                markup
            )
            if giveaway and count_pending_giveaway_participants(giveaway['id']):
                await _show_pending_giveaway_request(query, giveaway['id'], 0)
            return

        if action == "announce":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Объявлять итоги могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return

            giveaway_id = int(parts[3])
            giveaway = get_giveaway(giveaway_id)
            if not giveaway or giveaway['status'] != 'finished' or not giveaway.get('winner_id'):
                await _admin_edit(
                    query,
                    "❌ Итоги ещё не подведены или розыгрыш не найден.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return

            winner = {
                'user_id': giveaway['winner_id'],
                'ticket_number': giveaway['winner_ticket'],
            }
            sent_count = await _announce_giveaway_result(context.bot, giveaway, winner)
            await _admin_edit(
                query,
                f"📣 Итоги повторно объявлены. Уведомлено пользователей: {sent_count}",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ К розыгрышу", callback_data=f"adm:gw:view:{giveaway_id}"
                    )
                ]])
            )
            return

        if action == "draw":
            if not is_admin(user.id):
                await _admin_edit(
                    query,
                    "⚠️ Проводить розыгрыш могут только админы.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К розыгрышу", callback_data="adm:gw:list:0")
                    ]])
                )
                return

            giveaway_id = int(parts[3])
            giveaway = get_giveaway(giveaway_id)
            if not giveaway or giveaway['status'] != 'active':
                await _admin_edit(
                    query,
                    "❌ Этот розыгрыш уже завершён или не найден.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                    ]])
                )
                return

            pending = count_pending_giveaway_participants(giveaway_id)
            if pending:
                await _admin_edit(
                    query,
                    f"⏳ Сначала обработайте все заявки на участие. Осталось: {pending}",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "📸 Проверить заявки",
                            callback_data=f"adm:gw:pending:{giveaway_id}:0"
                        )
                    ], [
                        InlineKeyboardButton(
                            "⬅️ К розыгрышу", callback_data=f"adm:gw:view:{giveaway_id}"
                        )
                    ]])
                )
                return

            winner = draw_giveaway_winner(giveaway_id)
            if not winner:
                await _admin_edit(
                    query,
                    "❌ Нельзя провести розыгрыш: нет подтверждённых участников.",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "⬅️ К розыгрышу", callback_data=f"adm:gw:view:{giveaway_id}"
                        )
                    ]])
                )
                return

            sent_count = await _announce_giveaway_result(context.bot, giveaway, winner)
            winner_user = get_user(winner['user_id'])
            winner_username = (winner_user or {}).get('username')
            winner_label = (
                f"@{winner_username}"
                if winner_username and " " not in winner_username
                else f"ID {winner['user_id']}"
            )

            await _admin_edit(
                query,
                f"🏁 Итоги подведены!\n\n"
                f"🎁 {giveaway['title']}\n"
                f"🏆 Победитель: {winner_label}\n"
                f"🎟 Билет: {winner['ticket_number']}\n"
                f"📣 Уведомлено пользователей: {sent_count}",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ К списку", callback_data="adm:gw:list:0")
                ]])
            )
            return

    # --- Статистика ---
    if section == "stats":
        s = get_stats()
        await query.edit_message_text(
            "📊 Статистика бота:\n\n"
            f"👥 Всего пользователей: {s['total_users']}\n"
            f"✅ Зарегистрировано: {s['registered']}\n"
            f"⏳ Заявок на проверке: {s['pending_reg']}\n"
            f"💰 Суммарный баланс: {s['total_balance']}\n"
            f"🎫 Билетов куплено: {s['tickets_sold']}\n"
            f"💬 Предложений в очереди: {s['pending_suggestions']}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="adm:menu")
            ]])
        )
        return

    # --- Справка по промокодам ---
    if section == "promo":
        text = (
            "🎟 Управление промокодами\n\n"
            "Добавить новый промокод:\n"
            "/addpromo <код> <сумма> <лимит активаций>\n"
            "Пример: /addpromo DEP100 100 50\n"
            "Лимит 0 = без ограничений."
        )
        if not is_admin(user.id):
            text += "\n\n⚠️ Добавлять промокоды могут только админы."
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="adm:menu")
            ]])
        )
        return

    # --- Заявки на регистрацию ---
    if section == "reg":
        # adm:reg:<offset>  или  adm:reg:ok:<user_id>  /  adm:reg:no:<user_id>
        action_or_offset = parts[2]

        if action_or_offset in ("ok", "no"):
            target_id = int(parts[3])
            approved = action_or_offset == "ok"
            review_registration(target_id, approved, user.id)

            try:
                if approved:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text="✅ Ваша регистрация подтверждена! Теперь доступны /giveaway, /promocodes, /social, /promo и /referral."
                    )
                else:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text="❌ Ваш скриншот отклонён модератором. Пришлите новый, пожалуйста."
                    )
            except Exception as e:
                logger.warning(f"Could not notify user {target_id}: {e}")

            verdict = "✅ Одобрено" if approved else "❌ Отклонено"
            await query.edit_message_caption(caption=f"{query.message.caption}\n\n{verdict}")
            return

        offset = int(action_or_offset)
        pending = get_pending_registrations(limit=1, offset=offset)
        total = count_pending_registrations()

        if not pending:
            await query.edit_message_text(
                "📸 Нет заявок на проверке." if total == 0 else "Заявок больше нет на этой странице.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Назад", callback_data="adm:menu")
                ]])
            )
            return

        entry = pending[0]
        caption = (
            f"📸 Заявка {offset + 1}/{total}\n"
            f"От: @{entry['username']} (ID: {entry['user_id']})"
        )
        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"adm:reg:{offset - 1}"))
        if offset + 1 < total:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"adm:reg:{offset + 1}"))

        keyboard = [[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"adm:reg:ok:{entry['user_id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"adm:reg:no:{entry['user_id']}"),
        ]]
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")])

        # Отправляем новым сообщением, т.к. текущее может быть текстовым, а не фото
        await query.message.reply_photo(
            photo=entry['screenshot_file_id'],
            caption=caption,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # --- Предложения ---
    if section == "sugg":
        action_or_offset = parts[2]

        if action_or_offset == "done":
            suggestion_id = int(parts[3])
            mark_suggestion_reviewed(suggestion_id)
            await query.edit_message_text(
                "✅ Предложение отмечено как рассмотренное.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")
                ]])
            )
            return

        offset = int(action_or_offset)
        pending = get_pending_suggestions(limit=1, offset=offset)
        total = count_pending_suggestions()

        if not pending:
            await query.edit_message_text(
                "💬 Нет новых предложений." if total == 0 else "Предложений больше нет на этой странице.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")
                ]])
            )
            return

        entry = pending[0]
        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"adm:sugg:{offset - 1}"))
        if offset + 1 < total:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"adm:sugg:{offset + 1}"))

        keyboard = [[
            InlineKeyboardButton("✅ Отметить рассмотренным", callback_data=f"adm:sugg:done:{entry['id']}")
        ]]
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("⬅️ В меню", callback_data="adm:menu")])

        await query.edit_message_text(
            f"💬 Предложение {offset + 1}/{total}\n"
            f"От: @{entry['username']} (ID: {entry['user_id']})\n\n"
            f"{entry['text']}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return


async def addgiveaway_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /addgiveaway <текст> - create a new giveaway. Only for admins."""
    user = update.effective_user
    if not is_admin(user.id):
        await unknown_command(update, context)
        return

    # Новая команда всегда начинает новый сценарий создания.
    context.user_data.pop('new_giveaway_text', None)
    context.user_data.pop('new_giveaway_waiting_photo', None)

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Использование: /addgiveaway <текст розыгрыша>\n"
            "После команды пришлите изображение для розыгрыша."
        )
        return

    text = " ".join(context.args).strip()
    context.user_data['new_giveaway_text'] = text
    context.user_data['new_giveaway_waiting_photo'] = True
    await update.message.reply_text(
        f"Текст розыгрыша: {text}\n\nТеперь пришлите изображение для розыгрыша."
    )


async def addpromo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /addpromo <код> <сумма> <лимит> - только для админов."""
    user = update.effective_user
    if not is_admin(user.id):
        await unknown_command(update, context)
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "ℹ️ Использование: /addpromo <код> <сумма> <лимит активаций>\n"
            "Лимит 0 = без ограничений.\n"
            "Пример: /addpromo DEP100 100 50"
        )
        return

    code, value_str, limit_str = context.args
    try:
        value = int(value_str)
        uses_limit = int(limit_str)
    except ValueError:
        await update.message.reply_text("❌ Сумма и лимит должны быть числами.")
        return

    created = create_promo_code(code, value, uses_limit if uses_limit > 0 else None)
    if created:
        await update.message.reply_text(f"✅ Промокод {code.upper()} создан (сумма: {value}, лимит: {uses_limit or '∞'}).")
    else:
        await update.message.reply_text("❌ Такой промокод уже существует.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Неизвестная команда. Используйте /help, чтобы увидеть список команд."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")


# Entrypoint

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("giveaway", giveaway_command))
    application.add_handler(CommandHandler("promocodes", promocodes_command))
    application.add_handler(CommandHandler("social", social_command))
    application.add_handler(CommandHandler("promo", promo_command))
    application.add_handler(CommandHandler("suggest", suggest_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("addgiveaway", addgiveaway_command))
    application.add_handler(CommandHandler("addpromo", addpromo_command))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(CallbackQueryHandler(giveaway_callback_handler, pattern=r"^gw:"))
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern=r"^adm:"))

    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    application.add_error_handler(error_handler)

    logger.info("Bot started, polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()