"""
init_db.py
----------
Runs at runner container startup. Applies seed.sql if the DB is empty.
Also copies prompts.json to /data if not already present.
Safe to run multiple times (INSERT OR IGNORE everywhere).
"""
import shutil
import sqlite3
import sys
from pathlib import Path

DB_PATH      = Path("/data/flight_briefing.db")
SEED_PATH    = Path("/app/seed.sql")
PROMPTS_SRC  = Path("/app/prompts.json")
PROMPTS_DEST = Path("/data/prompts.json")


def init_db() -> None:
    if not SEED_PATH.exists():
        print(f"[init_db] seed.sql not found at {SEED_PATH}, skipping.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        # Migration: add name column before seed runs (seed may reference it)
        try:
            conn.execute("ALTER TABLE airport_profiles ADD COLUMN name TEXT")
            conn.commit()
            print("[init_db] Migration: added name column to airport_profiles")
        except Exception:
            pass  # column already exists

        sql = SEED_PATH.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.commit()

        # Set known airport names where not yet populated
        known_names = {"EDRF": "Mainbullau"}
        for icao, name in known_names.items():
            conn.execute(
                "UPDATE airport_profiles SET name = ? WHERE icao = ? AND (name IS NULL OR name = '')",
                (name, icao),
            )
        conn.commit()

        # Migration: add Wolfgang to passengers (safe to re-run via INSERT OR IGNORE)
        conn.execute(
            "INSERT OR IGNORE INTO passengers (name, weight, height) VALUES ('Wolfgang', 70.0, 180.0)"
        )
        conn.commit()

        pilot_count = conn.execute("SELECT COUNT(*) FROM pilot").fetchone()[0]
        pax_count   = conn.execute("SELECT COUNT(*) FROM passengers").fetchone()[0]
        ap_count    = conn.execute("SELECT COUNT(*) FROM airport_profiles").fetchone()[0]
        rw_count    = conn.execute("SELECT COUNT(*) FROM airport_runways").fetchone()[0]
        print(f"[init_db] DB ready: {pilot_count} pilot, {pax_count} passengers, "
              f"{ap_count} airports, {rw_count} runways")
    finally:
        conn.close()


def init_prompts() -> None:
    if PROMPTS_DEST.exists():
        print(f"[init_db] prompts.json already at {PROMPTS_DEST}, skipping.")
        return
    if PROMPTS_SRC.exists():
        shutil.copy(PROMPTS_SRC, PROMPTS_DEST)
        print(f"[init_db] Copied prompts.json to {PROMPTS_DEST}")
    else:
        print(f"[init_db] prompts.json not found at {PROMPTS_SRC}, skipping.")


if __name__ == "__main__":
    print(f"[init_db] Initialising DB at {DB_PATH}")
    init_db()
    init_prompts()
    print("[init_db] Done.")
