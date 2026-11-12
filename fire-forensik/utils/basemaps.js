const CARTO_API_KEY =
  process.env.NEXT_PUBLIC_CARTO_API_KEY || 'cb1_4rl4_1_b877170eaa644026e39b00c0';

export const BASEMAPS = [
  {
    id: 'dark',
    name: 'CARTO Dark Matter',
    thumbnail: `https://basemaps.cartocdn.com/rastertiles/dark_all/6/50/32.png?key=${CARTO_API_KEY}`,
    url: `https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png?key=${CARTO_API_KEY}`,
    attributions: '© CARTO · © OpenStreetMap contributors',
    category: 'monochrome',
    isDark: true,
    maxZoom: 19,
  },
  {
    id: 'positron',
    name: 'CARTO Positron (Light)',
    thumbnail: `https://basemaps.cartocdn.com/rastertiles/light_all/6/50/32.png?key=${CARTO_API_KEY}`,
    url: `https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}.png?key=${CARTO_API_KEY}`,
    attributions: '© CARTO · © OpenStreetMap contributors',
    category: 'monochrome',
    isDark: false,
    maxZoom: 19,
  },
  {
    id: 'voyager',
    name: 'CARTO Voyager',
    thumbnail: `https://basemaps.cartocdn.com/rastertiles/voyager/6/50/32.png?key=${CARTO_API_KEY}`,
    url: `https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=${CARTO_API_KEY}`,
    attributions: '© CARTO · © OpenStreetMap contributors',
    category: 'streets',
    isDark: false,
    maxZoom: 19,
  },
  {
    id: 'osm',
    name: 'OpenStreetMap',
    thumbnail: 'https://tile.openstreetmap.org/6/50/32.png',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attributions: '© OpenStreetMap contributors',
    category: 'streets',
    isDark: false,
    maxZoom: 19,
  },
  {
    id: 'satellite',
    name: 'Satellite (Esri)',
    thumbnail:
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/6/32/50',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attributions: 'Tiles © Esri World Imagery',
    category: 'satellite',
    isDark: true,
    maxZoom: 18,
  },
  {
    id: 'google_hybrid',
    name: 'Google Hybrid',
    thumbnail: 'https://mt1.google.com/vt/lyrs=y&x=50&y=32&z=6',
    url: 'https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
    attributions: '© Google',
    category: 'satellite',
    isDark: true,
    maxZoom: 19,
  },
  {
    id: 'opentopomap',
    name: 'OpenTopoMap',
    thumbnail: 'https://c.tile.opentopomap.org/6/50/32.png',
    url: 'https://{a-c}.tile.opentopomap.org/{z}/{x}/{y}.png',
    attributions: '© OpenStreetMap · SRTM · © OpenTopoMap',
    category: 'relief',
    isDark: false,
    maxZoom: 17,
  },
  {
    id: 'google_terrain',
    name: 'Google Terrain',
    thumbnail: 'https://mt1.google.com/vt/lyrs=p&x=50&y=32&z=6',
    url: 'https://mt1.google.com/vt/lyrs=p&x={x}&y={y}&z={z}',
    attributions: '© Google',
    category: 'relief',
    isDark: false,
    maxZoom: 19,
  },
  {
    id: 'terrain',
    name: 'Terrain Topo (Esri)',
    thumbnail:
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/6/32/50',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
    attributions: 'Tiles © Esri World Topo',
    category: 'streets',
    isDark: false,
    maxZoom: 18,
  },
  {
    id: 'grid_only',
    name: 'Grid UTM (Tanpa Peta)',
    thumbnail:
      'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" fill="%23141c22"/><path d="M0 16h64M0 32h64M0 48h64M16 0v64M32 0v64M48 0v64" stroke="%2327333c" stroke-width="1"/><circle cx="32" cy="32" r="6" fill="%23ff7b47"/></svg>',
    url: null,
    attributions: 'Proyeksi Metrik UTM 49N (EPSG:32649)',
    category: 'monochrome',
    isDark: null,
    maxZoom: 20,
  },
];

export function getBasemapConfig(id) {
  if (!id) return BASEMAPS[0];
  return BASEMAPS.find((b) => b.id === id) || BASEMAPS[0];
}

export function resolveTileUrl(template, z, x, y) {
  if (!template) return null;
  let url = template;
  const m = url.match(/\{([a-z])-([a-z])\}/);
  if (m) {
    const startCode = m[1].charCodeAt(0);
    const endCode = m[2].charCodeAt(0);
    const count = endCode - startCode + 1;
    const sub = String.fromCharCode(startCode + (Math.abs(x + y) % count));
    url = url.replace(m[0], sub);
  }
  return url.replace('{z}', String(z)).replace('{x}', String(x)).replace('{y}', String(y));
}

