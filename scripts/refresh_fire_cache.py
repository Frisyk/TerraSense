#!/usr/bin/env python3
"""
Sinkronisasi tabel tipis `monitoring.fire_points` dan rollup `monitoring.fire_hourly_grid`.

Contoh pemakaian:
  python scripts/refresh_fire_cache.py --init               # Buat tabel/fungsi + backfill dari 2026-01-01
  python scripts/refresh_fire_cache.py --init --since 2024-01-01
  python scripts/refresh_fire_cache.py                      # Refresh inkremental (3 hari / 96 jam terakhir)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"

for _p in [str(_PROJECT_ROOT), str(_BACKEND_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import app.db.db_connect as db_connect
except ImportError:
    try:
        import backend.db_connect as db_connect
    except ImportError:
        import db_connect


async def ensure_rollup_schema():
    """Jalankan DDL init-db/04-fire-rollup.sql jika tabel/fungsi belum ada."""
    sql_file = _PROJECT_ROOT / "init-db" / "04-fire-rollup.sql"
    if not sql_file.exists():
        return
    ddl = sql_file.read_text(encoding="utf-8")
    from sqlalchemy import text
    with db_connect.sync_engine.begin() as conn:
        conn.execute(text(ddl))


async def refresh_fire_cache():
    """Sinkronisasi inkremental 3 hari ke belakang & rollup 96 jam ke belakang (untuk APScheduler / cron)."""
    await db_connect.run_sql_area(
        "SELECT monitoring.sync_fire_points(now() - interval '3 days')"
    )
    await db_connect.run_sql_area(
        "SELECT monitoring.refresh_fire_hourly_grid("
        "(floor(extract(epoch FROM (now() AT TIME ZONE 'Asia/Jakarta')) / 3600) - 96)::int)"
    )


def parse_since(val: str) -> datetime:
    """Konversi string input tanggal/waktu ke objek datetime dengan timezone UTC."""
    val = val.strip().replace(" ", "T")
    if len(val) == 10:
        dt = datetime.strptime(val, "%Y-%m-%d")
    else:
        dt = datetime.fromisoformat(val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def init_and_backfill(since: str = "2026-01-01"):
    """Pastikan skema tersedia, isi penuh sejak `since`, dan hitung ulang seluruh rollup."""
    print("[1/3] Memastikan skema monitoring.fire_points & monitoring.fire_hourly_grid...")
    await ensure_rollup_schema()

    dt_since = parse_since(since)
    print(f"[2/3] Menyinkronkan monitoring.fire_points sejak {dt_since.isoformat()}...")
    await db_connect.run_sql_area(
        "SELECT monitoring.sync_fire_points(:since)",
        {"since": dt_since},
    )

    print("[3/3] Membangun rollup monitoring.fire_hourly_grid...")
    await db_connect.run_sql_area("SELECT monitoring.refresh_fire_hourly_grid(0)")

    rows = await db_connect.run_sql_area(
        "SELECT (SELECT count(*) FROM monitoring.fire_points) AS pts, "
        "(SELECT count(*) FROM monitoring.fire_hourly_grid) AS cells"
    )
    if rows:
        print(f"Selesai! fire_points={rows[0][0]:,} baris, fire_hourly_grid={rows[0][1]:,} sel.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Refresh fire_points & fire_hourly_grid cache")
    ap.add_argument("--init", action="store_true", help="Buat skema dan lakukan backfill awal")
    ap.add_argument("--since", default="2026-01-01", help="Tanggal awal backfill (default: 2026-01-01)")
    args = ap.parse_args()

    if args.init:
        asyncio.run(init_and_backfill(args.since))
    else:
        asyncio.run(refresh_fire_cache())

