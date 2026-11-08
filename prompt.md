# Prompt (versi diperbaiki): Analisis Titik Awal & Arah Penyebaran Hotspot

## Tujuan
Dari data hotspot (FIRMS: MODIS, VIIRS SNPP/NOAA20/NOAA21; SIPONGI) tentukan untuk setiap rekaman:
1. **Klaster kebakaran** tempat ia berada.
2. **Titik awal** klaster (deteksi paling awal).
3. **Arah penyebaran**: setiap titik punya satu *parent* (titik terdekat yang lebih dulu terdeteksi). Titik A yang menjadi parent bagi beberapa titik berarti A punya panah ke beberapa titik.
4. **Kotak grid bernomor**: nomor = urutan waktu kotak pertama kali terbakar dalam klaster (1 = kotak titik awal, di-highlight).

## Keputusan desain
- Dihitung di **backend**, hasil disimpan per rekaman (kunci `uid_alert`) di tabel `monitoring.fire_spread`. Frontend hanya membaca.
- Metode: **ST-DBSCAN sederhana + tautan waktu**
  - Klaster: dua titik terhubung bila jarak <= 3 km dan selisih waktu <= 24 jam; klaster = komponen terhubung.
  - Titik awal: deteksi paling awal di klaster (tie-break `uid_alert`).
  - Parent: titik terdekat yang lebih awal **minimal 60 menit** (`MIN_LINK_LAG_MIN`, mencegah panah antar titik satu lintasan satelit) dan <= 3 km, <= 24 jam. Panah = parent -> anak. Bearing dihitung dari parent ke anak. Titik tanpa kandidat parent (mis. deteksi dalam 60 menit pertama) = `branch_root`.
  - Klaster < 3 titik = `isolated` (tanpa panah/titik awal).
- Grid: sel 0.01 derajat (~1.1 km), nomor per klaster berdasarkan waktu deteksi pertama di sel.

## Kolom per rekaman (`monitoring.fire_spread`)
`cluster_id, cluster_size, role (origin|spread|branch_root|isolated), is_origin, origin_uid, parent_uid, parent_dist_m, parent_lag_min, bearing_deg, child_count, hop, grid_cell, grid_ix, grid_iy, grid_order, grid_is_origin`

## Deliverable
1. `fire_spread_migration.sql` : tabel + index.
2. `analyze_fire_spread.py` : skrip batch (CLI + fungsi async untuk scheduler), idempotent (upsert).
3. `/api/hotspots/spread` : JSON `{ arrows, cells, origins }` dengan filter start/end/source/bbox.
4. (Tahap berikut) Layer frontend di `MapView.jsx`: `PathLayer/LineLayer` + kepala panah, dan `TextLayer` + `PolygonLayer` untuk kotak bernomor.

## Kriteria penerimaan
- Menjalankan skrip dua kali menghasilkan hasil identik (deterministik, `cluster_id` = uuid5 dari `origin_uid`).
- Setiap klaster >= 3 titik punya tepat satu `is_origin = true`, dan `grid_order = 1` pada kotak titik awal.
- Tidak ada siklus parent (parent selalu lebih awal secara waktu).
- Parameter (jarak, jendela waktu, ukuran sel) ada di blok `# CONFIG` di atas skrip.

## Keterbatasan yang perlu dicatat
Arah = urutan waktu deteksi satelit, bukan jalur api sebenarnya. Interval lintasan satelit (beberapa jam) membuat arah kasar; titik kembar antar sensor (< 300 m) dapat menghasilkan panah sangat pendek (disaring di API dengan `minDist`).