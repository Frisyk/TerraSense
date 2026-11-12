import os
import psycopg2

LOCAL_URL = os.environ.get("LOCAL_DB_URL", "postgresql://postgres:postgres@db:5432/geonode_data")
PROD_URL = os.environ.get("PROD_DB_URL", "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres")

print("=== 1. Cek Local DB ===")
try:
    conn_loc = psycopg2.connect(LOCAL_URL)
    cur = conn_loc.cursor()
    cur.execute("SELECT to_regclass('monitoring.climate_history');")
    reg = cur.fetchone()[0]
    print("Local regclass:", reg)
    if reg:
        cur.execute("SELECT COUNT(*) FROM monitoring.climate_history;")
        print("Local rows:", cur.fetchone()[0])
        cur.execute("SELECT MIN(time_utc), MAX(time_utc) FROM monitoring.climate_history;")
        print("Local date range:", cur.fetchone())
    conn_loc.close()
except Exception as e:
    print("Local error:", e)

print("\n=== 2. Cek Supabase Prod DB ===")
try:
    conn_prod = psycopg2.connect(PROD_URL)
    cur = conn_prod.cursor()
    cur.execute("SELECT to_regclass('monitoring.climate_history');")
    reg = cur.fetchone()[0]
    print("Prod regclass:", reg)
    if reg:
        cur.execute("SELECT COUNT(*) FROM monitoring.climate_history;")
        print("Prod rows:", cur.fetchone()[0])
        cur.execute("SELECT MIN(time_utc), MAX(time_utc) FROM monitoring.climate_history;")
        print("Prod date range:", cur.fetchone())
    conn_prod.close()
except Exception as e:
    print("Prod error:", e)
