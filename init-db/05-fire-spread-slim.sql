-- Skema ringkas fire_spread: hanya titik awal & titik induk (daun tidak disimpan).
-- direction = array datar float8, triple per anak: [lon, lat, menit_epoch_UTC, lon, lat, menit, ...]

BEGIN;

-- 1. Simpan tabel lama dulu (jangan dihapus sebelum hasil baru dicek)
ALTER TABLE IF EXISTS monitoring.fire_spread RENAME TO fire_spread_old;

-- 2. Tabel baru
CREATE TABLE IF NOT EXISTS monitoring.fire_spread (
    uid_alert   text        NOT NULL,
    source      text        NOT NULL,              -- 'firms' | 'sipongi'
    datetm      timestamp   NOT NULL,              -- UTC (naive), sama seperti tabel alert
    lon         float8      NOT NULL,
    lat         float8      NOT NULL,
    cluster_id  uuid        NOT NULL,
    is_origin   boolean     NOT NULL DEFAULT false,
    is_parent   boolean     NOT NULL DEFAULT false,
    direction   float8[]    NOT NULL DEFAULT '{}', -- [lon,lat,t, lon,lat,t, ...] per anak
    analyzed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fire_spread_slim_pk PRIMARY KEY (uid_alert)
);

CREATE INDEX IF NOT EXISTS fire_spread_slim_datetm_idx ON monitoring.fire_spread (datetm);
CREATE INDEX IF NOT EXISTS fire_spread_slim_cluster_idx ON monitoring.fire_spread (cluster_id);

COMMIT;

-- 3. Setelah dicek di peta & fire_dir.py sudah dijalankan ulang:
--    DROP TABLE monitoring.fire_spread_old;
--    (ruang disk kembali setelah DROP; pada tabel yang dipertahankan gunakan VACUUM FULL)

