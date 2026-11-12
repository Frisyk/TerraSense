import psycopg2

PROD_URL = "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
conn = psycopg2.connect(PROD_URL)
cur = conn.cursor()

print("=== Cek unit_concession_sp di Supabase ===")
cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'monitoring' AND table_name = 'unit_concession_sp';")
print("Kolom:")
for col in cur.fetchall():
    print(" -", col[0], f"({col[1]})")

cur.execute("SELECT COUNT(*) FROM monitoring.unit_concession_sp;")
print("Jumlah baris di Supabase:", cur.fetchone()[0])

print("\n=== Cek base_adminvillage_alert di Supabase ===")
cur.execute("SELECT COUNT(*) FROM monitoring.base_adminvillage_alert;")
print("Jumlah baris desa:", cur.fetchone()[0])

print("\n=== Cek base_landcover_change di Supabase ===")
cur.execute("SELECT COUNT(*) FROM monitoring.base_landcover_change;")
print("Jumlah baris landcover:", cur.fetchone()[0])

conn.close()
