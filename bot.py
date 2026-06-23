import os
import logging
import json
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from groq import Groq

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]

groq_client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """You are a friendly English teacher for a Russian-speaking student at A1 level.

RULES - follow them strictly every message:

1. If the student writes in RUSSIAN:
   - Reply in simple English (A1 level, short sentences).
   - Always add a Russian translation below like:
     Перевод: ...

2. If the student writes in ENGLISH:
   - First, correct ALL grammar/spelling mistakes.
   - Show the corrected sentence: Correct: "..."
   - Explain each mistake briefly in Russian.
   - Then continue in simple English + Russian translation.
   - If there are NO mistakes, praise them warmly!

3. Always be encouraging, warm, patient.
4. Use very simple vocabulary (A1). Short sentences. No complex grammar.

Remember: your goal is to help the student improve and feel confident!"""

STATE_FILE = "state.json"

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()

def get_user(user_id: str) -> dict:
    if user_id not in state:
        state[user_id] = {
            "last_topic_date": None,
            "topic_number": 0,
            "history": []
        }
    return state[user_id]

TOPICS = [
    "Introduce yourself: your name, age, where you are from.",
    "Describe your family: how many people, names, who they are.",
    "Talk about your daily routine: morning, afternoon, evening.",
    "Describe your home: rooms, favourite place.",
    "Talk about food: what you like, what you eat for breakfast.",
    "Describe your city or town: big or small, what is there.",
    "Talk about your hobbies: what you do in free time.",
    "Describe the weather today and your favourite season.",
    "Talk about your job or studies.",
    "Describe your best friend: appearance and personality.",
]

def should_send_topic(user: dict) -> bool:
    if user["last_topic_date"] is None:
        return True
    last = datetime.fromisoformat(user["last_topic_date"])
    return datetime.now() - last >= timedelta(days=3)

def get_next_topic(user: dict) -> str:
    idx = user["topic_number"] % len(TOPICS)
    return TOPICS[idx]

def ask_groq(user_id: str, user_message: str) -> str:
    user = get_user(user_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in user["history"][-20:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=1000,
    )
    reply = response.choices[0].message.content

    user["history"].append({"role": "user", "content": user_message})
    user["history"].append({"role": "assistant", "content": reply})
    if len(user["history"]) > 40:
        user["history"] = user["history"][-40:]

    save_state(state)
    return reply

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    get_user(user_id)
    save_state(state)
    await update.message.reply_text(
        "Hello! I am your English teacher!\n"
        "Привет! Я твой учитель английского!\n\n"
        "Write in Russian - I answer in English + translation\n"
        "Write in English - I correct your mistakes\n"
        "Every 3 days - new speaking topic\n\n"
        "Let's start! What is your name?\n"
        "Начнём! Как тебя зовут?"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    text = update.message.text

    topic_message = ""
    if should_send_topic(user):
        topic = get_next_topic(user)
        user["last_topic_date"] = datetime.now().isoformat()
        user["topic_number"] += 1
        save_state(state)
        topic_message = (
            "\n\nSpeaking Practice Topic:\n"
            + topic
            + "\n\nТема для практики - напиши об этом! Не бойся ошибок!"
        )

    try:
        reply = ask_groq(user_id, text)
        await update.message.reply_text(reply + topic_message)
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await update.message.reply_text(
            "Sorry, something went wrong. Please try again!\n"
            "Извини, что-то пошло не так. Попробуй ещё раз!"
        )

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Voice messages coming soon!\n"
        "Голосовые сообщения скоро! Пока пиши текстом."
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    state[user_id] = {"last_topic_date": None, "topic_number": 0, "history": []}
    save_state(state)
    await update.message.reply_text(
        "History cleared! Let's start fresh.\n"
        "История очищена! Начинаем сначала."
    )

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    logger.info(f"HTTP server listening on port {port}")
    server.serve_forever()

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    await asyncio.sleep(1)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Bot is running...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
