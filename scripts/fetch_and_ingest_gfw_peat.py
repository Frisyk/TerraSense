#!/usr/bin/env python3
"""
scripts/fetch_and_ingest_gfw_peat.py
====================================
Menarik data Lahan Gambut Kalimantan Barat dari ArcGIS MapServer WRI/GFW
(Sumber: BBSDLP Kementan / Wetlands International / GFW)
dan memasukkannya langsung ke database Supabase (monitoring.unit_concession_sp).
"""

import sys
import json
import urllib.request
import psycopg2
from psycopg2.extras import execute_batch

PROD_URL = "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"

BBOX_KALBAR = "108.5,-3.2,114.3,2.3"
BASE_URL = "https://gis-gfw.wri.org/arcgis/rest/services/country_data/asia/MapServer/10/query"

def esri_rings_to_wkt(rings):
    """Konversi Esri rings koordinat ke WKT POLYGON atau MULTIPOLYGON"""
    if not rings:
        return None
    # Bersihkan rings: pastikan setiap ring tertutup dan memiliki minimal 4 titik
    valid_rings = []
    for ring in rings:
        if len(ring) < 3:
            continue
        # Pastikan tertutup
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if len(ring) >= 4:
            valid_rings.append(ring)
            
    if not valid_rings:
        return None

    # Format rings menjadi WKT POLYGON
    ring_strs = []
    for ring in valid_rings:
        coords_str = ", ".join(f"{pt[0]} {pt[1]}" for pt in ring)
        ring_strs.append(f"({coords_str})")
    
    return f"POLYGON({', '.join(ring_strs)})"

def main():
    print("=== PENGAMBILAN DATA LAHAN GAMBUT KALIMANTAN BARAT (WRI/GFW) ===")
    conn = psycopg2.connect(PROD_URL)
    cur = conn.cursor()

    # Pastikan kolom sector dan area_ha ada jika belum ada
    cur.execute("""
        ALTER TABLE monitoring.unit_concession_sp 
        ADD COLUMN IF NOT EXISTS sector VARCHAR(100),
        ADD COLUMN IF NOT EXISTS area_ha DOUBLE PRECISION;
    """)
    conn.commit()

    # 1. Cek total poligon di BBOX Kalbar
    count_url = (
        f"{BASE_URL}?where=1%3D1&geometry={BBOX_KALBAR}&geometryType=esriGeometryEnvelope"
        f"&inSR=4326&spatialRel=esriSpatialRelIntersects&returnCountOnly=true&f=json"
    )
    req = urllib.request.Request(count_url, headers={'User-Agent': 'Mozilla/5.0'})
    res = json.loads(urllib.request.urlopen(req, timeout=20).read())
    total_count = res.get('count', 0)
    print(f"Total poligon lahan gambut Kalbar ditemukan: {total_count}")

    if total_count == 0:
        print("Tidak ada poligon ditemukan.")
        return

    # 2. Ambil seluruh objectid di BBOX
    id_url = (
        f"{BASE_URL}?where=1%3D1&geometry={BBOX_KALBAR}&geometryType=esriGeometryEnvelope"
        f"&inSR=4326&spatialRel=esriSpatialRelIntersects&returnIdsOnly=true&f=json"
    )
    req = urllib.request.Request(id_url, headers={'User-Agent': 'Mozilla/5.0'})
    id_res = json.loads(urllib.request.urlopen(req, timeout=20).read())
    object_ids = id_res.get('objectIds', [])
    print(f"Mengambil geometri untuk {len(object_ids)} poligon gambut...")

    # 3. Ambil data per batch (30 IDs per request agar cepat dan tidak timeout)
    batch_size = 25
    inserted = 0

    insert_sql = """
        INSERT INTO monitoring.unit_concession_sp (uid, namobj, sector, area_ha, geometry)
        VALUES (%s, %s, %s, %s, ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromText(%s), 4326))))
        ON CONFLICT (uid) DO UPDATE
        SET namobj = EXCLUDED.namobj,
            sector = EXCLUDED.sector,
            area_ha = EXCLUDED.area_ha,
            geometry = EXCLUDED.geometry;
    """

    for i in range(0, len(object_ids), batch_size):
        chunk_ids = object_ids[i:i+batch_size]
        ids_str = ",".join(map(str, chunk_ids))
        q_url = (
            f"{BASE_URL}?objectIds={ids_str}&outFields=*&returnGeometry=true&f=json"
        )
        req = urllib.request.Request(q_url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            chunk_data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:
            print(f"Gagal mengambil batch {i}-{i+batch_size}: {e}")
            continue

        rows = []
        for feat in chunk_data.get('features', []):
            attrs = feat.get('attributes', {})
            geom = feat.get('geometry', {})
            rings = geom.get('rings', [])
            wkt = esri_rings_to_wkt(rings)
            if not wkt:
                continue

            oid = attrs.get('objectid_1') or attrs.get('objectid')
            uid = f"peat_kalbar_{oid}"
            revi = attrs.get('layer_revi') or 'Lahan Gambut'
            namobj = f"{revi.title()} Kalimantan Barat"
            area_sqm = attrs.get('shape_Area') or 0.0
            area_ha = round(area_sqm / 10000.0, 2)

            rows.append((uid, namobj, 'gambut', area_ha, wkt))

        if rows:
            try:
                execute_batch(cur, insert_sql, rows)
                conn.commit()
                inserted += len(rows)
                print(f"  ✓ Tersimpan {inserted}/{len(object_ids)} poligon gambut ke Supabase...")
            except Exception as e:
                conn.rollback()
                print(f"  Error inserting batch: {e}")

    # Verifikasi akhir
    cur.execute("SELECT COUNT(*) FROM monitoring.unit_concession_sp WHERE sector = 'gambut';")
    final_count = cur.fetchone()[0]
    print(f"\nSelesai! Total poligon gambut di tabel monitoring.unit_concession_sp: {final_count}")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
