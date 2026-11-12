import { Pool } from 'pg';

/** Singleton PostgreSQL connection pool untuk semua route handler & HMR. */
export function getPool() {
  const url = process.env.DATABASE_URL || '';
  const host = process.env.DB_HOST || 'localhost';
  const port = parseInt(process.env.POSTGRES_PORT || '5434', 10);
  const user = process.env.POSTGRES_USER || 'postgres';
  const password = process.env.POSTGRES_PASSWORD || 'postgres';
  const database = process.env.POSTGRES_DB || 'geonode_data';
  const poolKey = url || `${user}@${host}:${port}/${database}`;

  if (!globalThis.__forensikPgPool || globalThis.__forensikPgPoolKey !== poolKey) {
    if (globalThis.__forensikPgPool) {
      globalThis.__forensikPgPool.end().catch(() => {});
    }
    if (url) {
      const isSsl = url.includes('supabase') || process.env.DB_SSL === 'true';
      globalThis.__forensikPgPool = new Pool({
        connectionString: url,
        max: 10,
        idleTimeoutMillis: 30000,
        connectionTimeoutMillis: 15000,
        ssl: isSsl ? { rejectUnauthorized: false } : undefined,
      });
    } else {
      const isSsl = host.includes('supabase') || process.env.DB_SSL === 'true';
      globalThis.__forensikPgPool = new Pool({
        host,
        port,
        user,
        password,
        database,
        max: 10,
        idleTimeoutMillis: 30000,
        connectionTimeoutMillis: 15000,
        ssl: isSsl ? { rejectUnauthorized: false } : undefined,
      });
    }
    globalThis.__forensikPgPoolKey = poolKey;
  }
  return globalThis.__forensikPgPool;
}

