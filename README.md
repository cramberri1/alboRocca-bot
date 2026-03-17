# 🏛 Albo Pretorio Bot – Comune di Roccabascerana

Bot Telegram che monitora l'albo pretorio del comune e invia una notifica
ogni volta che viene pubblicato un nuovo atto.

---

## Requisiti

- Python 3.11 o superiore
- Connessione internet
- Un bot Telegram (crealo con @BotFather)

---

## Installazione

```bash
# 1. Clona o copia la cartella del bot
cd albo_bot

# 2. Installa le dipendenze
pip install -r requirements.txt

# 3. Configura il bot
cp config.example.py config.py
# Apri config.py con un editor di testo e inserisci:
#   - BOT_TOKEN  → il token che ti ha dato @BotFather
#   - CHAT_IDS   → il tuo ID Telegram (scoprilo con @userinfobot)
```

---

## Avvio

```bash
python bot.py
```

Al primo avvio il bot carica tutti gli atti già presenti come baseline
(senza inviarti notifiche). Da quel momento in poi ti avvisa solo dei nuovi.

---

## Esecuzione automatica su Raspberry Pi / Linux (systemd)

Crea il file `/etc/systemd/system/albo-bot.service`:

```ini
[Unit]
Description=Albo Pretorio Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/pi/albo_bot
ExecStart=/usr/bin/python3 /home/pi/albo_bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Poi:

```bash
sudo systemctl daemon-reload
sudo systemctl enable albo-bot
sudo systemctl start albo-bot

# Controlla i log in tempo reale
sudo journalctl -u albo-bot -f
```

---

## Adattare i selettori CSS

Se il bot non trova gli atti, apri l'albo nel browser, clicca tasto destro
su un atto → "Ispeziona elemento", e guarda come sono strutturate le righe.
Poi modifica `CSS_SELECTORS` in `config.py`.

---

## File generati

| File | Descrizione |
|------|-------------|
| `seen_items.json` | Database locale degli atti già visti |
| `albo_bot.log` | Log dell'attività del bot |

---

## Struttura del progetto

```
albo_bot/
├── bot.py              # Logica principale
├── config.py           # Configurazione (NON condividere: contiene il token!)
├── config.example.py   # Template di configurazione
├── requirements.txt    # Dipendenze Python
├── seen_items.json     # Generato automaticamente al primo avvio
└── albo_bot.log        # Generato automaticamente
```
