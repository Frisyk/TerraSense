/**
 * Mesin Forensik Hotspot VIIRS Kalimantan Barat
 * Porting lengkap dari scripts/forensik.py & Jejak Hotspot Kalbar (1).html
 */

export const H = 3600e3;
export const MIN = 60e3;
export const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'];
export const COMP = ['U', 'TL', 'T', 'TG', 'S', 'BD', 'B', 'BL'];
export const BUF = 190;
export const PIXEL_M = 375.0;
export const HALF_PIXEL = PIXEL_M / 2;
export const BBOX_KALBAR = [108.5, -3.2, 114.3, 2.3]; // [lon_min, lat_min, lon_max, lat_max]

/* ---------- Format & Waktu (WIB = UTC+7) ---------- */
export function wib(t) {
  return new Date(t + 7 * H);
}

export function two(n) {
  return (n < 10 ? '0' : '') + n;
}

export function fmt(t) {
  const d = wib(t);
  return (
    d.getUTCDate() +
    ' ' +
    MON[d.getUTCMonth()] +
    ' ' +
    two(d.getUTCHours()) +
    ':' +
    two(d.getUTCMinutes())
  );
}

export function fmtY(t) {
  const d = wib(t);
  return d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear();
}

export function dayKey(t) {
  const d = wib(t);
  return d.getUTCDate() + ' ' + MON[d.getUTCMonth()];
}

export function num(v, dg) {
  return Number(v || 0).toLocaleString('id-ID', {
    maximumFractionDigits: dg == null ? 0 : dg,
    minimumFractionDigits: dg == null ? 0 : dg,
  });
}

export function bearing(dx, dy) {
  const b = ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
  return { deg: b, dir: COMP[Math.round(b / 45) % 8] };
}

export function llText(ll) {
  if (!ll || ll.length < 2) return '-';
  return (
    Math.abs(ll[0]).toFixed(4) +
    '° ' +
    (ll[0] < 0 ? 'LS' : 'LU') +
    ', ' +
    ll[1].toFixed(4) +
    '° BT'
  );
}

/* ---------- PRNG Seeded (Mulberry32) ---------- */
export function mulberry(seed) {
  let a = seed | 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ---------- Proyeksi WGS84 (EPSG:4326) <-> UTM 49N (EPSG:32649) ---------- */
const WGS84_A = 6378137.0;
const WGS84_F = 1 / 298.257223563;
const WGS84_E2 = 2 * WGS84_F - WGS84_F * WGS84_F;
const WGS84_EP2 = WGS84_E2 / (1 - WGS84_E2);
const UTM_K0 = 0.9996;
const UTM49_LON0 = (111 * Math.PI) / 180; // Central meridian Zone 49 = 111°E

export function lonLatToUTM49N(lonDeg, latDeg) {
  const lat = (latDeg * Math.PI) / 180;
  const lon = (lonDeg * Math.PI) / 180;
  const sinLat = Math.sin(lat);
  const cosLat = Math.cos(lat);
  const tanLat = Math.tan(lat);

  const N = WGS84_A / Math.sqrt(1 - WGS84_E2 * sinLat * sinLat);
  const T = tanLat * tanLat;
  const C = WGS84_EP2 * cosLat * cosLat;
  const A = cosLat * (lon - UTM49_LON0);

  const e2 = WGS84_E2;
  const e4 = e2 * e2;
  const e6 = e4 * e2;
  const M =
    WGS84_A *
    ((1 - e2 / 4 - (3 * e4) / 64 - (5 * e6) / 256) * lat -
      ((3 * e2) / 8 + (3 * e4) / 32 + (45 * e6) / 1024) * Math.sin(2 * lat) +
      ((15 * e4) / 256 + (45 * e6) / 1024) * Math.sin(4 * lat) -
      ((35 * e6) / 3072) * Math.sin(6 * lat));

  const x =
    500000.0 +
    UTM_K0 *
      N *
      (A +
        ((1 - T + C) * A * A * A) / 6 +
        ((5 - 18 * T + T * T + 72 * C - 58 * WGS84_EP2) * Math.pow(A, 5)) / 120);

  const y =
    UTM_K0 *
    (M +
      N *
        tanLat *
        ((A * A) / 2 +
          ((5 - T + 9 * C + 4 * C * C) * Math.pow(A, 4)) / 24 +
          ((61 - 58 * T + T * T + 600 * C - 330 * WGS84_EP2) * Math.pow(A, 6)) / 720));

  return [x, y];
}

export function utm49NToLonLat(x, y) {
  const e2 = WGS84_E2;
  const e4 = e2 * e2;
  const e6 = e4 * e2;
  const e1 = (1 - Math.sqrt(1 - e2)) / (1 + Math.sqrt(1 - e2));

  const M = y / UTM_K0;
  const mu = M / (WGS84_A * (1 - e2 / 4 - (3 * e4) / 64 - (5 * e6) / 256));

  const phi1 =
    mu +
    ((3 * e1) / 2 - (27 * Math.pow(e1, 3)) / 32) * Math.sin(2 * mu) +
    ((21 * e1 * e1) / 16 - (55 * Math.pow(e1, 4)) / 32) * Math.sin(4 * mu) +
    ((151 * Math.pow(e1, 3)) / 96) * Math.sin(6 * mu) +
    ((1097 * Math.pow(e1, 4)) / 512) * Math.sin(8 * mu);

  const sinPhi1 = Math.sin(phi1);
  const cosPhi1 = Math.cos(phi1);
  const tanPhi1 = Math.tan(phi1);

  const N1 = WGS84_A / Math.sqrt(1 - e2 * sinPhi1 * sinPhi1);
  const T1 = tanPhi1 * tanPhi1;
  const C1 = WGS84_EP2 * cosPhi1 * cosPhi1;
  const R1 = (WGS84_A * (1 - e2)) / Math.pow(1 - e2 * sinPhi1 * sinPhi1, 1.5);
  const D = (x - 500000.0) / (N1 * UTM_K0);

  const lat =
    phi1 -
    ((N1 * tanPhi1) / R1) *
      ((D * D) / 2 -
        ((5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * WGS84_EP2) * Math.pow(D, 4)) / 24 +
        ((61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * WGS84_EP2 - 3 * C1 * C1) *
          Math.pow(D, 6)) /
          720);

  const lon =
    UTM49_LON0 +
    (D -
      ((1 + 2 * T1 + C1) * Math.pow(D, 3)) / 6 +
      ((5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * WGS84_EP2 + 24 * T1 * T1) * Math.pow(D, 5)) /
        120) /
      cosPhi1;

  return [(lon * 180) / Math.PI, (lat * 180) / Math.PI];
}

/* ---------- Normalisasi Dataset untuk UI ---------- */
// Format baris titik dari skrip: [x, y, t_ms, pass, sat_idx, frp, conf(0/1/2), parent_idx]
export function prepCluster(rc, sats) {
  const pts = rc.pts.map((a, i) => ({
    i,
    x: a[0],
    y: a[1],
    t: a[2],
    pass: a[3],
    sat: sats[a[4]] || '?',
    frp: a[5],
    conf: a[6] === 2 ? 'h' : a[6] === 0 ? 'l' : 'n',
  }));
  const passIds = [];
  const seen = {};
  pts.forEach((p) => {
    if (!seen[p.pass]) {
      seen[p.pass] = 1;
      passIds.push(p.pass);
    }
  });
  passIds.sort((a, b) => a - b);
  const stepOf = {};
  const steps = passIds.map((id, k) => {
    stepOf[id] = k;
    return { pass: id, t: Infinity, sat: '', n: 0, day: '' };
  });
  pts.forEach((p) => {
    p.step = stepOf[p.pass];
    const s = steps[p.step];
    s.n++;
    if (p.t < s.t) {
      s.t = p.t;
      s.sat = p.sat;
    }
  });
  steps.forEach((s) => {
    s.day = dayKey(s.t);
  });
  return {
    id: rc.id,
    n: rc.n,
    pts,
    parent: rc.pts.map((a) => a[7]),
    root: rc.root,
    steps,
    firstCount: rc.firstCount,
    stabRoot: rc.stabRoot,
    stabEdge: rc.stabEdge,
    rootLL: rc.rootLL,
    peat: !!rc.peat,
    t0: rc.t0,
    t1: rc.t1,
    areaHa: rc.areaHa,
  };
}

export function prepDataset(payload) {
  const sats = payload.meta?.sats || [];
  return {
    meta: payload.meta || {},
    clusters: (payload.clusters || []).map((c) => prepCluster(c, sats)),
    context: payload.context || [],
    rawPayload: payload,
  };
}

/* ---------- Delaunay Triangulation & Alpha Shape (Bowyer-Watson) ---------- */
export function delaunay(P) {
  const n = P.length;
  if (n < 3) return [];
  let minx = Infinity,
    maxx = -Infinity,
    miny = Infinity,
    maxy = -Infinity;
  for (let i = 0; i < n; i++) {
    const p = P[i];
    if (p.x < minx) minx = p.x;
    if (p.x > maxx) maxx = p.x;
    if (p.y < miny) miny = p.y;
    if (p.y > maxy) maxy = p.y;
  }
  const dm = Math.max(maxx - minx, maxy - miny) || 1;
  const mx = (minx + maxx) / 2;
  const my = (miny + maxy) / 2;
  const V = P.map((p) => [p.x, p.y]);
  V.push([mx - 20 * dm, my - dm], [mx, my + 20 * dm], [mx + 20 * dm, my - dm]);

  function mk(a, b, c) {
    const A = V[a],
      B = V[b],
      C = V[c];
    const d = 2 * (A[0] * (B[1] - C[1]) + B[0] * (C[1] - A[1]) + C[0] * (A[1] - B[1]));
    if (Math.abs(d) < 1e-9) return null;
    const a2 = A[0] * A[0] + A[1] * A[1];
    const b2 = B[0] * B[0] + B[1] * B[1];
    const c2 = C[0] * C[0] + C[1] * C[1];
    const ux = (a2 * (B[1] - C[1]) + b2 * (C[1] - A[1]) + c2 * (A[1] - B[1])) / d;
    const uy = (a2 * (C[0] - B[0]) + b2 * (A[0] - C[0]) + c2 * (B[0] - A[0])) / d;
    return {
      a,
      b,
      c,
      ux,
      uy,
      r2: (A[0] - ux) * (A[0] - ux) + (A[1] - uy) * (A[1] - uy),
    };
  }

  let tris = [mk(n, n + 1, n + 2)].filter(Boolean);
  for (let i = 0; i < n; i++) {
    const px = V[i][0],
      py = V[i][1];
    const keep = [];
    const edges = new Map();
    for (let j = 0; j < tris.length; j++) {
      const t = tris[j];
      if ((px - t.ux) * (px - t.ux) + (py - t.uy) * (py - t.uy) < t.r2) {
        const triEdges = [
          [t.a, t.b],
          [t.b, t.c],
          [t.c, t.a],
        ];
        for (let k = 0; k < 3; k++) {
          const e = triEdges[k];
          const key = e[0] < e[1] ? e[0] + '_' + e[1] : e[1] + '_' + e[0];
          if (edges.has(key)) edges.delete(key);
          else edges.set(key, e);
        }
      } else {
        keep.push(t);
      }
    }
    tris = keep;
    edges.forEach((e) => {
      const t = mk(e[0], e[1], i);
      if (t) tris.push(t);
    });
  }
  return tris.filter((t) => t.a < n && t.b < n && t.c < n);
}

export function computeGeom(vis, alphaM = 1300) {
  const kept = delaunay(vis).filter((t) => Math.sqrt(t.r2) <= alphaM);
  const cnt = new Map();
  const inTri = {};
  let area = 0;
  kept.forEach((t) => {
    const A = vis[t.a],
      B = vis[t.b],
      C = vis[t.c];
    area += Math.abs((B.x - A.x) * (C.y - A.y) - (C.x - A.x) * (B.y - A.y)) / 2;
    inTri[t.a] = inTri[t.b] = inTri[t.c] = 1;
    [
      [t.a, t.b],
      [t.b, t.c],
      [t.c, t.a],
    ].forEach((e) => {
      const kk = e[0] < e[1] ? e[0] + '_' + e[1] : e[1] + '_' + e[0];
      cnt.set(kk, (cnt.get(kk) || 0) + 1);
    });
  });
  const edges = [];
  cnt.forEach((v, kk) => {
    if (v === 1) edges.push(kk.split('_').map(Number));
  });
  vis.forEach((p, i) => {
    if (!inTri[i]) area += Math.PI * BUF * BUF;
  });
  return { vis, tris: kept, edges, area };
}

/* ---------- Data Simulasi (Persis seperti HTML asli) ---------- */
export function buildDemo() {
  const rnd = mulberry(20260904);
  function gauss() {
    let u = 0;
    while (!u) u = rnd();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rnd());
  }
  const LAT0 = -0.62,
    LON0 = 110.28,
    T0 = Date.UTC(2026, 8, 3, 17, 0);
  const SATS = [
    ['SNPP', 1.4],
    ['NOAA-21', 1.8],
    ['NOAA-20', 2.2],
    ['SNPP', 13.3],
    ['NOAA-21', 13.7],
    ['NOAA-20', 14.1],
  ];
  const passes = [];
  for (let d = 0; d < 8; d++) {
    SATS.forEach((s) => {
      passes.push({ t: T0 + (d * 24 + s[1]) * H, sat: s[0] });
    });
  }
  const EVENTS = [
    { o: [0, 0], brg: 62, ign: T0 + 11 * H, v: 30, bf: 0.42, endH: 100, first: { n: 1, frp: 18 } },
    {
      o: [-5200, 4200],
      brg: 150,
      ign: T0 + (24 + 18) * H,
      v: 22,
      bf: 0.4,
      endH: 90,
      first: { n: 3, frp: 10.5 },
      peat: true,
    },
    { o: [7500, -4000], brg: 20, ign: T0 + (48 + 20) * H, v: 15, bf: 0.4, endH: 16, first: { n: 3, frp: 7 } },
  ];
  function ell(ev, h) {
    const a = 250 + ev.v * Math.min(h, ev.endH);
    return { a, b: a * ev.bf };
  }
  function inside(ev, e, x, y) {
    const th = (ev.brg * Math.PI) / 180,
      dx = Math.sin(th),
      dy = Math.cos(th),
      u = x * dx + y * dy,
      w = -x * dy + y * dx,
      c = e.a - 150;
    return Math.pow((u - c) / e.a, 2) + Math.pow(w / e.b, 2) <= 1;
  }
  const raw = [];
  EVENTS.forEach((ev, evi) => {
    let prev = null;
    passes.forEach((ps, pi) => {
      if (ps.t <= ev.ign) return;
      const h = (ps.t - ev.ign) / H;
      if (h > ev.endH + 6) return;
      const e = ell(ev, h),
        isFirst = prev === null;
      const area = Math.PI * e.a * e.b - (prev ? Math.PI * prev.a * prev.b : 0);
      let n = Math.round((area / 0.14e6) * 0.5);
      if (isFirst) n = ev.first.n;
      else if (n < 1 || rnd() < 0.2) return;
      const c = e.a - 150,
        th = (ev.brg * Math.PI) / 180,
        dx = Math.sin(th),
        dy = Math.cos(th);
      for (let i = 0, made = 0; made < n && i < 400; i++) {
        const u = c + (rnd() * 2 - 1) * e.a,
          w = (rnd() * 2 - 1) * e.b;
        let x = u * dx - w * dy,
          y = u * dy + w * dx;
        if (!inside(ev, e, x, y)) continue;
        if (prev && inside(ev, prev, x, y)) continue;
        if (isFirst) {
          x = ev.first.n === 1 ? 0 : x * 0.5;
          y = ev.first.n === 1 ? 0 : y * 0.5;
        }
        const frp = isFirst
          ? ev.first.frp + (ev.first.n > 1 ? (rnd() - 0.5) * 2.4 : 0)
          : 4 + 20 * rnd() * (0.4 + Math.max(0, u / (2 * e.a)));
        raw.push({
          x: ev.o[0] + x + gauss() * 60,
          y: ev.o[1] + y + gauss() * 60,
          t: ps.t + rnd() * 3 * MIN,
          pass: pi,
          sat: ps.sat,
          frp,
          ev: evi,
        });
        made++;
      }
      prev = e;
    });
  });

  function buildTree(pts, xs, ys, frp) {
    let minPass = Infinity;
    pts.forEach((p) => {
      if (p.pass < minPass) minPass = p.pass;
    });
    let root = -1,
      best = -1;
    pts.forEach((p, i) => {
      if (p.pass === minPass && frp[i] > best) {
        best = frp[i];
        root = i;
      }
    });
    const parent = pts.map((p, i) => {
      if (i === root) return -1;
      if (p.pass === minPass) return root;
      let bi = -1,
        bd = Infinity;
      for (let j = 0; j < pts.length; j++) {
        if (pts[j].pass < p.pass) {
          const dd = Math.hypot(xs[i] - xs[j], ys[i] - ys[j]);
          if (dd < bd) {
            bd = dd;
            bi = j;
          }
        }
      }
      return bi;
    });
    return { root, parent };
  }

  const rows = raw.slice();
  const par = rows.map((_, i) => i);
  function find(i) {
    while (par[i] !== i) {
      par[i] = par[par[i]];
      i = par[i];
    }
    return i;
  }
  for (let i = 0; i < rows.length; i++) {
    for (let j = i + 1; j < rows.length; j++) {
      if (
        Math.abs(rows[i].t - rows[j].t) <= 72 * H &&
        Math.hypot(rows[i].x - rows[j].x, rows[i].y - rows[j].y) <= 1800
      ) {
        par[find(i)] = find(j);
      }
    }
  }
  const groups = {};
  rows.forEach((p, i) => {
    (groups[find(i)] = groups[find(i)] || []).push(p);
  });
  const srnd = mulberry(77);
  const satList = ['SNPP', 'NOAA-20', 'NOAA-21'];
  const clusters = Object.keys(groups)
    .map((k) => groups[k])
    .map((pts) => {
      pts.sort((a, b) => a.pass - b.pass || a.t - b.t);
      const xs = pts.map((p) => p.x),
        ys = pts.map((p) => p.y),
        fr = pts.map((p) => p.frp);
      const tr = buildTree(pts, xs, ys, fr);
      let hit = 0,
        eh = 0,
        en = 0;
      const RUNS = 200;
      for (let r = 0; r < RUNS; r++) {
        const t2 = buildTree(
          pts,
          xs.map((v) => v + (srnd() * 2 - 1) * 187.5),
          ys.map((v) => v + (srnd() * 2 - 1) * 187.5),
          fr.map((v) => v * (1 + (srnd() - 0.5) * 0.4))
        );
        if (t2.root === tr.root) hit++;
        t2.parent.forEach((pp, i) => {
          if (i === t2.root || i === tr.root) return;
          en++;
          if (pp === tr.parent[i]) eh++;
        });
      }
      const minPass = pts[0].pass,
        fc = pts.filter((p) => p.pass === minPass).length,
        rt = pts[tr.root];
      const gArea = computeGeom(pts, 1300).area / 1e4;
      return {
        _t: rt.t,
        id: '',
        n: pts.length,
        root: tr.root,
        firstCount: fc,
        stabRoot: hit / RUNS,
        stabEdge: en ? eh / en : 1,
        rootLL: [LAT0 + rt.y / 111320, LON0 + rt.x / (111320 * Math.cos((LAT0 * Math.PI) / 180))],
        peat: pts[0].ev === 1,
        t0: pts[0].t,
        t1: pts[pts.length - 1].t,
        areaHa: Math.round(gArea * 10) / 10,
        pts: pts.map((p, i) => [
          Math.round(p.x),
          Math.round(p.y),
          Math.round(p.t),
          p.pass,
          satList.indexOf(p.sat),
          Math.round(p.frp * 10) / 10,
          1,
          tr.parent[i],
        ]),
      };
    });
  clusters.sort((a, b) => a._t - b._t);
  clusters.forEach((c, i) => {
    c.id = 'Contoh ' + String.fromCharCode(65 + i);
  });
  const [demoX0, demoY0] = lonLatToUTM49N(LON0, LAT0);
  return prepDataset({
    meta: {
      sats: satList,
      start: '2026-09-04',
      end: '2026-09-12',
      x0: demoX0,
      y0: demoY0,
      lat0: LAT0,
      lon0: LON0,
    },
    clusters,
    context: [],
  });
}

/* ---------- Web Mercator Tile Helpers & UTM Origin Resolver ---------- */
export function resolveDatasetOriginUTM(ds) {
  if (ds?.meta?.x0 != null && ds?.meta?.y0 != null) {
    return [Number(ds.meta.x0), Number(ds.meta.y0)];
  }
  if (ds?.clusters?.length > 0) {
    const c = ds.clusters[0];
    const rt = c.pts?.[c.root];
    if (rt && c.rootLL && c.rootLL.length >= 2) {
      const [ux, uy] = lonLatToUTM49N(c.rootLL[1], c.rootLL[0]);
      return [ux - rt.x, uy - rt.y];
    }
  }
  // Fallback titik tengah Kalbar (110.28° BT, 0.62° LS)
  return lonLatToUTM49N(110.28, -0.62);
}

export function lonToTileX(lon, z) {
  return Math.floor(((lon + 180) / 360) * Math.pow(2, z));
}

export function latToTileY(lat, z) {
  const clampedLat = Math.max(-85.0511, Math.min(85.0511, lat));
  const rad = (clampedLat * Math.PI) / 180;
  return Math.floor(
    ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * Math.pow(2, z)
  );
}

export function tileXToLon(tx, z) {
  return (tx / Math.pow(2, z)) * 360 - 180;
}

export function tileYToLat(ty, z) {
  const n = Math.PI - (2 * Math.PI * ty) / Math.pow(2, z);
  return (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
}

/* ==========================================================================
 * PIPELINE FORENSIK SERVER-SIDE (Porting identik dari scripts/forensik.py)
 * ========================================================================== */

function pickRoot(pas, frp) {
  let minPass = Infinity;
  for (let i = 0; i < pas.length; i++) {
    if (pas[i] < minPass) minPass = pas[i];
  }
  let bestIdx = 0;
  let bestFrp = -Infinity;
  for (let i = 0; i < pas.length; i++) {
    if (pas[i] === minPass && frp[i] > bestFrp) {
      bestFrp = frp[i];
      bestIdx = i;
    }
  }
  return bestIdx;
}

function buildParents(xs, ys, pas, root) {
  const n = pas.length;
  const parent = new Int32Array(n).fill(-1);
  let minPass = Infinity;
  for (let i = 0; i < n; i++) {
    if (pas[i] < minPass) minPass = pas[i];
  }
  for (let i = 0; i < n; i++) {
    if (i === root) {
      parent[i] = -1;
    } else if (pas[i] === minPass) {
      parent[i] = root;
    } else {
      let bestJ = -1;
      let bestD2 = Infinity;
      const xi = xs[i],
        yi = ys[i],
        pi = pas[i];
      for (let j = 0; j < n; j++) {
        if (pas[j] < pi) {
          const dx = xi - xs[j];
          const dy = yi - ys[j];
          const d2 = dx * dx + dy * dy;
          if (d2 < bestD2) {
            bestD2 = d2;
            bestJ = j;
          }
        }
      }
      parent[i] = bestJ;
    }
  }
  return parent;
}

function computeStability(xs, ys, pas, frp, root, parent, rng, runs = 200, edgeNMax = 1500) {
  const n = pas.length;
  let rootHit = 0;
  let edgeHit = 0;
  let edgeTot = 0;
  const doEdges = n <= edgeNMax && n > 1;
  const f2 = new Float64Array(n);
  const xs2 = new Float64Array(n);
  const ys2 = new Float64Array(n);

  for (let r = 0; r < runs; r++) {
    for (let i = 0; i < n; i++) {
      f2[i] = frp[i] * (1 + (rng() * 2 - 1) * 0.2);
    }
    const r2 = pickRoot(pas, f2);
    if (r2 === root) rootHit++;

    if (doEdges) {
      for (let i = 0; i < n; i++) {
        xs2[i] = xs[i] + (rng() * 2 - 1) * HALF_PIXEL;
        ys2[i] = ys[i] + (rng() * 2 - 1) * HALF_PIXEL;
      }
      const p2 = buildParents(xs2, ys2, pas, r2);
      for (let i = 0; i < n; i++) {
        if (i === root || i === r2) continue;
        edgeTot++;
        if (p2[i] === parent[i]) edgeHit++;
      }
    }
  }
  return {
    stabRoot: rootHit / runs,
    stabEdge: edgeTot > 0 ? edgeHit / edgeTot : doEdges ? 1.0 : null,
  };
}

/**
 * Menjalankan seluruh pipeline forensik pada baris mentah dari PostgreSQL.
 * @param {Array<{latitude:number, longitude:number, frp:number, confidence:string, satellite:string, t_ms:number}>} rawRows
 * @param {Object} opts
 */
export function runForensicPipeline(rawRows, opts = {}) {
  const {
    start = '',
    end = '',
    minConf = 'n',
    epsS = 1500,
    epsTH = 48,
    minPoints = 3,
    staticMonths = 4,
    staticDays = 15,
    runs = 200,
    edgeNMax = 1500,
    alpha = 1300,
    seed = 7,
  } = opts;

  // 1. Bersihkan, filter confidence & deduplikasi (latitude, longitude, t_ms, satellite)
  const dedup = new Set();
  const cleaned = [];
  for (let i = 0; i < rawRows.length; i++) {
    const r = rawRows[i];
    const lat = Number(r.latitude);
    const lon = Number(r.longitude);
    const tMs = Number(r.t_ms);
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(tMs)) continue;
    const conf = String(r.confidence || 'n')
      .trim()
      .toLowerCase()[0] || 'n';
    if (minConf === 'n' && conf !== 'n' && conf !== 'h') continue;
    if (minConf === 'h' && conf !== 'h') continue;

    const sat = String(r.satellite || 'VIIRS').trim();
    const key = `${lat.toFixed(5)}_${lon.toFixed(5)}_${tMs}_${sat}`;
    if (dedup.has(key)) continue;
    dedup.add(key);

    const [x, y] = lonLatToUTM49N(lon, lat);
    const dUtc = new Date(tMs);
    const ym = dUtc.getUTCFullYear() + '-' + two(dUtc.getUTCMonth() + 1);
    const ymd = ym + '-' + two(dUtc.getUTCDate());
    const cell = `${Math.floor(x / PIXEL_M)}_${Math.floor(y / PIXEL_M)}`;

    cleaned.push({
      lat,
      lon,
      x,
      y,
      tMs,
      frp: Number.isFinite(Number(r.frp)) ? Number(r.frp) : 1.0,
      conf,
      sat,
      ym,
      ymd,
      cell,
    });
  }

  if (cleaned.length === 0) {
    return {
      meta: {
        generated: new Date().toISOString(),
        crs: 'EPSG:32649',
        start,
        end,
        sats: [],
        pixel_m: PIXEL_M,
        n_points: 0,
        n_static_cells: 0,
      },
      clusters: [],
      context: [],
    };
  }

  // 2. Topeng sumber panas statis (flare, pabrik, dll)
  const cellStats = new Map();
  for (let i = 0; i < cleaned.length; i++) {
    const p = cleaned[i];
    let st = cellStats.get(p.cell);
    if (!st) {
      st = { months: new Set(), days: new Set() };
      cellStats.set(p.cell, st);
    }
    st.months.add(p.ym);
    st.days.add(p.ymd);
  }
  const staticCells = new Set();
  cellStats.forEach((st, cell) => {
    if (st.months.size >= staticMonths && st.days.size >= staticDays) {
      staticCells.add(cell);
    }
  });

  const filtered = cleaned.filter((p) => !staticCells.has(p.cell));
  if (filtered.length === 0) {
    return {
      meta: {
        generated: new Date().toISOString(),
        crs: 'EPSG:32649',
        start,
        end,
        sats: [],
        pixel_m: PIXEL_M,
        n_points: 0,
        n_static_cells: staticCells.size,
      },
      clusters: [],
      context: [],
    };
  }

  // 3. Penomoran lintasan satelit (assign_passes, gap <= 20 menit)
  filtered.sort((a, b) => (a.sat < b.sat ? -1 : a.sat > b.sat ? 1 : a.tMs - b.tMs));
  let passLocal = 0;
  const passMinTime = new Map();
  for (let i = 0; i < filtered.length; i++) {
    if (
      i === 0 ||
      filtered[i].sat !== filtered[i - 1].sat ||
      filtered[i].tMs - filtered[i - 1].tMs > 20 * MIN
    ) {
      passLocal++;
    }
    filtered[i].passLocal = passLocal;
    const prevMin = passMinTime.get(passLocal);
    if (prevMin === undefined || filtered[i].tMs < prevMin) {
      passMinTime.set(passLocal, filtered[i].tMs);
    }
  }
  const sortedPassLocals = Array.from(passMinTime.entries())
    .sort((a, b) => a[1] - b[1])
    .map((e) => e[0]);
  const passRank = new Map();
  sortedPassLocals.forEach((pl, idx) => passRank.set(pl, idx));
  for (let i = 0; i < filtered.length; i++) {
    filtered[i].passId = passRank.get(filtered[i].passLocal);
  }
  filtered.sort((a, b) => a.tMs - b.tMs);

  // 4. Klaster Spatio-Temporal dengan Spatial Grid + Union-Find
  const n = filtered.length;
  const parentUF = new Int32Array(n);
  for (let i = 0; i < n; i++) parentUF[i] = i;
  function findUF(i) {
    while (parentUF[i] !== i) {
      parentUF[i] = parentUF[parentUF[i]];
      i = parentUF[i];
    }
    return i;
  }
  function unionUF(a, b) {
    const ra = findUF(a);
    const rb = findUF(b);
    if (ra !== rb) parentUF[ra] = rb;
  }

  const grid = new Map();
  const cellSize = epsS;
  const epsS2 = epsS * epsS;
  const epsTMs = epsTH * H;

  for (let i = 0; i < n; i++) {
    const p = filtered[i];
    const gx = Math.floor(p.x / cellSize);
    const gy = Math.floor(p.y / cellSize);
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        const bucket = grid.get(`${gx + dx}_${gy + dy}`);
        if (!bucket) continue;
        for (let k = 0; k < bucket.length; k++) {
          const j = bucket[k];
          const q = filtered[j];
          if (Math.abs(p.tMs - q.tMs) <= epsTMs) {
            const ddx = p.x - q.x;
            const ddy = p.y - q.y;
            if (ddx * ddx + ddy * ddy <= epsS2) {
              unionUF(i, j);
            }
          }
        }
      }
    }
    const ownKey = `${gx}_${gy}`;
    let ownBucket = grid.get(ownKey);
    if (!ownBucket) {
      ownBucket = [];
      grid.set(ownKey, ownBucket);
    }
    ownBucket.push(i);
  }

  const groupsMap = new Map();
  for (let i = 0; i < n; i++) {
    const r = findUF(i);
    let grp = groupsMap.get(r);
    if (!grp) {
      grp = [];
      groupsMap.set(r, grp);
    }
    grp.push(filtered[i]);
  }

  // Titik pusat rata-rata (x0, y0)
  let sumX = 0,
    sumY = 0;
  for (let i = 0; i < n; i++) {
    sumX += filtered[i].x;
    sumY += filtered[i].y;
  }
  const x0 = sumX / n;
  const y0 = sumY / n;
  const [lon0, lat0] = utm49NToLonLat(x0, y0);

  const satsSet = new Set();
  for (let i = 0; i < n; i++) satsSet.add(filtered[i].sat);
  const sats = Array.from(satsSet).sort();
  const satIdxMap = new Map(sats.map((s, idx) => [s, idx]));
  const confIdxMap = { l: 0, n: 1, h: 2 };

  const bigGroups = [];
  const contextPts = [];
  groupsMap.forEach((grp) => {
    if (grp.length >= minPoints) {
      bigGroups.push(grp);
    } else {
      for (let i = 0; i < grp.length; i++) {
        contextPts.push([Math.round(grp[i].x - x0), Math.round(grp[i].y - y0)]);
      }
    }
  });

  // Urutkan klaster berdasarkan waktu deteksi paling awal
  bigGroups.sort((a, b) => {
    let minA = Infinity,
      minB = Infinity;
    for (let i = 0; i < a.length; i++) if (a[i].tMs < minA) minA = a[i].tMs;
    for (let i = 0; i < b.length; i++) if (b[i].tMs < minB) minB = b[i].tMs;
    return minA - minB;
  });

  const rng = mulberry(seed);
  const clusters = [];

  for (let k = 0; k < bigGroups.length; k++) {
    const g = bigGroups[k];
    g.sort((a, b) => a.passId - b.passId || a.tMs - b.tMs);
    const m = g.length;
    const xs = new Float64Array(m);
    const ys = new Float64Array(m);
    const pas = new Int32Array(m);
    const frp = new Float64Array(m);
    const tms = new Float64Array(m);
    let minPass = Infinity,
      maxPass = -Infinity,
      t0 = Infinity,
      t1 = -Infinity;

    for (let i = 0; i < m; i++) {
      xs[i] = g[i].x;
      ys[i] = g[i].y;
      pas[i] = g[i].passId;
      frp[i] = g[i].frp;
      tms[i] = g[i].tMs;
      if (pas[i] < minPass) minPass = pas[i];
      if (pas[i] > maxPass) maxPass = pas[i];
      if (tms[i] < t0) t0 = tms[i];
      if (tms[i] > t1) t1 = tms[i];
    }

    const root = pickRoot(pas, frp);
    const parent = buildParents(xs, ys, pas, root);
    const { stabRoot, stabEdge } = computeStability(xs, ys, pas, frp, root, parent, rng, runs, edgeNMax);

    let firstCount = 0;
    let lastSumX = 0,
      lastSumY = 0,
      lastSumT = 0,
      lastCount = 0;
    for (let i = 0; i < m; i++) {
      if (pas[i] === minPass) firstCount++;
      if (pas[i] === maxPass) {
        lastSumX += xs[i];
        lastSumY += ys[i];
        lastSumT += tms[i];
        lastCount++;
      }
    }

    const cx = lastSumX / lastCount;
    const cy = lastSumY / lastCount;
    const dvecX = cx - xs[root];
    const dvecY = cy - ys[root];
    const dist = Math.hypot(dvecX, dvecY);
    const brg = dist >= PIXEL_M ? bearing(dvecX, dvecY).deg : null;

    const relPtsObj = [];
    const ptsArr = [];
    for (let i = 0; i < m; i++) {
      const rx = Math.round(xs[i] - x0);
      const ry = Math.round(ys[i] - y0);
      relPtsObj.push({ x: rx, y: ry });
      ptsArr.push([
        rx,
        ry,
        Math.round(tms[i]),
        pas[i],
        satIdxMap.get(g[i].sat) ?? 0,
        Math.round(frp[i] * 10) / 10,
        confIdxMap[g[i].conf] ?? 1,
        parent[i],
      ]);
    }

    const areaHa = Math.round((computeGeom(relPtsObj, alpha).area / 1e4) * 10) / 10;
    const [rlon, rlat] = utm49NToLonLat(xs[root], ys[root]);
    const cid = `K${String(k + 1).padStart(4, '0')}`;

    clusters.push({
      id: cid,
      n: m,
      t0: Math.round(t0),
      t1: Math.round(t1),
      root,
      firstCount,
      stabRoot: Math.round(stabRoot * 1000) / 1000,
      stabEdge: stabEdge == null ? null : Math.round(stabEdge * 1000) / 1000,
      dirDeg: brg == null ? null : Math.round(brg * 10) / 10,
      distM: Math.round(dist),
      areaHa,
      rootLL: [Math.round(rlat * 1e5) / 1e5, Math.round(rlon * 1e5) / 1e5],
      pts: ptsArr,
    });
  }

  return {
    meta: {
      generated: new Date().toISOString(),
      crs: 'EPSG:32649',
      x0,
      y0,
      lat0,
      lon0,
      start,
      end,
      sats,
      params: { minConf, epsS, epsTH, minPoints, staticMonths, staticDays, runs, alpha },
      pixel_m: PIXEL_M,
      n_points: filtered.length,
      n_static_cells: staticCells.size,
      note: 'x,y dalam meter relatif terhadap (x0,y0) UTM 49N; t dalam ms UTC; pts=[x,y,t,pass,sat,frp,conf,parent]',
    },
    clusters,
    context: contextPts,
  };
}

