-- Enable PostGIS and UUID extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create required schemas
CREATE SCHEMA IF NOT EXISTS monitoring;
CREATE SCHEMA IF NOT EXISTS system;

-- Create system settings table
CREATE TABLE IF NOT EXISTS system.setting (
    key VARCHAR(255) PRIMARY KEY,
    variable VARCHAR(255) UNIQUE,
    value TEXT,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Seed default settings for FIRMS and SIPONGI
INSERT INTO system.setting (key, variable, value, description)
VALUES 
    ('firms.map_key', 'firms.map_key', '70e8e413e8f4661fb7479c384395c2f8', 'NASA FIRMS API Map Key'),
    ('processing.target_extent', 'processing.target_extent', '108,-4.2,119.3,4.5', 'Kalimantan Bounding Box (min_lon, min_lat, max_lon, max_lat)'),
    ('processing.firms_extent', 'processing.firms_extent', '108,-4.2,119.3,4.5', 'Kalimantan Bounding Box for FIRMS'),
    ('processing.target_provinces', 'processing.target_provinces', '["Kalimantan Barat", "Kalimantan Tengah", "Kalimantan Timur", "Kalimantan Selatan", "Kalimantan Utara"]', 'Target provinces for SIPONGI')
ON CONFLICT (key) DO NOTHING;

-- Create spatial reference tables for enrichment/intersection if not exist
-- 1. Concession reference layer
CREATE TABLE IF NOT EXISTS monitoring.unit_concession_sp (
    uid VARCHAR(255) PRIMARY KEY,
    namobj VARCHAR(255),
    geometry geometry(Geometry, 4326)
);
CREATE INDEX IF NOT EXISTS unit_concession_sp_geom_idx ON monitoring.unit_concession_sp USING GIST (geometry);

-- 2. Village administrative reference layer
CREATE TABLE IF NOT EXISTS monitoring.base_adminvillage_alert (
    id SERIAL PRIMARY KEY,
    wadmkd VARCHAR(255),
    wadmkc VARCHAR(255),
    wadmkk VARCHAR(255),
    wadmpr VARCHAR(255),
    geometry geometry(Geometry, 4326)
);
CREATE INDEX IF NOT EXISTS base_adminvillage_alert_geom_idx ON monitoring.base_adminvillage_alert USING GIST (geometry);

-- 3. Landcover reference layer (supports both long and wide formats)
CREATE TABLE IF NOT EXISTS monitoring.base_landcover_change (
    id SERIAL PRIMARY KEY,
    pl_year INT,
    pl VARCHAR(255),
    pl_2019 VARCHAR(255),
    pl_2020 VARCHAR(255),
    pl_2021 VARCHAR(255),
    pl_2022 VARCHAR(255),
    pl_2023 VARCHAR(255),
    pl_2024 VARCHAR(255),
    pl_2025 VARCHAR(255),
    geometry geometry(Geometry, 4326)
);
CREATE INDEX IF NOT EXISTS base_landcover_change_geom_idx ON monitoring.base_landcover_change USING GIST (geometry);
