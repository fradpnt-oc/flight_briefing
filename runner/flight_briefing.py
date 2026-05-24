#!/usr/bin/env python3
"""
Deterministic flight briefing tool.
Collects user inputs, fetches public weather data (METAR/TAF), stores pilot and
passenger profiles in sqlite, and generates the standardized HTML briefing that
can be reused for each request.
"""
from __future__ import annotations
import os

import argparse
import csv
import json
import re
import sqlite3
import threading
import time as _time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent
# All persistent data lives on the shared Docker volume at /data
DB_PATH       = Path(os.environ.get("DB_PATH", "/data/flight_briefing.db"))
TEMPLATE_PATH = BASE_DIR / "briefing_template.html"
OUTPUT_HTML   = Path("/data/current_briefing.html")   # served by nginx on :3300

# GitHub Pages removed — briefing is served locally via nginx
GHPAGES_HTML  = None   # kept as None so references below are safe

# airports.csv / OurAirports removed — geo data in airports_geo SQLite table only
DATA_DIR     = Path("/data")
AIRPORTS_CSV = None
AIRPORTS_URL = None

API_TIMEOUT = 12                           # seconds per attempt (up from 8)
HTTP_MAX_RETRIES = 3                       # attempts before giving up
HTTP_CONCURRENCY = 3                       # max simultaneous requests (avoids burst drops)
METAR_API = "https://aviationweather.gov/api/data/metar"
TAF_API = "https://aviationweather.gov/api/data/taf"

# Shared HTTP session for connection reuse
_http_session: Optional[requests.Session] = None
_http_semaphore = threading.Semaphore(HTTP_CONCURRENCY)

def _session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
    return _http_session

def _http_get(url: str, params: Dict[str, Any]) -> requests.Response:
    """HTTP GET with concurrency cap + exponential-backoff retry."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(HTTP_MAX_RETRIES):
        try:
            with _http_semaphore:
                return _session().get(url, params=params, timeout=API_TIMEOUT)
        except Exception as exc:
            last_exc = exc
            if attempt < HTTP_MAX_RETRIES - 1:
                _time_module.sleep(1.0 * (2 ** attempt))   # 1 s → 2 s
    raise last_exc

@dataclass(frozen=True)
class AircraftConfig:
    code: str
    name: str
    empty_weight: float
    empty_lever: float
    fuel_start_roll: float
    fuel_climb: float
    fuel_cruise_per_hour: float
    fuel_reserve: float
    fuel_density: float
    fuel_lever: float
    baggage_lever: float
    max_passengers: Optional[int]
    cg_envelope: Optional[List[Tuple[float, float]]] = None
    cg_line_min: Optional[float] = None
    cg_line_max: Optional[float] = None
    # Gyrocopter station-limit fields (None = not a gyrocopter)
    mtow: Optional[float] = None
    max_seat_weight: Optional[float] = None
    max_cockpit_weight: Optional[float] = None
    min_cockpit_weight: Optional[float] = None
    max_storage_weight: Optional[float] = None
    fuel_capacity_liters: Optional[float] = None


AIRCRAFT_TYPES: Dict[str, AircraftConfig] = {
    "aquila_a211": AircraftConfig(
        code="aquila_a211",
        name="Aquila A211",
        empty_weight=514.5,
        empty_lever=0.439,
        fuel_start_roll=3.0,
        fuel_climb=5.0,
        fuel_cruise_per_hour=22.0,
        fuel_reserve=11.0,
        fuel_density=0.72,
        fuel_lever=0.325,
        baggage_lever=1.3,
        max_passengers=1,
        cg_envelope=[
            (560, 240),
            (560, 290),
            (750, 390),
            (750, 320),
        ],
        cg_line_min=0.427,
        cg_line_max=0.515,
    ),
    "cavalon_912_914": AircraftConfig(
        code="cavalon_912_914",
        name="AutoGyro Cavalon 912/914",
        empty_weight=290.0,
        empty_lever=0.0,
        fuel_start_roll=3.0,
        fuel_climb=5.0,
        fuel_cruise_per_hour=20.0,
        fuel_reserve=10.0,
        fuel_density=0.72,
        fuel_lever=0.0,
        baggage_lever=0.0,
        max_passengers=1,
        mtow=560.0,
        max_seat_weight=110.0,
        max_cockpit_weight=200.0,
        min_cockpit_weight=65.0,
        max_storage_weight=10.0,
        fuel_capacity_liters=98.0,
    ),
}

DEFAULT_AIRCRAFT_TYPE = "aquila_a211"


def get_aircraft_config(code: str) -> AircraftConfig:
    key = (code or DEFAULT_AIRCRAFT_TYPE).lower()
    if key not in AIRCRAFT_TYPES:
        raise ValueError(f"Unknown aircraft type: {code}")
    return AIRCRAFT_TYPES[key]

_airport_cache: Optional[Dict[str, Dict[str, Any]]] = None
_airport_db_ready: bool = False


@dataclass
class Passenger:
    name: str
    weight: float
    height: float


@dataclass
class RunwayInfo:
    runway: str
    surface: str
    tora: float
    lda: float


@dataclass
class AirportSegment:
    icao: str
    name: str
    elevation_ft: float
    runways: List[RunwayInfo]


@dataclass
class WindInfo:
    direction: Optional[float]
    speed_kt: Optional[float]
    gust_kt: Optional[float]
    variable: bool
    var_from: Optional[float]
    var_to: Optional[float]


@dataclass
class FlightInputs:
    departure_icao: str
    airport_name: str
    airport_elevation_ft: float
    runways: List[RunwayInfo]
    airports: List[AirportSegment]
    flight_type: str
    estimated_time_hours: float
    stopovers: List[str]
    passengers: List[Passenger]
    baggage_weight: float
    aircraft_type: str
    aircraft_name: str
    pilot_name: str
    pilot_weight: float
    pilot_height: float


@dataclass
class WeatherData:
    metars: List[Dict[str, Any]]
    taf: List[Dict[str, Any]]
    lowest_qnh: Optional[float]
    oat_c: Optional[float]
    wind: Optional[WindInfo]
    weather_summary: str
    nearby_airports: List[Dict[str, Any]]
    metar_entries: List[Dict[str, Any]]


@dataclass
class BriefingData:
    inputs: FlightInputs
    weather: WeatherData
    fuel: Dict[str, Any]
    mass_balance: Dict[str, Any]
    performance: Dict[str, Any]
    warnings: List[str]
    airport_sections: List[Dict[str, Any]]
    generation_time: str


def prompt(text: str, validator=None) -> str:
    while True:
        value = input(text).strip()
        if not value:
            print("Value required.")
            continue
        if validator:
            try:
                validator(value)
            except ValueError as exc:
                print(exc)
                continue
        return value


def prompt_aircraft_type(default_code: str = DEFAULT_AIRCRAFT_TYPE) -> str:
    print("Available aircraft profiles:")
    for key, cfg in AIRCRAFT_TYPES.items():
        seats = "unlimited" if cfg.max_passengers is None else str(cfg.max_passengers)
        print(f"  - {key}: {cfg.name} (passenger seats: {seats})")
    while True:
        choice = input(f"Select aircraft [{default_code}]: ").strip().lower()
        if not choice:
            return default_code
        if choice in AIRCRAFT_TYPES:
            return choice
        print("Unknown aircraft type. Try again.")


def ensure_template() -> None:
    TEMPLATE_PATH.write_text(DEFAULT_TEMPLATE, encoding="utf-8")


def ensure_airports_data() -> None:
    """No-op: airports.csv removed. Geo data lives in airports_geo SQLite table."""
    pass


def load_airports() -> Dict[str, Dict[str, Any]]:
    """Load airport geo data from airports_geo SQLite table (replaces CSV)."""
    global _airport_cache
    if _airport_cache is not None:
        return _airport_cache
    airports: Dict[str, Dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT icao, name, lat, lon, elevation_ft FROM airports_geo"
        ).fetchall()
        conn.close()
        for icao, name, lat, lon, elev in rows:
            airports[icao.upper()] = {
                "ident": icao.upper(),
                "name": name or "",
                "lat": lat,
                "lon": lon,
                "elevation_ft": elev,
            }
    except Exception:
        pass  # table may not exist yet; bbox fallback handles it
    _airport_cache = airports
    return airports


def get_airport(icao: str) -> Optional[Dict[str, Any]]:
    return load_airports().get(icao.upper())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return r * c


# ---------------------------------------------------------------------------
# Fast airport geo DB (SQLite-backed, populated once from CSV)
# ---------------------------------------------------------------------------

def ensure_airport_geo_db(conn: sqlite3.Connection) -> None:
    """Create airports_geo table and indexes if absent. No CSV loading."""
    global _airport_db_ready
    if _airport_db_ready:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS airports_geo (
            icao TEXT PRIMARY KEY,
            name TEXT,
            lat  REAL NOT NULL,
            lon  REAL NOT NULL,
            elevation_ft REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ag_lat ON airports_geo(lat)")
    conn.execute("CREATE INDEX IF NOT EXISTS ag_lon ON airports_geo(lon)")
    conn.commit()
    _airport_db_ready = True


def get_airport_latlon(conn: sqlite3.Connection, icao: str) -> Optional[Tuple[float, float]]:
    ensure_airport_geo_db(conn)
    row = conn.execute("SELECT lat, lon FROM airports_geo WHERE icao=?",
                       (icao.upper(),)).fetchone()
    return (row[0], row[1]) if row else None


def bbox_for_radius(lat: float, lon: float, radius_km: float = 50.0) -> Tuple[float, float, float, float]:
    """Return (min_lat, min_lon, max_lat, max_lon) for a circle approximation."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * cos(radians(lat)) + 1e-9)
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def find_nearby_airports_db(conn: sqlite3.Connection, icao: str,
                             radius_km: float = 50.0) -> List[Dict[str, Any]]:
    """Fast bounding-box lookup from SQLite (replaces O(n) in-memory scan)."""
    ensure_airport_geo_db(conn)
    home = conn.execute("SELECT lat, lon, name FROM airports_geo WHERE icao=?",
                        (icao.upper(),)).fetchone()
    if not home:
        return [{"ident": icao.upper(), "name": "", "distance_km": 0.0}]
    home_lat, home_lon, _ = home
    min_lat, min_lon, max_lat, max_lon = bbox_for_radius(home_lat, home_lon, radius_km)
    rows = conn.execute(
        "SELECT icao, name, lat, lon FROM airports_geo WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()
    nearby: List[Dict[str, Any]] = []
    for ident, name, lat, lon in rows:
        dist = haversine_km(home_lat, home_lon, lat, lon)
        if dist <= radius_km:
            nearby.append({"ident": ident, "name": name or "", "distance_km": round(dist, 1)})
    nearby.sort(key=lambda x: x["distance_km"])
    return nearby


def find_nearby_airports(icao: str, radius_km: float = 50.0) -> List[Dict[str, Any]]:
    """Legacy fallback (in-memory). Prefer find_nearby_airports_db when conn available."""
    airports = load_airports()
    home = airports.get(icao.upper())
    if not home:
        return [{"ident": icao.upper(), "name": "", "distance_km": 0.0}]
    nearby: List[Dict[str, Any]] = []
    for info in airports.values():
        lat = info.get("lat")
        lon = info.get("lon")
        if lat is None or lon is None:
            continue
        distance = haversine_km(home["lat"], home["lon"], lat, lon)
        if distance <= radius_km:
            nearby.append({
                "ident": info["ident"],
                "name": info.get("name", ""),
                "distance_km": round(distance, 1),
            })
    nearby.sort(key=lambda item: item["distance_km"])
    return nearby


def chunk_list(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parse_json_list(resp: requests.Response) -> List[Dict[str, Any]]:
    try:
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data", [])
    except Exception:
        pass
    return []


def fetch_metars_for_ids(icaos: List[str]) -> List[Dict[str, Any]]:
    if not icaos:
        return []
    station_list = sorted(set(filter(None, (c.strip().upper() for c in icaos))))
    if not station_list:
        return []
    return _parse_json_list(_http_get(METAR_API, {"ids": ",".join(station_list), "format": "json"}))


def fetch_tafs_for_ids(icaos: List[str]) -> List[Dict[str, Any]]:
    if not icaos:
        return []
    station_list = sorted(set(filter(None, (c.strip().upper() for c in icaos))))
    if not station_list:
        return []
    return _parse_json_list(_http_get(TAF_API, {"ids": ",".join(station_list), "format": "json"}))


def fetch_metars_bbox(bbox: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    """Fetch all METARs within a bounding box (min_lat,min_lon,max_lat,max_lon)."""
    bbox_str = f"{bbox[0]:.3f},{bbox[1]:.3f},{bbox[2]:.3f},{bbox[3]:.3f}"
    return _parse_json_list(_http_get(METAR_API, {"bbox": bbox_str, "format": "json"}))


def fetch_metar_taf_concurrent(
    metar_params: Dict[str, Any], taf_params: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch METAR and TAF in parallel, returning (metars, tafs)."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        fm = ex.submit(_http_get, METAR_API, metar_params)
        ft = ex.submit(_http_get, TAF_API, taf_params)
        try:
            metars = _parse_json_list(fm.result())
        except Exception:
            metars = []
        try:
            tafs = _parse_json_list(ft.result())
        except Exception:
            tafs = []
    return metars, tafs


QNH_MIN_HPA = 850
QNH_MAX_HPA = 1100

def parse_temperature_from_raw(raw: str) -> Optional[float]:
    if not raw:
        return None
    match = re.search(r"\b(M?\d{2})/(M?\d{2})\b", raw)
    if not match:
        return None
    token = match.group(1)
    negative = token.startswith("M")
    value = int(token[1:] if negative else token)
    return float(-value if negative else value)

def parse_qnh_entries(raw: str) -> List[Dict[str, Optional[float]]]:
    if not raw:
        return []
    entries: List[Dict[str, Optional[float]]] = []
    temp = parse_temperature_from_raw(raw)
    for match in re.findall(r"\bQ(\d{4})\b", raw):
        hpa = float(match)
        if QNH_MIN_HPA <= hpa <= QNH_MAX_HPA:
            entries.append({"qnh": hpa, "temp": temp, "raw": raw})
    for match in re.findall(r"\bA(\d{4})\b", raw):
        inhg = float(match) / 100.0
        hpa = inhg * 33.8639
        if QNH_MIN_HPA <= hpa <= QNH_MAX_HPA:
            entries.append({"qnh": round(hpa, 1), "temp": temp, "raw": raw})
    return entries

def parse_lowest_qnh_and_temp(metars: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    entries: List[Dict[str, Optional[float]]] = []
    for metar in metars:
        raw = metar.get("rawOb") or metar.get("raw_text") or ""
        entries.extend(parse_qnh_entries(raw))
    if not entries:
        return None, None, None
    best = min(entries, key=lambda item: item["qnh"])
    return best["qnh"], best.get("temp"), best.get("raw")


def extract_station_id(metar: Dict[str, Any]) -> str:
    return (
        metar.get("icaoId")
        or metar.get("stationId")
        or metar.get("station")
        or (metar.get("rawOb") or metar.get("raw_text") or "")[:4]
        or ""
    ).upper()


def extract_qnh_from_metar(metar: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    raw = metar.get("rawOb") or metar.get("raw_text") or ""
    entries = parse_qnh_entries(raw)
    if entries:
        primary = entries[0]
        return primary.get("qnh"), primary.get("temp"), raw
    return None, parse_temperature_from_raw(raw), raw if raw else None


def extract_oat(metars: List[Dict[str, Any]]) -> Optional[float]:
    for metar in metars:
        temp = metar.get("temp_c")
        if temp is not None:
            return float(temp)
    return None


def parse_wind_from_raw(raw: str) -> Optional[WindInfo]:
    if not raw:
        return None
    match = re.search(r"\b(?P<dir>\d{3}|VRB)(?P<speed>\d{2,3})(G(?P<gust>\d{2,3}))?KT\b", raw)
    if not match:
        return None
    dir_token = match.group("dir")
    direction = None if dir_token == "VRB" else float(dir_token)
    speed = float(match.group("speed"))
    gust_group = match.group("gust")
    gust = float(gust_group) if gust_group else None
    var_match = re.search(r"\b(?P<from>\d{3})V(?P<to>\d{3})\b", raw)
    var_from = float(var_match.group("from")) if var_match else None
    var_to = float(var_match.group("to")) if var_match else None
    variable = (dir_token == "VRB") or (var_from is not None and var_to is not None)
    return WindInfo(
        direction=direction,
        speed_kt=speed,
        gust_kt=gust,
        variable=variable,
        var_from=var_from,
        var_to=var_to,
    )


def summarize_weather(metars: List[Dict[str, Any]]) -> str:
    if not metars:
        return "No METAR data available."
    entries = []
    for metar in metars:
        raw = metar.get("rawOb") or metar.get("raw_text") or ""
        obs_time = metar.get("obsTime") or metar.get("obs_time")
        if obs_time:
            entries.append(f"{obs_time}: {raw}")
        else:
            entries.append(raw)
    return "\n".join(entries)


def build_metar_entries(metars: List[Dict[str, Any]], nearby: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    distance_lookup = {item["ident"]: item.get("distance_km") for item in nearby}
    entries = []
    for metar in metars:
        station = (
            metar.get("icaoId")
            or metar.get("stationId")
            or metar.get("station")
            or (metar.get("rawOb") or "")[:4]
            or ""
        ).upper()
        raw = metar.get("rawOb") or metar.get("raw_text") or ""
        obs_time = metar.get("obsTime") or metar.get("obs_time")
        entries.append(
            {
                "station": station,
                "distance_km": distance_lookup.get(station),
                "obs_time": obs_time,
                "raw": raw,
            }
        )
    return entries


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS passengers (
            name TEXT PRIMARY KEY,
            weight REAL NOT NULL,
            height REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            height REAL NOT NULL,
            weight REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS airport_profiles (
            icao TEXT PRIMARY KEY,
            elevation_ft REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS airport_runways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao TEXT NOT NULL,
            runway TEXT NOT NULL,
            surface TEXT NOT NULL,
            tora REAL NOT NULL,
            lda REAL NOT NULL,
            FOREIGN KEY (icao) REFERENCES airport_profiles(icao) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    return conn


def load_pilot(conn: sqlite3.Connection, auto_confirm: bool = False) -> Dict[str, float]:
    cur = conn.execute("SELECT name, height, weight FROM pilot WHERE id = 1")
    row = cur.fetchone()
    if row:
        name, height, weight = row
        print(f"Stored pilot profile: {name} ({height:.1f} cm, {weight:.1f} kg)")
        if auto_confirm:
            print("  → auto-confirmed")
            return {"name": name, "height": height, "weight": weight}
        confirm = input("Use stored pilot data? [Y/n]: ").strip().lower()
        if confirm == "" or confirm.startswith("y"):
            return {"name": name, "height": height, "weight": weight}
    name = prompt("Pilot name: ")
    height = float(prompt("Pilot height (cm): "))
    weight = float(prompt("Pilot weight (kg): "))
    conn.execute(
        "INSERT OR REPLACE INTO pilot (id, name, height, weight) VALUES (1, ?, ?, ?)",
        (name, height, weight),
    )
    conn.commit()
    return {"name": name, "height": height, "weight": weight}


def fetch_passenger(conn: sqlite3.Connection, name: str,
                    auto_confirm: bool = False) -> Optional[Passenger]:
    cur = conn.execute("SELECT weight, height FROM passengers WHERE name = ?", (name,))
    row = cur.fetchone()
    if not row:
        return None
    weight, height = row
    if auto_confirm:
        print(f"  Using stored data for {name}: {weight} kg / {height} cm")
        return Passenger(name=name, weight=weight, height=height)
    confirm = input(
        f"Confirm stored data for {name}: {weight} kg / {height} cm? [Y/n]: "
    ).strip().lower()
    if confirm == "" or confirm.startswith("y"):
        return Passenger(name=name, weight=weight, height=height)
    return None


def store_passenger(conn: sqlite3.Connection, passenger: Passenger) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO passengers (name, weight, height) VALUES (?, ?, ?)",
        (passenger.name, passenger.weight, passenger.height),
    )
    conn.commit()


def collect_passengers(conn: sqlite3.Connection, auto_confirm: bool = False,
                         max_passengers: Optional[int] = None) -> List[Passenger]:
    passengers = []
    while True:
        if max_passengers is not None and len(passengers) >= max_passengers:
            print(f"Passenger limit reached ({max_passengers}).")
            break
        name = input("Passenger name (leave empty to finish): ").strip()
        if not name:
            break
        stored = fetch_passenger(conn, name, auto_confirm=auto_confirm)
        if stored:
            passengers.append(stored)
            continue
        weight = float(prompt(f"Weight for {name} (kg): "))
        height = float(prompt(f"Height for {name} (cm): "))
        passenger = Passenger(name=name, weight=weight, height=height)
        store_passenger(conn, passenger)
        passengers.append(passenger)
    if not passengers:
        print("No passengers captured. Mass & balance will use pilot only.")
    return passengers


def summarize_runways(runways: List[RunwayInfo]) -> str:
    if not runways:
        return "No runway data stored."
    parts = []
    for rw in runways:
        parts.append(
            f"RWY {rw.runway} ({rw.surface}, TORA {rw.tora} m, LDA {rw.lda} m)"
        )
    return "; ".join(parts)


def load_airport_profile(conn: sqlite3.Connection, icao: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT elevation_ft FROM airport_profiles WHERE icao = ?",
        (icao.upper(),),
    )
    row = cur.fetchone()
    if not row:
        return None
    elevation_ft = row[0]
    cur = conn.execute(
        "SELECT runway, surface, tora, lda FROM airport_runways WHERE icao = ? ORDER BY runway",
        (icao.upper(),),
    )
    runways = [
        RunwayInfo(runway=r, surface=s, tora=float(t), lda=float(l))
        for r, s, t, l in cur.fetchall()
    ]
    return {
        "icao": icao.upper(),
        "elevation_ft": elevation_ft,
        "runways": runways,
    }


def store_airport_profile(
    conn: sqlite3.Connection,
    icao: str,
    elevation_ft: float,
    runways: List[RunwayInfo],
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO airport_profiles (icao, elevation_ft) VALUES (?, ?)",
        (icao.upper(), elevation_ft),
    )
    conn.execute("DELETE FROM airport_runways WHERE icao = ?", (icao.upper(),))
    for rw in runways:
        conn.execute(
            "INSERT INTO airport_runways (icao, runway, surface, tora, lda) VALUES (?, ?, ?, ?, ?)",
            (icao.upper(), rw.runway, rw.surface, rw.tora, rw.lda),
        )
    conn.commit()


def collect_runway_entries() -> List[RunwayInfo]:
    runways: List[RunwayInfo] = []
    while True:
        runway_id = input("Runway designator (leave empty to finish): ").strip()
        if not runway_id:
            if runways:
                break
            print("At least one runway entry is required.")
            continue
        surface = input("Runway surface (asphalt/grass/etc.): ").strip() or "unknown"
        tora = float(prompt(f"TORA for runway {runway_id} (m): "))
        lda = float(prompt(f"LDA for runway {runway_id} (m): "))
        runways.append(RunwayInfo(runway=runway_id, surface=surface, tora=tora, lda=lda))
    return runways


def ensure_airport_profile(conn: sqlite3.Connection, icao: str,
                           auto_confirm: bool = False) -> Dict[str, Any]:
    icao = icao.upper()
    existing = load_airport_profile(conn, icao)
    if existing:
        print(
            f"Stored airport data for {icao}: elevation {existing['elevation_ft']} ft; "
            f"{summarize_runways(existing['runways'])}"
        )
        if auto_confirm:
            print("  → auto-confirmed")
            return existing
        use_stored = input("Use stored airport data? [Y/n]: ").strip().lower()
        if use_stored == "" or use_stored.startswith("y"):
            return existing
    print(f"Enter airport details for {icao}.")
    elevation_ft = float(prompt("Airport elevation (ft): "))
    runways = collect_runway_entries()
    store_airport_profile(conn, icao, elevation_ft, runways)
    return load_airport_profile(conn, icao) or {
        "icao": icao,
        "elevation_ft": elevation_ft,
        "runways": runways,
    }


def compute_fuel(
    flight_type: str, time_hours: float, stopovers: List[str], aircraft: AircraftConfig
) -> Dict[str, Any]:
    legs = len(stopovers) + 1
    if flight_type in ("pattern", "local"):
        rolling_liters = aircraft.fuel_start_roll
    else:
        rolling_liters = legs * aircraft.fuel_start_roll
    trip_liters = time_hours * aircraft.fuel_cruise_per_hour
    reserve_liters = aircraft.fuel_reserve
    include_climb = flight_type == "cross-country"
    climb_liters = aircraft.fuel_climb if include_climb else 0.0
    total_liters = rolling_liters + trip_liters + reserve_liters + climb_liters
    fuel_mass = total_liters * aircraft.fuel_density
    return {
        "legs": legs,
        "rolling_liters": round(rolling_liters, 1),
        "rolling_label": f"{legs} x Leg" if legs == 1 else f"{legs} x Legs",
        "trip_liters": round(trip_liters, 1),
        "trip_label": f"{time_hours:.1f} h @ {aircraft.fuel_cruise_per_hour:.0f} L/h",
        "climb_liters": round(climb_liters, 1),
        "climb_label": "Climb allowance (4000 ft)" if include_climb else None,
        "reserve_liters": round(reserve_liters, 1),
        "reserve_label": f"{aircraft.fuel_reserve:.0f} L reserve",
        "total_liters": round(total_liters, 1),
        "fuel_mass": round(fuel_mass, 1),
    }


def lever_from_height(height_cm: float) -> float:
    return 0.484 if height_cm < 175 else 0.580


def _build_mass_balance_gyrocopter(inputs: FlightInputs, fuel: Dict[str, Any], aircraft: AircraftConfig) -> Dict[str, Any]:
    stations = []
    stations.append({"label": "Empty Weight", "mass": aircraft.empty_weight, "lever": 0.0, "moment": 0.0})
    stations.append({"label": f"Pilot ({inputs.pilot_name})", "mass": inputs.pilot_weight, "lever": 0.0, "moment": 0.0})
    pax_weight = 0.0
    for pax in inputs.passengers:
        stations.append({"label": f"Passenger ({pax.name})", "mass": pax.weight, "lever": 0.0, "moment": 0.0})
        pax_weight += pax.weight
    if inputs.baggage_weight > 0:
        stations.append({"label": "Baggage", "mass": inputs.baggage_weight, "lever": 0.0, "moment": 0.0})
    fuel_mass = fuel["fuel_mass"]
    stations.append({"label": "Fuel", "mass": fuel_mass, "lever": 0.0, "moment": 0.0})
    total_mass = sum(s["mass"] for s in stations)
    lh_station = inputs.pilot_weight
    rh_station = pax_weight
    cockpit_weight = lh_station + rh_station
    mtow = aircraft.mtow or 560.0
    max_seat = aircraft.max_seat_weight or 110.0
    max_cockpit = aircraft.max_cockpit_weight or 200.0
    min_cockpit = aircraft.min_cockpit_weight or 65.0
    max_storage = aircraft.max_storage_weight or 10.0
    return {
        "stations": stations,
        "total_mass": round(total_mass, 1),
        "total_moment": None,
        "cg": None,
        "inside_envelope": None,
        "line_position": None,
        "envelope": None,
        "gyrocopter": True,
        "lh_station_weight": round(lh_station, 1),
        "rh_station_weight": round(rh_station, 1),
        "cockpit_weight": round(cockpit_weight, 1),
        "baggage_weight": round(inputs.baggage_weight, 1),
        "mtow": mtow,
        "max_seat_weight": max_seat,
        "max_cockpit_weight": max_cockpit,
        "min_cockpit_weight": min_cockpit,
        "max_storage_weight": max_storage,
        "within_mtow": total_mass <= mtow,
        "lh_ok": lh_station <= max_seat,
        "rh_ok": rh_station <= max_seat,
        "cockpit_ok": min_cockpit <= cockpit_weight <= max_cockpit,
        "baggage_ok": inputs.baggage_weight <= max_storage * 2,
    }


def build_mass_balance(inputs: FlightInputs, fuel: Dict[str, Any], aircraft: AircraftConfig) -> Dict[str, Any]:
    if aircraft.max_seat_weight is not None:
        return _build_mass_balance_gyrocopter(inputs, fuel, aircraft)
    stations = []
    stations.append(
        {
            "label": "Empty Weight",
            "mass": aircraft.empty_weight,
            "lever": aircraft.empty_lever,
            "moment": aircraft.empty_weight * aircraft.empty_lever,
        }
    )
    pilot_lever = lever_from_height(inputs.pilot_height)
    stations.append(
        {
            "label": f"Pilot ({inputs.pilot_name})",
            "mass": inputs.pilot_weight,
            "lever": pilot_lever,
            "moment": inputs.pilot_weight * pilot_lever,
        }
    )
    for pax in inputs.passengers:
        lever = lever_from_height(pax.height)
        stations.append(
            {
                "label": f"Passenger ({pax.name})",
                "mass": pax.weight,
                "lever": lever,
                "moment": pax.weight * lever,
            }
        )
    if inputs.baggage_weight > 0:
        stations.append(
            {
                "label": "Baggage",
                "mass": inputs.baggage_weight,
                "lever": aircraft.baggage_lever,
                "moment": inputs.baggage_weight * aircraft.baggage_lever,
            }
        )
    fuel_mass = fuel["fuel_mass"]
    stations.append(
        {
            "label": "Fuel",
            "mass": fuel_mass,
            "lever": aircraft.fuel_lever,
            "moment": fuel_mass * aircraft.fuel_lever,
        }
    )
    total_mass = sum(s["mass"] for s in stations)
    total_moment = sum(s["moment"] for s in stations)
    cg = total_moment / total_mass
    inside_envelope = point_in_polygon((total_mass, total_moment), aircraft.cg_envelope)
    line_position = (cg - aircraft.cg_line_min) / (aircraft.cg_line_max - aircraft.cg_line_min)
    return {
        "stations": stations,
        "total_mass": round(total_mass, 1),
        "total_moment": round(total_moment, 1),
        "cg": round(cg, 3),
        "inside_envelope": inside_envelope,
        "line_position": line_position,
        "envelope": [{"mass": m, "moment": mom} for m, mom in aircraft.cg_envelope],
    }


def point_in_polygon(point, polygon):
    x, y = point
    num = len(polygon)
    inside = False
    px1, py1 = polygon[0]
    for i in range(num + 1):
        px2, py2 = polygon[i % num]
        if min(py1, py2) < y <= max(py1, py2) and x <= max(px1, px2):
            if py1 != py2:
                xints = (y - py1) * (px2 - px1) / (py2 - py1 + 1e-9) + px1
            else:
                xints = px1
            if px1 == px2 or x <= xints:
                inside = not inside
        px1, py1 = px2, py2
    return inside


def compute_airport_performance(
    elevation_ft: float,
    qnh: Optional[float],
    oat: Optional[float],
) -> Dict[str, Any]:
    """Pressure altitude and density altitude for a single airport."""
    if qnh is None:
        pa = None
    else:
        pa = elevation_ft + (1013.0 - qnh) * 30.0
    isa_temp = 15.0 - 2.0 * (pa / 1000.0) if pa is not None else None
    delta_isa = (oat - isa_temp) if (isa_temp is not None and oat is not None) else None
    if pa is not None and delta_isa is not None:
        da = pa + 120.0 * delta_isa
    else:
        da = None
    return {
        "pressure_altitude_ft": round(pa, 0) if pa is not None else None,
        "density_altitude_ft": round(da, 0) if da is not None else None,
        "isa_temp_c": round(isa_temp, 1) if isa_temp is not None else None,
        "delta_isa_c": round(delta_isa, 1) if delta_isa is not None else None,
    }


def compute_performance(inputs: FlightInputs, weather: WeatherData) -> Dict[str, Any]:
    """Legacy top-level performance — uses departure airport + primary weather."""
    return compute_airport_performance(
        inputs.airport_elevation_ft,
        weather.lowest_qnh,
        weather.oat_c,
    )


def collect_inputs(conn: sqlite3.Connection, aircraft_type: str, auto_confirm: bool = False) -> FlightInputs:
    config = get_aircraft_config(aircraft_type)
    seat_label = ("unlimited" if config.max_passengers is None else str(config.max_passengers))
    print(f"  Aircraft: {config.name} ({config.code}) — passenger seats: {seat_label}")
    def icao_validator(code: str) -> None:
        if len(code.strip()) != 4:
            raise ValueError("ICAO must be 4 letters")

    departure = prompt("From which airport will the flight depart? (ICAO): ", icao_validator)
    airport_profile = ensure_airport_profile(conn, departure, auto_confirm=auto_confirm)
    airport_name = input("Departure airport name (optional): ").strip() or departure.upper()
    elevation_ft = float(airport_profile["elevation_ft"])
    runways = airport_profile.get("runways", [])
    segments: List[AirportSegment] = [
        AirportSegment(
            icao=departure.upper(),
            name=airport_name,
            elevation_ft=elevation_ft,
            runways=runways,
        )
    ]
    def flight_type_validator(ftype: str) -> None:
        if ftype.lower() not in {"pattern", "local", "cross-country"}:
            raise ValueError("Invalid type")
    flight_type = prompt("What type of flight? (pattern/local/cross-country): ", flight_type_validator).lower()
    if flight_type == "cross-country":
        total_time = float(prompt("Total estimated flight time (hours): "))
        stop_count = int(prompt("Number of stopovers: "))
        stopovers: List[str] = []
        for idx in range(stop_count):
            stop_icao = prompt(f"Stopover {idx + 1} ICAO: ", icao_validator).upper()
            stop_profile = ensure_airport_profile(conn, stop_icao, auto_confirm=auto_confirm)
            stop_name = input(f"Stopover {idx + 1} name (optional): ").strip() or stop_icao
            stop_elevation = float(stop_profile["elevation_ft"])
            stop_runways = stop_profile.get("runways", [])
            segments.append(
                AirportSegment(
                    icao=stop_icao,
                    name=stop_name,
                    elevation_ft=stop_elevation,
                    runways=stop_runways,
                )
            )
            stopovers.append(stop_icao)
    else:
        total_time = float(prompt("Estimated flight time (hours): "))
        stopovers = []
    passengers = collect_passengers(conn, auto_confirm=auto_confirm,
                                        max_passengers=config.max_passengers)
    baggage_weight = float(prompt("Baggage weight (kg, 0 if none): "))
    pilot = load_pilot(conn, auto_confirm=auto_confirm)
    return FlightInputs(
        departure_icao=departure.upper(),
        airport_name=airport_name,
        airport_elevation_ft=elevation_ft,
        runways=runways,
        airports=segments,
        flight_type=flight_type,
        estimated_time_hours=total_time,
        stopovers=stopovers,
        passengers=passengers,
        baggage_weight=baggage_weight,
        aircraft_type=config.code,
        aircraft_name=config.name,
        pilot_name=pilot["name"],
        pilot_weight=pilot["weight"],
        pilot_height=pilot["height"],
    )


# ---------------------------------------------------------------------------
# Non-interactive fast path (CLI arguments)
# ---------------------------------------------------------------------------

def _lookup_airport_name(conn: sqlite3.Connection, icao: str) -> str:
    ensure_airport_geo_db(conn)
    row = conn.execute("SELECT name FROM airports_geo WHERE icao=?", (icao.upper(),)).fetchone()
    return row[0] if row else icao.upper()


def collect_inputs_from_args(args: argparse.Namespace, conn: sqlite3.Connection,
                                 aircraft_type: str) -> FlightInputs:
    """Build FlightInputs from CLI args without any interactive prompts.

    Usage examples
    --------------
    # Local flight (no stopovers):
    python3 flight_briefing.py EDDE --time 0.5 --type local --baggage 5

    # Cross-country with stopovers:
    python3 flight_briefing.py EDDE EDDM EDDL --time 3.0 --baggage 10 --pax Alice:60:165

    # Auto-use all stored data (fastest):
    python3 flight_briefing.py EDDE EDDM --time 2.0

    Passenger format: NAME:WEIGHT_KG:HEIGHT_CM
    If a passenger is stored in DB, weight/height from args override it when provided.
    If no --pax given, uses all stored passengers (prompted interactively in interactive mode).
    """
    config = get_aircraft_config(aircraft_type)
    seat_label = ("unlimited" if config.max_passengers is None else str(config.max_passengers))
    print(f"  Aircraft: {config.name} ({config.code}) — passenger seats: {seat_label}")
    icaos: List[str] = [args.departure.upper()] + [s.upper() for s in (args.stopovers or [])]
    departure_icao = icaos[0]
    stopover_icaos = icaos[1:]

    # Determine flight type
    flight_type = args.type
    if flight_type is None:
        flight_type = "cross-country" if stopover_icaos else "local"

    # Load all airport profiles (auto-confirm stored)
    segments: List[AirportSegment] = []
    for icao in icaos:
        profile = load_airport_profile(conn, icao)
        if profile is None:
            print(f"⚠  No stored profile for {icao}. Please enter it once interactively first.")
            print(f"   Run:  python3 flight_briefing.py  (interactive mode)")
            raise SystemExit(1)
        name = _lookup_airport_name(conn, icao)
        role = "Departure" if icao == departure_icao else icao
        print(f"  {icao}: {name} — {profile['elevation_ft']} ft; {summarize_runways(profile['runways'])}")
        segments.append(AirportSegment(
            icao=icao,
            name=name,
            elevation_ft=float(profile["elevation_ft"]),
            runways=profile["runways"],
        ))

    # Passengers
    passengers: List[Passenger] = []
    if args.pax:
        for pax_str in args.pax:
            parts = pax_str.split(":")
            if len(parts) != 3:
                print(f"⚠  Invalid --pax format '{pax_str}'. Expected NAME:WEIGHT:HEIGHT — skipped.")
                continue
            name_p, weight_s, height_s = parts
            try:
                pax = Passenger(name=name_p.strip(),
                                weight=float(weight_s),
                                height=float(height_s))
                _store_passenger_silent(conn, pax)
                passengers.append(pax)
            except ValueError:
                print(f"⚠  Invalid weight/height in '{pax_str}' — skipped.")
    elif getattr(args, 'no_pax', False):
        print("  No passengers (--no-pax).")
    else:
        # Use all stored passengers silently
        rows = conn.execute("SELECT name, weight, height FROM passengers").fetchall()
        for name_p, weight_p, height_p in rows:
            passengers.append(Passenger(name=name_p, weight=float(weight_p), height=float(height_p)))
        if passengers:
            print(f"  Using {len(passengers)} stored passenger(s): {', '.join(p.name for p in passengers)}")

    max_pax = config.max_passengers
    if max_pax is not None and len(passengers) > max_pax:
        print(f"⚠  {config.name} allows {max_pax} passenger(s). You provided {len(passengers)}. Use --pax/--no-pax to stay within limits.")
        raise SystemExit(1)

    baggage = float(args.baggage) if args.baggage is not None else 0.0

    # Pilot
    cur = conn.execute("SELECT name, height, weight FROM pilot WHERE id = 1")
    row = cur.fetchone()
    if not row:
        print("⚠  No stored pilot profile. Run once in interactive mode first.")
        raise SystemExit(1)
    pilot_name, pilot_height, pilot_weight = row
    print(f"  Pilot: {pilot_name} ({pilot_height:.0f} cm, {pilot_weight:.0f} kg)")

    total_time = float(args.time) if args.time else _prompt_time()

    return FlightInputs(
        departure_icao=departure_icao,
        airport_name=segments[0].name,
        airport_elevation_ft=segments[0].elevation_ft,
        runways=segments[0].runways,
        airports=segments,
        flight_type=flight_type,
        estimated_time_hours=total_time,
        stopovers=stopover_icaos,
        passengers=passengers,
        baggage_weight=baggage,
        aircraft_type=config.code,
        aircraft_name=config.name,
        pilot_name=pilot_name,
        pilot_weight=float(pilot_weight),
        pilot_height=float(pilot_height),
    )


def _store_passenger_silent(conn: sqlite3.Connection, pax: Passenger) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO passengers (name, weight, height) VALUES (?, ?, ?)",
        (pax.name, pax.weight, pax.height),
    )
    conn.commit()


def _prompt_time() -> float:
    return float(prompt("Estimated flight time (hours): "))


def _fetch_segment_weather(seg: AirportSegment,
                           conn_or_path: Any = None) -> Tuple[str, Dict[str, Any]]:
    """Fetch METAR (bbox) + TAF (direct) for one segment.
    Opens its own SQLite connection so it is safe to call from any thread.
    METAR and TAF are fetched sequentially within the thread; parallelism
    comes from the outer ThreadPoolExecutor (one thread per segment)."""
    icao = seg.icao.upper()
    # Thread-local DB connection (SQLite objects must not cross threads)
    _local_conn = sqlite3.connect(DB_PATH)
    try:
        ensure_airport_geo_db(_local_conn)
        latlon = get_airport_latlon(_local_conn, icao)
    finally:
        _local_conn.close()
    if latlon:
        bbox = bbox_for_radius(latlon[0], latlon[1], 50.0)
        metar_params: Dict[str, Any] = {
            "bbox": f"{bbox[0]:.3f},{bbox[1]:.3f},{bbox[2]:.3f},{bbox[3]:.3f}",
            "format": "json",
        }
    else:
        metar_params = {"ids": icao, "format": "json"}
    taf_params: Dict[str, Any] = {"ids": icao, "format": "json"}
    # Fetch METAR and TAF concurrently within this thread using the shared session
    try:
        metars_resp = _http_get(METAR_API, metar_params)
        metars = _parse_json_list(metars_resp)
    except Exception:
        metars = []
    try:
        tafs_resp = _http_get(TAF_API, taf_params)
        tafs = _parse_json_list(tafs_resp)
    except Exception:
        tafs = []
    return icao, {"metars": metars, "tafs": tafs, "latlon": latlon}


def _build_segment_payload_from_raw(
    seg: AirportSegment,
    raw: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert raw (metars, tafs) for a segment into the standard weather payload."""
    metars: List[Dict[str, Any]] = raw.get("metars", [])
    tafs_raw: List[Dict[str, Any]] = raw.get("tafs", [])
    latlon = raw.get("latlon")
    icao = seg.icao.upper()

    metar_by_station = {extract_station_id(m): m for m in metars}
    taf_by_station = {extract_station_id(t): t for t in tafs_raw}

    # Build ordered metar_entries: primary ICAO first, then nearby sorted by distance
    metar_entries: List[Dict[str, Any]] = []
    metar_objects: List[Dict[str, Any]] = []
    seen_metar: set = set()

    # Primary station first
    primary_metar = metar_by_station.get(icao)
    if primary_metar:
        metar_objects.append(primary_metar)
        metar_entries.append({
            "station": icao,
            "distance_km": 0.0,
            "obs_time": primary_metar.get("obsTime") or primary_metar.get("obs_time"),
            "raw": primary_metar.get("rawOb") or primary_metar.get("raw_text") or "",
        })
        seen_metar.add(icao)

    # Remaining stations — attach distance using SQLite bounding box query result
    if latlon:
        home_lat, home_lon = latlon
        for station, metar in metar_by_station.items():
            if station in seen_metar:
                continue
            dist = None
            # compute approximate distance
            try:
                lat_m = metar.get("lat") or metar.get("latitude")
                lon_m = metar.get("lon") or metar.get("longitude")
                # Fallback: lookup station in local airport cache if API coordinates missing
                if lat_m is None or lon_m is None:
                    cached = load_airports().get(station)
                    if cached:
                        lat_m = cached.get("lat")
                        lon_m = cached.get("lon")
                
                if lat_m is not None and lon_m is not None:
                    dist = round(haversine_km(home_lat, home_lon,
                                              float(lat_m), float(lon_m)), 1)
            except Exception:
                pass
            metar_objects.append(metar)
            metar_entries.append({
                "station": station,
                "distance_km": dist,
                "obs_time": metar.get("obsTime") or metar.get("obs_time"),
                "raw": metar.get("rawOb") or metar.get("raw_text") or "",
            })
            seen_metar.add(station)
        # Sort: primary first, then by distance
        if len(metar_entries) > 1:
            primary_entry = metar_entries[0] if metar_entries[0]["station"] == icao else None
            rest = [e for e in metar_entries if e["station"] != icao]
            rest.sort(key=lambda e: (e["distance_km"] is None, e["distance_km"] or 0))
            metar_entries = ([primary_entry] if primary_entry else []) + rest
            metar_objects = [metar_by_station[e["station"]] for e in metar_entries
                             if e["station"] in metar_by_station]
    else:
        for station, metar in metar_by_station.items():
            if station in seen_metar:
                continue
            metar_objects.append(metar)
            metar_entries.append({
                "station": station,
                "distance_km": None,
                "obs_time": metar.get("obsTime") or metar.get("obs_time"),
                "raw": metar.get("rawOb") or metar.get("raw_text") or "",
            })

    # TAF entries: bbox query may return several stations — sort by distance, keep closest
    taf_with_dist: List[Tuple[float, str, Dict[str, Any]]] = []
    home_lat2, home_lon2 = latlon if latlon else (None, None)
    for station, taf in taf_by_station.items():
        dist = 0.0 if station == icao else 9999.0
        if home_lat2 is not None:
            lat_t = taf.get("lat") or taf.get("latitude")
            lon_t = taf.get("lon") or taf.get("longitude")
            # Fallback: lookup station in local airport cache if API coordinates missing
            if lat_t is None or lon_t is None:
                cached = load_airports().get(station)
                if cached:
                    lat_t = cached.get("lat")
                    lon_t = cached.get("lon")

            if lat_t is not None and lon_t is not None:
                try:
                    dist = haversine_km(home_lat2, home_lon2, float(lat_t), float(lon_t))
                except Exception:
                    pass
        taf_with_dist.append((dist, station, taf))
    taf_with_dist.sort(key=lambda x: x[0])

    taf_entries: List[Dict[str, Any]] = []
    taf_objects: List[Dict[str, Any]] = []
    for dist_t, station, taf in taf_with_dist[:2]:   # at most 2 closest TAF stations
        raw_taf = taf.get("rawTAF") or taf.get("raw_text") or ""
        if not raw_taf:
            continue                                  # skip empty shells
        taf_entries.append({
            "station": station,
            "distance_km": round(dist_t, 1) if dist_t < 9000 else None,
            "issue": taf.get("issueTime") or taf.get("issue_time") or taf.get("bulletinTime"),
            "raw": raw_taf,
        })
        taf_objects.append(taf)

    lowest_qnh, temp, _ = parse_lowest_qnh_and_temp(metar_objects)
    if temp is None:
        temp = extract_oat(metar_objects)

    primary_raw = metar_entries[0]["raw"] if metar_entries else None
    wind_info = parse_wind_from_raw(primary_raw or "") if primary_raw else None
    if wind_info is None:
        for m in metar_objects:
            raw_str = m.get("rawOb") or m.get("raw_text") or ""
            wind_info = parse_wind_from_raw(raw_str)
            if wind_info:
                break

    weather_payload: Dict[str, Any] = {
        "metar_entries": metar_entries,
        "taf_entries": taf_entries,
        "lowest_qnh": round(lowest_qnh, 1) if lowest_qnh is not None else None,
        "oat_c": round(temp, 1) if temp is not None else None,
        "wind": wind_info,
    }
    if metar_entries:
        weather_payload["primary_station"] = metar_entries[0]["station"]
        weather_payload["primary_distance_km"] = metar_entries[0].get("distance_km")

    neighbors = [{"ident": e["station"], "name": "", "distance_km": e.get("distance_km")}
                 for e in metar_entries]

    return {
        "weather": weather_payload,
        "metars": metar_objects,
        "tafs": taf_objects,
        "weather_summary": "\n".join(e["raw"] for e in metar_entries if e.get("raw")),
        "nearby_airports": neighbors,
    }


# Module-level conn cache to pass into the payload builder without threading issues
conn_cache: Dict[str, Any] = {}


def build_briefing(inputs: FlightInputs, conn: Optional[sqlite3.Connection] = None) -> BriefingData:
    warnings: List[str] = []
    aircraft = get_aircraft_config(inputs.aircraft_type)

    segments = inputs.airports or [
        AirportSegment(
            icao=inputs.departure_icao.upper(),
            name=inputs.airport_name,
            elevation_ft=inputs.airport_elevation_ft,
            runways=inputs.runways,
        )
    ]

    # ── Fetch weather for ALL segments — one flat thread pool, all requests at once ──
    import time as _time
    t0 = _time.time()
    raw_by_icao: Dict[str, Dict[str, Any]] = {seg.icao.upper(): {"metars": [], "tafs": [], "latlon": None}
                                               for seg in segments}

    # Pre-compute lat/lon for all segments in the main thread (SQLite, fast)
    seg_http: List[Tuple[str, str, Dict[str, Any]]] = []  # (icao, 'metar'|'taf', params)
    if conn is not None:
        for seg in segments:
            icao_key = seg.icao.upper()
            latlon = get_airport_latlon(conn, icao_key)
            raw_by_icao[icao_key]["latlon"] = latlon
            if latlon:
                bbox = bbox_for_radius(latlon[0], latlon[1], 50.0)
                metar_p: Dict[str, Any] = {
                    "bbox": f"{bbox[0]:.3f},{bbox[1]:.3f},{bbox[2]:.3f},{bbox[3]:.3f}",
                    "format": "json",
                }
            else:
                metar_p = {"ids": icao_key, "format": "json"}
            seg_http.append((icao_key, "metar", metar_p))
            # TAF: same bbox as METAR so we get the closest station that publishes one
            seg_http.append((icao_key, "taf", metar_p))
    else:
        # Fallback (no conn / no lat-lon): bulk ID lookup for all segments
        all_codes = list({seg.icao.upper() for seg in segments})
        seg_http.append(("__all__", "metar", {"ids": ",".join(all_codes), "format": "json"}))
        seg_http.append(("__all__", "taf",   {"ids": ",".join(all_codes), "format": "json"}))

    def _do_request(icao: str, rtype: str, params: Dict[str, Any]) -> Tuple[str, str, List]:
        url = METAR_API if rtype == "metar" else TAF_API
        try:
            result = _parse_json_list(_http_get(url, params))
            if not result:
                print(f"  ℹ  {rtype.upper()} returned empty for {icao}")
            return icao, rtype, result
        except Exception as exc:
            print(f"  ⚠  {rtype.upper()} failed for {icao} after {HTTP_MAX_RETRIES} retries: {exc}")
            return icao, rtype, []

    # Cap workers to HTTP_CONCURRENCY — semaphore already limits in-flight requests,
    # but limiting threads too avoids unnecessary context switching.
    n_workers = max(2, min(len(seg_http), HTTP_CONCURRENCY * 2))
    print(f"  Firing {len(seg_http)} HTTP request(s) — concurrency cap: {HTTP_CONCURRENCY}, workers: {n_workers}…")
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [ex.submit(_do_request, icao, rtype, params) for icao, rtype, params in seg_http]
        for fut in as_completed(futs):
            icao_key, rtype, data = fut.result()
            if icao_key == "__all__":
                # Bulk fallback: distribute by station id
                if rtype == "metar":
                    for seg in segments:
                        raw_by_icao[seg.icao.upper()]["metars"] = data
                else:
                    taf_map = {extract_station_id(t): t for t in data}
                    for seg in segments:
                        k = seg.icao.upper()
                        raw_by_icao[k]["tafs"] = [taf_map[k]] if k in taf_map else []
            else:
                if rtype == "metar":
                    raw_by_icao[icao_key]["metars"] = data
                else:
                    raw_by_icao[icao_key]["tafs"] = data

    print(f"  Weather fetch: {_time.time() - t0:.2f}s ({len(seg_http)} requests)")

    # ── Build per-segment payloads (CPU-only, fast) ─────────────────────────
    segment_payloads: Dict[str, Dict[str, Any]] = {}
    airport_sections: List[Dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        raw = raw_by_icao.get(seg.icao.upper(), {"metars": [], "tafs": [], "latlon": None})
        payload = _build_segment_payload_from_raw(seg, raw)
        segment_payloads[seg.icao.upper()] = payload
        role = "Departure" if idx == 0 else f"Stop {idx}"
        seg_weather = payload["weather"]
        seg_perf = compute_airport_performance(
            seg.elevation_ft,
            seg_weather.get("lowest_qnh"),
            seg_weather.get("oat_c"),
        )
        airport_sections.append({
            "icao": seg.icao.upper(),
            "name": seg.name,
            "role": role,
            "elevation_ft": seg.elevation_ft,
            "runways": seg.runways,
            "weather": seg_weather,
            "performance": seg_perf,
        })

    primary_payload = segment_payloads[segments[0].icao.upper()]
    primary_weather = primary_payload["weather"]
    weather = WeatherData(
        metars=primary_payload["metars"],
        taf=primary_payload["tafs"],
        lowest_qnh=primary_weather["lowest_qnh"],
        oat_c=primary_weather["oat_c"],
        wind=primary_weather["wind"],
        weather_summary=primary_payload["weather_summary"],
        nearby_airports=primary_payload["nearby_airports"],
        metar_entries=primary_weather["metar_entries"],
    )
    if primary_weather["lowest_qnh"] is None:
        warnings.append("Reference QNH unavailable; performance needs manual input.")
    fuel = compute_fuel(inputs.flight_type, inputs.estimated_time_hours, inputs.stopovers, aircraft)
    mass_balance = build_mass_balance(inputs, fuel, aircraft)
    if mass_balance.get("gyrocopter"):
        if not mass_balance["within_mtow"]:
            warnings.append(f"Exceeds MTOW ({mass_balance['total_mass']} kg / {mass_balance['mtow']:.0f} kg)")
        if not mass_balance["lh_ok"]:
            warnings.append(f"LH seat exceeds limit ({mass_balance['lh_station_weight']} kg / {mass_balance['max_seat_weight']:.0f} kg max)")
        if not mass_balance["rh_ok"]:
            warnings.append(f"RH seat exceeds limit ({mass_balance['rh_station_weight']} kg / {mass_balance['max_seat_weight']:.0f} kg max)")
        if not mass_balance["cockpit_ok"]:
            cw = mass_balance["cockpit_weight"]
            if cw < mass_balance["min_cockpit_weight"]:
                warnings.append(f"Total cockpit below minimum ({cw} kg / min {mass_balance['min_cockpit_weight']:.0f} kg — ballast required)")
            else:
                warnings.append(f"Total cockpit exceeds maximum ({cw} kg / max {mass_balance['max_cockpit_weight']:.0f} kg)")
        if not mass_balance["baggage_ok"]:
            warnings.append(f"Baggage exceeds storage capacity ({mass_balance['baggage_weight']} kg / max {mass_balance['max_storage_weight'] * 2:.0f} kg)")
    elif mass_balance["inside_envelope"] is False:
        warnings.append("Center of gravity outside envelope")
    if aircraft.fuel_capacity_liters is not None and fuel["total_liters"] > aircraft.fuel_capacity_liters:
        warnings.append(f"Required fuel ({fuel['total_liters']} L) exceeds tank capacity ({aircraft.fuel_capacity_liters:.0f} L)")
    performance = compute_performance(inputs, weather)
    if performance["pressure_altitude_ft"] is None:
        warnings.append("Pressure altitude missing")
    generation_time = datetime.now(timezone.utc).isoformat()
    return BriefingData(
        inputs=inputs,
        weather=weather,
        fuel=fuel,
        mass_balance=mass_balance,
        performance=performance,
        warnings=[w for w in warnings if w],
        airport_sections=airport_sections,
        generation_time=generation_time,
    )


def inject_into_template(data: BriefingData) -> None:
    ensure_template()
    payload = json.loads(json.dumps(asdict(data)))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__BRIEFING_DATA__", json.dumps(payload))
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Briefing written to {OUTPUT_HTML}")
    print(f"BRIEFING_URL_READY")   # sentinel parsed by bot to confirm success


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flight_briefing.py",
        description=(
            "Flight Briefing Generator\n\n"
            "Fast mode (all stored data, no prompts):\n"
            "  python3 flight_briefing.py EDDE EDDM --time 2.0\n\n"
            "With passengers:\n"
            "  python3 flight_briefing.py EDDE EDDM --time 2.0 --pax Alice:60:165 --pax Bob:80:178\n\n"
            "Interactive mode (no args):\n"
            "  python3 flight_briefing.py\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("departure", nargs="?", metavar="DEPARTURE_ICAO",
                   help="Departure airport ICAO (e.g. EDDE). Omit for interactive mode.")
    p.add_argument("stopovers", nargs="*", metavar="ICAO",
                   help="Stopover / destination ICAO codes (last = final destination).")
    p.add_argument("--time", "-t", type=float, metavar="HOURS",
                   help="Total estimated flight time in hours.")
    p.add_argument("--type", choices=["pattern", "local", "cross-country"],
                   default=None, help="Flight type (auto-detected when stopovers given).")
    p.add_argument("--aircraft", choices=sorted(AIRCRAFT_TYPES.keys()), default=None,
                   help=f"Aircraft profile to use (default: {DEFAULT_AIRCRAFT_TYPE}).")
    p.add_argument("--pax", action="append", metavar="NAME:WEIGHT:HEIGHT",
                   help="Passenger in NAME:weight_kg:height_cm format. Repeatable. "
                        "Omit to use all stored passengers.")
    p.add_argument("--baggage", type=float, default=0.0, metavar="KG",
                   help="Baggage weight in kg (default: 0).")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Auto-confirm all stored data (pilot, airports, passengers).")
    p.add_argument("--no-pax", action="store_true",
                   help="Fly solo — ignore all stored passengers.")
    return p


def main() -> None:
    import time as _time
    t_start = _time.time()

    parser = _build_arg_parser()
    args = parser.parse_args()

    aircraft_code = (args.aircraft or DEFAULT_AIRCRAFT_TYPE).lower()
    if not args.departure and args.aircraft is None:
        aircraft_code = prompt_aircraft_type(DEFAULT_AIRCRAFT_TYPE)

    ensure_template()
    conn = init_db()
    ensure_airport_geo_db(conn)  # warm up SQLite airport cache once

    if args.departure:
        # ── Fast / non-interactive path ────────────────────────────────────
        print(f"\n🛫  Flight Briefing — fast mode")
        print(f"    Route: {args.departure.upper()}" +
              (f" → {' → '.join(s.upper() for s in args.stopovers)}" if args.stopovers else ""))
        try:
            inputs = collect_inputs_from_args(args, conn, aircraft_code)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"Error building inputs: {exc}")
            raise
    else:
        # ── Interactive path (legacy) ──────────────────────────────────────
        print("\n🛫  Flight Briefing — interactive mode")
        print("    Tip: pass ICAO codes on the command line for a much faster run.\n"
              "    Example: python3 flight_briefing.py EDDE EDDM --time 2.0\n")
        inputs = collect_inputs(conn, aircraft_code, auto_confirm=args.yes)

    data = build_briefing(inputs, conn=conn)
    inject_into_template(data)

    elapsed = _time.time() - t_start
    print(f"\n✅  Done in {elapsed:.1f}s")


DEFAULT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Flight Briefing</title>
<style>
body { font-family: system-ui, sans-serif; margin: 0; background:#f5f7fb; color:#0d1b2a; }
main { max-width: 1200px; margin: auto; padding: 2rem; }
section { background:#fff; margin-bottom:1.5rem; border-radius:12px; padding:1.25rem; box-shadow:0 2px 8px rgba(0,0,0,0.08); }
h2 { margin-top:0; font-size:1.2rem; letter-spacing:0.05em; text-transform:uppercase; color:#415a77; }
h3 { margin-top:1rem; color:#1b263b; }
table { width:100%; border-collapse:collapse; }
th, td { padding:0.5rem; border-bottom:1px solid #e0e7ff; text-align:left; vertical-align:top; }
.warning { border-left:4px solid #d90429; background:#ffe8ec; padding:0.75rem 1rem; margin-bottom:0.75rem; border-radius:8px; }
.badge-row { display:flex; flex-wrap:wrap; gap:0.5rem; margin-top:0.75rem; }
.badge-row a { text-decoration:none; color:#0d1b2a; background:#edf2fb; padding:0.35rem 0.9rem; border-radius:999px; font-size:0.85rem; border:1px solid #dbe4ff; }
.badge-row a:hover { background:#dbe4ff; }
.airport-list { display:flex; flex-wrap:wrap; gap:0.4rem; margin-top:0.5rem; margin-bottom:0.75rem; }
.airport-list span { background:#e9ecef; border-radius:999px; padding:0.3rem 0.8rem; font-size:0.85rem; color:#1b263b; border:1px solid #dee2e6; }
canvas { width:100%; max-width:none; height:auto; border:1px solid #dbe4ff; border-radius:8px; background:#fafcff; min-height:600px; }
.flex { display:flex; flex-direction:column; gap:1.5rem; }
.flex > div { width:100%; }
.airport-weather { border:1px solid #dbe4ff; border-radius:12px; padding:1rem; margin-bottom:1rem; background:#fdfcff; }
.airport-weather h4 { margin:0 0 0.5rem; color:#1b263b; }
.weather-flex { display:flex; gap:1rem; flex-wrap:wrap; flex-direction: column; }
.weather-flex > div { width: 100%; }
.runway-block { border:1px solid #e0e7ff; border-radius:12px; padding:1rem; margin-top:1rem; background:#ffffff; }
.runway-block h4 { margin:0 0 0.5rem; }
.metar-table code { font-family:"SFMono-Regular", Menlo, monospace; white-space:pre-wrap; word-break:break-word; display:block; }
.metar-table th:nth-child(4), .metar-table td:nth-child(4) { width:55%; }
.total-row th, .total-row td { font-weight:700; }
.weather-info { margin-top:0.75rem; font-weight:600; }
.runway-table th, .runway-table td { border-bottom:1px solid #e0e7ff; }
.status-ok { color:#2b9348; font-weight:700; }
.status-fail { color:#d90429; font-weight:700; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Flight Briefing</h1>
    <div id="meta"></div>
  </header>
  <div id="warnings"></div>
  <section>
    <h2>1 — Maintenance</h2>
    <p>Review aircraft maintenance status in logbook.</p>
  </section>
  <section>
    <h2>2 — Weather</h2>
    <div id="weather-sections"></div>
    <div class="badge-row">
      <a href="https://www.flugwetter.de/fw/gafor/index.htm" target="_blank" rel="noreferrer noopener">GAFOR</a>
      <a href="https://www.flugwetter.de/fw/warn/index.htm" target="_blank" rel="noreferrer noopener">SIGMET</a>
      <a href="https://www.flugwetter.de/fw/chartsga/skyview/index.htm" target="_blank" rel="noreferrer noopener">Surface Weather Chart</a>
      <a href="https://www.flugwetter.de/fw/bilder/sat/index.htm?type=ir_rgb_eu" target="_blank" rel="noreferrer noopener">Satellite Europe</a>
      <a href="https://www.flugwetter.de/fw/bilder/rad/index.htm?type=rx" target="_blank" rel="noreferrer noopener">Radar DE</a>
      <a href="https://www.flugwetter.de/fw/bilder/rad/index.htm?type=eu" target="_blank" rel="noreferrer noopener">Radar EU</a>
      <a href="https://apps.apple.com/app/windy-wind-weather-forecast/id1094311790" target="_blank" rel="noreferrer noopener">Windy (iOS)</a>
    </div>
  </section>
  <section>
    <h2>3 — NOTAMs</h2>
    <p>Check NOTAMs for departure, destination and alternates.</p>
  </section>
  <section>
    <h2>4 — Fuel Planning</h2>
    <div id="fuel-table"></div>
  </section>
  <section>
    <h2>5 — Mass & Balance</h2>
    <div class="flex">
      <div>
        <div id="mass-table"></div>
      </div>
      <div>
        <canvas id="cg-chart" width="1500" height="1000"></canvas>
      </div>
    </div>
  </section>
  <section>
    <h2>6 — Performance</h2>
    <div id="performance"></div>
    <p>Reminders: verify takeoff and landing distances.</p>
  </section>
  <section>
    <h2>7 — Charts</h2>
    <p>Review charts for departure, destination and alternates.</p>
  </section>
</main>
<script>
const data = __BRIEFING_DATA__;
const airportSections = data.airport_sections || [];
const qs = s => document.querySelector(s);
const toNumber = val => (val === null || val === undefined ? null : Number(val));
const normalizeDegrees = deg => {
  const num = toNumber(deg);
  if (num === null || Number.isNaN(num)) return null;
  let normalized = num % 360;
  if (normalized < 0) normalized += 360;
  return normalized;
};
const angleDiff = (a, b) => {
  const na = normalizeDegrees(a);
  const nb = normalizeDegrees(b);
  if (na === null || nb === null) return null;
  let diff = Math.abs(na - nb);
  return diff > 180 ? 360 - diff : diff;
};
const runwayHeading = runwayId => {
  if (!runwayId) return null;
  const match = runwayId.match(/(\\d{2})/);
  if (!match) return null;
  let value = parseInt(match[1], 10);
  if (Number.isNaN(value)) return null;
  if (value === 36) value = 0;
  return value * 10;
};
const runwayNumber = runwayId => {
  if (!runwayId) return null;
  const match = runwayId.match(/(\\d{2})/);
  if (!match) return null;
  let value = parseInt(match[1], 10);
  if (Number.isNaN(value)) return null;
  if (value === 0) value = 36;
  return value;
};
const runwayPairLabel = runwayId => {
  const number = runwayNumber(runwayId);
  if (number === null) return runwayId || 'RWY';
  let reciprocal = number + 18;
  if (reciprocal > 36) reciprocal -= 36;
  const parts = [number, reciprocal].sort((a, b) => a - b).map(n => String(n).padStart(2, '0'));
  return `${parts[0]}/${parts[1]}`;
};
const groupRunwaysByPair = runways => {
  const map = new Map();
  runways.forEach(rw => {
    const label = runwayPairLabel(rw.runway);
    const surfaceKey = (rw.surface || 'unknown').toLowerCase();
    const key = `${label}__${surfaceKey}`;
    if (!map.has(key)) {
      map.set(key, { pairLabel: label, surface: rw.surface || 'unknown', members: [] });
    }
    map.get(key).members.push(rw);
  });
  return Array.from(map.values()).map(entry => {
    entry.members.sort((a, b) => {
      const aNum = runwayNumber(a.runway) ?? 0;
      const bNum = runwayNumber(b.runway) ?? 0;
      return aNum - bNum;
    });
    return entry;
  }).sort((a, b) => {
    if (a.pairLabel === b.pairLabel) {
      return (a.surface || '').localeCompare(b.surface || '');
    }
    return a.pairLabel.localeCompare(b.pairLabel);
  });
};
const componentFor = (speed, heading, direction) => {
  const diff = angleDiff(heading, direction);
  if (diff === null) return null;
  return speed * Math.cos((diff * Math.PI) / 180);
};
const worstComponentForSpeed = (speed, heading, wind) => {
  const spd = toNumber(speed);
  if (spd === null || Number.isNaN(spd) || heading === null || !wind) return null;
  const dirs = [];
  const nominal = normalizeDegrees(wind.direction);
  if (nominal !== null) dirs.push(nominal);
  const varFrom = normalizeDegrees(wind.var_from);
  const varTo = normalizeDegrees(wind.var_to);
  if (varFrom !== null && varTo !== null) {
    dirs.push(varFrom, varTo);
  }
  if (!dirs.length) {
    return wind.variable ? -spd : null;
  }
  let worstAngle = -1;
  let worstComponent = null;
  dirs.forEach(dir => {
    const diff = angleDiff(heading, dir);
    if (diff === null) return;
    if (diff > worstAngle) {
      worstAngle = diff;
      worstComponent = componentFor(spd, heading, dir);
    }
  });
  return worstComponent;
};
const formatComponentText = (base, gust) => {
  if (base === null || base === undefined || Number.isNaN(Number(base))) return 'N/A';
  const baseNum = Number(base);
  const magnitude = Math.abs(baseNum);
  const baseLabel = magnitude < 0.1 ? 'Calm' : baseNum >= 0 ? `Head ${magnitude.toFixed(1)} kt` : `Tail ${magnitude.toFixed(1)} kt`;
  if (gust === null || gust === undefined || Number.isNaN(Number(gust))) {
    return baseLabel;
  }
  const gustNum = Number(gust);
  const gustMag = Math.abs(gustNum);
  const gustLabel = gustMag < 0.1 ? 'Calm' : gustNum >= 0 ? `Head ${gustMag.toFixed(1)} kt` : `Tail ${gustMag.toFixed(1)} kt`;
  return gustLabel === 'Calm' ? baseLabel : `${baseLabel} (G ${gustLabel})`;
};
const describeWind = wind => {
  if (!wind) return 'Wind: N/A';
  const baseSpeed = toNumber(wind.speed_kt);
  const gustSpeed = toNumber(wind.gust_kt);
  if (baseSpeed === null && gustSpeed === null) return 'Wind: N/A';
  const directionText = wind.direction === null || wind.direction === undefined
    ? 'VRB'
    : `${String(Math.round(toNumber(wind.direction) ?? 0)).padStart(3, '0')}°`;
  let text = `Wind: ${directionText}/`;
  text += baseSpeed !== null ? `${baseSpeed.toFixed(0)} kt` : '—';
  if (gustSpeed !== null) {
    text += ` (G${gustSpeed.toFixed(0)} kt)`;
  }
  if (wind.var_from != null && wind.var_to != null) {
    const fromTxt = String(Math.round(toNumber(wind.var_from) ?? 0)).padStart(3, '0');
    const toTxt = String(Math.round(toNumber(wind.var_to) ?? 0)).padStart(3, '0');
    text += ` ${fromTxt}°-${toTxt}°`;
  }
  return text;
};
const describeField = (members, field) => {
  const values = members.map(m => m[field]).filter(v => v !== undefined && v !== null);
  if (!values.length) return 'N/A';
  const unique = [...new Set(values.map(v => `${v}`))];
  if (unique.length === 1) {
    return `${unique[0]} m`;
  }
  return members.map(m => `${m.runway}: ${m[field] ?? 'N/A'} m`).join(' / ');
};
const describeComponents = (members, wind) => {
  if (!wind) return 'N/A';
  const baseWindSpeed = toNumber(wind.speed_kt);
  const gustWindSpeed = toNumber(wind.gust_kt);
  const rows = members.map(m => {
    const heading = runwayHeading(m.runway);
    const baseComponent = worstComponentForSpeed(baseWindSpeed, heading, wind);
    const gustComponent = gustWindSpeed !== null ? worstComponentForSpeed(gustWindSpeed, heading, wind) : null;
    return { runway: m.runway, text: formatComponentText(baseComponent, gustComponent) };
  });
  const unique = [...new Set(rows.map(r => r.text))];
  if (unique.length === 1) {
    return unique[0];
  }
  return rows.map(r => `${r.runway}: ${r.text}`).join(' / ');
};
const individualRunwayWind = (runwayId, wind) => {
  if (!wind) return 'N/A';
  const baseSpeed = toNumber(wind.speed_kt);
  if (baseSpeed === null) return 'N/A';
  const heading = runwayHeading(runwayId);
  if (heading === null) return 'N/A';
  const gustSpeed = toNumber(wind.gust_kt);
  const baseComponent = worstComponentForSpeed(baseSpeed, heading, wind);
  const gustComponent = gustSpeed !== null ? worstComponentForSpeed(gustSpeed, heading, wind) : null;
  return formatComponentText(baseComponent, gustComponent);
};
const aircraftLabel = data.inputs.aircraft_name || data.inputs.aircraft_type || 'n/a';
const meta = `Departure: ${data.inputs.departure_icao} (${data.inputs.airport_name}) · Flight type: ${data.inputs.flight_type} · Aircraft: ${aircraftLabel} · ETA: ${data.inputs.estimated_time_hours}h · Generated ${new Date(data.generation_time).toLocaleString()}`;
qs('#meta').textContent = meta;
const warnings = data.warnings || [];
const warnEl = qs('#warnings');
warnings.forEach(msg => {
  const div = document.createElement('div');
  div.className = 'warning';
  div.textContent = msg;
  warnEl.appendChild(div);
});
const weatherContainer = qs('#weather-sections');
if (!airportSections.length) {
  weatherContainer.innerHTML = '<div class="warning">No airport weather data available.</div>';
} else {
  weatherContainer.innerHTML = airportSections.map((section, index) => renderWeatherSection(section, index)).join('');
}
const fuel = data.fuel;
const fuelRows = [
  `<tr><th>Rolling fuel (${fuel.rolling_label})</th><td>${fuel.rolling_liters} L</td></tr>`,
  `<tr><th>Trip fuel (${fuel.trip_label})</th><td>${fuel.trip_liters} L</td></tr>`
];
if (Number(fuel.climb_liters) > 0) {
  const climbLabel = fuel.climb_label || 'Climb allowance';
  fuelRows.push(`<tr><th>${climbLabel}</th><td>${fuel.climb_liters} L</td></tr>`);
}
fuelRows.push(
  `<tr><th>Reserve (${fuel.reserve_label})</th><td>${fuel.reserve_liters} L</td></tr>`,
  `<tr class="total-row"><th>Total fuel</th><td><strong>${fuel.total_liters} L</strong></td></tr>`
);
qs('#fuel-table').innerHTML = `<table>${fuelRows.join('')}</table>`;
const mass = data.mass_balance;
if (mass.gyrocopter) {
  const okIcon = v => v ? '<span class="status-ok">✓</span>' : '<span class="status-fail">✗</span>';
  let srows = mass.stations.map(st => `<tr><td>${st.label}</td><td>${st.mass.toFixed(1)} kg</td></tr>`).join('');
  srows += `<tr class="total-row"><td><strong>Total</strong></td><td><strong>${mass.total_mass} kg</strong></td></tr>`;
  const checks = [
    { label: 'Total vs MTOW', value: `${mass.total_mass} kg / ${mass.mtow} kg max`, ok: mass.within_mtow },
    { label: 'LH seat — Pilot', value: `${mass.lh_station_weight} kg / ${mass.max_seat_weight} kg max`, ok: mass.lh_ok },
    { label: 'RH seat — Passenger', value: `${mass.rh_station_weight} kg / ${mass.max_seat_weight} kg max`, ok: mass.rh_ok },
    { label: 'Total cockpit', value: `${mass.cockpit_weight} kg (min ${mass.min_cockpit_weight} / max ${mass.max_cockpit_weight} kg)`, ok: mass.cockpit_ok },
    { label: 'Baggage (total storage)', value: `${mass.baggage_weight} kg / ${mass.max_storage_weight * 2} kg max`, ok: mass.baggage_ok },
  ];
  let crows = checks.map(c => `<tr><td>${c.label}</td><td>${c.value}</td><td>${okIcon(c.ok)}</td></tr>`).join('');
  qs('#mass-table').innerHTML = `<table><tr><th>Station</th><th>Mass</th></tr>${srows}</table>
    <h3>Weight Limits</h3>
    <table><tr><th>Check</th><th>Value</th><th></th></tr>${crows}</table>
    <p style="font-size:0.85rem;color:#666;margin-top:0.5rem;">Note: storage compartment weight counts against its seat station limit (max ${mass.max_seat_weight} kg per station incl. storage behind it).</p>`;
  document.getElementById('cg-chart').style.display = 'none';
} else {
  let rows = mass.stations.map(st => `<tr><td>${st.label}</td><td>${st.mass.toFixed(1)}</td><td>${st.lever.toFixed(3)}</td><td>${st.moment.toFixed(1)}</td></tr>`).join('');
  rows += `<tr><th>Total</th><th>${mass.total_mass}</th><th></th><th>${mass.total_moment}</th></tr>`;
  qs('#mass-table').innerHTML = `<table><tr><th>Station</th><th>Mass (kg)</th><th>Lever (m)</th><th>Moment (kg·m)</th></tr>${rows}</table>`;
  const cgInfo = document.createElement('p');
  cgInfo.textContent = `CG: ${mass.cg.toFixed(3)} m`;
  qs('#mass-table').appendChild(cgInfo);
}
const runwayHtml = airportSections.length
  ? airportSections.map((section, index) => renderRunwaySection(section, index)).join('')
  : '<div class="warning">No runway data stored.</div>';
qs('#performance').innerHTML = runwayHtml;
function buildMetarTable(entries) {
  let rows = '<tr><th>Station</th><th>Distance (km)</th><th>Observed</th><th>METAR</th></tr>';
  entries.forEach(entry => {
    rows += `<tr><td>${entry.station || 'N/A'}</td><td>${entry.distance_km ?? '—'}</td><td>${entry.obs_time || 'N/A'}</td><td><code>${entry.raw || ''}</code></td></tr>`;
  });
  return `<table class="metar-table">${rows}</table>`;
}
function buildTafTable(entries) {
  let rows = '<tr><th>Station</th><th>Distance (km)</th><th>Issued</th><th>TAF</th></tr>';
  entries.forEach(entry => {
    rows += `<tr><td>${entry.station || 'N/A'}</td><td>${entry.distance_km ?? '—'}</td><td>${entry.issue || 'N/A'}</td><td><code>${entry.raw || ''}</code></td></tr>`;
  });
  return `<table class="metar-table">${rows}</table>`;
}
function renderWeatherSection(section, index) {
  const roleLabel = section.role || (index === 0 ? 'Departure' : `Stop ${index}`);
  const metarEntries = (section.weather && section.weather.metar_entries) || [];
  const tafEntries = (section.weather && section.weather.taf_entries) || [];
  const qnhText = section.weather && section.weather.lowest_qnh !== null && section.weather.lowest_qnh !== undefined
    ? section.weather.lowest_qnh
    : 'N/A';
  const oatText = section.weather && section.weather.oat_c !== null && section.weather.oat_c !== undefined
    ? section.weather.oat_c
    : 'N/A';
  const metarTable = metarEntries.length ? buildMetarTable(metarEntries) : '<div class="warning">No METAR data available.</div>';
  const tafTable = tafEntries.length ? buildTafTable(tafEntries) : '<div class="warning">No TAF data available.</div>';
  return `
    <div class="airport-weather">
      <h4>${section.icao} — ${section.name} (${roleLabel})</h4>
      <p class="weather-info">QNH: ${qnhText} hPa · OAT: ${oatText} °C · ${describeWind(section.weather ? section.weather.wind : null)}</p>
      <div class="weather-flex">
        <div>${metarTable}</div>
        <div>${tafTable}</div>
      </div>
    </div>`;
}
function renderRunwaySection(section, index) {
  const runways = section.runways || [];
  const roleLabel = section.role || (index === 0 ? 'Departure' : `Stop ${index}`);
  const wind = section.weather ? section.weather.wind : null;
  const perf = section.performance || {};

  // Performance summary line
  const qnhText  = (section.weather && section.weather.lowest_qnh != null) ? `${section.weather.lowest_qnh} hPa` : 'N/A';
  const elevText  = section.elevation_ft != null ? `${section.elevation_ft} ft` : 'N/A';
  const oatText   = (section.weather && section.weather.oat_c != null) ? `${section.weather.oat_c} °C` : 'N/A';
  const windLine  = describeWind(wind);
  const deltaText = perf.delta_isa_c != null ? `${perf.delta_isa_c > 0 ? '+' : ''}${perf.delta_isa_c} °C` : 'N/A';
  const perfSummaryLine = `QNH: ${qnhText} · Field elevation: ${elevText} · Temperature: ${oatText} · ${windLine} · ΔISA: ${deltaText}`;

  const paText = perf.pressure_altitude_ft != null ? `${perf.pressure_altitude_ft} ft` : 'N/A';
  const daText = perf.density_altitude_ft  != null ? `${perf.density_altitude_ft} ft`  : 'N/A';
  const perfTable = `<table class="runway-table">
    <tr><th>Pressure altitude</th><td>${paText}</td></tr>
    <tr><th>Density altitude</th><td>${daText}</td></tr>
  </table>`;

  if (!runways.length) {
    return `<div class="runway-block">
      <h4>${section.icao} — ${section.name} (${roleLabel})</h4>
      <p class="weather-info">${perfSummaryLine}</p>
      ${perfTable}
      <p>No runway data stored.</p>
    </div>`;
  }

  // Sort by runway number then surface; each direction is its own row
  const sorted = [...runways].sort((a, b) => {
    const aNum = runwayNumber(a.runway) ?? 0;
    const bNum = runwayNumber(b.runway) ?? 0;
    if (aNum !== bNum) return aNum - bNum;
    return (a.surface || '').localeCompare(b.surface || '');
  });
  let runwayRows = '<tr><th>Runway</th><th>Surface</th><th>TORA (m)</th><th>LDA (m)</th><th>Head/Tailwind</th></tr>';
  sorted.forEach(rw => {
    const windText = individualRunwayWind(rw.runway, wind);
    runwayRows += `<tr><td>${rw.runway}</td><td>${rw.surface || 'unknown'}</td><td>${rw.tora ?? 'N/A'} m</td><td>${rw.lda ?? 'N/A'} m</td><td>${windText}</td></tr>`;
  });
  return `
    <div class="runway-block">
      <h4>${section.icao} — ${section.name} (${roleLabel})</h4>
      <p class="weather-info">${perfSummaryLine}</p>
      ${perfTable}
      <table class="runway-table">${runwayRows}</table>
    </div>`;
}
if (!mass.gyrocopter) {
const canvas = document.getElementById('cg-chart');
const ctx = canvas.getContext('2d');
const poly = data.mass_balance.envelope || [
  {mass:560,moment:240},{mass:560,moment:290},{mass:750,moment:390},{mass:750,moment:320}
];
const points = poly.concat([poly[0]]);
const massMin = 550;
const massMax = 760;
const momentMin = 230;
const momentMax = 420;
const margin = { left: 70, right: 40, top: 30, bottom: 60 };
const usableWidth = canvas.width - margin.left - margin.right;
const usableHeight = canvas.height - margin.top - margin.bottom;
const unitSize = Math.min(usableWidth / (momentMax - momentMin), usableHeight / (massMax - massMin));
const plotWidth = unitSize * (momentMax - momentMin);
const plotHeight = unitSize * (massMax - massMin);
const originX = margin.left + (usableWidth - plotWidth) / 2;
const originY = margin.top + plotHeight;
const scaleX = moment => originX + (moment - momentMin) * unitSize;
const scaleY = mass => originY - (mass - massMin) * unitSize;
ctx.clearRect(0,0,canvas.width,canvas.height);
ctx.strokeStyle = '#edf2fb';
ctx.lineWidth = 1;
for (let moment = momentMin; moment <= momentMax; moment += 10) {
  const x = scaleX(moment);
  ctx.beginPath();
  ctx.moveTo(x, originY);
  ctx.lineTo(x, originY - plotHeight);
  ctx.stroke();
}
for (let massValue = massMin; massValue <= massMax; massValue += 10) {
  const y = scaleY(massValue);
  ctx.beginPath();
  ctx.moveTo(originX, y);
  ctx.lineTo(originX + plotWidth, y);
  ctx.stroke();
}
ctx.strokeStyle = '#8d99ae';
ctx.lineWidth = 1.5;
ctx.strokeRect(originX, originY - plotHeight, plotWidth, plotHeight);
ctx.fillStyle = '#1b263b';
ctx.font = '16px system-ui';
const momentTicks = [];
for (let val = momentMin; val <= momentMax; val += 10) momentTicks.push(val);
const massTicks = [];
for (let val = massMin; val <= massMax; val += 10) massTicks.push(val);
momentTicks.forEach(val => {
  const x = scaleX(val);
  ctx.fillText(val.toString(), x - 18, originY + 28);
  ctx.fillText(val.toString(), x - 18, originY - plotHeight - 12);
});
massTicks.forEach(val => {
  const y = scaleY(val);
  ctx.fillText(val.toString(), originX - 60, y + 6);
  ctx.fillText(val.toString(), originX + plotWidth + 20, y + 6);
});
ctx.fillStyle = '#0d1b2a';
ctx.font = '20px system-ui';
ctx.fillText('Moment (kg·m)', canvas.width / 2 - 80, canvas.height - 20);
ctx.save();
ctx.translate(margin.left / 2, canvas.height / 2);
ctx.rotate(-Math.PI / 2);
ctx.fillText('Mass (kg)', 0, 0);
ctx.restore();
ctx.strokeStyle = '#415a77';
ctx.lineWidth = 2;
ctx.beginPath();
ctx.moveTo(scaleX(points[0].moment), scaleY(points[0].mass));
for (let i=1;i<points.length;i++) {
  ctx.lineTo(scaleX(points[i].moment), scaleY(points[i].mass));
}
ctx.closePath();
ctx.stroke();
ctx.fillStyle = 'rgba(65,90,119,0.15)';
ctx.fill();
ctx.fillStyle = mass.inside_envelope ? '#2b9348' : '#d90429';
const cgX = scaleX(mass.total_moment);
const cgY = scaleY(mass.total_mass);
ctx.beginPath();
ctx.arc(cgX, cgY, 6, 0, Math.PI*2);
ctx.fill();
} // end if (!mass.gyrocopter)
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
