import psycopg2
import sys

def check_db(name, url):
    print(f"\n--- Checking {name} ---")
    try:
        conn = psycopg2.connect(url, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT version();")
        print(f"Connected! Version: {cur.fetchone()[0]}")

        cur.execute("""
            SELECT table_schema, table_name 
            FROM information_schema.tables 
            WHERE table_schema IN ('monitoring', 'public', 'system') AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name;
        """)
        tables = cur.fetchall()
        print(f"Total tables: {len(tables)}")
        for s, t in tables:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{s}"."{t}"')
                cnt = cur.fetchone()[0]
                print(f"  {s}.{t}: {cnt:,} rows")
            except Exception as e:
                print(f"  {s}.{t}: error ({e})")
                conn.rollback()

        conn.close()
    except Exception as e:
        print(f"Failed to connect to {name}: {e}")

if __name__ == "__main__":
    local_url = "postgresql://postgres:postgres@fire_postgis_db:5432/geonode_data"
    supabase_url = "postgresql://postgres.envnrqgaoswhkwptesta:Kg1nnnkwIKfD1Bph@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"

    check_db("Local PostgreSQL (geonode_data)", local_url)
    check_db("Remote Supabase (postgres)", supabase_url)

