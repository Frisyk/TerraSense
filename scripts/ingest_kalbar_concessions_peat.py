#!/usr/bin/env python3
"""
scripts/ingest_kalbar_concessions_peat.py
==========================================
Script utilitas untuk mengimpor dan memverifikasi batas spasial:
 1. Batas Konsesi (Perkebunan Sawit / Kehutanan PBPH/HTI / Tambang) -> monitoring.unit_concession_sp
 2. Batas Lahan Gambut & KHG (BBSDLP / BRGM / KLHK)               -> monitoring.unit_concession_sp (atau layer gambut)
 3. Batas Administrasi Desa/Kecamatan (BIG Ina-Geoportal)        -> monitoring.base_adminvillage_alert
 4. Tutupan Lahan (KLHK / ESA WorldCover)                         -> monitoring.base_landcover_change

Penggunaan:
    python scripts/ingest_kalbar_concessions_peat.py --help
    python scripts/ingest_kalbar_concessions_peat.py --type concession --file path/to/konsesi_kalbar.geojson
    python scripts/ingest_kalbar_concessions_peat.py --type village --file path/to/desa_kalbar.geojson
    python scripts/ingest_kalbar_concessions_peat.py --status
"""

import os
import sys
import json
import argparse
import psycopg2
from psycopg2.extras import execute_batch

PROD_URL = "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
LOCAL_URL = "postgresql://postgres:postgres@db:5432/postgres"

def check_status(conn):
    cur = conn.cursor()
    print("=== STATUS TABEL SPASIAL REFERENSI (KALIMANTAN BARAT) ===")
    
    tables = [
        ("monitoring.unit_concession_sp", "Batas Konsesi (Sawit / HTI / Tambang / Gambut)"),
        ("monitoring.base_adminvillage_alert", "Batas Administrasi Desa (BIG)"),
        ("monitoring.base_landcover_change", "Tutupan Lahan & Perubahan Lahan (KLHK)"),
        ("monitoring.climate_history", "Riwayat Iklim & Angin ERA5"),
        ("monitoring.fire_points", "Hotspot VIIRS Rollup"),
    ]
    
    for tbl, desc in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {tbl};")
            cnt = cur.fetchone()[0]
            print(f" • {tbl:35} : {cnt:>8,} baris  ({desc})")
        except Exception as e:
            print(f" • {tbl:35} : [ERROR: {e}]")
            conn.rollback()
    
    cur.close()

def ingest_concessions(conn, geojson_path):
    print(f"Memuat GeoJSON konsesi dari: {geojson_path}")
    with open(geojson_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    features = data.get('features', [])
    print(f"Ditemukan {len(features)} fitur spasial...")

    cur = conn.cursor()
    insert_sql = """
        INSERT INTO monitoring.unit_concession_sp (uid, namobj, geometry)
        VALUES (%s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))
        ON CONFLICT (uid) DO UPDATE
        SET namobj = EXCLUDED.namobj,
            geometry = EXCLUDED.geometry;
    """

    batch = []
    for idx, feat in enumerate(features):
        props = feat.get('properties', {}) or {}
        geom = feat.get('geometry')
        if not geom:
            continue

        # Ekstrak nama perusahaan / jenis konsesi
        uid = str(props.get('uid') or props.get('id') or props.get('OBJECTID') or f"kalbar_conc_{idx+1}")
        namobj = (
            props.get('namobj') or 
            props.get('nama_pt') or 
            props.get('perusahaan') or 
            props.get('company') or 
            props.get('nama') or 
            'Konsesi Tanpa Nama'
        )

        batch.append((uid, namobj, json.dumps(geom)))

        if len(batch) >= 200:
            execute_batch(cur, insert_sql, batch)
            conn.commit()
            print(f"  Tersimpan {idx+1}/{len(features)} konsesi...")
            batch = []

    if batch:
        execute_batch(cur, insert_sql, batch)
        conn.commit()

    print(f"✓ Berhasil mengimpor {len(features)} konsesi ke monitoring.unit_concession_sp!")
    cur.close()

def ingest_villages(conn, geojson_path):
    print(f"Memuat GeoJSON batas desa dari: {geojson_path}")
    with open(geojson_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    features = data.get('features', [])
    print(f"Ditemukan {len(features)} wilayah administrasi desa...")

    cur = conn.cursor()
    insert_sql = """
        INSERT INTO monitoring.base_adminvillage_alert (wadmkd, wadmkc, wadmkk, wadmpr, geometry)
        VALUES (%s, %s, %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)));
    """

    batch = []
    for idx, feat in enumerate(features):
        props = feat.get('properties', {}) or {}
        geom = feat.get('geometry')
        if not geom:
            continue

        wadmkd = str(props.get('wadmkd') or props.get('desa') or props.get('kelurahan') or 'Desa')
        wadmkc = str(props.get('wadmkc') or props.get('kecamatan') or 'Kecamatan')
        wadmkk = str(props.get('wadmkk') or props.get('kabupaten') or 'Kabupaten')
        wadmpr = str(props.get('wadmpr') or props.get('provinsi') or 'Kalimantan Barat')

        batch.append((wadmkd, wadmkc, wadmkk, wadmpr, json.dumps(geom)))

        if len(batch) >= 200:
            execute_batch(cur, insert_sql, batch)
            conn.commit()
            print(f"  Tersimpan {idx+1}/{len(features)} desa...")
            batch = []

    if batch:
        execute_batch(cur, insert_sql, batch)
        conn.commit()

    print(f"✓ Berhasil mengimpor {len(features)} desa ke monitoring.base_adminvillage_alert!")
    cur.close()

def main():
    parser = argparse.ArgumentParser(description="Ingest & Verifikasi Lapisan Spasial Referensi Kalbar")
    parser.add_argument("--status", action="store_true", help="Cek status jumlah baris tabel spasial")
    parser.add_argument("--type", choices=["concession", "village", "peat"], help="Tipe data spasial yang diimpor")
    parser.add_argument("--file", type=str, help="Path ke berkas GeoJSON")
    args = parser.parse_args()

    conn = psycopg2.connect(PROD_URL)
    try:
        if args.status or not args.type:
            check_status(conn)
        elif args.type in ["concession", "peat"] and args.file:
            ingest_concessions(conn, args.file)
        elif args.type == "village" and args.file:
            ingest_villages(conn, args.file)
        else:
            parser.print_help()
    finally:
        conn.close()

if __name__ == "__main__":
    main()
