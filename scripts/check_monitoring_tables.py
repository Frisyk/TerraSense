import psycopg2

PROD_URL = "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
conn = psycopg2.connect(PROD_URL)
cur = conn.cursor()

cur.execute("""
    SELECT table_name 
    FROM information_schema.tables 
    WHERE table_schema = 'monitoring'
    ORDER BY table_name;
""")
print("Tabel di schema monitoring (Supabase):")
for row in cur.fetchall():
    print(" -", row[0])

conn.close()
