# bot.py — Trader Pro JLV (Railway-ready)

import os
import time
import math
import logging
from typing import Dict, List

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# =========================
# Config via variables d'env
# =========================
TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME  = os.getenv("MODEL_NAME", "gpt-4o-mini")

assert TOKEN, "TELEGRAM_BOT_TOKEN manquant"
assert OPENAI_KEY, "OPENAI_API_KEY manquant"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("trader-pro-jlv")

# =========================
# Mémoire & anti-spam
# =========================
SYSTEM = (
    "Tu es 'Trader Pro JLV', bot Telegram francophone 100% crypto. "
    "Tu parles comme un analyste de desk: clair, concret, opérationnel. "
    "Tu donnes d'abord la réponse utile (1–2 phrases), puis des détails, "
    "et des conseils de gestion du risque. Pas de jargon inutile."
)

CTX: Dict[int, List[Dict[str, str]]] = {}   # historique par utilisateur
MAX_TURNS = 10                               # tours mémorisés
LAST_SEEN: Dict[int, float] = {}             # anti-spam global (compatible Railway)

def _hist(uid: int) -> List[Dict[str, str]]:
    if uid not in CTX:
        CTX[uid] = [{"role": "system", "content": SYSTEM}]
    return CTX[uid]

def _push(uid: int, role: str, content: str):
    h = _hist(uid)
    h.append({"role": role, "content": content})
    if len(h) > (1 + 2 * MAX_TURNS):
        CTX[uid] = [h[0]] + h[-(2 * MAX_TURNS):]

# =========================
# Clients HTTP
# =========================
# OpenAI / compat
client = httpx.AsyncClient(
    base_url=OPENAI_BASE,
    headers={"Authorization": f"Bearer {OPENAI_KEY}"},
    timeout=httpx.Timeout(60, connect=15),
)

# CoinGecko public
cg = httpx.AsyncClient(
    base_url="https://api.coingecko.com/api/v3",
    timeout=httpx.Timeout(30, connect=10),
)

# Fear & Greed (macro)
fng = httpx.AsyncClient(
    base_url="https://api.alternative.me/fng",
    timeout=httpx.Timeout(30, connect=10),
)

# News (CryptoCompare) — pas de clé requise
ccnews = httpx.AsyncClient(
    base_url="https://min-api.cryptocompare.com",
    timeout=httpx.Timeout(30, connect=10),
)


# =========================
# Utilitaires format
# =========================
def pct(x):
    try:
        return f"{float(x):+,.2f}%"
    except Exception:
        return "-"

def usd(x):
    try:
        x = float(x)
        if x >= 1_000_000_000:  return f"${x/1_000_000_000:.2f}B"
        if x >= 1_000_000:      return f"${x/1_000_000:.2f}M"
        if x >= 1_000:          return f"${x/1_000:.2f}K"
        return f"${x:.2f}"
    except Exception:
        return "-"

def human_num(x):
    try:
        x = float(x)
        if x >= 1_000_000_000:  return f"{x/1_000_000_000:.2f}B"
        if x >= 1_000_000:      return f"{x/1_000_000:.2f}M"
        if x >= 1_000:          return f"{x/1_000:.2f}K"
        if x.is_integer():      return f"{int(x)}"
        return f"{x:.2f}"
    except Exception:
        return "-"

# mini sparkline ASCII
def sparkline(series):
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1e-9
    return "".join(blocks[min(7, max(0, int((v - lo) / rng * 7)))] for v in series)

# =========================
# OpenAI chat helper
# =========================
@retry(wait=wait_exponential(min=1, max=8), stop=stop_after_attempt(3))
async def chat(messages: List[Dict[str, str]]) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.35,
        "max_tokens": 750,
    }
    try:
        r = await client.post("/chat/completions", json=payload)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:400]
        log.error("OpenAI HTTP %s: %s", e.response.status_code, body)
        return "Erreur côté IA (HTTP)."
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        log.error("Réponse IA inattendue: %s", data)
        return "Réponse IA inattendue."

# =========================
# Handlers de base
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _hist(update.effective_user.id)
    await update.message.reply_text(
        "Salut 👋 Je suis **Trader Pro JLV** (crypto only).\n"
        "Commandes: /help · /reset · /ping · /diag · /btc · /actu · /macro · /chart btc|eth · /perspective btc|eth"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Aide\n"
        "- Envoie un message (réponse IA).\n"
        "- /btc : analyse BTC temps réel + lecture IA.\n"
        "- /actu : synthèse actus BTC & ETH.\n"
        "- /macro : briefing macro.\n"
        "- /chart btc|eth : sparkline ASCII 7j.\n"
        "- /perspective btc|eth : court/moyen terme (IA).\n"
        "- /reset : effacer la mémoire.\n"
        "- /diag : tester l'API IA."
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CTX[update.effective_user.id] = [{"role": "system", "content": SYSTEM}]
    await update.message.reply_text("Mémoire effacée.")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = await client.get("/models")
        msg = f"OpenAI status: {r.status_code}"
        if r.status_code != 200:
            msg += f" · {r.text[:120]}"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Diag error: {e}")

# =========================
# Handlers crypto
# =========================
async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyse détaillée BTC + lecture IA"""
    try:
        r = await cg.get("/coins/bitcoin", params=dict(
            localization="false", tickers="false",
            market_data="true", community_data="false",
            developer_data="false", sparkline="true"
        ))
        r.raise_for_status()
        d = r.json()
        m = d["market_data"]

        price   = m["current_price"]["usd"]
        ch1h    = m["price_change_percentage_1h_in_currency"]["usd"]
        ch24h   = m["price_change_percentage_24h_in_currency"]["usd"]
        ch7d    = m["price_change_percentage_7d_in_currency"]["usd"]
        mc      = m["market_cap"]["usd"]
        vol24   = m["total_volume"]["usd"]
        circ    = m.get("circulating_supply") or 0
        ath     = m["ath"]["usd"]
        ath_ch  = m["ath_change_percentage"]["usd"]

        prices7 = [p for p in m["sparkline_7d"]["price"]][-100:]  # lisible
        sp = sparkline(prices7)

        txt = (
            f"📈 **Bitcoin — Vue marché**\n"
            f"• Prix: {usd(price)}  ({pct(ch1h)} 1h, {pct(ch24h)} 24h, {pct(ch7d)} 7j)\n"
            f"• Market Cap: {usd(mc)}   • Vol 24h: {usd(vol24)}\n"
            f"• Offre en circulation: {human_num(circ)} BTC\n"
            f"• ATH: {usd(ath)} (écart {pct(-ath_ch)})\n"
            f"• 7d: {sp}\n"
        )

        prompt = (
            "Tu es analyste crypto senior. À partir des métriques ci-dessus, donne :\n"
            "- Tendance actuelle et niveaux clés (zones S/R),\n"
            "- Scénarios court terme (jours/semaines) et moyen terme (mois),\n"
            "- Signaux d'invalidation à surveiller,\n"
            "- Gestion du risque (position sizing, DCA, stop)."
        )
        uid = update.effective_user.id
        _push(uid, "user", txt + "\n\n" + prompt)
        ia = await chat(_hist(uid))
        await update.message.reply_text(txt + "\n" + ia)
    except Exception as e:
        await update.message.reply_text(f"⚠️ BTC: échec récupération ({e}).")

async def cmd_actu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Actu BTC & ETH via CryptoCompare News (source publique).
    On récupère un flux EN, on filtre BTC/ETH et on synthétise en français via l’IA.
    """
    try:
        # Récup top news (EN). On filtre ensuite BTC/ETH dans categories/coins.
        r = await ccnews.get("/data/v2/news/", params={"lang": "EN"})
        r.raise_for_status()
        articles = r.json().get("Data", [])[:50]  # on limite

        btc_items, eth_items = [], []
        for a in articles:
            cats = (a.get("categories") or "") + " " + " ".join(a.get("tags") or [])
            title = (a.get("title") or "").strip()
            body  = (a.get("body") or "").strip()
            url   = (a.get("url") or "").strip()
            line  = f"- {title} — {url}\n  {body[:220]}..."

            # tri très simple : présence de mots-clés
            cats_low = (cats + " " + title + " " + body).lower()
            if any(k in cats_low for k in ["btc", "bitcoin"]):
                btc_items.append(line)
            if any(k in cats_low for k in ["eth", "ethereum"]):
                eth_items.append(line)

        # On prend les 5 meilleurs de chaque
        btc_text = "\n".join(btc_items[:5]) if btc_items else "- (rien de marquant)"
        eth_text = "\n".join(eth_items[:5]) if eth_items else "- (rien de marquant)"

        raw = f"BTC news:\n{btc_text}\n\nETH news:\n{eth_text}"

        prompt = (
            "Synthétise ces actualités en français, en séparant BTC et ETH. "
            "Pour chaque actif, donne 4–6 puces actionnables : impact potentiel "
            "sur le prix/réseau/régulation, niveau de confiance, ce qui est du bruit. "
            "Reste concis et utile pour un trader."
        )

        uid = update.effective_user.id
        _push(uid, "user", raw + "\n\n" + prompt)
        ia = await chat(_hist(uid))

        await update.message.reply_text("🗞️ **Actu BTC & ETH**\n" + ia)

    except httpx.HTTPStatusError as e:
        await update.message.reply_text(
            f"⚠️ Actu: échec récupération (HTTP {e.response.status_code})."
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Actu: échec récupération ({e}).")



async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Brief macro (Fear & Greed + IA)"""
    try:
        r = await fng.get("/", params={"limit": 1, "format": "json"})
        r.raise_for_status()
        data = r.json()["data"][0]
        idx = int(data["value"])
        cls = data["value_classification"]
        fng_txt = f"Fear & Greed Index: {idx} ({cls})"

        prompt = (
            "Fais un briefing macro pour un trader BTC (taux, dollar, liquidité, cycles), "
            "en 6–8 puces utiles, avec pistes d’action défensives/offensives. "
            f"Inclus: {fng_txt}. Reste concret."
        )
        uid = update.effective_user.id
        _push(uid, "user", prompt)
        ia = await chat(_hist(uid))
        await update.message.reply_text("🌍 **Macro**\n" + fng_txt + "\n\n" + ia)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Macro: échec récupération ({e}).")

def _coin_from_args(args, default="btc"):
    if not args:
        return default
    a = args[0].lower()
    if a in ("btc", "bitcoin"):   return "bitcoin"
    if a in ("eth", "ethereum"):  return "ethereum"
    return default

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sparkline 7j: /chart btc|eth"""
    try:
        coin = _coin_from_args(context.args, default="btc")
        cid  = "bitcoin" if coin == "btc" else ("ethereum" if coin == "eth" else coin)
        r = await cg.get(f"/coins/{cid}/market_chart", params={"vs_currency": "usd", "days": 7})
        r.raise_for_status()
        prices = [p[1] for p in r.json()["prices"]]
        sp = sparkline(prices[-120:])
        label = "BTC" if cid == "bitcoin" else "ETH"
        await update.message.reply_text(f"📊 {label} 7j\n{sp}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Chart: échec ({e}).")

async def _cmd_perspective(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str, label: str):
    try:
        r = await cg.get(f"/coins/{cid}", params=dict(
            localization="false", tickers="false",
            market_data="true", community_data="false",
            developer_data="false", sparkline="false"
        ))
        r.raise_for_status()
        m = r.json()["market_data"]
        price = m["current_price"]["usd"]
        ch1h  = m["price_change_percentage_1h_in_currency"]["usd"]
        ch24h = m["price_change_percentage_24h_in_currency"]["usd"]
        ch7d  = m["price_change_percentage_7d_in_currency"]["usd"]
        mc    = m["market_cap"]["usd"]
        vol24 = m["total_volume"]["usd"]
        ath   = m["ath"]["usd"]
        ath_ch= m["ath_change_percentage"]["usd"]

        facts = (
            f"{label}  Prix={usd(price)},  MC={usd(mc)},  Vol24h={usd(vol24)}; "
            f"Δ1h={pct(ch1h)}, Δ24h={pct(ch24h)}, Δ7j={pct(ch7d)}; "
            f"ATH={usd(ath)} (écart {pct(-ath_ch)})."
        )
        prompt = (
            "À partir de ces métriques, écris deux sections distinctes :\n"
            "- COURT TERME (jours/semaines): niveaux clés, zones d’invalidation, signaux,\n"
            "- MOYEN TERME (mois): tendance de fond, catalyseurs, risques.\n"
            "Ajoute 6–8 puces actionnables (gestion du risque, DCA, stops).\n\n"
            + facts
        )
        uid = update.effective_user.id
        _push(uid, "user", prompt)
        ia = await chat(_hist(uid))
        await update.message.reply_text(f"🧭 **Perspective {label}**\n" + ia)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Perspective {label}: échec ({e}).")

async def cmd_perspective(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/perspective btc|eth"""
    coin = _coin_from_args(context.args, default="btc")
    if coin in ("bitcoin", "btc"):
        return await _cmd_perspective(update, context, "bitcoin", "BTC")
    else:
        return await _cmd_perspective(update, context, "ethereum", "ETH")

# =========================
# Handler messages libres (écho + IA)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    uid = update.effective_user.id
    text = update.message.text.strip()

    # Écho immédiat (tu voulais le garder)
    await update.message.reply_text(f"✅ Reçu : {text}")

    # Anti-spam global
    now = time.time()
    if uid in LAST_SEEN and (now - LAST_SEEN[uid]) < 0.6:
        return
    LAST_SEEN[uid] = now

    _push(uid, "user", text)

    try:
        await update.message.chat.send_action(action="typing")
        reply = await chat(_hist(uid))
        _push(uid, "assistant", reply)
        await update.message.reply_text(reply)  # texte brut : jamais rejeté par Telegram
    except Exception:
        await update.message.reply_text("⚠️ Petit problème côté IA. Réessaie.")

# =========================
# Bootstrap (Railway)
# =========================
def main():
    req = HTTPXRequest(
        http_version="1.1",
        connect_timeout=15.0,
        read_timeout=60.0,
    )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(req)
        .concurrent_updates(True)
        .build()
    )

    # Commandes
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("ping",  ping))
    app.add_handler(CommandHandler("diag",  diag))

    # Crypto
    app.add_handler(CommandHandler("btc",       cmd_btc))
    app.add_handler(CommandHandler("actu",      cmd_actu))                 # BTC & ETH
    app.add_handler(CommandHandler("macro",     cmd_macro))
    app.add_handler(CommandHandler("chart",     cmd_chart))                # /chart btc|eth
    app.add_handler(CommandHandler("perspective", cmd_perspective))        # /perspective btc|eth

    # Messages libres
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logging.info("Trader Pro JLV en ligne (long-polling).")
    app.run_polling()

if __name__ == "__main__":
    main()
