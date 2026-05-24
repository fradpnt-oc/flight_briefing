"""
Admin API — manages airports and passengers in the briefing DB.
Runs on port 8080, mounted at /api by nginx.
"""
import os
import sqlite3
from pathlib import Path
from typing import List, Optional
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = Path(os.environ.get("DB_PATH", "/data/flight_briefing.db"))

app = FastAPI(title="Briefing Admin API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Models ────────────────────────────────────────────────────────────────────

class Passenger(BaseModel):
    name: str
    weight: float
    height: float

class Runway(BaseModel):
    runway: str
    surface: str
    tora: float
    lda: float

class Airport(BaseModel):
    icao: str
    elevation_ft: float
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    runways: List[Runway] = []


# ── Passengers ────────────────────────────────────────────────────────────────

@app.get("/passengers")
def list_passengers():
    with get_db() as conn:
        rows = conn.execute("SELECT name, weight, height FROM passengers ORDER BY name").fetchall()
    return [dict(r) for r in rows]

@app.post("/passengers")
def upsert_passenger(p: Passenger):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO passengers (name, weight, height) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET weight=excluded.weight, height=excluded.height",
            (p.name, p.weight, p.height)
        )
    return {"ok": True}

@app.delete("/passengers/{name}")
def delete_passenger(name: str):
    with get_db() as conn:
        conn.execute("DELETE FROM passengers WHERE name=?", (name,))
    return {"ok": True}


# ── Airports ──────────────────────────────────────────────────────────────────

@app.get("/airports")
def list_airports():
    with get_db() as conn:
        airports = conn.execute(
            "SELECT ap.icao, ap.elevation_ft, ag.name, ag.lat, ag.lon "
            "FROM airport_profiles ap "
            "LEFT JOIN airports_geo ag ON ag.icao = ap.icao "
            "ORDER BY ap.icao"
        ).fetchall()
        result = []
        for a in airports:
            runways = conn.execute(
                "SELECT runway, surface, tora, lda FROM airport_runways WHERE icao=? ORDER BY runway",
                (a["icao"],)
            ).fetchall()
            result.append({**dict(a), "runways": [dict(r) for r in runways]})
    return result

@app.post("/airports")
def upsert_airport(a: Airport):
    icao = a.icao.upper()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO airport_profiles (icao, elevation_ft) VALUES (?,?) "
            "ON CONFLICT(icao) DO UPDATE SET elevation_ft=excluded.elevation_ft",
            (icao, a.elevation_ft)
        )
        if a.lat is not None and a.lon is not None:
            conn.execute(
                "INSERT INTO airports_geo (icao, name, lat, lon, elevation_ft) VALUES (?,?,?,?,?) "
                "ON CONFLICT(icao) DO UPDATE SET name=excluded.name, lat=excluded.lat, "
                "lon=excluded.lon, elevation_ft=excluded.elevation_ft",
                (icao, a.name or icao, a.lat, a.lon, a.elevation_ft)
            )
        if a.runways:
            conn.execute("DELETE FROM airport_runways WHERE icao=?", (icao,))
            conn.executemany(
                "INSERT INTO airport_runways (icao, runway, surface, tora, lda) VALUES (?,?,?,?,?)",
                [(icao, r.runway, r.surface, r.tora, r.lda) for r in a.runways]
            )
    return {"ok": True}

@app.delete("/airports/{icao}")
def delete_airport(icao: str):
    icao = icao.upper()
    with get_db() as conn:
        conn.execute("DELETE FROM airport_runways WHERE icao=?", (icao,))
        conn.execute("DELETE FROM airports_geo WHERE icao=?", (icao,))
        conn.execute("DELETE FROM airport_profiles WHERE icao=?", (icao,))
    return {"ok": True}

@app.get("/health")
def health():
    return {"ok": True}
