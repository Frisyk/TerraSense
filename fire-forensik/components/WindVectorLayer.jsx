'use client';

/**
 * @file WindVectorLayer.jsx
 * @description Animated 60fps wind particle streamline layer (diadaptasi dari wind-vector-layer.tsx)
 * untuk kanvas peta penuh Fire Forensik Kalimantan Barat.
 *
 * Algoritma:
 *  1. Menerima titik-titik medan vektor angin (lon, lat, u, v, wind_speed) pada jam lintasan satelit aktif
 *     (baik dari tabel monitoring.climate_history maupun Open-Meteo ERA5).
 *  2. Membangun medan interpolasi Inverse-Distance Weighting (IDW) di atas titik-titik grid
 *     sehingga setiap piksel pada kanvas dapat mengambil sampel komponen kecepatan (u, v).
 *  3. Menganimasikan 2.400 partikel menggunakan requestAnimationFrame dengan efek jejak halus
 *     (destination-out alpha fade) dan pengelompokan 6 tingkat warna kecepatan angin (WIND_SPEED_TIERS).
 */

import React, { useEffect, useRef } from 'react';
import { utm49NToLonLat, resolveDatasetOriginUTM } from '../lib/forensicEngine';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const DEFAULT_PARTICLE_COUNT = 600; // jumlah vektor angin ramping agar titik api tetap fokus utama
const EMBER_COUNT = 45; // partikel bara api halus yang ditiup angin dari hotspot aktif
const MAX_AGE = 140;
const FADE_ALPHA_NORMAL = 0.935; // jejak luruh lebih cepat & bersih, mencegah layar berdebu
const FADE_ALPHA_BURST = 0.78; // akselerasi luruh instan saat lintasan satelit melompat
const LINE_WIDTH = 1.05; // garis benang sutra tipis & tajam
const SPEED_SCALE = 0.60; // kalibrasi kecepatan aliran agar selaras dengan skala sebaran visual peta

/**
 * Palet warna meteorologi berdasarkan kecepatan angin (m/s)
 * Dibuat semi-transparan elegan agar tidak menenggelamkan kontras titik hotspot api.
 */
export const WIND_SPEED_TIERS = [
  { minSpeed: 0.0, label: '< 1.0 m/s', color: 'rgba(186, 230, 253, 0.42)' },
  { minSpeed: 1.0, label: '1.0 - 2.0 m/s', color: 'rgba(56, 189, 248, 0.52)' },
  { minSpeed: 2.0, label: '2.0 - 3.2 m/s', color: 'rgba(52, 211, 153, 0.58)' },
  { minSpeed: 3.2, label: '3.2 - 4.5 m/s', color: 'rgba(250, 204, 21, 0.62)' },
  { minSpeed: 4.5, label: '4.5 - 6.0 m/s', color: 'rgba(251, 146, 60, 0.68)' },
  { minSpeed: 6.0, label: '> 6.0 m/s', color: 'rgba(244, 63, 94, 0.72)' },
];

// ---------------------------------------------------------------------------
// Helpers: Inverse-Distance Weighting (IDW) Wind Field & Particle Factory
// ---------------------------------------------------------------------------

function makeParticle(w, h) {
  return {
    x: Math.random() * w,
    y: Math.random() * h,
    age: Math.floor(Math.random() * MAX_AGE),
    maxAge: 100 + Math.random() * 80,
  };
}

function makeEmber(hotspots, W, Hh, view) {
  if (!hotspots || hotspots.length === 0) {
    return {
      active: false,
      x: -100,
      y: -100,
      age: 0,
      maxAge: 30,
      size: 1.5,
    };
  }
  const idx = Math.floor(Math.random() * hotspots.length);
  const hp = hotspots[idx];
  const sx = (hp.x - view.cx) * view.s + W / 2;
  const sy = Hh / 2 - (hp.y - view.cy) * view.s;
  const angle = Math.random() * Math.PI * 2;
  const r = Math.random() * 8;
  return {
    active: true,
    x: sx + Math.cos(angle) * r,
    y: sy + Math.sin(angle) * r,
    age: 0,
    maxAge: 35 + Math.random() * 40,
    size: 1.2 + Math.random() * 2.4,
    uDrift: (Math.random() - 0.5) * 0.35,
    vDrift: (Math.random() - 0.5) * 0.35,
  };
}

/**
 * Membangun fungsi interpolasi IDW (1/d^2) di atas titik-titik vektor angin (lon, lat, u, v).
 */
export function buildWindField(points) {
  if (!points || points.length === 0) return null;

  const getUV = (lon, lat) => {
    if (points.length === 1) {
      const p0 = points[0];
      const wave = Math.sin(lon * 18.0 + lat * 14.0) * 0.18;
      const cosW = Math.cos(wave);
      const sinW = Math.sin(wave);
      return {
        u: p0.u * cosW - p0.v * sinW,
        v: p0.u * sinW + p0.v * cosW,
      };
    }

    let sumW = 0;
    let sumU = 0;
    let sumV = 0;
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      const dx = lon - p.lon;
      const dy = lat - p.lat;
      const d2 = dx * dx + dy * dy;
      if (d2 < 1e-10) return { u: p.u, v: p.v };
      const w = 1 / d2;
      sumW += w;
      sumU += w * p.u;
      sumV += w * p.v;
    }
    return sumW > 0 ? { u: sumU / sumW, v: sumV / sumW } : { u: 0, v: 0 };
  };

  return { getUV };
}

/**
 * Mengonversi satu atau beberapa rekaman cuaca aktif menjadi array titik spasial 3x3
 * untuk diinterpolasi oleh buildWindField().
 */
export function buildSpatialPointsFromWeather(activeWeather, centerLat = -0.5, centerLon = 110.4) {
  if (!activeWeather) return [];
  if (Array.isArray(activeWeather.gridPoints) && activeWeather.gridPoints.length > 0) {
    return activeWeather.gridPoints;
  }

  const baseU = Number(activeWeather.wu ?? 0);
  const baseV = Number(activeWeather.wv ?? 0);
  const baseSpeed = Number(activeWeather.ws ?? Math.hypot(baseU, baseV));

  // Bangun 9 titik grid (3x3 dengan jarak 0.15 derajat) di sekitar pusat kejadian
  // dengan sedikit curl/divergensi atmosfer agar medan IDW terlihat hidup dan realistis
  const pts = [];
  const step = 0.15;
  for (let di = -1; di <= 1; di++) {
    for (let dj = -1; dj <= 1; dj++) {
      const lat = centerLat + di * step;
      const lon = centerLon + dj * step;
      const angleShift = (di * 0.07 - dj * 0.06);
      const speedMod = 1 + (di * 0.05 + dj * 0.04);
      const cosA = Math.cos(angleShift);
      const sinA = Math.sin(angleShift);
      const u = (baseU * cosA - baseV * sinA) * speedMod;
      const v = (baseU * sinA + baseV * cosA) * speedMod;
      pts.push({
        lon,
        lat,
        u,
        v,
        wind_speed: Math.max(0.2, baseSpeed * speedMod),
      });
    }
  }
  return pts;
}

// ---------------------------------------------------------------------------
// Component: WindVectorLayer
// ---------------------------------------------------------------------------
export default function WindVectorLayer({
  visible = true,
  activeWeather = null,
  centerLL = [-0.5, 110.4],
  viewRef,
  sizeRef,
  ds,
  stepK = 0,
  activeCluster = null,
  playing = false,
  particleCount = DEFAULT_PARTICLE_COUNT,
}) {
  const canvasRef = useRef(null);
  const animRef = useRef(0);
  const particlesRef = useRef([]);
  const embersRef = useRef([]);
  const prevFieldRef = useRef(null);
  const targetFieldRef = useRef(null);
  const fieldAlphaRef = useRef(1);
  const burstFadeRef = useRef(0);
  const lastStepRef = useRef(stepK);
  const bucketsRef = useRef([[], [], [], [], [], []]);
  const originUtmRef = useRef([433226, 9944729]);

  // Perbarui origin UTM saat dataset berubah
  useEffect(() => {
    if (ds) {
      originUtmRef.current = resolveDatasetOriginUTM(ds);
    }
  }, [ds]);

  // Perbarui kerapatan partikel jika prop particleCount berubah
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const w = canvas.width || 1200;
    const h = canvas.height || 800;
    particlesRef.current = Array.from({ length: particleCount }, () => makeParticle(w, h));
  }, [particleCount]);

  // Perbarui medan interpolasi IDW & inisiasi transisi halus saat cuaca / step berubah
  useEffect(() => {
    if (!visible || !activeWeather) {
      targetFieldRef.current = null;
      prevFieldRef.current = null;
      fieldAlphaRef.current = 1;
      return;
    }

    const [cLat, cLon] = Array.isArray(centerLL) && centerLL.length === 2 ? centerLL : [-0.5, 110.4];
    const pts = buildSpatialPointsFromWeather(activeWeather, cLat, cLon);
    const newField = buildWindField(pts);

    if (targetFieldRef.current) {
      prevFieldRef.current = targetFieldRef.current;
      targetFieldRef.current = newField;
      fieldAlphaRef.current = 0; // Mulai transisi lerp halus menuju medan baru
    } else {
      targetFieldRef.current = newField;
      prevFieldRef.current = newField;
      fieldAlphaRef.current = 1;
    }
  }, [visible, activeWeather, centerLL]);

  // Reaksi cepat saat step lintasan satelit melompat: bersihkan jejak lama (trail flush)
  useEffect(() => {
    if (lastStepRef.current !== stepK) {
      lastStepRef.current = stepK;
      burstFadeRef.current = 14; // Selama 14 frame, percepat luruh jejak agar arah baru langsung dominan
    }
  }, [stepK]);

  // Siklus hidup kanvas & animasi 60fps
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !visible) return undefined;

    const syncCanvasSize = () => {
      const parent = canvas.parentElement;
      const w = parent ? parent.clientWidth : window.innerWidth;
      const h = parent ? parent.clientHeight : window.innerHeight;
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
        particlesRef.current = Array.from({ length: particleCount }, () => makeParticle(w, h));
        embersRef.current = Array.from({ length: EMBER_COUNT }, () => ({
          active: false,
          x: -100,
          y: -100,
          age: 0,
          maxAge: 30,
          size: 1.5,
          uDrift: 0,
          vDrift: 0,
        }));
      }
    };

    syncCanvasSize();
    if (particlesRef.current.length === 0) {
      particlesRef.current = Array.from({ length: particleCount }, () =>
        makeParticle(canvas.width || 1200, canvas.height || 800)
      );
    }
    if (embersRef.current.length === 0) {
      embersRef.current = Array.from({ length: EMBER_COUNT }, () => ({
        active: false,
        x: -100,
        y: -100,
        age: 0,
        maxAge: 30,
        size: 1.5,
        uDrift: 0,
        vDrift: 0,
      }));
    }

    const ro = new ResizeObserver(syncCanvasSize);
    if (canvas.parentElement) {
      ro.observe(canvas.parentElement);
    }

    let active = true;

    const drawFrame = () => {
      if (!active) return;
      const ctx = canvas.getContext('2d');
      const targetField = targetFieldRef.current;
      const prevField = prevFieldRef.current || targetField;

      if (ctx && targetField) {
        const cw = canvas.width;
        const ch = canvas.height;
        const particles = particlesRef.current;
        const embers = embersRef.current;
        const buckets = bucketsRef.current;
        const view = viewRef?.current || { cx: 0, cy: 0, s: 0.05 };
        const [x0, y0] = originUtmRef.current;

        // Kosongkan bucket tanpa alokasi memori baru
        for (let i = 0; i < 6; i++) {
          buckets[i].length = 0;
        }

        // Hitung faktor transisi LERP medan angin halus (antar jam satelit atau update cuaca)
        let alpha = fieldAlphaRef.current;
        if (alpha < 1) {
          alpha = Math.min(1, alpha + 0.065);
          fieldAlphaRef.current = alpha;
        }

        // Tentukan tingkat luruh jejak (trail persistence)
        let fadeFactor;
        if (burstFadeRef.current > 0) {
          burstFadeRef.current--;
          fadeFactor = FADE_ALPHA_BURST; // Luruh cepat saat baru lompat lintasan satelit
        } else {
          fadeFactor = FADE_ALPHA_NORMAL;
        }

        // Luruhkan jejak partikel sebelumnya secara halus
        ctx.globalCompositeOperation = 'destination-out';
        ctx.fillStyle = `rgba(0, 0, 0, ${1 - fadeFactor})`;
        ctx.fillRect(0, 0, cw, ch);
        ctx.globalCompositeOperation = 'source-over';

        // 1. Perbarui posisi setiap partikel angin & kelompokkan ke dalam 6 tier kecepatan
        for (let i = 0; i < particles.length; i++) {
          const p = particles[i];

          // Konversi piksel layar (p.x, p.y) -> UTM 49N -> (lon, lat)
          const rx = (p.x - cw / 2) / view.s + view.cx;
          const ry = (ch / 2 - p.y) / view.s + view.cy;
          const [lon, lat] = utm49NToLonLat(x0 + rx, y0 + ry);

          const uvT = targetField.getUV(lon, lat);
          let u = uvT.u;
          let v = uvT.v;
          if (alpha < 1 && prevField) {
            const uvP = prevField.getUV(lon, lat);
            u = uvP.u * (1 - alpha) + uvT.u * alpha;
            v = uvP.v * (1 - alpha) + uvT.v * alpha;
          }

          const nx = p.x + u * SPEED_SCALE;
          const ny = p.y - v * SPEED_SCALE; // sumbu Y kanvas mengarah ke bawah

          const speed = Math.sqrt(u * u + v * v);

          let tier = 0;
          if (speed >= 6.0) tier = 5;
          else if (speed >= 4.5) tier = 4;
          else if (speed >= 3.2) tier = 3;
          else if (speed >= 2.0) tier = 2;
          else if (speed >= 1.0) tier = 1;

          buckets[tier].push(p.x, p.y, nx, ny);

          p.x = nx;
          p.y = ny;
          p.age++;

          if (p.age > p.maxAge || p.x < 0 || p.x > cw || p.y < 0 || p.y > ch) {
            const fresh = makeParticle(cw, ch);
            p.x = fresh.x;
            p.y = fresh.y;
            p.age = fresh.age;
            p.maxAge = fresh.maxAge;
          }
        }

        // Gambar batch streamline angin per tier kecepatan
        ctx.lineWidth = LINE_WIDTH;
        ctx.lineCap = 'round';

        for (let i = 0; i < 6; i++) {
          const b = buckets[i];
          const len = b.length;
          if (len === 0) continue;

          ctx.strokeStyle = WIND_SPEED_TIERS[i].color;
          ctx.beginPath();
          for (let j = 0; j < len; j += 4) {
            ctx.moveTo(b[j], b[j + 1]);
            ctx.lineTo(b[j + 2], b[j + 3]);
          }
          ctx.stroke();
        }

        // 2. Partikel Bara Api (Fire Embers): Berhamburan dari titik api aktif mengikuti angin
        if (activeCluster && Array.isArray(activeCluster.pts)) {
          // Ambil titik api yang aktif sampai lintasan saat ini (terutama yang terbaru)
          const currentPts = activeCluster.pts.filter(
            (pt) => pt.step <= stepK && pt.step >= Math.max(0, stepK - 1)
          );
          const candidatePts = currentPts.length > 0 ? currentPts : activeCluster.pts;

          for (let i = 0; i < embers.length; i++) {
            const emb = embers[i];
            if (!emb.active) {
              if (candidatePts.length > 0) {
                embers[i] = makeEmber(candidatePts, cw, ch, view);
              }
              continue;
            }

            const rx = (emb.x - cw / 2) / view.s + view.cx;
            const ry = (ch / 2 - emb.y) / view.s + view.cy;
            const [lon, lat] = utm49NToLonLat(x0 + rx, y0 + ry);
            const uv = targetField.getUV(lon, lat);

            emb.x += uv.u * SPEED_SCALE * 0.95 + emb.uDrift;
            emb.y -= uv.v * SPEED_SCALE * 0.95 + emb.vDrift;
            emb.age++;

            const progress = emb.age / emb.maxAge;
            if (progress >= 1 || emb.x < 0 || emb.x > cw || emb.y < 0 || emb.y > ch) {
              embers[i] = makeEmber(candidatePts, cw, ch, view);
              continue;
            }

            // Gambar bara api dengan gradasi api menyala -> padam
            ctx.beginPath();
            ctx.arc(emb.x, emb.y, Math.max(0.6, emb.size * (1 - progress * 0.45)), 0, Math.PI * 2);
            if (progress < 0.28) {
              ctx.fillStyle = `rgba(254, 240, 138, ${0.95 - progress})`; // kuning emas terang
            } else if (progress < 0.65) {
              ctx.fillStyle = `rgba(251, 146, 60, ${0.85 - progress * 0.4})`; // oranye bara
            } else {
              ctx.fillStyle = `rgba(239, 68, 68, ${(1 - progress) * 0.75})`; // merah padam
            }
            ctx.fill();
          }
        }
      }

      animRef.current = requestAnimationFrame(drawFrame);
    };

    animRef.current = requestAnimationFrame(drawFrame);

    return () => {
      active = false;
      cancelAnimationFrame(animRef.current);
      ro.disconnect();
    };
  }, [visible, viewRef, sizeRef, activeCluster, stepK]);

  if (!visible) return null;

  return (
    <canvas
      ref={canvasRef}
      id="wind-animation-canvas"
      aria-hidden="true"
      style={{
        position: 'absolute',
        inset: 0,
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
        zIndex: 20,
      }}
    />
  );
}

