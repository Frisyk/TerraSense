import os
import psycopg2

PROD_URL = os.environ.get(
    "TARGET_DB_URL",
    "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
)

print("Menguji DDL di Supabase...")
conn = psycopg2.connect(PROD_URL)
cur = conn.cursor()

cur.execute("CREATE SCHEMA IF NOT EXISTS monitoring;")

cur.execute("""
CREATE TABLE IF NOT EXISTS monitoring.climate_grid_kalbar (
    id SERIAL PRIMARY KEY,
    grid_code TEXT UNIQUE NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    grid_step DOUBLE PRECISION NOT NULL DEFAULT 0.25,
    source_mode TEXT NOT NULL DEFAULT 'era5_grid',
    geom geometry(Point, 4326)
);
CREATE INDEX IF NOT EXISTS idx_climate_grid_kalbar_geom ON monitoring.climate_grid_kalbar USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_climate_grid_kalbar_latlon ON monitoring.climate_grid_kalbar (lat, lon);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS monitoring.climate_history (
    grid_id INTEGER NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    time_utc TIMESTAMPTZ NOT NULL,
    time_wib TIMESTAMP NOT NULL,
    time_wita TIMESTAMP NOT NULL,
    time_wit TIMESTAMP NOT NULL,
    drought_code DOUBLE PRECISION,
    duff_moisture_code DOUBLE PRECISION,
    fine_fuel_moisture_code DOUBLE PRECISION,
    initial_spread_index DOUBLE PRECISION,
    build_up_index DOUBLE PRECISION,
    fire_weather_index DOUBLE PRECISION,
    daily_severity_index DOUBLE PRECISION,
    ffmc_category TEXT,
    dmc_category TEXT,
    dc_category TEXT,
    bui_category TEXT,
    isi_category TEXT,
    fwi_category TEXT,
    visibility INTEGER,
    temp DOUBLE PRECISION,
    feels_like DOUBLE PRECISION,
    temp_min DOUBLE PRECISION,
    temp_max DOUBLE PRECISION,
    pressure INTEGER,
    sea_level_pressure INTEGER,
    grnd_level_pressure INTEGER,
    humidity INTEGER,
    humidity_category TEXT,
    wind_speed DOUBLE PRECISION,
    wind_deg INTEGER,
    wind_to_deg INTEGER,
    wind_compass TEXT,
    wind_u DOUBLE PRECISION,
    wind_v DOUBLE PRECISION,
    wind_gust DOUBLE PRECISION,
    wind_speed_category TEXT,
    cloud_cover_percent INTEGER,
    weather_code_name TEXT,
    weather_code_description TEXT,
    weather_code_icon TEXT,
    precipitation_mm DOUBLE PRECISION,
    rain_24h_mm DOUBLE PRECISION,
    PRIMARY KEY (grid_id, time_utc)
);
CREATE INDEX IF NOT EXISTS idx_climate_history_time_utc ON monitoring.climate_history (time_utc);
CREATE INDEX IF NOT EXISTS idx_climate_history_latlon ON monitoring.climate_history (lat, lon);
""")

conn.commit()
print("✓ Tabel monitoring.climate_grid_kalbar dan monitoring.climate_history berhasil dibuat di Supabase!")

cur.execute("SELECT to_regclass('monitoring.climate_history');")
print("Regclass:", cur.fetchone()[0])
conn.close()
