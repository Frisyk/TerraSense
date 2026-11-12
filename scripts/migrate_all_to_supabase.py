#!/usr/bin/env python3
"""
Migrate all hotspot and forensic data from Local PostgreSQL (geonode_data) to Supabase.
Tables migrated:
  - monitoring.alert_fire_viirssnpp_raw
  - monitoring.alert_fire_viirsnoaa20_raw
  - monitoring.alert_fire_viirsnoaa21_raw
  - monitoring.alert_fire_sipongi_raw
  - monitoring.alert_fire_sipongi
  - system.setting

Also triggers rollup synchronization on Supabase:
  - monitoring.sync_fire_points()
  - monitoring.refresh_fire_hourly_grid()
"""

import sys
import os
import time
import tempfile
import argparse
import psycopg2

LOCAL_DEFAULT = "postgresql://postgres:postgres@fire_postgis_db:5432/geonode_data"
SUPABASE_DEFAULT = (
    "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph"
    "@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
)

TABLES = [
    ("monitoring", "alert_fire_viirssnpp_raw"),
    ("monitoring", "alert_fire_viirsnoaa20_raw"),
    ("monitoring", "alert_fire_viirsnoaa21_raw"),
    ("monitoring", "alert_fire_sipongi_raw"),
    ("monitoring", "alert_fire_sipongi"),
    ("system", "setting"),
]

def migrate_table(cur_l, cur_s, conn_s, schema, table, truncate=True):
    full_name = f'"{schema}"."{table}"'
    print(f"\n==================================================")
    print(f"[*] Memproses tabel: {schema}.{table}")
    print(f"==================================================")

    # 1. Hitung baris lokal
    cur_l.execute(f"SELECT COUNT(*) FROM {full_name}")
    local_count = cur_l.fetchone()[0]
    print(f"    Jumlah baris di DB Lokal: {local_count:,}")

    if local_count == 0:
        print("    [!] Tabel kosong di lokal, dilewati.")
        return local_count, 0

    # 2. Truncate tabel di Supabase jika diminta
    if truncate:
        print(f"    [~] Mengosongkan (TRUNCATE) {schema}.{table} di Supabase...")
        cur_s.execute(f"TRUNCATE TABLE {full_name} RESTART IDENTITY CASCADE;")
        conn_s.commit()

    # 3. Stream data menggunakan COPY ke temporary file
    t0 = time.time()
    with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as tmp_file:
        tmp_path = tmp_file.name

    try:
        print("    [1/2] Mengekspor dari database lokal via COPY TO STDOUT...")
        with open(tmp_path, "wb") as f_out:
            cur_l.copy_expert(f"COPY {full_name} TO STDOUT WITH (FORMAT CSV, HEADER)", f_out)
        size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        t_export = time.time() - t0
        print(f"    ✓ Ekspor selesai: {size_mb:.2f} MB dalam {t_export:.2f} detik.")

        print("    [2/2] Mengunggah ke Supabase via COPY FROM STDIN...")
        t1 = time.time()
        with open(tmp_path, "rb") as f_in:
            cur_s.copy_expert(f"COPY {full_name} FROM STDIN WITH (FORMAT CSV, HEADER)", f_in)
        conn_s.commit()
        t_import = time.time() - t1
        print(f"    ✓ Impor ke Supabase selesai dalam {t_import:.2f} detik.")

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # 4. Verifikasi jumlah baris di Supabase
    cur_s.execute(f"SELECT COUNT(*) FROM {full_name}")
    supa_count = cur_s.fetchone()[0]
    print(f"    ✓ Verifikasi Supabase: {supa_count:,} baris (Lokal: {local_count:,})")
    if supa_count == local_count:
        print(f"    [OK] Sinkronisasi 100% cocok untuk {schema}.{table}!")
    else:
        print(f"    [PERINGATAN] Selisih baris: lokal {local_count} vs supabase {supa_count}")

    return local_count, supa_count

def run_sync_rollups(conn_s):
    print("\n==================================================")
    print("[*] Menjalankan Sinkronisasi Rollup & View di Supabase")
    print("==================================================")
    cur = conn_s.cursor()

    # 1. Cek view alert_fire_firms
    cur.execute("SELECT COUNT(*) FROM monitoring.alert_fire_firms")
    cnt_firms = cur.fetchone()[0]
    print(f"    ✓ monitoring.alert_fire_firms (Unified View): {cnt_firms:,} baris total")

    # 2. Sinkronkan monitoring.fire_points
    print("    [~] Menjalankan monitoring.sync_fire_points('2020-01-01')...")
    t0 = time.time()
    cur.execute("SELECT monitoring.sync_fire_points('2020-01-01'::timestamptz);")
    conn_s.commit()
    cur.execute("SELECT COUNT(*) FROM monitoring.fire_points")
    cnt_pts = cur.fetchone()[0]
    print(f"    ✓ monitoring.fire_points terisi: {cnt_pts:,} baris ({time.time()-t0:.2f} detik)")

    # 3. Sinkronkan monitoring.fire_hourly_grid
    print("    [~] Menjalankan monitoring.refresh_fire_hourly_grid()...")
    t1 = time.time()
    cur.execute("SELECT monitoring.refresh_fire_hourly_grid(NULL, NULL);")
    conn_s.commit()
    cur.execute("SELECT COUNT(*) FROM monitoring.fire_hourly_grid")
    cnt_grid = cur.fetchone()[0]
    print(f"    ✓ monitoring.fire_hourly_grid terisi: {cnt_grid:,} agregat ({time.time()-t1:.2f} detik)")

def main():
    parser = argparse.ArgumentParser(description="Migrate data from local DB to Supabase.")
    parser.add_argument("--local", default=LOCAL_DEFAULT, help="Local DB connection URL")
    parser.add_argument("--supabase", default=SUPABASE_DEFAULT, help="Supabase connection URL")
    parser.add_argument("--no-truncate", action="store_true", help="Do not truncate Supabase tables before copy")
    parser.add_argument("--skip-rollup", action="store_true", help="Skip running sync_fire_points and rollup")
    args = parser.parse_args()

    print("==================================================")
    print("  MIGRASI DATA HOTSPOT: LOCAL POSTGRES -> SUPABASE")
    print("==================================================")
    print(f"Sumber (Lokal): {args.local.split('@')[-1]}")
    print(f"Tujuan (Supabase): {args.supabase.split('@')[-1]}")

    t_start = time.time()
    try:
        conn_l = psycopg2.connect(args.local)
        conn_s = psycopg2.connect(args.supabase)
    except Exception as e:
        print(f"[ERROR] Gagal menyambung database: {e}")
        sys.exit(1)

    cur_l = conn_l.cursor()
    cur_s = conn_s.cursor()

    total_local = 0
    total_supa = 0

    for schema, table in TABLES:
        try:
            nl, ns = migrate_table(cur_l, cur_s, conn_s, schema, table, truncate=not args.no_truncate)
            total_local += nl
            total_supa += ns
        except Exception as e:
            print(f"    [ERROR] Gagal migrasi {schema}.{table}: {e}")
            conn_s.rollback()

    if not args.skip_rollup:
        try:
            run_sync_rollups(conn_s)
        except Exception as e:
            print(f"    [ERROR] Gagal menjalankan rollup: {e}")
            conn_s.rollback()

    conn_l.close()
    conn_s.close()

    total_time = time.time() - t_start
    print("\n==================================================")
    print("  MIGRASI KE SUPABASE SELESAI")
    print("==================================================")
    print(f"Total baris ditransfer : {total_supa:,} / {total_local:,}")
    print(f"Total waktu pemrosesan : {total_time:.2f} detik")

if __name__ == "__main__":
    main()

