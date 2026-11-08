import asyncio
import app.db.db_connect as db_connect


async def refresh_fire_cache():
    # Data telat datang: sinkron 3 hari ke belakang, rollup 96 jam ke belakang
    await db_connect.run_sql_area(
        "SELECT monitoring.sync_fire_points(now() - interval '3 days')"
    )
    await db_connect.run_sql_area(
        "SELECT monitoring.refresh_fire_hourly_grid("
        "(floor(extract(epoch FROM (now() AT TIME ZONE 'Asia/Jakarta')) / 3600) - 96)::int)"
    )


if __name__ == "__main__":
    asyncio.run(refresh_fire_cache())

