"""
Database and GIS Utilities Module - OPTIMIZED & ASYNC VERSION
Supports both Local PostGIS and Supabase (Direct / Connection Pooler)

This module provides utility functions to interact with a PostgreSQL/PostGIS database and QGIS.
It includes functions for executing SQL queries, retrieving settings, processing GIS layers,
and converting results to JSON or Earth Engine FeatureCollections.

Dependencies:
- SQLAlchemy for database interaction (with async support).
- asyncpg for async PostgreSQL driver.
- psycopg2 for sync PostgreSQL operations.
- QGIS for geographic data manipulation (optional).
- GeoJSON and Earth Engine for geospatial data handling (optional).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote_plus, urlparse, parse_qs, urlencode, urlunparse

try:
    import geojson
except ImportError:
    geojson = None
import pandas as pd
import sqlalchemy as sal
from sqlalchemy import create_engine as _create_sync_engine, text
from sqlalchemy.exc import (
    DisconnectionError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_scoped_session, create_async_engine
from sqlalchemy.orm import sessionmaker

try:
    from app.core.config import SPATIAL_DATABASE_CONFIG, DATABASE_URL
except ImportError:
    try:
        from backend.config import SPATIAL_DATABASE_CONFIG, DATABASE_URL
    except ImportError:
        from config import SPATIAL_DATABASE_CONFIG, DATABASE_URL

try:
    from app.core.exceptions import DatabaseException
except ImportError:
    class DatabaseException(Exception):
        pass

try:
    from app.utils.utilities import convert_result_to_json
except ImportError:
    def convert_result_to_json(result):
        if hasattr(result, "to_json"):
            return result.to_json()
        return json.dumps(result, default=str)

try:
    from qgis.core import QgsDataSourceUri, QgsVectorLayer
except ImportError:
    QgsDataSourceUri = None
    QgsVectorLayer = None

try:
    import ee
except ImportError:
    ee = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection URLs Resolution
# ---------------------------------------------------------------------------

def _build_connection_urls():
    """
    Build asyncpg and psycopg2 connection URLs.
    Prioritizes DATABASE_URL if present, otherwise uses SPATIAL_DATABASE_CONFIG.
    Handles Supabase SSL requirements and special characters in passwords.
    """
    raw_url = os.getenv("DATABASE_URL") or (globals().get("DATABASE_URL"))
    if raw_url:
        # Normalize protocol prefix
        url_clean = raw_url
        if url_clean.startswith("postgres://"):
            url_clean = "postgresql://" + url_clean[len("postgres://"):]

        if url_clean.startswith("postgresql://"):
            async_url = "postgresql+asyncpg://" + url_clean[len("postgresql://"):]
            sync_url = "postgresql+psycopg2://" + url_clean[len("postgresql://"):]
        elif url_clean.startswith("postgresql+asyncpg://"):
            async_url = url_clean
            sync_url = "postgresql+psycopg2://" + url_clean[len("postgresql+asyncpg://"):]
        elif url_clean.startswith("postgresql+psycopg2://"):
            async_url = "postgresql+asyncpg://" + url_clean[len("postgresql+psycopg2://"):]
            sync_url = url_clean
        else:
            async_url = url_clean
            sync_url = url_clean

        return async_url, sync_url

    # Fallback to SPATIAL_DATABASE_CONFIG
    user = quote_plus(str(SPATIAL_DATABASE_CONFIG["user"]))
    password = quote_plus(str(SPATIAL_DATABASE_CONFIG["password"]))
    host = SPATIAL_DATABASE_CONFIG["host"]
    port = str(SPATIAL_DATABASE_CONFIG["port"])
    database = SPATIAL_DATABASE_CONFIG["database"]

    # Supabase hosts typically require sslmode
    ssl_param = ""
    if "supabase" in host or os.getenv("DB_SSLMODE") == "require":
        ssl_param = "?sslmode=require"

    async_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"
    sync_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}{ssl_param}"
    return async_url, sync_url

_DB_URL_ASYNC, _DB_URL_SYNC = _build_connection_urls()

# ---------------------------------------------------------------------------
# Async engine (used by all async query helpers)
# ---------------------------------------------------------------------------

engine = create_async_engine(
    _DB_URL_ASYNC,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    pool_timeout=180,
    pool_recycle=3600,
    echo=False,
    connect_args={
        "timeout": 180,
        "server_settings": {
            "application_name": "fist_async",
            "statement_timeout": "1800000",
        },
    },
)

# Backward-compat alias
engine_area = engine

# ---------------------------------------------------------------------------
# Sync engine (for geopandas.to_postgis only - must run in a thread executor)
# ---------------------------------------------------------------------------

sync_engine = _create_sync_engine(
    _DB_URL_SYNC,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_timeout=600,
    pool_recycle=1800,
    connect_args={
        "connect_timeout": 60,
        "options": "-c statement_timeout=600000",
    },
)

# ---------------------------------------------------------------------------
# Session factories
# ---------------------------------------------------------------------------

_AsyncSessionFactory = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

AsyncSessionLocal = async_scoped_session(_AsyncSessionFactory, scopefunc=asyncio.current_task)

# Backward-compat alias
AsyncSessionArea = AsyncSessionLocal

# ---------------------------------------------------------------------------
# Session context managers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db_session():
    """
    Async context manager that yields a managed :class:`AsyncSession`.
    Commits on clean exit, rolls back on error.
    """
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except (OperationalError, DisconnectionError) as exc:
        await session.rollback()
        logger.error("DB connection error: %s", exc)
        await engine.dispose()
        raise
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.error("SQLAlchemy error: %s", exc)
        raise
    except Exception as exc:
        await session.rollback()
        logger.error("Unexpected DB error: %s", exc)
        raise
    finally:
        await session.close()
        await AsyncSessionLocal.remove()

get_db_session_area = get_db_session

# ---------------------------------------------------------------------------
# Core query helpers
# ---------------------------------------------------------------------------

async def run_sql(sql: str, params: dict | None = None, retry_count: int = 3):
    """
    Execute *sql* asynchronously with optional bound *params*.
    Retries up to *retry_count* times on transient connection failures.
    """
    last_exc: Exception | None = None

    for attempt in range(retry_count):
        try:
            async with get_db_session() as session:
                result = await session.execute(text(sql), params or {})
                return result.fetchall() if result.returns_rows else result

        except (OperationalError, DisconnectionError) as exc:
            last_exc = exc
            logger.warning(
                "Connection lost (attempt %d/%d): %s", attempt + 1, retry_count, exc
            )
            if attempt < retry_count - 1:
                wait = 2 ** attempt
                logger.info("Retrying in %d s ...", wait)
                await asyncio.sleep(wait)
                await engine.dispose()
            else:
                logger.error("All %d retry attempts exhausted.", retry_count)
                raise

        except SQLAlchemyError as exc:
            logger.error("SQL execution error: %s", exc)
            raise

    if last_exc:
        raise last_exc

run_sql_area = run_sql

async def run_sql_with_result_area(sql: str, params: dict | None = None):
    """Execute *sql* and return raw CursorResult without fetching."""
    try:
        async with get_db_session() as session:
            return await session.execute(text(sql), params or {})
    except Exception as exc:
        logger.error("Error in run_sql_with_result_area: %s", exc)
        raise

# ---------------------------------------------------------------------------
# High-level data fetchers
# ---------------------------------------------------------------------------

async def fetch_data_from_sql(sql: str, params: dict | None = None) -> list[dict]:
    """Execute *sql* and return results as a list of plain dicts."""
    try:
        logger.debug("Executing SQL: %.120s", sql)
        result = await run_sql(sql, params)
        data = json.loads(convert_result_to_json(result))
        logger.debug("Fetched %d rows.", len(data))
        return data
    except Exception:
        logger.error("fetch_data_from_sql failed:\n%s", traceback.format_exc())
        raise DatabaseException("Terjadi kesalahan saat mengambil data")

fetch_data_from_sql_area = fetch_data_from_sql

async def fetch_data_from_table(
    table_name: str,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict]:
    """Fetch all rows from *table_name* with optional pagination."""
    if not table_name.replace("_", "").replace(".", "").isalnum():
        raise ValueError(f"Invalid table name: {table_name!r}")

    sql = f"SELECT * FROM {table_name}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    if offset is not None:
        sql += f" OFFSET {int(offset)}"

    try:
        return await fetch_data_from_sql(sql)
    except Exception:
        logger.error("fetch_data_from_table failed:\n%s", traceback.format_exc())
        raise RuntimeError("Failed to fetch data from table")

# ---------------------------------------------------------------------------
# Settings & metadata helpers
# ---------------------------------------------------------------------------

async def get_setting_value(variable_name: str, default: any = None) -> any:
    """Retrieve a single value from system.setting by variable_name or key."""
    try:
        sql = "SELECT value FROM system.setting WHERE variable = :v OR key = :v LIMIT 1"
        async with get_db_session() as session:
            result = await session.execute(text(sql), {"v": variable_name})
            val = result.scalar_one_or_none()
            if val is not None:
                return val
    except Exception as exc:
        logger.debug("get_setting_value('%s') notice: %s", variable_name, exc)
    return default

async def fetch_unit_settings() -> dict[str, dict]:
    """Return all rows from system.unit keyed by unit_name."""
    try:
        sql = """
            SELECT unit_name, label_id, label_en, unit_column_name, unit_column_group
            FROM system.unit
        """
        async with get_db_session() as session:
            result = await session.execute(text(sql))
            rows = result.mappings().all()

        return {row["unit_name"]: dict(row) for row in rows}

    except Exception as exc:
        logger.error("fetch_unit_settings failed: %s", exc)
        raise RuntimeError("Failed to fetch unit settings")

# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

async def is_table_exist(table_name: str) -> bool:
    """Return True if table_name exists in the monitoring schema."""
    try:
        sql = """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'monitoring'
                  AND c.relname = :table_name
                  AND c.relkind IN ('r', 'v', 'm')
            )
        """
        async with get_db_session() as session:
            result = await session.execute(text(sql), {"table_name": table_name})
            return bool(result.scalar())

    except Exception:
        return False

# ---------------------------------------------------------------------------
# GIS / Earth Engine helpers
# ---------------------------------------------------------------------------

async def get_feature_collection(
    table_name: str, geometry_column: str = "geom"
) -> ee.FeatureCollection:
    """Load geometries from table_name and return as ee.FeatureCollection."""
    try:
        sql = f"""
            SELECT ST_AsGeoJSON(ST_MakeValid(ST_Force2D({geometry_column})))::json AS geom
            FROM {table_name}
            WHERE {geometry_column} IS NOT NULL
        """
        result = await run_sql(sql)
        features: list[ee.Geometry] = []

        for row in result:
            if not row[0]:
                continue
            try:
                feature = geojson.loads(str(row[0]).replace("'", '"'))
                features.append(ee.Geometry(feature))
            except Exception:
                logger.warning("Skipping invalid geometry in %s.", table_name)

        return ee.FeatureCollection(features)

    except Exception as exc:
        logger.error("get_feature_collection('%s') failed: %s", table_name, exc)
        raise RuntimeError(f"Failed to build FeatureCollection from '{table_name}'")

def load_layer_from_db(
    schema_name: str, table_name: str, geometry_column: str
) -> QgsVectorLayer:
    """Load a PostGIS table as a QgsVectorLayer (synchronous QGIS operation)."""
    try:
        uri = QgsDataSourceUri()
        uri.setConnection(
            SPATIAL_DATABASE_CONFIG["host"],
            str(SPATIAL_DATABASE_CONFIG["port"]),
            SPATIAL_DATABASE_CONFIG["database"],
            SPATIAL_DATABASE_CONFIG["user"],
            SPATIAL_DATABASE_CONFIG["password"],
        )
        uri.setDataSource(schema_name, table_name, geometry_column)
        layer = QgsVectorLayer(uri.uri(), f"{schema_name}_{table_name}", "postgres")

        if not layer.isValid():
            raise RuntimeError(
                f"QgsVectorLayer is invalid for {schema_name}.{table_name}"
            )
        return layer

    except Exception as exc:
        logger.error("load_layer_from_db failed: %s", exc)
        raise RuntimeError("Failed to load GIS layer from database")

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def split_schema_table(table_name: str) -> tuple[str | None, str]:
    """Split a qualified table name into (schema, table) components."""
    if "." in table_name:
        schema, table = table_name.split(".", 1)
        return schema, table
    return None, table_name

def extract_column(result, column_name: str) -> list:
    """Extract a single column from a SQLAlchemy query result as a plain list."""
    try:
        if hasattr(result, "_asdict"):
            return [result._asdict()[column_name]]
        return [
            row._asdict()[column_name] if hasattr(row, "_asdict") else row[column_name]
            for row in result
        ]
    except (KeyError, AttributeError) as exc:
        logger.error("extract_column('%s') failed: %s", column_name, exc)
        raise RuntimeError(f"Column '{column_name}' not found in query result")

async def filter_new_results_sql(
    results: pd.DataFrame,
    conn,
    table_name: str,
    key_columns: list[str],
    split_func=None,
) -> pd.DataFrame:
    """Return only the rows in *results* that do not yet exist in *table_name*."""
    _split = split_func or split_schema_table
    schema, table = _split(table_name)
    qualified = f"{schema}.{table}" if schema else table

    new_rows: list[pd.Series] = []

    for _, row in results.iterrows():
        clauses: list[str] = []
        for col in key_columns:
            if col not in row or pd.isna(row[col]):
                continue
            val = row[col]
            if isinstance(val, str):
                clauses.append(f"{col} = '{val.replace(chr(39), chr(39)*2)}'")
            elif isinstance(val, pd.Timestamp):
                clauses.append(f"{col} = '{val}'")
            else:
                clauses.append(f"{col} = {val}")

        if not clauses:
            new_rows.append(row)
            continue

        sql = f"SELECT COUNT(*) FROM {qualified} WHERE " + " AND ".join(clauses)
        try:
            res = await conn.execute(sal.text(sql))
            count = res.scalar() or 0
        except Exception as exc:
            logger.warning("Duplicate check failed for row: %s", exc)
            count = 0

        if int(count) == 0:
            new_rows.append(row)

    if new_rows:
        return pd.DataFrame(new_rows, columns=results.columns).reset_index(drop=True)
    return pd.DataFrame(columns=results.columns)

# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def health_check() -> bool:
    """Return True if the database is reachable, False otherwise."""
    try:
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

async def close_all_connections() -> None:
    """Gracefully close the session scope and dispose the connection pool."""
    try:
        await AsyncSessionLocal.remove()
        await engine.dispose()
        logger.info("All database connections closed.")
    except Exception as exc:
        logger.error("Error while closing connections: %s", exc)
