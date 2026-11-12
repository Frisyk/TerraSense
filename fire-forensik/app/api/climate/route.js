import { NextResponse } from 'next/server';
import { getPool } from '../../../lib/db';

export const dynamic = 'force-dynamic';

const COMPASS_ID = ['U', 'TL', 'T', 'TG', 'S', 'BD', 'B', 'BL'];
function compassDir(deg) {
  const ix = Math.round((((deg % 360) + 360) % 360) / 45) % 8;
  return COMPASS_ID[ix];
}

function getCache() {
  if (!globalThis.__forensikClimateCache) {
    globalThis.__forensikClimateCache = new Map();
  }
  return globalThis.__forensikClimateCache;
}

/**
 * Menghitung komponen FWI (Van Wagner 1985) secara berurutan untuk deret jam/hari.
 */
function computeFwiSingle(temp, rh, windKmh, rain24h, ffmcPrev = 85.0, dmcPrev = 20.0, dcPrev = 200.0) {
  const safeRh = Math.max(1, Math.min(100, Number(rh) || 75));
  const safeWind = Math.max(0, Number(windKmh) || 5);
  const safeRain = Math.max(0, Number(rain24h) || 0);
  const safeTemp = Number.isFinite(Number(temp)) ? Number(temp) : 27;

  // 1. FFMC
  let m0 = (147.2 * (101.0 - ffmcPrev)) / (59.5 + ffmcPrev);
  if (safeRain > 0.5) {
    const rf = safeRain - 0.5;
    let mr =
      m0 +
      42.5 * rf * Math.exp(-100.0 / (251.0 - m0)) * (1.0 - Math.exp(-6.93 / rf));
    if (m0 > 150.0) {
      mr += 0.0015 * Math.pow(m0 - 150.0, 2) * Math.sqrt(rf);
    }
    m0 = Math.min(250.0, mr);
  }

  const ed =
    0.942 * Math.pow(safeRh, 0.679) +
    11.0 * Math.exp((safeRh - 100.0) / 10.0) +
    0.18 * (21.1 - safeTemp) * (1.0 - Math.exp(-0.115 * safeRh));

  let m = m0;
  if (m0 > ed) {
    const k0 =
      0.424 * (1.0 - Math.pow(safeRh / 100.0, 1.7)) +
      0.0694 * Math.sqrt(safeWind) * (1.0 - Math.pow(safeRh / 100.0, 8));
    const kd = k0 * 0.581 * Math.exp(0.0365 * safeTemp);
    m = ed + (m0 - ed) * Math.pow(10.0, -kd);
  } else {
    const ew =
      0.618 * Math.pow(safeRh, 0.753) +
      10.0 * Math.exp((safeRh - 100.0) / 10.0) +
      0.18 * (21.1 - safeTemp) * (1.0 - Math.exp(-0.115 * safeRh));
    if (m0 < ew) {
      const kl =
        0.424 * (1.0 - Math.pow((100.0 - safeRh) / 100.0, 1.7)) +
        0.0694 * Math.sqrt(safeWind) * (1.0 - Math.pow((100.0 - safeRh) / 100.0, 8));
      const kw = kl * 0.581 * Math.exp(0.0365 * safeTemp);
      m = ew - (ew - m0) * Math.pow(10.0, -kw);
    }
  }
  const ffmc = Math.max(0, Math.min(101, (59.5 * (250.0 - m)) / (147.2 + m)));

  // 2. DMC
  let p0 = dmcPrev;
  if (safeRain > 1.5) {
    const ra = 0.92 * safeRain - 1.27;
    const w0 = 20.0 + Math.exp(5.634 - p0 / 43.43);
    const b =
      p0 <= 33.0
        ? 100.0 / (0.5 + 0.3 * p0)
        : p0 <= 65.0
        ? 14.0 - 1.3 * Math.log(p0)
        : 6.2 * Math.log(p0) - 17.2;
    const wr = w0 + (1000.0 * ra) / (48.77 + b * ra);
    const pr = 244.72 - 43.43 * Math.log(Math.max(0.001, wr - 20.0));
    p0 = Math.max(0.0, pr);
  }
  const kDmc = safeTemp > -1.1 ? 1.894 * (safeTemp + 1.1) * (100.0 - safeRh) * 9.0 * 1e-6 : 0.0;
  const dmc = Math.max(0.0, p0 + 100.0 * kDmc);

  // 3. DC
  let d0 = dcPrev;
  if (safeRain > 2.8) {
    const rd = 0.83 * safeRain - 1.27;
    const q0 = 800.0 * Math.exp(-d0 / 400.0);
    const qr = q0 + 3.937 * rd;
    const dr = 400.0 * Math.log(800.0 / Math.max(0.001, qr));
    d0 = Math.max(0.0, dr);
  }
  const v = safeTemp > -2.8 ? 0.36 * (safeTemp + 2.8) + 1.39 : 1.39;
  const dc = Math.max(0.0, d0 + 0.5 * Math.max(0.0, v));

  // 4. ISI
  const fw = Math.exp(0.05039 * safeWind);
  const mIsi = (147.2 * (101.0 - ffmc)) / (59.5 + ffmc);
  const ff = 91.9 * Math.exp(-0.1386 * mIsi) * (1.0 + Math.pow(mIsi, 5.31) / 4.93e7);
  const isi = 0.208 * fw * ff;

  // 5. BUI
  let bui = 0;
  if (dmc + 0.4 * dc > 0) {
    if (dmc <= 0.4 * dc) {
      bui = (0.8 * dmc * dc) / (dmc + 0.4 * dc);
    } else {
      bui = dmc - (1.0 - (0.8 * dc) / (dmc + 0.4 * dc)) * (0.92 + Math.pow(0.0114 * dmc, 1.7));
    }
  }
  bui = Math.max(0.0, bui);

  // 6. FWI
  const fd = bui <= 80.0 ? 0.626 * Math.pow(bui, 0.809) + 2.0 : 1000.0 / (25.0 + 108.64 * Math.exp(-0.023 * bui));
  const bb = 0.1 * isi * fd;
  const fwi = bb > 1.0 ? Math.exp(2.72 * Math.pow(0.434 * Math.log(bb), 0.647)) : bb;

  const fwiCat = fwi < 2 ? 'Low' : fwi < 7 ? 'Moderate' : fwi < 13 ? 'High' : 'Extreme';

  return {
    ffmc: Number(ffmc.toFixed(1)),
    dmc: Number(dmc.toFixed(1)),
    dc: Number(dc.toFixed(1)),
    isi: Number(isi.toFixed(1)),
    bui: Number(bui.toFixed(1)),
    fwi: Number(fwi.toFixed(1)),
    fwiCategory: fwiCat,
  };
}

export async function GET(request) {
  const sp = new URL(request.url).searchParams;
  const lat = parseFloat(sp.get('lat') || '-0.5');
  const lon = parseFloat(sp.get('lon') || '110.4');
  const start = sp.get('start') || '';
  const end = sp.get('end') || '';

  if (!start || !end) {
    return NextResponse.json({ error: 'Parameter start dan end diperlukan' }, { status: 400 });
  }

  // Bulatkan ke sel 0.25° terdekat (resolusi native ERA5 Open-Meteo)
  const snapLat = Math.round(lat / 0.25) * 0.25;
  const snapLon = Math.round(lon / 0.25) * 0.25;
  const cacheKey = `${snapLat.toFixed(2)}_${snapLon.toFixed(2)}_${start}_${end}`;

  const cache = getCache();
  const cached = cache.get(cacheKey);
  if (cached && Date.now() - cached.ts < 300_000) {
    return NextResponse.json(cached.data);
  }

  // 1. Coba cek di tabel monitoring.climate_history terlebih dahulu
  try {
    const client = await getPool().connect();
    try {
      const checkTbl = await client.query(
        `SELECT to_regclass('monitoring.climate_history') IS NOT NULL AS exists`
      );
      if (checkTbl.rows[0]?.exists) {
        const dbRes = await client.query(
          `SELECT EXTRACT(EPOCH FROM time_utc) * 1000 AS t_ms,
                  lat, lon,
                  temp, humidity, pressure, wind_speed, wind_deg, wind_to_deg,
                  wind_compass, wind_u, wind_v, wind_gust, cloud_cover_percent AS clouds,
                  precipitation_mm AS precip, rain_24h_mm AS rain24h,
                  weather_code_description AS weather_desc,
                  fire_weather_index AS fwi, fwi_category,
                  drought_code AS dc, initial_spread_index AS isi,
                  fine_fuel_moisture_code AS ffmc
           FROM monitoring.climate_history
           WHERE ABS(lat - $1) <= 0.55 AND ABS(lon - $2) <= 0.55
             AND time_utc >= $3::timestamptz
             AND time_utc <= ($4::timestamptz + interval '1 day')
           ORDER BY time_utc ASC`,
          [snapLat, snapLon, start, end]
        );
        if (dbRes.rows.length > 0) {
          const byTime = new Map();
          for (const r of dbRes.rows) {
            const tKey = Number(r.t_ms);
            const rLat = Number(r.lat);
            const rLon = Number(r.lon);
            const dist2 = (rLat - snapLat) ** 2 + (rLon - snapLon) ** 2;
            const gp = {
              lon: rLon,
              lat: rLat,
              u: Number(r.wind_u),
              v: Number(r.wind_v),
              wind_speed: Number(r.wind_speed),
            };
            const existing = byTime.get(tKey);
            if (!existing) {
              byTime.set(tKey, { bestRow: r, bestDist2: dist2, gridPoints: [gp] });
            } else {
              existing.gridPoints.push(gp);
              if (dist2 < existing.bestDist2) {
                existing.bestRow = r;
                existing.bestDist2 = dist2;
              }
            }
          }

          const hourly = Array.from(byTime.entries()).map(([tMs, entry]) => {
            const r = entry.bestRow;
            return {
              t: tMs,
              temp: Number(r.temp),
              rh: Number(r.humidity),
              pressure: Number(r.pressure),
              ws: Number(r.wind_speed),
              wdeg: Number(r.wind_deg),
              wto: Number(r.wind_to_deg),
              wdir: r.wind_compass || compassDir(Number(r.wind_deg)),
              wu: Number(r.wind_u),
              wv: Number(r.wind_v),
              gust: Number(r.wind_gust),
              clouds: Number(r.clouds),
              precip: Number(r.precip || 0),
              rain24h: Number(r.rain24h || 0),
              desc: r.weather_desc || 'berawan',
              fwi: Number(r.fwi || 0),
              fwiCat: r.fwi_category || 'Moderate',
              dc: Number(r.dc || 200),
              isi: Number(r.isi || 3),
              ffmc: Number(r.ffmc || 85),
              gridPoints: entry.gridPoints,
            };
          });

          const payload = {
            source: 'Database (monitoring.climate_history)',
            resolution: '0.25° (~27.5 km ERA5)',
            gridLat: snapLat,
            gridLon: snapLon,
            hourly,
          };
          cache.set(cacheKey, { ts: Date.now(), data: payload });
          return NextResponse.json(payload);
        }
      }
    } finally {
      client.release();
    }
  } catch {
    // Lanjut ke fallback Open-Meteo Archive API
  }

  // 2. Fallback langsung ke Open-Meteo Archive API
  try {
    // Pastikan tanggal tidak di masa depan bagi Archive API (jika tahun simulasi 2026, petakan atau gunakan Forecast/Archive yang sesuai)
    const todayStr = new Date().toISOString().slice(0, 10);
    const safeEnd = end > todayStr ? todayStr : end;
    const safeStart = start > safeEnd ? safeEnd : start;

    const url = new URL('https://archive-api.open-meteo.com/v1/archive');
    url.searchParams.set('latitude', String(snapLat));
    url.searchParams.set('longitude', String(snapLon));
    url.searchParams.set('start_date', safeStart);
    url.searchParams.set('end_date', safeEnd);
    url.searchParams.set(
      'hourly',
      'temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover,precipitation,weather_code'
    );
    url.searchParams.set('wind_speed_unit', 'ms');
    url.searchParams.set('timezone', 'UTC');

    const res = await fetch(url.toString(), { next: { revalidate: 3600 } });
    if (!res.ok) throw new Error(`Open-Meteo HTTP ${res.status}`);
    const raw = await res.json();
    const h = raw.hourly || {};
    const times = h.time || [];

    let ffmc = 85.0,
      dmc = 20.0,
      dc = 200.0;
    const hourly = [];

    for (let i = 0; i < times.length; i++) {
      const tMs = Date.parse(`${times[i]}Z`);
      const temp = h.temperature_2m?.[i] ?? 27.5;
      const rh = h.relative_humidity_2m?.[i] ?? 74;
      const pressure = Math.round(h.surface_pressure?.[i] ?? 1010);
      const ws = Number((h.wind_speed_10m?.[i] ?? 2.4).toFixed(2));
      const wdeg = Math.round(h.wind_direction_10m?.[i] ?? 135);
      const wto = (wdeg + 180) % 360;
      const radTo = (wto * Math.PI) / 180;
      const wu = Number((ws * Math.sin(radTo)).toFixed(3));
      const wv = Number((ws * Math.cos(radTo)).toFixed(3));
      const gust = Number((h.wind_gusts_10m?.[i] ?? ws * 1.4).toFixed(2));
      const clouds = Math.round(h.cloud_cover?.[i] ?? 45);
      const precip = Number((h.precipitation?.[i] ?? 0).toFixed(2));

      let rain24h = 0;
      for (let j = Math.max(0, i - 23); j <= i; j++) {
        rain24h += h.precipitation?.[j] ?? 0;
      }
      rain24h = Number(rain24h.toFixed(2));

      const fwiObj = computeFwiSingle(temp, rh, ws * 3.6, rain24h, ffmc, dmc, dc);
      if (i % 24 === 6) {
        ffmc = fwiObj.ffmc;
        dmc = fwiObj.dmc;
        dc = fwiObj.dc;
      }

      hourly.push({
        t: tMs,
        temp: Number(temp.toFixed(1)),
        rh: Math.round(rh),
        pressure,
        ws,
        wdeg,
        wto,
        wdir: compassDir(wdeg),
        wu,
        wv,
        gust,
        clouds,
        precip,
        rain24h,
        desc: precip > 0.5 ? 'hujan ringan' : clouds > 75 ? 'mendung' : 'cerah berawan',
        fwi: fwiObj.fwi,
        fwiCat: fwiObj.fwiCategory,
        dc: fwiObj.dc,
        isi: fwiObj.isi,
        ffmc: fwiObj.ffmc,
      });
    }

    const payload = {
      source: 'Open-Meteo ERA5/IFS Archive',
      resolution: '0.1°–0.25° (~9–27 km)',
      gridLat: snapLat,
      gridLon: snapLon,
      hourly,
    };
    cache.set(cacheKey, { ts: Date.now(), data: payload });
    return NextResponse.json(payload);
  } catch (err) {
    return NextResponse.json(
      { error: 'Gagal memuat data cuaca Open-Meteo', details: err.message },
      { status: 502 }
    );
  }
}

