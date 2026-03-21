"""
Albo Pretorio Bot - Comune di Roccabascerana
Sistema Halley EG / EGHOMEPAGE.HBL

Comandi Telegram:
  /start        — benvenuto
  /abbonati     — iscriviti alle notifiche
  /disabbonati  — cancella iscrizione
  /atti         — mostra gli atti attuali con PDF allegato
  /controlla    — forza un controllo immediato
"""

import asyncio
import json
import logging
import os
import re
import sys
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
def load_config():
    cfg = {}
    if os.environ.get("BOT_TOKEN"):
        cfg["BOT_TOKEN"]        = os.environ["BOT_TOKEN"]
        cfg["ADMIN_IDS"]        = [int(x.strip()) for x in os.environ.get("CHAT_IDS", "").split(",") if x.strip()]
        cfg["ALBO_URL"]         = os.environ.get("ALBO_URL", "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL")
        cfg["INTERVAL_MINUTES"] = int(os.environ.get("INTERVAL_MINUTES", "180"))
    else:
        try:
            import config
            cfg["BOT_TOKEN"]        = config.BOT_TOKEN
            cfg["ADMIN_IDS"]        = config.CHAT_IDS
            cfg["ALBO_URL"]         = getattr(config, "ALBO_URL", "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL")
            cfg["INTERVAL_MINUTES"] = getattr(config, "INTERVAL_MINUTES", 180)
        except ImportError:
            print("ERRORE: config.py non trovato.")
            sys.exit(1)
    return cfg

CONFIG = load_config()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------
BASE_URL = CONFIG["ALBO_URL"]
ORIGIN   = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(BASE_URL))
ENTE     = "e1396"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
    "Content-Type":    "text/plain;charset=UTF-8; boundary=AZazAZ",
    "Referer":         BASE_URL,
    "Origin":          ORIGIN,
}

MENU_TEXT = (
    "\n\n─────────────────\n"
    "*Comandi disponibili:*\n"
    "/abbonati — iscriviti alle notifiche\n"
    "/disabbonati — cancella iscrizione\n"
    "/atti — mostra gli atti attuali\n"
    "/controlla — forza un controllo immediato\n"
    "/start — messaggio di benvenuto"
)

# ---------------------------------------------------------------------------
# Gestione iscritti
# ---------------------------------------------------------------------------
SUBSCRIBERS_PATH = Path("subscribers.json")

def load_subscribers() -> set:
    if SUBSCRIBERS_PATH.exists():
        return set(json.loads(SUBSCRIBERS_PATH.read_text(encoding="utf-8")))
    return set(CONFIG["ADMIN_IDS"])

def save_subscribers(subs: set):
    SUBSCRIBERS_PATH.write_text(
        json.dumps(sorted(subs), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def get_all_recipients() -> set:
    return load_subscribers() | set(CONFIG["ADMIN_IDS"])

# ---------------------------------------------------------------------------
# Database atti visti
# ---------------------------------------------------------------------------
DB_PATH = Path("seen_items.json")

def load_seen():
    if DB_PATH.exists():
        return set(json.loads(DB_PATH.read_text(encoding="utf-8")))
    return set()

def save_seen(seen):
    DB_PATH.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Heartbeat giornaliero (solo alle 9:00 UTC = 10:00 ora italiana)
# ---------------------------------------------------------------------------
async def send_heartbeat(bot, seen):
    now = datetime.utcnow()
    if now.hour != 7:
        return
    subs = load_subscribers()
    text = (
        "✅ *Albo Pretorio Bot – report giornaliero*\n\n"
        f"🗂 Atti in archivio: *{len(seen)}*\n"
        f"👥 Iscritti: *{len(subs)}*\n"
        f"🕐 {now.strftime('%d/%m/%Y %H:%M')} UTC\n\n"
        "_Tutto funziona correttamente._"
    )
    for chat_id in CONFIG["ADMIN_IDS"]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
            log.info(f"💓 Heartbeat inviato a {chat_id}")
        except Exception as e:
            log.error(f"Errore heartbeat: {e}")

# ---------------------------------------------------------------------------
# Sessione Halley EG
# ---------------------------------------------------------------------------
async def open_session(client: httpx.AsyncClient):
    try:
        r = await client.post(
            BASE_URL,
            content=f"ss=1&F=MC09&en={ENTE}&freeze=1".encode(),
            headers=HEADERS
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.error(f"Errore sessione: {e}")
        return None
    soup   = BeautifulSoup(r.text, "html.parser")
    jb_tag = soup.find("meta", {"name": "jb"})
    jb     = jb_tag["content"] if jb_tag and jb_tag.get("content") else ""
    if not jb:
        m  = re.search(r'name="jb"\s+content="([^"]+)"', r.text)
        jb = m.group(1) if m else ""
    if not jb:
        log.error("Token jb non trovato")
        return None
    return ORIGIN + "/" + jb.lstrip("/")

# ---------------------------------------------------------------------------
# Fetch lista atti (senza PDF)
# ---------------------------------------------------------------------------
async def fetch_albo_html():
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        session_url = await open_session(client)
        if not session_url:
            return None
        try:
            r = await client.post(
                session_url,
                content=f"&F=MC01&en={ENTE}".encode(),
                headers=HEADERS
            )
            r.raise_for_status()
            return r.text if len(r.text) > 100 else None
        except httpx.HTTPError as e:
            log.error(f"Errore MC01: {e}")
            return None

# ---------------------------------------------------------------------------
# Fetch allegati per una lista di atti (dentro sessione già aperta)
# ---------------------------------------------------------------------------
async def enrich_with_pdf(client, session_url, items):
    await client.post(
        session_url,
        content=f"&F=MC01&en={ENTE}".encode(),
        headers=HEADERS
    )
    for item in items:
        num_riga = item.get("num_riga")
        if not num_riga:
            continue
        try:
            r2    = await client.post(
                session_url,
                content=f"&F=MC02&NUMRIGA={num_riga}&en={ENTE}".encode(),
                headers=HEADERS
            )
            soup2    = BeautifulSoup(r2.text, "html.parser")
            allegati = []
            for func in ["MC96", "MC97", "MC98", "MC99"]:
                pattern = re.compile(rf"{func}\(")
                tags    = soup2.find_all("a", attrs={"onclick": pattern})
                for tag in tags:
                    m = re.search(rf"{func}\(['\"]?(\d+)['\"]?\)", tag.get("onclick", ""))
                    if not m:
                        continue
                    num_alleg = m.group(1)
                    filename  = tag.get_text(strip=True) or f"allegato_{num_alleg}.pdf"
                    r3        = await client.post(
                        session_url,
                        content=f"&F={func}&NUMRIG={num_alleg}&en={ENTE}".encode(),
                        headers=HEADERS
                    )
                    data = r3.json()
                    if data.get("K") == "000" and data.get("PATH"):
                        allegati.append({"url": data["PATH"], "filename": filename})
                    await asyncio.sleep(0.2)
            if allegati:
                item["allegati"] = allegati
                log.info(f"'{item['title'][:40]}': {len(allegati)} allegato/i")
            await asyncio.sleep(0.3)
        except Exception as e:
            log.debug(f"Errore allegati riga {num_riga}: {e}")
    return items

# ---------------------------------------------------------------------------
# Fetch lista atti + PDF in sessione unica
# ---------------------------------------------------------------------------
async def fetch_atti_with_pdf():
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        session_url = await open_session(client)
        if not session_url:
            return None
        try:
            r = await client.post(
                session_url,
                content=f"&F=MC01&en={ENTE}".encode(),
                headers=HEADERS
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.error(f"Errore MC01: {e}")
            return None
        items = parse_albo_html(r.text)
        if not items:
            return []
        await enrich_with_pdf(client, session_url, items)
    return items

# ---------------------------------------------------------------------------
# Parsing lista atti
# ---------------------------------------------------------------------------
def item_id(item):
    raw = (item.get("title", "") + item.get("num_pub", "")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]

def parse_albo_html(html):
    soup  = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.cmp-card")
    log.info(f"Card trovate: {len(cards)}")
    items = []
    for card in cards:
        link = card.find("a", attrs={"onclick": re.compile(r"MC02\(")})
        if not link:
            continue
        h5    = link.find("h5")
        title = h5.get_text(strip=True) if h5 else link.get_text(strip=True)
        if not title:
            continue
        num_span   = card.find("span", class_="fw-semibold")
        num_pub    = num_span.get_text(strip=True) if num_span else ""
        card_text  = card.get_text(separator=" ", strip=True)
        date_match = re.search(r"Pubblicazione dal\s+(\d{2}-\d{2}-\d{4})", card_text)
        date_str   = date_match.group(1) if date_match else ""
        tipo_match = re.search(r"Tipo:\s+([A-ZÀÈÉÌÒÙA-Z ]+)", card_text)
        tipo       = tipo_match.group(1).strip() if tipo_match else ""
        onclick    = link.get("onclick", "")
        nm         = re.search(r"MC02\('(\d+)'\)", onclick)
        num_riga   = nm.group(1) if nm else ""
        items.append({
            "title":    title,
            "num_riga": num_riga,
            "date":     date_str,
            "tipo":     tipo,
            "num_pub":  num_pub,
            "allegati": [],
        })
    seen_keys, unique = set(), []
    for item in items:
        if item["title"] not in seen_keys:
            seen_keys.add(item["title"])
            unique.append(item)
    return unique

# ---------------------------------------------------------------------------
# Formattazione e invio messaggi
# ---------------------------------------------------------------------------
def format_caption(item):
    tipo     = f" ({item['tipo']})" if item.get("tipo") else ""
    allegati = item.get("allegati", [])
    lines    = ["🏛 *Nuovo atto in Albo Pretorio*\n"]
    lines.append(f"📄 {item['title']}{tipo}")
    if item.get("num_pub"):
        lines.append(f"🔢 {item['num_pub']}")
    if item.get("date"):
        lines.append(f"📅 Dal {item['date']}")
    if len(allegati) > 1:
        lines.append(f"📎 Allegati: {len(allegati)} documenti — seguono in sequenza")
    elif len(allegati) == 1:
        lines.append("📎 Allegati: 1 documento")
    else:
        lines.append("📎 Nessun documento allegato")
    return "\n".join(lines)

async def send_item_to_chat(bot, chat_id: int, item: dict):
    caption  = format_caption(item)
    allegati = item.get("allegati", [])
    try:
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)
        for alleg in allegati:
            await asyncio.sleep(0.3)
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as dl:
                resp = await dl.get(alleg["url"])
                resp.raise_for_status()
            await bot.send_document(
                chat_id=chat_id,
                document=resp.content,
                filename=alleg["filename"],
            )
        log.info(f"✓ Inviato a {chat_id}: {item['title'][:60]}")
    except Exception as e:
        log.error(f"Errore invio a {chat_id}: {e}")

async def reply_item(update: Update, item: dict):
    caption  = format_caption(item)
    allegati = item.get("allegati", [])
    try:
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)
        for alleg in allegati:
            await asyncio.sleep(0.3)
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as dl:
                resp = await dl.get(alleg["url"])
                resp.raise_for_status()
            await update.message.reply_document(
                document=resp.content,
                filename=alleg["filename"],
            )
    except Exception as e:
        log.error(f"Errore reply atto: {e}")

async def notify(bot, item):
    for chat_id in get_all_recipients():
        await send_item_to_chat(bot, chat_id, item)

# ---------------------------------------------------------------------------
# Comandi Telegram
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs    = load_subscribers()
    chat_id = update.effective_chat.id
    iscritto = "✅ Sei già iscritto alle notifiche." if chat_id in subs else "ℹ️ Non sei ancora iscritto. Usa /abbonati per ricevere le notifiche."
    await update.message.reply_text(
        "🏛 *Albo Pretorio Bot*\n"
        "Comune di Roccabascerana\n\n"
        "Ti notifica ogni nuovo atto pubblicato in albo, "
        "con il documento allegato direttamente in chat.\n\n"
        f"{iscritto}" + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_abbonati(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs    = load_subscribers()
    if chat_id in subs:
        await update.message.reply_text("✅ Sei già iscritto alle notifiche dell'Albo Pretorio." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    subs.add(chat_id)
    save_subscribers(subs)
    log.info(f"Nuovo iscritto: {chat_id} (totale: {len(subs)})")
    await update.message.reply_text(
        "✅ *Iscrizione completata!*\n\n"
        "Riceverai una notifica con il documento allegato ogni volta che "
        "viene pubblicato un nuovo atto nell'Albo Pretorio del Comune di Roccabascerana."
        + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_disabbonati(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs    = load_subscribers()
    if chat_id in CONFIG["ADMIN_IDS"]:
        await update.message.reply_text("ℹ️ Gli amministratori ricevono sempre le notifiche." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    if chat_id not in subs:
        await update.message.reply_text("ℹ️ Non eri iscritto alle notifiche." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    subs.discard(chat_id)
    save_subscribers(subs)
    log.info(f"Disiscritto: {chat_id} (totale: {len(subs)})")
    await update.message.reply_text(
        "✅ *Iscrizione cancellata.*\n\n"
        "Non riceverai più notifiche dall'Albo Pretorio.\n"
        "Puoi reiscriverti in qualsiasi momento con /abbonati."
        + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_atti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("⏳ Recupero atti e documenti in corso...")
    items = await fetch_atti_with_pdf()
    if items is None:
        await msg.edit_text("❌ Impossibile recuperare gli atti. Riprova tra poco." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    if not items:
        await msg.edit_text("Nessun atto trovato al momento." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    await msg.delete()
    for item in items:
        await reply_item(update, item)
        await asyncio.sleep(0.3)
    await update.message.reply_text(f"✅ Mostrati {len(items)} atti." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_controlla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Controllo in corso...")
    seen = load_seen()
    html = await fetch_albo_html()
    if not html:
        await update.message.reply_text("❌ Impossibile raggiungere l'albo." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    items     = parse_albo_html(html)
    new_items = [i for i in items if item_id(i) not in seen]
    if new_items:
        await update.message.reply_text(f"🆕 {len(new_items)} nuovi atti! Recupero documenti...")
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            session_url = await open_session(client)
            if session_url:
                await enrich_with_pdf(client, session_url, new_items)
        new_seen = set(seen)
        for item in new_items:
            await reply_item(update, item)
            new_seen.add(item_id(item))
            await asyncio.sleep(0.3)
        save_seen(new_seen)
        await update.message.reply_text(f"✅ {len(new_items)} nuovi atti aggiunti all'archivio." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            f"✅ Nessun nuovo atto.\n🗂 Archivio: {len(seen)} · Albo ora: {len(items)}" + MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN
        )

async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Non riconosco questo tipo di messaggio." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Loop polling automatico
# ---------------------------------------------------------------------------
async def polling_loop(app):
    seen = load_seen()
    bot  = app.bot

    if not seen:
        log.info("Prima esecuzione: carico baseline...")
        html = await fetch_albo_html()
        if html:
            items = parse_albo_html(html)
            seen  = {item_id(i) for i in items}
            save_seen(seen)
            log.info(f"Baseline: {len(seen)} atti.")

    log.info(f"Polling ogni {CONFIG['INTERVAL_MINUTES']} min.")
    while True:
        await asyncio.sleep(CONFIG["INTERVAL_MINUTES"] * 60)
        try:
            html = await fetch_albo_html()
            if not html:
                continue
            items     = parse_albo_html(html)
            new_seen  = set(seen)
            new_items = [i for i in items if item_id(i) not in seen]
            if new_items:
                log.info(f"🆕 {len(new_items)} nuovi atti — recupero PDF...")
                async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                    session_url = await open_session(client)
                    if session_url:
                        await enrich_with_pdf(client, session_url, new_items)
                for item in new_items:
                    await notify(bot, item)
                    new_seen.add(item_id(item))
                save_seen(new_seen)
                seen = new_seen
            else:
                log.info("Nessun nuovo atto.")
            await send_heartbeat(bot, seen)
        except Exception as e:
            log.error(f"Errore loop: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    log.info("=== Albo Pretorio Bot avviato ===")
    app = Application.builder().token(CONFIG["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("abbonati",    cmd_abbonati))
    app.add_handler(CommandHandler("disabbonati", cmd_disabbonati))
    app.add_handler(CommandHandler("atti",        cmd_atti))
    app.add_handler(CommandHandler("controlla",   cmd_controlla))
    app.add_handler(MessageHandler(filters.ALL,   cmd_unknown))

    async with app:
        await app.start()
        await app.updater.start_polling()
        log.info("Bot in ascolto comandi + polling automatico ogni 3 ore...")
        await polling_loop(app)

if __name__ == "__main__":
    asyncio.run(main())
