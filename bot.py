import sys
import subprocess

# Auto-install missing dependencies if running locally
required_libs = ["aiosqlite", "aiohttp", "aiogram"]
for lib in required_libs:
    try:
        __import__(lib)
    except ImportError:
        print(f"📦 Auto-installing missing module: {lib}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", lib])
        except Exception as e:
            print(f"⚠️ Failed to install {lib}: {e}")

import os
import asyncio
import logging
from datetime import datetime

import aiosqlite
import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    BusinessConnection
)
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

BOT_TOKEN = os.getenv("BOT_TOKEN", "8668761677:AAGw4pWsnxaLsYVnWAr1xZtQEOnvlLHnX0I")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
PORT = int(os.getenv("PORT", "10000"))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

class AsyncDatabaseManager:
    """Async SQLite Database Manager using aiosqlite with WAL mode"""
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_db(self):
        """Initialize SQLite database tables"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT,
                    chat_id TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS focus_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_name TEXT,
                    message_text TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT,
                    remind_time TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('focus_mode', '0')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_reply_mode', '1')")
            await db.commit()

    async def get_setting(self, key: str, default: str = '0') -> str:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                res = await cursor.fetchone()
                return res[0] if res else default

    async def toggle_setting(self, key: str) -> bool:
        current = await self.get_setting(key, '0')
        new_val = '0' if current == '1' else '1'
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE settings SET value = ? WHERE key = ?", (new_val, key))
            await db.commit()
        return new_val == '1'

    async def is_focus_mode(self) -> bool:
        return (await self.get_setting('focus_mode')) == '1'

    async def is_ai_reply_enabled(self) -> bool:
        return (await self.get_setting('ai_reply_mode')) == '1'

    async def log_message(self, user_name: str, chat_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO stats (user_name, chat_id) VALUES (?, ?)", (user_name, chat_id))
            await db.commit()

    async def log_focus_message(self, sender_name: str, message_text: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO focus_logs (sender_name, message_text) VALUES (?, ?)", (sender_name, message_text))
            await db.commit()

    async def get_today_stats(self):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM stats WHERE DATE(timestamp) = DATE('now')") as cursor:
                total_row = await cursor.fetchone()
                total = total_row[0] if total_row else 0
            
            async with db.execute("""
                SELECT user_name, COUNT(*) as cnt 
                FROM stats 
                WHERE DATE(timestamp) = DATE('now') 
                GROUP BY user_name 
                ORDER BY cnt DESC 
                LIMIT 3
            """) as cursor:
                top_chats = await cursor.fetchall()
            return total, top_chats

db = AsyncDatabaseManager(DB_PATH)

TEMPLATES = [
    "📌 Я сейчас на уроке / занят, отвечу сразу как освобожусь!",
    "⚡ На связи! Посмотрел твоё сообщение, скину чуть позже.",
    "🤝 Понял тебя, принял! Скоро вернусь с ответом.",
    "⏳ Сейчас у меня режим фокусировки. Отвечу в течение часа."
]

async def generate_groq_reply(user_prompt: str, sender_name: str) -> str:
    """Generate instant response using Groq LPU API (Llama 3.3 70B)"""
    if not GROQ_API_KEY:
        return f"🤖 Здравствуйте, {sender_name}! Сообщение получено, скоро свяжусь с вами."

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты — умный, вежливый и краткий бизнес-ассистент в Telegram. "
                    "Отвечай на русском языке очень коротко (1-2 предложения). "
                    "Передай собеседнику, что владелец аккаунта сейчас занят, но обязательно ответит позже."
                )
            },
            {
                "role": "user",
                "content": f"Собеседник по имени {sender_name} написал: \"{user_prompt}\". Напиши краткий вежливый ответ."
            }
        ],
        "temperature": 0.5,
        "max_tokens": 100
    }

    try:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(url, json=payload, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    reply = data["choices"][0]["message"]["content"].strip()
                    return f"⚡ **Groq AI:**\n{reply}"
                else:
                    logging.error(f"⚠️ Groq API Status error: {resp.status}")
    except Exception as e:
        logging.error(f"❌ Error requesting Groq API: {e}")

    return f"🤖 Здравствуйте, {sender_name}! Сообщение принял, скоро отвечу!"

def get_main_keyboard(focus_active: bool = False, ai_active: bool = True):
    """Inline control keyboard with status indicators"""
    focus_label = "🟢 Фокус: ВКЛ" if focus_active else "🔴 Фокус: ВЫКЛ"
    ai_label = "⚡ Groq AI: ВКЛ" if ai_active else "⚡ Groq AI: ВЫКЛ"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="view_stats"),
            InlineKeyboardButton(text=focus_label, callback_data="toggle_focus")
        ],
        [
            InlineKeyboardButton(text=ai_label, callback_data="toggle_ai"),
            InlineKeyboardButton(text="⚡ Шаблоны", callback_data="view_templates")
        ],
        [
            InlineKeyboardButton(text="⏰ Напоминания", callback_data="view_reminders_info")
        ]
    ])

connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=15)
session = AiohttpSession(connector=connector)

bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()

@dp.business_connection()
async def on_business_connect(bc: BusinessConnection):
    logging.info(f"✅ Telegram Business connected! ID: {bc.id}")

@dp.business_message()
@dp.message(F.chat.type == "private")
async def handle_all_messages(message: Message):
    sender_name = message.from_user.first_name if message.from_user else "Собеседник"
    chat_id = str(message.chat.id)
    msg_text = message.text or message.caption or ""

    if msg_text.startswith("/"):
        return

    await db.log_message(sender_name, chat_id)

    focus_on = await db.is_focus_mode()
    ai_on = await db.is_ai_reply_enabled()

    if focus_on:
        await db.log_focus_message(sender_name, msg_text)
        logging.info(f"🔕 [FOCUS] Logged message from {sender_name}: {msg_text}")
    elif ai_on:
        ai_response = await generate_groq_reply(msg_text, sender_name)
        try:
            biz_conn_id = getattr(message, 'business_connection_id', None)
            if biz_conn_id:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=ai_response,
                    business_connection_id=biz_conn_id
                )
            else:
                await message.reply(ai_response)
        except Exception as e:
            logging.error(f"❌ Error sending AI response: {e}")

@dp.message(CommandStart())
async def cmd_start(message: Message):
    focus_active = await db.is_focus_mode()
    ai_active = await db.is_ai_reply_enabled()
    
    welcome_text = (
        "👋 **Привет! Я твой Telegram Business Ассистент на базе Groq LPU AI.**\n\n"
        "⚡ **Главные функции:**\n"
        "1. 🧠 **Groq AI (Llama 3.3)**: Моментальный отклик за 0.1 секунды.\n"
        "2. 🔕 **Режим Фокуса**: Бесшумно записывает сообщения во время учебы.\n"
        "3. 📊 **Статистика**: Анализирует входящую активность.\n"
        "4. ⏰ **Напоминания**: Распознает даты в пересланных сообщениях."
    )
    
    biz_conn_id = getattr(message, 'business_connection_id', None)
    if biz_conn_id:
        await bot.send_message(
            chat_id=message.chat.id,
            text=welcome_text,
            business_connection_id=biz_conn_id,
            reply_markup=get_main_keyboard(focus_active, ai_active)
        )
    else:
        await message.answer(welcome_text, reply_markup=get_main_keyboard(focus_active, ai_active))

@dp.callback_query()
async def process_callbacks(callback: CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass

    target_text = ""
    
    if callback.data == "view_stats":
        total, top_chats = await db.get_today_stats()
        top_str = "\n".join([f"• {name}: **{count}**" for name, count in top_chats]) if top_chats else "Нет данных"
        target_text = f"📊 **Аналитика за сегодня:**\nВсего обработано: **{total}**\n\n🏆 **Топ собеседников:**\n{top_str}"
        
    elif callback.data == "toggle_focus":
        is_active = await db.toggle_setting('focus_mode')
        status_str = "🟢 ВКЛЮЧЕН" if is_active else "🔴 ВЫКЛЮЧЕН"
        target_text = f"⚙️ **Режим фокуса изменен!**\nТекущий статус: **{status_str}**"
        
    elif callback.data == "toggle_ai":
        is_active = await db.toggle_setting('ai_reply_mode')
        status_str = "🟢 ВКЛЮЧЕН" if is_active else "🔴 ВЫКЛЮЧЕН"
        target_text = f"🧠 **Groq AI автоотвечик изменен!**\nТекущий статус: **{status_str}**"

    elif callback.data == "view_templates":
        target_text = "⚡ **Быстрые шаблоны ответов:**\n\n"
        for i, t in enumerate(TEMPLATES, 1):
            target_text += f"{i}. `{t}`\n\n"
            
    elif callback.data == "view_reminders_info":
        target_text = "💡 **Как ставить напоминания:**\nПерешлите боту сообщение с датой или временем, и он зафиксирует задачу!"

    focus_active = await db.is_focus_mode()
    ai_active = await db.is_ai_reply_enabled()

    if target_text and callback.message:
        try:
            biz_conn_id = getattr(callback.message, 'business_connection_id', None)
            if biz_conn_id:
                await bot.edit_message_text(
                    text=target_text,
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    business_connection_id=biz_conn_id,
                    reply_markup=get_main_keyboard(focus_active, ai_active)
                )
            else:
                await callback.message.edit_text(
                    text=target_text,
                    reply_markup=get_main_keyboard(focus_active, ai_active)
                )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                logging.warning(f"⚠️ Error editing message: {e}")

async def handle_health_check(request):
    return web.Response(text="OK - Bot is running!")

async def start_dummy_server():
    """Start simple Web Server for Render Web Service health checks"""
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"🌐 Health check web server running on port {PORT}")

async def start_bot_with_retry():
    retry_count = 0
    while True:
        try:
            logging.info("⏳ Connecting to Telegram API...")
            await bot.delete_webhook(drop_pending_updates=True)
            logging.info("🚀 Bot with Groq LPU AI started successfully!")
            retry_count = 0
            await dp.start_polling(bot, polling_timeout=10)
            break
        except TelegramNetworkError as e:
            retry_count += 1
            logging.warning(f"🌐 Network error (attempt {retry_count}): {e}. Retrying...")
            await asyncio.sleep(3)
        except Exception as e:
            logging.error(f"❌ Application error: {e}")
            await asyncio.sleep(3)

async def main():
    await db.init_db()
    await start_dummy_server()
    await start_bot_with_retry()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
