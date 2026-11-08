-- Hasil analisis titik awal & arah penyebaran hotspot (scripts/fire_dir.py)
-- datetm = UTC (naive), sama seperti yang dibaca /api/hotspots/spread.
CREATE TABLE IF NOT EXISTS monitoring.fire_spread (
    uid_alert       TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    datetm          TIMESTAMP NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    lat             DOUBLE PRECISION NOT NULL,
    cluster_id      UUID NOT NULL,
    cluster_size    INTEGER NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('origin','spread','branch_root','isolated')),
    is_origin       BOOLEAN NOT NULL DEFAULT FALSE,
    origin_uid      TEXT,
    parent_uid      TEXT,
    parent_dist_m   DOUBLE PRECISION,
    parent_lag_min  DOUBLE PRECISION,
    bearing_deg     DOUBLE PRECISION,
    child_count     INTEGER NOT NULL DEFAULT 0,
    hop             INTEGER NOT NULL DEFAULT 0,
    grid_cell       TEXT,
    grid_ix         INTEGER,
    grid_iy         INTEGER,
    grid_order      INTEGER,
    grid_is_origin  BOOLEAN NOT NULL DEFAULT FALSE,
    analyzed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fire_spread_datetm_idx  ON monitoring.fire_spread (datetm);
CREATE INDEX IF NOT EXISTS fire_spread_cluster_idx ON monitoring.fire_spread (cluster_id);
CREATE INDEX IF NOT EXISTS fire_spread_parent_idx  ON monitoring.fire_spread (parent_uid);
CREATE INDEX IF NOT EXISTS fire_spread_origin_idx  ON monitoring.fire_spread (datetm) WHERE is_origin;
CREATE INDEX IF NOT EXISTS fire_spread_lonlat_idx  ON monitoring.fire_spread (lon, lat);

-- Kolom tambahan (aman untuk tabel yang sudah ada)
ALTER TABLE monitoring.fire_spread ADD COLUMN IF NOT EXISTS parent_lon DOUBLE PRECISION;
ALTER TABLE monitoring.fire_spread ADD COLUMN IF NOT EXISTS parent_lat DOUBLE PRECISION;
ALTER TABLE monitoring.fire_spread ADD COLUMN IF NOT EXISTS edge_censored BOOLEAN NOT NULL DEFAULT FALSE;
