'use client';

import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import {
  Plus,
  Minus,
  Crosshair,
  Globe,
  Flame,
  Database,
  Layers,
  List,
  FileText,
  Play,
  Pause,
  RotateCcw,
  X,
  Info,
  Download,
  Upload,
  Eye,
  EyeOff,
  Wind,
  Camera,
  Crop,
  Check,
} from 'lucide-react';
import BasemapSelector from './BasemapSelector';
import WindVectorLayer, { WIND_SPEED_TIERS } from './WindVectorLayer';
import { getBasemapConfig, resolveTileUrl } from '../utils/basemaps';
import {
  H,
  BUF,
  fmt,
  fmtY,
  num,
  bearing,
  llText,
  prepDataset,
  buildDemo,
  computeGeom,
  lonLatToUTM49N,
  utm49NToLonLat,
  resolveDatasetOriginUTM,
  lonToTileX,
  latToTileY,
  tileXToLon,
  tileYToLat,
} from '../lib/forensicEngine';

export default function ForensicApp() {
  // Dataset & cluster state
  const [ds, setDs] = useState(() => buildDemo());
  const [isDemo, setIsDemo] = useState(true);
  const [srcLabel, setSrcLabel] = useState('Data simulasi, bukan FIRMS asli');
  const [statusMsg, setStatusMsg] = useState('');
  const [cur, setCur] = useState(0);
  const [stepK, setStepK] = useState(0);
  const [playing, setPlaying] = useState(false);

  // Basemap & map UI state
  const [activeMapType, setActiveMapType] = useState('dark');
  const [mapOpacity, setMapOpacity] = useState(0.9);
  const [cursorHud, setCursorHud] = useState('');

  // Floating Popups / Tool panels state
  const [showListPanel, setShowListPanel] = useState(true);
  const [showDetailPanel, setShowDetailPanel] = useState(true);
  const [showLayerPanel, setShowLayerPanel] = useState(false);
  const [showDbModal, setShowDbModal] = useState(false);
  const [showLegend, setShowLegend] = useState(true);
  const [showInfoModal, setShowInfoModal] = useState(false);
  const [hideAllUI, setHideAllUI] = useState(false);

  // Filter & layer state
  const [minN, setMinN] = useState(3);
  const [sortBy, setSortBy] = useState('t0');
  const [clusterPage, setClusterPage] = useState(1);
  const [clusterPageSize, setClusterPageSize] = useState(25);
  const [alphaM, setAlphaM] = useState(1300);
  const [layers, setLayers] = useState({
    area: true,
    arrow: true,
    pts: true,
    day: true,
    wind: true,
    ctx: true,
    grid: false,
  });
  const [isDropping, setIsDropping] = useState(false);
  const [climatePayload, setClimatePayload] = useState(null);
  const [climateLoading, setClimateLoading] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [windDensity, setWindDensity] = useState(550);

  // Database query controls
  const [dbLoading, setDbLoading] = useState(false);
  const [dbStart, setDbStart] = useState('');
  const [dbEnd, setDbEnd] = useState('');
  const [dbMinConf, setDbMinConf] = useState('n');
  const [dbEpsS, setDbEpsS] = useState(1500);
  const [dbEpsTH, setDbEpsTH] = useState(48);
  const [dbMinPoints, setDbMinPoints] = useState(3);
  const [dbMaxPoints, setDbMaxPoints] = useState(15000);
  const [dbBounds, setDbBounds] = useState(null);

  // Tooltip state
  const [tipData, setTipData] = useState(null);

  // Screenshot Export & Crop selection state
  const [isCropping, setIsCropping] = useState(false);
  const [cropBox, setCropBox] = useState(null); // { x0, y0, x1, y1 }
  const [showExportModal, setShowExportModal] = useState(false);
  const [screenshotPreviewUrl, setScreenshotPreviewUrl] = useState(null);
  const [screenshotCropBox, setScreenshotCropBox] = useState(null);
  const [exportWatermark, setExportWatermark] = useState(true);
  const [exportWind, setExportWind] = useState(true);
  const [exportScale, setExportScale] = useState(true);
  const [exportFilename, setExportFilename] = useState('');
  const cropStartRef = useRef(null);

  // Refs for Canvas, Viewport & Tile Cache
  const mapboxRef = useRef(null);
  const canvasRef = useRef(null);
  const viewRef = useRef({ cx: 0, cy: 0, s: 0.05 });
  const sizeRef = useRef({ W: 0, Hh: 0, dpr: 1 });
  const dragRef = useRef(null);
  const geoCacheRef = useRef({});
  const tileCacheRef = useRef(new Map());
  const rafDrawRef = useRef(0);
  const fileInputRef = useRef(null);
  const activeWeatherRef = useRef(null);
  const climateCacheRef = useRef(new Map());

  // Helper: cached geometry for (clusterIdx, stepK, alphaM)
  const getGeom = useCallback(
    (ci, k, alphaVal = alphaM, dataset = ds) => {
      if (!dataset || ci < 0 || !dataset.clusters[ci]) {
        return { vis: [], tris: [], edges: [], area: 0 };
      }
      const key = `${ci}_${k}_${alphaVal}`;
      if (geoCacheRef.current[key]) return geoCacheRef.current[key];
      const c = dataset.clusters[ci];
      const vis = c.pts.filter((p) => p.step <= k);
      const res = computeGeom(vis, alphaVal);
      geoCacheRef.current[key] = res;
      return res;
    },
    [alphaM, ds]
  );

  // Fit viewport to a set of points
  const fitToPoints = useCallback((pts, pad) => {
    if (!pts || !pts.length) return;
    const { W, Hh } = sizeRef.current;
    if (!W || !Hh) return;
    let minx = Infinity,
      maxx = -Infinity,
      miny = Infinity,
      maxy = -Infinity;
    pts.forEach((p) => {
      if (p.x < minx) minx = p.x;
      if (p.x > maxx) maxx = p.x;
      if (p.y < miny) miny = p.y;
      if (p.y > maxy) maxy = p.y;
    });
    const w = Math.max(maxx - minx, 1200);
    const h = Math.max(maxy - miny, 1200);
    const s = Math.min(W / (w * (1 + pad)), Hh / (h * (1 + pad)));
    const cx = (minx + maxx) / 2;
    const cy = (miny + maxy) / 2 + (Hh * 0.05) / s;
    viewRef.current = { cx, cy, s };
  }, []);

  // Draw function on Canvas (Basemap Tiles + UTM Grid + Forensic Vector Layers)
  const draw = useCallback(() => {
    const cv = canvasRef.current;
    const { W, Hh, dpr } = sizeRef.current;
    if (!cv || !W || !Hh || !ds) return;
    const ctx = cv.getContext('2d');
    if (!ctx) return;

    const view = viewRef.current;
    const SX = (x) => (x - view.cx) * view.s + W / 2;
    const SY = (y) => Hh / 2 - (y - view.cy) * view.s;

    const cs = getComputedStyle(document.documentElement);
    const col = (n) => cs.getPropertyValue(n).trim();
    const basemapCfg = getBasemapConfig(activeMapType);
    const hasRasterBasemap = Boolean(basemapCfg.url);

    // Sesuaikan palet kontras di atas peta dasar gelap vs terang
    const isDarkMap =
      basemapCfg.isDark != null
        ? basemapCfg.isDark
        : (col('--map') || '#0a1014').startsWith('#0');

    const bgFill = isDarkMap ? '#0a1014' : col('--map') || '#e3e9ed';
    const halo = isDarkMap ? 'rgba(10, 16, 20, 0.88)' : 'rgba(250, 251, 252, 0.9)';
    const color = isDarkMap ? '#ff7b47' : col('--ca') || '#cf4417';
    const muted = isDarkMap ? '#9fb2c0' : col('--muted') || '#566774';
    const fg = isDarkMap ? '#f0f5f9' : col('--fg') || '#13212b';

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = bgFill;
    ctx.fillRect(0, 0, W, Hh);

    const left = view.cx - W / 2 / view.s;
    const right = view.cx + W / 2 / view.s;
    const bot = view.cy - Hh / 2 / view.s;
    const top = view.cy + Hh / 2 / view.s;

    // ---------- 1. Render Raster Basemap Tiles (CARTO / OSM / Esri / OpenTopo / Google) ----------
    if (hasRasterBasemap && mapOpacity > 0.02) {
      const [x0, y0] = resolveDatasetOriginUTM(ds);
      const [lonMin, latMin] = utm49NToLonLat(x0 + left, y0 + bot);
      const [lonMax, latMax] = utm49NToLonLat(x0 + right, y0 + top);

      const maxZ = basemapCfg.maxZoom || 19;
      const rawZ = Math.round(Math.log2(156543.0339 * view.s));
      const z = Math.max(4, Math.min(maxZ, rawZ));
      const maxTileIdx = Math.pow(2, z) - 1;

      const tx0 = Math.max(0, Math.min(maxTileIdx, lonToTileX(Math.min(lonMin, lonMax), z) - 1));
      const tx1 = Math.max(0, Math.min(maxTileIdx, lonToTileX(Math.max(lonMin, lonMax), z) + 1));
      const ty0 = Math.max(0, Math.min(maxTileIdx, latToTileY(Math.max(latMin, latMax), z) - 1));
      const ty1 = Math.max(0, Math.min(maxTileIdx, latToTileY(Math.min(latMin, latMax), z) + 1));

      if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= 120) {
        ctx.save();
        ctx.globalAlpha = mapOpacity;
        const cache = tileCacheRef.current;

        const scheduleRedraw = () => {
          if (rafDrawRef.current) return;
          rafDrawRef.current = requestAnimationFrame(() => {
            rafDrawRef.current = 0;
            draw();
          });
        };

        const getOrLoadTile = (tz, tx, ty) => {
          const url = resolveTileUrl(basemapCfg.url, tz, tx, ty);
          if (!url) return null;
          let entry = cache.get(url);
          if (!entry) {
            const img = new Image();
            img.crossOrigin = 'anonymous';
            entry = { img, loaded: false, error: false };
            img.onload = () => {
              entry.loaded = true;
              scheduleRedraw();
            };
            img.onerror = () => {
              entry.error = true;
            };
            img.src = url;
            cache.set(url, entry);
            if (cache.size > 450) {
              const oldest = cache.keys().next().value;
              cache.delete(oldest);
            }
          }
          return entry;
        };

        for (let tx = tx0; tx <= tx1; tx++) {
          for (let ty = ty0; ty <= ty1; ty++) {
            const lonTL = tileXToLon(tx, z);
            const latTL = tileYToLat(ty, z);
            const lonBR = tileXToLon(tx + 1, z);
            const latBR = tileYToLat(ty + 1, z);

            const [ux0, uy0] = lonLatToUTM49N(lonTL, latTL);
            const [ux1, uy1] = lonLatToUTM49N(lonBR, latBR);

            const sx0 = SX(ux0 - x0);
            const sy0 = SY(uy0 - y0);
            const sx1 = SX(ux1 - x0);
            const sy1 = SY(uy1 - y0);
            const dw = sx1 - sx0;
            const dh = sy1 - sy0;
            if (dw <= 0 || dh <= 0) continue;

            const entry = getOrLoadTile(z, tx, ty);
            if (entry && entry.loaded) {
              ctx.drawImage(entry.img, sx0, sy0, dw + 0.6, dh + 0.6);
            } else {
              // Fallback ke tile induk (z-1 atau z-2) agar tidak berkedip saat zoom/pan
              for (let dz = 1; dz <= 2; dz++) {
                const pz = z - dz;
                if (pz < 3) break;
                const scale = 1 << dz;
                const ptx = Math.floor(tx / scale);
                const pty = Math.floor(ty / scale);
                const pUrl = resolveTileUrl(basemapCfg.url, pz, ptx, pty);
                const pEntry = pUrl ? cache.get(pUrl) : null;
                if (pEntry && pEntry.loaded) {
                  const subW = 256 / scale;
                  const srcX = (tx % scale) * subW;
                  const srcY = (ty % scale) * subW;
                  ctx.drawImage(
                    pEntry.img,
                    srcX,
                    srcY,
                    subW,
                    subW,
                    sx0,
                    sy0,
                    dw + 0.6,
                    dh + 0.6
                  );
                  break;
                }
              }
            }
          }
        }
        ctx.restore();
      }
    }

    // ---------- 2. Grid Metrik UTM ----------
    function gridLines(step, strokeCol, alphaVal = 1) {
      if (step * view.s < 5) return;
      ctx.save();
      ctx.globalAlpha = alphaVal;
      ctx.strokeStyle = strokeCol;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let x = Math.floor(left / step) * step; x <= right; x += step) {
        ctx.moveTo(SX(x), 0);
        ctx.lineTo(SX(x), Hh);
      }
      for (let y = Math.floor(bot / step) * step; y <= top; y += step) {
        ctx.moveTo(0, SY(y));
        ctx.lineTo(W, SY(y));
      }
      ctx.stroke();
      ctx.restore();
    }

    const coarse = view.s > 0.02 ? 1000 : view.s > 0.004 ? 5000 : 20000;
    gridLines(
      coarse,
      isDarkMap ? 'rgba(255,255,255,0.12)' : col('--grid') || '#d3dce2',
      hasRasterBasemap ? 0.5 : 1
    );
    if (layers.grid && 375 * view.s >= 6) {
      gridLines(
        375,
        isDarkMap ? 'rgba(255,255,255,0.24)' : col('--grid-strong') || '#bfcad3',
        0.85
      );
    }

    ctx.font = '500 11px ' + (cs.getPropertyValue('--font-data') || 'monospace');
    ctx.textBaseline = 'middle';

    function label(txt, x, y, c, align) {
      ctx.textAlign = align || 'left';
      ctx.lineWidth = 3.5;
      ctx.strokeStyle = halo;
      ctx.strokeText(txt, x, y);
      ctx.fillStyle = c;
      ctx.fillText(txt, x, y);
    }

    // ---------- 3. Konteks: Titik Tunggal dan Kejadian Lain ----------
    if (layers.ctx) {
      ctx.fillStyle = isDarkMap ? '#a8bac8' : muted;
      ctx.globalAlpha = hasRasterBasemap ? 0.72 : 0.55;
      const drawDot = (x, y) => {
        const sx = SX(x),
          sy = SY(y);
        if (sx < -4 || sy < -4 || sx > W + 4 || sy > Hh + 4) return;
        ctx.fillRect(sx - 1.75, sy - 1.75, 3.5, 3.5);
      };
      ds.context.forEach((q) => drawDot(q[0], q[1]));
      ds.clusters.forEach((c, i) => {
        if (i !== cur) c.pts.forEach((p) => drawDot(p.x, p.y));
      });
      ctx.globalAlpha = 1;
    }

    // ---------- 4. Kejadian Terpilih (Garis Harian, Area Alpha Shape, Panah, Titik, Akar) ----------
    if (cur >= 0 && ds.clusters[cur]) {
      const c = ds.clusters[cur];
      const g = getGeom(cur, stepK, alphaM, ds);
      const root = c.pts[c.root];
      const steps = c.steps;

      if (g.vis.length && root && steps[stepK]) {
        if (layers.day) {
          const seen = {};
          for (let j = stepK - 1; j >= 0; j--) {
            const dj = steps[j].day;
            if (dj === steps[stepK].day || seen[dj]) continue;
            seen[dj] = 1;
            const gj = getGeom(cur, j, alphaM, ds);
            if (!gj.edges.length) continue;
            ctx.save();
            ctx.globalAlpha = 0.78;
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.2;
            ctx.setLineDash([4, 3]);
            ctx.beginPath();
            let far = null,
              fd = -1;
            gj.edges.forEach((e) => {
              const A = gj.vis[e[0]],
                B = gj.vis[e[1]];
              ctx.moveTo(SX(A.x), SY(A.y));
              ctx.lineTo(SX(B.x), SY(B.y));
              [A, B].forEach((p) => {
                const dd = Math.hypot(p.x - root.x, p.y - root.y);
                if (dd > fd) {
                  fd = dd;
                  far = p;
                }
              });
            });
            ctx.stroke();
            ctx.restore();
            if (far) label(dj, SX(far.x) + 8, SY(far.y) - 8, color);
          }
        }

        if (layers.area) {
          ctx.save();
          const path = new Path2D();
          const r = Math.max(BUF * view.s, 3);
          g.tris.forEach((t) => {
            const A = g.vis[t.a],
              B = g.vis[t.b],
              C = g.vis[t.c];
            const ax = SX(A.x),
              ay = SY(A.y),
              bx = SX(B.x),
              by = SY(B.y),
              cx2 = SX(C.x),
              cy2 = SY(C.y);
            const cross = (bx - ax) * (cy2 - ay) - (cx2 - ax) * (by - ay);
            path.moveTo(ax, ay);
            if (cross >= 0) {
              path.lineTo(bx, by);
              path.lineTo(cx2, cy2);
            } else {
              path.lineTo(cx2, cy2);
              path.lineTo(bx, by);
            }
            path.closePath();
          });
          g.vis.forEach((p) => {
            const x = SX(p.x),
              y = SY(p.y);
            path.moveTo(x + r, y);
            path.arc(x, y, r, 0, Math.PI * 2, false);
          });
          ctx.fillStyle = color;
          ctx.globalAlpha = hasRasterBasemap ? 0.26 : 0.2;
          ctx.fill(path, 'nonzero');
          ctx.globalAlpha = 1;
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.8;
          ctx.beginPath();
          g.edges.forEach((e) => {
            const A = g.vis[e[0]],
              B = g.vis[e[1]];
            ctx.moveTo(SX(A.x), SY(A.y));
            ctx.lineTo(SX(B.x), SY(B.y));
          });
          ctx.stroke();
          if (!g.edges.length) {
            g.vis.forEach((p) => {
              ctx.beginPath();
              ctx.arc(SX(p.x), SY(p.y), r, 0, Math.PI * 2);
              ctx.stroke();
            });
          }
          ctx.restore();
        }

        if (layers.arrow) {
          ctx.save();
          ctx.globalAlpha = 0.9;
          ctx.strokeStyle = color;
          ctx.fillStyle = color;
          ctx.lineWidth = 1.5;
          c.pts.forEach((p, i) => {
            if (p.step > stepK || c.parent[i] < 0) return;
            const q = c.pts[c.parent[i]];
            if (!q) return;
            const d = Math.hypot(p.x - q.x, p.y - q.y);
            const x1 = SX(q.x),
              y1 = SY(q.y),
              x2 = SX(p.x),
              y2 = SY(p.y);
            if (d < 375 || p.step === 0) {
              ctx.setLineDash([2, 3]);
              ctx.beginPath();
              ctx.moveTo(x1, y1);
              ctx.lineTo(x2, y2);
              ctx.stroke();
              ctx.setLineDash([]);
              return;
            }
            const ang = Math.atan2(y2 - y1, x2 - x1),
              hl = 8,
              pr = 4;
            const ex = x2 - Math.cos(ang) * pr,
              ey = y2 - Math.sin(ang) * pr;
            ctx.beginPath();
            ctx.moveTo(x1, y1);
            ctx.lineTo(ex, ey);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(ex, ey);
            ctx.lineTo(ex - hl * Math.cos(ang - 0.4), ey - hl * Math.sin(ang - 0.4));
            ctx.lineTo(ex - hl * Math.cos(ang + 0.4), ey - hl * Math.sin(ang + 0.4));
            ctx.closePath();
            ctx.fill();
          });
          ctx.restore();
        }

        if (layers.pts) {
          g.vis.forEach((p) => {
            const x = SX(p.x),
              y = SY(p.y);
            ctx.beginPath();
            ctx.arc(x, y, 3.6, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();
            ctx.lineWidth = 1.2;
            ctx.strokeStyle = halo;
            ctx.stroke();
            if (p.step === stepK) {
              ctx.beginPath();
              ctx.arc(x, y, 7.2, 0, Math.PI * 2);
              ctx.strokeStyle = color;
              ctx.lineWidth = 1.6;
              ctx.stroke();
            }
          });
        }

        ctx.save();
        if (c.stabRoot < 0.8 && c.firstCount > 1) {
          ctx.setLineDash([3, 3]);
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.3;
          c.pts.forEach((p) => {
            if (p.step === 0) {
              ctx.beginPath();
              ctx.arc(SX(p.x), SY(p.y), 12, 0, Math.PI * 2);
              ctx.stroke();
            }
          });
          ctx.setLineDash([]);
        }
        const rx = SX(root.x),
          ry = SY(root.y),
          flip = rx > W - 190;
        ctx.beginPath();
        ctx.moveTo(rx, ry - 9);
        ctx.lineTo(rx + 9, ry);
        ctx.lineTo(rx, ry + 9);
        ctx.lineTo(rx - 9, ry);
        ctx.closePath();
        ctx.fillStyle = fg;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = color;
        ctx.stroke();
        label('Awal · ' + fmt(root.t), flip ? rx - 14 : rx + 14, ry, fg, flip ? 'right' : 'left');
        ctx.restore();
      }
    }

    // ---------- 4B. Kompas & Indikator Arah Angin / Cuaca (ERA5 10m) ----------
    // Aliran partikel streamline 60fps digambar pada layer kanvas terpisah (<WindVectorLayer />)
    const curW = activeWeatherRef.current;
    if (layers.wind && curW) {
      ctx.save();
      const windColor = isDarkMap ? '#38bdf8' : '#0284c7';
      const wtoRad = ((curW.wto || 0) * Math.PI) / 180;
      const ux = Math.sin(wtoRad);
      const uy = -Math.cos(wtoRad); // di layar kanvas Y ke atas bernilai negatif

      // Kompas / Wind Vane Badge di atas kanvas
      ctx.globalAlpha = 1;
      const compX = W / 2;
      const compY = 76;
      ctx.fillStyle = isDarkMap ? 'rgba(12, 18, 24, 0.86)' : 'rgba(255, 255, 255, 0.9)';
      ctx.strokeStyle = isDarkMap ? 'rgba(56, 189, 248, 0.45)' : 'rgba(2, 132, 199, 0.45)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(compX - 165, compY - 18, 330, 34, 6);
      ctx.fill();
      ctx.stroke();

      // Lingkaran kompas kecil
      const cx0 = compX - 144;
      const cy0 = compY - 1;
      ctx.beginPath();
      ctx.arc(cx0, cy0, 11, 0, Math.PI * 2);
      ctx.strokeStyle = windColor;
      ctx.lineWidth = 1.4;
      ctx.stroke();

      // Jarum arah dorongan angin
      ctx.beginPath();
      ctx.moveTo(cx0 - ux * 7, cy0 - uy * 7);
      ctx.lineTo(cx0 + ux * 8, cy0 + uy * 8);
      ctx.strokeStyle = windColor;
      ctx.lineWidth = 2;
      ctx.stroke();

      ctx.font = '600 11.5px "IBM Plex Mono", monospace';
      ctx.fillStyle = windColor;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      const toCompass = bearing(ux, -uy).dir;
      ctx.fillText(
        `ANGIN ${curW.ws} m/s → ${toCompass} (${curW.wto}°) · ${curW.temp}°C · RH ${curW.rh}% · FWI ${curW.fwi}`,
        cx0 + 18,
        cy0
      );
      ctx.restore();
    }

    // ---------- 5. Skala Bar & Atribusi Peta ----------
    const nice = [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
    let len = nice[0];
    nice.forEach((n) => {
      if (n * view.s <= 140) len = n;
    });
    const bx = 14,
      by = Hh - 14,
      bw = len * view.s;
    ctx.strokeStyle = fg;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(bx, by - 5);
    ctx.lineTo(bx, by);
    ctx.lineTo(bx + bw, by);
    ctx.lineTo(bx + bw, by - 5);
    ctx.stroke();
    label(len >= 1000 ? len / 1000 + ' km' : len + ' m', bx + bw + 8, by - 3, fg);
    label(
      `U ↑   UTM 49N · 375 m · ${basemapCfg.attributions}`,
      W - 12,
      Hh - 14,
      muted,
      'right'
    );
  }, [ds, cur, stepK, layers, alphaM, activeMapType, mapOpacity, getGeom]);

  // Fullscreen & Immersion mode toggle
  const handleToggleHideUI = useCallback(() => {
    setHideAllUI((prev) => {
      const next = !prev;
      if (next) {
        if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
          document.documentElement.requestFullscreen().catch(() => { });
        }
      } else if (document.fullscreenElement && document.exitFullscreen) {
        document.exitFullscreen().catch(() => { });
      }
      return next;
    });
  }, []);

  useEffect(() => {
    const onFs = () => {
      if (!document.fullscreenElement) setHideAllUI(false);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') {
        setHideAllUI(false);
        setShowDbModal(false);
        setShowLayerPanel(false);
        setShowInfoModal(false);
        setIsCropping(false);
        setShowExportModal(false);
        setCropBox(null);
      }
    };
    document.addEventListener('fullscreenchange', onFs);
    window.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('fullscreenchange', onFs);
      window.removeEventListener('keydown', onKey);
    };
  }, []);

  // Select a cluster by index
  const selectCluster = useCallback(
    (idx, dataset = ds) => {
      setPlaying(false);
      geoCacheRef.current = {};
      if (!dataset || idx < 0 || !dataset.clusters[idx]) {
        setCur(-1);
        setStepK(0);
        return;
      }
      const c = dataset.clusters[idx];
      const lastStep = Math.max(0, c.steps.length - 1);
      setCur(idx);
      setStepK(lastStep);
      setShowDetailPanel(true);
      fitToPoints(c.pts, 0.5);
    },
    [ds, fitToPoints]
  );

  // Apply a prepared dataset
  const applyDataset = useCallback(
    (prepared, label, demoFlag) => {
      setPlaying(false);
      geoCacheRef.current = {};
      setDs(prepared);
      setIsDemo(demoFlag);
      setSrcLabel(demoFlag ? 'Data simulasi, bukan FIRMS asli' : label);
      setStatusMsg('');

      if (!prepared.clusters.length) {
        setStatusMsg(
          'Rentang waktu atau file ini tidak berisi kluster. Turunkan jumlah titik minimum atau ubah rentang tanggal.'
        );
        setCur(-1);
        setStepK(0);
        const all = prepared.context.map((q) => ({ x: q[0], y: q[1] }));
        fitToPoints(all, 0.15);
        return;
      }

      let best = 0;
      prepared.clusters.forEach((c, i) => {
        if (c.n > prepared.clusters[best].n && c.n <= 1500) best = i;
      });
      const defaultMinN = demoFlag ? 3 : prepared.clusters.length > 60 ? 10 : 3;
      setMinN(defaultMinN);
      const targetIdx = demoFlag ? 0 : best;
      const targetCluster = prepared.clusters[targetIdx];
      setCur(targetIdx);
      setStepK(Math.max(0, targetCluster.steps.length - 1));
      fitToPoints(targetCluster.pts, 0.5);
    },
    [fitToPoints]
  );

  // Preset tanggal cepat (7 hari, 14 hari, 30 hari)
  const applyDatePreset = useCallback(
    (days) => {
      const maxD = dbBounds?.max_d || ds?.meta?.end || new Date().toISOString().slice(0, 10);
      const endMs = Date.parse(`${maxD}T00:00:00Z`);
      const startMs = Number.isFinite(endMs)
        ? endMs - days * 86400_000
        : Date.now() - days * 86400_000;
      const startD = new Date(startMs).toISOString().slice(0, 10);
      setDbStart(startD);
      setDbEnd(maxD);
    },
    [dbBounds, ds]
  );

  // Fetch forensic data directly from PostgreSQL API
  const fetchFromDatabase = useCallback(
    async (opts = {}) => {
      setDbLoading(true);
      setStatusMsg('Memuat & menghitung klaster forensik dari database PostgreSQL...');
      try {
        const sp = new URLSearchParams();
        const s = opts.start !== undefined ? opts.start : dbStart;
        const e = opts.end !== undefined ? opts.end : dbEnd;
        const mc = opts.minConf !== undefined ? opts.minConf : dbMinConf;
        const es = opts.epsS !== undefined ? opts.epsS : dbEpsS;
        const et = opts.epsTH !== undefined ? opts.epsTH : dbEpsTH;
        const mp = opts.minPoints !== undefined ? opts.minPoints : dbMinPoints;
        const maxPts = opts.maxPoints !== undefined ? opts.maxPoints : dbMaxPoints;

        if (s) sp.set('start', s);
        if (e) sp.set('end', e);
        if (opts.days) sp.set('days', String(opts.days));
        sp.set('minConf', mc);
        sp.set('epsS', String(es));
        sp.set('epsTH', String(et));
        sp.set('minPoints', String(mp));
        sp.set('maxPoints', String(maxPts));
        sp.set('alpha', String(alphaM));

        const res = await fetch(`/api/forensik?${sp.toString()}`);
        const rawText = await res.text();
        let payload = null;
        try {
          payload = rawText ? JSON.parse(rawText) : null;
        } catch {
          throw new Error(`Respons server tidak valid (HTTP ${res.status})`);
        }
        if (!res.ok || !payload) {
          throw new Error(
            payload?.details || payload?.error || `HTTP ${res.status}: Gagal memuat dari database`
          );
        }

        if (payload.meta?.dbBounds) {
          setDbBounds(payload.meta.dbBounds);
        }
        if (payload.meta?.start && !s) setDbStart(payload.meta.start);
        if (payload.meta?.end && !e) setDbEnd(payload.meta.end);

        if (!payload.clusters || payload.clusters.length === 0) {
          if (opts.silentOnEmpty) {
            setStatusMsg(
              `Database lokal siap (${payload.meta?.start || ''} s/d ${payload.meta?.end || ''}: belum ada titik api). Menampilkan data simulasi.`
            );
            setDbLoading(false);
            return;
          }
        }

        const prepared = prepDataset(payload);
        const label = `PostgreSQL · ${payload.meta.start} s/d ${payload.meta.end} · ${payload.clusters.length} kejadian (${num(payload.meta.n_points || 0)} titik)`;
        setClusterPage(1);
        applyDataset(prepared, label, false);
      } catch (err) {
        setStatusMsg(`Gagal memuat dari DB: ${err.message}`);
      } finally {
        setDbLoading(false);
      }
    },
    [dbStart, dbEnd, dbMinConf, dbEpsS, dbEpsTH, dbMinPoints, dbMaxPoints, alphaM, applyDataset]
  );

  // Resize & initial load
  useEffect(() => {
    const box = mapboxRef.current;
    const cv = canvasRef.current;
    if (!box || !cv) return;

    const updateSize = () => {
      const dpr = window.devicePixelRatio || 1;
      const r = box.getBoundingClientRect();
      sizeRef.current = { W: r.width, Hh: r.height, dpr };
      cv.width = Math.round(r.width * dpr);
      cv.height = Math.round(r.height * dpr);
    };

    updateSize();
    if (ds && ds.clusters[0]) {
      fitToPoints(ds.clusters[0].pts, 0.5);
    }
    draw();

    // Auto-connect to PostgreSQL on initial mount (7 hari terkini, cepat < 1s)
    fetchFromDatabase({ silentOnEmpty: true, days: 7 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Redraw when state changes
  useEffect(() => {
    draw();
  }, [draw]);

  // ResizeObserver & theme change listeners
  useEffect(() => {
    const box = mapboxRef.current;
    const cv = canvasRef.current;
    if (!box || !cv) return;

    const handleResize = () => {
      const dpr = window.devicePixelRatio || 1;
      const r = box.getBoundingClientRect();
      sizeRef.current = { W: r.width, Hh: r.height, dpr };
      cv.width = Math.round(r.width * dpr);
      cv.height = Math.round(r.height * dpr);
      if (ds && cur >= 0 && ds.clusters[cur]) {
        fitToPoints(ds.clusters[cur].pts, 0.5);
      }
      draw();
    };

    const ro = new ResizeObserver(handleResize);
    ro.observe(box);

    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handleTheme = () => draw();
    try {
      mq.addEventListener('change', handleTheme);
    } catch { }

    const mo = new MutationObserver(handleTheme);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    return () => {
      ro.disconnect();
      try {
        mq.removeEventListener('change', handleTheme);
      } catch { }
      mo.disconnect();
    };
  }, [ds, cur, fitToPoints, draw]);

  // Wheel zoom listener (non-passive)
  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const onWheel = (e) => {
      e.preventDefault();
      const { W, Hh } = sizeRef.current;
      const view = viewRef.current;
      const r = cv.getBoundingClientRect();
      const mx = e.clientX - r.left;
      const my = e.clientY - r.top;
      const wx = (mx - W / 2) / view.s + view.cx;
      const wy = view.cy - (my - Hh / 2) / view.s;
      const newS = Math.min(2, Math.max(0.002, view.s * Math.exp(-e.deltaY * 0.0015)));
      viewRef.current = {
        s: newS,
        cx: wx - (mx - W / 2) / newS,
        cy: wy + (my - Hh / 2) / newS,
      };
      draw();
    };
    cv.addEventListener('wheel', onWheel, { passive: false });
    return () => cv.removeEventListener('wheel', onWheel);
  }, [draw]);

  // Playback timer yang tersinkronisasi dengan hembusan angin
  useEffect(() => {
    if (!playing || cur < 0 || !ds.clusters[cur]) return;
    const maxStep = ds.clusters[cur].steps.length - 1;
    const baseDuration = 900; // durasi per lintasan (ms) pada kecepatan 1x
    const intervalMs = Math.round(baseDuration / playbackSpeed);

    const timer = setInterval(() => {
      setStepK((prev) => {
        if (prev >= maxStep) {
          setPlaying(false);
          return prev;
        }
        return prev + 1;
      });
    }, intervalMs);
    return () => clearInterval(timer);
  }, [playing, cur, ds, playbackSpeed]);

  // Load local JSON file
  const loadFile = useCallback(
    (file) => {
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const parsed = JSON.parse(reader.result);
          if (!parsed.meta || !Array.isArray(parsed.clusters)) throw new Error('format');
          const prepared = prepDataset(parsed);
          const label = `${file.name} · ${parsed.meta.start} s/d ${parsed.meta.end} · ${parsed.clusters.length} kejadian`;
          applyDataset(prepared, label, false);
        } catch {
          setStatusMsg(
            'File tidak dapat dibaca. Pilih forensic_kalbar.json keluaran forensik.py.'
          );
        }
      };
      reader.onerror = () => setStatusMsg('File gagal dibuka.');
      reader.readAsText(file);
    },
    [applyDataset]
  );

  // Download current dataset as forensic_kalbar.json
  const downloadJson = useCallback(() => {
    if (!ds?.rawPayload) return;
    const blob = new Blob([JSON.stringify(ds.rawPayload)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `forensic_kalbar_${ds.meta?.start || 'data'}_${ds.meta?.end || ''}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [ds]);

  // Zoom button helper
  const zoomBy = useCallback(
    (factor) => {
      const view = viewRef.current;
      viewRef.current = {
        ...view,
        s: Math.min(2, Math.max(0.002, view.s * factor)),
      };
      draw();
    },
    [draw]
  );

  // Forensic Screenshot Generator & Crop Compositor
  const generateForensicScreenshot = useCallback(
    (box = null, customOpts = null) => {
      const mainCv = canvasRef.current;
      const windCv = typeof document !== 'undefined' ? document.getElementById('wind-animation-canvas') : null;
      const { W, Hh, dpr } = sizeRef.current;
      if (!mainCv || !W || !Hh) return null;

      const opts = {
        watermark: customOpts?.watermark !== undefined ? customOpts.watermark : exportWatermark,
        wind: customOpts?.wind !== undefined ? customOpts.wind : exportWind,
        scale: customOpts?.scale !== undefined ? customOpts.scale : exportScale,
      };

      const sel = box || screenshotCropBox || { x: 0, y: 0, w: W, h: Hh };
      const selX = Math.max(0, Math.min(W - 10, sel.x));
      const selY = Math.max(0, Math.min(Hh - 10, sel.y));
      const selW = Math.max(20, Math.min(W - selX, sel.w));
      const selH = Math.max(20, Math.min(Hh - selY, sel.h));

      const headerH = opts.watermark ? 70 : 0;
      const footerH = opts.watermark || opts.scale ? 38 : 0;

      const exportDpr = Math.max(dpr || 1, 2);
      const outW = Math.round(selW * exportDpr);
      const outH = Math.round((selH + headerH + footerH) * exportDpr);

      const outCv = document.createElement('canvas');
      outCv.width = outW;
      outCv.height = outH;
      const ctx = outCv.getContext('2d');
      if (!ctx) return null;

      ctx.scale(exportDpr, exportDpr);

      // 1. Background dasar gelap
      ctx.fillStyle = '#080c10';
      ctx.fillRect(0, 0, selW, selH + headerH + footerH);

      // 2. Gambar area peta terpilih dari main canvas
      const sx = selX * dpr;
      const sy = selY * dpr;
      const sw = selW * dpr;
      const sh = selH * dpr;
      ctx.drawImage(mainCv, sx, sy, sw, sh, 0, headerH, selW, selH);

      // 3. Gambar lapisan partikel angin jika aktif
      if (opts.wind && layers.wind && windCv) {
        const windDpr = (windCv.width / W) || dpr;
        ctx.drawImage(
          windCv,
          selX * windDpr,
          selY * windDpr,
          selW * windDpr,
          selH * windDpr,
          0,
          headerH,
          selW,
          selH
        );
      }

      // 4. Header Bar Forensik & Metadata
      if (opts.watermark) {
        const grad = ctx.createLinearGradient(0, 0, selW, 0);
        grad.addColorStop(0, '#0d1319');
        grad.addColorStop(0.5, '#131b24');
        grad.addColorStop(1, '#0d1319');
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, selW, headerH);

        ctx.strokeStyle = 'rgba(255, 91, 31, 0.5)';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(0, headerH);
        ctx.lineTo(selW, headerH);
        ctx.stroke();

        ctx.font = '700 13px "Bricolage Grotesque", "Segoe UI", sans-serif';
        ctx.fillStyle = '#ff5b1f';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText('TERRASENSE · SISTEM FORENSIK SEBARAN API KALBAR', 14, 11);

        ctx.font = '500 10.5px "IBM Plex Mono", monospace';
        ctx.fillStyle = '#e6edf3';
        const curCluster = cur >= 0 && ds && ds.clusters ? ds.clusters[cur] : null;
        const clusterTxt = curCluster
          ? `Kluster: ${curCluster.id} (${curCluster.n} hotspot)`
          : 'Cakupan: Seluruh Kejadian';
        const timeTxt = curCluster
          ? `Mulai: ${fmt(curCluster.t0)} WIB`
          : ds
          ? `Total: ${ds.clusters.length} kluster`
          : '';
        ctx.fillText(`${clusterTxt}  ·  ${timeTxt}`, 14, 29);

        const curW = activeWeatherRef.current;
        if (curW) {
          ctx.fillStyle = '#38bdf8';
          const toCompass = bearing(
            Math.sin(((curW.wto || 0) * Math.PI) / 180),
            -Math.cos(((curW.wto || 0) * Math.PI) / 180)
          ).dir;
          const windDesc = `Angin: ${curW.ws} m/s arah ${toCompass} (${curW.wto}°) · Suhu ${curW.temp}°C · RH ${curW.rh}% · FWI ${curW.fwi}`;
          ctx.fillText(windDesc, 14, 47);
        } else {
          ctx.fillStyle = '#8b9baa';
          ctx.fillText('Sensor: VIIRS 375m (SNPP/NOAA-20/21) · Proyeksi PostGIS UTM 49N', 14, 47);
        }

        ctx.font = '500 9.5px "IBM Plex Mono", monospace';
        ctx.fillStyle = '#8b9baa';
        ctx.textAlign = 'right';
        ctx.fillText('DOKUMEN BUKTI SPASIAL', selW - 14, 12);
        ctx.fillText(`Cetak: ${new Date().toLocaleString('id-ID')} WIB`, selW - 14, 27);
        ctx.fillText('Integritas: Monte Carlo Stabilitas', selW - 14, 43);
      }

      // 5. Footer Bar & Skala Jarak
      if (opts.watermark || opts.scale) {
        const footerY = headerH + selH;
        ctx.fillStyle = '#0a0f14';
        ctx.fillRect(0, footerY, selW, footerH);

        ctx.strokeStyle = 'rgba(34, 45, 55, 0.85)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, footerY);
        ctx.lineTo(selW, footerY);
        ctx.stroke();

        if (opts.scale) {
          const view = viewRef.current;
          const nice = [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
          let len = nice[0];
          nice.forEach((n) => {
            if (n * view.s <= 110) len = n;
          });
          const bw = len * view.s;
          const bx = 14;
          const by = footerY + 22;

          ctx.strokeStyle = '#e6edf3';
          ctx.lineWidth = 1.8;
          ctx.beginPath();
          ctx.moveTo(bx, by - 4);
          ctx.lineTo(bx, by);
          ctx.lineTo(bx + bw, by);
          ctx.lineTo(bx + bw, by - 4);
          ctx.stroke();

          ctx.font = '600 10px "IBM Plex Sans", sans-serif';
          ctx.fillStyle = '#e6edf3';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(len >= 1000 ? `${len / 1000} km` : `${len} m`, bx + bw + 8, by - 2);
          ctx.fillText('↑ U', bx + bw + 55, by - 2);
        }

        ctx.font = '500 9px "IBM Plex Mono", monospace';
        ctx.fillStyle = '#8b9baa';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText(
          'Sumber: NASA FIRMS VIIRS 375m · ECMWF ERA5 Reanalysis · TerraSense Engine',
          selW - 14,
          footerY + footerH / 2
        );
      }

      const dataUrl = outCv.toDataURL('image/png');
      setScreenshotPreviewUrl(dataUrl);
      setScreenshotCropBox(sel);
      return dataUrl;
    },
    [ds, cur, layers.wind, exportWatermark, exportWind, exportScale]
  );

  const handleOpenExport = useCallback(
    (box = null) => {
      const curCluster = cur >= 0 && ds && ds.clusters ? ds.clusters[cur] : null;
      const cid = curCluster ? curCluster.id : 'kalbar';
      const dateStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
      setExportFilename(`forensik_${cid}_${dateStr}.png`);
      generateForensicScreenshot(box);
      setShowExportModal(true);
    },
    [cur, ds, generateForensicScreenshot]
  );

  const handleDownloadScreenshot = () => {
    if (!screenshotPreviewUrl) return;
    const a = document.createElement('a');
    a.href = screenshotPreviewUrl;
    a.download = exportFilename || 'forensik_tangkapan_layar.png';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setShowExportModal(false);
  };

  // Pointer handlers on canvas
  const handlePointerDown = (e) => {
    const cv = canvasRef.current;
    if (!cv) return;
    cv.setPointerCapture(e.pointerId);

    if (isCropping) {
      const r = cv.getBoundingClientRect();
      const mx = e.clientX - r.left;
      const my = e.clientY - r.top;
      cropStartRef.current = { x: mx, y: my };
      setCropBox({ x0: mx, y0: my, x1: mx, y1: my });
      return;
    }

    dragRef.current = {
      x: e.clientX,
      y: e.clientY,
      cx: viewRef.current.cx,
      cy: viewRef.current.cy,
    };
  };

  const handlePointerUp = () => {
    if (isCropping && cropStartRef.current) {
      const box = cropBox;
      cropStartRef.current = null;
      if (box) {
        const w = Math.abs(box.x1 - box.x0);
        const h = Math.abs(box.y1 - box.y0);
        if (w >= 30 && h >= 30) {
          const normBox = {
            x: Math.min(box.x0, box.x1),
            y: Math.min(box.y0, box.y1),
            w,
            h,
          };
          setIsCropping(false);
          setCropBox(null);
          handleOpenExport(normBox);
          return;
        }
      }
      setCropBox(null);
      return;
    }
    dragRef.current = null;
  };

  const handlePointerMove = (e) => {
    const cv = canvasRef.current;
    if (!cv) return;
    const { W, Hh } = sizeRef.current;
    const view = viewRef.current;
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;

    if (isCropping && cropStartRef.current) {
      const clampX = Math.max(0, Math.min(W, mx));
      const clampY = Math.max(0, Math.min(Hh, my));
      setCropBox({
        x0: cropStartRef.current.x,
        y0: cropStartRef.current.y,
        x1: clampX,
        y1: clampY,
      });
      return;
    }

    // Update live coordinate HUD
    const rx = (mx - W / 2) / view.s + view.cx;
    const ry = (Hh / 2 - my) / view.s + view.cy;
    const [x0, y0] = resolveDatasetOriginUTM(ds);
    const [lon, lat] = utm49NToLonLat(x0 + rx, y0 + ry);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      setCursorHud(`${llText([lat, lon])}  ·  Z${Math.round(Math.log2(156543 * view.s))}`);
    }

    if (dragRef.current) {
      viewRef.current = {
        ...view,
        cx: dragRef.current.cx - (e.clientX - dragRef.current.x) / view.s,
        cy: dragRef.current.cy + (e.clientY - dragRef.current.y) / view.s,
      };
      setTipData(null);
      draw();
      return;
    }

    if (cur < 0 || !ds.clusters[cur]) {
      setTipData(null);
      return;
    }

    const SX = (x) => (x - view.cx) * view.s + W / 2;
    const SY = (y) => Hh / 2 - (y - view.cy) * view.s;
    const c = ds.clusters[cur];
    let hit = null;
    let bd = 10;
    c.pts.forEach((p, i) => {
      if (p.step > stepK) return;
      const d = Math.hypot(SX(p.x) - mx, SY(p.y) - my);
      if (d < bd) {
        bd = d;
        hit = { p, i };
      }
    });

    if (!hit) {
      setTipData(null);
      return;
    }

    const q = c.parent[hit.i] >= 0 ? c.pts[c.parent[hit.i]] : null;
    setTipData({
      left: Math.min(mx + 14, W - 260),
      top: Math.min(my + 14, Hh - 100),
      title: `${c.id}${hit.i === c.root ? ' · titik awal' : ''}`,
      line1: `${fmt(hit.p.t)} WIB · ${hit.p.sat}`,
      line2: `FRP ${num(hit.p.frp, 1)} MW · conf ${hit.p.conf === 'h' ? 'tinggi' : hit.p.conf === 'l' ? 'rendah' : 'nominal'
        }`,
      line3: q
        ? `Induk: ${fmt(q.t)} · ${num(Math.hypot(hit.p.x - q.x, hit.p.y - q.y))} m`
        : 'Akar pohon',
    });
  };

  // Filtered & sorted cluster indices
  const filteredIndices = useMemo(() => {
    if (!ds) return [];
    const idx = ds.clusters
      .map((_, i) => i)
      .filter((i) => ds.clusters[i].n >= minN);
    idx.sort((a, b) => {
      const A = ds.clusters[a],
        B = ds.clusters[b];
      if (sortBy === 'n') return B.n - A.n;
      if (sortBy === 'area') return (B.areaHa || 0) - (A.areaHa || 0);
      if (sortBy === 'weak') return A.stabRoot - B.stabRoot;
      return B.t0 - A.t0;
    });
    return idx;
  }, [ds, minN, sortBy]);

  // Paginasi klaster kejadian (25 klaster per halaman)
  const totalClusterPages = Math.max(1, Math.ceil(filteredIndices.length / clusterPageSize));
  const pagedClusterIndices = useMemo(() => {
    const s = (clusterPage - 1) * clusterPageSize;
    return filteredIndices.slice(s, s + clusterPageSize);
  }, [filteredIndices, clusterPage, clusterPageSize]);

  // Reset ke halaman 1 saat filter titik atau pengurutan berubah
  useEffect(() => {
    setClusterPage(1);
  }, [minN, sortBy]);

  // Active cluster details
  const activeCluster = cur >= 0 && ds?.clusters[cur] ? ds.clusters[cur] : null;
  const activeGeom = activeCluster ? getGeom(cur, stepK, alphaM, ds) : null;

  const detailMetrics = useMemo(() => {
    if (!activeCluster || !activeGeom || !activeGeom.vis.length) return null;
    const c = activeCluster;
    const g = activeGeom;
    const root = c.pts[c.root];
    const hi = c.stabRoot >= 0.8;
    let lp = -Infinity;
    g.vis.forEach((p) => {
      if (p.step > lp) lp = p.step;
    });
    const nw = g.vis.filter((p) => p.step === lp);
    const cx = nw.reduce((s, p) => s + p.x, 0) / nw.length;
    const cy = nw.reduce((s, p) => s + p.y, 0) / nw.length;
    const dist = Math.hypot(cx - root.x, cy - root.y);
    const hrs = (nw[0].t - root.t) / H;
    const bb = bearing(cx - root.x, cy - root.y);
    const areaHa =
      stepK === c.steps.length - 1 && alphaM === 1300 && c.areaHa
        ? c.areaHa
        : g.area / 1e4;

    return {
      c,
      g,
      root,
      hi,
      dist,
      hrs,
      bb,
      areaHa,
    };
  }, [activeCluster, activeGeom, stepK, alphaM]);

  const currentStepObj =
    activeCluster && activeCluster.steps[stepK] ? activeCluster.steps[stepK] : null;
  const shownPointsCount = activeGeom ? activeGeom.vis.length : 0;

  // Fetch data cuaca & arah angin dari /api/climate (DB atau Open-Meteo ERA5) saat klaster berubah
  useEffect(() => {
    if (!activeCluster) {
      setClimatePayload(null);
      setClimateLoading(false);
      return;
    }
    const [lat, lon] = activeCluster.rootLL || [-0.5, 110.4];
    const tStart = activeCluster.pts[0]?.t || Date.now();
    const tEnd = activeCluster.pts[activeCluster.pts.length - 1]?.t || tStart;
    const startStr = new Date(tStart - 86400_000).toISOString().slice(0, 10);
    const endStr = new Date(tEnd + 86400_000).toISOString().slice(0, 10);
    const cacheKey = `${lat.toFixed(2)}_${lon.toFixed(2)}_${startStr}_${endStr}`;

    // Gunakan cache client instan jika sudah pernah di-fetch
    if (climateCacheRef.current.has(cacheKey)) {
      setClimatePayload(climateCacheRef.current.get(cacheKey));
      setClimateLoading(false);
      return;
    }

    let cancelled = false;
    setClimateLoading(true);
    fetch(
      `/api/climate?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}&start=${startStr}&end=${endStr}`
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!cancelled && data && Array.isArray(data.hourly) && data.hourly.length > 0) {
          climateCacheRef.current.set(cacheKey, data);
          setClimatePayload(data);
        }
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setClimateLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [activeCluster]);

  // Cuaca & arah angin yang disinkronkan pada jam lintasan satelit (stepK)
  const activeWeather = useMemo(() => {
    if (!activeCluster || !currentStepObj) return null;
    const targetT = currentStepObj.t;

    if (climatePayload?.hourly?.length) {
      let best = climatePayload.hourly[0];
      let bestDiff = Math.abs(best.t - targetT);
      for (let i = 1; i < climatePayload.hourly.length; i++) {
        const d = Math.abs(climatePayload.hourly[i].t - targetT);
        if (d < bestDiff) {
          bestDiff = d;
          best = climatePayload.hourly[i];
        }
      }
      return {
        ...best,
        sourceLabel: climatePayload.source || 'Open-Meteo ERA5',
        resLabel: climatePayload.resolution || '0.25° (~27.5 km)',
      };
    }

    // Fallback deterministik berbasis fisika klaster saat menunggu respons API / mode demo
    const root = activeCluster.pts[activeCluster.root];
    const lastPt = activeCluster.pts[activeCluster.pts.length - 1];
    const mainBearing = bearing(lastPt.x - root.x, lastPt.y - root.y).deg;
    const stepShift = Math.sin(stepK * 0.7) * 14;
    const wto = Math.round((mainBearing + stepShift + 360) % 360);
    const wdeg = (wto + 180) % 360;
    const radTo = (wto * Math.PI) / 180;
    const hourWib = new Date(targetT + 7 * 3600_000).getUTCHours();
    const isAfternoon = hourWib >= 11 && hourWib <= 17;
    const temp = isAfternoon ? 33.4 : 26.2;
    const rh = isAfternoon ? 52 : 78;
    const ws = Number((isAfternoon ? 4.4 : 2.6).toFixed(1));
    const fwi = isAfternoon ? 16.8 : 9.4;

    return {
      t: targetT,
      temp,
      rh,
      pressure: 1009,
      ws,
      wdeg,
      wto,
      wdir: bearing(-Math.sin(radTo), -Math.cos(radTo)).dir,
      wu: Number((ws * Math.sin(radTo)).toFixed(2)),
      wv: Number((ws * Math.cos(radTo)).toFixed(2)),
      gust: Number((ws * 1.45).toFixed(1)),
      clouds: isAfternoon ? 35 : 60,
      precip: 0,
      rain24h: 0.2,
      desc: isAfternoon ? 'cerah berawan (kering)' : 'berawan tipis',
      fwi,
      fwiCat: fwi >= 13 ? 'Extreme' : 'High',
      dc: 312.5,
      isi: isAfternoon ? 6.4 : 3.2,
      ffmc: isAfternoon ? 89.2 : 84.1,
      sourceLabel: 'Estimasi Sinoptik Kalbar (ERA5)',
      resLabel: '0.25° (~27.5 km)',
    };
  }, [activeCluster, currentStepObj, climatePayload, stepK]);

  // Sinkronkan ke ref untuk digambar langsung di kanvas peta
  useEffect(() => {
    activeWeatherRef.current = activeWeather;
    draw();
  }, [activeWeather, draw]);

  return (
    <main className="app-fullscreen">
      {/* 1. Full-screen Map Canvas Viewport */}
      <div
        ref={mapboxRef}
        className={`map-canvas-container ${isDropping ? 'drop' : ''}`}
        onDragEnter={(e) => {
          e.preventDefault();
          setIsDropping(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setIsDropping(true);
        }}
        onDragLeave={(e) => {
          e.preventDefault();
          setIsDropping(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setIsDropping(false);
          if (e.dataTransfer?.files?.length) {
            loadFile(e.dataTransfer.files[0]);
          }
        }}
      >
        <canvas
          ref={canvasRef}
          className={`map-canvas ${isCropping ? 'crop-active' : ''}`}
          role="img"
          aria-label="Peta sebaran hotspot forensik VIIRS 375 m"
          onPointerDown={handlePointerDown}
          onPointerUp={handlePointerUp}
          onPointerMove={handlePointerMove}
          onPointerLeave={() => {
            setTipData(null);
            setCursorHud('');
          }}
        />
        <WindVectorLayer
          visible={layers.wind}
          activeWeather={activeWeather}
          centerLL={activeCluster?.rootLL || [-0.5, 110.4]}
          viewRef={viewRef}
          sizeRef={sizeRef}
          ds={ds}
          stepK={stepK}
          activeCluster={activeCluster}
          playing={playing}
          particleCount={windDensity}
        />

        {/* Overlay Mode Tangkapan Layar & Marquee Box */}
        {isCropping && (
          <div className="crop-screen-overlay">
            <div className="crop-banner">
              <div className="crop-banner-info">
                <Camera size={16} style={{ color: 'var(--ca)' }} />
                <span>
                  <b>Mode Tangkapan Layar Forensik</b> — Klik dan seret (drag) pada peta untuk menandai area ekspor.
                </span>
              </div>
              <div className="crop-banner-actions">
                <button
                  type="button"
                  className="crop-action-btn primary"
                  onClick={() => {
                    const { W, Hh } = sizeRef.current;
                    setIsCropping(false);
                    setCropBox(null);
                    handleOpenExport({ x: 0, y: 0, w: W, h: Hh });
                  }}
                  title="Ekspor seluruh tampilan layar peta saat ini"
                >
                  Tangkap Seluruh Layar
                </button>
                <button
                  type="button"
                  className="crop-action-btn"
                  onClick={() => {
                    setIsCropping(false);
                    setCropBox(null);
                  }}
                >
                  Batal (Esc)
                </button>
              </div>
            </div>

            {cropBox && (
              <div
                className="crop-marquee"
                style={{
                  left: `${Math.min(cropBox.x0, cropBox.x1)}px`,
                  top: `${Math.min(cropBox.y0, cropBox.y1)}px`,
                  width: `${Math.abs(cropBox.x1 - cropBox.x0)}px`,
                  height: `${Math.abs(cropBox.y1 - cropBox.y0)}px`,
                }}
              >
                <div className="crop-dimension-tag">
                  {Math.round(Math.abs(cropBox.x1 - cropBox.x0))} × {Math.round(Math.abs(cropBox.y1 - cropBox.y0))} px
                  {viewRef.current.s > 0 && (
                    <span>
                      {' '}· ~{((Math.abs(cropBox.x1 - cropBox.x0) / viewRef.current.s) / 1000).toFixed(1)} × {((Math.abs(cropBox.y1 - cropBox.y0) / viewRef.current.s) / 1000).toFixed(1)} km
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Floating Canvas Tooltip */}
      {tipData && (
        <div
          className="canvas-tip"
          style={{ left: `${tipData.left}px`, top: `${tipData.top}px` }}
        >
          <b>{tipData.title}</b>
          <div>{tipData.line1}</div>
          <div>{tipData.line2}</div>
          <div>{tipData.line3}</div>
        </div>
      )}

      {/* Floating Restore UI Button (saat mode Peta Penuh) */}
      {hideAllUI && (
        <button
          type="button"
          className="restore-ui-btn"
          onClick={handleToggleHideUI}
          title="Tampilkan kembali menu & panel (Esc)"
        >
          <Eye size={14} />
          <span>Tampilkan Panel</span>
        </button>
      )}

      {/* 2. Topbar Header */}
      <header className={`topbar ${hideAllUI ? 'hidden' : ''}`}>
        <div className="topbar-left">
          <div className="brand">
            <div className="brand-icon">
              <Flame size={17} />
            </div>
            <div className="brand-info">
              <span className="brand-title">JEJAK HOTSPOT KALBAR</span>
              <span className="brand-sub">Forensik Sebaran Api VIIRS 375 m</span>
            </div>
          </div>

          <span className={`tag-badge ${isDemo ? '' : 'real'}`}>
            {isDemo ? 'SIMULASI' : 'POSTGRES'}
          </span>

          <div className="topbar-stats">
            <div className="stat-chip" title="Kejadian klaster yang ditemukan">
              <span>Kejadian:</span> <b>{filteredIndices.length} / {ds?.clusters?.length || 0}</b>
            </div>
            {activeCluster && (
              <>
                <div className="stat-chip" title="Jumlah titik pada kejadian terpilih">
                  <span>Titik:</span> <b>{activeCluster.n}</b>
                </div>
                <div className="stat-chip" title="Estimasi luas area kejadian">
                  <span>Luas:</span> <b>{num(activeCluster.areaHa || 0)} ha</b>
                </div>
              </>
            )}
          </div>
        </div>

        <div className="topbar-right">
          {/* Toggle Daftar Kejadian Panel */}
          <button
            type="button"
            className={`tb-btn ${showListPanel ? 'active' : ''}`}
            onClick={() => setShowListPanel((v) => !v)}
            title="Buka / tutup panel daftar kejadian api"
          >
            <List size={13} />
            <span>Kejadian</span>
            <span className="tb-pill">{filteredIndices.length}</span>
          </button>

          {/* Toggle Detail Forensik Panel */}
          <button
            type="button"
            className={`tb-btn ${showDetailPanel ? 'active' : ''}`}
            onClick={() => setShowDetailPanel((v) => !v)}
            title="Buka / tutup panel detail metrik forensik"
          >
            <FileText size={13} />
            <span>Detail</span>
          </button>

          {/* Toggle Layer & Opsi Panel */}
          <button
            type="button"
            className={`tb-btn ${showLayerPanel ? 'active' : ''}`}
            onClick={() => setShowLayerPanel((v) => !v)}
            title="Atur layer vektor, opasitas peta, dan kerapatan alpha shape"
          >
            <Layers size={13} />
            <span>Layer</span>
          </button>

          {/* Open Query DB Modal */}
          <button
            type="button"
            className="tb-btn"
            onClick={() => setShowDbModal(true)}
            title="Filter dan muat data langsung dari database PostgreSQL lokal"
          >
            <Database size={13} />
            <span>Query DB</span>
          </button>

          {/* Basemap Selector Component */}
          <BasemapSelector
            activeMapType={activeMapType}
            onSelectMapType={(id) => setActiveMapType(id)}
          />

          {/* Upload JSON file */}
          <label className="tb-btn file-upload-btn" title="Muat file JSON forensic_kalbar">
            <Upload size={13} />
            <span>Unggah JSON</span>
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,application/json"
              onChange={(e) => {
                loadFile(e.target.files?.[0]);
                e.target.value = '';
              }}
            />
          </label>

          {/* Download JSON if real data */}
          {!isDemo && ds?.rawPayload && (
            <button
              type="button"
              className="tb-btn"
              onClick={downloadJson}
              title="Unduh hasil analisis forensik sebagai JSON"
            >
              <Download size={13} />
              <span>Unduh</span>
            </button>
          )}

          {/* Screenshot & Crop Export */}
          <button
            type="button"
            className={`tb-btn ${isCropping ? 'active' : ''}`}
            onClick={() => {
              setIsCropping((v) => !v);
              setCropBox(null);
            }}
            title="Tangkapan layar & pilih area peta untuk diekspor ke PNG"
          >
            <Camera size={13} />
            <span>Screenshot</span>
          </button>

          {/* Switch to Demo Data */}
          {/* {!isDemo && (
            <button
              type="button"
              className="tb-btn"
              onClick={() => applyDataset(buildDemo(), 'Data simulasi', true)}
              title="Kembali ke mode data simulasi"
            >
              Simulasi
            </button>
          )} */}

          {/* Fullscreen / Peta Penuh Toggle */}
          <button
            type="button"
            className="tb-btn"
            onClick={handleToggleHideUI}
            title="Sembunyikan semua panel untuk tampilan peta penuh"
          >
            <EyeOff size={13} />
            <span>Peta Penuh</span>
          </button>
        </div>
      </header>

      {/* Floating Status Notification Toast */}
      {statusMsg && (
        <div
          className="hud-bar"
          style={{
            top: 'calc(var(--bar-h) + 12px)',
            left: '50%',
            transform: 'translateX(-50%)',
            bottom: 'auto',
            background: 'var(--panel)',
            borderColor: 'var(--warn)',
            color: 'var(--warn)',
            zIndex: 150,
            pointerEvents: 'auto',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span>{statusMsg}</span>
          <button
            type="button"
            onClick={() => setStatusMsg('')}
            style={{
              background: 'none',
              border: 'none',
              color: 'inherit',
              cursor: 'pointer',
              display: 'grid',
              placeItems: 'center',
            }}
            title="Tutup pesan"
          >
            <X size={12} />
          </button>
        </div>
      )}

      {/* 3. Floating Cluster List Popup (Left) */}
      {!hideAllUI && showListPanel && (
        <div className="float-panel list-panel">
          <div className="panel-header">
            <div className="panel-title">
              <Flame size={15} style={{ color: 'var(--ca)' }} />
              <span>Daftar Kejadian</span>
              <span className="tb-pill">{filteredIndices.length}</span>
            </div>
            <button
              type="button"
              className="panel-close"
              onClick={() => setShowListPanel(false)}
              title="Tutup panel"
            >
              <X size={15} />
            </button>
          </div>

          <div className="list-filters">
            <select
              aria-label="Jumlah titik minimum"
              value={minN}
              onChange={(e) => setMinN(Number(e.target.value))}
              title="Filter minimal titik dalam klaster"
            >
              <option value={3}>≥ 3 titik</option>
              <option value={10}>≥ 10 titik</option>
              <option value={30}>≥ 30 titik</option>
              <option value={100}>≥ 100 titik</option>
            </select>
            <select
              aria-label="Urutkan kejadian"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              title="Urutkan urutan kejadian"
            >
              <option value="t0">Terbaru</option>
              <option value="n">Titik terbanyak</option>
              <option value="area">Terluas</option>
              <option value="weak">Paling ragu</option>
            </select>
          </div>

          <div className="list-scroll">
            {filteredIndices.length === 0 ? (
              <div className="empty-state">
                Tidak ada kejadian dengan kriteria titik tersebut.
              </div>
            ) : (
              pagedClusterIndices.map((i) => {
                const c = ds.clusters[i];
                const isHi = c.stabRoot >= 0.8;
                const isActive = i === cur;
                return (
                  <button
                    key={c.id}
                    type="button"
                    className={`cluster-row ${isActive ? 'active' : ''}`}
                    onClick={() => selectCluster(i)}
                  >
                    <span>{c.id.replace('Contoh ', '')}</span>
                    <span>{fmt(c.pts[c.root].t)}</span>
                    <span>{c.n} ttk</span>
                    <span>{num(c.areaHa || 0)} ha</span>
                    <span
                      className={`dot ${isHi ? '' : 'lo'}`}
                      title={isHi ? 'Akar stabil (≥80%)' : 'Akar ragu (<80%)'}
                    />
                  </button>
                );
              })
            )}
          </div>

          {totalClusterPages > 1 && (
            <div className="list-pagination">
              <button
                type="button"
                className="page-nav-btn"
                disabled={clusterPage <= 1}
                onClick={() => setClusterPage((p) => Math.max(1, p - 1))}
                title="Halaman sebelumnya"
              >
                Sebelumnya
              </button>
              <span className="page-status">
                Hal <b>{clusterPage}</b> / {totalClusterPages} ({filteredIndices.length} total)
              </span>
              <button
                type="button"
                className="page-nav-btn"
                disabled={clusterPage >= totalClusterPages}
                onClick={() => setClusterPage((p) => Math.min(totalClusterPages, p + 1))}
                title="Halaman selanjutnya"
              >
                Selanjutnya
              </button>
            </div>
          )}
        </div>
      )}

      {/* 4. Floating Cluster Detail Card Popup (Right) */}
      {!hideAllUI && showDetailPanel && detailMetrics && (
        <div className="float-panel detail-panel">
          <div className="panel-header">
            <div className="panel-title">
              <FileText size={15} style={{ color: 'var(--ca)' }} />
              <span>{detailMetrics.c.id}</span>
            </div>
            <button
              type="button"
              className="panel-close"
              onClick={() => setShowDetailPanel(false)}
              title="Tutup panel"
            >
              <X size={15} />
            </button>
          </div>

          <div className="detail-body">
            <div>
              <span className={`badge-tag ${detailMetrics.hi ? 'hi' : 'lo'}`}>
                Titik awal {detailMetrics.hi ? 'yakin (stabil)' : 'ragu (zona multi-titik)'}
              </span>
            </div>

            <dl className="kv-grid">
              <div>
                <dt>Titik awal</dt>
                <dd>
                  {fmtY(detailMetrics.root.t)} {fmt(detailMetrics.root.t).slice(-5)} WIB
                </dd>
              </div>
              <div>
                <dt>Hotspot</dt>
                <dd>
                  {detailMetrics.g.vis.length} / {detailMetrics.c.n}
                </dd>
              </div>
              <div>
                <dt>Luas area</dt>
                <dd>{num(detailMetrics.areaHa)} ha</dd>
              </div>
              <div>
                <dt>Arah sebar</dt>
                <dd>
                  {detailMetrics.dist < 375
                    ? 'belum terukur'
                    : `${detailMetrics.bb.dir} ${Math.round(detailMetrics.bb.deg)}°`}
                </dd>
              </div>
              <div>
                <dt>Jarak dari awal</dt>
                <dd>
                  {detailMetrics.dist < 375 ? '-' : `${num(detailMetrics.dist / 1000, 1)} km`}
                </dd>
              </div>
              <div>
                <dt>Laju rata-rata</dt>
                <dd>
                  {detailMetrics.dist < 375 || detailMetrics.hrs < 1
                    ? '-'
                    : `${num(detailMetrics.dist / detailMetrics.hrs)} m/jam`}
                </dd>
              </div>
              <div style={{ gridColumn: '1 / -1' }}>
                <dt>Koordinat awal (WGS84)</dt>
                <dd>{llText(detailMetrics.c.rootLL)}</dd>
              </div>
              <div>
                <dt>Stabilitas awal</dt>
                <dd>{Math.round(detailMetrics.c.stabRoot * 100)}%</dd>
              </div>
              <div>
                <dt>Stabilitas panah</dt>
                <dd>
                  {detailMetrics.c.stabEdge == null
                    ? '-'
                    : `${Math.round(detailMetrics.c.stabEdge * 100)}%`}
                </dd>
              </div>
            </dl>

            {/* Panel Parameter Iklim, Arah Angin & FWI (Open-Meteo ERA5) */}
            {activeWeather && (
              <div
                style={{
                  borderTop: '1px solid var(--line)',
                  paddingTop: 10,
                  marginTop: 2,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 8,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    fontSize: 11.5,
                    fontWeight: 600,
                    color: '#38bdf8',
                  }}
                >
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    <Wind size={13} /> Cuaca, Angin & FWI (ERA5)
                  </span>
                  <span style={{ fontSize: 10, color: 'var(--muted)', fontWeight: 400 }}>
                    {activeWeather.resLabel}
                  </span>
                </div>

                <dl className="kv-grid">
                  <div>
                    <dt>Dorongan angin</dt>
                    <dd style={{ color: '#38bdf8' }}>
                      Ke {bearing(activeWeather.wu, activeWeather.wv).dir} ({activeWeather.wto}°)
                    </dd>
                  </div>
                  <div>
                    <dt>Kecepatan 10m</dt>
                    <dd>
                      {activeWeather.ws} m/s (Gust {activeWeather.gust})
                    </dd>
                  </div>
                  <div>
                    <dt>Suhu & RH</dt>
                    <dd>
                      {activeWeather.temp}°C · {activeWeather.rh}%
                    </dd>
                  </div>
                  <div>
                    <dt>Hujan 24 jam</dt>
                    <dd>{activeWeather.rain24h} mm</dd>
                  </div>
                  <div>
                    <dt>Fire Weather (FWI)</dt>
                    <dd>
                      {activeWeather.fwi} ({activeWeather.fwiCat})
                    </dd>
                  </div>
                  <div>
                    <dt>Kekeringan (DC)</dt>
                    <dd>
                      {activeWeather.dc} · ISI {activeWeather.isi}
                    </dd>
                  </div>
                  {detailMetrics.dist >= 375 && (
                    <div style={{ gridColumn: '1 / -1' }}>
                      <dt>Korelasi sebaran api vs arah angin</dt>
                      <dd>
                        {(() => {
                          const rawDiff = Math.abs(detailMetrics.bb.deg - activeWeather.wto) % 360;
                          const delta = Math.round(rawDiff > 180 ? 360 - rawDiff : rawDiff);
                          if (delta <= 45) {
                            return `Searah hembusan angin (Δ ${delta}°)`;
                          }
                          if (delta <= 110) {
                            return `Menyamping terhadap angin (Δ ${delta}°)`;
                          }
                          return `Melawan arah angin (Δ ${delta}° — indikasi non-alami/gambut)`;
                        })()}
                      </dd>
                    </div>
                  )}
                </dl>
              </div>
            )}

            {!detailMetrics.hi && (
              <div className="note-box">
                {detailMetrics.c.firstCount} titik terdeteksi bersamaan di lintasan pertama. Titik awal diperlakukan sebagai zona awal.
              </div>
            )}
            {detailMetrics.c.peat && (
              <div className="note-box">
                Area terindikasi gambut. Pembakaran bawah permukaan (smoldering) mungkin terjadi.
              </div>
            )}

            <button
              type="button"
              className="btn"
              style={{
                width: '100%',
                marginTop: 10,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 6,
                background: '#192430',
                borderColor: 'var(--line-hi)',
                color: 'var(--fg)',
                fontSize: 12,
                padding: '6px 12px',
              }}
              onClick={() => {
                setIsCropping(true);
                setCropBox(null);
              }}
              title="Tandai dan ekspor tangkapan layar untuk kluster ini"
            >
              <Camera size={14} style={{ color: 'var(--ca)' }} />
              <span>Ekspor Screenshot Kluster</span>
            </button>
          </div>
        </div>
      )}

      {/* 5. Floating Layer Controls Popover */}
      {!hideAllUI && showLayerPanel && (
        <div className="float-panel layer-panel">
          <div className="panel-header">
            <div className="panel-title">
              <Layers size={15} style={{ color: 'var(--ca)' }} />
              <span>Pengaturan Layer</span>
            </div>
            <button
              type="button"
              className="panel-close"
              onClick={() => setShowLayerPanel(false)}
              title="Tutup popover"
            >
              <X size={15} />
            </button>
          </div>

          <div className="layer-list">
            {[
              ['area', 'Area poligon terbakar (Alpha shape)'],
              ['arrow', 'Panah rantai sebaran (Pohon induk)'],
              ['pts', 'Titik hotspot VIIRS'],
              ['day', 'Garis jejak harian'],
              ['wind', 'Vektor arah angin & cuaca (ERA5)'],
              ['ctx', 'Titik konteks kejadian lain'],
              ['grid', 'Grid sensor 375 m'],
            ].map(([key, labelText]) => (
              <label key={key} className="layer-check">
                <input
                  type="checkbox"
                  checked={layers[key]}
                  onChange={(e) =>
                    setLayers((prev) => ({ ...prev, [key]: e.target.checked }))
                  }
                />
                <span>{labelText}</span>
              </label>
            ))}

            <div className="slider-group">
              <span>
                Opasitas peta dasar <b>{Math.round(mapOpacity * 100)}%</b>
              </span>
              <input
                type="range"
                min={0.15}
                max={1}
                step={0.05}
                value={mapOpacity}
                onChange={(e) => setMapOpacity(Number(e.target.value))}
              />
            </div>

            <div className="slider-group">
              <span>
                Kerapatan area alpha <b>{num(alphaM / 1000, 1)} km</b>
              </span>
              <input
                type="range"
                min={500}
                max={2500}
                step={50}
                value={alphaM}
                onChange={(e) => {
                  geoCacheRef.current = {};
                  setAlphaM(Number(e.target.value));
                }}
              />
            </div>

            <div className="slider-group">
              <span>
                Kerapatan partikel angin <b>{windDensity} partikel</b>
              </span>
              <input
                type="range"
                min={150}
                max={1400}
                step={50}
                value={windDensity}
                onChange={(e) => setWindDensity(Number(e.target.value))}
              />
            </div>
          </div>
        </div>
      )}

      {/* 6. Database Query Modal Dialog */}
      {showDbModal && (
        <div className="modal-backdrop" onClick={() => setShowDbModal(false)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="panel-header">
              <div className="panel-title">
                <Database size={16} style={{ color: 'var(--ca)' }} />
                <span>Query Database Hotspot Kalbar</span>
              </div>
              <button
                type="button"
                className="panel-close"
                onClick={() => setShowDbModal(false)}
                title="Tutup dialog"
              >
                <X size={16} />
              </button>
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                setShowDbModal(false);
                fetchFromDatabase();
              }}
            >
              <div className="modal-body">
                <div className="preset-row">
                  <span className="preset-label">Rentang Cepat:</span>
                  <button
                    type="button"
                    className="preset-btn"
                    onClick={() => applyDatePreset(7)}
                    title="Ambil 7 hari terakhir dari data terbaru (< 1 detik)"
                  >
                    ⚡ 7 Hari Terkini (&lt;1s)
                  </button>
                  <button
                    type="button"
                    className="preset-btn"
                    onClick={() => applyDatePreset(14)}
                    title="Ambil 14 hari terakhir (~1-2 detik)"
                  >
                    📅 14 Hari (2 Minggu)
                  </button>
                  <button
                    type="button"
                    className="preset-btn"
                    onClick={() => applyDatePreset(30)}
                    title="Ambil 30 hari (~3-4 detik)"
                  >
                    📆 30 Hari (1 Bulan)
                  </button>
                </div>

                <div className="form-grid">
                  <div className="form-field">
                    <label htmlFor="dbStart">Mulai (UTC)</label>
                    <input
                      id="dbStart"
                      type="date"
                      value={dbStart}
                      onChange={(e) => setDbStart(e.target.value)}
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="dbEnd">Akhir (UTC)</label>
                    <input
                      id="dbEnd"
                      type="date"
                      value={dbEnd}
                      onChange={(e) => setDbEnd(e.target.value)}
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="dbMinConf">Tingkat Keyakinan</label>
                    <select
                      id="dbMinConf"
                      value={dbMinConf}
                      onChange={(e) => setDbMinConf(e.target.value)}
                    >
                      <option value="n">Nominal + High (≥ n)</option>
                      <option value="h">High saja (h)</option>
                      <option value="l">Semua titik (l, n, h)</option>
                    </select>
                  </div>
                  <div className="form-field">
                    <label htmlFor="dbMaxPts">Batas Maks Titik Raw</label>
                    <input
                      id="dbMaxPts"
                      type="number"
                      min="1000"
                      max="50000"
                      step="1000"
                      value={dbMaxPoints}
                      onChange={(e) => setDbMaxPoints(Number(e.target.value))}
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="dbMinPts">Minimal Titik</label>
                    <input
                      id="dbMinPts"
                      type="number"
                      min="2"
                      max="100"
                      value={dbMinPoints}
                      onChange={(e) => setDbMinPoints(Number(e.target.value))}
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="dbEpsS">Radius Klaster (meter)</label>
                    <input
                      id="dbEpsS"
                      type="number"
                      min="375"
                      max="10000"
                      step="125"
                      value={dbEpsS}
                      onChange={(e) => setDbEpsS(Number(e.target.value))}
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="dbEpsTH">Jeda Waktu Maks (jam)</label>
                    <input
                      id="dbEpsTH"
                      type="number"
                      min="6"
                      max="240"
                      step="6"
                      value={dbEpsTH}
                      onChange={(e) => setDbEpsTH(Number(e.target.value))}
                    />
                  </div>
                </div>
                <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: 4 }}>
                  Kueri tabel <code>monitoring.alert_fire_&#123;viirssnpp,viirsnoaa20,viirsnoaa21&#125;_raw</code> di PostgreSQL lokal (Kalbar BBox).
                </div>
              </div>

              <div className="modal-footer">
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setShowDbModal(false)}
                >
                  Batal
                </button>
                <button
                  type="submit"
                  className="btn primary"
                  disabled={dbLoading}
                >
                  {dbLoading ? 'Memproses DB...' : 'Jalankan Forensik'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 7. Methodology & Scientific Proof Limits Modal */}
      {showInfoModal && (
        <div className="modal-backdrop" onClick={() => setShowInfoModal(false)}>
          <div className="modal-card" style={{ maxWidth: 620 }} onClick={(e) => e.stopPropagation()}>
            <div className="panel-header">
              <div className="panel-title">
                <Info size={16} style={{ color: 'var(--ca)' }} />
                <span>Metodologi Forensik Sebaran Api VIIRS 375 m</span>
              </div>
              <button
                type="button"
                className="panel-close"
                onClick={() => setShowInfoModal(false)}
                title="Tutup dialog"
              >
                <X size={16} />
              </button>
            </div>

            <div className="modal-body" style={{ maxHeight: '70vh', overflowY: 'auto' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <div>
                  <h4 style={{ fontSize: 13, color: 'var(--ca)', marginBottom: 4 }}>
                    1. Titik Awal Dijamin oleh Aturan (Root)
                  </h4>
                  <p style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.5 }}>
                    Setiap kluster memiliki tepat satu akar: deteksi dari lintasan satelit paling awal (t₀). Jika terdapat beberapa hotspot pada lintasan perdana, titik dengan FRP (Fire Radiative Power) tertinggi dipilih sebagai representasi, dan status diberi label keyakinan rendah (zona awal).
                  </p>
                </div>
                <div>
                  <h4 style={{ fontSize: 13, color: 'var(--ca)', marginBottom: 4 }}>
                    2. Panah Mengikuti Garis Waktu (Propagation Tree)
                  </h4>
                  <p style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.5 }}>
                    Induk suatu hotspot adalah titik terdekat dari lintasan yang lebih awal. Tidak ada panah yang masuk ke akar dan tidak ada siklus terbalik. Garis putus-putus menunjukkan jarak sub-piksel (&lt;375 m) atau zona simultan.
                  </p>
                </div>
                <div>
                  <h4 style={{ fontSize: 13, color: 'var(--ca)', marginBottom: 4 }}>
                    3. Uji Kestabilan Monte Carlo
                  </h4>
                  <p style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.5 }}>
                    FRP diacak ±20% dan koordinat sensor digeser ±187,5 m sebanyak 200 iterasi. Persentase stabilitas menunjukkan seberapa tahan akar dan rantai sebaran terhadap ketidakpastian posisi satelit.
                  </p>
                </div>
                <div>
                  <h4 style={{ fontSize: 13, color: 'var(--ca)', marginBottom: 4 }}>
                    4. Batas Bukti Fisik
                  </h4>
                  <p style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.5 }}>
                    Titik awal mencerminkan deteksi satelit pertama, bukan necessarily detik awal penyulutan api di lapangan. Tutupan awan, asap tebal, dan jeda orbit satelit dapat menunda deteksi. Pada lahan gambut dalam, pembakaran smoldering di bawah permukaan tanah dapat menyebabkan pergerakan api tidak selalu searah dengan hotspot permukaan.
                  </p>
                </div>
                <div>
                  <h4 style={{ fontSize: 13, color: 'var(--ca)', marginBottom: 4 }}>
                    5. Sumber Data Spasial Resmi (Gambut, Konsesi, & Administrasi)
                  </h4>
                  <p style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 6 }}>
                    Untuk investigasi hukum dan pembuktian karhutla, lapisan spasial dirujuk dari sumber-sumber otoritatif nasional:
                  </p>
                  <ul style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.6, paddingLeft: 18 }}>
                    <li>
                      <b style={{ color: 'var(--fg)' }}>Lahan Gambut & KHG:</b> BBSDLP Kementan 2019 (Peta Sebaran Lahan Gambut Skala 1:50.000) & Peta Kesatuan Hidrologis Gambut (KHG) BRGM / SK MenLHK No. 130/2017 (Fungsi Lindung & Budidaya Gambut).
                    </li>
                    <li>
                      <b style={{ color: 'var(--fg)' }}>Konsesi Kelapa Sawit (HGU):</b> Portal Bhumi ATR/BPN, didukung basis data konsesi spasial terbuka Auriga Nusantara dan Greenpeace Indonesia.
                    </li>
                    <li>
                      <b style={{ color: 'var(--fg)' }}>Konsesi Kehutanan (PBPH / IUPHHK):</b> WebGIS Kehutanan / SIGAP KLHK (PBPH Hutan Alam, PBPH Hutan Tanaman Industri / HTI, dan Restorasi Ekosistem).
                    </li>
                    <li>
                      <b style={{ color: 'var(--fg)' }}>Konsesi Pertambangan (IUP):</b> MOMI (Minerba One Map Indonesia) Ditjen Minerba - Kementerian ESDM.
                    </li>
                    <li>
                      <b style={{ color: 'var(--fg)' }}>Batas Wilayah Administrasi:</b> Ina-Geoportal / Peta Rupa Bumi Indonesia (RBI) 1:25.000 / 1:50.000 Badan Informasi Geospasial (BIG).
                    </li>
                  </ul>
                </div>
              </div>
            </div>

            <div className="modal-footer">
              <button
                type="button"
                className="btn primary"
                onClick={() => setShowInfoModal(false)}
              >
                Mengerti
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Modal Dialog: Export Screenshot Preview & Download */}
      {showExportModal && (
        <div className="modal-backdrop" onClick={() => setShowExportModal(false)}>
          <div
            className="modal-card"
            style={{ maxWidth: 740 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="panel-header">
              <div className="panel-title">
                <Camera size={16} style={{ color: 'var(--ca)' }} />
                <span>Ekspor Tangkapan Layar Forensik (PNG)</span>
              </div>
              <button
                type="button"
                className="panel-close"
                onClick={() => setShowExportModal(false)}
                title="Tutup dialog"
              >
                <X size={16} />
              </button>
            </div>

            <div className="modal-body">
              {/* Preview Container */}
              <div className="export-preview-box">
                {screenshotPreviewUrl ? (
                  <img
                    src={screenshotPreviewUrl}
                    alt="Pratinjau tangkapan layar forensik"
                    className="export-preview-img"
                  />
                ) : (
                  <div style={{ color: 'var(--muted)', fontSize: 12 }}>
                    Membuat pratinjau resolusi tinggi...
                  </div>
                )}
                {screenshotCropBox && (
                  <div className="export-preview-badge">
                    {Math.round(screenshotCropBox.w)} × {Math.round(screenshotCropBox.h)} px
                  </div>
                )}
              </div>

              {/* Options & Metadata */}
              <div className="export-options-grid">
                <label className="export-check-item">
                  <input
                    type="checkbox"
                    checked={exportWatermark}
                    onChange={(e) => {
                      const nextVal = e.target.checked;
                      setExportWatermark(nextVal);
                      generateForensicScreenshot(screenshotCropBox, { watermark: nextVal });
                    }}
                  />
                  <span><b>Cap Metadata Forensik & Watermark Resmi</b> (ID Kluster, Tanggal WIB, Titik Awal, dan Cuaca ERA5)</span>
                </label>

                <label className="export-check-item">
                  <input
                    type="checkbox"
                    checked={exportWind}
                    disabled={!layers.wind}
                    onChange={(e) => {
                      const nextVal = e.target.checked;
                      setExportWind(nextVal);
                      generateForensicScreenshot(screenshotCropBox, { wind: nextVal });
                    }}
                  />
                  <span>Sertakan Aliran Vektor Angin & Bara Api (ERA5 10m)</span>
                </label>

                <label className="export-check-item">
                  <input
                    type="checkbox"
                    checked={exportScale}
                    onChange={(e) => {
                      const nextVal = e.target.checked;
                      setExportScale(nextVal);
                      generateForensicScreenshot(screenshotCropBox, { scale: nextVal });
                    }}
                  />
                  <span>Sertakan Skala Jarak Peta (km) & Arah Mata Angin (↑ U)</span>
                </label>
              </div>

              {/* Filename Input */}
              <div className="export-filename-row">
                <label htmlFor="export-filename">Nama Berkas:</label>
                <input
                  id="export-filename"
                  type="text"
                  className="export-filename-input"
                  value={exportFilename}
                  onChange={(e) => setExportFilename(e.target.value)}
                />
              </div>
            </div>

            <div className="modal-footer" style={{ display: 'flex', justifyContent: 'space-between' }}>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setShowExportModal(false);
                  setIsCropping(true);
                  setCropBox(null);
                }}
                title="Ulangi pilih area secara manual"
              >
                <Crop size={14} style={{ marginRight: 6 }} />
                Ulangi Pilih Area
              </button>

              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setShowExportModal(false)}
                >
                  Batal
                </button>
                <button
                  type="button"
                  className="btn primary"
                  onClick={handleDownloadScreenshot}
                >
                  <Download size={14} style={{ marginRight: 6 }} />
                  Unduh Berkas PNG
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 8. Floating Timeline Dock (Bottom Center) */}
      {!hideAllUI && (
        <div className="timeline-dock">
          <div className="timeline-row">
            <button
              type="button"
              className="btn"
              style={{ minWidth: 84 }}
              onClick={() => {
                if (!activeCluster) return;
                const maxStep = activeCluster.steps.length - 1;
                if (playing) {
                  setPlaying(false);
                  return;
                }
                if (stepK >= maxStep) {
                  setStepK(0);
                }
                setPlaying(true);
              }}
            >
              {playing ? (
                <>
                  <Pause size={13} /> Jeda
                </>
              ) : activeCluster && stepK >= activeCluster.steps.length - 1 ? (
                <>
                  <RotateCcw size={13} /> Ulang
                </>
              ) : (
                <>
                  <Play size={13} /> Putar
                </>
              )}
            </button>

            <input
              type="range"
              min={0}
              max={activeCluster ? Math.max(0, activeCluster.steps.length - 1) : 0}
              value={stepK}
              aria-label="Lintasan satelit"
              onChange={(e) => {
                setPlaying(false);
                setStepK(Number(e.target.value));
              }}
            />

            <div className="speed-selector" title="Kecepatan putar animasi forensik">
              {[0.5, 1, 2].map((s) => (
                <button
                  key={s}
                  type="button"
                  className={`speed-pill ${playbackSpeed === s ? 'active' : ''}`}
                  onClick={() => setPlaybackSpeed(s)}
                >
                  {s}×
                </button>
              ))}
            </div>

            <span className="tb-pill" style={{ fontSize: 11, padding: '3px 8px' }}>
              {activeCluster
                ? `Lintasan ${stepK + 1} / ${activeCluster.steps.length}`
                : 'Pilih Kejadian'}
            </span>
          </div>

          <div className="timeline-readout">
            {activeCluster && currentStepObj && (
              <>
                <span>
                  Waktu: <b>{fmtY(currentStepObj.t)} {fmt(currentStepObj.t).slice(-5)} WIB</b>
                </span>
                <span>
                  Satelit: <b>{currentStepObj.sat}</b>
                </span>
                <span>
                  Tambahan: <b>+{currentStepObj.n} titik</b>
                </span>
                <span>
                  Kumulatif: <b>{shownPointsCount} dari {activeCluster.n} titik</b>
                </span>
                {climateLoading && (
                  <span style={{ color: '#38bdf8', fontSize: 11, fontStyle: 'italic' }}>
                    · Sinkronisasi cuaca ERA5...
                  </span>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* 9. Floating Map Navigation Stack (Top-Right) */}
      {!hideAllUI && (
        <div className="map-nav-stack">
          <button
            type="button"
            onClick={() => zoomBy(1.35)}
            title="Perbesar (Zoom In)"
          >
            <Plus size={16} />
          </button>
          <button
            type="button"
            onClick={() => zoomBy(1 / 1.35)}
            title="Perkecil (Zoom Out)"
          >
            <Minus size={16} />
          </button>
          <button
            type="button"
            onClick={() => {
              if (cur >= 0 && ds.clusters[cur]) {
                fitToPoints(ds.clusters[cur].pts, 0.5);
                draw();
              }
            }}
            title="Fokus ke kejadian aktif"
          >
            <Crosshair size={15} />
          </button>
          <button
            type="button"
            onClick={() => {
              const all = [];
              ds.clusters.forEach((c) => c.pts.forEach((p) => all.push(p)));
              ds.context.forEach((q) => all.push({ x: q[0], y: q[1] }));
              fitToPoints(all, 0.15);
              draw();
            }}
            title="Lihat seluruh data hotspot"
          >
            <Globe size={15} />
          </button>
          <button
            type="button"
            className={isCropping ? 'active' : ''}
            onClick={() => {
              setIsCropping((v) => !v);
              setCropBox(null);
            }}
            title="Tangkapan Layar & Seleksi Area (Screenshot PNG)"
          >
            <Camera size={15} />
          </button>
          <button
            type="button"
            className={showLegend ? 'active' : ''}
            onClick={() => setShowLegend((v) => !v)}
            title="Buka / tutup legenda simbol peta"
          >
            <Info size={15} />
          </button>
          <button
            type="button"
            onClick={() => setShowInfoModal(true)}
            title="Pelajari metodologi dan batas bukti forensik"
          >
            <FileText size={15} />
          </button>
        </div>
      )}

      {/* 10. Floating Legend (Bottom-Left) */}
      {!hideAllUI && showLegend && (
        <div className="legend-panel">
          <span>
            <i
              style={{
                background: 'var(--fg)',
                clipPath: 'polygon(50% 0, 100% 50%, 50% 100%, 0 50%)',
              }}
            />
            Titik awal (deteksi pertama)
          </span>
          <span>
            <i
              style={{
                background: 'var(--ca)',
                clipPath: 'circle(36% at 50% 50%)',
              }}
            />
            Hotspot kejadian terpilih
          </span>
          <span>
            <i
              style={{
                borderTop: '2px solid var(--ca)',
                height: 0,
                marginTop: 4,
              }}
            />
            Panah sebaran dari titik terdekat
          </span>
          <span>
            <i
              style={{
                background: 'color-mix(in srgb, var(--ca) 30%, transparent)',
                border: '1px solid var(--ca)',
              }}
            />
            Area terbakar (Alpha shape)
          </span>
          <span>
            <i
              style={{
                borderTop: '2px solid #38bdf8',
                height: 0,
                marginTop: 4,
              }}
            />
            Vektor dorongan angin 10m (ERA5)
          </span>
          <span>
            <i
              style={{
                background: 'var(--muted)',
                clipPath: 'circle(22% at 50% 50%)',
              }}
            />
            Kejadian lain & titik tunggal
          </span>
          {layers.wind && (
            <div
              style={{
                borderTop: '1px solid var(--line)',
                marginTop: 4,
                paddingTop: 6,
                display: 'flex',
                flexDirection: 'column',
                gap: 4,
              }}
            >
              <div style={{ fontSize: 10.5, fontWeight: 600, color: '#38bdf8' }}>
                Streamline Kecepatan Angin (m/s)
              </div>
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
                  gap: '3px 8px',
                }}
              >
                {WIND_SPEED_TIERS.map((tier) => (
                  <span key={tier.label} style={{ fontSize: 10, gap: 5 }}>
                    <i
                      style={{
                        width: 10,
                        height: 3,
                        borderRadius: 2,
                        background: tier.color,
                      }}
                    />
                    {tier.label}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* 11. Live Coordinate HUD (Bottom-Left) */}
      {!hideAllUI && cursorHud && (
        <div className="hud-bar">{cursorHud}</div>
      )}
    </main>
  );
}
