#!/usr/bin/env python3
"""
Lapisan A: klaster spatio-temporal hotspot VIIRS, titik awal, pohon panah,
uji kestabilan, dan area bertumbuh (alpha shape) untuk Kalimantan Barat.

Alur:
    monitoring.alert_fire_{viirssnpp,viirsnoaa20,viirsnoaa21}_raw   (atau CSV FIRMS)
        -> filter provinsi + confidence
        -> proyeksi UTM 49N (EPSG:32649), satuan meter
        -> topeng sumber panas statis
        -> penomoran lintasan satelit (pass)
        -> klaster ST (jarak <= eps_s, selisih waktu <= eps_t, rantai/DBSCAN minPts=1)
        -> per klaster: akar (deteksi paling awal), pohon induk-anak, uji kestabilan
        -> keluaran: forensic_kalbar.json (untuk peta), ringkasan CSV, GeoJSON opsional

Contoh:
    python hotspot_forensic.py --start 2025-11-01 --end 2026-11-01 --out out/
    python hotspot_forensic.py --csv snpp.csv n20.csv n21.csv --out out/ --geojson
    python hotspot_forensic.py --start 2025-11-01 --end 2026-11-01 --boundary kalbar.geojson --out out/

Catatan metodologi (ringkas):
  * Titik awal = deteksi satelit pertama, bukan penyalaan sebenarnya.
  * Induk sebuah titik = titik terdekat dari lintasan yang STRICT lebih awal.
    Titik di lintasan pertama (selain akar) menjadi anak akar dan ditandai "zona awal".
  * Akar jika ada beberapa titik di lintasan pertama: FRP tertinggi.
  * Kestabilan: FRP diubah +-20% dan posisi digeser +-187,5 m (setengah piksel 375 m),
    diulang n kali; dilaporkan berapa persen akar dan induk tetap sama.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import Delaunay, cKDTree

SCHEMA = "monitoring"
SOURCES = ["viirssnpp", "viirsnoaa20", "viirsnoaa21"]
CRS_M = "EPSG:32649"           # UTM 49N, Kalbar (108E-114E) muat dalam satu zona
PIXEL_M = 375.0
HALF_PIXEL = PIXEL_M / 2
BBOX_KALBAR = (108.5, -3.2, 114.3, 2.3)   # lon_min, lat_min, lon_max, lat_max
WIB = dt.timezone(dt.timedelta(hours=7))


# --------------------------------------------------------------------------
# 1. Muat data
# --------------------------------------------------------------------------
def get_engine():
    """Pakai bootstrap yang sama dengan backfill_alert_fire_firms.py; fallback DATABASE_URL."""
    here = Path(__file__).resolve().parent
    for p in (here, here.parent, here.parent / "backend"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        import app.db.db_connect as db_connect
        return db_connect.sync_engine
    except Exception:
        pass
    import os
    from sqlalchemy import create_engine
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Tidak bisa membuat koneksi DB: set DATABASE_URL atau jalankan dari folder proyek.")
    return create_engine(url, pool_pre_ping=True)


def table_columns(engine, table):
    from sqlalchemy import text
    sql = text("""SELECT a.attname FROM pg_attribute a
                  JOIN pg_class c ON a.attrelid=c.oid JOIN pg_namespace n ON c.relnamespace=n.oid
                  WHERE n.nspname=:s AND c.relname=:t AND a.attnum>0 AND NOT a.attisdropped""")
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(sql, {"s": SCHEMA, "t": table})]


def load_from_db(start, end, province, use_boundary_table):
    from sqlalchemy import text
    engine = get_engine()
    frames = []
    lon0, lat0, lon1, lat1 = BBOX_KALBAR
    for src in SOURCES:
        table = f"alert_fire_{src}_raw"
        cols = table_columns(engine, table)
        if not cols:
            print(f"  [{src}] tabel {table} tidak ada, dilewati")
            continue
        want = ["latitude", "longitude", "frp", "confidence", "satellite", "datetm", "scan", "track", "daynight"]
        sel = ", ".join(f"p.{c}" for c in want if c in cols)
        boundary = ""
        if use_boundary_table:
            boundary = (f"AND EXISTS (SELECT 1 FROM {SCHEMA}.base_adminvillage_alert v "
                        f"WHERE upper(v.wadmpr) = :prov AND ST_Intersects(v.geometry, p.geometry))")
        sql = text(f"""SELECT {sel} FROM {SCHEMA}.{table} p
                       WHERE p.datetm >= :s AND p.datetm < :e
                         AND p.latitude BETWEEN :la0 AND :la1 AND p.longitude BETWEEN :lo0 AND :lo1
                         {boundary}""")
        params = {"s": start, "e": end, "la0": lat0, "la1": lat1, "lo0": lon0, "lo1": lon1}
        if use_boundary_table:
            params["prov"] = province.upper()
        df = pd.read_sql(sql, engine, params=params)
        df["source"] = src
        print(f"  [{src}] {len(df):,} titik")
        frames.append(df)
    if not frames:
        sys.exit("Tidak ada data dari DB.")
    return pd.concat(frames, ignore_index=True)


def load_from_csv(paths):
    frames = []
    for p in paths:
        df = pd.read_csv(p, dtype={"confidence": str, "acq_time": str})
        if "datetm" not in df.columns:
            df["datetm"] = pd.to_datetime(df["acq_date"].astype(str) + " " + df["acq_time"].astype(str).str.zfill(4),
                                          format="%Y-%m-%d %H%M", utc=True)
        df["source"] = Path(p).stem
        frames.append(df)
        print(f"  [{p}] {len(df):,} titik")
    return pd.concat(frames, ignore_index=True)


def clean(df, start, end, min_conf, boundary_path):
    df = df.copy()
    df["datetm"] = pd.to_datetime(df["datetm"], utc=True)
    df = df[(df["datetm"] >= start) & (df["datetm"] < end)]
    df = df.dropna(subset=["latitude", "longitude", "datetm"])
    if "frp" not in df.columns:
        df["frp"] = 1.0
    df["frp"] = pd.to_numeric(df["frp"], errors="coerce").fillna(0.0)
    df["confidence"] = df.get("confidence", "n").astype(str).str.lower().str[0]
    df["satellite"] = df.get("satellite", df["source"]).astype(str)
    if min_conf == "n":
        df = df[df["confidence"].isin(["n", "h"])]
    elif min_conf == "h":
        df = df[df["confidence"] == "h"]
    # buang duplikat antar sumber (lat, lon, waktu persis sama)
    df = df.drop_duplicates(subset=["latitude", "longitude", "datetm", "satellite"])
    if boundary_path:
        bp = Path(boundary_path)
        if not bp.exists():
            sys.exit(f"\n[ERROR] File boundary '{boundary_path}' tidak ditemukan.\n"
                     f"Jika ingin memproses murni BBox Kalbar tanpa file GeoJSON luar, gunakan flag --no-boundary.")
        import shapely
        from shapely.geometry import shape
        from shapely.ops import unary_union
        gj = json.loads(bp.read_text())
        feats = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
        poly = unary_union([shape(f["geometry"]) for f in feats])
        shapely.prepare(poly)
        inside = shapely.contains_xy(poly, df["longitude"].to_numpy(), df["latitude"].to_numpy())
        df = df[inside]
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# 2. Proyeksi, topeng statis, lintasan
# --------------------------------------------------------------------------
def to_ms(series):
    """ms sejak epoch UTC, tidak bergantung pada resolusi datetime pandas (ns/us/s)."""
    return ((series - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1)).to_numpy().astype(np.int64)


def project(df):
    tf = Transformer.from_crs("EPSG:4326", CRS_M, always_xy=True)
    x, y = tf.transform(df["longitude"].to_numpy(), df["latitude"].to_numpy())
    df["x"], df["y"] = x, y
    return df, tf


def static_mask(df, min_months, min_days):
    """Sel 375 m yang terdeteksi hampir sepanjang tahun = sumber panas statis (flare, pabrik, dll)."""
    cell = (np.floor(df["x"] / PIXEL_M).astype(np.int64).astype(str) + "_" +
            np.floor(df["y"] / PIXEL_M).astype(np.int64).astype(str))
    d = df.assign(cell=cell, ym=df["datetm"].dt.strftime("%Y-%m"), day=df["datetm"].dt.strftime("%Y-%m-%d"))
    g = d.groupby("cell").agg(months=("ym", "nunique"), days=("day", "nunique"), n=("frp", "size"),
                              x=("x", "mean"), y=("y", "mean"), lon=("longitude", "mean"), lat=("latitude", "mean"))
    static = g[(g["months"] >= min_months) & (g["days"] >= min_days)]
    keep = ~d["cell"].isin(static.index)
    return df[keep.to_numpy()].reset_index(drop=True), static.reset_index()


def assign_passes(df, gap_min=20):
    """Satu lintasan = satu satelit, titik berurutan dengan jeda <= gap_min menit."""
    df = df.sort_values(["satellite", "datetm"]).reset_index(drop=True)
    new = (df["satellite"] != df["satellite"].shift()) | (df["datetm"].diff() > pd.Timedelta(minutes=gap_min))
    df["pass_local"] = new.cumsum()
    order = df.groupby("pass_local")["datetm"].min().sort_values().index
    rank = {p: i for i, p in enumerate(order)}
    df["pass_id"] = df["pass_local"].map(rank).astype(np.int32)
    return df.sort_values("datetm").reset_index(drop=True)


# --------------------------------------------------------------------------
# 3. Klaster spatio-temporal
# --------------------------------------------------------------------------
def st_cluster(df, eps_s, eps_t_h):
    xy = df[["x", "y"]].to_numpy()
    t = to_ms(df["datetm"]) / 3.6e6                            # jam sejak epoch (UTC)
    tree = cKDTree(xy)
    pairs = tree.query_pairs(eps_s, output_type="ndarray")
    if len(pairs):
        ok = np.abs(t[pairs[:, 0]] - t[pairs[:, 1]]) <= eps_t_h
        pairs = pairs[ok]
    n = len(df)
    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    _, labels = connected_components(g, directed=False)
    return labels


# --------------------------------------------------------------------------
# 4. Akar, pohon induk-anak, kestabilan
# --------------------------------------------------------------------------
def pick_root(pas, frp):
    first = np.flatnonzero(pas == pas.min())
    return int(first[np.argmax(frp[first])])


def build_parents(xy, pas, root, chunk=400):
    n = len(pas)
    parent = np.full(n, -1, dtype=np.int32)
    first = pas == pas.min()
    parent[first] = root
    parent[root] = -1
    rest = np.flatnonzero(~first)
    for s in range(0, len(rest), chunk):
        idx = rest[s:s + chunk]
        d2 = ((xy[idx, None, :] - xy[None, :, :]) ** 2).sum(-1)
        d2[pas[None, :] >= pas[idx, None]] = np.inf          # hanya lintasan STRICT lebih awal
        parent[idx] = np.argmin(d2, axis=1)
    return parent


def stability(xy, pas, frp, root, parent, rng, runs, edge_n_max):
    n = len(pas)
    root_hit = 0
    edge_hit = edge_tot = 0
    do_edges = n <= edge_n_max and n > 1
    for _ in range(runs):
        f2 = frp * (1 + rng.uniform(-0.2, 0.2, n))
        r2 = pick_root(pas, f2)
        root_hit += r2 == root
        if do_edges:
            xy2 = xy + rng.uniform(-HALF_PIXEL, HALF_PIXEL, xy.shape)
            p2 = build_parents(xy2, pas, r2)
            mask = np.ones(n, bool)
            mask[[root, r2]] = False
            edge_hit += int((p2[mask] == parent[mask]).sum())
            edge_tot += int(mask.sum())
    return root_hit / runs, (edge_hit / edge_tot if edge_tot else (1.0 if do_edges else None))


def bearing_deg(dx, dy):
    return float((np.degrees(np.arctan2(dx, dy)) + 360) % 360)


# --------------------------------------------------------------------------
# 5. Area bertumbuh (alpha shape)
# --------------------------------------------------------------------------
def alpha_shape(xy, alpha, buf):
    """Gabungan segitiga Delaunay dengan jari-jari lingkaran luar <= alpha, ditambah buffer tiap titik."""
    from shapely.geometry import MultiPoint, Polygon
    from shapely.ops import unary_union
    pts_buf = [MultiPoint(xy).buffer(buf)] if len(xy) else []
    if len(xy) < 3:
        return unary_union(pts_buf) if pts_buf else None
    try:
        tri = Delaunay(xy)
    except Exception:
        return unary_union(pts_buf)
    A, B, C = (xy[tri.simplices[:, i]] for i in range(3))
    a = np.linalg.norm(B - C, axis=1); b = np.linalg.norm(A - C, axis=1); c = np.linalg.norm(A - B, axis=1)
    AB, AC = B - A, C - A
    area = np.abs(AB[:, 0] * AC[:, 1] - AB[:, 1] * AC[:, 0]) / 2
    with np.errstate(divide="ignore", invalid="ignore"):
        circum_r = a * b * c / (4 * area)
    keep = np.isfinite(circum_r) & (circum_r <= alpha)
    polys = [Polygon(xy[s]) for s in tri.simplices[keep]]
    return unary_union(polys + pts_buf)


# --------------------------------------------------------------------------
# 6. Utama
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Klaster ST + titik awal + pohon panah hotspot VIIRS Kalbar (GEE Edition)")
    ap.add_argument("--months", type=int, default=5, help="Jumlah bulan ke belakang jika --start tidak diisi (default: 5)")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (UTC). Default: 5 bulan sebelum --end")
    ap.add_argument("--end", default=str(dt.date.today()), help="YYYY-MM-DD (UTC, eksklusif)")
    ap.add_argument("--csv", nargs="*", help="CSV FIRMS VIIRS (satu atau lebih) sebagai ganti DB")
    ap.add_argument("--boundary", help="GeoJSON batas Kalbar opsional (EPSG:4326)")
    ap.add_argument("--province", default="KALIMANTAN BARAT")
    ap.add_argument("--no-boundary", action="store_true", default=True,
                    help="Murni BBox Kalbar dari tabel _raw tanpa intersect tabel/file lain (AKTIF secara default)")
    ap.add_argument("--use-boundary-table", action="store_true",
                    help="Opsional: filter ST_Intersects ke tabel base_adminvillage_alert")
    ap.add_argument("--min-conf", default="n", choices=["l", "n", "h"], help="Confidence minimum VIIRS (default n: buang low)")
    ap.add_argument("--eps-s", type=float, default=1500, help="Jarak klaster (m), default 1500")
    ap.add_argument("--eps-t-h", type=float, default=48, help="Selisih waktu klaster (jam), default 48")
    ap.add_argument("--min-points", type=int, default=5, help="Klaster lebih kecil dari ini hanya jadi titik konteks")
    ap.add_argument("--static-months", type=int, default=4, help="Min bulan aktif untuk mask sumber panas statis (default 4)")
    ap.add_argument("--static-days", type=int, default=15, help="Min hari aktif untuk mask sumber panas statis (default 15)")
    ap.add_argument("--runs", type=int, default=200, help="Jumlah ulangan uji kestabilan")
    ap.add_argument("--edge-n-max", type=int, default=1500, help="Stabilitas panah dihitung hanya untuk klaster <= n titik")
    ap.add_argument("--alpha", type=float, default=1300, help="Alpha (m) untuk area, default 1300")
    ap.add_argument("--geojson", action="store_true", help="Tulis juga points/edges/roots/areas_daily .geojson")
    ap.add_argument("--out", default="out")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    end = pd.Timestamp(args.end, tz="UTC")
    start = pd.Timestamp(args.start, tz="UTC") if args.start else end - pd.Timedelta(days=args.months * 30)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)

    print(f"Periode (UTC): {start.date()} -> {end.date()} | Mode: Pure Raw BBox Kalbar {BBOX_KALBAR}")
    if args.csv:
        raw = load_from_csv(args.csv)
    else:
        raw = load_from_db(start.to_pydatetime(), end.to_pydatetime(), args.province,
                           use_boundary_table=bool(args.use_boundary_table))
    df = clean(raw, start, end, args.min_conf, args.boundary)
    print(f"Setelah filter (confidence >= {args.min_conf}, provinsi): {len(df):,} titik")
    if df.empty:
        sys.exit("Tidak ada titik tersisa.")

    df, tf_fwd = project(df)
    df, static = static_mask(df, args.static_months, args.static_days)
    static.to_csv(out / "static_sources.csv", index=False)
    print(f"Sumber panas statis dibuang: {len(static)} sel -> {len(df):,} titik tersisa")

    df = assign_passes(df)
    print(f"Lintasan satelit: {df['pass_id'].nunique():,}")

    labels = st_cluster(df, args.eps_s, args.eps_t_h)
    df["cl"] = labels
    sizes = df.groupby("cl").size()
    big = sizes[sizes >= args.min_points].index
    ctx = df[~df["cl"].isin(big)]
    print(f"Klaster >= {args.min_points} titik: {len(big):,} (terbesar {sizes.max():,} titik); "
          f"titik konteks: {len(ctx):,}")
    if sizes.max() > 3000:
        print("  PERINGATAN: ada klaster sangat besar (rantai antar area terbakar). "
              "Pertimbangkan --eps-s / --eps-t-h lebih kecil.")

    tf_inv = Transformer.from_crs(CRS_M, "EPSG:4326", always_xy=True)
    x0, y0 = float(df["x"].mean()), float(df["y"].mean())
    sats = sorted(df["satellite"].unique())
    sat_idx = {s: i for i, s in enumerate(sats)}
    conf_idx = {"l": 0, "n": 1, "h": 2}

    clusters, summary = [], []
    geo_pts, geo_edges, geo_roots, geo_areas = [], [], [], []

    order = sorted(big, key=lambda c: df.loc[df["cl"] == c, "datetm"].min())
    for k, c in enumerate(order):
        g = df[df["cl"] == c].sort_values(["pass_id", "datetm"]).reset_index(drop=True)
        xy = g[["x", "y"]].to_numpy(); pas = g["pass_id"].to_numpy(); frp = g["frp"].to_numpy()
        tms = to_ms(g["datetm"])
        root = pick_root(pas, frp)
        parent = build_parents(xy, pas, root)
        st_root, st_edge = stability(xy, pas, frp, root, parent, rng, args.runs, args.edge_n_max)
        first_count = int((pas == pas.min()).sum())

        last = pas == pas.max()
        cxy = xy[last].mean(axis=0); dvec = cxy - xy[root]
        dist = float(np.hypot(*dvec)); hrs = (tms[last].mean() - tms[root]) / 3.6e6
        brg = bearing_deg(*dvec) if dist >= PIXEL_M else None
        shape = alpha_shape(xy, args.alpha, 190.0)
        area_ha = float(shape.area / 1e4) if shape is not None else 0.0
        rlon, rlat = tf_inv.transform(*xy[root])

        pts = []
        for i in range(len(g)):
            pts.append([int(round(xy[i, 0] - x0)), int(round(xy[i, 1] - y0)), int(tms[i]), int(pas[i]),
                        sat_idx[g.at[i, "satellite"]], round(float(frp[i]), 1), conf_idx.get(g.at[i, "confidence"], 1),
                        int(parent[i])])
        cid = f"K{k + 1:04d}"
        cl = {"id": cid, "n": len(g), "t0": int(tms.min()), "t1": int(tms.max()), "root": root,
              "firstCount": first_count, "stabRoot": round(st_root, 3),
              "stabEdge": None if st_edge is None else round(st_edge, 3),
              "dirDeg": None if brg is None else round(brg, 1), "distM": round(dist), "areaHa": round(area_ha, 1),
              "rootLL": [round(rlat, 5), round(rlon, 5)], "pts": pts}
        clusters.append(cl)
        summary.append({"id": cid, "t_awal_wib": pd.Timestamp(tms[root], unit="ms", tz="UTC").tz_convert(WIB).strftime("%Y-%m-%d %H:%M"),
                        "n_titik": len(g), "n_lintasan": int(g["pass_id"].nunique()),
                        "durasi_jam": round((tms.max() - tms.min()) / 3.6e6, 1), "luas_ha": round(area_ha, 1),
                        "arah_deg": cl["dirDeg"], "jarak_m": cl["distM"],
                        "laju_m_per_jam": round(dist / hrs) if hrs >= 1 and brg is not None else None,
                        "titik_awal_serentak": first_count, "stabilitas_awal": cl["stabRoot"],
                        "stabilitas_panah": cl["stabEdge"], "lat_awal": cl["rootLL"][0], "lon_awal": cl["rootLL"][1]})

        if args.geojson:
            lon, lat = tf_inv.transform(xy[:, 0], xy[:, 1])
            for i in range(len(g)):
                geo_pts.append({"type": "Feature", "properties": {
                    "cluster": cid, "t_wib": pd.Timestamp(tms[i], unit="ms", tz="UTC").tz_convert(WIB).strftime("%Y-%m-%d %H:%M"),
                    "pass_id": int(pas[i]), "sat": g.at[i, "satellite"], "frp": float(frp[i]),
                    "conf": g.at[i, "confidence"], "is_root": int(i == root), "t_ms": int(tms[i])},
                    "geometry": {"type": "Point", "coordinates": [float(lon[i]), float(lat[i])]}})
                if parent[i] >= 0:
                    p = parent[i]
                    geo_edges.append({"type": "Feature", "properties": {
                        "cluster": cid, "dist_m": round(float(np.hypot(*(xy[i] - xy[p])))),
                        "dt_h": round(float((tms[i] - tms[p]) / 3.6e6), 2), "zona_awal": int(pas[i] == pas.min()),
                        "pass_id": int(pas[i]), "t_ms": int(tms[i])},
                        "geometry": {"type": "LineString", "coordinates": [[float(lon[p]), float(lat[p])], [float(lon[i]), float(lat[i])]]}})
            geo_roots.append({"type": "Feature", "properties": {"cluster": cid, "stab_awal": cl["stabRoot"], "stab_edge": cl["stabEdge"], "t_ms": int(tms[root]),
                                                                  "t_end_ms": int(tms.max()), "n_titik": len(g), "luas_ha": cl["areaHa"], "arah_deg": cl["dirDeg"],
                                                                  "jarak_m": cl["distM"], "titik_awal_serentak": first_count},
                              "geometry": {"type": "Point", "coordinates": [rlon, rlat]}})
            # area kumulatif di akhir tiap hari WIB
            day = pd.to_datetime(tms, unit="ms", utc=True).tz_convert(WIB).strftime("%Y-%m-%d")
            for d in sorted(set(day)):
                sel = np.flatnonzero(np.array(day) <= d)
                shp = alpha_shape(xy[sel], args.alpha, 190.0)
                if shp is None or shp.is_empty:
                    continue
                import shapely
                shp_ll = shapely.transform(shp, lambda c: np.column_stack(tf_inv.transform(c[:, 0], c[:, 1])))
                geo_areas.append({"type": "Feature", "properties": {"cluster": cid, "hingga_wib": d, "luas_ha": round(shp.area / 1e4, 1),
                                                                 "t_end_ms": int(tms[sel].max())},
                                  "geometry": json.loads(json.dumps(shp_ll.__geo_interface__))})
        if (k + 1) % 25 == 0 or k + 1 == len(order):
            print(f"  klaster {k + 1}/{len(order)}")

    ctx_pts = [[int(round(a - x0)), int(round(b - y0))] for a, b in zip(ctx["x"], ctx["y"])]
    lon0, lat0 = tf_inv.transform(x0, y0)
    payload = {"meta": {"generated": dt.datetime.now(dt.timezone.utc).isoformat(), "crs": CRS_M, "x0": x0, "y0": y0,
                        "lat0": lat0, "lon0": lon0, "start": str(start.date()), "end": str(end.date()), "sats": sats,
                        "params": {k: v for k, v in vars(args).items() if k not in ("csv",)},
                        "pixel_m": PIXEL_M, "n_points": int(len(df)), "n_static_cells": int(len(static)),
                        "note": "x,y dalam meter relatif terhadap (x0,y0) UTM 49N; t dalam ms UTC; pts=[x,y,t,pass,sat,frp,conf,parent]"},
               "clusters": clusters, "context": ctx_pts}
    (out / "forensic_kalbar.json").write_text(json.dumps(payload, separators=(",", ":")))
    pd.DataFrame(summary).to_csv(out / "clusters_summary.csv", index=False)
    if args.geojson:
        for name, feats in [("points", geo_pts), ("edges", geo_edges), ("roots", geo_roots), ("areas_daily", geo_areas)]:
            (out / f"{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
            try:
                import geopandas as gpd, zipfile, shutil
                tmp_dir = out / f"_tmp_{name}"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
                rename_map = {"titik_awal_serentak": "ttkAwalSrt"}
                gdf = gdf.rename(columns={k: v for k, v in rename_map.items() if k in gdf.columns})
                gdf.to_file(tmp_dir / f"{name}.shp", driver="ESRI Shapefile", encoding="utf-8")
                with zipfile.ZipFile(out / f"{name}.zip", "w", zipfile.ZIP_DEFLATED) as z:
                    for f in tmp_dir.iterdir():
                        z.write(f, arcname=f.name)
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
    size_mb = (out / "forensic_kalbar.json").stat().st_size / 1e6
    print(f"\nSelesai. {len(clusters)} klaster -> {out}/forensic_kalbar.json ({size_mb:.1f} MB), clusters_summary.csv, static_sources.csv")


if __name__ == "__main__":
    main()