#!/usr/bin/env python3
"""
Backfill historical SIPONGI fire hotspot data for Kalimantan Island.

Uses SIPONGI date-range API (startdate/enddate, tipe=txt) to fetch historical
hotspot data per Kalimantan province. Two tables are populated:

  monitoring.alert_fire_sipongi_archive_raw  — raw API data (all columns, no filter)
  monitoring.alert_fire_sipongi_archive      — processed data with landcover (left join)

Landcover is year-matched: pl_{alert_year} column from base_landcover_change.
If the exact year column is not available, nearest prior year is used.

Usage:
  python backfill_alert_fire_sipongi.py
  python backfill_alert_fire_sipongi.py --start 2023-01-01 --end 2026-09-17
  python backfill_alert_fire_sipongi.py --chunk-days 30 --confidence all
  python backfill_alert_fire_sipongi.py --resume
"""

import sys
import io
import json
import time
import asyncio
import argparse
import uuid
from typing import List, Optional
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import pandas as pd
import numpy as np
import geopandas as gpd
from shapely.geometry import Point
from sqlalchemy import create_engine, text
from geoalchemy2 import Geometry

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_SCRIPT_DIR  = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"

for _p in [str(_PROJECT_ROOT), str(_BACKEND_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from app.core.config import SPATIAL_DATABASE_CONFIG
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCHEMA         = "monitoring"

# Production tables — same tables used by update_alert_fire_sipongi_v2.py
TABLE_RAW       = "alert_fire_sipongi_raw"    # raw: all API records before concession filter
TABLE_ENRICHED  = "alert_fire_sipongi"         # enriched: intersected with concession+village+landcover

TABLE_CONCESSION = "monitoring.unit_concession_sp"
TABLE_VILLAGE    = "monitoring.base_adminvillage_alert"
TABLE_LANDCOVER  = "monitoring.base_landcover_change"

SIPONGI_URL = "https://opsroom.sipongidata.my.id/api/sebaran/download"

KALIMANTAN_PROVINCES = [
    "Kalimantan Barat",
    "Kalimantan Tengah",
    "Kalimantan Timur",
    "Kalimantan Selatan",
    "Kalimantan Utara",
]

DEFAULT_CHUNK_DAYS = 30

CHECKPOINT_FILE = _SCRIPT_DIR / "backfill_sipongi_checkpoint.json"

SIPONGI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/plain, */*",
    "Referer": "https://opsroom.sipongidata.my.id/",
}


async def get_target_provinces(default: Optional[List[str]] = None) -> List[str]:
    """Retrieve target provinces list from system.setting ('processing.target_provinces')."""
    if default is None:
        default = KALIMANTAN_PROVINCES
    try:
        val = await db_connect.get_setting_value("processing.target_provinces")
        if val:
            val_str = str(val).strip()
            if val_str.startswith("["):
                import json
                res = json.loads(val_str)
                if isinstance(res, list):
                    return [str(x).strip() for x in res if x]
            return [x.strip() for x in val_str.split(",") if x.strip()]
    except Exception as exc:
        print(f"Note: Could not fetch 'processing.target_provinces' from system.setting ({exc}). Using default.")
    return default




# ---------------------------------------------------------------------------
# Flexible column name resolution
# SIPONGI historical API may return different column names than the live API.
# Each key is the canonical name we use internally; values are accepted aliases.
# ---------------------------------------------------------------------------
COL_ALIASES = {
    "Latitude":   ["Latitude", "latitude", "LAT", "Lat", "lat"],
    "Longitude":  ["Longitude", "longitude", "LON", "Lon", "long", "Long", "lng"],
    "Tanggal":    ["Tanggal", "tanggal", "Date", "date", "Tgl", "tgl", "TANGGAL"],
    "Waktu":      ["Waktu", "waktu", "Time", "time", "Jam", "jam", "WAKTU"],
    "Provinsi":   ["Provinsi", "provinsi", "Province", "PROV", "prov", "PROVINSI"],
    "Kab Kota":   ["Kab Kota", "Kab/Kota", "KabKota", "Kabupaten", "kabkota",
                   "Kabupaten/Kota", "District", "kab_kota", "KAB_KOTA"],
    "Kecamatan":  ["Kecamatan", "kecamatan", "Subdistrict", "KECAMATAN", "Kec"],
    "Desa":       ["Desa", "desa", "Village", "Kelurahan", "DESA", "Kel/Desa"],
    "Satelit":    ["Satelit", "satelit", "Satellite", "Sensor", "sensor", "SATELIT"],
    "Confidence": ["Confidence", "confidence", "CONFIDENCE", "Conf", "conf"],
}


def resolve_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Renames DataFrame columns to canonical names using COL_ALIASES.
    Prints a warning for any canonical name that could not be resolved.
    Returns df with renamed columns (other columns are preserved as-is).
    """
    rename_map = {}
    resolved   = set()

    for canonical, aliases in COL_ALIASES.items():
        for alias in aliases:
            if alias in df.columns:
                if alias != canonical:
                    rename_map[alias] = canonical
                resolved.add(canonical)
                break

    if rename_map:
        df = df.rename(columns=rename_map)

    missing = set(COL_ALIASES.keys()) - resolved
    if missing:
        print(f"    [WARN] Could not resolve columns: {missing}")
        print(f"    [WARN] Actual columns received: {list(df.columns)}")

    return df


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_checkpoint(cp):
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2, default=str))


def checkpoint_key(prov, chunk_start):
    return f"{prov}|{chunk_start.isoformat()}"


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def fetch_sipongi_historical(provinsi, start_dt, end_dt, confidence="all", retries=3):
    """
    Fetch historical SIPONGI hotspot data as CSV/txt (tipe=txt).
    Returns raw pd.DataFrame with original API columns, or empty DataFrame on failure.
    """
    params = {
        "startdate":  start_dt.strftime("%Y-%m-%d 00:00:00"),
        "enddate":    end_dt.strftime("%Y-%m-%d 00:00:00"),
        "provinsi":   provinsi,
        "confidence": confidence,
        "satelit":    "all-nasa",
        "tipe":       "txt",   # CSV — lighter than xlsx, consistent column names
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                SIPONGI_URL, params=params, headers=SIPONGI_HEADERS, timeout=60
            )
            resp.raise_for_status()

            content = resp.text.strip()

            if not content:
                print(f"    (empty response)")
                return pd.DataFrame()

            # Detect and parse CSV
            if "Provinsi" in content or "provinsi" in content or "LAT" in content.split("\n")[0].upper():
                df = pd.read_csv(io.StringIO(content))
                df.columns = df.columns.str.strip()
                print(f"    {len(df)} rows (txt/csv)")
                return df

            print(f"    Unrecognised response format. First 200 chars: {content[:200]}")
            return pd.DataFrame()

        except Exception as e:
            print(f"    Attempt {attempt}/{retries} failed for '{provinsi}': {e}")
            if attempt < retries:
                time.sleep(5 * attempt)

    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Spatial layer caches — loaded once per run to avoid repeated DB reads
# ---------------------------------------------------------------------------

_landcover_cache  = None
_lc_columns_cache = None
_concession_cache = None
_village_cache    = None


def get_landcover():
    """
    Load landcover GeoDataFrame with ALL available pl_* columns.
    Cached after first call. Returns (GeoDataFrame, list_of_pl_columns).
    """
    global _landcover_cache, _lc_columns_cache
    if _landcover_cache is not None:
        return _landcover_cache, _lc_columns_cache

    print("Loading landcover table (once)...")
    lc_schema, lc_table = TABLE_LANDCOVER.split(".")

    with engine.connect() as conn:
        result = conn.execute(text(f"""
            SELECT 
                a.attname AS column_name,
                format_type(a.atttypid, a.atttypmod) AS data_type
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = :s
            AND c.relname = :t
            AND a.attnum > 0
            AND NOT a.attisdropped
            AND a.attname ILIKE '%pl%'
            ORDER BY a.attname;
        """), {"s": lc_schema, "t": lc_table})
        lc_cols = [r[0] for r in result]

    if not lc_cols:
        print("  [WARN] No pl_* columns found in landcover table. Landcover enrichment skipped.")
        _landcover_cache  = gpd.GeoDataFrame()
        _lc_columns_cache = []
        return _landcover_cache, _lc_columns_cache

    print(f"  Available landcover columns: {lc_cols}")
    select_cols = ", ".join(lc_cols) + ", geometry"
    with engine.connect() as conn:
        lc_gdf = gpd.read_postgis(
            text(f"SELECT {select_cols} FROM {TABLE_LANDCOVER}"),
            conn,
            geom_col="geometry",
        )

    if not lc_gdf.empty:
        if lc_gdf.crs is None:
            lc_gdf.set_crs("EPSG:4326", inplace=True)
        else:
            lc_gdf = lc_gdf.to_crs("EPSG:4326")
        lc_gdf["geometry"] = lc_gdf["geometry"].buffer(0)
    else:
        if lc_gdf.crs is None:
            lc_gdf.set_crs("EPSG:4326", inplace=True)
    print(f"  Landcover loaded: {len(lc_gdf)} polygons, columns: {lc_cols}")

    _landcover_cache  = lc_gdf
    _lc_columns_cache = lc_cols
    return _landcover_cache, _lc_columns_cache


def get_concession():
    """
    Load concession GeoDataFrame (uid_concession, concession, geometry).
    Cached after first call.
    """
    global _concession_cache
    if _concession_cache is not None:
        return _concession_cache

    print("Loading concession table (once)...")
    with engine.connect() as conn:
        gdf = gpd.read_postgis(
            text(
                f"SELECT uid as uid_concession, namobj as concession,"
                f" ST_MakeValid(ST_Force2D(geometry)) as geometry"
                f" FROM {TABLE_CONCESSION}"
            ),
            conn,
            geom_col="geometry",
        )
    if not gdf.empty:
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
        else:
            gdf = gdf.to_crs("EPSG:4326")
        gdf["geometry"] = gdf["geometry"].buffer(0)
    else:
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
    print(f"  Concession loaded: {len(gdf)} polygons")
    _concession_cache = gdf
    return _concession_cache


def get_village():
    """
    Load village GeoDataFrame (village, subdistrict, district, province, geometry).
    Cached after first call.
    """
    global _village_cache
    if _village_cache is not None:
        return _village_cache

    print("Loading village table (once)...")
    with engine.connect() as conn:
        gdf = gpd.read_postgis(
            text(
                f"SELECT wadmkd as village, wadmkc as subdistrict,"
                f" wadmkk as district, wadmpr as province,"
                f" ST_MakeValid(ST_Force2D(geometry)) as geometry"
                f" FROM {TABLE_VILLAGE}"
            ),
            conn,
            geom_col="geometry",
        )
    if not gdf.empty:
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
        else:
            gdf = gdf.to_crs("EPSG:4326")
        gdf["geometry"] = gdf["geometry"].buffer(0)
    else:
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
    print(f"  Village loaded: {len(gdf)} polygons")
    _village_cache = gdf
    return _village_cache


def resolve_lctype(row, lc_cols):
    """
    Pick the lctype value for this row from the year-matched pl_ column.
    Falls back to nearest earlier year if exact match not available.
    """
    alert_year = int(row.get("year_loc", 0) or 0)
    # Try exact year, then walk backwards up to 4 years
    for y in range(alert_year, alert_year - 5, -1):
        col = f"pl_{y}"
        if col in lc_cols and pd.notna(row.get(col)):
            return row[col]
    return None


# ---------------------------------------------------------------------------
# DB Schema helper
# ---------------------------------------------------------------------------

def get_table_columns(schema_name, table_name):
    """
    Fetch column names for a database table from information_schema.
    Returns empty list if table does not exist.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = :s AND table_name = :t
            """), {"s": schema_name, "t": table_name})
            return [r[0] for r in result]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Raw table insert (parsed API records before spatial joins)
# ---------------------------------------------------------------------------

def insert_raw(gdf_raw, provinsi, chunk_start, chunk_end):
    """
    Save parsed raw GeoDataFrame to monitoring.alert_fire_sipongi_raw.
    Deduplicates against existing records using composite key (lat, long, date_hotspot_ori, sumber).
    """
    if gdf_raw is None or gdf_raw.empty:
        return 0

    # Deduplicate against existing raw records for this province + date range
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"""
                SELECT lat, long, date_hotspot_ori, sumber
                FROM {SCHEMA}.{TABLE_RAW}
                WHERE acq_date >= :s AND acq_date < :e
                  AND nama_provinsi = :p
            """), {"s": chunk_start, "e": chunk_end, "p": provinsi})
            rows = result.fetchall()
            if rows:
                existing_keys = set()
                for r in rows:
                    e_lat = round(float(r[0]), 5) if r[0] is not None else None
                    e_long = round(float(r[1]), 5) if r[1] is not None else None
                    e_dt = pd.to_datetime(r[2]).tz_localize(None) if pd.notna(r[2]) else None
                    e_sumber = str(r[3]).strip() if r[3] is not None else ""
                    existing_keys.add((e_lat, e_long, e_dt, e_sumber))

                def is_new_raw(row):
                    lat = round(float(row['lat']), 5) if pd.notna(row['lat']) else None
                    long = round(float(row['long']), 5) if pd.notna(row['long']) else None
                    dt = pd.to_datetime(row['date_hotspot_ori']).tz_localize(None) if pd.notna(row['date_hotspot_ori']) else None
                    sumber = str(row['sumber']).strip() if pd.notna(row['sumber']) else ""
                    return (lat, long, dt, sumber) not in existing_keys

                initial_cnt = len(gdf_raw)
                gdf_raw = gdf_raw[gdf_raw.apply(is_new_raw, axis=1)].copy()
                dropped = initial_cnt - len(gdf_raw)
                if dropped > 0:
                    print(f"    (dedup raw: {dropped} duplicates dropped)")
    except Exception:
        pass  # Table does not exist yet

    if gdf_raw.empty:
        return 0

    raw_desired_cols = [
        "lat", "long", "sumber", "nama_provinsi", "kabkota", "kecamatan", "desa",
        "counter", "confidence_level", "confidence", "date_hotspot_ori", "date_hotspot", "hs_id",
        "acq_date", "acq_time", "datetm", "date", "wdate", "year", "month", "day",
        "datetm_loc", "date_loc", "wdate_loc", "year_loc", "month_loc", "day_loc",
        "uid_alert", "date_processing", "geometry",
    ]

    existing_cols = get_table_columns(SCHEMA, TABLE_RAW)
    if existing_cols:
        cols_to_keep = [c for c in raw_desired_cols if c in existing_cols and c in gdf_raw.columns]
    else:
        cols_to_keep = [c for c in raw_desired_cols if c in gdf_raw.columns]

    gdf_to_insert = gpd.GeoDataFrame(gdf_raw[cols_to_keep].copy(), geometry="geometry", crs="EPSG:4326")

    gdf_to_insert.to_postgis(
        name=TABLE_RAW,
        con=engine,
        schema=SCHEMA,
        if_exists="append",
        index=False,
        dtype={"geometry": Geometry("POINT", srid=4326)},
    )
    return len(gdf_to_insert)


# ---------------------------------------------------------------------------
# Enriched processing + landcover join
# ---------------------------------------------------------------------------

def process_and_enrich(df_raw, provinsi, chunk_start, chunk_end, save_raw=True, raw_only=False):
    """
    Process raw SIPONGI DataFrame — logic mirrors update_alert_fire_sipongi_v2.py
    exactly, with two additions:
      • flexible column name resolution before processing
      • landcover spatial left-join (year-matched pl_year column) at the end

    Returns (enriched_gdf, n_raw_inserted).
    """
    if df_raw.empty:
        return gpd.GeoDataFrame(), 0

    # ---- Step A: resolve column names (handles historical API name differences) ----
    raw_df = resolve_columns(df_raw.copy())

    REQUIRED = {"Latitude", "Longitude", "Tanggal", "Waktu"}
    missing  = REQUIRED - set(raw_df.columns)
    if missing:
        print(f"    [ENRICH] Still missing required columns after mapping: {missing}")
        return gpd.GeoDataFrame(), 0

    # ---- Step B: same as v2 Step 3 — column mapping & normalization ----
    raw_df["lat"]  = pd.to_numeric(raw_df["Latitude"],  errors="coerce")
    raw_df["long"] = pd.to_numeric(raw_df["Longitude"], errors="coerce")
    raw_df = raw_df.dropna(subset=["lat", "long"])
    if raw_df.empty:
        return gpd.GeoDataFrame(), 0

    raw_df["sumber"]        = raw_df["Satelit"].astype(str).str.strip()   if "Satelit"   in raw_df.columns else provinsi
    raw_df["nama_provinsi"] = raw_df["Provinsi"].astype(str).str.strip()  if "Provinsi"  in raw_df.columns else provinsi
    raw_df["kabkota"]       = raw_df["Kab Kota"].astype(str).str.strip()  if "Kab Kota"  in raw_df.columns else ""
    raw_df["kecamatan"]     = raw_df["Kecamatan"].astype(str).str.strip() if "Kecamatan" in raw_df.columns else ""
    raw_df["desa"]          = raw_df["Desa"].astype(str).str.strip()      if "Desa"      in raw_df.columns else ""
    raw_df["counter"]       = 1

    conf_str = raw_df["Confidence"].astype(str).str.strip().str.lower() if "Confidence" in raw_df.columns else pd.Series("", index=raw_df.index)
    raw_df["confidence_level"] = conf_str
    conf_map                   = {"high": 80, "medium": 50, "low": 20}
    raw_df["confidence"]       = conf_str.map(conf_map).fillna(50).astype(int)

    # Parse datetime — same format as v2: "02-09-2026" + "14:29 WIB"
    clean_time       = raw_df["Waktu"].astype(str).str.replace(" WIB", "", regex=False).str.strip()
    datetime_wib_str = raw_df["Tanggal"].astype(str).str.strip() + " " + clean_time
    dt_wib           = pd.to_datetime(datetime_wib_str, format="%d-%m-%Y %H:%M", errors="coerce")
    if dt_wib.isna().all():
        dt_wib       = pd.to_datetime(datetime_wib_str, dayfirst=True, errors="coerce")

    raw_df  = raw_df[dt_wib.notna()].copy()
    dt_wib  = dt_wib[dt_wib.notna()]
    if raw_df.empty:
        return gpd.GeoDataFrame(), 0

    # Convert WIB -> UTC (tz-naive, same convention as v2)
    dt_utc = dt_wib - pd.Timedelta(hours=7)
    raw_df["date_hotspot_ori"] = dt_utc.values          # tz-naive UTC
    raw_df["date_hotspot"]     = dt_wib.dt.strftime("%A, %d %B %Y %H:%M:%S").values
    raw_df["hs_id"]            = dt_utc.astype(str).values

    # ---- Step C: Build GeoDataFrame (same as v2 Step 4) ----
    gdf = gpd.GeoDataFrame(
        raw_df,
        geometry=[Point(xy) for xy in zip(raw_df["long"], raw_df["lat"])],
        crs="EPSG:4326",
    )

    # ---- Step D: derived time columns — identical to v2 Step 6 ----
    # Use pd.to_datetime(gdf[col]) -> Series so .dt accessor works correctly
    base_dt = pd.to_datetime(gdf["date_hotspot_ori"])
    gdf["datetm"] = base_dt

    base_dt_utc = base_dt.dt.tz_localize("UTC")

    gdf["acq_date"] = base_dt.dt.date
    gdf["acq_time"] = base_dt.dt.time
    gdf["date"]     = base_dt.dt.date
    gdf["wdate"]    = (
        pd.to_datetime(gdf["date"])
        + (7 - pd.to_datetime(gdf["date"]).dt.weekday) * np.timedelta64(1, "D")
    )
    gdf["year"]  = gdf["wdate"].dt.year
    gdf["month"] = gdf["wdate"].dt.month
    gdf["day"]   = gdf["wdate"].dt.day

    # Local time (WIB — Asia/Jakarta)
    datetm_loc        = base_dt_utc.dt.tz_convert("Asia/Jakarta")
    gdf["datetm_loc"] = datetm_loc.dt.tz_localize(None)  # tz-naive WIB wall-clock
    date_loc          = pd.to_datetime(gdf["datetm_loc"]).dt.date
    gdf["date_loc"]   = pd.to_datetime(date_loc, format="%Y-%m-%d")
    gdf["wdate_loc"]  = (
        gdf["date_loc"]
        + (7 - gdf["date_loc"].dt.weekday) * np.timedelta64(1, "D")
    )
    gdf["year_loc"]  = gdf["date_loc"].dt.year
    gdf["month_loc"] = gdf["date_loc"].dt.month
    gdf["day_loc"]   = gdf["date_loc"].dt.day

    # datetm keep as tz-naive UTC
    gdf["datetm"] = base_dt

    gdf["uid_alert"]       = [str(uuid.uuid4()) for _ in range(len(gdf))]
    gdf["date_processing"] = datetime.utcnow()

    # ---- Step D.5: Deduplicate hotspots by lat, long, sumber, date_hotspot_ori ----
    gdf = gdf.drop_duplicates(subset=["lat", "long", "sumber", "date_hotspot_ori"]).copy()

    # ---- Step E: Save to RAW table BEFORE spatial joins ----
    n_raw = 0
    if save_raw:
        n_raw = insert_raw(gdf, provinsi, chunk_start, chunk_end)

    if raw_only:
        return gpd.GeoDataFrame(), n_raw

    # -----------------------------------------------------------------------
    # Spatial pipeline — mirrors update_alert_fire_sipongi_v2.py Steps 7-9
    # -----------------------------------------------------------------------

    def _drop_index_cols(g):
        for col in ["index_left", "index_right"]:
            if col in g.columns:
                g = g.drop(columns=[col])
        return g.reset_index(drop=True)

    # Step 7: Intersect with concession (INNER) — drops points outside concessions
    concession = get_concession()
    if not concession.empty:
        print(f"    Intersecting {len(gdf)} points with concession (inner)...", end=" ", flush=True)
        gdf = _drop_index_cols(gdf)
        gdf = gpd.sjoin(gdf, concession, how="inner", predicate="within")
        gdf = _drop_index_cols(gdf)
        print(f"{len(gdf)} remain after concession filter.")

        if gdf.empty:
            print("    No points inside any concession. Skipping chunk.")
            return gpd.GeoDataFrame(), n_raw
    else:
        print(f"    (info) Tabel concession kosong; filter dilewati.")
        gdf["uid_concession"] = None
        gdf["concession"] = None

    # Step 8: Enrich with village (LEFT) — keeps all concession points
    villages = get_village()
    if not villages.empty:
        print(f"    Joining {len(gdf)} points with village (left)...", end=" ", flush=True)
        gdf = _drop_index_cols(gdf)
        gdf = gpd.sjoin(gdf, villages, how="left", predicate="within")
        gdf = _drop_index_cols(gdf)
        print(f"done.")
    else:
        gdf["village"] = None
        gdf["subdistrict"] = None
        gdf["district"] = None
        gdf["province"] = provinsi

    # Step 9: Enrich with landcover (LEFT, year-matched pl_year column)
    lc_gdf, lc_cols = get_landcover()
    if not lc_gdf.empty and lc_cols:
        print(f"    Joining {len(gdf)} points with landcover (left)...", end=" ", flush=True)
        gdf = _drop_index_cols(gdf)
        joined = gpd.sjoin(gdf, lc_gdf, how="left", predicate="within")
        joined = _drop_index_cols(joined)
        # Pick year-appropriate pl_ column per row, then drop raw pl_* columns
        joined["lctype"] = joined.apply(lambda row: resolve_lctype(row, lc_cols), axis=1)
        joined = joined.drop(columns=[c for c in lc_cols if c in joined.columns], errors="ignore")
        gdf = joined
        print(f"done.")
    else:
        gdf["lctype"] = None

    return gdf, n_raw


def insert_enriched(gdf, chunk_start, chunk_end, provinsi):
    """
    Insert into alert_fire_sipongi (production table) with composite key deduplication (lat, long, date_hotspot_ori, sumber).
    Column list mirrors the production schema from update_alert_fire_sipongi_v2.py.
    Returns number of rows inserted.
    """
    if gdf is None or gdf.empty:
        return 0

    # Deduplicate against existing enriched records for this province + date range
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"""
                SELECT lat, long, date_hotspot_ori, sumber
                FROM {SCHEMA}.{TABLE_ENRICHED}
                WHERE acq_date >= :s AND acq_date < :e
                  AND nama_provinsi = :p
            """), {"s": chunk_start, "e": chunk_end, "p": provinsi})
            rows = result.fetchall()
            if rows:
                existing_keys = set()
                for r in rows:
                    e_lat = round(float(r[0]), 5) if r[0] is not None else None
                    e_long = round(float(r[1]), 5) if r[1] is not None else None
                    e_dt = pd.to_datetime(r[2]).tz_localize(None) if pd.notna(r[2]) else None
                    e_sumber = str(r[3]).strip() if r[3] is not None else ""
                    existing_keys.add((e_lat, e_long, e_dt, e_sumber))

                def is_new_enriched(row):
                    lat = round(float(row['lat']), 5) if pd.notna(row['lat']) else None
                    long = round(float(row['long']), 5) if pd.notna(row['long']) else None
                    dt = pd.to_datetime(row['date_hotspot_ori']).tz_localize(None) if pd.notna(row['date_hotspot_ori']) else None
                    sumber = str(row['sumber']).strip() if pd.notna(row['sumber']) else ""
                    return (lat, long, dt, sumber) not in existing_keys

                initial_cnt = len(gdf)
                gdf = gdf[gdf.apply(is_new_enriched, axis=1)].copy()
                dropped = initial_cnt - len(gdf)
                if dropped > 0:
                    print(f"    (dedup archive: {dropped} duplicates dropped)")
    except Exception:
        pass

    if gdf.empty:
        return 0

    # Column list matching monitoring.alert_fire_sipongi schema (same as v2)
    desired_cols = [
        "lat", "long",
        "sumber", "nama_provinsi", "kabkota", "kecamatan", "desa",
        "confidence_level", "confidence",
        "date_hotspot_ori", "date_hotspot", "hs_id",
        "acq_date", "acq_time",
        "datetm", "date", "wdate", "year", "month", "day",
        "datetm_loc", "date_loc", "wdate_loc", "year_loc", "month_loc", "day_loc",
        "uid_concession", "concession",      # from concession inner join
        "village", "subdistrict", "district", "province",  # from village left join
        "lctype",                             # from landcover left join
        "uid_alert", "date_processing",
        "geometry",
    ]
    
    existing_cols = get_table_columns(SCHEMA, TABLE_ENRICHED)
    if existing_cols:
        cols_to_keep = [c for c in desired_cols if c in existing_cols and c in gdf.columns]
    else:
        cols_to_keep = [c for c in desired_cols if c in gdf.columns]

    gdf_to_insert = gpd.GeoDataFrame(gdf[cols_to_keep].copy(), geometry="geometry", crs="EPSG:4326")

    gdf_to_insert.to_postgis(
        name=TABLE_ENRICHED,
        con=engine,
        schema=SCHEMA,
        if_exists="append",
        index=False,
        dtype={"geometry": Geometry("POINT", srid=4326)},
    )
    return len(gdf_to_insert)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Backfill SIPONGI fire hotspot archive for Kalimantan",
    )
    parser.add_argument("--start",      default="2023-01-01",      help="Start date YYYY-MM-DD")
    parser.add_argument("--end",        default=str(date.today()),  help="End date YYYY-MM-DD")
    parser.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS,
                        help=f"Days per API request (default: {DEFAULT_CHUNK_DAYS})")
    parser.add_argument("--confidence", default="all",
                        choices=["all", "high", "medium", "low"],
                        help="Confidence level filter (default: all)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from checkpoint file")
    parser.add_argument("--no-raw",     action="store_true",
                        help="Skip writing to _raw table")
    parser.add_argument("--raw-only",   action="store_true",
                        help="Download raw data to _raw table only without spatial intersect")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date   = date.fromisoformat(args.end)
    chunk_days = args.chunk_days
    checkpoint = load_checkpoint() if args.resume else {}

    target_provinces = await get_target_provinces()

    print(f"\n{'='*70}")
    print(f"SIPONGI Kalimantan Backfill  |  {start_date} -> {end_date}")
    print(f"Provinces    : {len(target_provinces)} provinces ({', '.join(target_provinces)})")
    print(f"Chunk        : {chunk_days} days  |  Confidence: {args.confidence}")
    print(f"Resume       : {args.resume}")
    print(f"Raw table    : {SCHEMA}.{TABLE_RAW}")
    print(f"Main table   : {SCHEMA}.{TABLE_ENRICHED}  (concession+village+landcover enriched)")
    print(f"{'='*70}\n")

    grand_total_raw = 0
    grand_total_enr = 0

    for prov in target_provinces:
        print(f"\n=== Province: {prov} ===")
        prov_raw = 0
        prov_enr = 0
        chunk_start = start_date

        while chunk_start < end_date:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end_date)
            ck_key    = checkpoint_key(prov, chunk_start)

            # Skip already processed chunks when resuming
            if args.resume and checkpoint.get(ck_key) == "done":
                print(f"  [{chunk_start}] already done (checkpoint). Skipping.")
                chunk_start = chunk_end
                continue

            print(f"  [{chunk_start} -> {chunk_end}] '{prov}'...", end=" ", flush=True)

            raw_df = fetch_sipongi_historical(
                prov, chunk_start, chunk_end, confidence=args.confidence
            )

            if raw_df.empty:
                print("  (no data from API)")
            else:
                enriched_gdf, n_raw = process_and_enrich(
                    raw_df, prov, chunk_start, chunk_end, save_raw=not args.no_raw, raw_only=args.raw_only
                )
                prov_raw += n_raw
                if n_raw > 0:
                    print(f"    -> raw: {n_raw} rows saved to {TABLE_RAW}")

                if enriched_gdf.empty:
                    print(f"    -> archive: 0 rows (processing failed or no valid rows)")
                else:
                    n_enr = insert_enriched(enriched_gdf, chunk_start, chunk_end, prov)
                    prov_enr += n_enr
                    print(f"    -> archive: {n_enr} rows saved to {TABLE_ENRICHED}")

            checkpoint[ck_key] = "done"
            save_checkpoint(checkpoint)

            chunk_start = chunk_end
            time.sleep(4)

        print(f"  [{prov}] raw={prov_raw}, archive={prov_enr}")
        grand_total_raw += prov_raw
        grand_total_enr += prov_enr

    print(f"\n{'='*70}")
    print(f"Backfill complete.")
    print(f"  Raw rows inserted   : {grand_total_raw} -> {SCHEMA}.{TABLE_RAW}")
    print(f"  Archive rows inserted: {grand_total_enr} -> {SCHEMA}.{TABLE_ENRICHED}")
    print(f"  Checkpoint file     : {CHECKPOINT_FILE}")
    print(f"{'='*70}\n")



if __name__ == "__main__":
    asyncio.run(main())
