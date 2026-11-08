-- ============================================================================
-- Langkah 2 & 3: Tabel tipis terpadu `fire_points` & Rollup `fire_hourly_grid`
-- ============================================================================

-- 1. Tabel tipis terpadu `monitoring.fire_points`
CREATE TABLE IF NOT EXISTS monitoring.fire_points (
  id         bigserial PRIMARY KEY,
  kind       smallint     NOT NULL,           -- 0 = FIRMS, 1 = SIPONGI
  sensor     text         NOT NULL,
  lat        double precision NOT NULL,
  lon        double precision NOT NULL,
  datetm     timestamptz  NOT NULL,           -- UTC
  datetm_loc timestamp    NOT NULL,           -- jam dinding WIB
  hr_loc     integer GENERATED ALWAYS AS
             (floor(extract(epoch FROM datetm_loc) / 3600)::int) STORED,
  bright     real NOT NULL DEFAULT 0,
  conf       text,
  prov       text,
  geom       geometry(Point, 4326) GENERATED ALWAYS AS
             (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED
);

CREATE UNIQUE INDEX IF NOT EXISTS fire_points_uq
  ON monitoring.fire_points (kind, sensor, lat, lon, datetm);
CREATE INDEX IF NOT EXISTS fire_points_datetm_idx ON monitoring.fire_points (datetm);
CREATE INDEX IF NOT EXISTS fire_points_hr_idx     ON monitoring.fire_points (hr_loc);
CREATE INDEX IF NOT EXISTS fire_points_geom_idx   ON monitoring.fire_points USING GIST (geom);

-- 2. Fungsi sinkronisasi inkremental `monitoring.sync_fire_points`
CREATE OR REPLACE FUNCTION monitoring.sync_fire_points(p_since timestamptz)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_firms_tz boolean;
  v_sip_tz   boolean;
  v_tbl      text;
BEGIN
  -- A. Sinkronisasi FIRMS (prioritas monitoring.alert_fire_firms, fallback ke tabel _raw)
  IF to_regclass('monitoring.alert_fire_firms') IS NOT NULL THEN
    SELECT data_type ILIKE '%with time zone%' INTO v_firms_tz
    FROM information_schema.columns
    WHERE table_schema = 'monitoring' AND table_name = 'alert_fire_firms' AND column_name = 'datetm';

    IF COALESCE(v_firms_tz, true) THEN
      INSERT INTO monitoring.fire_points (kind, sensor, lat, lon, datetm, datetm_loc, bright, conf, prov)
      SELECT 0, COALESCE(source::text, 'FIRMS'), latitude, longitude, datetm, datetm_loc,
             COALESCE(brightness, 0), COALESCE(confidence::text, '-'), 'Kalimantan'
      FROM monitoring.alert_fire_firms
      WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND datetm >= p_since
      ON CONFLICT DO NOTHING;
    ELSE
      INSERT INTO monitoring.fire_points (kind, sensor, lat, lon, datetm, datetm_loc, bright, conf, prov)
      SELECT 0, COALESCE(source::text, 'FIRMS'), latitude, longitude, datetm AT TIME ZONE 'UTC', datetm_loc,
             COALESCE(brightness, 0), COALESCE(confidence::text, '-'), 'Kalimantan'
      FROM monitoring.alert_fire_firms
      WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        AND datetm >= (p_since AT TIME ZONE 'UTC')
      ON CONFLICT DO NOTHING;
    END IF;
  ELSE
    FOREACH v_tbl IN ARRAY ARRAY['alert_fire_viirssnpp_raw', 'alert_fire_viirsnoaa20_raw', 'alert_fire_viirsnoaa21_raw', 'alert_fire_modis_raw'] LOOP
      IF to_regclass('monitoring.' || v_tbl) IS NOT NULL THEN
        EXECUTE format($f$
          INSERT INTO monitoring.fire_points (kind, sensor, lat, lon, datetm, datetm_loc, bright, conf, prov)
          SELECT 0, COALESCE(instrument::text, satellite::text, 'FIRMS'), latitude, longitude,
                 datetm::timestamptz, datetm_loc,
                 COALESCE(brightness, 0), COALESCE(confidence::text, '-'), 'Kalimantan'
          FROM monitoring.%I
          WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND datetm >= $1
          ON CONFLICT DO NOTHING
        $f$, v_tbl) USING p_since;
      END IF;
    END LOOP;
  END IF;

  -- B. Sinkronisasi SIPONGI
  IF to_regclass('monitoring.alert_fire_sipongi_raw') IS NOT NULL THEN
    SELECT data_type ILIKE '%with time zone%' INTO v_sip_tz
    FROM information_schema.columns
    WHERE table_schema = 'monitoring' AND table_name = 'alert_fire_sipongi_raw' AND column_name = 'datetm';

    IF COALESCE(v_sip_tz, false) THEN
      INSERT INTO monitoring.fire_points (kind, sensor, lat, lon, datetm, datetm_loc, bright, conf, prov)
      SELECT 1, COALESCE(sumber, 'SIPONGI'), lat, long, datetm, datetm_loc,
             0, COALESCE(confidence_level, confidence::text, 'unknown'),
             COALESCE(nama_provinsi, 'Kalimantan')
      FROM monitoring.alert_fire_sipongi_raw
      WHERE lat IS NOT NULL AND long IS NOT NULL AND datetm >= p_since
      ON CONFLICT DO NOTHING;
    ELSE
      INSERT INTO monitoring.fire_points (kind, sensor, lat, lon, datetm, datetm_loc, bright, conf, prov)
      SELECT 1, COALESCE(sumber, 'SIPONGI'), lat, long, datetm AT TIME ZONE 'UTC', datetm_loc,
             0, COALESCE(confidence_level, confidence::text, 'unknown'),
             COALESCE(nama_provinsi, 'Kalimantan')
      FROM monitoring.alert_fire_sipongi_raw
      WHERE lat IS NOT NULL AND long IS NOT NULL
        AND datetm >= (p_since AT TIME ZONE 'UTC')
      ON CONFLICT DO NOTHING;
    END IF;
  END IF;
END;
$$;

-- 3. Tabel Rollup per jam `monitoring.fire_hourly_grid` (resolusi dasar 0.0125 derajat)
CREATE TABLE IF NOT EXISTS monitoring.fire_hourly_grid (
  kind       smallint NOT NULL,
  hr         integer  NOT NULL,   -- jam dinding WIB: floor(epoch/3600)
  gx         integer  NOT NULL,   -- floor(lon / 0.0125)
  gy         integer  NOT NULL,   -- floor(lat / 0.0125)
  cnt        integer  NOT NULL,
  max_bright real     NOT NULL DEFAULT 0,
  PRIMARY KEY (hr, kind, gx, gy)
);

-- 4. Fungsi refresh `monitoring.refresh_fire_hourly_grid`
CREATE OR REPLACE FUNCTION monitoring.refresh_fire_hourly_grid(p_from_hr integer)
RETURNS void LANGUAGE sql AS $$
  DELETE FROM monitoring.fire_hourly_grid WHERE hr >= p_from_hr;
  INSERT INTO monitoring.fire_hourly_grid (kind, hr, gx, gy, cnt, max_bright)
  SELECT kind, hr_loc,
         floor(lon / 0.0125)::int, floor(lat / 0.0125)::int,
         count(*)::int, max(bright)
  FROM monitoring.fire_points
  WHERE hr_loc >= p_from_hr
  GROUP BY 1, 2, 3, 4;
$$;

