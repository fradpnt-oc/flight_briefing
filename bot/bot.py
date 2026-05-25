"""
Flight Briefing Bot
-------------------
Conversation states per chat_id:
  idle                → waiting for a briefing request
  awaiting_clarify    → asked a flight parameter question (duration, type, etc.)
  awaiting_data       → asked for missing airport/passenger DB data
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Configuration ─────────────────────────────────────────────────────────────

BOT_TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID    = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
OLLAMA_URL         = os.environ["OLLAMA_URL"]           # e.g. http://192.168.0.108:11434
OLLAMA_MODEL       = os.environ.get("OLLAMA_MODEL", "mistral")
WEBSERVER_URL      = os.environ["WEBSERVER_URL"]        # e.g. http://<tailscale-ip>:3300/briefing
DB_PATH            = Path(os.environ.get("DB_PATH", "/data/flight_briefing.db"))
PROMPTS_PATH       = Path(os.environ.get("PROMPTS_PATH", "/data/prompts.json"))
RUNNER_CONTAINER   = os.environ.get("RUNNER_CONTAINER", "briefing-runner")

CLI_SCHEMA = (
    "python3 flight_briefing.py [ICAO...] "
    "--time HOURS "
    "[--type pattern|local|cross-country] "
    "[--aircraft aquila_a211|cavalon_914|mto_sport_912] "
    "[--pax NAME:WEIGHT_KG:HEIGHT_CM] "
    "[--baggage KG] "
    "[--no-pax] "
    "[--yes]\n\n"
    "Examples:\n"
    "  flight_briefing.py EDFE --time 1.0 --type pattern --no-pax --yes\n"
    "  flight_briefing.py EDFE EDFM --time 3.0 --pax Eric:68:180 --baggage 10 --yes\n"
    "  flight_briefing.py EDFE EDFV EDFZ --time 2.5 --pax Gabi:60:165 --baggage 5 --yes"
)

AIRCRAFT_CHOICES: list[tuple[str, str]] = []
AIRCRAFT_KEYWORDS: dict[str, str] = {}

def reload_aircraft_from_db() -> None:
    """Load aircraft choices and keyword aliases from the DB."""
    global AIRCRAFT_CHOICES, AIRCRAFT_KEYWORDS
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT code, name, aliases FROM aircraft ORDER BY name").fetchall()
        conn.close()
    except Exception as exc:
        log.warning("Could not load aircraft from DB: %s", exc)
        return
    choices = []
    keywords: dict[str, str] = {}
    for code, name, aliases in rows:
        choices.append((code, name))
        if aliases:
            for alias in aliases.split(","):
                kw = alias.strip().lower()
                if kw:
                    keywords[kw] = code
        # Always map the code itself
        keywords[code.lower()] = code
    AIRCRAFT_CHOICES = choices
    AIRCRAFT_KEYWORDS = keywords
    log.info("Loaded %d aircraft from DB", len(choices))

DONE_PHRASES = [
    "done", "i'm done", "im done", "all set", "all good", "ready",
    "finished", "ok done", "c'est fait", "fertig", "erledigt",
    "added", "ok go", "let's go", "continue", "go ahead", "proceed",
]

ADMIN_PHRASES = [
    # Airport list
    "airport list", "list airports", "show airports", "my airports",
    "which airports", "what airports", "aeroports", "flugplätze",
    "add airport", "new airport", "edit airport",
    # Passenger list
    "passenger list", "list passengers", "show passengers", "my passengers",
    "who are the passengers", "passagiere", "passagers",
    "add passenger", "new passenger", "edit passenger",
    # Generic admin
    "admin", "database", "manage", "settings",
]

TRIGGER_PHRASES = [
    "flight briefing",
    "flight briefing agent",
    "flight planning assistant",
    "briefing for a flight",
    "create a briefing",
    "prepare a briefing",
    "briefing ",
]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("briefing-bot")


# ── Conversation state ────────────────────────────────────────────────────────

@dataclass
class ConversationState:
    status: str = "idle"
    # The accumulated request — grows as the user answers clarification questions
    accumulated_request: str = ""
    # Selected aircraft code (set before parsing begins)
    aircraft_type: str = ""
    # Pilot override name (empty = use stored pilot)
    pilot_name: str = ""
    # DB-level missing data
    missing_airports: list = field(default_factory=list)
    missing_passengers: list = field(default_factory=list)
    # How many clarification rounds we've done (safety limit)
    clarify_rounds: int = 0


MAX_CLARIFY_ROUNDS = 4   # prevent infinite loops

_state: dict[int, ConversationState] = {}

def get_state(chat_id: int) -> ConversationState:
    if chat_id not in _state:
        _state[chat_id] = ConversationState()
    return _state[chat_id]

def reset_state(chat_id: int) -> None:
    _state[chat_id] = ConversationState()


# ── Database helpers ──────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_known_passengers() -> str:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT name, weight, height FROM passengers ORDER BY name"
        ).fetchall()
    return "\n".join(f"{r['name']}:{int(r['weight'])}:{int(r['height'])}" for r in rows)

def get_known_airports() -> str:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT icao, elevation_ft FROM airport_profiles ORDER BY icao"
        ).fetchall()
    return "  ".join(f"{r['icao']}:{int(r['elevation_ft'])}" for r in rows)

def insert_airport(icao: str, elevation_ft: float, runways: list) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO airport_profiles (icao, elevation_ft) VALUES (?, ?)",
            (icao.upper(), elevation_ft),
        )
        for rw in runways:
            if rw.get("designator") and rw.get("surface") and rw.get("tora_m") and rw.get("lda_m"):
                conn.execute(
                    "INSERT INTO airport_runways (icao, runway, surface, tora, lda) VALUES (?,?,?,?,?)",
                    (icao.upper(), rw["designator"], rw["surface"], rw["tora_m"], rw["lda_m"]),
                )
        conn.commit()

def insert_passenger(name: str, weight_kg: float, height_cm: float) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO passengers (name, weight, height) VALUES (?,?,?)",
            (name, weight_kg, height_cm),
        )
        conn.commit()


# ── Prompt helpers ────────────────────────────────────────────────────────────

def load_prompts() -> dict:
    if PROMPTS_PATH.exists():
        return json.loads(PROMPTS_PATH.read_text())
    log.warning("prompts.json not found at %s, using bare fallback", PROMPTS_PATH)
    return {
        "parser":      {"system": "Parse the briefing request into a CLI command."},
        "clarify":     {"system": "Ask the user for missing data."},
        "parse_reply": {"system": "Parse the user reply into JSON."},
    }

def render_prompt(template: str, **kwargs) -> str:
    for key, value in kwargs.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


# ── Ollama client ─────────────────────────────────────────────────────────────

async def call_ollama(system: str, user: str, timeout: float = 120.0) -> str:
    import time
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    prompt_chars = len(system) + len(user)
    log.info("[ollama] sending | model=%s prompt_chars=%d", OLLAMA_MODEL, prompt_chars)
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    elapsed = time.monotonic() - t0
    content = data["message"]["content"].strip()
    eval_tokens   = data.get("eval_count", 0)
    eval_duration = data.get("eval_duration", 0) / 1e9
    tok_per_sec   = round(eval_tokens / eval_duration, 1) if eval_duration else "?"
    log.info("[ollama] done | %.1fs | prompt_tokens=%s eval_tokens=%s tok/s=%s",
             elapsed, data.get("prompt_eval_count","?"), eval_tokens, tok_per_sec)
    log.info("[ollama] output: %s", content[:300])
    return content


# ── Runner executor ───────────────────────────────────────────────────────────

async def run_briefing(cli_args: str) -> tuple[bool, str]:
    args = re.sub(r"^(python3\s+)?flight_briefing\.py\s+", "", cli_args).strip()
    # No caching — each run is a fresh process, weather is always re-fetched
    cmd = ["docker", "exec", RUNNER_CONTAINER,
           "python3", "-u", "flight_briefing.py"] + args.split()
    import datetime as _dt
    log.info("[%s] Executing: %s", _dt.datetime.now().strftime("%H:%M:%S"), " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode("utf-8", errors="replace")
        success = proc.returncode == 0
        log.info("Runner exit=%d", proc.returncode)
        return success, output
    except asyncio.TimeoutError:
        return False, "Briefing generation timed out after 120 seconds."
    except Exception as exc:
        return False, f"Runner error: {exc}"


# ── LLM parser ────────────────────────────────────────────────────────────────

import re as _re


def _parse_duration(text: str):
    """Extract flight duration in decimal hours from natural language.
    Handles both . and , as decimal separator (e.g. 1.5h and 1,5h).
    """
    t = text.lower().replace(',', '.')  # normalise comma decimal separator
    m = _re.search(r'(\d+)h(\d+)m?', t)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    m = _re.search(r'(\d+\.\d+)\s*h', t)
    if m:
        return float(m.group(1))
    m = _re.search(r'(\d+)\s*h(?:r|rs|our|ours)?\b', t)
    if m:
        return float(m.group(1))
    m = _re.search(r'(\d+)\s*m(?:in)?\b', t)
    if m:
        return round(int(m.group(1)) / 60, 2)
    if _re.search(r'\bone hour\b|\ban hour\b', t):
        return 1.0
    return None


def _deterministic_parse(request: str):
    """Regex-based parser. Returns result dict or None if too ambiguous."""
    text = _resolve_airport_names(request.strip())
    lower = text.lower()

    # Detect pilot override so we can exclude them from passenger list
    pilot_lower = ""
    m_pilot = _re.search(r'\bwith\s+(\w+)\s+as\s+pilot\b|\b(\w+)\s+as\s+pilot\b', lower)
    if m_pilot:
        pilot_lower = next(g for g in m_pilot.groups() if g is not None).lower()

    icaos = _re.findall(r'\b([A-Z]{4})\b', text)
    if not icaos:
        return None

    duration = _parse_duration(lower)
    if duration is None:
        return {"type": "clarify", "question": "How long is the flight? (e.g. 1h, 2h30, 90min)"}

    if _re.search(r'cross.country|\bxc\b', lower):
        flight_type = "cross-country"
    elif _re.search(r'pattern|patter|circuit|local|tour de piste', lower):
        flight_type = "pattern"
    else:
        flight_type = "pattern" if len(icaos) == 1 else "cross-country"

    no_pax = bool(_re.search(
        r'\bsolo\b|\balone\b|\bjust me\b|no.pa[sx]|no passenger|'
        r'flying alone|i will fly alone|no one|\bseul\b|\ballein\b', lower))

    baggage = None
    m = _re.search(r'(\d+)\s*kg\s*(?:luggage|baggage|bag|bagage)?\b', lower)
    if m:
        baggage = int(m.group(1))

    known_pax = {}
    known_airports = set()
    with db_connect() as conn:
        for r in conn.execute("SELECT name, weight, height FROM passengers").fetchall():
            known_pax[r["name"].lower()] = r
        for r in conn.execute("SELECT icao FROM airport_profiles").fetchall():
            known_airports.add(r["icao"])

    pax_args = []
    missing_pax = []
    missing_airports = [icao for icao in icaos if icao not in known_airports]

    if not no_pax:
        for name_lower, pax in known_pax.items():
            if name_lower in lower and name_lower != "francois" and name_lower != pilot_lower:
                pax_args.append(f"{pax['name']}:{int(pax['weight'])}:{int(pax['height'])}")

    if missing_airports:
        return {"type": "missing", "airports": missing_airports, "passengers": missing_pax}
    if missing_pax:
        return {"type": "missing", "airports": [], "passengers": missing_pax}

    parts = ["python3", "flight_briefing.py"] + icaos
    parts += ["--time", str(duration), "--type", flight_type]
    if no_pax or not pax_args:
        parts.append("--no-pax")
    else:
        for p in pax_args:
            parts += ["--pax", p]
    if baggage:
        parts += ["--baggage", str(baggage)]
    parts.append("--yes")

    return {"type": "cli", "line": " ".join(parts)}


async def parse_request(request: str) -> dict:
    """Hybrid parser: deterministic regex first, LLM fallback for ambiguous cases."""
    result = _deterministic_parse(request)
    if result is not None:
        log.info("[parser] deterministic result: %s", result)
        return result

    log.info("[parser] falling back to LLM")
    prompts = load_prompts()
    system = render_prompt(
        prompts["parser"]["system"],
        cli_schema=CLI_SCHEMA,
        known_passengers=get_known_passengers(),
        known_airports=get_known_airports(),
        current_date=__import__("datetime").date.today().isoformat(),
    )
    try:
        llm_output = await call_ollama(system, request)
    except Exception as exc:
        return {"type": "error", "message": f"LLM error: {exc}"}

    log.info("[parser] LLM output: %s", llm_output)

    if not llm_output:
        return {"type": "error", "message": "LLM returned empty response. Please try again."}

    missing_airports, missing_passengers, cli_line = [], [], None
    for line in llm_output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("MISSING_AIRPORT:"):
            missing_airports.append(line.split(":", 1)[1].strip().upper())
        elif line.startswith("MISSING_PAX:"):
            missing_passengers.append(line.split(":", 1)[1].strip())
        elif line.startswith("CLARIFY:"):
            return {"type": "clarify", "question": line.split(":", 1)[1].strip()}
        elif "flight_briefing.py" in line and "|" not in line and "$" not in line:
            cli_line = line

    if missing_airports or missing_passengers:
        return {"type": "missing", "airports": missing_airports, "passengers": missing_passengers}
    if cli_line:
        return {"type": "cli", "line": cli_line}
    return {"type": "error", "message": "Couldn't parse request. Try: Briefing EDFE pattern 1h solo"}


# ── Core briefing flow ────────────────────────────────────────────────────────

def is_briefing_trigger(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in TRIGGER_PHRASES)

def is_admin_trigger(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in ADMIN_PHRASES)

def _detect_aircraft(text: str) -> Optional[str]:
    lower = text.lower()
    for keyword, code in AIRCRAFT_KEYWORDS.items():
        if keyword in lower:
            return code
    return None

def _detect_pilot(text: str) -> Optional[str]:
    """Extract pilot name from patterns like 'with X as pilot' or 'X as pilot'."""
    m = re.search(r'\bwith\s+(\w+)\s+as\s+pilot\b|\b(\w+)\s+as\s+pilot\b', text, re.IGNORECASE)
    if not m:
        return None
    name = next(g for g in m.groups() if g is not None)
    return name.capitalize()

def _resolve_airport_names(text: str) -> str:
    """Replace known airport display names in the message with their ICAO codes."""
    with db_connect() as conn:
        try:
            rows = conn.execute("""
                SELECT ap.icao, COALESCE(ap.name, ag.name) AS name
                FROM airport_profiles ap
                LEFT JOIN airports_geo ag ON ag.icao = ap.icao
                WHERE COALESCE(ap.name, ag.name) IS NOT NULL
                  AND COALESCE(ap.name, ag.name) != ''
            """).fetchall()
        except Exception:
            return text
    result = text
    for row in rows:
        icao, name = row["icao"], row["name"]
        if name and len(name) >= 3:
            result = re.sub(r'\b' + re.escape(name) + r'\b', icao, result, flags=re.IGNORECASE)
    return result

def _extract_aircraft_name(cli_line: str) -> str:
    m = re.search(r'--aircraft\s+(\S+)', cli_line)
    if not m:
        return ""
    code = m.group(1)
    return next((name for c, name in AIRCRAFT_CHOICES if c == code), code)

def _aircraft_menu() -> str:
    lines = "\n".join(f"  {i + 1} - {name}" for i, (_, name) in enumerate(AIRCRAFT_CHOICES))
    return f"Which aircraft?\n{lines}"

async def handle_briefing_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state.accumulated_request = text
    state.clarify_rounds = 0

    state.pilot_name = _detect_pilot(text) or ""

    detected = _detect_aircraft(text)
    if detected:
        state.aircraft_type = detected
        await _run_parse_loop(update, context, state)
    else:
        state.status = "awaiting_aircraft"
        await update.message.reply_text(_aircraft_menu())

async def handle_aircraft_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    lower = text.strip().lower()

    detected: Optional[str] = None
    # Accept a number like "1" or "2"
    if lower.isdigit():
        idx = int(lower) - 1
        if 0 <= idx < len(AIRCRAFT_CHOICES):
            detected = AIRCRAFT_CHOICES[idx][0]
    if detected is None:
        detected = _detect_aircraft(text)

    if not detected:
        await update.message.reply_text(
            f"Sorry, I didn't recognise that. Please reply with a number:\n{_aircraft_menu()}"
        )
        return

    state.aircraft_type = detected
    state.status = "idle"
    await _run_parse_loop(update, context, state)

async def _run_parse_loop(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: ConversationState
) -> None:
    """
    Core loop: parse → act on result → ask if needed → repeat.
    Keeps asking until we have a valid CLI command or give up.
    """
    chat_id = update.effective_chat.id
    await update.message.reply_text("Parsing your request...")

    log.info("[loop] round=%d request=%s", state.clarify_rounds, state.accumulated_request[:120])
    result = await parse_request(state.accumulated_request)
    log.info("[loop] result type=%s", result["type"])

    if result["type"] == "cli":
        # All parameters gathered — inject aircraft and pilot overrides, then execute
        cli_line = result["line"]
        if state.aircraft_type and "--aircraft" not in cli_line:
            cli_line += f" --aircraft {state.aircraft_type}"
        if state.pilot_name and "--pilot" not in cli_line:
            cli_line += f" --pilot {state.pilot_name}"
        await execute_and_reply(update, context, cli_line)

    elif result["type"] == "clarify":
        # Missing flight parameter (duration, type, etc.)
        if state.clarify_rounds >= MAX_CLARIFY_ROUNDS:
            reset_state(chat_id)
            await update.message.reply_text(
                "I'm having trouble understanding the request after several attempts. "
                "Please start over with a clearer format, e.g.:\n"
                "Briefing EDFE pattern 1h solo"
            )
            return
        state.status = "awaiting_clarify"
        state.clarify_rounds += 1
        await update.message.reply_text(result["question"])

    elif result["type"] == "missing":
        # Missing DB data (airport or passenger)
        state.status = "awaiting_data"
        state.missing_airports  = result["airports"]
        state.missing_passengers = result["passengers"]
        prompts = load_prompts()
        await send_db_clarification(update, context, state, prompts)

    else:
        reset_state(chat_id)
        await update.message.reply_text(result["message"])


async def handle_clarify_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """User answered a flight parameter question — append and re-parse."""
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    # Merge the answer into the accumulated request
    state.accumulated_request = f"{state.accumulated_request}, {text}"
    state.status = "idle"
    log.info("Accumulated request: %s", state.accumulated_request)
    await _run_parse_loop(update, context, state)


async def send_db_clarification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: ConversationState,
    prompts: dict,
) -> None:
    """Send the admin URL so the user can add missing data via the web UI."""
    admin_url = WEBSERVER_URL.replace("/briefing", "/admin")
    parts = []
    if state.missing_airports:
        parts.append(f"Airport(s) not in database: {', '.join(state.missing_airports)}")
    if state.missing_passengers:
        parts.append(f"Passenger(s) not in database: {', '.join(state.missing_passengers)}")

    msg = (
        "Some data is missing from the database.\n\n"
        + "\n".join(parts)
        + f"\n\nPlease add them here:\n{admin_url}\n\n"
        "When done, reply: done"
    )
    await update.message.reply_text(msg)


async def handle_data_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """User signals they have added the missing data via the admin UI."""
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    # Check if the user is saying they are done
    if text.lower().strip() in DONE_PHRASES:
        await update.message.reply_text("Great, generating your briefing now...")
        state.status = "idle"
        state.missing_airports = []
        state.missing_passengers = []
        await _run_parse_loop(update, context, state)
        return

    # Otherwise remind them to use the admin UI
    admin_url = WEBSERVER_URL.replace("/briefing", "/admin")
    await update.message.reply_text(
        f"Please add the missing data at:\n{admin_url}\n\nThen reply: done"
    )


async def execute_and_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, cli_line: str
) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text("Generating briefing, please wait...")

    success, output = await run_briefing(cli_line)
    reset_state(chat_id)

    if success:
        route = _extract_route(cli_line)
        aircraft_label = _extract_aircraft_name(cli_line)
        details = f"Route: {route}\nAircraft: {aircraft_label or 'default'}"
        m_pilot = re.search(r'--pilot\s+(\S+)', cli_line)
        if m_pilot:
            details += f"\nPilot: {m_pilot.group(1)}"
        await update.message.reply_text(
            f"Your briefing is ready:\n{WEBSERVER_URL}\n\n{details}"
        )
    else:
        err = output[-1500:] if len(output) > 1500 else output
        await update.message.reply_text(f"Briefing generation failed.\n\n{err}")


def _extract_route(cli_line: str) -> str:
    icaos = re.findall(r"\b[A-Z]{4}\b", cli_line)
    return " → ".join(icaos) if icaos else cli_line[:60]


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    state = get_state(update.effective_chat.id)

    if state.status == "awaiting_aircraft":
        await handle_aircraft_reply(update, context, text)
        return

    if state.status == "awaiting_data":
        await handle_data_reply(update, context, text)
        return

    if state.status == "awaiting_clarify":
        await handle_clarify_reply(update, context, text)
        return

    if is_admin_trigger(text):
        await on_admin(update, context)
        return

    if is_briefing_trigger(text):
        await handle_briefing_request(update, context, text)
        return

    log.debug("Ignoring non-briefing message: %s", text[:80])


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    aircraft_list = "\n".join(f"  • {name}" for _, name in AIRCRAFT_CHOICES)
    await update.message.reply_text(
        "✈️  <b>Flight Briefing Bot</b>\n\n"
        "/help - All commands with descriptions\n"
        "/airport - Manage airports in the database\n"
        "/pilots - Manage pilots &amp; passengers\n"
        "/aircraft - Manage aircraft types\n"
        "/cancel - Abort the current request\n\n"
        "<b>Create a briefing</b> — tap an example to copy:\n\n"
        "<code>Briefing Cavalon EDFE pattern 1h solo</code>\n"
        "<code>Briefing Aquila EDFE EDFM 2h with Gabi</code>\n"
        "<code>Briefing EDFZ EDFE 3h cross country with Wolfgang as pilot</code>\n"
        "<code>Briefing EDFE pattern 45min 5kg baggage</code>\n\n"
        "<b>Aircraft</b>\n"
        f"{aircraft_list}\n\n"
        "<b>Tips</b>\n"
        "Name a passenger to include them — <i>with Gabi</i>\n"
        "Say <i>solo</i> or <i>no passenger</i> to fly alone\n"
        "Add <i>with X as pilot</i> to change the pilot\n"
        "Airport names saved in /airport are auto-recognised",
        parse_mode="HTML",
    )

async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await on_help(update, context)

async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    state = get_state(update.effective_chat.id)
    pax = get_known_passengers().replace("\n", ", ")
    airports = get_known_airports()
    await update.message.reply_text(
        f"State: {state.status} (clarify rounds: {state.clarify_rounds})\n"
        f"Accumulated: {state.accumulated_request or '—'}\n\n"
        f"Passengers: {pax}\n"
        f"Airports: {airports}"
    )

async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    reset_state(update.effective_chat.id)
    await update.message.reply_text("Cancelled. Ready for a new briefing request.")

async def on_airport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    admin_url = WEBSERVER_URL.replace("/briefing", "/admin")
    await update.message.reply_text(
        f"Manage airports (ICAO, elevation, runways, name alias):\n"
        f'<a href="{admin_url}#sec-airports">{admin_url}#sec-airports</a>',
        parse_mode="HTML",
    )

async def on_pilots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    admin_url = WEBSERVER_URL.replace("/briefing", "/admin")
    await update.message.reply_text(
        f"Manage pilots &amp; passengers (name, weight, height):\n"
        f'<a href="{admin_url}#sec-passengers">{admin_url}#sec-passengers</a>',
        parse_mode="HTML",
    )

async def on_aircraft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    admin_url = WEBSERVER_URL.replace("/briefing", "/admin")
    await update.message.reply_text(
        f"Manage aircraft types (create, edit, delete):\n"
        f'<a href="{admin_url}#sec-aircraft">{admin_url}#sec-aircraft</a>',
        parse_mode="HTML",
    )

async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    admin_url = WEBSERVER_URL.replace("/briefing", "/admin")
    await update.message.reply_text(
        f"Manage airports and passengers here:\n{admin_url}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Starting flight briefing bot (model: %s, ollama: %s)", OLLAMA_MODEL, OLLAMA_URL)
    reload_aircraft_from_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    on_start))
    app.add_handler(CommandHandler("help",     on_help))
    app.add_handler(CommandHandler("airport",  on_airport))
    app.add_handler(CommandHandler("pilots",   on_pilots))
    app.add_handler(CommandHandler("aircraft", on_aircraft))
    app.add_handler(CommandHandler("status",   on_status))
    app.add_handler(CommandHandler("cancel",   on_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    async def post_init(application):
        reload_aircraft_from_db()
        await application.bot.set_my_commands([
            ("help",     "Show help and examples"),
            ("airport",  "Manage airports"),
            ("pilots",   "Manage pilots & passengers"),
            ("aircraft", "Manage aircraft types"),
            ("cancel",   "Cancel current request"),
        ])

    app.post_init = post_init
    log.info("Bot polling started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
