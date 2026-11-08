import os, psycopg2, time
from dotenv import load_dotenv

load_dotenv('backend/.env')
url = os.environ.get('DATABASE_URL')
conn = psycopg2.connect(url)
cur = conn.cursor()

print("--- Testing origins query ---")
t0 = time.time()
cur.execute("""
EXPLAIN ANALYZE
SELECT s.lon, s.lat, s.cluster_id, s.cluster_size, s.child_count,
       to_char(s.datetm + interval '7 hours', 'YYYY-MM-DD"T"HH24:MI') AS t,
       (EXTRACT(EPOCH FROM (s.datetm + interval '7 hours')) / 60)::float8 AS tm
  FROM monitoring.fire_spread s
 WHERE s.datetm >= ('2026-10-14 00:00'::timestamp - interval '7 hours')
   AND s.datetm <= ('2026-10-28 23:59'::timestamp - interval '7 hours')
   AND s.is_origin
 LIMIT 4000;
""")
for r in cur.fetchall():
    print(r[0])
print(f"Time: {time.time() - t0:.3f}s\n")

print("--- Testing hotspots grid query ---")
t0 = time.time()
cur.execute("""
EXPLAIN ANALYZE
WITH src AS (
  SELECT 0 AS kind, source::text AS sensor, latitude::float8 AS lat, longitude::float8 AS lon,
         datetm_loc, COALESCE(brightness, 0)::float8 AS bright,
         COALESCE(confidence::text, '-') AS conf, 'Kalimantan'::text AS prov
  FROM monitoring.alert_fire_firms
  WHERE datetm_loc >= '2026-10-14 00:00:00' AND datetm_loc <= '2026-10-28 23:59:00'
)
SELECT kind,
       floor(lon / 0.05)::int AS gx,
       floor(lat / 0.05)::int AS gy,
       floor(extract(epoch FROM datetm_loc::timestamp) / 3600)::int AS hr,
       count(*)::int AS cnt,
       max(bright) AS bright
FROM src
WHERE lat IS NOT NULL AND lon IS NOT NULL
GROUP BY 1, 2, 3, 4
ORDER BY hr;
""")
for r in cur.fetchall():
    print(r[0])
print(f"Time: {time.time() - t0:.3f}s\n")
