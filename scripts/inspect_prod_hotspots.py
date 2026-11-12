import psycopg2

PROD_URL = "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"

conn = psycopg2.connect(PROD_URL)
cur = conn.cursor()

cur.execute("""
    SELECT MIN(datetm), MAX(datetm), COUNT(*) 
    FROM monitoring.alert_fire_viirssnpp_raw;
""")
print("VIIRS SNPP:", cur.fetchone())

# Cek 5 klaster/konsentrasi hotspot terbesar di Kalbar
cur.execute("""
    SELECT 
        ROUND((latitude / 0.25)::numeric, 0) * 0.25 AS c_lat,
        ROUND((longitude / 0.25)::numeric, 0) * 0.25 AS c_lon,
        MIN(datetm::date), MAX(datetm::date),
        COUNT(*) AS pts
    FROM monitoring.alert_fire_viirssnpp_raw
    GROUP BY c_lat, c_lon
    ORDER BY pts DESC
    LIMIT 10;
""")
print("\nTop 10 Hotspot Cells in Kalbar:")
for row in cur.fetchall():
    print(row)

conn.close()

