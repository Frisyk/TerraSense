# Optimasi Performa Load Hotspot

Oct 30, 2026 · @fris

## Ringkasan

Penyebab utama lambatnya load adalah setiap request grid dan summary memindai ±570 ribu baris (FIRMS ±357rb + SIPONGI 214rb untuk 1 bulan) dan menghitung jam/sel per baris. Targetnya: grid dan summary turun ke < 300 ms, tier raw per viewport < 1 detik.

Urutan kerja (tiap langkah bisa di-deploy sendiri dan aman di-rollback):

1. **Langkah 0** — ukur baseline dengan `EXPLAIN (ANALYZE, BUFFERS)`.
2. **Langkah 1** — quick win: `next build`, cache key stabil (±30 menit).
3. **Langkah 2** — tabel tipis terpadu `fire_points` + index.
4. **Langkah 3** — rollup per jam `fire_hourly_grid` + refresh terjadwal.
5. **Langkah 4** — ubah `lib/hotspotStore.js` agar memakai tabel baru (dengan flag rollback).
6. **Langkah 5** — cache server berbatas memori; opsional chunk per hari di klien.
7. **Langkah 6** — pisahkan perhitungan warna di `MapView.jsx`.
8. **Langkah 7** — verifikasi, rollback, checklist.

Asumsi: `alert_fire_firms` adalah tabel/view hasil gabungan FIRMS, `datetm` FIRMS bertipe `timestamptz` dan SIPONGI `timestamp` (UTC) — cocok dengan default di `hotspotStore.js`. Jika berbeda, sesuaikan di Langkah 2.

## Langkah 0 — Ukur baseline

Catat angka "sebelum" agar hasil optimasi bisa dibuktikan. Jalankan di `psql` pada rentang 1 bulan yang sama dengan yang Anda pakai; catat `Execution Time` dan node `Seq Scan`.

```sql
-- grid (meniru queryGrid, FIRMS saja agar singkat)
EXPLAIN (ANALYZE, BUFFERS)
SELECT floor(longitude/0.05)::int, floor(latitude/0.05)::int,
       floor(extract(epoch FROM datetm_loc::timestamp)/3600)::int AS hr, count(*)
FROM monitoring.alert_fire_firms
WHERE datetm >= '2026-09-30 17:00+00' AND datetm <= '2026-10-30 17:00+00'
GROUP BY 1,2,3;

-- ukuran tabel & index
SELECT relname, pg_size_pretty(pg_total_relation_size(oid))
FROM pg_class WHERE relname IN ('alert_fire_firms','alert_fire_sipongi_raw');

SELECT indexrelid::regclass, idx_scan FROM pg_stat_user_indexes
WHERE relname IN ('alert_fire_firms','alert_fire_sipongi_raw');
```

Di browser (DevTools → Network) catat waktu `/api/hotspots?tier=grid`, `/api/hotspots?tier=raw`, dan `/api/hotspots/summary` untuk 3 skenario: load awal, filter 30 hari, geser peta. Simpan hasilnya di tabel pada Langkah 7.

## Langkah 1 — Quick win (±30 menit, tanpa ubah database)

**1a. Jangan jalankan mode dev di produksi.** `npm run dev` mengompilasi route saat diminta dan tidak mengoptimasi bundel. Ganti `Dockerfile`:

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "run", "start"]
```

**1b. Kunci cache yang stabil di `app/api/hotspots/route.js`.** Saat ini `key = request.url`, sehingga urutan parameter atau parameter tak relevan membuat cache miss. Bangun dari nilai yang sudah dinormalisasi:

```js
const key = `hs|${tier}|${source}|${start}|${end}|${bbox ? bbox.join(',') : ''}|${tier === 'grid' ? sp.get('cell') : ''}`;
```

Lakukan hal yang sama di `summary/route.js` (`hs-sum|${source}|${start}|${end}|${bbox}`).

**1c. Hindari dua agregasi berat bersamaan.** Di `app/page.jsx`, `applyFilter` memanggil `fetchSummary` saat `MapView` juga memuat `/api/hotspots`. Sementara rollup (Langkah 3) belum ada, tunda summary sampai peta selesai: simpan filter di state, lalu panggil `fetchSummary(filter)` dalam `useEffect` yang menunggu `mapLoading === false`. Setelah Langkah 4, summary hanya ms sehingga penundaan ini boleh dihapus.

**1d. Pastikan index ada.** Dari hasil Langkah 0: bila `idx_scan` untuk `datetm` dan `geometry` bernilai 0 atau index tidak ada, buat manual (di luar jam sibuk):

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS alert_fire_sipongi_raw_datetm_idx
  ON monitoring.alert_fire_sipongi_raw (datetm);
CREATE INDEX CONCURRENTLY IF NOT EXISTS alert_fire_sipongi_raw_geom_idx
  ON monitoring.alert_fire_sipongi_raw USING GIST (geometry);
```

`ensureIndexesInBackground` sudah mencoba ini, tetapi gagal diam-diam bila user DB read-only.

## Langkah 2 — Tabel tipis terpadu `fire_points`

Satu tabel berisi FIRMS + SIPONGI dengan kolom yang sudah dihitung (jam lokal, geometri), sehingga query tidak lagi `UNION ALL` ke beberapa tabel lebar dan tidak menghitung `epoch` per baris.

**2a. Buat tabel dan index** (sekali, di `psql`):

```sql
CREATE TABLE IF NOT EXISTS monitoring.fire_points (
  id         bigserial PRIMARY KEY,
  kind       smallint     NOT NULL,           -- 0 = FIRMS, 1 = SIPONGI
  sensor     text         NOT NULL,
  lat        double precision NOT NULL,
  lon        double precision NOT NULL,
  datetm     timestamptz  NOT NULL,           -- UTC
  datetm_loc timestamp    NOT NULL,           -- jam dinding WIB
  hr_loc     integer GENERATED ALWAYS AS
             (floor(extract(epoch FROM datetm_loc) / 3600)::int) STORED,
  bright     real NOT NULL DEFAULT 0,
  conf       text,
  prov       text,
  geom       geometry(Point,4326) GENERATED ALWAYS AS
             (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED
);

CREATE UNIQUE INDEX IF NOT EXISTS fire_points_uq
  ON monitoring.fire_points (kind, sensor, lat, lon, datetm);
CREATE INDEX IF NOT EXISTS fire_points_datetm_idx ON monitoring.fire_points (datetm);
CREATE INDEX IF NOT EXISTS fire_points_geom_idx   ON monitoring.fire_points USING GIST (geom);
```

**2b. Fungsi sinkronisasi inkremental.** Idempotent (`ON CONFLICT DO NOTHING`), jadi aman dijalankan berulang dan untuk data telat datang:

```sql
CREATE OR REPLACE FUNCTION monitoring.sync_fire_points(p_since timestamptz)
RETURNS void LANGUAGE sql AS $$
  INSERT INTO monitoring.fire_points
        (kind, sensor, lat, lon, datetm, datetm_loc, bright, conf, prov)
  SELECT 0, COALESCE(source::text,'FIRMS'), latitude, longitude, datetm, datetm_loc,
         COALESCE(brightness,0), COALESCE(confidence::text,'-'), 'Kalimantan'
  FROM monitoring.alert_fire_firms
  WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND datetm >= p_since
  ON CONFLICT DO NOTHING;

  INSERT INTO monitoring.fire_points
        (kind, sensor, lat, lon, datetm, datetm_loc, bright, conf, prov)
  SELECT 1, COALESCE(sumber,'SIPONGI'), lat, long, datetm AT TIME ZONE 'UTC', datetm_loc,
         0, COALESCE(confidence_level, confidence::text, 'unknown'),
         COALESCE(nama_provinsi,'Kalimantan')
  FROM monitoring.alert_fire_sipongi_raw
  WHERE lat IS NOT NULL AND long IS NOT NULL
    AND (datetm AT TIME ZONE 'UTC') >= p_since
  ON CONFLICT DO NOTHING;
$$;
```

Catatan: `datetm AT TIME ZONE 'UTC'` benar bila `datetm` SIPONGI bertipe `timestamp` (naive UTC). Bila ternyata `timestamptz`, hapus bagian `AT TIME ZONE`. Bila `alert_fire_firms` bukan tabel/view (hanya ada tabel `_raw`), ganti sumbernya dengan `UNION ALL` empat tabel `alert_fire_*_raw`.

**2c. Backfill awal**, bertahap bila datanya besar:

```sql
SELECT monitoring.sync_fire_points('2026-01-01');
ANALYZE monitoring.fire_points;
```

Bila data lebih lama dari 2026-01-01 ingin disertakan, turunkan tanggalnya (jalankan bertahap per beberapa bulan).

**2d. Cek:**

```sql
SELECT kind, count(*) FROM monitoring.fire_points GROUP BY 1;
```

Jumlah harus mendekati jumlah baris sumber (selisih kecil wajar akibat duplikat yang dibuang constraint).

## Langkah 3 — Rollup per jam `fire_hourly_grid`

Tabel kecil berisi jumlah titik per (jam, sumber, sel 0.0125°). Ukuran sel 0.025°, 0.05°, dan 0.1° adalah kelipatan 2×, 4×, 8× dari sel dasar, sehingga cukup digabung ulang saat query. Satu bulan menjadi puluhan ribu baris, bukan 570 ribu.

**3a. Buat tabel dan fungsi refresh:**

```sql
CREATE TABLE IF NOT EXISTS monitoring.fire_hourly_grid (
  kind       smallint NOT NULL,
  hr         integer  NOT NULL,   -- jam dinding WIB: floor(epoch/3600)
  gx         integer  NOT NULL,   -- floor(lon / 0.0125)
  gy         integer  NOT NULL,   -- floor(lat / 0.0125)
  cnt        integer  NOT NULL,
  max_bright real     NOT NULL DEFAULT 0,
  PRIMARY KEY (hr, kind, gx, gy)
);

CREATE OR REPLACE FUNCTION monitoring.refresh_fire_hourly_grid(p_from_hr integer)
RETURNS void LANGUAGE sql AS $$
  DELETE FROM monitoring.fire_hourly_grid WHERE hr >= p_from_hr;
  INSERT INTO monitoring.fire_hourly_grid (kind, hr, gx, gy, cnt, max_bright)
  SELECT kind, hr_loc,
         floor(lon / 0.0125)::int, floor(lat / 0.0125)::int,
         count(*)::int, max(bright)
  FROM monitoring.fire_points
  WHERE hr_loc >= p_from_hr
  GROUP BY 1,2,3,4;
$$;
```

**3b. Build awal penuh, lalu cek ukuran:**

```sql
SELECT monitoring.refresh_fire_hourly_grid(0);
ANALYZE monitoring.fire_hourly_grid;
SELECT count(*) FROM monitoring.fire_hourly_grid;
```

**3c. Refresh terjadwal.** Buat `app/scripts/cron/refresh_fire_cache.py`:

```python
import asyncio
import app.db.db_connect as db_connect

async def refresh_fire_cache():
    # data telat datang: sinkron 3 hari ke belakang, rollup 96 jam ke belakang
    await db_connect.run_sql_area(
        "SELECT monitoring.sync_fire_points(now() - interval '3 days')")
    await db_connect.run_sql_area(
        "SELECT monitoring.refresh_fire_hourly_grid("
        "(floor(extract(epoch FROM (now() AT TIME ZONE 'Asia/Jakarta')) / 3600) - 96)::int)")

if __name__ == '__main__':
    asyncio.run(refresh_fire_cache())
```

Daftarkan di `app/scheduler.py` (di dalam `start_scheduler`), tiap 30 menit:

```python
from app.scripts.cron.refresh_fire_cache import refresh_fire_cache
scheduler.add_job(
    refresh_fire_cache,
    IntervalTrigger(minutes=30),
    id='refresh_fire_cache', name='Refresh fire_points + hourly grid',
    replace_existing=True,
)
```

Bila `backfill_alert_fire_*.py` dijalankan, panggil ulang `sync_fire_points('2000-01-01')` lalu `refresh_fire_hourly_grid(0)` setelahnya, karena backfill menulis data lama di luar jendela 3 hari.

## Langkah 4 — Ubah `lib/hotspotStore.js`

Strategi: tambahkan jalur baru, pertahankan jalur lama sebagai fallback. Bila tabel baru belum ada atau env `USE_ROLLUP=0`, perilaku tetap seperti sekarang.

**4a. Deteksi tabel baru** di dalam `ensureTables()` (di blok `try`, setelah `client` didapat):

```js
let hasRollup = false;   // taruh di level modul

// di dalam try { ... } pada ensureTables():
const rr = await client.query(`
  SELECT to_regclass('monitoring.fire_points') IS NOT NULL AS a,
         to_regclass('monitoring.fire_hourly_grid') IS NOT NULL AS b`);
hasRollup = process.env.USE_ROLLUP !== '0' && rr.rows[0].a && rr.rows[0].b;
```

**4b. Helper dan `queryGrid` / `querySummary` dari rollup.** Tambahkan di level modul:

```js
const BASE = 0.0125;

function rollupWhere({ start, end, source, bbox }) {
  const params = [];
  const p = (v) => { params.push(v); return `$${params.length}`; };
  const w = [];
  if (start) w.push(`hr >= floor(extract(epoch FROM ${p(start.replace('T', ' '))}::timestamp) / 3600)::int`);
  if (end)   w.push(`hr <= floor(extract(epoch FROM ${p(end.replace('T', ' '))}::timestamp) / 3600)::int`);
  if (source === 'firms') w.push('kind = 0');
  else if (source === 'sipongi') w.push('kind = 1');
  if (bbox) {
    w.push(`gx BETWEEN ${p(Math.floor(bbox[0] / BASE))} AND ${p(Math.floor(bbox[2] / BASE))}`,
           `gy BETWEEN ${p(Math.floor(bbox[1] / BASE))} AND ${p(Math.floor(bbox[3] / BASE))}`);
  }
  return { where: w.length ? 'WHERE ' + w.join(' AND ') : '', params, p };
}

async function queryGridRollup(opts, cell) {
  const k = Math.max(1, Math.round(cell / BASE));      // 0.0125→1, 0.025→2, 0.05→4, 0.1→8
  const { where, params, p } = rollupWhere(opts);
  const kRef = p(k);
  const sql = `
    SELECT kind,
           floor(gx::float8 / ${kRef}::float8)::int AS gx,
           floor(gy::float8 / ${kRef}::float8)::int AS gy,
           hr, sum(cnt)::int AS cnt, max(max_bright) AS bright
    FROM monitoring.fire_hourly_grid ${where}
    GROUP BY 1, 2, 3, 4`;
  const res = await withClient((c) => c.query({ text: sql, values: params, rowMode: 'array' }));
  const rows = res.rows, n = rows.length;
  const cell2 = k * BASE;
  const cols = {
    n, lon: new Float32Array(n), lat: new Float32Array(n), tMin: new Float64Array(n),
    cnt: new Float32Array(n), bright: new Float32Array(n),
    sensor: new Uint8Array(n), confIdx: new Uint8Array(n), provIdx: new Uint8Array(n),
    sensors: [['FIRMS', 0], ['SIPONGI', 1]], conf: ['-'], prov: ['Kalimantan'],
  };
  for (let i = 0; i < n; i++) {
    const r = rows[i];                         // [kind, gx, gy, hr, cnt, bright]
    cols.sensor[i] = r[0];
    cols.lon[i] = (r[1] + 0.5) * cell2;
    cols.lat[i] = (r[2] + 0.5) * cell2;
    cols.tMin[i] = r[3] * 60;
    cols.cnt[i] = r[4];
    cols.bright[i] = r[5] || 0;
  }
  return { cols, truncated: false };
}

async function querySummaryRollup(opts) {
  const bbox = opts.bbox || [108.0, -4.5, 119.5, 4.5];
  const { where, params } = rollupWhere({ ...opts, bbox });
  const sql = `
    SELECT kind, hr, sum(cnt)::int AS cnt,
           min(gx) * ${BASE} AS w, (max(gx) + 1) * ${BASE} AS e,
           min(gy) * ${BASE} AS s, (max(gy) + 1) * ${BASE} AS n
    FROM monitoring.fire_hourly_grid ${where}
    GROUP BY 1, 2 ORDER BY 2`;
  const res = await withClient((c) => c.query({ text: sql, values: params, rowMode: 'array' }));
  return buildSummary(res.rows);
}
```

Pada `querySummary` lama, pindahkan blok pemrosesan baris (mulai `if (rows.length === 0)` sampai `return { total, hr0, ... }`) ke fungsi `buildSummary(rows)`, lalu panggil dari kedua jalur. Format barisnya sama: `[kind, hr, cnt, w, e, s, n]`.

**4c. Titik mentah dari `fire_points`.** Pindahkan blok pemetaan baris → kolom di `queryRaw` lama ke `rowsToCols(rows, limit)` agar dipakai bersama, lalu:

```js
async function queryRawPoints({ start, end, source, bbox }, limit) {
  const params = [];
  const p = (v, cast) => { params.push(v); return `$${params.length}${cast ? '::' + cast : ''}`; };
  const w = [];
  if (start) w.push(`datetm >= ((${p(start.replace('T', ' '))}::timestamp - interval '7 hours') AT TIME ZONE 'UTC')`);
  if (end)   w.push(`datetm <= ((${p(end.replace('T', ' '))}::timestamp - interval '7 hours') AT TIME ZONE 'UTC')`);
  if (source === 'firms') w.push('kind = 0');
  else if (source === 'sipongi') w.push('kind = 1');
  if (bbox) {
    w.push(`geom && ST_MakeEnvelope(${p(bbox[0], 'float8')}, ${p(bbox[1], 'float8')}, ${p(bbox[2], 'float8')}, ${p(bbox[3], 'float8')}, 4326)`);
  }
  const cap = Number(limit) + 1;
  const sql = `
    SELECT kind, sensor, lat, lon,
           floor(extract(epoch FROM datetm_loc) / 60)::int AS tmin,
           bright, conf, COALESCE(prov, 'Kalimantan') AS prov
    FROM monitoring.fire_points
    ${w.length ? 'WHERE ' + w.join(' AND ') : ''}
    ORDER BY datetm DESC
    LIMIT ${cap}`;
  const res = await withClient((c) => c.query({ text: sql, values: params, rowMode: 'array' }));
  return rowsToCols(res.rows, limit);   // mengembalikan { cols, truncated }
}
```

`ORDER BY datetm DESC` membuat pemotongan limit membuang titik tertua, bukan acak.

**4d. Sambungkan dengan fallback:**

```js
export async function queryGrid(opts, cell) {
  await ensureTables();
  return hasRollup ? queryGridRollup(opts, cell) : queryGridLegacy(opts, cell);
}
export async function querySummary(opts) {
  await ensureTables();
  return hasRollup ? querySummaryRollup(opts) : querySummaryLegacy(opts);
}
export async function queryRaw(opts, limit = RAW_LIMIT) {
  await ensureTables();
  return hasRollup ? queryRawPoints(opts, limit) : queryRawLegacy(opts, limit);
}
```

Ganti nama fungsi lama menjadi `queryGridLegacy`, `querySummaryLegacy`, `queryRawLegacy`. `queryDetail` tidak diubah (ia mencari satu titik dengan GIST dan sudah cepat).

**4e. Uji sebelum deploy:**

```bash
# jalankan server dengan USE_ROLLUP=0, lalu:
curl -s 'localhost:3000/api/hotspots/summary?start=2026-10-01T00:00&end=2026-10-30T23:59' > old.json
# restart server dengan USE_ROLLUP=1, lalu:
curl -s 'localhost:3000/api/hotspots/summary?start=2026-10-01T00:00&end=2026-10-30T23:59' > new.json
```

`total` dan `dateStats` harus sama (atau selisih sangat kecil bila ada dedup di `fire_points`). Bandingkan juga jumlah titik grid antar dua jalur.

## Langkah 5 — Cache server yang tepat, chunk klien (opsional)

**5a. Batasi cache berdasarkan byte, bukan jumlah entri.** `CACHE_MAX = 32` terlalu kecil untuk pola geser peta, sedangkan satu respons raw bisa ±1.4 MB (60 ribu titik × 23 byte). Di `lib/hotspotStore.js`:

```js
const CACHE_MAX_BYTES = 150 * 1024 * 1024;
let cacheBytes = 0;
const sizeOf = (v) => (v instanceof ArrayBuffer ? v.byteLength : JSON.stringify(v).length);

// di dalam producer sukses, ganti blok cache.set + while(size > CACHE_MAX):
const old = cache.get(key);
if (old) cacheBytes -= old.size;
const size = sizeOf(value);
cache.set(key, { ts: Date.now(), value, size });
cacheBytes += size;
while (cacheBytes > CACHE_MAX_BYTES && cache.size > 1) {
  const k = cache.keys().next().value;
  cacheBytes -= cache.get(k).size;
  cache.delete(k);
}
```

Jangan lupa kurangi `cacheBytes` juga saat entri kedaluwarsa dihapus.

**5b. TTL menurut kesegaran data.** Rentang yang berakhir di masa lalu tidak berubah lagi, jadi cache lama. Di `app/api/hotspots/route.js`:

```js
const nowWallMs = Date.now() + 7 * 3600_000;                 // jam dinding WIB
const endMs = end ? Date.parse(end + ':00Z') : Infinity;     // 'YYYY-MM-DDTHH:mm' → UTC-as-wall
const isLive = endMs > nowWallMs - 3 * 3600_000;
const ttl = isLive ? 60 : 3600;                              // detik
const buf = await cached(key, producer, ttl);
// header: isLive ? 'private, max-age=30' : 'private, max-age=3600'
```

Catatan: filter default Anda berakhir tengah malam hari ini (`23:59`), sehingga selalu dianggap live. Itu memang benar karena hari ini masih menerima data.

**5c. Chunk per hari di klien (lakukan hanya bila raw tier masih > 1 detik setelah Langkah 4).** Saat ini fase 2 (`MapView.jsx`) mengambil seluruh sisa riwayat dalam satu request lalu menggabungkannya. Ganti menjadi beberapa request per hari yang berjalan paralel (maks 3), masing-masing ter-cache sendiri di server:

```js
const chunks = [];
for (let t = startMs; t < splitMs; t += 86400000) chunks.push([t, Math.min(t + 86400000, splitMs)]);
const parts = [decoded1];
const worker = async () => {
  while (chunks.length) {
    const [a, b] = chunks.pop();                    // terbaru dulu
    const pp = new URLSearchParams(params);
    pp.set('start', formatInput(a)); pp.set('end', formatInput(b));
    const r = await fetch(`/api/hotspots?${pp}`, { signal: ctrl.signal });
    if (r.ok) parts.push(decodeHotspots(await r.arrayBuffer()));
  }
};
await Promise.all([worker(), worker(), worker()]);
if (seq === seqRef.current) {
  const merged = mergeMany(parts);                  // alokasi sekali (lihat di bawah)
  setDataset({ decoded: merged, geom: buildGeometry(merged) });
}
```

Tambahkan `mergeMany(parts)` di `lib/hotspotCodec.js`: hitung total `n`, alokasikan semua TypedArray satu kali, lalu salin tiap bagian (sama seperti `mergeHotspotDatasets` tetapi untuk N bagian; jangan `reduce` berulang karena menyalin O(N²)). Naikkan `RAW_LIMIT` per chunk hanya bila perlu; dengan chunk harian, 60 ribu per hari jarang tercapai, tetapi batasi total titik di klien (misal 400 ribu) agar GPU tidak kewalahan.

## Langkah 6 — Frontend: pisahkan warna bara dari warna titik

Dampak: kecil sampai menengah (puluhan ms untuk ratusan ribu titik), jadi kerjakan setelah Langkah 1–4. Masalahnya: `buildColors` menghitung ulang kepadatan spasial (`spatialFreq`, satu lintasan atas semua titik) setiap `timeBounds` berubah, misalnya saat Anda klik tanggal di legenda, padahal kepadatan tidak bergantung pada waktu.

Di `components/MapView.jsx`, pecah `buildColors` menjadi dua fungsi:

```js
// Bergantung hanya pada dataset
function buildGlowColors(ds) {
  const { n, cnt, bright, header, lonlat } = ds.decoded;
  const isGrid = header.tier === 'grid';
  const glow = new Uint8Array(n * 4);
  const step = 0.015;
  const key = (x, y) => (Math.floor(x / step) + 20000) * 40000 + (Math.floor(y / step) + 20000); // tanpa tabrakan hash

  let freq = null;
  if (!isGrid) {
    freq = new Map();
    for (let i = 0; i < n; i++) {
      const k = key(lonlat[i * 2], lonlat[i * 2 + 1]);
      freq.set(k, (freq.get(k) || 0) + 1);
    }
  }
  for (let i = 0; i < n; i++) {
    let ratio, a;
    if (isGrid) {
      ratio = clamp(Math.log2(cnt ? (cnt[i] || 1) : 1) / 6, 0, 1);
      a = Math.min(255, 52 + Math.round(ratio * 185));
    } else {
      const f = freq.get(key(lonlat[i * 2], lonlat[i * 2 + 1])) || 1;
      const bonus = clamp(((bright ? bright[i] || 310 : 310) - 300) / 100, 0, 0.25);
      ratio = clamp(Math.log2(f) / 5 + bonus, 0, 1);
      a = Math.min(255, 45 + Math.round(ratio * 175));
    }
    const rgb = getRgbForDensity(ratio);
    const o = i * 4;
    glow[o] = rgb[0]; glow[o + 1] = rgb[1]; glow[o + 2] = rgb[2]; glow[o + 3] = a;
  }
  return glow;
}

// Bergantung pada dataset + rentang jam
function buildDotColors(ds, minHour, maxHour) {
  const { n, header, tRel } = ds.decoded;
  const t0 = header.t0Min;
  const span = Math.max(1, maxHour - minHour);
  const dots = new Uint8Array(n * 4);
  const cache = new Map();
  for (let i = 0; i < n; i++) {
    const h = Math.floor((t0 + tRel[i]) / 60);
    let rgb = cache.get(h);
    if (!rgb) { rgb = getRgbForRatio(clamp((h - minHour) / span, 0, 1)); cache.set(h, rgb); }
    const o = i * 4;
    dots[o] = rgb[0]; dots[o + 1] = rgb[1]; dots[o + 2] = rgb[2]; dots[o + 3] = 255;
  }
  return dots;
}
```

Lalu ganti `colors` di komponen:

```js
const glowColors = useMemo(() => (dataset ? buildGlowColors(dataset) : null), [dataset]);
const dotColors  = useMemo(
  () => (dataset ? buildDotColors(dataset, timeBounds.minHour, timeBounds.maxHour) : null),
  [dataset, timeBounds.minHour, timeBounds.maxHour]
);
// glowData memakai getFillColor: { value: glowColors, ... }
// dotData  memakai getFillColor: { value: dotColors,  ... }
// dependency useMemo glowData/dotData diganti dari `colors` menjadi glowColors / dotColors
```

Catatan: hash lama `(gx*73856093) ^ (gy*19349663)` bisa bertabrakan sehingga dua sel berbeda dihitung sebagai satu; kunci numerik di atas deterministik dan unik untuk rentang koordinat Anda.

Perbaikan kecil lain: bila filter tidak berubah tetapi peta digeser keluar bbox, `load()` memakai padding 35%. Bila geser kecil masih sering memicu reload, naikkan padding ke 0.5 (setara versi lama) dan kuantisasi bbox 0.1° sudah menjaga cache server tetap kena.

## Langkah 7 — Verifikasi, rollback, checklist

Isi kolom "Sebelum" dari Langkah 0, lalu ukur ulang setelah tiap langkah (rentang 1 bulan, cache dingin: restart server atau tambah parameter acak).

| Endpoint / skenario | Sebelum | Target | Sesudah |
| --- | --- | --- | --- |
| `/api/hotspots/summary` 30 hari |  | < 300 ms |  |
| `/api/hotspots?tier=grid` 30 hari |  | < 300 ms |  |
| `/api/hotspots?tier=raw` 3 hari, 1 viewport |  | < 600 ms |  |
| `/api/hotspots?tier=raw` 30 hari, 1 viewport |  | < 1,5 detik |  |
| Load awal sampai titik muncul |  | < 2 detik |  |

**Rollback.** Set `USE_ROLLUP=0` dan restart server: seluruh query kembali ke jalur lama. Tabel baru boleh dibiarkan; ia tidak dibaca oleh kode lama.

**Risiko yang perlu dipantau:**

- Data telat datang di luar jendela refresh (3 hari untuk `fire_points`, 96 jam untuk rollup). Jalankan refresh penuh setelah backfill.
- `fire_points` menghitung hotspot FIRMS dan SIPONGI terpisah (seperti sekarang). Dedup lintas sumber belum dilakukan; bila diinginkan, itu perubahan terpisah dengan aturan jarak/waktu.
- Ukuran tabel: perkirakan `fire_points` ±150–250 byte per baris termasuk index.

**Checklist:**

- [ ] Langkah 0: baseline tercatat
- [ ] Langkah 1: `next build` + `next start`, cache key dinormalisasi, summary ditunda
- [ ] Langkah 2: `fire_points` terisi, jumlah sesuai sumber
- [ ] Langkah 3: `fire_hourly_grid` terisi, job refresh 30 menit aktif
- [ ] Langkah 4: `hotspotStore.js` memakai jalur baru, hasil summary lama = baru
- [ ] Langkah 5: cache berbatas byte, TTL menurut kesegaran (chunk harian bila perlu)
- [ ] Langkah 6: warna bara dipisah dari warna titik
- [ ] Langkah 7: tabel sebelum/sesudah terisi
