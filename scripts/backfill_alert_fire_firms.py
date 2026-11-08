#!/usr/bin/env python3
"""
Backfill historis FIRMS fire hotspot untuk Pulau Kalimantan.

Alur (sama persis dengan cron NRT harian):
    FIRMS API  ->  monitoring.alert_fire_{source}_raw
               ->  intersect concession / village / landcover (di PostGIS)
               ->  monitoring.alert_fire_{source}

Perbedaan utama dengan versi sebelumnya:
  1. Source & rentang tanggal diambil dinamis dari endpoint /api/data_availability
     (menghindari 400/Invalid source — penyebab utama request gagal sebelumnya).
     SP dipakai untuk data lama, NRT untuk data baru, otomatis per chunk.
  2. Intersect dikerjakan di PostGIS (index-assisted), bukan gpd.sjoin di memori.
     Untuk belasan tahun data ini bedanya jam vs menit.
  3. Kolom raw diselaraskan otomatis dengan kolom tabel yang sudah ada,
     jadi struktur kolom identik dengan pipeline NRT (kolom asing di-drop).
  4. Idempotent: tiap chunk menghapus dulu window-nya di raw & final sebelum insert.

Contoh:
  python backfill_alert_fire_firms.py --start 2010-01-01
  python backfill_alert_fire_firms.py --source viirssnpp --start 2012-01-20 --resume
  python backfill_alert_fire_firms.py --reintersect --start 2015-01-01   # tanpa download ulang
  python backfill_alert_fire_firms.py --raw-only --start 2010-01-01      # download saja
"""

import sys
import json
import time
import uuid
import asyncio
import argparse
import datetime
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import requests
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from sqlalchemy import create_engine, text
from geoalchemy2 import Geometry
# --------------------------------------------------------------------------
# Path bootstrap
# --------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"

for _p in [str(_PROJECT_ROOT), str(_BACKEND_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from app.core.config import SPATIAL_DATABASE_CONFIG  # noqa: E402
except ImportError:
    try:
        from backend.config import SPATIAL_DATABASE_CONFIG
    except ImportError:
        from config import SPATIAL_DATABASE_CONFIG

try:
    import app.db.db_connect as db_connect
except ImportError:
    try:
        import backend.db_connect as db_connect
    except ImportError:
        import db_connect

try:
    engine = db_connect.sync_engine
except Exception:
    from urllib.parse import quote_plus
    engine = create_engine(
        f"postgresql+psycopg2://{SPATIAL_DATABASE_CONFIG['user']}:{quote_plus(str(SPATIAL_DATABASE_CONFIG['password']))}"
        f"@{SPATIAL_DATABASE_CONFIG['host']}:{SPATIAL_DATABASE_CONFIG['port']}/{SPATIAL_DATABASE_CONFIG['database']}",
        pool_pre_ping=True,
    )

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------
SCHEMA = "monitoring"

# API token & extent — loaded dynamically from system.setting at startup
# (with hardcoded fallbacks so backfill can run standalone without DB settings)
DEFAULT_TOKEN  = "70e8e413e8f4661fb7479c384395c2f8"
DEFAULT_EXTENT = "108,-4.2,119.3,4.5"
# Kalau nanti mau 1 Indonesia: "94.9,-11.1,141.1,6.1"

# Module globals, populated in async main()
TOKEN  = DEFAULT_TOKEN
EXTENT = DEFAULT_EXTENT


async def get_target_extent(default: str = DEFAULT_EXTENT) -> str:
    """Retrieve bounding box extent from system.setting ('processing.target_extent' or 'processing.firms_extent')."""
    try:
        val = await db_connect.get_setting_value("processing.target_extent")
        if val and str(val).strip():
            return str(val).strip()
    except Exception:
        try:
            val = await db_connect.get_setting_value("processing.firms_extent")
            if val and str(val).strip():
                return str(val).strip()
        except Exception:
            pass
    return default


async def get_firms_token(default: str = DEFAULT_TOKEN) -> str:
    """Retrieve FIRMS API token from system.setting ('firms.map_key')."""
    try:
        val = await db_connect.get_setting_value("firms.map_key")
        if val and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return default

CHUNK_DAYS = 5          # batas chunk per request (1..10 hari untuk SP, 1..1 hari untuk NRT historis)
SLEEP_BETWEEN = 1.5      # detik; limit resmi 5000 transaksi / 10 menit
REQUEST_TIMEOUT = 120

CHECKPOINT_FILE = _SCRIPT_DIR / "backfill_firms_checkpoint.json"

TABLE_CONCESSION = f"{SCHEMA}.unit_concession_sp"
TABLE_VILLAGE = f"{SCHEMA}.base_adminvillage_alert"
TABLE_LANDCOVER = f"{SCHEMA}.base_landcover_change"

# Nama produk FIRMS per source. SP = Science Processing (arsip),
# NRT = Near Real Time (±2 bulan terakhir). Rentang tanggal valid diambil
# dari API data_availability saat runtime, ini cuma fallback.
SOURCES = {
    "modis":       {"sp": "MODIS_SP",       "nrt": "MODIS_NRT",         "from": date(2000, 11, 1)},
    "viirssnpp":   {"sp": "VIIRS_SNPP_SP",  "nrt": "VIIRS_SNPP_NRT",    "from": date(2012, 1, 20)},
    "viirsnoaa20": {"sp": None,             "nrt": "VIIRS_NOAA20_NRT",  "from": date(2020, 1, 1)},
    "viirsnoaa21": {"sp": None,             "nrt": "VIIRS_NOAA21_NRT",  "from": date(2024, 1, 17)},
}

# Mapping kolom hasil join -> ekspresi SQL. Sisanya diambil dari tabel raw (p.*)
JOIN_EXPR = {
    "uid_concession": "c.uid",
    "concession":     "c.namobj",
    "village":        "v.wadmkd",
    "subdistrict":    "v.wadmkc",
    "district":       "v.wadmkk",
    "province":       "v.wadmpr",
    # "lctype" diisi dinamis, tergantung skema landcover (lihat detect_landcover_schema)
}

# Kandidat nama kolom kelas penutupan lahan kalau MV pakai format long (ada pl_year)
LC_VALUE_CANDIDATES = ["lctype", "pl", "pl_class", "landcover", "penutupan", "kelas"]


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------
def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_checkpoint(cp):
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2, default=str))


# --------------------------------------------------------------------------
# Introspeksi DB
# --------------------------------------------------------------------------
def table_exists(table):
    sql = """
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = :s AND c.relname = :t
        )
    """
    with engine.connect() as conn:
        return conn.execute(text(sql), {"s": SCHEMA, "t": table}).scalar()


def get_columns(table):
    """Kolom tabel/MV/view, urut ordinal. Kosong kalau belum ada."""
    sql = """
        SELECT a.attname AS column_name
        FROM pg_attribute a
        JOIN pg_class c ON a.attrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = :s AND c.relname = :t
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
    """
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(sql), {"s": SCHEMA, "t": table})]


def get_column_types(table):
    """Dict {column_name: data_type} untuk tabel/MV/view."""
    sql = """
        SELECT a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type
        FROM pg_attribute a
        JOIN pg_class c ON a.attrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = :s AND c.relname = :t
          AND a.attnum > 0
          AND NOT a.attisdropped
    """
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text(sql), {"s": SCHEMA, "t": table})}


def detect_landcover_schema():
    """
    Mendukung dua bentuk MV base_landcover_change:
      A. long  : ada kolom `pl_year` + kolom kelas (lctype/pl/...)  -> filter per tahun
      B. wide  : kolom pl_2019, pl_2020, ...                        -> pilih kolom per tahun
    Return dict {'mode': 'long'|'wide', ...}

    Menggunakan pg_attribute + pg_namespace (bukan information_schema.columns) agar
    Materialized View juga terdeteksi dengan benar.
    """
    cols = get_columns("base_landcover_change")
    if not cols:
        print(f"    (info) {TABLE_LANDCOVER} belum memiliki kolom atau tidak ditemukan.")
        return {"mode": "wide", "columns": {}, "years": []}

    if "pl_year" in cols:
        value_col = next((c for c in LC_VALUE_CANDIDATES if c in cols), None)
        if value_col is None:
            return {"mode": "wide", "columns": {}, "years": []}
        with engine.connect() as conn:
            years = [int(r[0]) for r in conn.execute(
                text(f"SELECT DISTINCT pl_year FROM {TABLE_LANDCOVER} WHERE pl_year IS NOT NULL ORDER BY 1")
            )]
        return {"mode": "long", "value_col": value_col, "years": years}

    # Wide mode: cari kolom pl_YYYY — hanya yang suffix-nya digit
    wide = {}
    for c in cols:
        suffix = c[3:] if c.startswith("pl_") else ""
        if suffix.isdigit():
            wide[int(suffix)] = c
    if not wide:
        return {"mode": "wide", "columns": {}, "years": []}
    return {"mode": "wide", "columns": wide, "years": sorted(wide)}


def pick_lc_year(lc, alert_year):
    """Tahun landcover terdekat <= tahun alert; kalau tidak ada, tahun paling awal."""
    years = lc.get("years", [])
    if not years:
        return alert_year
    candidates = [y for y in years if y <= alert_year]
    return max(candidates) if candidates else min(years)


# --------------------------------------------------------------------------
# FIRMS API
# --------------------------------------------------------------------------
def fetch_availability():
    """
    Ambil rentang tanggal valid tiap produk dari FIRMS.
    Ini yang bikin request historis sering gagal: source name / tanggal di luar
    rentang -> API balas teks error, bukan CSV.
    """
    url = f"https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{TOKEN}/ALL"
    try:
        resp = requests.get(url, timeout=60)
        df = pd.read_csv(StringIO(resp.text))
        avail = {}
        for _, row in df.iterrows():
            avail[str(row["data_id"]).strip()] = (
                date.fromisoformat(str(row["min_date"]).strip()),
                date.fromisoformat(str(row["max_date"]).strip()),
            )
        print(f"Data availability: {len(avail)} produk terdeteksi")
        return avail
    except Exception as e:
        print(f"WARNING: gagal ambil data_availability ({e}) — pakai fallback statis.")
        return {}


def resolve_product(source_key, chunk_start, chunk_end, avail):
    """Pilih SP/NRT yang valid untuk window ini. None kalau tidak ada yang cocok."""
    cfg = SOURCES[source_key]
    for product in (cfg["sp"], cfg["nrt"]):
        if not product:
            continue
        if product in avail:
            lo, hi = avail[product]
            if lo <= chunk_start and chunk_start <= hi:
                return product
        else:
            # fallback statis: SP untuk data > 60 hari, NRT untuk sisanya
            if product.endswith("_SP") and chunk_end < date.today() - timedelta(days=60):
                return product
            if product.endswith("_NRT") and chunk_end >= date.today() - timedelta(days=75):
                return product
    return None


def download_chunk(product, chunk_date, days, retries=4):
    url = (
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        f"{TOKEN}/{product}/{EXTENT}/{days}/{chunk_date.isoformat()}"
    )
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            body = resp.text.strip()

            if resp.status_code != 200:
                print(f"\n    HTTP {resp.status_code} (percobaan {attempt}/{retries}): {body[:200]}")
            elif "Invalid" in body[:200] or "error" in body[:80].lower():
                # API membalas 200 + pesan error teks
                print(f"\n    API error: {body[:200]}")
                return pd.DataFrame()
            elif "acq_date" not in body:
                return pd.DataFrame()   # window kosong, wajar
            else:
                return pd.read_csv(StringIO(body))

        except Exception as e:
            print(f"\n    Exception percobaan {attempt}/{retries}: {e}")

        if attempt < retries:
            time.sleep(5 * attempt)
    return pd.DataFrame()


# --------------------------------------------------------------------------
# Transformasi — kolom dibuat identik dengan cron NRT
# --------------------------------------------------------------------------
def rename_columns(df):
    col_map = {"bright_t31": "bright_t", "bright_ti4": "brightness", "bright_ti5": "bright_t"}
    return df.rename(columns={c: n for c, n in col_map.items() if c in df.columns})


def process_chunk(df, source_key=""):
    df = rename_columns(df)

    if "version" in df.columns:
        df["version"] = df["version"].astype(str)
    if "confidence" in df.columns:
        df["confidence"] = df["confidence"].astype(str)

    df["datetm"] = pd.to_datetime(
        df["acq_date"].astype(str) + " " + df["acq_time"].astype(str).str.zfill(4),
        format="%Y-%m-%d %H%M",
        errors="coerce",
    )
    df = df.dropna(subset=["datetm", "latitude", "longitude"])
    if df.empty:
        return df

    df["datetm"] = df["datetm"].dt.tz_localize("UTC")

    # kolom turunan UTC (persis seperti script NRT)
    _d = pd.to_datetime(df["acq_date"])
    df["acq_date"] = _d.dt.date
    df["date"] = _d.dt.date
    _wdate = _d + (7 - _d.dt.weekday) * np.timedelta64(1, "D")
    df["wdate"] = _wdate.dt.date
    df["year"] = pd.to_datetime(df["wdate"]).dt.year
    df["month"] = pd.to_datetime(df["wdate"]).dt.month
    df["day"] = pd.to_datetime(df["wdate"]).dt.day

    # kolom waktu lokal WIB
    df["datetm_loc"] = df["datetm"].dt.tz_convert("Asia/Jakarta")
    _d_loc = pd.to_datetime(df["datetm_loc"].dt.date)
    df["date_loc"] = _d_loc.dt.date
    _wdate_loc = _d_loc + (7 - _d_loc.dt.weekday) * np.timedelta64(1, "D")
    df["wdate_loc"] = _wdate_loc.dt.date
    df["year_loc"] = pd.to_datetime(df["wdate_loc"]).dt.year
    df["month_loc"] = pd.to_datetime(df["wdate_loc"]).dt.month
    df["day_loc"] = pd.to_datetime(df["wdate_loc"]).dt.day
    df["datetm_loc"] = df["datetm_loc"].dt.tz_localize(None)

    # uid deterministik (sumber + lat/lon + waktu): unduh ulang titik yang sama -> uid sama,
    # sehingga riwayat titik api (mis. monitoring.fire_spread) tetap tertaut.
    _keys = (source_key + "|" + df["latitude"].map(lambda v: f"{float(v):.5f}")
             + "|" + df["longitude"].map(lambda v: f"{float(v):.5f}")
             + "|" + df["datetm"].dt.strftime("%Y-%m-%dT%H:%M"))
    df["uid_alert"] = [str(uuid.uuid5(uuid.NAMESPACE_URL, k)) for k in _keys]
    df = df.drop_duplicates("uid_alert").copy()
    df["date_processing"] = datetime.datetime.now(datetime.timezone.utc)

    return df


def align_to_table(gdf, table):
    """
    Buang kolom yang tidak ada di tabel tujuan (mis. kolom `type` yang cuma
    muncul di produk SP) supaya struktur tetap identik dengan tabel NRT.
    """
    existing = get_columns(table)
    if not existing:
        return gdf  # tabel belum ada, biar dibuat apa adanya
    extra = [c for c in gdf.columns if c not in existing]
    missing = [c for c in existing if c not in gdf.columns]
    if extra:
        gdf = gdf.drop(columns=extra)
    if missing:
        print(f"\n    (info) kolom tabel tidak terisi dari FIRMS: {missing}")
    return gdf


# --------------------------------------------------------------------------
# Insert raw + intersect di PostGIS
# --------------------------------------------------------------------------
def deduplicate_raw_dataframe(df, raw_table, start, end):
    """
    Filter DataFrame agar hanya menyisakan baris yang (latitude, longitude, datetm)-nya
    belum ada di tabel _raw untuk window waktu ini.
    """
    if df.empty or not table_exists(raw_table):
        return df

    sql = f"""
        SELECT latitude, longitude, datetm 
        FROM {SCHEMA}.{raw_table}
        WHERE datetm >= :s AND datetm < :e
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(sql),
                {
                    "s": datetime.datetime.combine(start, datetime.time.min, datetime.timezone.utc),
                    "e": datetime.datetime.combine(end, datetime.time.min, datetime.timezone.utc),
                },
            ).fetchall()
    except Exception:
        return df

    if not rows:
        return df

    existing_keys = set()
    for r in rows:
        if r[0] is not None and r[1] is not None and r[2] is not None:
            lat = round(float(r[0]), 5)
            lon = round(float(r[1]), 5)
            dt = pd.to_datetime(r[2])
            dt_naive = dt.tz_localize(None) if dt.tzinfo is not None else dt
            existing_keys.add((lat, lon, dt_naive))

    def is_new(row):
        lat = round(float(row["latitude"]), 5)
        lon = round(float(row["longitude"]), 5)
        dt = pd.to_datetime(row["datetm"])
        dt_naive = dt.tz_localize(None) if dt.tzinfo is not None else dt
        return (lat, lon, dt_naive) not in existing_keys

    initial_count = len(df)
    df_dedup = df[df.apply(is_new, axis=1)].copy()
    dropped = initial_count - len(df_dedup)
    if dropped > 0:
        print(f" (dedup raw: {dropped} duplikat dibuang)", end="")
    return df_dedup


def insert_raw(df, raw_table):
    gdf = gpd.GeoDataFrame(
        df,
        geometry=[Point(xy) for xy in zip(df.longitude, df.latitude)],
        crs="EPSG:4326",
    ).reset_index(drop=True)

    gdf = align_to_table(gdf, raw_table)
    gdf.to_postgis(
        name=raw_table,
        con=engine,
        schema=SCHEMA,
        if_exists="append",
        index=False,
        dtype={"geometry": Geometry("POINT", srid=4326)},
    )
    return len(gdf)


def ensure_raw_index(raw_table):
    with engine.begin() as conn:
        conn.execute(text(
            f'CREATE INDEX IF NOT EXISTS {raw_table}_geom_idx '
            f'ON {SCHEMA}.{raw_table} USING GIST (geometry)'
        ))
        conn.execute(text(
            f'CREATE INDEX IF NOT EXISTS {raw_table}_datetm_idx '
            f'ON {SCHEMA}.{raw_table} (datetm)'
        ))


def intersect_window(source_key, start, end, lc, lc_year):
    """
    Bangun tabel final dari raw untuk window tertentu, seluruhnya di PostGIS.
    concession = INNER JOIN (titik di luar konsesi dibuang, sama seperti cron),
    village & landcover = LEFT JOIN.
    Deduplikasi: Mengabaikan titik yang sudah ada di tabel final.
    """
    raw_table = f"alert_fire_{source_key}_raw"
    final_table = f"alert_fire_{source_key}"

    final_cols = get_columns(final_table)
    if not final_cols:
        print(f"    ! Tabel {SCHEMA}.{final_table} belum ada — jalankan cron NRT sekali "
              f"dulu agar skema final terbentuk, atau pakai --raw-only.")
        return 0

    raw_cols = set(get_columns(raw_table))

    # ekspresi landcover
    if lc["mode"] == "long":
        lc_expr = f'l.{lc["value_col"]}'
        lc_join = (f'LEFT JOIN {TABLE_LANDCOVER} l '
                   f'ON l.pl_year = {lc_year} AND ST_Intersects(l.geometry, p.geometry)')
    else:
        lc_expr = f'l.{lc["columns"][lc_year]}'
        lc_join = f'LEFT JOIN {TABLE_LANDCOVER} l ON ST_Intersects(l.geometry, p.geometry)'

    expr_map = dict(JOIN_EXPR)
    expr_map["lctype"] = lc_expr

    final_types = get_column_types(final_table)
    raw_types = get_column_types(raw_table)

    select_parts, insert_cols = [], []
    for col in final_cols:
        expr = None
        if col in expr_map:
            expr = expr_map[col]
        elif col in raw_cols:
            expr = f'p."{col}"'

        if expr is not None:
            f_type = final_types.get(col, "").lower()
            if f_type == "uuid":
                select_parts.append(f'({expr})::uuid AS "{col}"')
            elif f_type == "date":
                select_parts.append(f'({expr})::date AS "{col}"')
            elif "timestamp" in f_type:
                select_parts.append(f'({expr})::timestamp with time zone AS "{col}"')
            elif f_type in ("integer", "smallint", "bigint"):
                select_parts.append(f'({expr})::{f_type} AS "{col}"')
            elif f_type in ("double precision", "real", "numeric"):
                select_parts.append(f'({expr})::{f_type} AS "{col}"')
            else:
                select_parts.append(f'{expr} AS "{col}"')
            insert_cols.append(f'"{col}"')

    sql = f"""
        INSERT INTO {SCHEMA}.{final_table} ({", ".join(insert_cols)})
        SELECT {", ".join(select_parts)}
        FROM (
            SELECT * FROM {SCHEMA}.{raw_table}
            WHERE datetm >= :s AND datetm < :e
        ) p
        JOIN {TABLE_CONCESSION} c
             ON ST_Intersects(c.geometry, p.geometry)
        LEFT JOIN {TABLE_VILLAGE} v
             ON ST_Intersects(v.geometry, p.geometry)
        {lc_join}
        WHERE NOT EXISTS (
            SELECT 1 FROM {SCHEMA}.{final_table} f
            WHERE f.datetm >= :s AND f.datetm < :e
              AND ROUND(f.latitude::numeric, 5) = ROUND(p.latitude::numeric, 5)
              AND ROUND(f.longitude::numeric, 5) = ROUND(p.longitude::numeric, 5)
              AND f.datetm = p.datetm
        )
    """

    with engine.begin() as conn:
        res = conn.execute(text(sql), {
            "s": datetime.datetime.combine(start, datetime.time.min, datetime.timezone.utc),
            "e": datetime.datetime.combine(end, datetime.time.min, datetime.timezone.utc),
        })
        return res.rowcount or 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
async def main():
    global TOKEN, EXTENT
    TOKEN = await get_firms_token(DEFAULT_TOKEN)
    EXTENT = await get_target_extent(DEFAULT_EXTENT)
    print(f"[config] TOKEN={TOKEN[:6]}..., EXTENT={EXTENT}")

    ap = argparse.ArgumentParser(description="Backfill historis FIRMS — Kalimantan")
    ap.add_argument("--start", default="2010-01-01", help="Tanggal mulai YYYY-MM-DD (default 2010-01-01)")
    ap.add_argument("--end", default=str(date.today()), help="Tanggal akhir YYYY-MM-DD (default hari ini)")
    ap.add_argument("--source", default="all", choices=["all"] + list(SOURCES))
    ap.add_argument("--resume", action="store_true", help="Lanjut dari checkpoint")
    ap.add_argument("--raw-only", action="store_true", help="Download ke tabel _raw saja, tanpa intersect")
    ap.add_argument("--reintersect", action="store_true",
                    help="Tidak download; bangun ulang tabel final dari tabel _raw yang sudah ada")
    args = ap.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    checkpoint = load_checkpoint() if args.resume else {}
    sources = list(SOURCES) if args.source == "all" else [args.source]

    lc = detect_landcover_schema()
    avail = {} if args.reintersect else fetch_availability()

    print(f"\n{'='*72}")
    print(f"FIRMS backfill Kalimantan | {start_date} -> {end_date}")
    print(f"Sources    : {', '.join(sources)}")
    print(f"Landcover  : mode={lc['mode']} tahun tersedia={lc['years']}")
    print(f"Mode       : {'reintersect' if args.reintersect else ('raw-only' if args.raw_only else 'full')}")
    print(f"{'='*72}")

    grand_raw = grand_final = 0

    for src in sources:
        cfg = SOURCES[src]
        raw_table, final_table = f"alert_fire_{src}_raw", f"alert_fire_{src}"

        eff_start = max(start_date, cfg["from"])
        avail_mins = [avail[p][0] for p in (cfg["sp"], cfg["nrt"]) if p and p in avail]
        if avail_mins:
            eff_start = max(eff_start, min(avail_mins))

        if args.resume and src in checkpoint:
            resume_from = date.fromisoformat(checkpoint[src]) + timedelta(days=CHUNK_DAYS)
            if resume_from > eff_start:
                eff_start = resume_from
                print(f"\n[{src}] resume dari checkpoint -> {eff_start}")

        if eff_start >= end_date:
            print(f"\n[{src}] sudah selesai. Dilewati.")
            continue

        print(f"\n--- {src.upper()} | {eff_start} -> {end_date} ---")
        if table_exists(raw_table):
            ensure_raw_index(raw_table)

        chunk_start = eff_start
        n_raw = n_final = 0

        while chunk_start < end_date:
            days = min(CHUNK_DAYS, (end_date - chunk_start).days)
            chunk_end = chunk_start + timedelta(days=days)
            lc_year = pick_lc_year(lc, chunk_start.year)

            if args.reintersect:
                f = intersect_window(src, chunk_start, chunk_end, lc, lc_year)
                n_final += f
                print(f"  [{chunk_start}] reintersect (lc {lc_year}) -> {f} baris final baru")
            else:
                product = resolve_product(src, chunk_start, chunk_end, avail)
                if not product:
                    print(f"  [{chunk_start}] tidak ada produk valid — dilewati")
                    chunk_start += timedelta(days=CHUNK_DAYS)
                    continue

                print(f"  [{chunk_start}] +{days}h via {product} ...", end=" ", flush=True)
                raw_df = download_chunk(product, chunk_start, days)

                if raw_df.empty:
                    print("kosong")
                else:
                    proc = process_chunk(raw_df, src)
                    if proc.empty:
                        print("tidak ada baris valid")
                    else:
                        proc = deduplicate_raw_dataframe(proc, raw_table, chunk_start, chunk_end)
                        if proc.empty:
                            print("semua baris raw sudah ada (duplikat)")
                        else:
                            r = insert_raw(proc, raw_table)
                            ensure_raw_index(raw_table)
                            n_raw += r
                            if args.raw_only:
                                print(f"raw {r}")
                            else:
                                f = intersect_window(src, chunk_start, chunk_end, lc, lc_year)
                                n_final += f
                                print(f"raw {r} -> final {f} (lc {lc_year})")

                time.sleep(SLEEP_BETWEEN)

            checkpoint[src] = chunk_start.isoformat()
            save_checkpoint(checkpoint)
            chunk_start += timedelta(days=CHUNK_DAYS)

        print(f"[{src}] SELESAI — raw {n_raw}, final {n_final}")
        grand_raw += n_raw
        grand_final += n_final

    print(f"\n{'='*72}")
    print(f"Total: {grand_raw} baris raw, {grand_final} baris final")
    print(f"Checkpoint: {CHECKPOINT_FILE}")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    asyncio.run(main())