#!/usr/bin/env python3
"""
Backfill historis data iklim, arah angin, dan Fire Weather Index (FWI)
untuk wilayah Kalimantan Barat menggunakan Open-Meteo Historical Weather API.

Sumber Data & Resolusi Open-Meteo Archive (v1/archive):
  - ERA5-Land / ECMWF IFS (2017–sekarang): Resolusi spasial 0.1° (~9–11 km di ekuator)
    untuk suhu 2m, kelembapan relatif 2m, dan presipitasi.
  - ERA5 Reanalysis (1940–sekarang): Resolusi spasial 0.25° (~25–28 km di ekuator)
    untuk vektor angin 10m (kecepatan, arah, hembusan/gust), tekanan udara, dan awan.
  - Resolusi Temporal: 1 jam (hourly), disimpan pada jam sinoptik (default: 00, 06, 12, 18 UTC)
    atau seluruh 24 jam (--all-hours).

Fitur Utama Penyesuaian Kalimantan Barat:
  1. Otomatis membuat & mengisi tabel grid Kalimantan Barat (`monitoring.climate_grid_kalbar`)
     dalam BBOX Kalbar (108.5° BT - 114.3° BT, -3.2° LS - 2.3° LU) dengan resolusi native 0.25° (~27.5 km)
     atau mode `--mode hotspots` (mengambil titik pusat kejadian api dari tabel VIIRS raw).
  2. Mendukung Batch Request multi-koordinat Open-Meteo (hingga 25 titik sekaligus per request)
     sehingga jauh lebih cepat dan hemat kuota API.
  3. Perhitungan FWI (Van Wagner 1985) yang STATEFUL (meneruskan FFMC, DMC, DC dari hari ke hari
     beserta akumulasi hujan 24 jam), serta menghitung komponen vektor angin (u, v) dan arah dorong api.
  4. Menyimpan ke `monitoring.climate_history` (idempotent via ON CONFLICT DO UPDATE) dan kompatibel
     dengan PostgreSQL lokal maupun Remote Supabase.

Contoh Penggunaan:
  python scripts/backfill_climate_history.py --start 2026-09-01 --end 2026-11-01
  python scripts/backfill_climate_history.py --start 2026-10-01 --end 2026-11-01 --grid-step 0.5
  python scripts/backfill_climate_history.py --mode hotspots --start 2026-09-01 --end 2026-11-01
"""

import argparse
import datetime
import math
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import requests

# --------------------------------------------------------------------------
# Konfigurasi Wilayah Kalimantan Barat & Database
# --------------------------------------------------------------------------
BBOX_KALBAR = (108.5, -3.2, 114.3, 2.3)  # (lon_min, lat_min, lon_max, lat_max)
SCHEMA = "monitoring"
GRID_TABLE = "climate_grid_kalbar"
HISTORY_TABLE = "climate_history"

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def load_env_db_url() -> str:
    """Membaca DATABASE_URL dari environment atau file .env di root project."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        env_vars = {}
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip().strip('"').strip("'")
        if "DATABASE_URL" in env_vars:
            # Jika dijalankan di dalam container docker fire_backend dan menunjuk ke localhost:5434,
            # arahkan otomatis ke container fire_postgis_db:5432
            url = env_vars["DATABASE_URL"]
            if os.path.exists("/.dockerenv") and "localhost:5434" in url:
                url = url.replace("localhost:5434", "fire_postgis_db:5432")
            return url

    if os.path.exists("/.dockerenv"):
        return "postgresql://postgres:postgres@fire_postgis_db:5432/geonode_data"
    return "postgresql://postgres:postgres@localhost:5434/geonode_data"


# --------------------------------------------------------------------------
# Pemetaan Kode Cuaca WMO & Kategori Indeks
# --------------------------------------------------------------------------
def map_wmo_code(code) -> Tuple[str, str, str]:
    """Memetakan WMO weather code dari Open-Meteo ke nama, deskripsi, dan kode ikon."""
    mapping = {
        0: ("Clear", "cerah / langit bersih", "01d"),
        1: ("Clouds", "cerah berawan", "02d"),
        2: ("Clouds", "berawan sebagian", "03d"),
        3: ("Clouds", "mendung / tertutup awan", "04d"),
        45: ("Fog", "kabut / asap tipis", "50d"),
        48: ("Fog", "kabut tebal", "50d"),
        51: ("Drizzle", "gerimis ringan", "09d"),
        53: ("Drizzle", "gerimis sedang", "09d"),
        55: ("Drizzle", "gerimis rapat", "09d"),
        56: ("Drizzle", "gerimis dingin ringan", "09d"),
        57: ("Drizzle", "gerimis dingin rapat", "09d"),
        61: ("Rain", "hujan ringan", "10d"),
        63: ("Rain", "hujan sedang", "10d"),
        65: ("Rain", "hujan lebat", "10d"),
        66: ("Rain", "hujan dingin ringan", "13d"),
        67: ("Rain", "hujan dingin lebat", "13d"),
        80: ("Rain", "hujan lokal ringan", "09d"),
        81: ("Rain", "hujan lokal sedang", "09d"),
        82: ("Rain", "hujan deras / badai", "09d"),
        95: ("Thunderstorm", "hujan petir", "11d"),
        96: ("Thunderstorm", "hujan petir disertai angin kencang", "11d"),
        99: ("Thunderstorm", "badai petir ekstrem", "11d"),
    }
    try:
        c = int(code) if code is not None and not pd.isna(code) else 0
    except Exception:
        c = 0
    return mapping.get(c, ("Clouds", "berawan", "03d"))


def categorize_humidity_val(h) -> str:
    if h is None or pd.isna(h):
        return "Moderate"
    if h < 45:
        return "Dry"
    elif h < 70:
        return "Moderate"
    return "Damp"


def categorize_wind_speed_val(w) -> str:
    """Skala Beaufort (w dalam m/s)."""
    if w is None or pd.isna(w):
        return "Light breeze"
    if w < 1.5:
        return "Light air"
    elif w < 3.3:
        return "Light breeze"
    elif w < 5.5:
        return "Gentle breeze"
    elif w < 8.0:
        return "Moderate breeze"
    elif w < 10.8:
        return "Fresh breeze"
    elif w < 13.9:
        return "Strong breeze"
    elif w < 17.2:
        return "Moderate gale"
    elif w < 20.7:
        return "Fresh gale"
    elif w < 24.5:
        return "Strong gale"
    elif w < 28.4:
        return "Whole gale"
    elif w < 32.6:
        return "Storm"
    return "Hurricane"


def categorize_fwi_val(val, bins, labels) -> str:
    if val is None or pd.isna(val):
        return labels[0]
    for i in range(len(bins) - 1):
        if bins[i] <= val < bins[i + 1]:
            return labels[i]
    return labels[-1]


def compass_dir_id(deg: float) -> str:
    """Konversi derajat (0-360) ke arah mata angin bahasa Indonesia."""
    dirs = ["U", "TL", "T", "TG", "S", "BD", "B", "BL"]
    ix = int(round((deg % 360) / 45.0)) % 8
    return dirs[ix]


# --------------------------------------------------------------------------
# Perhitungan Canadian Forest Fire Weather Index (Van Wagner 1985)
# --------------------------------------------------------------------------
def calculate_fwi(
    temp: float,
    rh: float,
    wind_kmh: float,
    rain_24h: float,
    ffmc_prev: float = 85.0,
    dmc_prev: float = 20.0,
    dc_prev: float = 200.0,
) -> Dict[str, float]:
    """
    Menghitung komponen Canadian Forest Fire Weather Index (FWI) (Van Wagner 1985).
    Disesuaikan dengan faktor panjang hari ekuator Kalimantan (Le=9.0, Lf=1.39).
    """
    try:
        rh = max(1.0, min(100.0, float(rh)))
        wind_kmh = max(0.0, float(wind_kmh))
        rain_24h = max(0.0, float(rain_24h))

        # 1. Fine Fuel Moisture Code (FFMC)
        m0 = 147.2 * (101.0 - ffmc_prev) / (59.5 + ffmc_prev)
        if rain_24h > 0.5:
            rf = rain_24h - 0.5
            if m0 <= 150.0:
                mr = m0 + 42.5 * rf * math.exp(-100.0 / (251.0 - m0)) * (1.0 - math.exp(-6.93 / rf))
            else:
                mr = (
                    m0
                    + 42.5 * rf * math.exp(-100.0 / (251.0 - m0)) * (1.0 - math.exp(-6.93 / rf))
                    + 0.0015 * ((m0 - 150.0) ** 2) * math.sqrt(rf)
                )
            m0 = min(250.0, mr)

        ed = (
            0.942 * (rh ** 0.679)
            + 11.0 * math.exp((rh - 100.0) / 10.0)
            + 0.18 * (21.1 - temp) * (1.0 - math.exp(-0.115 * rh))
        )
        if m0 > ed:
            k0 = 0.424 * (1.0 - ((rh / 100.0) ** 1.7)) + 0.0694 * math.sqrt(wind_kmh) * (
                1.0 - ((rh / 100.0) ** 8)
            )
            kd = k0 * 0.581 * math.exp(0.0365 * temp)
            m = ed + (m0 - ed) * (10.0 ** (-kd))
        else:
            ew = (
                0.618 * (rh ** 0.753)
                + 10.0 * math.exp((rh - 100.0) / 10.0)
                + 0.18 * (21.1 - temp) * (1.0 - math.exp(-0.115 * rh))
            )
            if m0 < ew:
                kl = 0.424 * (1.0 - (((100.0 - rh) / 100.0) ** 1.7)) + 0.0694 * math.sqrt(wind_kmh) * (
                    1.0 - (((100.0 - rh) / 100.0) ** 8)
                )
                kw = kl * 0.581 * math.exp(0.0365 * temp)
                m = ew - (ew - m0) * (10.0 ** (-kw))
            else:
                m = m0

        ffmc = 59.5 * (250.0 - m) / (147.2 + m)
        ffmc = max(0.0, min(101.0, ffmc))

        # 2. Duff Moisture Code (DMC)
        le = 9.0
        p0 = dmc_prev
        if rain_24h > 1.5:
            ra = 0.92 * rain_24h - 1.27
            w0 = 20.0 + math.exp(5.634 - p0 / 43.43)
            if p0 <= 33.0:
                b = 100.0 / (0.5 + 0.3 * p0)
            elif p0 <= 65.0:
                b = 14.0 - 1.3 * math.log(p0)
            else:
                b = 6.2 * math.log(p0) - 17.2
            wr = w0 + 1000.0 * ra / (48.77 + b * ra)
            pr = 244.72 - 43.43 * math.log(max(0.001, wr - 20.0))
            p0 = max(0.0, pr)

        k_dmc = 1.894 * (temp + 1.1) * (100.0 - rh) * le * 1e-6 if temp > -1.1 else 0.0
        dmc = max(0.0, p0 + 100.0 * k_dmc)

        # 3. Drought Code (DC) — Indikator kekeringan gambut dalam
        lf = 1.39
        d0 = dc_prev
        if rain_24h > 2.8:
            rd = 0.83 * rain_24h - 1.27
            q0 = 800.0 * math.exp(-d0 / 400.0)
            qr = q0 + 3.937 * rd
            dr = 400.0 * math.log(800.0 / max(0.001, qr))
            d0 = max(0.0, dr)
        v = 0.36 * (temp + 2.8) + lf if temp > -2.8 else lf
        dc = max(0.0, d0 + 0.5 * max(0.0, v))

        # 4. Initial Spread Index (ISI) — Menggabungkan FFMC & Kecepatan Angin
        fw = math.exp(0.05039 * wind_kmh)
        m_isi = 147.2 * (101.0 - ffmc) / (59.5 + ffmc)
        ff = 91.9 * math.exp(-0.1386 * m_isi) * (1.0 + (m_isi ** 5.31) / 4.93e7)
        isi = 0.208 * fw * ff

        # 5. Build Up Index (BUI)
        if dmc <= 0.4 * dc:
            bui = 0.8 * dmc * dc / (dmc + 0.4 * dc) if (dmc + 0.4 * dc) > 0 else 0.0
        else:
            bui = (
                dmc - (1.0 - 0.8 * dc / (dmc + 0.4 * dc)) * (0.92 + (0.0114 * dmc) ** 1.7)
                if (dmc + 0.4 * dc) > 0
                else 0.0
            )
        bui = max(0.0, bui)

        # 6. Fire Weather Index (FWI)
        if bui <= 80.0:
            fd = 0.626 * (bui ** 0.809) + 2.0
        else:
            fd = 1000.0 / (25.0 + 108.64 * math.exp(-0.023 * bui))
        bb = 0.1 * isi * fd
        if bb > 1.0:
            fwi = math.exp(2.72 * ((0.434 * math.log(bb)) ** 0.647))
        else:
            fwi = bb

        # 7. Daily Severity Index (DSI)
        dsi = 0.0272 * (fwi ** 1.77)

        return {
            "drought_code": round(dc, 2),
            "duff_moisture_code": round(dmc, 2),
            "fine_fuel_moisture_code": round(ffmc, 2),
            "initial_spread_index": round(isi, 2),
            "build_up_index": round(bui, 2),
            "fire_weather_index": round(fwi, 2),
            "daily_severity_index": round(dsi, 2),
        }
    except Exception as e:
        print(f"Error calculating FWI: {e}")
        return {
            "drought_code": dc_prev,
            "duff_moisture_code": dmc_prev,
            "fine_fuel_moisture_code": ffmc_prev,
            "initial_spread_index": 3.0,
            "build_up_index": 30.0,
            "fire_weather_index": 7.0,
            "daily_severity_index": 1.0,
        }


# --------------------------------------------------------------------------
# Inisialisasi Tabel & Pembuatan Grid Kalimantan Barat
# --------------------------------------------------------------------------
def ensure_tables(conn):
    """Membuat tabel grid dan tabel climate_history di skema monitoring jika belum ada."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.{GRID_TABLE} (
                id SERIAL PRIMARY KEY,
                grid_code TEXT UNIQUE NOT NULL,
                lat DOUBLE PRECISION NOT NULL,
                lon DOUBLE PRECISION NOT NULL,
                grid_step DOUBLE PRECISION NOT NULL DEFAULT 0.25,
                source_mode TEXT NOT NULL DEFAULT 'era5_grid',
                geom geometry(Point, 4326)
            );
            CREATE INDEX IF NOT EXISTS idx_{GRID_TABLE}_geom ON {SCHEMA}.{GRID_TABLE} USING GIST (geom);
            CREATE INDEX IF NOT EXISTS idx_{GRID_TABLE}_latlon ON {SCHEMA}.{GRID_TABLE} (lat, lon);
            """
        )

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.{HISTORY_TABLE} (
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
            CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_time_utc ON {SCHEMA}.{HISTORY_TABLE} (time_utc);
            CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_latlon ON {SCHEMA}.{HISTORY_TABLE} (lat, lon);
            """
        )
    conn.commit()


def build_or_load_kalbar_grid(
    conn,
    mode: str = "grid",
    grid_step: float = 0.25,
    start_date_str: str = None,
    end_date_str: str = None,
) -> List[Dict]:
    """
    Menghasilkan titik-titik pengambilan cuaca untuk Kalimantan Barat:
      - mode='grid': Grid reguler mencakup BBOX Kalbar sesuai resolusi ERA5 (default 0.25° ≈ 27.5 km, atau 0.5°).
      - mode='hotspots': Mengambil titik-titik sel 0.25° di Kalimantan Barat yang memiliki aktivitas hotspot VIIRS
        pada periode tersebut (jauh lebih cepat dan langsung fokus pada area kebakaran).
    """
    lon_min, lat_min, lon_max, lat_max = BBOX_KALBAR
    points = []

    with conn.cursor() as cur:
        if mode == "hotspots" and start_date_str and end_date_str:
            # Ambil sel 0.25 derajat unik di Kalbar yang memiliki deteksi titik api VIIRS
            cur.execute(
                f"""
                SELECT DISTINCT
                    ROUND((latitude / %s)::numeric, 0) * %s AS cell_lat,
                    ROUND((longitude / %s)::numeric, 0) * %s AS cell_lon
                FROM monitoring.alert_fire_viirssnpp_raw
                WHERE datetm >= %s::timestamptz
                  AND datetm < (%s::timestamptz + interval '1 day')
                  AND latitude BETWEEN %s AND %s
                  AND longitude BETWEEN %s AND %s
                ORDER BY cell_lat, cell_lon;
                """,
                (grid_step, grid_step, grid_step, grid_step, start_date_str, end_date_str, lat_min, lat_max, lon_min, lon_max),
            )
            rows = cur.fetchall()
            for r_lat, r_lon in rows:
                lat_v = round(float(r_lat), 4)
                lon_v = round(float(r_lon), 4)
                code = f"KB_{lat_v:.2f}_{lon_v:.2f}"
                points.append((code, lat_v, lon_v, grid_step, "hotspot_cell"))

        if not points:
            # Bangun grid reguler di atas BBOX Kalimantan Barat
            lats = np.arange(lat_min, lat_max + 1e-6, grid_step)
            lons = np.arange(lon_min, lon_max + 1e-6, grid_step)
            for la in lats:
                for lo in lons:
                    lat_v = round(float(la), 4)
                    lon_v = round(float(lo), 4)
                    code = f"KB_{lat_v:.2f}_{lon_v:.2f}"
                    points.append((code, lat_v, lon_v, grid_step, "era5_grid"))

        # Upsert ke tabel monitoring.climate_grid_kalbar
        execute_values(
            cur,
            f"""
            INSERT INTO {SCHEMA}.{GRID_TABLE} (grid_code, lat, lon, grid_step, source_mode, geom)
            VALUES %s
            ON CONFLICT (grid_code) DO UPDATE
            SET lat = EXCLUDED.lat,
                lon = EXCLUDED.lon,
                grid_step = EXCLUDED.grid_step
            RETURNING id, grid_code, lat, lon;
            """,
            [(c, la, lo, st, sm, f"SRID=4326;POINT({lo} {la})") for (c, la, lo, st, sm) in points],
            template="(%s, %s, %s, %s, %s, ST_GeomFromEWKT(%s))",
        )
        # Ambil semua ID dari kode yang baru saja di-upsert
        codes = [p[0] for p in points]
        cur.execute(
            f"SELECT id, grid_code, lat, lon FROM {SCHEMA}.{GRID_TABLE} WHERE grid_code = ANY(%s) ORDER BY id;",
            (codes,),
        )
        grid_records = [
            {"id": row[0], "grid_code": row[1], "lat": float(row[2]), "lon": float(row[3])}
            for row in cur.fetchall()
        ]
    conn.commit()
    return grid_records


# --------------------------------------------------------------------------
# Pengambilan Batch Open-Meteo Archive & Pemrosesan FWI + Vektor Angin
# --------------------------------------------------------------------------
def fetch_openmeteo_batch(
    batch_grids: List[Dict],
    start_date_str: str,
    end_date_str: str,
    retries: int = 3,
) -> List[Dict]:
    """
    Mengambil data hourly dari Open-Meteo Archive API untuk satu batch koordinat sekaligus.
    Open-Meteo mendukung comma-separated latitudes & longitudes dalam 1 HTTP request.
    """
    lats_str = ",".join(str(g["lat"]) for g in batch_grids)
    lons_str = ",".join(str(g["lon"]) for g in batch_grids)

    params = {
        "latitude": lats_str,
        "longitude": lons_str,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "hourly": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m,"
            "wind_gusts_10m,cloud_cover,weather_code,precipitation,visibility"
        ),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }

    for attempt in range(retries):
        try:
            r = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                # Jika hanya 1 koordinat, Open-Meteo mengembalikan dict tunggal, jika >1 mengembalikan list of dicts
                if isinstance(data, list):
                    return data
                return [data]
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [429 Rate Limit] Menunggu {wait} detik sebelum mencoba ulang...")
                time.sleep(wait)
            else:
                print(f"  [HTTP {r.status_code}] Gagal mengambil batch: {r.text[:160]}")
                break
        except Exception as e:
            print(f"  [Attempt {attempt+1}/{retries}] Error koneksi Open-Meteo: {e}")
            time.sleep(2)
    return []


def backfill_climate_history(
    start_date_str: str,
    end_date_str: str,
    mode: str = "hotspots",
    grid_step: float = 0.25,
    hours_to_keep: List[int] = (0, 6, 12, 18),
    batch_size: int = 20,
    db_url: str = None,
):
    """
    Menjalankan backfill data iklim & arah angin untuk Kalimantan Barat.
    """
    target_db = db_url or load_env_db_url()
    safe_db_label = target_db.split("@")[-1] if "@" in target_db else target_db

    print("==================================================================")
    print("  BACKFILL CLIMATE & WIND HISTORY — KALIMANTAN BARAT (OPEN-METEO)")
    print("==================================================================")
    print(f"Database Tujuan : {safe_db_label}")
    print(f"Periode (UTC)   : {start_date_str} s/d {end_date_str}")
    print(f"Mode Titik      : {mode} (Resolusi Grid: {grid_step}° ≈ {grid_step * 111.3:.1f} km)")
    print(f"Jam Disimpan    : {list(hours_to_keep)} UTC")

    conn = psycopg2.connect(target_db)
    ensure_tables(conn)

    grid_list = build_or_load_kalbar_grid(
        conn, mode=mode, grid_step=grid_step, start_date_str=start_date_str, end_date_str=end_date_str
    )
    print(f"Total titik grid Kalimantan Barat yang akan diproses: {len(grid_list)} titik")

    bins_ffmc = [-1, 72, 77, 83, float("inf")]
    bins_dmc = [-1, 5, 15, 29, float("inf")]
    bins_dc = [-1, 141, 261, 350, float("inf")]
    bins_bui = [-1, 7, 20, 33, float("inf")]
    bins_isi = [-1, 2, 4, 5, float("inf")]
    bins_fwi = [-1, 2, 7, 13, float("inf")]
    labels_fwi = ["Low", "Moderate", "High", "Extreme"]

    hours_set = set(hours_to_keep)
    total_inserted = 0

    for b_start in range(0, len(grid_list), batch_size):
        batch = grid_list[b_start : b_start + batch_size]
        print(
            f"  -> Mengunduh batch grid {b_start + 1}..{b_start + len(batch)} dari {len(grid_list)}..."
        )
        responses = fetch_openmeteo_batch(batch, start_date_str, end_date_str)
        if not responses:
            continue

        batch_rows = []
        for g_info, resp in zip(batch, responses):
            grid_id = g_info["id"]
            lat = g_info["lat"]
            lon = g_info["lon"]

            h_data = resp.get("hourly", {})
            times = h_data.get("time", [])
            if not times:
                continue

            temps = h_data.get("temperature_2m", [])
            rhs = h_data.get("relative_humidity_2m", [])
            apps = h_data.get("apparent_temperature", [])
            pressures = h_data.get("surface_pressure", [])
            msls = h_data.get("pressure_msl", [])
            w_speeds = h_data.get("wind_speed_10m", [])
            w_degs = h_data.get("wind_direction_10m", [])
            w_gusts = h_data.get("wind_gusts_10m", [])
            clouds = h_data.get("cloud_cover", [])
            w_codes = h_data.get("weather_code", [])
            precips = h_data.get("precipitation", [])
            visibilities = h_data.get("visibility", [])

            # State FWI berkelanjutan per titik grid
            ffmc_state = 85.0
            dmc_state = 20.0
            dc_state = 200.0
            last_fwi_day = None
            fwi_dict = calculate_fwi(27.0, 75.0, 7.2, 0.0, ffmc_state, dmc_state, dc_state)

            for i in range(len(times)):
                t_utc = pd.to_datetime(times[i], utc=True)
                temp_val = float(temps[i]) if i < len(temps) and temps[i] is not None else 26.0
                rh_val = float(rhs[i]) if i < len(rhs) and rhs[i] is not None else 75.0
                ws_val = float(w_speeds[i]) if i < len(w_speeds) and w_speeds[i] is not None else 2.0
                w_deg_val = int(round(w_degs[i])) if i < len(w_degs) and w_degs[i] is not None else 0
                precip_val = float(precips[i]) if i < len(precips) and precips[i] is not None else 0.0

                # Akumulasi curah hujan 24 jam terakhir
                start_24 = max(0, i - 23)
                rain_24h = sum(
                    float(precips[j]) for j in range(start_24, i + 1) if j < len(precips) and precips[j] is not None
                )

                # Update state harian FWI pada jam 06:00 UTC (13:00 WIB - puncak pemanasan siang) atau awal hari
                cur_day = t_utc.date()
                if last_fwi_day != cur_day or t_utc.hour == 6:
                    fwi_dict = calculate_fwi(
                        temp=temp_val,
                        rh=rh_val,
                        wind_kmh=ws_val * 3.6,
                        rain_24h=rain_24h,
                        ffmc_prev=ffmc_state,
                        dmc_prev=dmc_state,
                        dc_prev=dc_state,
                    )
                    if t_utc.hour == 6 or last_fwi_day is None:
                        ffmc_state = fwi_dict["fine_fuel_moisture_code"]
                        dmc_state = fwi_dict["duff_moisture_code"]
                        dc_state = fwi_dict["drought_code"]
                        last_fwi_day = cur_day
                else:
                    # Hitung ulang ISI & FWI instan dengan kecepatan angin jam tersebut
                    fwi_dict = calculate_fwi(
                        temp=temp_val,
                        rh=rh_val,
                        wind_kmh=ws_val * 3.6,
                        rain_24h=rain_24h,
                        ffmc_prev=ffmc_state,
                        dmc_prev=dmc_state,
                        dc_prev=dc_state,
                    )

                if t_utc.hour not in hours_set:
                    continue

                t_naive = t_utc.tz_localize(None)
                t_wib = t_naive + pd.Timedelta(hours=7)
                t_wita = t_naive + pd.Timedelta(hours=8)
                t_wit = t_naive + pd.Timedelta(hours=9)

                # Vektor angin meteorologi:
                # wind_deg adalah arah DATANGNYA angin (0° = dari Utara).
                # wind_to_deg adalah arah HEMBUSAN / dorongan angin ((wind_deg + 180) % 360).
                wind_to_deg = (w_deg_val + 180) % 360
                rad_to = math.radians(wind_to_deg)
                wind_u = round(ws_val * math.sin(rad_to), 3)  # Komponen Timur (+) / Barat (-)
                wind_v = round(ws_val * math.cos(rad_to), 3)  # Komponen Utara (+) / Selatan (-)
                wind_compass = compass_dir_id(w_deg_val)

                w_code_raw = w_codes[i] if i < len(w_codes) else 0
                w_name, w_desc, w_icon = map_wmo_code(w_code_raw)
                vis_val = (
                    int(round(visibilities[i]))
                    if i < len(visibilities) and visibilities[i] is not None
                    else 10000
                )
                app_val = float(apps[i]) if i < len(apps) and apps[i] is not None else temp_val
                p_surf = int(round(pressures[i])) if i < len(pressures) and pressures[i] is not None else 1010
                p_msl = int(round(msls[i])) if i < len(msls) and msls[i] is not None else 1010
                gust_val = float(w_gusts[i]) if i < len(w_gusts) and w_gusts[i] is not None else ws_val
                cloud_val = int(round(clouds[i])) if i < len(clouds) and clouds[i] is not None else 0

                batch_rows.append(
                    (
                        grid_id,
                        lat,
                        lon,
                        t_utc.to_pydatetime(),
                        t_wib.to_pydatetime(),
                        t_wita.to_pydatetime(),
                        t_wit.to_pydatetime(),
                        fwi_dict["drought_code"],
                        fwi_dict["duff_moisture_code"],
                        fwi_dict["fine_fuel_moisture_code"],
                        fwi_dict["initial_spread_index"],
                        fwi_dict["build_up_index"],
                        fwi_dict["fire_weather_index"],
                        fwi_dict["daily_severity_index"],
                        categorize_fwi_val(fwi_dict["fine_fuel_moisture_code"], bins_ffmc, labels_fwi),
                        categorize_fwi_val(fwi_dict["duff_moisture_code"], bins_dmc, labels_fwi),
                        categorize_fwi_val(fwi_dict["drought_code"], bins_dc, labels_fwi),
                        categorize_fwi_val(fwi_dict["build_up_index"], bins_bui, labels_fwi),
                        categorize_fwi_val(fwi_dict["initial_spread_index"], bins_isi, labels_fwi),
                        categorize_fwi_val(fwi_dict["fire_weather_index"], bins_fwi, labels_fwi),
                        vis_val,
                        round(temp_val, 2),
                        round(app_val, 2),
                        round(temp_val, 2),
                        round(temp_val, 2),
                        p_surf,
                        p_msl,
                        p_surf,
                        int(round(rh_val)),
                        categorize_humidity_val(rh_val),
                        round(ws_val, 2),
                        w_deg_val,
                        wind_to_deg,
                        wind_compass,
                        wind_u,
                        wind_v,
                        round(gust_val, 2),
                        categorize_wind_speed_val(ws_val),
                        cloud_val,
                        w_name,
                        w_desc,
                        w_icon,
                        round(precip_val, 2),
                        round(rain_24h, 2),
                    )
                )

        if batch_rows:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    f"""
                    INSERT INTO {SCHEMA}.{HISTORY_TABLE} (
                        grid_id, lat, lon, time_utc, time_wib, time_wita, time_wit,
                        drought_code, duff_moisture_code, fine_fuel_moisture_code,
                        initial_spread_index, build_up_index, fire_weather_index, daily_severity_index,
                        ffmc_category, dmc_category, dc_category, bui_category, isi_category, fwi_category,
                        visibility, temp, feels_like, temp_min, temp_max,
                        pressure, sea_level_pressure, grnd_level_pressure,
                        humidity, humidity_category,
                        wind_speed, wind_deg, wind_to_deg, wind_compass, wind_u, wind_v,
                        wind_gust, wind_speed_category,
                        cloud_cover_percent, weather_code_name, weather_code_description, weather_code_icon,
                        precipitation_mm, rain_24h_mm
                    ) VALUES %s
                    ON CONFLICT (grid_id, time_utc) DO UPDATE SET
                        temp = EXCLUDED.temp,
                        humidity = EXCLUDED.humidity,
                        wind_speed = EXCLUDED.wind_speed,
                        wind_deg = EXCLUDED.wind_deg,
                        wind_to_deg = EXCLUDED.wind_to_deg,
                        wind_u = EXCLUDED.wind_u,
                        wind_v = EXCLUDED.wind_v,
                        wind_gust = EXCLUDED.wind_gust,
                        precipitation_mm = EXCLUDED.precipitation_mm,
                        rain_24h_mm = EXCLUDED.rain_24h_mm,
                        fire_weather_index = EXCLUDED.fire_weather_index,
                        drought_code = EXCLUDED.drought_code,
                        initial_spread_index = EXCLUDED.initial_spread_index,
                        fwi_category = EXCLUDED.fwi_category;
                    """,
                    batch_rows,
                )
            conn.commit()
            total_inserted += len(batch_rows)
            print(f"     ✓ Tersimpan {len(batch_rows):,} baris (Total kumulatif: {total_inserted:,})")

        time.sleep(0.4)  # Jeda sopan antar batch Open-Meteo

    conn.close()
    print("==================================================================")
    print(f"SELESAI! Total rekaman iklim & angin disimpan: {total_inserted:,} baris")
    print(f"Tabel: {SCHEMA}.{HISTORY_TABLE} & {SCHEMA}.{GRID_TABLE}")
    print("==================================================================")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill data iklim, arah angin, dan FWI Kalimantan Barat dari Open-Meteo Archive."
    )
    parser.add_argument("--start", type=str, default="2026-10-01", help="Tanggal mulai (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-11-01", help="Tanggal akhir (YYYY-MM-DD)")
    parser.add_argument(
        "--mode",
        choices=["hotspots", "grid"],
        default="hotspots",
        help="'hotspots' = hanya sel 0.25° yang memiliki titik api di Kalbar; 'grid' = seluruh grid BBOX Kalbar",
    )
    parser.add_argument(
        "--grid-step",
        type=float,
        default=0.25,
        help="Resolusi derajat grid (default 0.25° sesuai resolusi native ERA5 ~27.5 km)",
    )
    parser.add_argument(
        "--all-hours",
        action="store_true",
        help="Simpan seluruh 24 jam per hari (default hanya jam sinoptik 00, 06, 12, 18 UTC)",
    )
    parser.add_argument("--db-url", type=str, default=None, help="Override DATABASE_URL")
    args = parser.parse_args()

    hours = list(range(24)) if args.all_hours else [0, 6, 12, 18]
    backfill_climate_history(
        start_date_str=args.start,
        end_date_str=args.end,
        mode=args.mode,
        grid_step=args.grid_step,
        hours_to_keep=hours,
        db_url=args.db_url,
    )
