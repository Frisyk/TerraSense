import psycopg2

def run_init():
    print("Connecting to Supabase to run 01-init.sql...")
    conn = psycopg2.connect(
        host="aws-0-ap-south-1.pooler.supabase.com",
        port=5432,
        user="postgres.envnrqgaoswhkwptesta",
        password="Kg1nnnkwIKfD1Bph",
        dbname="postgres"
    )
    conn.autocommit = True
    cur = conn.cursor()

    with open("/app/init-db/01-init.sql", "r") as f:
        sql = f.read()

    cur.execute(sql)
    print("01-init.sql successfully applied to Supabase!")

    # Check tables created
    cur.execute("""
        SELECT table_schema, table_name 
        FROM information_schema.tables 
        WHERE table_schema IN ('monitoring', 'system')
        ORDER BY table_schema, table_name;
    """)
    print("Tables in monitoring/system schemas on Supabase:")
    for s, t in cur.fetchall():
        print(f"  {s}.{t}")

    conn.close()

if __name__ == "__main__":
    run_init()
