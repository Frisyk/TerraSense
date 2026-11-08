Berdasarkan analisis terhadap `fire_dir.py`, `route.js`, `spreadLayers.js`, dan hasil visualisasi di layar, terdapat ketidaksesuaian antara logika algoritma saat ini dengan ekspektasi topologi penyebaran dan spesifikasi spasial yang Anda inginkan.

Berikut adalah rincian akar masalah beserta solusi perbaikannya di masing-masing file.

---

### 1. Masalah Titik Awal (Origin) Tidak Memiliki Panah / Bukan Pusat Cabang

Pada gambar, lingkaran kuning (`Titik awal` / `is_origin`) tampak terisolasi tanpa satu pun panah keluar, sedangkan titik di sebelah kirinya justru memancarkan banyak panah (menjadi *hub*). Terdapat **3 penyebab utama** di dalam kode:

1. **Aturan Jeda Minimum (`MIN_LINK_LAG_MIN = 60` menit) di `fire_dir.py`:**
* Saat klaster terbentuk (`_cluster`), titik-titik dalam satu sapuan/lintasan satelit (*overpass* yang sama, selisih waktu 0–15 menit) digabungkan ke dalam satu klaster yang sama.


* Namun saat mencari *parent*, script membatasi kandidat harus lebih awal minimal **60 menit** (`hi = np.searchsorted(tm, tm[k] - MIN_LINK_LAG_MIN / 60.0)`).


* **Akibatnya:** Jika pada lintasan pertama satelit mendeteksi 5 titik sekaligus (misal pukul 13:00), titik ke-1 ditandai sebagai `is_origin = True`. Titik ke-2 s.d. ke-5 (pukul 13:00) tidak boleh menjadikan titik ke-1 sebagai *parent* karena selisih waktunya 0 menit (< 60 menit). Ketika lintasan satelit berikutnya lewat (pukul 16:00), titik-titik baru mencari titik terdekat dari lintasan pertama—yang secara kebetulan secara geografis lebih dekat ke titik ke-3 (bukan titik ke-1). Akhirnya titik ke-3 memiliki banyak panah keluar (`role = 'branch_root'`), sedangkan titik ke-1 (`is_origin`) tidak punya panah sama sekali.




2. **Model Rantai Terdekat vs Model Star/Hub (`origin` sebagai Induk Utama):**
* Saat ini script mencari *parent* dari **semua titik sebelumnya** yang paling dekat secara jarak (`best = np.lexsort((-t_h[cand], d))[0]`). Jika Anda menginginkan **Titik Awal (Node Pertama) menjadi pusat yang memancarkan banyak panah ke titik-titik lain** (atau mewajibkan semua titik di gelombang pertama terhubung langsung ke `origin`), maka pencarian kandidat *parent* atau penentuan `origin` harus disesuaikan.




3. **Filter `minDist >= 300` di API `route.js`:**
* Di `route.js`, query `arrowsQ` menyaring `AND s.parent_dist_m >= 300`. Jika panah pertama dari `origin` menuju titik anak berjarak 150 meter, panah tersebut **dihapus oleh API**, sehingga di peta `origin` terlihat berdiri sendiri tanpa panah.





---

### 2. Masalah Ukuran Grid (Saat Ini 1,1 km vs Keinginan 300m x 300m & Angka Hanya `1`)

1. **Ukuran Grid masih `0.01°` (~1,1 km):**
* Di `fire_dir.py` (`GRID_CELL_DEG = 0.01`), `route.js` (`cellDeg: 0.01`), dan `spreadLayers.js` (`cellDeg || 0.01`), ukuran sel disetel `0.01` derajat ($\approx 1.113\text{ meter}$).


* Untuk ukuran **300m x 300m** di ekuator (Kalimantan), nilai derajatnya adalah:

$$\frac{300\text{ m}}{111.320\text{ m/derajat}} \approx 0.0027^\circ$$




2. **Membuat Grid Berpusat per Titik Api (Bukan Snap ke Grid Global) & Angka Hanya `1`:**
* Saat ini grid menggunakan `np.floor(lon / GRID_CELL_DEG)` sehingga kotak tidak berpusat tepat di titik koordinat hotspot, serta `grid_order` menghitung urutan kumulatif (`1, 2, 3...`).


* Jika Anda ingin kotak 300m x 300m dibuat per titik api (atau per sel aktif) dan **angkanya hanya `1**`, kita cukup mengunci label/order menjadi `1` (atau hanya menampilkan angka `1` pada sel titik awal / setiap kotak).



---

### Solusi Perubahan Kode

Berikut adalah penyesuaian pada ketiga file tersebut:

#### A. Perbaikan `scripts/fire_dir.py`

Mengubah `GRID_CELL_DEG` menjadi `0.0027` (300m), memastikan titik awal (`origin`) adalah node yang benar-benar menjadi induk penyebaran (dan mengizinkan penautan dari `origin` ke titik-titik lain di klaster tersebut), serta mengunci `grid_order = 1`.

```python
# ============================================================
# CONFIG (Ubah di bagian atas fire_dir.py)
# ============================================================
SPATIAL_EPS_M = 3000        # jarak maks antar titik se-klaster / parent (meter)
TEMPORAL_EPS_H = 24         # selisih waktu maks (jam)
MIN_CLUSTER_SIZE = 3        # klaster lebih kecil = 'isolated'
MIN_LINK_LAG_MIN = 0        # UBAH KE 0: agar titik di overpass pertama tetap terhubung ke origin
MIN_ARROW_DIST_M = 50       # Jarak minimum (meter) agar tidak menumpuk di titik yang persis sama
GRID_CELL_DEG = 0.0027      # UBAH KE 0.0027 derajat (~300m x 300m di Kalimantan)

```

Selanjutnya, pada fungsi `analyze()` di `fire_dir.py`, ubah bagian pencarian *parent* dan *grid* (sekitar baris 137–195) menjadi:

```python
    # df sudah urut waktu -> indeks naik = waktu naik di dalam tiap klaster
    order = np.argsort(labels, kind="stable")
    bounds = np.flatnonzero(np.diff(labels[order])) + 1

    # Array untuk menyimpan indeks origin tiap klaster
    first_of = np.zeros(labels.max() + 1, dtype=np.int64)

    for members in np.split(order, bounds):
        m = np.sort(members)
        cid_label = labels[m[0]]
        if m.size < MIN_CLUSTER_SIZE:
            first_of[cid_label] = m[0]
            continue

        tm = t_h[m]
        # Cari deteksi gelombang pertama (overpass paling awal di klaster ini)
        first_wave_mask = (tm - tm[0]) <= 0.25  # dalam 15 menit pertama
        first_wave_idx = m[first_wave_mask]

        # Pilih titik di gelombang pertama yang paling sentral (paling banyak tetangga di klaster)
        if first_wave_idx.size > 1:
            d_matrix = np.array([_hav_m(lat[idx], lon[idx], lat[m], lon[m]) for idx in first_wave_idx])
            reach_count = (d_matrix <= SPATIAL_EPS_M).sum(axis=1)
            best_origin_local = np.argmax(reach_count)
            orig_idx = first_wave_idx[best_origin_local]
        else:
            orig_idx = m[0]

        first_of[cid_label] = orig_idx

        # Hubungkan semua anggota klaster
        for i in m:
            if i == orig_idx:
                continue
            
            # Prioritaskan terhubung langsung dari Origin jika dalam jangkauan SPATIAL_EPS_M
            d_orig = float(_hav_m(lat[i], lon[i], np.array([lat[orig_idx]]), np.array([lon[orig_idx]]))[0])
            lag_orig = (t_h[i] - t_h[orig_idx]) * 60.0

            if d_orig >= MIN_ARROW_DIST_M and d_orig <= SPATIAL_EPS_M and 0 <= lag_orig <= TEMPORAL_EPS_H * 60:
                parent[i] = orig_idx
                dist[i] = d_orig
                lag[i] = lag_orig
                brg[i] = _bearing(lat[orig_idx], lon[orig_idx], lat[i], lon[i])
                hop[i] = 1
                continue

            # Jika di luar jangkauan langsung origin, cari parent terdekat yang lebih awal
            cand = m[(t_h[m] <= t_h[i]) & (m != i)]
            if not cand.size:
                continue
            d = _hav_m(lat[i], lon[i], lat[cand], lon[cand])
            valid = (d >= MIN_ARROW_DIST_M) & (d <= SPATIAL_EPS_M) & ((t_h[i] - t_h[cand]) <= TEMPORAL_EPS_H)
            if not valid.any():
                continue
            cand, d = cand[valid], d[valid]
            best = np.lexsort((t_h[cand], d))[0]
            p = cand[best]
            parent[i] = p
            dist[i] = d[best]
            lag[i] = (t_h[i] - t_h[p]) * 60.0
            brg[i] = _bearing(lat[p], lon[p], lat[i], lon[i])
            hop[i] = hop[p] + 1

    origin_pos = first_of[labels]
    origin_uid = uid[origin_pos]
    cluster_uuid = {l: str(uuid.uuid5(uuid.NAMESPACE_URL, uid[first_of[l]])) for l in np.unique(labels)}
    cluster_id = np.array([cluster_uuid[l] for l in labels])

    if window_start is not None:
        off_h = (df["datetm"].min() - pd.Timestamp(window_start)).total_seconds() / 3600.0
    else:
        off_h = TEMPORAL_EPS_H
    censored = (t_h[origin_pos] + off_h) < TEMPORAL_EPS_H

    child_count = np.bincount(parent[parent >= 0], minlength=n)
    isolated = size < MIN_CLUSTER_SIZE
    # Origin harus memiliki minimal 1 panah keluar (child_count > 0)
    is_origin = (np.arange(n) == origin_pos) & ~isolated & ~censored & (child_count > 0)
    role = np.where(isolated, "isolated",
                    np.where(is_origin, "origin",
                             np.where(parent >= 0, "spread", "branch_root")))

    # Grid 300m x 300m & angka dikunci menjadi 1
    gix = np.floor(lon / GRID_CELL_DEG).astype(int)
    giy = np.floor(lat / GRID_CELL_DEG).astype(int)
    cell = np.array([f"{a}_{b}" for a, b in zip(gix, giy)])
    origin_cell = cell[origin_pos]
    grid_is_origin = (cell == origin_cell) & ~isolated & ~censored
    grid_order = np.ones(n, dtype=int)  # Angka selalu 1

```

---

#### B. Perbaikan `app/api/hotspots/spread/route.js`

1. Turunkan default `minDist` dari `300` menjadi `50` meter agar panah dari titik awal ke titik di dekatnya tidak terpotong oleh API.


2. Ubah `cellDeg` dari `0.01` menjadi `0.0027` (300m x 300m) dan pastikan `order` bernilai `1` (serta kirim koordinat `lon, lat` jika Anda ingin kotak presisi membungkus masing-masing titik api).



```javascript
// Di dalam route.js:
const minDist = Math.max(0, Number(sp.get('minDist') ?? 50) || 0); // Ubah default 300 -> 50

```

Dan pada bagian `cellsQ` serta `NextResponse.json` di `route.js`:

```javascript
    const cellsQ = client.query(
      `SELECT s.cluster_id, s.grid_ix, s.grid_iy, 1 AS ord,
              AVG(s.lon)::float8 AS lon, AVG(s.lat)::float8 AS lat,
              COUNT(*)::int AS cnt, BOOL_OR(s.grid_is_origin) AS origin,
              (MIN(EXTRACT(EPOCH FROM (s.datetm + interval '7 hours'))) / 60)::float8 AS tm
         FROM monitoring.fire_spread s
        WHERE ${W} AND s.role <> 'isolated'
        GROUP BY s.cluster_id, s.grid_ix, s.grid_iy
        ORDER BY s.cluster_id, tm LIMIT ${CELL_LIMIT}`, params);

```

```javascript
    return NextResponse.json({
      cellDeg: 0.0027, // 300m x 300m
      arrows: a.rows.map((r) => ({
        from: [r.plon, r.plat], to: [r.lon, r.lat], bearing: r.bearing_deg,
        dist: r.parent_dist_m, lagMin: r.parent_lag_min, cluster: r.cluster_id, hop: r.hop, tm: r.tm,
      })),
      cells: c.rows.map((r) => ({
        cluster: r.cluster_id, ix: r.grid_ix, iy: r.grid_iy,
        lon: r.lon, lat: r.lat,
        order: 1, // Angka dikunci 1
        count: r.cnt, origin: r.origin, tm: r.tm,
      })),

```

---

#### C. Perbaikan `spreadLayers.js`

Sesuaikan deskripsi UI, ukuran `cellDeg` (`0.0027`), dan posisi kotak agar berpusat tepat di titik api (`c.lon, c.lat`) dengan rentang 300m x 300m serta menampilkan angka `1`:

```javascript
export const SPREAD_LAYERS = [
  { key: 'origin', label: 'Titik awal', desc: 'Node induk klaster', color: '#FFD65A', shape: 'ring' },
  { key: 'arrows', label: 'Arah penyebaran', desc: 'Panah induk → anak', color: '#60DCFF', shape: 'arrow' },
  { key: 'grid', label: 'Kotak grid', desc: 'Rentang 300m × 300m per titik', color: '#FFFFFF', shape: 'box' },
  { key: 'order', label: 'Nomor kotak', desc: 'Label titik aktif (1)', color: '#EBF0F5', shape: 'num' },
];

```

Pada `buildSpreadLayers` di `spreadLayers.js`:

```javascript
export function buildSpreadLayers({ data, vis, zoom, curMin, cumulative = true }) {
  if (!data) return [];
  const ok = spreadTimeTest(curMin, cumulative);
  const cellDeg = data.cellDeg || 0.0027; // ~300m
  const half = cellDeg / 2;
  const layers = [];

  if (vis.grid || vis.order) {
    const cells = data.cells.filter((c) => ok(c.tm));
    if (vis.grid) {
      layers.push(new PolygonLayer({
        id: 'spread-grid',
        data: cells,
        visible: vis.grid,
        pickable: false,
        stroked: true,
        filled: true,
        lineWidthUnits: 'pixels',
        getLineWidth: (c) => (c.origin ? 2 : 1),
        getPolygon: (c) => {
          // Berpusat di titik api (300m x 300m) atau fallback ke indeks grid
          const cx = c.lon ?? (c.ix + 0.5) * cellDeg;
          const cy = c.lat ?? (c.iy + 0.5) * cellDeg;
          return [
            [cx - half, cy - half],
            [cx + half, cy - half],
            [cx + half, cy + half],
            [cx - half, cy + half],
          ];
        },
        getFillColor: (c) => (c.origin ? [255, 214, 90, 95] : [255, 255, 255, 14]),
        getLineColor: (c) => (c.origin ? [255, 214, 90, 255] : [255, 255, 255, 70]),
        parameters: NO_DEPTH,
        updateTriggers: { getPolygon: [cellDeg] },
      }));
    }
    if (vis.order && zoom >= 8.5) {
      layers.push(new TextLayer({
        id: 'spread-order',
        data: cells,
        pickable: false,
        getPosition: (c) => [
          c.lon ?? (c.ix + 0.5) * cellDeg,
          c.lat ?? (c.iy + 0.5) * cellDeg,
        ],
        getText: () => '1', // Hanya menampilkan angka 1
        getSize: (c) => (c.origin ? 14 : 11),
        sizeUnits: 'pixels',
        getColor: (c) => (c.origin ? [255, 232, 140, 255] : [235, 240, 245, 230]),
        fontWeight: 700,
        outlineWidth: 2,
        outlineColor: [10, 12, 16, 220],
        fontSettings: { sdf: true },
        getTextAnchor: 'middle',
        getAlignmentBaseline: 'center',
        parameters: NO_DEPTH,
      }));
    }
  }

```

Setelah mengubah `fire_dir.py`, jalankan ulang script analisisnya (`python scripts/fire_dir.py --days 30`) agar tabel `monitoring.fire_spread` di-update dengan topologi panah dan ukuran grid yang baru. Apakah Anda ingin panah hanya keluar dari `Titik awal` (1-hop star) saja, atau tetap mengizinkan panah berantai (hop ke-2, ke-3) jika jarak titik baru sudah lebih dari 3 km dari titik awal?