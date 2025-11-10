import os, time, logging
from typing import Dict, List
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.request import HTTPXRequest

# ---------- Config via variables d'environnement ----------
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE  = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME   = os.getenv("MODEL_NAME", "gpt-4o-mini")

assert TOKEN, "TELEGRAM_BOT_TOKEN manquant"
assert OPENAI_KEY, "OPENAI_API_KEY manquant"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("jlv-bot")

SYSTEM = (
    "Tu es 'JLV Assistant', bot Telegram francophone pour un ingénieur. "
    "Réponds clairement en 1–2 phrases, puis détaille au besoin. "
    "Exemples concrets, pas de jargon inutile."
)

# mémoire courte par user
CTX: Dict[int, List[Dict[str, str]]] = {}
MAX_TURNS = 10

# Client OpenAI-compatible
client = httpx.AsyncClient(
    base_url=OPENAI_BASE,
    headers={"Authorization": f"Bearer {OPENAI_KEY}"},
    timeout=httpx.Timeout(60, connect=15)
)

def _hist(uid: int) -> List[Dict[str, str]]:
    if uid not in CTX:
        CTX[uid] = [{"role": "system", "content": SYSTEM}]
    return CTX[uid]

def _push(uid: int, role: str, content: str):
    h = _hist(uid); h.append({"role": role, "content": content})
    if len(h) > (1 + 2*MAX_TURNS):
        CTX[uid] = [h[0]] + h[-(2*MAX_TURNS):]

@retry(wait=wait_exponential(min=1, max=8), stop=stop_after_attempt(3))
async def chat(messages: List[Dict[str, str]]) -> str:
    payload = {"model": MODEL_NAME, "messages": messages, "temperature": 0.35, "max_tokens": 700}
    try:
        r = await client.post("/chat/completions", json=payload)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:400]
        log.error("OpenAI HTTP %s: %s", e.response.status_code, body)
        return "Erreur IA (HTTP)."
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        log.error("Réponse IA inattendue: %s", data)
        return "Réponse IA inattendue."

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _hist(update.effective_user.id)
    await update.message.reply_markdown("Salut 👋 Je suis **JLV Assistant**.\n_Commandes:_ /help · /reset · /ping · /diag")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown("**Aide**\n- Envoie un message.\n- /reset efface la mémoire.\n- /diag teste l’API IA.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CTX[update.effective_user.id] = [{"role": "system", "content": SYSTEM}]
    await update.message.reply_text("Mémoire effacée.")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = await client.get("/models")
        txt = f"OpenAI status: {r.status_code}"
        if r.status_code != 200:
            txt += f" · {r.text[:120]}"
        await update.message.reply_text(txt)
    except Exception as e:
        await update.message.reply_text(f"Diag error: {e}")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    uid = update.effective_user.id
    text = update.message.text.strip()

    # Écho immédiat (debug)
    await update.message.reply_text(f"✅ Reçu : {text}")

    # Anti-spam léger
    now = time.time()
    last = getattr(context.application, "last", {})
    if uid in last and (now - last[uid]) < 0.6:
        return
    last[uid] = now
    context.application.last = last

    # Mémorisation utilisateur
    _push(uid, "user", text)

    try:
        # L’IA réfléchit
        await update.message.chat.send_action(action="typing")

        # Appel IA
        reply = await chat(_hist(uid))

        # Mémorisation assistant
        _push(uid, "assistant", reply)

        # Réponse IA (texte brut)
        await update.message.reply_text(reply)

    except Exception:
        await update.message.reply_text("⚠️ Petit problème côté IA. Réessaie.")




def main():
    # Requête HTTP custom pour forcer HTTP/1.1 (robuste partout)
    req = HTTPXRequest(
        http_version="1.1",
        connect_timeout=15.0,
        read_timeout=60.0
)


    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(req)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("ping",  ping))
    app.add_handler(CommandHandler("diag",  diag))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_text))

    logging.info("Bot en ligne (long-polling).")
    app.run_polling()

if __name__ == "__main__":
    main()
