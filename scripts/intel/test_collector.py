"""Regression tests for collector.py. Run: python3 -m unittest scripts/intel/test_collector.py -v
All network access is mocked."""
import importlib.util, json, os, stat, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("collector", HERE / "collector.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

URL = {k: u for k, (_, u) in c.SOURCES.items()}
RSS = b"<rss><channel><item><title>A</title><link>http://x/1</link><pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"
INSIDE = [-103.6, 31.4]    # near Pecos, inside Reeves
OUTSIDE = [-101.0, 31.4]   # well east of Reeves
QUAKE = json.dumps({"features": [
    {"id": "in", "properties": {"mag": 3.1}, "geometry": {"type": "Point", "coordinates": INSIDE}},
    {"properties": {"mag": 2.6}, "geometry": {"type": "Point", "coordinates": INSIDE}},   # no id
    {"id": "out", "properties": {"mag": 4.0}, "geometry": {"type": "Point", "coordinates": OUTSIDE}},
]}).encode()
CSV = ("country,gppd_idnr,name,latitude,longitude,capacity_mw,primary_fuel\n"
       "USA,P1,Inside Plant,31.4,-103.6,120.5,Gas\n"
       "USA,P2,Outside Plant,31.4,-101.0,50,Solar\n"
       "USA,P3,Bad Coords,abc,def,nan,Solar\n").encode()
PAYLOADS = {URL["eia_news"]: RSS, URL["rigzone_news"]: RSS, URL["earthquakes"]: QUAKE, URL["power_plants"]: CSV}


class Base(unittest.TestCase):
    def setUp(self):
        self.fail_urls = set()
        self.payloads = dict(PAYLOADS)
        self._orig = c.fetch
        c.fetch = self._fake
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "o.json"

    def tearDown(self):
        c.fetch = self._orig

    def _fake(self, url):
        if url in self.fail_urls:
            raise OSError("boom")
        return self.payloads[url]

    def run_main(self, *extra):
        sys.argv = ["collector", "--output", str(self.out), *extra]
        return c.main()

    def written(self):
        return json.loads(self.out.read_text())


class MalformedRecords(Base):
    def test_quake_without_id_is_skipped_not_fatal(self):
        status, feats = c.collect("earthquakes", "USA")
        self.assertEqual(status["status"], "OK")
        self.assertEqual(len(feats), 2)             # the 2 records that have ids
        self.assertEqual(status["skipped_records"], 1)

    def test_bad_coordinates_kept_without_geometry(self):
        status, feats = c.collect("power_plants", "USA")
        bad = [f for f in feats if f["properties"]["title"] == "Bad Coords"][0]
        self.assertIsNone(bad["geometry"])
        self.assertIsNone(bad["properties"]["reported_capacity_mw"])


class PartialFailure(Base):
    def test_failed_source_rows_carried_over_and_marked_stale(self):
        self.assertEqual(self.run_main("--sources", "eia_news", "power_plants"), 0)
        before = {f["properties"]["source"] for f in self.written()["features"]}
        self.assertEqual(before, {"eia_news", "power_plants"})
        self.fail_urls = {URL["power_plants"]}
        self.assertEqual(self.run_main("--sources", "eia_news", "power_plants"), 1)
        data = self.written()
        plants = [f for f in data["features"] if f["properties"]["source"] == "power_plants"]
        self.assertTrue(plants, "power plant rows were erased by a partial failure")
        self.assertTrue(all(f["properties"]["stale"] for f in plants))
        st = {s["source"]: s for s in data["sources"]}
        self.assertEqual(st["power_plants"]["status"], "UNAVAILABLE")
        self.assertEqual(st["power_plants"]["carried_over"], len(plants))
        self.assertEqual(data["status"], "PARTIAL")

    def test_total_failure_leaves_file_untouched(self):
        self.run_main("--sources", "eia_news")
        original = self.out.read_text()
        self.fail_urls = set(PAYLOADS.values()) and set(URL.values())
        self.assertEqual(self.run_main("--sources", "eia_news"), 2)
        self.assertEqual(self.out.read_text(), original)

    def test_no_carry_over_across_different_regions(self):
        self.run_main("--sources", "eia_news", "power_plants")                    # no region
        self.fail_urls = {URL["power_plants"]}
        self.run_main("--sources", "eia_news", "power_plants", "--region", "reeves-tx")
        data = self.written()
        self.assertFalse([f for f in data["features"] if f["properties"]["source"] == "power_plants"])


class Region(Base):
    def test_reeves_filter_keeps_inside_drops_outside(self):
        self.assertEqual(self.run_main("--sources", "power_plants", "earthquakes", "--region", "reeves-tx"), 0)
        names = {f["properties"]["title"] for f in self.written()["features"] if f["properties"].get("category") == "power_plant"}
        self.assertEqual(names, {"Inside Plant"})
        quake_ids = {f["properties"]["upstream_id"] for f in self.written()["features"] if f["properties"].get("category") == "earthquake"}
        self.assertEqual(quake_ids, {"in"})

    def test_located_feature_without_geometry_dropped_under_region(self):
        self.run_main("--sources", "power_plants", "--region", "reeves-tx")
        titles = {f["properties"]["title"] for f in self.written()["features"]}
        self.assertNotIn("Bad Coords", titles)

    def test_empty_region_result_is_ok_not_failure(self):
        self.payloads[URL["earthquakes"]] = json.dumps({"features": [
            {"id": "out", "properties": {}, "geometry": {"type": "Point", "coordinates": OUTSIDE}}]}).encode()
        # a quiet day inside the region must not be reported as a failed source
        status, feats = c.collect("earthquakes", "USA", c.load_region("reeves-tx"))
        self.assertEqual(status["status"], "OK")
        self.assertEqual(status["count"], 0)
        self.assertEqual(status["parsed_before_region_filter"], 1)

    def test_region_status_recorded(self):
        self.run_main("--sources", "power_plants", "--region", "reeves-tx")
        self.assertEqual(self.written()["region"], "Reeves County, Texas")

    def test_polygon_with_hole(self):
        region = {"name": "t", "precision": "t", "geometry": {"type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}}
        pt = lambda x, y: {"type": "Point", "coordinates": [x, y]}
        self.assertTrue(c.in_region(pt(1, 1), region))
        self.assertFalse(c.in_region(pt(5, 5), region))     # inside the hole
        self.assertFalse(c.in_region(pt(11, 1), region))
        self.assertFalse(c.in_region(None, region))


class Hardening(Base):
    def test_rss_with_entity_declaration_refused(self):
        self.payloads[URL["eia_news"]] = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss><channel><item><title>&a;</title></item></channel></rss>'
        status, feats = c.collect("eia_news", "USA")
        self.assertEqual(status["status"], "UNAVAILABLE")
        self.assertIn("DOCTYPE", status["error"])

    def test_output_permissions_not_owner_only(self):
        self.run_main("--sources", "eia_news")
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
