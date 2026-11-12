import time
import psycopg2

PROD_URL = "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"

conn = psycopg2.connect(PROD_URL)
cur = conn.cursor()

print("=== Benchmark Waktu Kueri Supabase ===")

# Test 1: Query 7 hari terakhir (2026-10-24 s/d 2026-10-31)
t0 = time.time()
cur.execute("""
    SELECT COUNT(*) FROM monitoring.alert_fire_viirssnpp_raw
    WHERE datetm >= '2026-10-24'::timestamptz AND datetm < '2026-11-01'::timestamptz
      AND latitude BETWEEN -3.2 AND 2.3 AND longitude BETWEEN 108.5 AND 114.3;
""")
cnt_7d = cur.fetchone()[0]
dur_7d = time.time() - t0
print(f"1. Query 7 Hari (24-31 Okt): {cnt_7d:,} titik dalam {dur_7d:.3f} detik")

# Test 2: Query 14 hari (2026-10-17 s/d 2026-10-31)
t0 = time.time()
cur.execute("""
    SELECT COUNT(*) FROM monitoring.alert_fire_viirssnpp_raw
    WHERE datetm >= '2026-10-17'::timestamptz AND datetm < '2026-11-01'::timestamptz
      AND latitude BETWEEN -3.2 AND 2.3 AND longitude BETWEEN 108.5 AND 114.3;
""")
cnt_14d = cur.fetchone()[0]
dur_14d = time.time() - t0
print(f"2. Query 14 Hari (17-31 Okt): {cnt_14d:,} titik dalam {dur_14d:.3f} detik")

# Test 3: Query 30 hari (2026-10-01 s/d 2026-11-01)
t0 = time.time()
cur.execute("""
    SELECT COUNT(*) FROM monitoring.alert_fire_viirssnpp_raw
    WHERE datetm >= '2026-10-01'::timestamptz AND datetm < '2026-11-01'::timestamptz
      AND latitude BETWEEN -3.2 AND 2.3 AND longitude BETWEEN 108.5 AND 114.3;
""")
cnt_30d = cur.fetchone()[0]
dur_30d = time.time() - t0
print(f"3. Query 30 Hari (Okt 2026): {cnt_30d:,} titik dalam {dur_30d:.3f} detik")

# Test 4: Query 150 hari (Juni - Okt 2026) seperti default saat ini
t0 = time.time()
cur.execute("""
    SELECT COUNT(*) FROM monitoring.alert_fire_viirssnpp_raw
    WHERE datetm >= '2026-06-01'::timestamptz AND datetm < '2026-11-01'::timestamptz
      AND latitude BETWEEN -3.2 AND 2.3 AND longitude BETWEEN 108.5 AND 114.3;
""")
cnt_150d = cur.fetchone()[0]
dur_150d = time.time() - t0
print(f"4. Query 150 Hari (Current Default): {cnt_150d:,} titik dalam {dur_150d:.3f} detik")

conn.close()
