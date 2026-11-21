#!/usr/bin/env python3
"""Fetch a full county boundary polygon from OpenStreetMap (Nominatim) to a GeoJSON file.

Run on a machine with normal internet access. Data (c) OpenStreetMap contributors, ODbL 1.0.
Example: python3 scripts/intel/fetch_region.py --county Reeves --state Texas \
             --out scripts/intel/regions/reeves-tx.geojson
"""
import argparse, json, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--county", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    qs = urllib.parse.urlencode({
        "county": a.county, "state": a.state, "country": "us", "format": "jsonv2",
        "polygon_geojson": 1, "polygon_threshold": 0.0001, "limit": 5})
    req = urllib.request.Request("https://nominatim.openstreetmap.org/search?" + qs,
                                 headers={"User-Agent": "AMARA-Public-Intelligence/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        hits = json.loads(r.read(16 * 1024 * 1024))
    hit = next((h for h in hits if h.get("addresstype") == "county"
                and h.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")), None)
    if not hit:
        raise SystemExit("No county polygon found; check spelling.")
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {
        "name": hit["display_name"],
        "precision": "OSM boundary, simplified at threshold 0.0001 degrees; not a survey-grade boundary",
        "source": f"OpenStreetMap {hit['osm_type']} {hit['osm_id']} via Nominatim, retrieved "
                  + datetime.now(timezone.utc).date().isoformat(),
        "license": "ODbL 1.0 (c) OpenStreetMap contributors"}, "geometry": hit["geojson"]}]}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(fc), encoding="utf-8")
    print(f"wrote {a.out}: {hit['display_name']}")


if __name__ == "__main__":
    main()
