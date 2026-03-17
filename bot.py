"""
Albo Pretorio Bot - Comune di Roccabascerana
Sistema Halley EG / EGHOMEPAGE.HBL

Comandi:
  /start     — benvenuto
  /atti      — mostra gli atti attuali con link al PDF
  /controlla — forza un controllo immediato
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
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
def load_config():
    cfg = {}
    if os.environ.get("BOT_TOKEN"):
        cfg["BOT_TOKEN"]        = os.environ["BOT_TOKEN"]
        cfg["CHAT_IDS"]         = [int(x.strip()) for x in os.environ.get("CHAT_IDS", "").split(",") if x.strip()]
        cfg["ALBO_URL"]         = os.environ.get("ALBO_URL", "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL")
        cfg["INTERVAL_MINUTES"] = int(os.environ.get("INTERVAL_MINUTES", "30"))
    else:
        try:
            import config
            cfg["BOT_TOKEN"]        = config.BOT_TOKEN
            cfg["CHAT_IDS"]         = config.CHAT_IDS
            cfg["ALBO_URL"]         = getattr(config, "ALBO_URL", "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL")
            cfg["INTERVAL_MINUTES"] = getattr(config, "INTERVAL_MINUTES", 30)
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
BASE_URL  = CONFIG["ALBO_URL"]
ORIGIN    = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(BASE_URL))
ENTE      = "e1396"
ALBO_LINK = "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
    "Content-Type":    "text/plain;charset=UTF-8; boundary=AZazAZ",
    "Referer":         BASE_URL,
    "Origin":          ORIGIN,
}

# ---------------------------------------------------------------------------
# Database locale
# ---------------------------------------------------------------------------
DB_PATH        = Path("seen_items.json")
HEARTBEAT_PATH = Path("last_heartbeat.txt")

def load_seen():
    if DB_PATH.exists():
        return set(json.loads(DB_PATH.read_text(encoding="utf-8")))
    return set()

def save_seen(seen):
    DB_PATH.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def should_send_heartbeat():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if HEARTBEAT_PATH.exists():
        return HEARTBEAT_PATH.read_text(encoding="utf-8").strip() != today
    return True

def mark_heartbeat_sent():
    HEARTBEAT_PATH.write_text(datetime.utcnow().strftime("%Y-%m-%d"), encoding="utf-8")

async def send_heartbeat(bot, seen):
    text = (
        "✅ *Albo Pretorio Bot – report giornaliero*\n\n"
        f"🗂 Atti in archivio: *{len(seen)}*\n"
        f"🕐 {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC\n\n"
        "_Tutto funziona correttamente._"
    )
    for chat_id in CONFIG["CHAT_IDS"]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.error(f"Errore heartbeat: {e}")
    mark_heartbeat_sent()

# ---------------------------------------------------------------------------
# Sessione: apre una sessione e restituisce (client, session_url)
# IMPORTANTE: il client deve rimanere aperto per tutta la sessione
# ---------------------------------------------------------------------------
async def open_session(client: httpx.AsyncClient):
    """Prima POST MC09 → restituisce l'URL di sessione con token."""
    body = f"ss=1&F=MC09&en={ENTE}&freeze=1"
    try:
        r = await client.post(BASE_URL, content=body.encode(), headers=HEADERS)
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
# Fetch lista atti + PDF tutto in una sessione
# ---------------------------------------------------------------------------
async def fetch_atti_with_pdf():
    """
    Apre UNA sola sessione e in sequenza:
    1. MC01 → lista atti
    2. Per ogni atto: MC02 → scheda → MC98 → URL PDF
    Restituisce la lista completa con pdf_url popolati.
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:

        # Apri sessione
        session_url = await open_session(client)
        if not session_url:
            return None

        # MC01: lista atti
        try:
            r = await client.post(
                session_url,
                content=f"&F=MC01&en={ENTE}".encode(),
                headers=HEADERS
            )
            r.raise_for_status()
            html = r.text
        except httpx.HTTPError as e:
            log.error(f"Errore MC01: {e}")
            return None

        if len(html) < 100:
            return None

        items = parse_albo_html(html)
        if not items:
            return []

        # Per ogni atto: MC02 → MC98 → PDF
        for item in items:
            num_riga = item.get("num_riga")
            if not num_riga:
                continue
            try:
                # MC02: scheda atto
                r2 = await client.post(
                    session_url,
                    content=f"&F=MC02&NUMRIGA={num_riga}&en={ENTE}".encode(),
                    headers=HEADERS
                )
                r2.raise_for_status()

                # Trova MC98(N) nella scheda
                soup2    = BeautifulSoup(r2.text, "html.parser")
                mc98_tag = soup2.find("a", attrs={"onclick": re.compile(r"MC98\(")})
                if not mc98_tag:
                    log.debug(f"Nessun MC98 per riga {num_riga}")
                    continue

                m = re.search(r"MC98\((\d+)\)", mc98_tag.get("onclick", ""))
                if not m:
                    continue
                num_allegato = m.group(1)

                # MC98: URL del PDF
                r3 = await client.post(
                    session_url,
                    content=f"&F=MC98&NUMRIG={num_allegato}&en={ENTE}".encode(),
                    headers=HEADERS
                )
                r3.raise_for_status()

                data = r3.json()
                log.debug(f"MC98 riga {num_riga}: {data}")
                if data.get("K") == "000" and data.get("PATH"):
                    item["pdf_url"] = data["PATH"]
                    log.info(f"PDF trovato per '{item['title'][:40]}': {data['PATH']}")

                await asyncio.sleep(0.3)

            except Exception as e:
                log.debug(f"Errore PDF riga {num_riga}: {e}")

    return items


async def fetch_albo_html():
    """Versione senza PDF, per il polling veloce."""
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

        num_span = card.find("span", class_="fw-semibold")
        num_pub  = num_span.get_text(strip=True) if num_span else ""

        card_text  = card.get_text(separator=" ", strip=True)
        date_match = re.search(r"Pubblicazione dal\s+(\d{2}-\d{2}-\d{4})", card_text)
        date_str   = date_match.group(1) if date_match else ""

        tipo_match = re.search(r"Tipo:\s+([A-ZÀÈÉÌÒÙA-Z ]+)", card_text)
        tipo       = tipo_match.group(1).strip() if tipo_match else ""

        onclick  = link.get("onclick", "")
        nm       = re.search(r"MC02\('(\d+)'\)", onclick)
        num_riga = nm.group(1) if nm else ""

        items.append({
            "title":    title,
            "num_riga": num_riga,
            "date":     date_str,
            "tipo":     tipo,
            "num_pub":  num_pub,
            "pdf_url":  None,
        })

    seen_keys, unique = set(), []
    for item in items:
        if item["title"] not in seen_keys:
            seen_keys.add(item["title"])
            unique.append(item)
    return unique

# ---------------------------------------------------------------------------
# Notifiche
# ---------------------------------------------------------------------------
def format_message(item):
    tipo  = f" ({item['tipo']})" if item.get("tipo") else ""
    lines = ["🏛 *Nuovo atto in Albo Pretorio*\n"]
    lines.append(f"📄 {item['title']}{tipo}")
    if item.get("num_pub"):
        lines.append(f"🔢 {item['num_pub']}")
    if item.get("date"):
        lines.append(f"📅 Dal {item['date']}")
    return "\n".join(lines)

def make_keyboard(item):
    buttons = []
    if item.get("pdf_url"):
        buttons.append(InlineKeyboardButton("📎 Apri PDF", url=item["pdf_url"]))
    buttons.append(InlineKeyboardButton("🏛 Albo Pretorio", url=ALBO_LINK))
    return InlineKeyboardMarkup([buttons])

async def notify(bot, item):
    for chat_id in CONFIG["CHAT_IDS"]:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_message(item),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_keyboard(item),
            )
            log.info(f"✓ Notificato: {item['title'][:60]}")
        except Exception as e:
            log.error(f"Errore invio: {e}")

# ---------------------------------------------------------------------------
# Comandi Telegram
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏛 *Albo Pretorio Bot*\n"
        "Comune di Roccabascerana\n\n"
        "Ti notifica ogni nuovo atto pubblicato in albo.\n\n"
        "*Comandi:*\n"
        "/atti — mostra gli atti attuali con link al PDF\n"
        "/controlla — forza un controllo immediato\n"
        "/start — questo messaggio"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_atti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Recupero atti e PDF in corso...")
    items = await fetch_atti_with_pdf()
    if items is None:
        await msg.edit_text("❌ Impossibile recuperare gli atti. Riprova tra poco.")
        return
    if not items:
        await msg.edit_text("Nessun atto trovato al momento.")
        return

    await msg.delete()
    for item in items:
        tipo  = f" — _{item['tipo']}_" if item.get("tipo") else ""
        data  = f" · dal {item['date']}" if item.get("date") else ""
        num   = f"*{item['num_pub']}*  " if item.get("num_pub") else ""
        text  = f"{num}{item['title']}{tipo}{data}"
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_keyboard(item),
        )
        await asyncio.sleep(0.1)


async def cmd_controlla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Controllo in corso...")
    seen = load_seen()
    html = await fetch_albo_html()
    if not html:
        await update.message.reply_text("❌ Impossibile raggiungere l'albo.")
        return

    items     = parse_albo_html(html)
    new_items = [i for i in items if item_id(i) not in seen]

    if new_items:
        await update.message.reply_text(f"🆕 {len(new_items)} nuovi atti! Recupero PDF...")
        # Recupera PDF per i soli nuovi atti in sessione unica
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            session_url = await open_session(client)
            # Prima MC01 per "entrare" nella lista (necessario per il contesto di sessione)
            await client.post(
                session_url,
                content=f"&F=MC01&en={ENTE}".encode(),
                headers=HEADERS
            )
            for item in new_items:
                if not item.get("num_riga"):
                    continue
                try:
                    r2 = await client.post(
                        session_url,
                        content=f"&F=MC02&NUMRIGA={item['num_riga']}&en={ENTE}".encode(),
                        headers=HEADERS
                    )
                    soup2    = BeautifulSoup(r2.text, "html.parser")
                    mc98_tag = soup2.find("a", attrs={"onclick": re.compile(r"MC98\(")})
                    if mc98_tag:
                        m = re.search(r"MC98\((\d+)\)", mc98_tag.get("onclick", ""))
                        if m:
                            r3   = await client.post(
                                session_url,
                                content=f"&F=MC98&NUMRIG={m.group(1)}&en={ENTE}".encode(),
                                headers=HEADERS
                            )
                            data = r3.json()
                            if data.get("K") == "000" and data.get("PATH"):
                                item["pdf_url"] = data["PATH"]
                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.debug(f"Errore PDF nuovo atto: {e}")

        new_seen = set(seen)
        for item in new_items:
            await notify(context.bot, item)
            new_seen.add(item_id(item))
        save_seen(new_seen)
    else:
        await update.message.reply_text(
            f"✅ Nessun nuovo atto.\n🗂 Archivio: {len(seen)} · Albo ora: {len(items)}"
        )

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
                log.info(f"🆕 {len(new_items)} nuovi atti — recupero PDF in sessione unica...")
                async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                    session_url = await open_session(client)
                    await client.post(
                        session_url,
                        content=f"&F=MC01&en={ENTE}".encode(),
                        headers=HEADERS
                    )
                    for item in new_items:
                        if not item.get("num_riga"):
                            continue
                        try:
                            r2 = await client.post(
                                session_url,
                                content=f"&F=MC02&NUMRIGA={item['num_riga']}&en={ENTE}".encode(),
                                headers=HEADERS
                            )
                            soup2    = BeautifulSoup(r2.text, "html.parser")
                            mc98_tag = soup2.find("a", attrs={"onclick": re.compile(r"MC98\(")})
                            if mc98_tag:
                                m = re.search(r"MC98\((\d+)\)", mc98_tag.get("onclick", ""))
                                if m:
                                    r3   = await client.post(
                                        session_url,
                                        content=f"&F=MC98&NUMRIG={m.group(1)}&en={ENTE}".encode(),
                                        headers=HEADERS
                                    )
                                    data = r3.json()
                                    if data.get("K") == "000" and data.get("PATH"):
                                        item["pdf_url"] = data["PATH"]
                            await asyncio.sleep(0.3)
                        except Exception as e:
                            log.debug(f"Errore PDF: {e}")

                for item in new_items:
                    await notify(bot, item)
                    new_seen.add(item_id(item))
                save_seen(new_seen)
                seen = new_seen
            else:
                log.info("Nessun nuovo atto.")
            if should_send_heartbeat():
                await send_heartbeat(bot, seen)
        except Exception as e:
            log.error(f"Errore loop: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    log.info("=== Albo Pretorio Bot avviato ===")
    app = Application.builder().token(CONFIG["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("atti",      cmd_atti))
    app.add_handler(CommandHandler("controlla", cmd_controlla))

    async with app:
        await app.start()
        await app.updater.start_polling()
        log.info("Bot in ascolto...")
        await polling_loop(app)

if __name__ == "__main__":
    asyncio.run(main())
