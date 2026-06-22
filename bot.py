
import os
import logging
import json
import threading
import requests
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
import anthropic
from pydub import AudioSegment
import tempfile

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Clients ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Persistent state (in-memory, survives restarts via JSON file) ─────────────
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
            "last_topic_date": None,   # ISO date string
            "topic_number": 0,
            "history": []
        }
    return state[user_id]

# ── Speaking topics ───────────────────────────────────────────────────────────
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

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a friendly English teacher for a Russian-speaking student at A1 level.

RULES — follow them strictly every message:

1. If the student writes in RUSSIAN:
   - Reply in simple English (A1 level, short sentences).
   - Always add a Russian translation in italics below, like:
     *Перевод: ...*

2. If the student writes in ENGLISH:
   - First, correct ALL grammar/spelling mistakes.
   - Show the corrected sentence clearly: ✅ Correct: "..."
   - Explain each mistake briefly in Russian.
   - Then continue the conversation naturally in simple English + Russian translation.
   - If there are NO mistakes, praise them warmly! 🎉

3. Always be encouraging, warm, patient.
4. Use very simple vocabulary (A1). Short sentences. No complex grammar.
5. If the student sends a voice message transcription, treat it as English writing and correct it.

Remember: your goal is to help the student improve and feel confident!"""

# ── Helper: should we send a topic today? ────────────────────────────────────
def should_send_topic(user: dict) -> bool:
    if user["last_topic_date"] is None:
        return True
    last = datetime.fromisoformat(user["last_topic_date"])
    return datetime.now() - last >= timedelta(days=3)

def get_next_topic(user: dict) -> str:
    idx = user["topic_number"] % len(TOPICS)
    return TOPICS[idx]

# ── Claude call ───────────────────────────────────────────────────────────────
def ask_claude(user_id: str, user_message: str) -> str:
    user = get_user(user_id)

    # Build messages list from history + new message
    messages = user["history"][-20:]  # keep last 20 exchanges to save tokens
    messages = messages + [{"role": "user", "content": user_message}]

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    reply = response.content[0].text

    # Save to history
    user["history"].append({"role": "user",    "content": user_message})
    user["history"].append({"role": "assistant","content": reply})
    # Keep history manageable
    if len(user["history"]) > 40:
        user["history"] = user["history"][-40:]

    save_state(state)
    return reply

# ── Voice transcription ───────────────────────────────────────────────────────
def transcribe_voice(ogg_path: str) -> str:
    """Convert OGG → WAV → send to Google Speech API."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    audio = AudioSegment.from_ogg(ogg_path)
    audio = audio.set_channels(1).set_frame_rate(16000)
    audio.export(wav_path, format="wav")

    with open(wav_path, "rb") as f:
        wav_data = f.read()
    os.unlink(wav_path)

    url = "http://www.google.com/speech-api/v2/recognize"
    params = {
        "client": "chromium",
        "lang":   "en-US",
        "key":    "AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw",
    }
    headers = {"Content-Type": "audio/l16; rate=16000"}

    try:
        resp = requests.post(url, params=params, headers=headers, data=wav_data, timeout=10)
        # Response has two JSON lines; first is empty, second has result
        for line in resp.text.strip().splitlines():
            if '"transcript"' in line:
                import json as _json
                data = _json.loads(line)
                return data["result"][0]["alternative"][0]["transcript"]
    except Exception as e:
        logger.error(f"Transcription error: {e}")
    return ""

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    save_state(state)

    welcome = (
        "👋 Hello! I am your English teacher!\n"
        "*Привет! Я твой учитель английского!*\n\n"
        "📌 How it works:\n"
        "• Write to me in Russian → I answer in English + translation\n"
        "• Write to me in English → I correct your mistakes\n"
        "• Send voice messages → I transcribe and correct them\n"
        "• Every 3 days I give you a new speaking topic 🎤\n\n"
        "*Как это работает:*\n"
        "*• Пишешь по-русски → отвечаю на английском + перевод*\n"
        "*• Пишешь по-английски → исправляю ошибки*\n"
        "*• Голосовые сообщения → расшифровываю и исправляю*\n"
        "*• Каждые 3 дня — новая тема для практики 🎤*\n\n"
        "Let's start! What is your name? 😊\n"
        "*Начнём! Как тебя зовут?*"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user    = get_user(user_id)
    text    = update.message.text

    # Check if it's time for a speaking topic
    topic_message = ""
    if should_send_topic(user):
        topic = get_next_topic(user)
        user["last_topic_date"] = datetime.now().isoformat()
        user["topic_number"]   += 1
        save_state(state)
        topic_message = (
            f"\n\n🎤 *Speaking Practice Topic:*\n"
            f"_{topic}_\n"
            f"*Тема для разговорной практики:* запиши голосовое сообщение на эту тему!\n"
            f"Не бойся ошибок — это нормально! 💪"
        )

    # Get Claude's reply
    reply = ask_claude(user_id, text)
    full_reply = reply + topic_message

    await update.message.reply_text(full_reply, parse_mode="Markdown")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    await update.message.reply_text(
        "🎧 I hear you! Let me transcribe...\n*Слышу тебя! Расшифровываю...*",
        parse_mode="Markdown"
    )

    # Download voice file
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        ogg_path = tmp.name
    await voice_file.download_to_drive(ogg_path)

    # Transcribe
    transcription = transcribe_voice(ogg_path)
    os.unlink(ogg_path)

    if not transcription:
        await update.message.reply_text(
            "😕 I could not understand the audio. Please try again, speak clearly.\n"
            "*Не смогла разобрать аудио. Попробуй ещё раз, говори чётче.*",
            parse_mode="Markdown"
        )
        return

    # Show what was heard
    heard_msg = f"🎙 I heard: _{transcription}_\n*Я услышала:* _{transcription}_\n\n"

    # Ask Claude to correct it
    reply = ask_claude(user_id, transcription)
    await update.message.reply_text(heard_msg + reply, parse_mode="Markdown")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in state:
        state[user_id] = {
            "last_topic_date": None,
            "topic_number": 0,
            "history": []
        }
        save_state(state)
    await update.message.reply_text(
        "🔄 History cleared! Let's start fresh.\n*История очищена! Начинаем сначала.*",
        parse_mode="Markdown"
    )


# ── Fake HTTP server (Render free Web Service requires a bound port) ─────────
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        pass  # silence default HTTP logs


def run_fake_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Start fake web server in a background thread so Render sees an open port
    threading.Thread(target=run_fake_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
