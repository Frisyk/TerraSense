#!/usr/bin/env python3
"""
Konversi GeoJSON keluaran forensik menjadi Shapefile (.zip)
yang siap di-upload ke Google Earth Engine (GEE) Web Code Editor.
"""

import sys
import shutil
import zipfile
from pathlib import Path
import geopandas as gpd

def convert_to_zip(geojson_path: Path, out_dir: Path):
    if not geojson_path.exists():
        print(f"  [skip] {geojson_path.name} tidak ditemukan.")
        return None

    name = geojson_path.stem
    zip_path = out_dir / f"{name}.zip"
    tmp_dir = out_dir / f"_tmp_{name}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        print(f"  Mengonversi {geojson_path.name} -> {name}.zip ...", end=" ", flush=True)
        gdf = gpd.read_file(geojson_path)
        if gdf.empty:
            print("kosong (dilewati)")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        # Singkat nama kolom jika melebihi 10 karakter (standar dBase/ESRI Shapefile)
        rename_map = {
            "titik_awal_serentak": "ttkAwalSrt",
            "t_start_ms": "t_start_ms",
            "t_end_ms": "t_end_ms",
            "stab_awal": "stab_awal",
            "stab_edge": "stab_edge",
            "hingga_wib": "hingga_wib",
        }
        gdf = gdf.rename(columns={k: v for k, v in rename_map.items() if k in gdf.columns})

        shp_file = tmp_dir / f"{name}.shp"
        gdf.to_file(shp_file, driver="ESRI Shapefile", encoding="utf-8")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in tmp_dir.iterdir():
                z.write(f, arcname=f.name)

        print(f"SELESAI ({len(gdf):,} fitur)")
        return zip_path
    except Exception as e:
        print(f"GAGAL ({e})")
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def main():
    base_dir = Path(__file__).resolve().parent
    out_dir = base_dir / "out"
    if len(sys.argv) > 1:
        out_dir = Path(sys.argv[1]).resolve()

    print(f"\n{'='*60}")
    print(f"Konversi GeoJSON -> Shapefile .zip untuk GEE")
    print(f"Direktori: {out_dir}")
    print(f"{'='*60}")

    targets = ["roots.geojson", "areas_daily.geojson", "edges.geojson", "points.geojson"]
    success_zips = []
    for tgt in targets:
        p = out_dir / tgt
        res = convert_to_zip(p, out_dir)
        if res:
            success_zips.append(res)

    print(f"\n{'='*60}")
    print(f"Hasil siap di-upload ke Google Earth Engine:")
    for z in success_zips:
        size_kb = z.stat().st_size / 1024
        print(f"  -> {z.name} ({size_kb:.1f} KB)")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()

