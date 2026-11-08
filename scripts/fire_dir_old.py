"""
Analisis titik awal & arah penyebaran hotspot (ST-DBSCAN sederhana + tautan waktu).

Hasil disimpan per rekaman (kunci uid_alert) di monitoring.fire_spread.

Langkah:
  1. Muat hotspot (FIRMS + SIPONGI) dalam jendela waktu.
  2. Klaster ruang-waktu: dua titik terhubung bila jarak <= SPATIAL_EPS_M dan
     selisih waktu <= TEMPORAL_EPS_H; klaster = komponen terhubung.
  3. Titik awal = deteksi paling awal di klaster.
  4. Penautan: pohon rentang minimum ruang-waktu (Prim) yang tumbuh dari titik awal; biaya = jarak + bobot*|dt|.
     Titik satu lintasan satelit (selisih <= MIN_LINK_LAG_MIN) boleh saling menaut. Setiap titik di klaster >= 2
     punya panah masuk (parent) atau keluar (anak / next_*). Akar cabang yang tak punya anak diberi panah next_*.
  5. Grid 0.01 derajat: nomor kotak = urutan deteksi pertama di klaster (1 = kotak titik awal).
  6. Upsert ke monitoring.fire_spread.

Usage:
  python scripts/fire_dir.py                      # 14 hari terakhir, tabel raw
  python scripts/fire_dir.py --days 30
  python scripts/fire_dir.py --start 2026-09-01 --end 2026-09-20
  python scripts/fire_dir.py --tables final       # tabel yang sudah di-intersect konsesi
  python scripts/fire_dir.py --dry-run            # hitung & cetak ringkasan, tanpa menulis

Prasyarat: init-db/03-fire-spread.sql (tabel monitoring.fire_spread) + 04-fire-spread-next.sql (kolom next_*).

File: scripts/fire_dir.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timedelta

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
MIN_CLUSTER_SIZE = 2        # CONFIG: klaster lebih kecil = 'isolated' (2 = pasangan titik tetap diberi panah)
MIN_LINK_LAG_MIN = 60       # CONFIG: toleransi satu lintasan satelit (menit): titik selisih <= ini boleh saling menaut ke dua arah
TIME_WEIGHT_M_PER_H = 150   # CONFIG: bobot waktu pada biaya tautan = jarak_m + bobot * selisih_jam
MIN_ARROW_DIST_M = 50       # CONFIG: di bawah jarak ini tautan tetap disimpan, tapi bearing = NULL (titik kembar antar sensor)
GRID_CELL_DEG = 0.0027      # CONFIG: ukuran kotak (derajat, ~300m x 300m di Kalimantan)
DEFAULT_DAYS = 14           # CONFIG: jendela analisis default
LOOKBACK_DAYS = 7           # CONFIG: data tambahan sebelum jendela agar klaster di tepi kiri tidak terpotong (hanya dihitung, tidak disimpan)
KNN_MAX_NEIGHBORS = 16      # CONFIG: maks tetangga per titik (ruang-waktu); mencegah ledakan memori di area padat
QUERY_CHUNK = 100_000       # CONFIG: ukuran batch pencarian tetangga
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

OUT_COLS = [
    "uid_alert", "source", "datetm", "lon", "lat", "cluster_id", "cluster_size", "role",
    "is_origin", "origin_uid", "parent_uid", "parent_dist_m", "parent_lag_min", "bearing_deg",
    "child_count", "hop", "grid_cell", "grid_ix", "grid_iy", "grid_order", "grid_is_origin",
    "parent_lon", "parent_lat", "edge_censored",
    "next_uid", "next_dist_m", "next_bearing_deg",
]


# ============================================================
# GEOMETRI
# ============================================================
def _hav_m(lat1, lon1, lat2, lon2):
    """Jarak haversine (meter); lat1/lon1 skalar, lat2/lon2 array."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dl = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def _bearing(lat1, lon1, lat2, lon2):
    """Bearing awal dari titik 1 ke titik 2 (derajat 0-360)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0


# ============================================================
# INTI ANALISIS (murni, tanpa DB)
# ============================================================
def _cluster(lat, lon, t_h):
    """Klaster ruang-waktu (komponen terhubung) + matriks tetangga CSR (data = jarak m + 1e-3).

    Pencarian dilakukan di ruang 4D [x, y, z (bola), waktu] ternormalisasi (norma Chebyshev), sehingga
    waktu ikut membatasi kandidat. Tiap titik hanya mengambil KNN_MAX_NEIGHBORS tetangga terdekat,
    lalu difilter tepat (jarak <= SPATIAL_EPS_M, |dt| <= TEMPORAL_EPS_H) dan disimetrikan.
    """
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
        a_, b_ = np.minimum(src, dst), np.maximum(src, dst)   # simpan sekali per pasangan
        rows.append(a_.astype(np.int32)); cols.append(b_.astype(np.int32)); ds.append(chord.astype(np.float32))
    r, c, d = np.concatenate(rows), np.concatenate(cols), np.concatenate(ds)
    if r.size == 0:
        return np.arange(n), None
    g = coo_matrix((d + 1e-3, (r, c)), shape=(n, n)).tocsr()
    g = g.maximum(g.T)                                          # simetris, tanpa menjumlah ganda
    _, labels = connected_components(g, directed=False)
    return labels, g


def _link(adj, t_h, labels):
    """Pohon tautan terarah per klaster (Prim dari titik awal, tumbuh maju dalam waktu).

    Tautan u -> v hanya boleh bila t_v >= t_u - toleransi satu lintasan; biaya = jarak + bobot*|dt|.
    Node yang tidak terjangkau (minimum waktu lokal) menjadi akar cabang; bila akar itu tidak punya
    anak, ia 'mengambil' tetangga terdekat yang lebih akhir sebagai anak -> setiap titik pasti
    punya panah masuk atau keluar. Hasil tidak bergantung urutan uid selain tie-break deterministik.
    """
    import heapq
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

    for root in range(n):          # indeks naik = waktu naik
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

    # akar tanpa anak (bukan titik tunggal) -> ambil tetangga terdekat yang lebih akhir sebagai anak.
    # Titik awal klaster tidak boleh kehilangan anak; rebutan yang tidak membuat akar lain yatim didahulukan.
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
    # sisa akar tanpa anak (dua titik awal bertemu di satu titik): panah tambahan ke tetangga terdekat yang lebih akhir
    nxt = np.full(n, -1, dtype=np.int64)
    ndist = np.full(n, np.nan)
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

    window_start: awal data yang dimuat. Klaster yang deteksi pertamanya < TEMPORAL_EPS_H dari
    window_start mungkin terpotong (induknya di luar data) -> edge_censored=True dan tidak
    diberi titik awal, supaya tidak ada titik awal palsu di tepi jendela.
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
    t_h = (df["datetm"] - df["datetm"].min()).dt.total_seconds().to_numpy() / 3600.0
    uid = df["uid_alert"].astype(str).to_numpy()

    labels, adj = _cluster(lat, lon, t_h)
    size = np.bincount(labels)[labels]

    parent, dist, nxt, ndist = _link(adj, t_h, labels)
    has_p = parent >= 0
    pidx0 = np.maximum(parent, 0)
    lag = np.where(has_p, (t_h - t_h[pidx0]) * 60.0, np.nan)
    brg = np.full(n, np.nan)
    ok = has_p & (dist >= MIN_ARROW_DIST_M)
    brg[ok] = _bearing(lat[pidx0[ok]], lon[pidx0[ok]], lat[ok], lon[ok])

    hop = np.zeros(n, dtype=np.int64)
    kids = {}
    for i in np.flatnonzero(has_p):
        kids.setdefault(int(parent[i]), []).append(int(i))
    stack = list(np.flatnonzero(~has_p))
    while stack:
        u = stack.pop()
        for c in kids.get(int(u), ()):
            hop[c] = hop[u] + 1
            stack.append(c)

    # titik awal per klaster = kemunculan pertama (df urut waktu)
    _, first_idx = np.unique(labels, return_index=True)
    first_of = np.empty(labels.max() + 1, dtype=np.int64)
    first_of[np.unique(labels)] = first_idx
    origin_pos = first_of[labels]
    origin_uid = uid[origin_pos]
    cluster_uuid = {l: str(uuid.uuid5(uuid.NAMESPACE_URL, uid[first_of[l]])) for l in np.unique(labels)}
    cluster_id = np.array([cluster_uuid[l] for l in labels])

    # klaster terpotong di tepi kiri data? (deteksi pertama masih dalam jangkauan tautan dari luar data)
    if window_start is not None:
        off_h = (df["datetm"].min() - pd.Timestamp(window_start)).total_seconds() / 3600.0
    else:
        off_h = TEMPORAL_EPS_H  # tanpa info jendela: anggap data mulai dari awal sebenarnya
    censored = (t_h[origin_pos] + off_h) < TEMPORAL_EPS_H

    isolated = size < MIN_CLUSTER_SIZE
    is_origin = (np.arange(n) == origin_pos) & ~isolated & ~censored
    role = np.where(isolated, "isolated",
                    np.where(is_origin, "origin",
                             np.where(parent >= 0, "spread", "branch_root")))
    child_count = np.bincount(parent[parent >= 0], minlength=n)

    # grid bernomor
    gix = np.floor(lon / GRID_CELL_DEG).astype(int)
    giy = np.floor(lat / GRID_CELL_DEG).astype(int)
    cell = pd.Series([f"{a}_{b}" for a, b in zip(gix, giy)])
    tmp = pd.DataFrame({"cid": cluster_id, "cell": cell, "gix": gix, "giy": giy, "t": t_h})
    cells = (tmp.groupby(["cid", "cell"], as_index=False)
                .agg(first_t=("t", "min"), gix=("gix", "first"), giy=("giy", "first"))
                .sort_values(["cid", "first_t", "gix", "giy"]))
    cells["grid_order"] = cells.groupby("cid").cumcount() + 1
    tmp = tmp.merge(cells[["cid", "cell", "grid_order"]], on=["cid", "cell"], how="left")
    origin_cell = cell.to_numpy()[origin_pos]
    grid_is_origin = (cell.to_numpy() == origin_cell) & ~isolated & ~censored
    pidx = np.maximum(parent, 0)

    out = pd.DataFrame({
        "uid_alert": uid,
        "source": df["source"].to_numpy(),
        "datetm": df["datetm"].to_numpy(),
        "lon": lon, "lat": lat,
        "cluster_id": cluster_id,
        "cluster_size": size,
        "role": role,
        "is_origin": is_origin,
        "origin_uid": origin_uid,
        "parent_uid": np.where(parent >= 0, uid[np.maximum(parent, 0)], None),
        "parent_dist_m": dist,
        "parent_lag_min": lag,
        "bearing_deg": brg,
        "child_count": child_count,
        "hop": hop,
        "grid_cell": cell.to_numpy(),
        "grid_ix": gix, "grid_iy": giy,
        "grid_order": tmp["grid_order"].to_numpy(),
        "grid_is_origin": grid_is_origin,
        "parent_lon": np.where(parent >= 0, lon[pidx], np.nan),
        "parent_lat": np.where(parent >= 0, lat[pidx], np.nan),
        "edge_censored": censored & ~isolated,
        "next_uid": np.where(nxt >= 0, uid[np.maximum(nxt, 0)], None),
        "next_dist_m": ndist,
        "next_bearing_deg": np.where(
            (nxt >= 0) & (ndist >= MIN_ARROW_DIST_M),
            _bearing(lat, lon, lat[np.maximum(nxt, 0)], lon[np.maximum(nxt, 0)]), np.nan),
    })
    return out[OUT_COLS]


# ============================================================
# DB
# ============================================================
def _get_engine():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for p in (root, os.path.join(root, "backend")):
        if p not in sys.path:
            sys.path.insert(0, p)
    # `engine` di db_connect adalah engine ASYNC; pandas/psycopg2 butuh engine sinkron.
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


def save(engine, out: pd.DataFrame, page_size: int = 2000, chunk: int = 20_000):
    """Upsert per batch agar memori tetap kecil (ratusan ribu baris)."""
    from psycopg2.extras import execute_values
    if out.empty:
        return 0
    cols = ", ".join(OUT_COLS)
    upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in OUT_COLS if c != "uid_alert")
    sql = (f"INSERT INTO monitoring.fire_spread ({cols}) VALUES %s "
           f"ON CONFLICT (uid_alert) DO UPDATE SET {upd}, analyzed_at = now()")
    total = 0
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        for s0 in range(0, len(out), chunk):
            part = out.iloc[s0:s0 + chunk]
            obj = part.astype(object).where(part.notna(), None)
            rows = [tuple(r) for r in obj.itertuples(index=False, name=None)]
            execute_values(cur, sql, rows, page_size=page_size)
            raw.commit()
            total += len(rows)
            print(f"  ...tersimpan {total}/{len(out)}")
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
    # lookback = konteks. Simpan jendela diminta + baris lookback dari klaster lengkap (agar titik awal
    # yang terjadi sebelum jendela tetap ada). Baris lain di tepi kiri tidak ditimpa (history utuh).
    if len(out):
        dt = out["datetm"]
        keep = (dt >= pd.Timestamp(start)) | (
            ~out["edge_censored"].astype(bool) & (dt >= pd.Timestamp(load_start) + pd.Timedelta(hours=TEMPORAL_EPS_H)))
        out = out[keep].reset_index(drop=True)
    n_cl = out["cluster_id"].nunique() if len(out) else 0
    n_or = int(out["is_origin"].sum()) if len(out) else 0
    print(f"Hotspot={len(out)} klaster={n_cl} titik_awal={n_or} "
          f"panah={int(out['parent_uid'].notna().sum()) if len(out) else 0}")
    if dry_run:
        return out
    print(f"Tersimpan: {save(engine, out)} baris -> monitoring.fire_spread")
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