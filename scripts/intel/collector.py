#!/usr/bin/env python3
"""AMARA public intelligence collector. Python 3.10+.

Changes from the original ChatGPT draft (each covered by test_collector.py):
  * A failed/empty source no longer erases its rows from the previous output;
    they are carried over and marked stale (only when region/country match).
  * One malformed record no longer kills a whole source; it is skipped and counted.
  * Optional --region filter (GeoJSON polygon) for point features.
  * RSS with DOCTYPE/ENTITY declarations is rejected (entity-expansion guard).
  * Output file is world-readable (0644), not the 0600 tempfile default.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

UNKNOWN = "UNKNOWN"
REGIONS_DIR = Path(__file__).resolve().parent / "regions"
MAX_BYTES = 32 * 1024 * 1024

SOURCES = {
    "eia_news": (
        "rss",
        "https://www.eia.gov/rss/todayinenergy.xml",
    ),
    "rigzone_news": (
        "rss",
        "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
    ),
    "earthquakes": (
        "geojson",
        "https://earthquake.usgs.gov/earthquakes/feed/"
        "v1.0/summary/2.5_day.geojson",
    ),
    "power_plants": (
        "csv",
        "https://raw.githubusercontent.com/wri/"
        "global-power-plant-database/master/output_database/"
        "global_power_plant_database.csv",
    ),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def point(longitude, latitude):
    lon, lat = number(longitude), number(latitude)
    if lon is None or lat is None:
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return {"type": "Point", "coordinates": [lon, lat]}


def rss_date(value):
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            return UNKNOWN
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return UNKNOWN


def epoch_date(value):
    value = number(value)
    if value is None:
        return UNKNOWN
    try:
        return datetime.fromtimestamp(
            value / 1000, timezone.utc
        ).isoformat()
    except (ValueError, OverflowError, OSError):
        return UNKNOWN


def load_region(spec):
    """Load a region polygon from a GeoJSON file path or a name in regions/."""
    path = Path(spec).expanduser()
    if not path.is_file():
        path = REGIONS_DIR / f"{spec}.geojson"
    data = json.loads(path.read_text(encoding="utf-8"))
    geometry = data
    if data.get("type") == "FeatureCollection":
        geometry = data["features"][0]["geometry"]
    elif data.get("type") == "Feature":
        geometry = data["geometry"]
    if geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Region must be a Polygon or MultiPolygon")
    props = data.get("properties") or {}
    if data.get("type") == "FeatureCollection":
        props = data["features"][0].get("properties") or {}
    return {
        "name": props.get("name") or path.stem,
        "precision": props.get("precision") or UNKNOWN,
        "geometry": geometry,
    }


def _ring_contains(ring, lon, lat):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            if lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def in_region(geometry, region):
    """True if a Point geometry lies inside the region polygon (holes respected)."""
    if not geometry or geometry.get("type") != "Point":
        return False
    lon, lat = geometry["coordinates"][0], geometry["coordinates"][1]
    region_geometry = region["geometry"]
    polygons = (
        [region_geometry["coordinates"]]
        if region_geometry["type"] == "Polygon"
        else region_geometry["coordinates"]
    )
    for rings in polygons:
        if _ring_contains(rings[0], lon, lat) and not any(
            _ring_contains(hole, lon, lat) for hole in rings[1:]
        ):
            return True
    return False


def feature(source, key, geometry, properties):
    digest = hashlib.sha256(str(key).encode()).hexdigest()[:24]
    return {
        "type": "Feature",
        "id": f"{source}:{digest}",
        "geometry": geometry,
        "properties": {
            "source": source,
            "verification": "SOURCE-REPORTED (not independently verified)",
            **properties,
        },
    }


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AMARA-Public-Intelligence/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Source exceeded the download size limit")
        return data


def collect(source, country, region=None):
    kind, url = SOURCES[source]
    retrieved_at = utc_now()

    try:
        data = fetch(url)
        features = []
        skipped = 0

        if kind == "rss":
            head = data.upper()
            if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                raise ValueError("RSS contains DOCTYPE/ENTITY declarations; refused")
            for item in ET.fromstring(data).findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                published = item.findtext("pubDate")
                key = item.findtext("guid") or link or title
                if not key:
                    continue

                features.append(feature(source, key, None, {
                    "category": "news",
                    "title": title or UNKNOWN,
                    "source_url": link or url,
                    "published_at": rss_date(published),
                    "location_status": "UNKNOWN — not geocoded",
                    "freshness": "CHECK_PUBLISHED_DATE",
                }))

        elif kind == "geojson":
            payload = json.loads(data)
            for item in payload.get("features", []):
                props = item.get("properties") or {}
                geometry = item.get("geometry") or {}
                coordinates = geometry.get("coordinates") or []
                if geometry.get("type") != "Point" or len(coordinates) < 2:
                    skipped += 1
                    continue
                if not item.get("id"):
                    skipped += 1  # no stable id -> cannot dedupe or cite; skip, don't fail the feed
                    continue

                features.append(feature(source, item["id"],
                    point(coordinates[0], coordinates[1]), {
                        "category": "earthquake",
                        "upstream_id": item["id"],
                        "title": props.get("title") or UNKNOWN,
                        "magnitude": number(props.get("mag")),
                        "source_url": props.get("url") or url,
                        "published_at": epoch_date(props.get("time")),
                        "source_updated_at": epoch_date(props.get("updated")),
                        "freshness": "CHECK_SOURCE_UPDATED_DATE",
                    }
                ))

        else:
            rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
            for row in rows:
                if country != "ALL" and row.get("country") != country:
                    continue

                key = row.get("gppd_idnr")
                if not key:
                    skipped += 1
                    continue

                features.append(feature(source, key,
                    point(row.get("longitude"), row.get("latitude")), {
                        "category": "power_plant",
                        "upstream_id": key,
                        "title": row.get("name") or UNKNOWN,
                        "country": row.get("country") or UNKNOWN,
                        "fuel": row.get("primary_fuel") or UNKNOWN,
                        "reported_capacity_mw": number(row.get("capacity_mw")),
                        "capacity_year": row.get("year_of_capacity_data") or UNKNOWN,
                        "original_source": row.get("source") or UNKNOWN,
                        "source_url": row.get("url") or url,
                        "dataset_url": url,
                        "freshness": "HISTORICAL — current status unverified",
                        "available_parcel_power_mw": None,
                        "available_parcel_power_status": UNKNOWN,
                    }
                ))

        # Deduplicate within each source.
        unique = {}
        for item in features:
            item["properties"]["retrieved_at"] = retrieved_at
            unique[item["id"]] = item

        parsed = len(unique)  # EMPTY means the feed parsed to nothing (suspicious),
        # not "nothing in the region" (a legitimate answer).

        if region:
            # Located features must be inside the region. Features with no usable
            # location cannot be shown to be inside it, so they are dropped, except
            # news, which is not spatial and is kept with its UNKNOWN location label.
            kept = {}
            for key_, item in unique.items():
                if item["properties"].get("category") == "news":
                    kept[key_] = item
                elif in_region(item["geometry"], region):
                    item["properties"]["region_filter"] = (
                        f"{region['name']} ({region['precision']})"
                    )
                    kept[key_] = item
            unique = kept

        status = {
            "source": source,
            "status": "OK" if parsed else "EMPTY",
            "source_url": url,
            "retrieved_at": retrieved_at,
            "count": len(unique),
            "skipped_records": skipped,
        }
        if region:
            status["region"] = region["name"]
            status["parsed_before_region_filter"] = parsed
        return status, list(unique.values())

    except Exception as exc:
        # A failed source remains visibly unavailable.
        return {
            "source": source,
            "status": "UNAVAILABLE",
            "source_url": url,
            "retrieved_at": retrieved_at,
            "count": 0,
            "skipped_records": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }, []


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)  # NamedTemporaryFile defaults to 0600
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_previous(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data.get("type") == "FeatureCollection" else None
    except (OSError, ValueError):
        return None


def carry_over(previous, statuses, region_name, country):
    """Previous rows for sources that did not refresh, marked stale.

    Only used when the previous file was produced for the same region and country;
    otherwise old rows would be wrong for this run and are not carried.
    """
    for s in statuses:
        s["carried_over"] = 0
    if not previous:
        return []
    if previous.get("region") != region_name or previous.get("country") != country:
        return []
    failed = {s["source"]: s for s in statuses if s["status"] != "OK"}
    carried = []
    for item in previous.get("features", []):
        props = item.get("properties", {})
        source = props.get("source")
        if source in failed:
            props["stale"] = True
            props["stale_reason"] = (
                f"source {failed[source]['status']} on this run; "
                "showing rows from the last successful run"
            )
            carried.append(item)
    for s in statuses:
        s["carried_over"] = sum(
            1 for i in carried if i["properties"].get("source") == s["source"]
        )
    return carried


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--country", default="USA",
                        help="Power-plant country code, or ALL")
    parser.add_argument("--region",
                        help="GeoJSON polygon file, or a name in scripts/intel/regions/ "
                             "(e.g. reeves-tx). Filters located features to inside it.")
    parser.add_argument("--sources", nargs="+", choices=list(SOURCES),
                        default=list(SOURCES))
    args = parser.parse_args()

    region = load_region(args.region) if args.region else None
    country = args.country.upper()
    output = Path(args.output).expanduser()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda source: collect(source, country, region),
            dict.fromkeys(args.sources),
        ))

    statuses = [status for status, _ in results]
    features = [item for _, items in results for item in items]

    # If no source refreshed, leave the previous output untouched.
    if not any(s["status"] == "OK" for s in statuses):
        print(json.dumps({"status": "UNAVAILABLE", "sources": statuses}, indent=2))
        return 2

    # Partial run: keep each failed source's last good rows (marked stale)
    # instead of silently erasing them.
    region_name = region["name"] if region else None
    features += carry_over(load_previous(output), statuses, region_name, country)

    payload = {
        "type": "FeatureCollection",
        "generated_at": utc_now(),
        "region": region_name,
        "country": country,
        "status": "OK" if all(s["status"] == "OK" for s in statuses) else "PARTIAL",
        "sources": statuses,
        "features": features,
    }
    atomic_write(output, payload)

    print(json.dumps({
        "status": payload["status"],
        "output": args.output,
        "features": len(features),
        "sources": statuses,
    }, indent=2))
    return 0 if payload["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
