# FIRE-ANIMATION: Docker & Backfill Hotspot Pipeline

Repositori ini berisi setup Docker untuk PostgreSQL/PostGIS dan container backend/runner untuk menjalankan script ingestion data hotspot kebakaran hutan (NASA FIRMS & KLHK SIPONGI) ke database spatial.

---

## 1. Arsitektur Sistem

- **PostGIS (`db`)**: PostgreSQL 16 + PostGIS 3.4 (port `5432`). Menyimpan data titik api (raw dan enriched) di schema `monitoring`.
- **Backend / Script Runner (`backend`)**: Container Python 3.11 dengan dependensi geospasial (`geopandas`, `shapely`, `SQLAlchemy`, `GeoAlchemy2`, `psycopg2`, `requests`).
- **Frontend Visualizer (`frontend`)**: Web App Next.js 14 + OpenLayers pada port `3000` (`http://localhost:3000`) dengan fitur filter waktu sampai tingkat jam, pewarnaan titik api dinamis sesuai tanggal, dan timeline animation player.
- **Initial SQL (`init-db/01-init.sql`)**: Otomatis dijalankan saat pertama kali container database dibuat. Membuat ekstensi PostGIS, schema `monitoring` & `system`, default settings, serta skeleton tabel referensi (`unit_concession_sp`, `base_adminvillage_alert`, `base_landcover_change`).

---

## 2. Cara Menjalankan Service

### A. Pastikan Service Aktif
Jalankan container PostGIS dan Backend di background:
```bash
docker compose up -d
```

Cek status container:
```bash
docker compose ps
```

---

## 3. Menjalankan Script Backfill

Semua script dijalankan melalui container `backend` agar tidak memerlukan instalasi Python atau dependensi GIS di komputer lokal (host).

### A. NASA FIRMS (`backfill_alert_fire_firms.py`)

#### 1. Mode Raw Only (Unduh data mentah titik api ke tabel `monitoring.alert_fire_{source}_raw`):
```bash
docker compose run --rm backend python scripts/backfill_alert_fire_firms.py --source viirssnpp --start 2024-01-01 --end 2024-01-05 --raw-only
```

Pilihan `--source`:
- `viirssnpp` (VIIRS Suomi-NPP)
- `modis` (MODIS Terra/Aqua)
- `viirsnoaa20` (VIIRS NOAA-20)
- `viirsnoaa21` (VIIRS NOAA-21)
- `all` (Semua sensor)

#### 2. Mode Full (Download + Spatial Intersect Konsesi, Desa, & Tutupan Lahan):
```bash
docker compose run --rm backend python scripts/backfill_alert_fire_firms.py --source viirssnpp --start 2024-01-01 --end 2024-01-05
```

#### 3. Resume jika proses terhenti:
```bash
docker compose run --rm backend python scripts/backfill_alert_fire_firms.py --source viirssnpp --start 2024-01-01 --resume
```

---

### B. KLHK SIPONGI (`backfill_alert_fire_sipongi.py`)

Menjalankan backfill historis hotspot dari API SIPONGI untuk provinsi di Pulau Kalimantan:
```bash
docker compose run --rm backend python scripts/backfill_alert_fire_sipongi.py --start 2024-01-01 --end 2024-01-10
```

Parameter tambahan:
- `--confidence high` atau `--confidence all` (default: high)
- `--chunk-days 30` (default: 30 hari per chunk request)
- `--resume` (lanjutkan dari checkpoint terakhir)
- `--no-raw` (hanya proses enriched tanpa simpan ke tabel raw)

---

## 4. Frontend Visualizer (Next.js & OpenLayers)

Aplikasi web visualisasi peta interaktif dapat diakses langsung melalui browser di:
👉 **`http://localhost:3000`**

### Fitur Utama:
1. **Interactive OpenLayers Map**: Menampilkan titik hotspot di atas peta pulau Kalimantan dengan tema Dark, Satelit, dan Street.
2. **Date-Based Color Coding**: Setiap tanggal kejadian titik api secara otomatis memiliki warna neon/kontras yang berbeda sehingga urutan kronologis titik api terlihat jelas.
3. **Filter Waktu sampai Tingkat Jam (`datetime-local`)**: Filter data berdasarkan rentang waktu spesifik (mis. `2024-01-01 00:00` sampai `2024-01-03 14:00`).
4. **Timeline Animation Scrubber**: Slider waktu interaktif di bagian bawah dengan tombol **Play / Pause**, kecepatan putar (1x, 2x, 4x), dan mode akumulasi untuk melihat animasi persebaran kebakaran jam demi jam.
5. **Interactive Legend**: Legenda tanggal di pojok kanan bawah yang menampilkan jumlah titik per hari dan dapat diklik untuk filter cepat per tanggal.
6. **Tooltip Detail Titik Api**: Hover atau klik titik api untuk melihat informasi waktu lokal WIB, koordinat, tingkat keyakinan (*confidence*), kecerahan (*brightness*), serta provinsi/sumber data.

---

## 5. Memeriksa Data di Database

Anda dapat langsung mengecek tabel dan data yang masuk ke PostgreSQL menggunakan `docker compose exec`:

### Masuk ke psql:
```bash
docker compose exec db psql -U postgres -d geonode_data
```

### Query contoh di psql:
```sql
-- Cek daftar tabel di schema monitoring
\dt monitoring.*

-- Cek jumlah baris raw FIRMS
SELECT count(*) FROM monitoring.alert_fire_viirssnpp_raw;

-- Cek 5 data titik api FIRMS terbaru
SELECT acq_date, acq_time, latitude, longitude, confidence FROM monitoring.alert_fire_viirssnpp_raw LIMIT 5;

-- Cek jumlah baris data SIPONGI
SELECT count(*) FROM monitoring.alert_fire_sipongi_raw;
```

---

## 5. Konfigurasi Environment (`.env`)

File konfigurasi `.env` telah disediakan dengan konfigurasi standar:
- `POSTGRES_DB`: Nama database (default: `geonode_data`)
- `POSTGRES_USER`: User database (default: `postgres`)
- `POSTGRES_PASSWORD`: Password database (default: `postgres`)
- `POSTGRES_PORT`: Port host mapping (default: `5432`)
- `FIRMS_MAP_KEY`: NASA FIRMS API key
