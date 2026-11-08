"""
Analisis titik awal & arah penyebaran hotspot (versi ringkas).

Tabel monitoring.fire_spread hanya menyimpan baris yang berguna untuk peta:
  - titik awal klaster (is_origin), dan
  - titik induk yang punya anak (is_parent).
Titik daun (tanpa anak) TIDAK disimpan -> tabel jauh lebih kecil.

Kolom: uid_alert, source, datetm, lon, lat, cluster_id, is_origin, is_parent, direction[]
  direction = array datar float8 berisi triple per anak: [lon, lat, t_menit_epoch_UTC, lon, lat, t, ...]
  (panah = dari titik ini ke tiap anak; waktu anak dipakai agar panah muncul bersama anaknya)

Langkah:
  1. Muat hotspot (FIRMS + SIPONGI) dalam jendela waktu (+ lookback).
  2. Klaster ruang-waktu (jarak <= SPATIAL_EPS_M, selisih waktu <= TEMPORAL_EPS_H).
  3. Titik awal = deteksi paling awal di klaster.
  4. Penautan pohon rentang minimum (Prim) -> daftar anak per induk.
  5. Ganti isi tabel pada jendela waktu (DELETE + INSERT dalam satu transaksi).

Usage:
  python scripts/fire_dir.py                      # 14 hari terakhir, tabel raw
  python scripts/fire_dir.py --days 30
  python scripts/fire_dir.py --start 2026-09-01 --end 2026-09-20
  python scripts/fire_dir.py --tables final
  python scripts/fire_dir.py --dry-run

Prasyarat: init-db/05-fire-spread-slim.sql

File: scripts/fire_dir.py
"""
from __future__ import annotations

import argparse
import asyncio
import heapq
import os
import sys
import uuid
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

# ============================================================
# CONFIG
# ============================================================
SPATIAL_EPS_M = 3000        # CONFIG: jarak maks antar titik se-klaster / parent (meter)
TEMPORAL_EPS_H = 24         # CONFIG: selisih waktu maks (jam)
MIN_CLUSTER_SIZE = 2        # CONFIG: klaster lebih kecil = 'isolated' (tidak disimpan)
MIN_LINK_LAG_MIN = 60       # CONFIG: toleransi satu lintasan satelit (menit)
TIME_WEIGHT_M_PER_H = 150   # CONFIG: biaya tautan = jarak_m + bobot * selisih_jam
MIN_ARROW_DIST_M = 50       # CONFIG: tautan lebih pendek dari ini tidak dijadikan panah (titik kembar antar sensor)
MAX_CHILDREN = 32           # CONFIG: maks anak per induk yang disimpan di direction[]
DEFAULT_DAYS = 14           # CONFIG: jendela analisis default
LOOKBACK_DAYS = 7           # CONFIG: data tambahan sebelum jendela agar klaster di tepi kiri tidak terpotong
KNN_MAX_NEIGHBORS = 16      # CONFIG: maks tetangga per titik
QUERY_CHUNK = 100_000       # CONFIG: ukuran batch pencarian tetangga
COORD_DECIMALS = 5          # CONFIG: pembulatan koordinat di direction[] (5 = ~1 m)
EARTH_R = 6371008.8

TABLES_RAW = [
    ("alert_fire_modis_raw", "firms"),
    ("alert_fire_viirssnpp_raw", "firms"),
    ("alert_fire_viirsnoaa20_raw", "firms"),
    ("alert_fire_viirsnoaa21_raw", "firms"),
    ("alert_fire_sipongi_raw", "sipongi"),
]
TABLES_FINAL = [
    ("alert_fire_modis", "firms"),
    ("alert_fire_viirssnpp", "firms"),
    ("alert_fire_viirsnoaa20", "firms"),
    ("alert_fire_viirsnoaa21", "firms"),
    ("alert_fire_sipongi", "sipongi"),
]

# kolom yang disimpan (urutan = urutan INSERT)
DB_COLS = ["uid_alert", "source", "datetm", "lon", "lat",
           "cluster_id", "is_origin", "is_parent", "direction"]
OUT_COLS = DB_COLS + ["edge_censored"]   # edge_censored hanya dipakai saat filter, tidak disimpan


# ============================================================
# INTI ANALISIS (murni, tanpa DB)
# ============================================================
def _cluster(lat, lon, t_h):
    """Klaster ruang-waktu (komponen terhubung) + matriks tetangga CSR (data = jarak m + 1e-3)."""
    n = len(lat)
    la, lo = np.radians(lat), np.radians(lon)
    xyz = EARTH_R * np.c_[np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]
    feats = np.c_[xyz / SPATIAL_EPS_M, t_h / TEMPORAL_EPS_H]
    tree = cKDTree(feats)
    rows, cols, ds = [], [], []
    k = KNN_MAX_NEIGHBORS + 1
    for s0 in range(0, n, QUERY_CHUNK):
        q = np.arange(s0, min(n, s0 + QUERY_CHUNK))
        dd, ii = tree.query(feats[q], k=k, p=np.inf, distance_upper_bound=1.0, workers=-1)
        ii = np.asarray(ii).reshape(len(q), -1)
        src = np.repeat(q, ii.shape[1])
        dst = ii.ravel()
        ok = (dst < n) & (dst != src)
        src, dst = src[ok], dst[ok]
        chord = np.linalg.norm(xyz[src] - xyz[dst], axis=1)
        ok = (chord <= SPATIAL_EPS_M) & (np.abs(t_h[src] - t_h[dst]) <= TEMPORAL_EPS_H)
        src, dst, chord = src[ok], dst[ok], chord[ok]
        a_, b_ = np.minimum(src, dst), np.maximum(src, dst)
        rows.append(a_.astype(np.int32)); cols.append(b_.astype(np.int32)); ds.append(chord.astype(np.float32))
    r, c, d = np.concatenate(rows), np.concatenate(cols), np.concatenate(ds)
    if r.size == 0:
        return np.arange(n), None
    g = coo_matrix((d + 1e-3, (r, c)), shape=(n, n)).tocsr()
    g = g.maximum(g.T)
    _, labels = connected_components(g, directed=False)
    return labels, g


def _link(adj, t_h, labels):
    """Pohon tautan terarah per klaster (Prim, tumbuh maju dalam waktu).

    Mengembalikan parent[], pdist[] (jarak ke induk, m) serta nxt[], ndist[] untuk akar cabang
    yang tidak punya anak (panah tambahan ke tetangga terdekat yang lebih akhir).
    """
    n = len(t_h)
    parent = np.full(n, -1, dtype=np.int64)
    pdist = np.full(n, np.nan)
    nxt = np.full(n, -1, dtype=np.int64)
    ndist = np.full(n, np.nan)
    if adj is None:
        return parent, pdist, nxt, ndist
    ip, ix, dt_ = adj.indptr, adj.indices, adj.data
    tol = MIN_LINK_LAG_MIN / 60.0
    visited = np.zeros(n, dtype=bool)

    def cost(k, u, v):
        return dt_[k] + TIME_WEIGHT_M_PER_H * abs(t_h[v] - t_h[u])

    for root in range(n):
        if visited[root]:
            continue
        visited[root] = True
        h = []

        def push(u):
            for k in range(ip[u], ip[u + 1]):
                v = ix[k]
                if not visited[v] and t_h[v] >= t_h[u] - tol:
                    heapq.heappush(h, (cost(k, u, v), v, u, dt_[k] - 1e-3))

        push(root)
        while h:
            _, v, u, dm = heapq.heappop(h)
            if visited[v]:
                continue
            visited[v] = True
            parent[v] = u
            pdist[v] = dm
            push(v)

    is_origin_node = np.zeros(n, dtype=bool)
    is_origin_node[np.unique(labels, return_index=True)[1]] = True
    child = np.bincount(parent[parent >= 0], minlength=n)
    for _ in range(50):
        orph = np.flatnonzero((parent < 0) & (child == 0) & (np.diff(ip) > 0))
        if orph.size == 0:
            break
        for v in orph:
            if parent[v] >= 0 or child[v] > 0:
                continue
            best = None
            for k in range(ip[v], ip[v + 1]):
                w = ix[k]
                p = parent[w]
                if p < 0 or t_h[w] < t_h[v] - tol:
                    continue
                if is_origin_node[p] and child[p] == 1:
                    continue
                strands = parent[p] < 0 and child[p] == 1
                c = (strands, cost(k, v, w), int(w), dt_[k] - 1e-3)
                if best is None or c < best:
                    best = c
            if best is not None:
                w = best[2]
                child[parent[w]] -= 1
                parent[w] = v
                pdist[w] = best[3]
                child[v] += 1

    child = np.bincount(parent[parent >= 0], minlength=n)
    for v in np.flatnonzero((parent < 0) & (child == 0) & (np.diff(ip) > 0)):
        best = None
        for k in range(ip[v], ip[v + 1]):
            w = ix[k]
            if t_h[w] >= t_h[v] - tol:
                c = (cost(k, v, w), int(w), dt_[k] - 1e-3)
                if best is None or c < best:
                    best = c
        if best is not None:
            nxt[v], ndist[v] = best[1], best[2]
    return parent, pdist, nxt, ndist


def analyze(df: pd.DataFrame, window_start=None) -> pd.DataFrame:
    """df: uid_alert, source, lon, lat, datetm (UTC, naive). Mengembalikan df dengan OUT_COLS.

    Hanya baris titik awal / induk yang dikembalikan (daun dibuang).
    """
    if df.empty:
        return pd.DataFrame(columns=OUT_COLS)

    df = df.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["datetm"]):
        df["datetm"] = pd.to_datetime(df["datetm"])
    if df["datetm"].dt.tz is not None:
        df["datetm"] = df["datetm"].dt.tz_convert("UTC").dt.tz_localize(None)

    df = df.drop_duplicates("uid_alert").sort_values(["datetm", "uid_alert"]).reset_index(drop=True)
    n = len(df)
    lat = df["lat"].to_numpy(float)
    lon = df["lon"].to_numpy(float)
    t0 = df["datetm"].min()
    t_h = (df["datetm"] - t0).dt.total_seconds().to_numpy() / 3600.0
    t_min_abs = (t0 - pd.Timestamp("1970-01-01")).total_seconds() / 60.0 + t_h * 60.0   # menit epoch UTC
    uid = df["uid_alert"].astype(str).to_numpy()

    labels, adj = _cluster(lat, lon, t_h)
    size = np.bincount(labels)[labels]
    parent, dist, nxt, ndist = _link(adj, t_h, labels)

    # titik awal per klaster = kemunculan pertama (df urut waktu)
    uniq, first_idx = np.unique(labels, return_index=True)
    first_of = np.empty(labels.max() + 1, dtype=np.int64)
    first_of[uniq] = first_idx
    origin_pos = first_of[labels]
    cluster_uuid = {l: str(uuid.uuid5(uuid.NAMESPACE_URL, uid[first_of[l]])) for l in uniq}
    cluster_id = np.array([cluster_uuid[l] for l in labels])

    # klaster terpotong di tepi kiri data?
    if window_start is not None:
        off_h = (t0 - pd.Timestamp(window_start)).total_seconds() / 3600.0
    else:
        off_h = TEMPORAL_EPS_H
    censored = (t_h[origin_pos] + off_h) < TEMPORAL_EPS_H

    isolated = size < MIN_CLUSTER_SIZE
    is_origin = (np.arange(n) == origin_pos) & ~isolated & ~censored

    # ---- daftar tepi induk -> anak (hanya yang cukup panjang untuk jadi panah) ----
    has_p = (parent >= 0) & (dist >= MIN_ARROW_DIST_M)
    c_idx = np.flatnonzero(has_p)
    src = parent[c_idx]
    dst = c_idx
    has_n = (nxt >= 0) & (ndist >= MIN_ARROW_DIST_M)
    v_idx = np.flatnonzero(has_n)
    src = np.concatenate([src, v_idx])
    dst = np.concatenate([dst, nxt[v_idx]])

    direction = {}
    if src.size:
        e = pd.DataFrame({"src": src, "dst": dst}).drop_duplicates()
        e["t"] = t_h[e["dst"].to_numpy()]
        e = e.sort_values(["src", "t", "dst"]).groupby("src", sort=False).head(MAX_CHILDREN)
        for s, g in e.groupby("src", sort=False):
            d = g["dst"].to_numpy()
            tri = np.c_[np.round(lon[d], COORD_DECIMALS), np.round(lat[d], COORD_DECIMALS),
                        np.round(t_min_abs[d], 1)]
            direction[int(s)] = tri.ravel().tolist()

    is_parent = np.zeros(n, dtype=bool)
    is_parent[list(direction.keys())] = True
    keep = is_origin | is_parent

    out = pd.DataFrame({
        "uid_alert": uid,
        "source": df["source"].to_numpy(),
        "datetm": df["datetm"].to_numpy(),
        "lon": lon, "lat": lat,
        "cluster_id": cluster_id,
        "is_origin": is_origin,
        "is_parent": is_parent,
        "direction": [direction.get(i, []) for i in range(n)],
        "edge_censored": censored & ~isolated,
    })
    return out[keep][OUT_COLS].reset_index(drop=True)


# ============================================================
# DB
# ============================================================
def _get_engine():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for p in (root, os.path.join(root, "backend")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from app.db.db_connect import sync_engine
    return sync_engine


def load_hotspots(engine, tables, start: datetime, end: datetime) -> pd.DataFrame:
    from sqlalchemy import text
    frames = []
    with engine.connect() as conn:
        conn.execute(text("SET TIME ZONE 'UTC'"))
        for tbl, src in tables:
            ok = conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"monitoring.{tbl}"}).scalar()
            if not ok:
                print(f"  (lewati) monitoring.{tbl} tidak ada")
                continue
            sql = text(f"""
                SELECT uid_alert::text AS uid_alert, '{src}' AS source,
                       ST_X(geometry) AS lon, ST_Y(geometry) AS lat,
                       datetm::timestamp AS datetm
                FROM monitoring.{tbl}
                WHERE datetm >= :s AND datetm < :e AND uid_alert IS NOT NULL AND geometry IS NOT NULL
            """)
            d = pd.read_sql(sql, conn, params={"s": start, "e": end})
            print(f"  monitoring.{tbl}: {len(d)} baris")
            frames.append(d)
    if not frames:
        return pd.DataFrame(columns=["uid_alert", "source", "lon", "lat", "datetm"])
    df = pd.concat(frames, ignore_index=True)
    df["datetm"] = pd.to_datetime(df["datetm"])
    return df


def save(engine, out: pd.DataFrame, lo: datetime, hi: datetime,
         page_size: int = 2000, chunk: int = 20_000):
    """Ganti isi tabel pada [lo, hi) dalam satu transaksi (baris lama yang tak lagi jadi induk ikut hilang)."""
    from psycopg2.extras import execute_values
    cols = ", ".join(DB_COLS)
    upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in DB_COLS if c != "uid_alert")
    sql = (f"INSERT INTO monitoring.fire_spread ({cols}) VALUES %s "
           f"ON CONFLICT (uid_alert) DO UPDATE SET {upd}, analyzed_at = now()")
    raw = engine.raw_connection()
    total = 0
    try:
        cur = raw.cursor()
        cur.execute("DELETE FROM monitoring.fire_spread WHERE datetm >= %s AND datetm < %s", (lo, hi))
        print(f"  dihapus {cur.rowcount} baris lama pada jendela")
        for s0 in range(0, len(out), chunk):
            part = out.iloc[s0:s0 + chunk]
            rows = [
                (u, s, d.to_pydatetime(), float(x), float(y), c, bool(o), bool(p), dr)
                for u, s, d, x, y, c, o, p, dr in zip(
                    part["uid_alert"], part["source"], part["datetm"], part["lon"], part["lat"],
                    part["cluster_id"], part["is_origin"], part["is_parent"], part["direction"])
            ]
            execute_values(cur, sql, rows, page_size=page_size,
                           template="(%s,%s,%s,%s,%s,%s::uuid,%s,%s,%s::float8[])")
            total += len(rows)
            print(f"  ...tersimpan {total}/{len(out)}")
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return total


def run(start: datetime, end: datetime, tables="raw", dry_run=False):
    engine = _get_engine()
    tbls = TABLES_RAW if tables == "raw" else TABLES_FINAL
    load_start = start - timedelta(days=LOOKBACK_DAYS)
    print(f"Analisis penyebaran: {start} -> {end} (UTC), lookback dari {load_start}, tabel={tables}")
    df = load_hotspots(engine, tbls, load_start, end)
    out = analyze(df, window_start=load_start)
    if len(out):
        dt = out["datetm"]
        keep = (dt >= pd.Timestamp(start)) | (
            ~out["edge_censored"].astype(bool) & (dt >= pd.Timestamp(load_start) + pd.Timedelta(hours=TEMPORAL_EPS_H)))
        out = out[keep].reset_index(drop=True)
    n_arrow = int(out["direction"].map(len).sum() // 3) if len(out) else 0
    print(f"Disimpan: baris={len(out)} klaster={out['cluster_id'].nunique() if len(out) else 0} "
          f"titik_awal={int(out['is_origin'].sum()) if len(out) else 0} induk={int(out['is_parent'].sum()) if len(out) else 0} "
          f"panah={n_arrow}")
    if dry_run:
        return out
    lo = min(start, out["datetm"].min().to_pydatetime()) if len(out) else start
    print(f"Tersimpan: {save(engine, out, lo, end)} baris -> monitoring.fire_spread")
    return out


async def run_fire_spread_analysis():
    """Entrypoint untuk APScheduler (dijalankan di thread agar tidak memblokir event loop)."""
    end = datetime.utcnow() + timedelta(hours=1)
    start = end - timedelta(days=DEFAULT_DAYS)
    await asyncio.get_event_loop().run_in_executor(None, run, start, end, "raw", False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Analisis titik awal & arah penyebaran hotspot")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--tables", choices=["raw", "final"], default="raw")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    e = datetime.fromisoformat(a.end) if a.end else datetime.utcnow() + timedelta(hours=1)
    s = datetime.fromisoformat(a.start) if a.start else e - timedelta(days=a.days)
    run(s, e, a.tables, a.dry_run)