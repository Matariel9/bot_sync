import telebot
from telebot import types as telebot_types, apihelper
from google import genai
from google.genai import types as genai_types
import logging
import io
import docx
import requests
import os
import re
from datetime import datetime, timezone, timedelta
from youtube_transcript_api import YouTubeTranscriptApi

# Импортируем ключи и прокси из секретного файла
from sacred_data import TG_TOKEN, GEMINI_API_KEY, TAVILY_API_KEY, PROXY_URL 

logging.basicConfig(level=logging.INFO)

# --- НАСТРОЙКИ ---
ALLOWED_USERS = [1350259604, 1769256488, 1468528137, 5043149856]

# --- ПОДКЛЮЧЕНИЕ ПРОКСИ ДЛЯ БОТА ---
if PROXY_URL:
    apihelper.proxy = {'https': PROXY_URL}

# --- ЧТЕНИЕ ПРОМПТА ИЗ ФАЙЛА ---
prompt_path = '/root/my_family_bot/prompt.txt'
try:
    with open(prompt_path, 'r', encoding='utf-8') as file:
        family_rules = file.read()
except FileNotFoundError:
    logging.warning("Файл prompt.txt не найден! Использую базовые правила.")
    family_rules = "Ты умный семейный помощник. Отвечай кратко и по делу."

# --- ИНИЦИАЛИЗАЦИЯ НОВОГО КЛИЕНТА GOOGLE ---
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = 'gemini-3.1-flash-lite-preview'

bot = telebot.TeleBot(TG_TOKEN)
user_chats = {}

def search_internet(query):
    """Поиск через Tavily API"""
    url = "https://api.tavily.com/search"
    data = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "include_answer": True, 
        "max_results": 3
    }
    try:
        response = requests.post(url, json=data, timeout=10)
        if response.status_code != 200:
            return ""

        res_json = response.json()
        answer = res_json.get('answer')
        if answer:
            return f"[Свежие данные из интернета: {answer}]\n\n"

        results = [f"- {r.get('content', '')}" for r in res_json.get('results', [])]
        if not results:
            return ""
        return "[Свежие данные из интернета]:\n" + "\n".join(results) + "\n\n"
    except Exception as e:
        logging.error(f"Сбой подключения к Tavily: {e}")
        return ""

def get_youtube_transcript(url):
    """Извлекает текст из видео на YouTube"""
    try:
        # Ищем ID видео в ссылке (работает с youtube.com и youtu.be)
        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
        if not match:
            return None
        video_id = match.group(1)

        # Пытаемся получить русские или английские субтитры
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ru', 'en'])
        
        # Склеиваем всё в один огромный текст
        text = " ".join([t['text'] for t in transcript_list])
        return text
    except Exception as e:
        logging.error(f"Ошибка YouTube: {e}")
        return None

def format_for_telegram(text):
    """Шлюз-переводчик: чистит Markdown от ИИ и делает безопасный HTML"""
    text = re.sub(r'```(\w*)\n(.*?)\n```', r'<pre><code class="\1">\2</code></pre>', text, flags=re.DOTALL)
    text = re.sub(r'```(.*?)```', r'<pre><code>\1</code></pre>', text, flags=re.DOTALL)
    text = re.sub(r'(?<!`)`(?!`)(.*?)(?<!`)`(?!`)', r'<code>\1</code>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'^###\s+(.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+(.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s+(.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    return text

def get_or_create_chat(user_id):
    """Создает новую сессию чата по новым правилам библиотеки"""
    if user_id not in user_chats:
        config = genai_types.GenerateContentConfig(
            system_instruction=family_rules,
            temperature=0.7
        )
        user_chats[user_id] = client.chats.create(
            model=MODEL_ID,
            config=config
        )
    return user_chats[user_id]

@bot.message_handler(commands=['reset', 'clear'])
def reset_chat(message):
    user_id = message.from_user.id
    if user_id not in ALLOWED_USERS: return
    
    config = genai_types.GenerateContentConfig(system_instruction=family_rules)
    user_chats[user_id] = client.chats.create(model=MODEL_ID, config=config)
    bot.send_message(message.chat.id, "Память очищена!")

@bot.message_handler(content_types=['text', 'photo', 'document', 'voice'])
def handle_message(message):
    user_id = message.from_user.id
    if user_id not in ALLOWED_USERS: return

    chat = get_or_create_chat(user_id)

    try:
        msk_tz = timezone(timedelta(hours=3))
        now = datetime.now(msk_tz).strftime("%d.%m.%Y, %H:%M")
        time_prefix = f"[Системное время (МСК): {now}]\n"

        action = 'record_voice' if message.content_type == 'voice' else 'typing'
        bot.send_chat_action(message.chat.id, action)

        content_to_send = []

        if message.content_type == 'text':
            user_text = message.text
            context = ""
            
            # --- ЛОГИКА ДЛЯ YOUTUBE ---
            if "youtube.com" in user_text.lower() or "youtu.be" in user_text.lower():
                bot.send_message(message.chat.id, "🎬 Вижу ссылку на YouTube! Изучаю видео, дай мне пару секунд...")
                transcript = get_youtube_transcript(user_text)
                
                if transcript:
                    context = f"[СУБТИТРЫ ВИДЕО]: {transcript}\n\n"
                    # Меняем текст запроса, чтобы направить ИИ на пересказ, если пользователь просто кинул ссылку
                    user_text = f"Опираясь на предоставленные субтитры, выполни просьбу: {user_text}. Если конкретной просьбы нет, просто сделай подробный пересказ этого видео, выдели главные мысли в виде красивого списка. Игнорируй рекламные интеграции."
                else:
                    context = "[ОШИБКА]: Не удалось вытащить субтитры. Возможно, автор отключил их для этого видео.\n\n"
            
            # --- ЛОГИКА ДЛЯ ПОИСКА (Если это не YouTube) ---
            else:
                triggers = ['?', 'курс', 'погода', 'сколько', 'акции', 'новости', 'что сейчас', 'какой', 'какая']
                if any(t in user_text.lower() for t in triggers):
                    context = search_internet(user_text)

            content_to_send.append(f"{time_prefix}{context}Запрос пользователя: {user_text}")

        elif message.content_type == 'photo':
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content_to_send.append(genai_types.Part.from_bytes(data=downloaded_file, mime_type='image/jpeg'))
            content_to_send.append(time_prefix + (message.caption if message.caption else "Что на фото?"))

        elif message.content_type == 'voice':
            file_info = bot.get_file(message.voice.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content_to_send.append(genai_types.Part.from_bytes(data=downloaded_file, mime_type='audio/ogg'))
            content_to_send.append(time_prefix + "Прослушай и ответь.")

        elif message.content_type == 'document':
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            if message.document.mime_type == 'application/pdf':
                content_to_send.append(genai_types.Part.from_bytes(data=downloaded_file, mime_type='application/pdf'))
                content_to_send.append(time_prefix + (message.caption if message.caption else "Анализируй PDF."))
            elif 'officedocument' in message.document.mime_type:
                doc = docx.Document(io.BytesIO(downloaded_file))
                full_text = "\n".join([para.text for para in doc.paragraphs])
                content_to_send.append(time_prefix + f"ТЕКСТ ДОКУМЕНТА:\n{full_text}")
            else:
                bot.send_message(message.chat.id, "Поддерживаю только PDF и DOCX.")
                return

        response = chat.send_message(content_to_send)
        
        if response.text:
            clean_text = format_for_telegram(response.text)
            
            if len(clean_text) > 4000:
                file_stream = io.BytesIO(response.text.encode('utf-8'))
                file_stream.name = f"Длинный_ответ_ИИ.txt"
                bot.send_document(
                    message.chat.id, 
                    file_stream, 
                    caption="Ответ получился слишком объемным, поэтому я сохранил его в файл 👆",
                    reply_markup=telebot_types.ReplyKeyboardRemove()
                )
            else:
                try:
                    bot.send_message(
                        message.chat.id, 
                        clean_text, 
                        parse_mode='HTML', 
                        reply_markup=telebot_types.ReplyKeyboardRemove()
                    )
                except Exception as format_error:
                    logging.warning(f"Ошибка парсера HTML, отправляю сырой текст: {format_error}")
                    bot.send_message(
                        message.chat.id, 
                        response.text, 
                        reply_markup=telebot_types.ReplyKeyboardRemove()
                    )

    except Exception as e:
        if "429" in str(e):
            bot.send_message(message.chat.id, "Я перегрелся. Подожди немного!")
        else:
            logging.error(f"Ошибка в handle_message: {e}")
            bot.send_message(message.chat.id, "Произошла ошибка, но я скоро поправлюсь!")

print("Бот запущен (YouTube + SDK + HTML + Файлы)...")
bot.infinity_polling()