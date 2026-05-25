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

class CgPoint(BaseModel):
    mass_kg: float
    cg_mm: float

class AircraftModel(BaseModel):
    code: str
    name: str
    type: str  # fixed_wing | gyro_side_by_side | gyro_tandem
    aliases: Optional[str] = None
    empty_weight: float
    empty_lever: float = 0.0
    fuel_start_roll: float = 0.0
    fuel_climb: float = 0.0
    fuel_cruise_per_hour: float
    fuel_reserve: float
    fuel_density: float = 0.72
    fuel_lever: float = 0.0
    baggage_lever: float = 0.0
    max_passengers: int = 1
    fuel_capacity_liters: Optional[float] = None
    cg_line_min: Optional[float] = None
    cg_line_max: Optional[float] = None
    mtow: Optional[float] = None
    max_seat_weight: Optional[float] = None
    max_aft_seat_weight: Optional[float] = None
    max_cockpit_weight: Optional[float] = None
    min_cockpit_weight: Optional[float] = None
    max_storage_weight: Optional[float] = None
    max_baggage_weight: Optional[float] = None
    min_front_seat_weight: Optional[float] = None
    nose_penalty_factor: Optional[float] = None
    cg_envelope: List[CgPoint] = []

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
            "SELECT ap.icao, ap.elevation_ft, COALESCE(ap.name, ag.name) AS name, ag.lat, ag.lon "
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
            "INSERT INTO airport_profiles (icao, elevation_ft, name) VALUES (?,?,?) "
            "ON CONFLICT(icao) DO UPDATE SET elevation_ft=excluded.elevation_ft, "
            "name=COALESCE(excluded.name, airport_profiles.name)",
            (icao, a.elevation_ft, a.name)
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


# ── Aircraft ──────────────────────────────────────────────────────────────────

_AIRCRAFT_COLS = [
    "code","name","type","aliases","empty_weight","empty_lever",
    "fuel_start_roll","fuel_climb","fuel_cruise_per_hour","fuel_reserve",
    "fuel_density","fuel_lever","baggage_lever","max_passengers",
    "fuel_capacity_liters","cg_line_min","cg_line_max",
    "mtow","max_seat_weight","max_aft_seat_weight","max_cockpit_weight",
    "min_cockpit_weight","max_storage_weight","max_baggage_weight",
    "min_front_seat_weight","nose_penalty_factor",
]

@app.get("/aircraft")
def list_aircraft():
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {','.join(_AIRCRAFT_COLS)} FROM aircraft ORDER BY name"
        ).fetchall()
        result = []
        for row in rows:
            ac = dict(row)
            pts = conn.execute(
                "SELECT mass_kg, cg_mm FROM aircraft_cg_envelope "
                "WHERE aircraft_code=? ORDER BY sort_order",
                (ac["code"],)
            ).fetchall()
            ac["cg_envelope"] = [dict(p) for p in pts]
            result.append(ac)
    return result

@app.post("/aircraft")
def upsert_aircraft(ac: AircraftModel):
    code = ac.code.strip().lower().replace(" ", "_")
    vals = (
        code, ac.name, ac.type, ac.aliases,
        ac.empty_weight, ac.empty_lever, ac.fuel_start_roll, ac.fuel_climb,
        ac.fuel_cruise_per_hour, ac.fuel_reserve, ac.fuel_density,
        ac.fuel_lever, ac.baggage_lever, ac.max_passengers,
        ac.fuel_capacity_liters, ac.cg_line_min, ac.cg_line_max,
        ac.mtow, ac.max_seat_weight, ac.max_aft_seat_weight,
        ac.max_cockpit_weight, ac.min_cockpit_weight,
        ac.max_storage_weight, ac.max_baggage_weight,
        ac.min_front_seat_weight, ac.nose_penalty_factor,
    )
    placeholders = ",".join(["?"] * len(_AIRCRAFT_COLS))
    updates = ",".join(f"{c}=excluded.{c}" for c in _AIRCRAFT_COLS if c != "code")
    with get_db() as conn:
        conn.execute(
            f"INSERT INTO aircraft ({','.join(_AIRCRAFT_COLS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(code) DO UPDATE SET {updates}",
            vals,
        )
        conn.execute("DELETE FROM aircraft_cg_envelope WHERE aircraft_code=?", (code,))
        if ac.cg_envelope:
            conn.executemany(
                "INSERT INTO aircraft_cg_envelope (aircraft_code, sort_order, mass_kg, cg_mm) VALUES (?,?,?,?)",
                [(code, i, p.mass_kg, p.cg_mm) for i, p in enumerate(ac.cg_envelope)],
            )
    return {"ok": True}

@app.delete("/aircraft/{code}")
def delete_aircraft(code: str):
    code = code.strip().lower()
    with get_db() as conn:
        conn.execute("DELETE FROM aircraft_cg_envelope WHERE aircraft_code=?", (code,))
        conn.execute("DELETE FROM aircraft WHERE code=?", (code,))
    return {"ok": True}
