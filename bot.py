import asyncio
import os
import sys
import sqlite3
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import requests
import whisper
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeDefault

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
load_dotenv()

# ---------- АВТООПРЕДЕЛЕНИЕ FFMPEG ----------
print("🔧 Настройка ffmpeg...")

def find_ffmpeg():
    """Автоматически находит ffmpeg на разных платформах"""
    
    # 1. Проверяем, есть ли ffmpeg в PATH
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✅ ffmpeg найден в PATH")
            return 'ffmpeg'
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    
    # 2. Проверяем стандартные пути для Windows
    if sys.platform == 'win32':
        possible_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\ffmpeg\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        ]
        for path in possible_paths:
            if os.path.exists(path):
                print(f"✅ Найден ffmpeg: {path}")
                return path
    
    # 3. Для Linux/Mac проверяем стандартные пути
    else:
        possible_paths = [
            '/usr/bin/ffmpeg',
            '/usr/local/bin/ffmpeg',
            '/opt/ffmpeg/bin/ffmpeg',
        ]
        for path in possible_paths:
            if os.path.exists(path):
                print(f"✅ Найден ffmpeg: {path}")
                return path
    
    print("❌ ffmpeg не найден! Будет использован системный путь.")
    return 'ffmpeg'

# Находим ffmpeg
FFMPEG_PATH = find_ffmpeg()

# Добавляем в PATH если это путь к файлу
if FFMPEG_PATH != 'ffmpeg' and os.path.exists(os.path.dirname(FFMPEG_PATH)):
    ffmpeg_dir = os.path.dirname(FFMPEG_PATH)
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ["PATH"]
    
# Настраиваем Whisper
try:
    whisper.ffmpeg = FFMPEG_PATH
    print(f"✅ Whisper настроен на: {FFMPEG_PATH}")
except:
    pass

# Проверяем работу
try:
    result = subprocess.run([FFMPEG_PATH, '-version'], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
        print("✅ ffmpeg работает")
        print(result.stdout.split('\n')[0])
    else:
        print("⚠️ ffmpeg не отвечает")
except Exception as e:
    print(f"⚠️ Ошибка проверки ffmpeg: {e}")

# ---------- КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")
API_KEY = os.getenv("API_KEY")

if not BOT_TOKEN:
    logging.error("BOT_TOKEN не найден в .env файле!")
    exit(1)
if not FOLDER_ID or not API_KEY:
    logging.warning("FOLDER_ID или API_KEY не найдены. Суммаризация не будет работать!")

# ---------- ИНИЦИАЛИЗАЦИЯ ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------- WHISPER ----------
print("🔧 Загрузка Whisper модели...")
whisper_model = None
try:
    # На сервере лучше использовать модель "tiny" или "base"
    # "tiny" - 75 MB, быстрее, чуть хуже качество
    # "base" - 145 MB, хорошее качество
    model_size = os.getenv("WHISPER_MODEL", "base")  # можно указать в .env
    whisper_model = whisper.load_model(model_size)
    print(f"✅ Whisper модель '{model_size}' загружена")
except Exception as e:
    print(f"❌ Ошибка загрузки Whisper: {e}")

# ---------- FSM ----------
class SummaryStates(StatesGroup):
    waiting_for_hours = State()

# ---------- БАЗА ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user TEXT,
        user_id INTEGER,
        text TEXT,
        time TEXT,
        type TEXT
    )''')
    conn.commit()
    conn.close()
    logging.info("Database initialized")

init_db()

# ---------- ФУНКЦИИ БД ----------
async def save_message(chat_id, user, user_id, text, msg_type):
    def sync_save():
        try:
            current_time = datetime.now(timezone.utc).isoformat()
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            c.execute("INSERT INTO messages (chat_id, user, user_id, text, type, time) VALUES (?, ?, ?, ?, ?, ?)",
                      (chat_id, user, user_id, text, msg_type, current_time))
            conn.commit()
            conn.close()
            logging.info(f"Saved message from {user}")
        except Exception as e:
            logging.error(f"Error saving message: {e}")
    
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, sync_save)

async def get_messages(chat_id, hours):
    def sync_get():
        try:
            time_limit = datetime.now(timezone.utc) - timedelta(hours=hours)
            time_limit_str = time_limit.isoformat()
            
            conn = sqlite3.connect('database.db')
            c = conn.cursor()
            c.execute("SELECT user, text FROM messages WHERE chat_id = ? AND time >= ? ORDER BY time ASC",
                      (chat_id, time_limit_str))
            messages = c.fetchall()
            conn.close()
            return messages
        except Exception as e:
            logging.error(f"Error getting messages: {e}")
            return []
    
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, sync_get)

# ---------- ФУНКЦИЯ ДЛЯ ИЗВЛЕЧЕНИЯ АУДИО ИЗ ВИДЕО ----------
async def extract_audio_from_video(video_path: str, audio_path: str) -> bool:
    try:
        cmd = [FFMPEG_PATH, '-i', video_path, '-vn', '-acodec', 'mp3', '-y', audio_path]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.wait()
        return process.returncode == 0 and os.path.exists(audio_path)
    except Exception as e:
        logging.error(f"Error extracting audio: {e}")
        return False

# ---------- WHISPER ФУНКЦИЯ ----------
async def voice_to_text(file_path: str) -> str:
    if not whisper_model:
        return "[Whisper не загружен]"
    
    try:
        if not os.path.exists(file_path):
            return "[Файл не найден]"
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: whisper_model.transcribe(file_path, language="ru"))
        text = result["text"].strip()
        return text if text else "[Не распознано]"
    except Exception as e:
        logging.error(f"Voice recognition error: {e}")
        return f"[Ошибка: {e}]"

# ---------- YANDEXGPT ----------
async def summarize_with_yandex(messages: list) -> str:
    if not messages:
        return "Сообщений за указанный период нет."
    
    if not FOLDER_ID or not API_KEY:
        return "YandexGPT не настроен."
    
    if len(messages) > 100:
        messages = messages[-100:]
        note = "\n\n(показаны только последние 100 сообщений)"
    else:
        note = ""
    
    # Формируем диалог
    dialog = "\n".join([f"{user}: {text}" for user, text in messages])
    
    prompt = (
        "Ниже приведен диалог. Напиши краткое содержание. "
        "Укажи основные темы, о чем говорили участники. "
        "Не добавляй лишних комментариев. Пиши просто и кратко.\n\n"
        f"{dialog}"
    )
    
    body = {
        "modelUri": f"gpt://{FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {
            "stream": False,
            "temperature": 0.3,
            "maxTokens": 500
        },
        "messages": [
            {"role": "system", "text": "Ты пишешь краткое содержание диалога."},
            {"role": "user", "text": prompt}
        ]
    }
    
    headers = {
        "Authorization": f"Api-Key {API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            json=body,
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        
        if "result" in data and "alternatives" in data["result"]:
            text = data["result"]["alternatives"][0]["message"]["text"]
            return text + note
        else:
            return "Ошибка ответа от YandexGPT"
            
    except Exception as e:
        logging.error(f"YandexGPT error: {e}")
        return f"Ошибка: {e}"

# ---------- УСТАНОВКА КНОПОК КОМАНД ----------
async def set_commands():
    commands = [
        BotCommand(command="summary", description="Сделать суммаризацию чата"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logging.info("Bot commands set")

# ---------- ОБРАБОТЧИКИ ----------
@dp.message(Command("summary"))
async def summary_command(message: types.Message, state: FSMContext):
    await state.clear()
    await state.update_data(chat_id=message.chat.id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 час", callback_data="hours_1"),
            InlineKeyboardButton(text="2 часа", callback_data="hours_2"),
            InlineKeyboardButton(text="4 часа", callback_data="hours_4"),
        ],
        [
            InlineKeyboardButton(text="8 часов", callback_data="hours_8"),
            InlineKeyboardButton(text="12 часов", callback_data="hours_12"),
            InlineKeyboardButton(text="24 часа", callback_data="hours_24"),
        ],
        [
            InlineKeyboardButton(text="2 дня", callback_data="hours_48"),
            InlineKeyboardButton(text="3 дня", callback_data="hours_72"),
            InlineKeyboardButton(text="Неделя", callback_data="hours_168"),
        ],
        [
            InlineKeyboardButton(text="Свой период", callback_data="custom_hours"),
        ]
    ])
    
    await message.answer("Выберите период:", reply_markup=keyboard)
    await state.set_state(SummaryStates.waiting_for_hours)

@dp.message(Command("help"))
async def help_command(message: types.Message):
    await message.answer(
        "🤖 Бот для суммаризации чата\n\n"
        "Я слушаю все сообщения в чате и запоминаю их.\n\n"
        "Используйте /summary, чтобы получить краткое содержание.\n\n"
        "Поддерживаются: текст, голосовые, видеосообщения."
    )

@dp.callback_query(lambda c: c.data.startswith("hours_") or c.data == "custom_hours")
async def hours_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "custom_hours":
        await callback.message.answer("Введите количество часов (1-168):")
        await state.set_state(SummaryStates.waiting_for_hours)
        return
    
    hours = int(callback.data.split("_")[1])
    
    data = await state.get_data()
    chat_id = data.get("chat_id")
    
    if not chat_id:
        await callback.message.answer("Ошибка")
        await state.clear()
        return
    
    status_msg = await callback.message.answer(f"Получаю сообщения за {hours} часов...")
    messages = await get_messages(chat_id, hours)
    
    if not messages:
        await status_msg.delete()
        await callback.message.answer(f"Сообщений за последние {hours} часов не найдено.")
        await state.clear()
        return
    
    await status_msg.edit_text(f"Суммаризирую {len(messages)} сообщений...")
    summary = await summarize_with_yandex(messages)
    
    await status_msg.delete()
    await callback.message.answer(f"📝 Суммаризация за {hours} ч:\n\n{summary}")
    await state.clear()

@dp.message(SummaryStates.waiting_for_hours)
async def process_custom_hours(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.reply("Введите число.")
        return
    
    hours = int(message.text)
    if not (1 <= hours <= 168):
        await message.reply("Введите число от 1 до 168.")
        return
    
    data = await state.get_data()
    chat_id = data.get("chat_id")
    
    if not chat_id:
        await message.reply("Ошибка")
        await state.clear()
        return
    
    status_msg = await message.reply(f"Получаю сообщения за {hours} часов...")
    messages = await get_messages(chat_id, hours)
    
    if not messages:
        await status_msg.delete()
        await message.reply(f"Сообщений за последние {hours} часов не найдено.")
        await state.clear()
        return
    
    await status_msg.edit_text(f"Суммаризирую {len(messages)} сообщений...")
    summary = await summarize_with_yandex(messages)
    
    await status_msg.delete()
    await message.reply(f"📝 Суммаризация за {hours} ч:\n\n{summary}")
    await state.clear()

# ---------- ОБРАБОТКА СООБЩЕНИЙ ----------
@dp.message(F.text)
async def handle_text(message: types.Message):
    if message.text and message.text.startswith('/'):
        return
    
    user_name = message.from_user.username or message.from_user.full_name
    user_id = message.from_user.id
    
    await save_message(message.chat.id, user_name, user_id, message.text, "text")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_name = message.from_user.username or message.from_user.full_name
    user_id = message.from_user.id
    
    try:
        file_id = message.voice.file_id
        file = await bot.get_file(file_id)
        file_path = f"voice_{message.message_id}.mp3"
        await bot.download_file(file.file_path, file_path)
        
        text = await voice_to_text(file_path)
        await save_message(message.chat.id, user_name, user_id, text, "voice")
        
        if os.path.exists(file_path):
            os.remove(file_path)
            
    except Exception as e:
        logging.error(f"Error processing voice: {e}")
        await save_message(message.chat.id, user_name, user_id, f"[Ошибка: {e}]", "voice_error")

@dp.message(F.video_note)
async def handle_video_note(message: types.Message):
    user_name = message.from_user.username or message.from_user.full_name
    user_id = message.from_user.id
    
    try:
        file_id = message.video_note.file_id
        file = await bot.get_file(file_id)
        video_path = f"video_note_{message.message_id}.mp4"
        await bot.download_file(file.file_path, video_path)
        
        audio_path = f"audio_{message.message_id}.mp3"
        success = await extract_audio_from_video(video_path, audio_path)
        
        if success:
            text = await voice_to_text(audio_path)
            await save_message(message.chat.id, user_name, user_id, text, "video_note")
        else:
            await save_message(message.chat.id, user_name, user_id, "[Не удалось извлечь аудио]", "video_note_error")
        
        for path in [video_path, audio_path]:
            if os.path.exists(path):
                os.remove(path)
                
    except Exception as e:
        logging.error(f"Error processing video note: {e}")
        await save_message(message.chat.id, user_name, user_id, f"[Ошибка: {e}]", "video_note_error")

@dp.message(F.video)
async def handle_video(message: types.Message):
    user_name = message.from_user.username or message.from_user.full_name
    user_id = message.from_user.id
    
    try:
        file_id = message.video.file_id
        file = await bot.get_file(file_id)
        video_path = f"video_{message.message_id}.mp4"
        await bot.download_file(file.file_path, video_path)
        
        audio_path = f"audio_{message.message_id}.mp3"
        success = await extract_audio_from_video(video_path, audio_path)
        
        if success:
            text = await voice_to_text(audio_path)
            await save_message(message.chat.id, user_name, user_id, text, "video")
        else:
            await save_message(message.chat.id, user_name, user_id, "[Не удалось извлечь аудио]", "video_error")
        
        for path in [video_path, audio_path]:
            if os.path.exists(path):
                os.remove(path)
                
    except Exception as e:
        logging.error(f"Error processing video: {e}")
        await save_message(message.chat.id, user_name, user_id, f"[Ошибка: {e}]", "video_error")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_name = message.from_user.username or message.from_user.full_name
    user_id = message.from_user.id
    caption = message.caption or "[Фото без подписи]"
    await save_message(message.chat.id, user_name, user_id, f"[Фото] {caption}", "photo")

@dp.message()
async def handle_other(message: types.Message):
    user_name = message.from_user.username or message.from_user.full_name
    user_id = message.from_user.id
    await save_message(message.chat.id, user_name, user_id, f"[{message.content_type}]", "other")

# ---------- ЗАПУСК ----------
async def main():
    logging.info("Starting bot...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await set_commands()
        
        me = await bot.get_me()
        logging.info(f"Bot started: @{me.username}")
        
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Error: {e}")
        raise
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())