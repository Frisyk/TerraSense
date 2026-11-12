import { NextResponse } from 'next/server';
import { getPool } from '../../../lib/db';
import { BBOX_KALBAR, runForensicPipeline } from '../../../lib/forensicEngine';

export const dynamic = 'force-dynamic';

const VIIRS_TABLES = [
  'alert_fire_viirssnpp_raw',
  'alert_fire_viirsnoaa20_raw',
  'alert_fire_viirsnoaa21_raw',
];

function getCache() {
  if (!globalThis.__forensikApiCache) {
    globalThis.__forensikApiCache = new Map();
  }
  return globalThis.__forensikApiCache;
}

async function getSchemaInfo(client) {
  if (globalThis.__forensikSchemaInfo && Date.now() - globalThis.__forensikSchemaInfo.ts < 60_000) {
    return globalThis.__forensikSchemaInfo;
  }
  const res = await client.query(`
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'monitoring'
      AND table_name IN (
        'alert_fire_viirssnpp_raw',
        'alert_fire_viirsnoaa20_raw',
        'alert_fire_viirsnoaa21_raw',
        'alert_fire_firms',
        'fire_points',
        'base_adminvillage_alert'
      )
  `);
  const tables = new Map();
  for (const r of res.rows) {
    if (!tables.has(r.table_name)) tables.set(r.table_name, new Set());
    tables.get(r.table_name).add(r.column_name);
  }
  const info = { ts: Date.now(), tables };
  globalThis.__forensikSchemaInfo = info;
  return info;
}

async function detectDateBounds(client, tables) {
  const [lon0, lat0, lon1, lat1] = BBOX_KALBAR;
  const rawExisting = VIIRS_TABLES.filter((t) => tables.has(t));

  try {
    if (rawExisting.length > 0) {
      const unions = rawExisting
        .map(
          (t) => `
          SELECT MIN(datetm) AS min_dt, MAX(datetm) AS max_dt
          FROM monitoring.${t}
          WHERE latitude BETWEEN $1 AND $2 AND longitude BETWEEN $3 AND $4`
        )
        .join(' UNION ALL ');
      const r = await client.query(
        `SELECT to_char(MIN(min_dt) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS min_d,
                to_char((MAX(max_dt) AT TIME ZONE 'UTC') + interval '1 day', 'YYYY-MM-DD') AS max_d
         FROM (${unions}) u`,
        [lat0, lat1, lon0, lon1]
      );
      if (r.rows[0]?.max_d) return r.rows[0];
    }

    if (tables.has('alert_fire_firms')) {
      const r = await client.query(
        `SELECT to_char(MIN(datetm) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS min_d,
                to_char((MAX(datetm) AT TIME ZONE 'UTC') + interval '1 day', 'YYYY-MM-DD') AS max_d
         FROM monitoring.alert_fire_firms
         WHERE latitude BETWEEN $1 AND $2 AND longitude BETWEEN $3 AND $4`,
        [lat0, lat1, lon0, lon1]
      );
      if (r.rows[0]?.max_d) return r.rows[0];
    }

    if (tables.has('fire_points')) {
      const r = await client.query(
        `SELECT to_char(MIN(datetm) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS min_d,
                to_char((MAX(datetm) AT TIME ZONE 'UTC') + interval '1 day', 'YYYY-MM-DD') AS max_d
         FROM monitoring.fire_points
         WHERE kind = 0 AND lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4`,
        [lat0, lat1, lon0, lon1]
      );
      if (r.rows[0]?.max_d) return r.rows[0];
    }
  } catch (e) {
    console.warn('Gagal mendeteksi rentang tanggal otomatis:', e.message);
  }
  return null;
}

export async function GET(request) {
  const sp = new URL(request.url).searchParams;
  let start = sp.get('start') || '';
  let end = sp.get('end') || '';
  const minConf = ['l', 'n', 'h'].includes(sp.get('minConf')) ? sp.get('minConf') : 'n';
  const epsS = Math.max(200, Math.min(10000, parseFloat(sp.get('epsS') || '1500')));
  const epsTH = Math.max(1, Math.min(240, parseFloat(sp.get('epsTH') || '48')));
  const minPoints = Math.max(2, Math.min(500, parseInt(sp.get('minPoints') || '3', 10)));
  const runs = Math.max(20, Math.min(500, parseInt(sp.get('runs') || '200', 10)));
  const alpha = Math.max(300, Math.min(5000, parseFloat(sp.get('alpha') || '1300')));
  const useBoundary = sp.get('useBoundary') === '1';
  const province = (sp.get('province') || 'KALIMANTAN BARAT').toUpperCase();

  let client = null;
  try {
    client = await getPool().connect();
    const { tables } = await getSchemaInfo(client);
    const bounds = await detectDateBounds(client, tables);

    // Smart Initial Window: default 7 hari terakhir dari data terbaru di DB (bukan 150 hari!)
    const initialDays = Math.max(1, Math.min(90, parseInt(sp.get('days') || '7', 10)));
    if (!end) {
      end = bounds?.max_d || new Date().toISOString().slice(0, 10);
    }
    if (!start) {
      const endMs = Date.parse(`${end}T00:00:00Z`);
      const startMs = Number.isFinite(endMs) ? endMs - initialDays * 86400_000 : Date.now() - initialDays * 86400_000;
      start = new Date(startMs).toISOString().slice(0, 10);
    }

    const page = Math.max(1, parseInt(sp.get('page') || '1', 10));
    const pageSizeParam = sp.get('pageSize');
    const pageSize = pageSizeParam === 'all' ? 0 : Math.max(5, Math.min(200, parseInt(pageSizeParam || '30', 10)));
    const maxPoints = Math.max(1000, Math.min(50000, parseInt(sp.get('maxPoints') || '20000', 10)));
    const paginateServer = sp.get('paginateServer') === '1';

    const cacheKey = `${start}|${end}|${minConf}|${epsS}|${epsTH}|${minPoints}|${runs}|${alpha}|${useBoundary}|${province}|${page}|${pageSize}|${maxPoints}|${paginateServer}`;
    const cache = getCache();
    const cachedHit = cache.get(cacheKey);
    if (cachedHit && Date.now() - cachedHit.ts < 120_000) {
      return NextResponse.json(cachedHit.payload, {
        headers: { 'X-Cache': 'HIT' },
      });
    }

    const [lon0, lat0, lon1, lat1] = BBOX_KALBAR;
    const rawExisting = VIIRS_TABLES.filter((t) => tables.has(t));
    const hasBoundaryTable = useBoundary && tables.has('base_adminvillage_alert');
    const perTableLimit = Math.max(2000, Math.floor(maxPoints / Math.max(1, rawExisting.length)));

    let rawRows = [];

    // 1. Jalur Utama: monitoring.alert_fire_viirssnpp_raw, viirsnoaa20_raw, viirsnoaa21_raw
    if (rawExisting.length > 0) {
      for (const table of rawExisting) {
        if (rawRows.length >= maxPoints) break;
        const cols = tables.get(table);
        const satExpr = cols.has('satellite') ? `COALESCE(p.satellite::text, '${table}')` : `'${table}'`;
        const frpExpr = cols.has('frp') ? `COALESCE(p.frp::float8, 1.0)` : `1.0::float8`;
        const confExpr = cols.has('confidence') ? `COALESCE(p.confidence::text, 'n')` : `'n'`;
        const boundaryClause =
          hasBoundaryTable && cols.has('geometry')
            ? `AND EXISTS (
                 SELECT 1 FROM monitoring.base_adminvillage_alert v
                 WHERE upper(v.wadmpr) = $7 AND ST_Intersects(v.geometry, p.geometry)
               )`
            : '';
        const params = [start, end, lat0, lat1, lon0, lon1];
        if (boundaryClause) params.push(province);

        const sql = `
          SELECT p.latitude::float8 AS latitude,
                 p.longitude::float8 AS longitude,
                 ${frpExpr} AS frp,
                 ${confExpr} AS confidence,
                 ${satExpr} AS satellite,
                 (EXTRACT(EPOCH FROM p.datetm::timestamptz) * 1000)::float8 AS t_ms
          FROM monitoring.${table} p
          WHERE p.datetm >= $1::timestamptz
            AND p.datetm < $2::timestamptz
            AND p.latitude BETWEEN $3 AND $4
            AND p.longitude BETWEEN $5 AND $6
            ${boundaryClause}
          ORDER BY p.datetm ASC
          LIMIT ${perTableLimit}
        `;
        const res = await client.query(sql, params);
        if (res.rows.length > 0) {
          for (let i = 0; i < res.rows.length; i++) rawRows.push(res.rows[i]);
        }
      }
    }

    // 2. Jalur Fallback A: monitoring.alert_fire_firms
    if (rawRows.length === 0 && tables.has('alert_fire_firms')) {
      const cols = tables.get('alert_fire_firms');
      const satExpr = cols.has('satellite')
        ? `COALESCE(p.satellite::text, p.source::text, 'VIIRS')`
        : `COALESCE(p.source::text, 'VIIRS')`;
      const sql = `
        SELECT p.latitude::float8 AS latitude,
               p.longitude::float8 AS longitude,
               COALESCE(p.frp::float8, 1.0) AS frp,
               COALESCE(p.confidence::text, 'n') AS confidence,
               ${satExpr} AS satellite,
               (EXTRACT(EPOCH FROM p.datetm::timestamptz) * 1000)::float8 AS t_ms
        FROM monitoring.alert_fire_firms p
        WHERE p.datetm >= $1::timestamptz
          AND p.datetm < $2::timestamptz
          AND p.latitude BETWEEN $3 AND $4
          AND p.longitude BETWEEN $5 AND $6
        LIMIT 80000
      `;
      const res = await client.query(sql, [start, end, lat0, lat1, lon0, lon1]);
      rawRows = res.rows;
    }

    // 3. Jalur Fallback B: monitoring.fire_points (jika hanya tabel rollup yang terisi)
    if (rawRows.length === 0 && tables.has('fire_points')) {
      const sql = `
        SELECT p.lat::float8 AS latitude,
               p.lon::float8 AS longitude,
               GREATEST(1.0, COALESCE(p.bright::float8 - 300.0, 5.0)) AS frp,
               COALESCE(p.conf::text, 'n') AS confidence,
               COALESCE(p.sensor::text, 'VIIRS') AS satellite,
               (EXTRACT(EPOCH FROM p.datetm) * 1000)::float8 AS t_ms
        FROM monitoring.fire_points p
        WHERE p.kind = 0
          AND p.datetm >= $1::timestamptz
          AND p.datetm < $2::timestamptz
          AND p.lat BETWEEN $3 AND $4
          AND p.lon BETWEEN $5 AND $6
        LIMIT 80000
      `;
      const res = await client.query(sql, [start, end, lat0, lat1, lon0, lon1]);
      rawRows = res.rows;
    }

    // Adaptive Monte Carlo runs agar respons sub-detik pada volume data besar
    let effectiveRuns = runs;
    if (rawRows.length > 5000) {
      effectiveRuns = Math.min(runs, 40);
    } else if (rawRows.length > 2000) {
      effectiveRuns = Math.min(runs, 60);
    }

    const payload = runForensicPipeline(rawRows, {
      start,
      end,
      minConf,
      epsS,
      epsTH,
      minPoints,
      runs: effectiveRuns,
      alpha,
    });
    payload.meta.dbBounds = bounds;
    payload.meta.rawFetched = rawRows.length;

    const totalAllClusters = payload.clusters.length;
    const totalPages = pageSize > 0 ? Math.ceil(totalAllClusters / pageSize) : 1;

    payload.meta.pagination = {
      page,
      pageSize: pageSize > 0 ? pageSize : totalAllClusters,
      totalClusters: totalAllClusters,
      totalPages: Math.max(1, totalPages),
      hasNext: pageSize > 0 ? page < totalPages : false,
      hasPrev: page > 1,
    };

    if (pageSize > 0 && paginateServer) {
      const startIdx = (page - 1) * pageSize;
      payload.clusters = payload.clusters.slice(startIdx, startIdx + pageSize);
    }

    cache.set(cacheKey, { ts: Date.now(), payload });
    if (cache.size > 12) {
      cache.delete(cache.keys().next().value);
    }

    return NextResponse.json(payload, {
      headers: { 'X-Cache': 'MISS' },
    });
  } catch (error) {
    console.error('Error in /api/forensik:', error);
    return NextResponse.json(
      { error: 'Gagal memuat data forensik dari database PostgreSQL', details: error.message },
      { status: 500 }
    );
  } finally {
    if (client) client.release();
  }
}

