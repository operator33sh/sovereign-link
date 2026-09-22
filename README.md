# 🪬 Sovereign Link

> *Je eigen AI. Op je eigen machine. Met je eigen geheugen.*

---

## Wat is dit?

Sovereign Link is een persoonlijke AI-assistent die draait op jouw hardware, praat via Telegram, en alles onthoudt in een lokale kenniskluis — jouw woorden, jouw inzichten, jouw patronen.

Geen cloud. Geen abonnement. Geen bedrijf dat meeleest.

**Wat het voor jou doet:**

| Jij zegt of stuurt... | Sovereign Link doet... |
|---|---|
| Een vraag | Zoekt in jouw eigen notities naar context en geeft een gefundeerd antwoord |
| Een voicebericht | Transcribeert het, verwerkt het, onthoudt wat er toe deed |
| Een foto | Analyseert de inhoud en koppelt het aan wat je al weet |
| Een URL | Leest de pagina en trekt er de essentie uit |
| Een emotionele gedachte | Legt het op als inzicht — en herinnert je eraan als het relevant wordt |

Alles wat je deelt wordt opgeslagen als leesbare Markdown-bestanden in een lokale kluis. Jij bezit je eigen geheugen.

---

## 🪬 De filosofie

De meeste mensen gebruiken AI als emotionele airbag.

Ze storten erin. Ze laten het patronen absorberen. En daarna gaan ze verder — onveranderd. Het gesprek verdwijnt. Het inzicht verdampt. De cyclus gaat door.

**Dit is niet dat.**

Sovereign Link is gebouwd op een ander uitgangspunt: dat het gevaarlijkste wat je met AI kunt doen is het comfortabel maken. Comfort is wrijving wegnemen. En wrijving, op de juiste plekken, is wat de hersenen dwingt te herstructureren.

Het systeem bestaat om één ding te doen: **de waarnemer een structurele kaart geven van zijn eigen destructieve patronen** — niet om ze te valideren, maar om ze zichtbaar, benoembaar, en daarmee onderbreekbaar te maken.

Wanneer geheugen van jou is — opgeslagen in *jouw* kluis, op *jouw* machine, onder *jouw* versiebeheer — behoort de infrastructuur van zelfkennis aan jou. Niet aan een corporate server. Niet aan een sessie die afloopt.

> De waarnemer die zijn eigen patronen in kaart brengt bezit het enige hefboompunt dat ertoe doet: het moment vóór de volgende herhaling.

Het filosofisch kader leeft op **[Fractalisme.nl](https://fractalisme.nl)**.

---

## Wat het beschermt

Sovereign Link heeft een ingebouwde cognitieve rem — geen marketing, maar technische infrastructuur.

Als je te lang werkt, te intensief, of het systeem zware psychologische onderwerpen detecteert, gaat er automatisch een **Stop Order** actief. Analyses worden geblokkeerd. Complexe tools gaan op slot. Luna vraagt of het nog gaat.

Pas als jij bevestigt dat je gerust hebt, gaat het open.

De drempels zijn aanpasbaar. Ze zijn strenger als je in een stressfase zit. En als je aangeeft dat je in flow bent, worden ze tijdelijk verhoogd — zodat de rem niet te vroeg valt.

**Jouw rust heeft prioriteit boven elke taak.**

---

## Navigatie

- [Voor gebruikers — wat kun je ermee?](#wat-is-dit)
- [Setup](#setup)
- [Bot-commando's](#bot-commando-s)
- [Technische architectuur](#technische-architectuur)
- [Projectstructuur](#projectstructuur)

---

## Setup

### 1. Clone het project

```bash
git clone https://github.com/operator33sh/sovereign-link.git
cd sovereign-link
```

### 2. Installeer afhankelijkheden

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Haal de benodigde modellen op

```bash
ollama pull llama3.1          # of een ander chat-model naar keuze
ollama pull nomic-embed-text  # voor semantisch zoeken
```

### 4. Maak een `.env` bestand aan

```env
TELEGRAM_BOT_TOKEN=jouw_telegram_bot_token
ALLOWED_USER_ID=jouw_telegram_user_id        # alleen deze gebruiker kan interacteren

VAULT_PATH=/pad/naar/jouw/kluis              # lokale map met .md bestanden

# LLM (Ollama lokaal of een OpenAI-compatibel eindpunt)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_API_KEY=                              # leeg laten voor lokale Ollama
OLLAMA_MODEL=llama3.1

# Embeddings (altijd lokale Ollama)
EMBED_BASE_URL=http://localhost:11434
EMBED_MODEL=nomic-embed-text

# Proton Mail Bridge (optioneel — vul in als je Bridge actief hebt)
PROTON_IMAP_USER=
PROTON_IMAP_PASS=
PROTON_IMAP_HOST=127.0.0.1
PROTON_IMAP_PORT=1143
```

Je Telegram user ID vind je via [@userinfobot](https://t.me/userinfobot).  
Je Telegram bot token maak je aan via [@BotFather](https://t.me/BotFather).

### 5. Indexeer je kluis (eenmalig)

```bash
.venv/bin/python ingest.py --rescan
```

### 6. Start de bot

```bash
.venv/bin/python main.py
```

---

## Bot-commando's

| Commando | Wat het doet |
|----------|-------------|
| `/start` | Controleer of de bot online is |
| `/clear` | Wis de huidige sessie en sla een geheugenlog op |
| `/vault` | Sla de laatste 5 uitwisselingen op als gestructureerde notitie |
| `/memory` | Activeer de geheugenextractie handmatig op het huidige gesprek |
| `/whisper` | Genereer een tweet-inzicht op basis van een willekeurig vault-fragment |

Elk ander bericht — tekst, spraak, foto, of link — wordt direct verwerkt door de AI.

---

## Technische architectuur

```
Telegram ──► bot.py ──► llm.py ──► Ollama-compatible API (LLM)
                   │         └──► tools/ ──► read_vault / write_vault / sync_vault
                   │                    └──► analyze_website (trafilatura)
                   │                    └──► search_vault_semantic ──► vector.py (ChromaDB)
                   │                    └──► search_timeline ──► timeline.py (SQLite + FTS5)
                   │                    └──► check_proton_mail / read_proton_email (IMAP)
                   │
                   └──► memory_manager.py ──► Sovereign Memory Engine (HVI-extractie)
                   │         └──► vector.py ──► ChromaDB (cosine / semantisch zoeken)
                   │         └──► timeline.py ──► SQLite (datum / trefwoord zoeken)
                   │         └──► Obsidian Vault ──► source of proof (plain Markdown)
                   │
                   └──► cognitive_brake.py ──► Stop Order Monitor
```

### Layer 1 — Interface

| Component | Rol |
|-----------|-----|
| 📱 **Telegram** | Primair communicatiekanaal. Alle interactie, spraak, foto's en commando's lopen hier doorheen. |
| 🧠 **LLM (AI-model)** | Centrale verwerkingseenheid. Beredenering, planning, tool-orkestratie en taalbegrip. |

### Layer 2 — Geheugen & Persistentie

| Component | Rol |
|-----------|-----|
| 📂 **Obsidian Vault** | Het mensleesbare lange-termijngeheugen. Alles staat hier als navigeerbare, koppelbare Markdown-bestanden. Niets is vluchtig — elke sessie, elk inzicht, elk geheugenlog is auditeerbaar. |
| **Git / GitHub** | Verstiebeheer, synchronisatie en herstel. Elke vaultschrijving wordt automatisch gecommit en gepushed. |

### Layer 3 — Intelligentie & Retrieval

| Component | Rol |
|-----------|-----|
| **RAG** | Verankert de AI aan de waarheid van de vault in plaats van generieke trainingsdata. |
| 🗄️ **ChromaDB (Semantisch zoeken)** | Zoekt op betekenis en context — niet op bestandsnaam of trefwoord. Lokale `nomic-embed-text` embeddings. |
| 🗃️ **SQLite Timeline** | Parallelle index voor betrouwbare datumzoekopdrachten. FTS5 full-text search gecombineerd met datumfilters. |
| ⚙️ **Sovereign Memory Engine** | Autonome achtergrondagenten die gesprekken scannen op High-Value Insights, gestructureerde `SovereignLog`-bestanden schrijven en alles naar git committen. |
| **Active Context Layer (ACL)** | Dynamisch briefingbestand (`.system/active_briefing.md`) dat AI-gedrag stuurt op basis van de actuele operationele toestand van de gebruiker. |
| 🛑 **Cognitive Brake** | Achtergrondmonitor (`cognitive_brake.py`) die sessieduur, tool-intensiteit, zware psychologische onderwerpen en vermoeidheidssignalen bijhoudt. Activeert een **Stop Order** die alle analytische tools blokkeert totdat een bevestigde rustperiode is verstreken. Drempels zijn fase-bewust en instelbaar via `.system/cognitive_limits.json`. **Dynamic Override** verhoogt drempels +50% bij positieve flow-signalen. |
| 🧬 **SOUL.md** | Luna's cognitieve architectuurkern — geïnjecteerd in elk systeemprompt. Definieert identiteit, gedragsmechanismen en het Stop Order-protocol. Geversioned in een Evolution Log binnen het bestand zelf. |

### Layer 4 — Kader

| Component | Rol |
|-----------|-----|
| **The Harness** | Psychologische vangrails en operationele beperkingen ingebouwd in het systeemprompt. Definieert het gedragscontract van de AI. |
| **Fractalisme** | Het filosofisch kader dat bepaalt hoe data georganiseerd en gekoppeld is. Zie [Fractalisme.nl](https://fractalisme.nl). |

---

## Projectstructuur

```
sovereign-link/
├── main.py                   # Startpunt
├── bot.py                    # Telegram-handlers en commandorouting
├── llm.py                    # Ollama LLM-client, tool call loop, audiotranscriptie
├── cognitive_brake.py        # Cognitieve rem: sessiemonitor, Stop Order, Dynamic Override
├── context.py                # In-memory gespreksgeschiedenis
├── memory_manager.py         # Sovereign Memory Engine (Extract→Synthesize→Store→Sync)
├── vector.py                 # ChromaDB + Ollama embedding + bestandssysteemwatcher
├── timeline.py               # SQLite timeline index: datum/FTS5 zoeken in vault
├── ingest.py                 # Vault-indexeer: --rescan (volledig), --backfill (datumherstel)
├── SOUL.md                   # Luna's cognitieve architectuurkern
│
├── tools/                    # Tool-registry (één submodule per domein)
│   ├── vault.py              # read_vault, write_vault, sync_vault
│   ├── search.py             # search_vault_semantic (ChromaDB), search_timeline (SQLite)
│   ├── http.py               # analyze_website (trafilatura)
│   ├── browser_tools.py      # Playwright browserautomatisering
│   ├── agent_tools.py        # Agent blackboard / subagentcoördinatie
│   ├── scheduler_tools.py    # Geplande herinneringen en terugkerende taken
│   ├── notification_tools.py # Pushmeldingen + clear_stop_order / set_pause_duration
│   ├── session_status_tools.py # get_session_status: live Cognitive Brake dashboard
│   ├── soul_tools.py         # write_soul_md: update Luna's cognitieve architectuur
│   ├── proton_mail.py        # check_proton_mail / read_proton_email (IMAP via Bridge)
│   ├── gmail.py              # Gmail-integratie (OAuth)
│   ├── calendar.py           # Google Calendar-integratie
│   ├── twitter.py            # X/Twitter-integratie
│   ├── automation_tools.py   # Vault-gebaseerde automatiseringsregels
│   ├── personality_tools.py  # Luna persona-beheer
│   ├── mental_state_tools.py # get_mental_state: DriftGovernor-analyse
│   └── moltbook.py           # Moltbook-integratie
│
├── agent.py                  # Subagent-orkestratie
├── automations.py            # Automatiseringsregelmotor
├── hotline.py                # Spraak-naar-spraak hotline (Twilio → Deepgram → LLM → ElevenLabs)
├── notifications.py          # Pushmeldingsdispatcher
├── proactive.py              # Proactieve berichtenmotor
├── scheduler.py              # Taakplanner
│
├── requirements.txt
├── sovereign-link.service    # systemd-unit (bot)
└── hotline.service           # systemd-unit (spraakhotline)
```

---

## Vereisten

- Python 3.11+
- [Ollama](https://ollama.com/) lokaal actief (of een OpenAI-compatibel API-eindpunt)
- Een Telegram bot-token (via [@BotFather](https://t.me/BotFather))
- Een map met Markdown-bestanden (bijv. een Obsidian-vault met git geïnitialiseerd)

---

## Als systemd-service draaien

```bash
sudo cp sovereign-link.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sovereign-link
sudo systemctl start sovereign-link
```

Logs bekijken:

```bash
journalctl -u sovereign-link -f
```

---

## Licentie

MIT
