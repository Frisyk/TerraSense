"""Terapkan init-db/03-fire-spread.sql ke DB yang dipakai backend (Supabase/DATABASE_URL)."""
import os
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (root, os.path.join(root, "backend")):
    sys.path.insert(0, p)
from app.db.db_connect import sync_engine

sql = open(os.path.join(root, "init-db", "03-fire-spread.sql"), encoding="utf-8").read()
c = sync_engine.raw_connection()
cur = c.cursor()
cur.execute(sql)
c.commit()
cur.execute("select column_name from information_schema.columns where table_schema='monitoring' "
            "and table_name='fire_spread' and column_name in ('parent_lon','parent_lat','edge_censored')")
print(cur.fetchall())
c.close()
