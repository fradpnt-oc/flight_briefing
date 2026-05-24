-- ============================================================
-- seed.sql — pre-populated from existing flight_briefing.db
-- Applied once on first container start via init_db.py
-- ============================================================

CREATE TABLE IF NOT EXISTS pilot (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    name    TEXT    NOT NULL,
    height  REAL    NOT NULL,
    weight  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS passengers (
    name    TEXT PRIMARY KEY,
    weight  REAL NOT NULL,
    height  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS airport_profiles (
    icao         TEXT PRIMARY KEY,
    elevation_ft REAL NOT NULL,
    name         TEXT
);

CREATE TABLE IF NOT EXISTS airport_runways (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    icao    TEXT NOT NULL,
    runway  TEXT NOT NULL,
    surface TEXT NOT NULL,
    tora    REAL NOT NULL,
    lda     REAL NOT NULL,
    FOREIGN KEY (icao) REFERENCES airport_profiles(icao) ON DELETE CASCADE
);

-- Pilot
INSERT OR IGNORE INTO pilot (id, name, height, weight)
    VALUES (1, 'Francois', 178.0, 80.0);

-- Passengers
INSERT OR IGNORE INTO passengers (name, weight, height) VALUES ('Gabi',     60.0, 165.0);
INSERT OR IGNORE INTO passengers (name, weight, height) VALUES ('Eric',     68.0, 180.0);
INSERT OR IGNORE INTO passengers (name, weight, height) VALUES ('Éric',     80.0, 175.0);
INSERT OR IGNORE INTO passengers (name, weight, height) VALUES ('Wolfgang', 70.0, 180.0);

-- Airport profiles
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDEF', 436.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDEL', 295.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFA', 1102.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFB', 398.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFC', 410.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFE', 385.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFG', 413.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFM', 309.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFO', 1143.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFU', 1500.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFV', 295.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFX', 315.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDFZ', 760.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDGP', 279.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDGX', 346.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDLE', 424.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft, name) VALUES ('EDRF', 351.0, 'Mainbullau');
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('EDRY', 312.0);
INSERT OR IGNORE INTO airport_profiles (icao, elevation_ft) VALUES ('LFRI', 299.0);

-- Airport runways
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDEF', '06/24', 'asphalt',   671.0,  671.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDEL', '01',    'grass',     450.0,  450.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDEL', '19',    'grass',     450.0,  450.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFA', '06',    'grass',     640.0,  580.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFA', '24',    'grass',     580.0,  640.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFB', '18',    'asphalt',  1300.0, 1230.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFB', '36',    'asphalt',  1230.0, 1300.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFC', '08',    'asphalt',   665.0,  636.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFC', '08',    'grass',     597.0,  566.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFC', '26',    'asphalt',   636.0,  665.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFC', '26',    'grass',     566.0,  597.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFE', '08',    'asphalt',  1166.0, 1400.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFE', '26',    'asphalt',  1400.0, 1166.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFG', '07',    'grass',     740.0,  840.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFG', '25',    'grass',     840.0,  740.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFM', '09',    'asphalt',  1066.0, 1066.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFM', '09L',   'grass',     700.0,  700.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFM', '27',    'asphalt',  1066.0, 1066.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFM', '27R',   'grass',     700.0,  700.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFO', '08',    'asphalt',   570.0,  540.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFO', '26',    'asphalt',   574.0,  540.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFU', '05',    'asphalt',   675.0,  675.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFU', '05',    'grass',     450.0,  580.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFU', '23',    'asphalt',   675.0,  675.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFU', '23',    'grass',     580.0,  450.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFV', '06',    'asphalt',   800.0,  800.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFV', '06',    'grass',     920.0,  920.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFV', '24',    'asphalt',   800.0,  800.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFV', '24',    'grass',     920.0,  920.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFX', '14',    'grass',     840.0,  820.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFX', '32',    'grass',     820.0,  840.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFZ', '07',    'concrete', 1000.0, 1000.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFZ', '07',    'grass',    1000.0, 1000.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFZ', '25',    'concrete', 1000.0, 1000.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDFZ', '25',    'grass',    1000.0, 1000.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDGP', '01',    'grass',     870.0,  870.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDGP', '19',    'grass',     870.0,  870.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDGX', '18',    'grass',     651.0,  463.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDGX', '36',    'grass',     463.0,  505.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDLE', '06',    'asphalt',  1200.0, 1553.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDLE', '24',    'asphalt',  1553.0, 1200.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDRF', '09',    'asphalt',   480.0,  600.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDRF', '27',    'asphalt',   600.0,  480.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDRY', '16',    'asphalt',  1400.0, 1400.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('EDRY', '34',    'asphalt',  1400.0, 1400.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('LFRI', '10',    'asphalt',  1550.0, 1550.0);
INSERT OR IGNORE INTO airport_runways (icao, runway, surface, tora, lda) VALUES ('LFRI', '28',    'asphalt',  1550.0, 1260.0);
