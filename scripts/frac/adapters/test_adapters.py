"""Tests for the two adapters. Run: python3 -m unittest scripts/frac/adapters/test_adapters.py -v

LIMIT, stated plainly: fixtures are built from the RRC manual's layout and the FracFocus
dictionary's column names, NOT copied from the real downloaded files (the build sandbox
cannot reach them). They prove the code does what the layout says; the real-file check is
the runtime sanity gate plus the 10-row hand check on Scott's Mac."""
import csv, importlib.util, io, json, tempfile, unittest, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

daf = load("daf", HERE / "daf318_to_permits.py")
ffz = load("ffz", HERE / "fracfocus_to_frac.py")
core = load("core", HERE.parent / "frac_forecast.py")

# Positions typed independently from the adapter (1-based start, length), per the manual.
POS = [(1, 7, "permit_number"), (12, 13, "county"), (25, 3, "api_county"), (28, 5, "api_unique"),
       (33, 6, "op_no"), (39, 32, "op_name"), (71, 32, "lease"), (103, 8, "date"), (111, 5, "depth")]


def record(length=354, **kw):
    vals = {"permit_number": "0912345", "county": "REEVES", "api_county": "389", "api_unique": "45678",
            "op_no": "123456", "op_name": "TEST OPERATING LLC", "lease": "SMITH UNIT", "date": "20240315",
            "depth": "18500"}
    vals.update(kw)
    buf = [" "] * length
    for start, ln, name in POS:
        text = str(vals[name])[:ln].ljust(ln)
        buf[start - 1:start - 1 + ln] = list(text)
    return "".join(buf)


def write_lines(lines, eol="\n"):
    path = Path(tempfile.mkdtemp()) / "daf318"
    path.write_bytes((eol.join(lines) + eol).encode("latin-1"))
    return str(path)


class Daf318(unittest.TestCase):
    def test_good_record_fields(self):
        row, why = daf.parse_record(record())
        self.assertIsNone(why)
        self.assertEqual(row["api"], "38945678")
        self.assertEqual(row["permit_date"], "2024-03-15")
        self.assertEqual(row["operator"], "TEST OPERATING LLC")
        self.assertEqual(row["county"], "REEVES")
        self.assertEqual(row["total_depth_ft"], "18500")
        self.assertEqual(row["lateral_ft"], "")                      # never invented
        self.assertTrue(row["lateral_basis"].startswith("UNKNOWN"))

    def test_manual_length_360_accepted_and_353_rejected(self):
        self.assertIsNotNone(daf.parse_record(record(360))[0])
        self.assertEqual(daf.parse_record(record(353))[1], "bad_length")

    def test_bad_api_and_bad_dates_rejected_with_reason(self):
        self.assertEqual(daf.parse_record(record(api_unique="4A678"))[1], "bad_api")
        self.assertEqual(daf.parse_record(record(date="00000000"))[1], "bad_issue_date")
        self.assertEqual(daf.parse_record(record(date="20241345"))[1], "bad_issue_date")
        self.assertEqual(daf.parse_record(record(date="20240230"))[1], "bad_issue_date")   # no Feb 30
        self.assertEqual(daf.parse_record(record(date="20240229"))[0]["permit_date"], "2024-02-29")  # leap day ok

    def test_zero_total_depth_is_blank_not_zero(self):
        self.assertEqual(daf.parse_record(record(depth="00000"))[0]["total_depth_ft"], "")

    def test_crlf_and_county_filter_and_counts(self):
        # the sanity gate needs >=95% good records in its sample, so build a mostly-good file
        out = str(Path(tempfile.mkdtemp()) / "p.csv")
        good = [record(api_unique=f"{i:05d}") for i in range(50)] + [record(county="PECOS", api_county="371")] + [record(api_unique="XXXXX")]
        path = write_lines(good, eol="\r\n")
        res = daf.convert(path, out, county="reeves")
        self.assertEqual(res["rows_written"], 50)
        self.assertEqual(res["rejected"], {"bad_api": 1})
        with open(out) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({r["county"] for r in rows}, {"REEVES"})

    def test_sanity_gate_stops_on_wrong_layout(self):
        path = write_lines(["X" * 80 for _ in range(20)])
        with self.assertRaises(SystemExit) as cm:
            daf.sanity_check(path)
        self.assertIn("does not match the layout", str(cm.exception))

    def test_empty_file_stops(self):
        with self.assertRaises(SystemExit):
            daf.sanity_check(write_lines([]))

    def test_probe_runs(self):
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            daf.probe(write_lines([record()]))
        self.assertIn("county_name", buf.getvalue())
        self.assertIn("'REEVES'", buf.getvalue())


HEADER = ["UploadKey", "JobStartDate", "JobEndDate", "APINumber", "StateName", "CountyName", "OperatorName",
          "WellName", "Latitude", "Longitude", "TotalBaseWaterVolume", "Purpose", "IngredientName", "MassIngredient"]


def row(key, start, api, state, county, op, water, purpose, mass):
    return [key, start, start, api, state, county, op, "WELL 1", "31.4", "-103.6", water, purpose, "x", mass]


def make_zip(members):
    path = Path(tempfile.mkdtemp()) / "FracFocusCSV.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, (header, rows) in members.items():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(header)
            w.writerows(rows)
            zf.writestr(name, buf.getvalue())
        zf.writestr("readme csv.txt", "dictionary")
    return str(path)


ROWS = [
    row("u1", "3/14/2023 12:00:00 AM", "42-389-45678-00-00", "Texas", "Reeves", 'Test, "Quoted" Op', "5000000", "Carrier/Base Fluid", ""),
    row("u1", "3/14/2023 12:00:00 AM", "42-389-45678-00-00", "Texas", "Reeves", 'Test, "Quoted" Op', "5000000", "Proppant", "1,000"),
    row("u1", "3/14/2023 12:00:00 AM", "42-389-45678-00-00", "Texas", "Reeves", 'Test, "Quoted" Op', "5000000", "Proppant - Natural", "2500.5"),
    row("u2", "2023-04-01", "42389456790000", "Texas", "Reeves", "OP B", "", "Proppant", ""),         # no water, no mass
    row("u3", "5/1/2023", "42389456800000", "Texas", "Reeves", "OP C", "4000000", "Surfactant", "10"),  # no proppant rows
    row("u4", "5/2/2023", "30015000010000", "New Mexico", "Eddy", "OP D", "1", "Proppant", "5"),       # not Texas
    row("u5", "garbage", "42389456810000", "Texas", "Reeves", "OP E", "1", "Proppant", "5"),          # bad date
    row("u6", "6/1/2023", "42371456820000", "Texas", "Pecos", "OP F", "3000000", "Proppant", "7000"),
]


class FracFocus(unittest.TestCase):
    def run_convert(self, members, **kw):
        out = str(Path(tempfile.mkdtemp()) / "frac.csv")
        res = ffz.convert(make_zip(members), out, **kw)
        with open(out) as handle:
            return res, {r["api"]: r for r in csv.DictReader(handle)}

    def test_job_aggregation_and_units(self):
        res, rows = self.run_convert({"FracFocusRegistry_1.csv": (HEADER, ROWS)})
        a = rows["42389456780000"]
        self.assertEqual(a["frac_start"], "2023-03-14")
        self.assertEqual(float(a["water_gal"]), 5000000.0)
        self.assertEqual(float(a["proppant_lb"]), 3500.5)           # "1,000" + 2500.5, counted once per row
        self.assertEqual(res["job_key_used"], "UploadKey")

    def test_unknown_is_blank_never_zero(self):
        _, rows = self.run_convert({"FracFocusRegistry_1.csv": (HEADER, ROWS)})
        self.assertEqual(rows["42389456790000"]["water_gal"], "")
        self.assertEqual(rows["42389456790000"]["proppant_lb"], "")  # proppant rows but no mass
        self.assertEqual(rows["42389456800000"]["proppant_lb"], "")  # no proppant rows at all
        self.assertEqual(float(rows["42389456800000"]["water_gal"]), 4000000.0)

    def test_zero_water_volume_is_unknown_not_zero(self):
        rows_ = [row("z1", "7/1/2023", "42389456900000", "Texas", "Reeves", "OP Z", "0", "Proppant", "100"),
                 row("z2", "7/2/2023", "42389456910000", "Texas", "Reeves", "OP Z", "abc", "Proppant", "100")]
        res, out = self.run_convert({"FracFocusRegistry_1.csv": (HEADER, rows_)})
        self.assertEqual(out["42389456900000"]["water_gal"], "")
        self.assertEqual(out["42389456910000"]["water_gal"], "")      # non-numeric
        self.assertEqual(res["stats"]["water_unknown"], 2)

    def test_texas_only_and_rejects_counted(self):
        res, rows = self.run_convert({"FracFocusRegistry_1.csv": (HEADER, ROWS)})
        self.assertNotIn("30015000010000", rows)
        self.assertNotIn("42389456810000", rows)                     # bad date
        self.assertEqual(res["stats"]["skipped_not_texas"], 1)
        self.assertEqual(res["stats"]["rejected_bad_start_date"], 1)

    def test_county_filter(self):
        _, rows = self.run_convert({"FracFocusRegistry_1.csv": (HEADER, ROWS)}, county="pecos")
        self.assertEqual(set(rows), {"42371456820000"})

    def test_job_spanning_two_members_is_one_job(self):
        r1 = [ROWS[0], ROWS[1]]
        r2 = [ROWS[2]]
        res, rows = self.run_convert({"FracFocusRegistry_1.csv": (HEADER, r1), "FracFocusRegistry_2.csv": (HEADER, r2)})
        self.assertEqual(res["jobs_written"], 1)
        self.assertEqual(float(rows["42389456780000"]["proppant_lb"]), 3500.5)

    def test_water_conflict_within_job_is_counted_first_kept(self):
        rows_ = [ROWS[0], row("u1", "3/14/2023", "42-389-45678-00-00", "Texas", "Reeves", "X", "9999999", "Proppant", "5")]
        res, out = self.run_convert({"FracFocusRegistry_1.csv": (HEADER, rows_)})
        self.assertEqual(res["stats"]["job_water_conflicts"], 1)
        self.assertEqual(float(out["42389456780000"]["water_gal"]), 5000000.0)

    def test_header_mismatch_stops_loudly(self):
        bad = [c for c in HEADER if c != "TotalBaseWaterVolume"]
        with self.assertRaises(SystemExit) as cm:
            self.run_convert({"FracFocusRegistry_1.csv": (bad, [[""] * len(bad)])})
        self.assertIn("TotalBaseWaterVolume", str(cm.exception))

    def test_no_job_key_column_stops_unless_given(self):
        header = [c for c in HEADER if c != "UploadKey"]
        rows_ = [r[1:] for r in ROWS]
        with self.assertRaises(SystemExit):
            self.run_convert({"FracFocusRegistry_1.csv": (header, rows_)})
        res, _ = self.run_convert({"FracFocusRegistry_1.csv": (header, rows_)}, job_key="APINumber")
        self.assertEqual(res["job_key_used"], "APINumber")

    def test_list_purposes(self):
        with zipfile.ZipFile(make_zip({"FracFocusRegistry_1.csv": (HEADER, ROWS)})) as zf:
            counts = ffz.list_purposes(zf)
        self.assertEqual(counts["Proppant"], 5)   # u1, u2, u4, u5, u6 each have one exact "Proppant" row
        self.assertIn("Proppant - Natural", counts)

    def test_zip_is_streamed_not_extracted(self):
        zpath = make_zip({"FracFocusRegistry_1.csv": (HEADER, ROWS)})
        before = set(Path(zpath).parent.iterdir())
        self.run_convert({"FracFocusRegistry_1.csv": (HEADER, ROWS)})
        self.assertEqual(set(Path(zpath).parent.iterdir()), before)


class ProppantMatching(unittest.TestCase):
    def test_propping_agent_and_typos_count_as_proppant(self):
        for p in ("Proppant", "PROPPANT", "Propping Agent", "propping agent", "Proppan", "Proppant (30/50 Mesh)",
                  "Resin coated proppant", "Proppant - Natural", "Fracturing Sand, Propping Agent"):
            self.assertTrue(ffz.is_proppant(p), p)

    def test_additives_about_proppant_do_not_count(self):
        for p in ("Proppant Transport Polymer", "Proppant Binder 1", "Proppant Resin Coat Activator",
                  "Proppant Suspending Agent", "Proppant Carrier Fluid", "PROPPANT SHIELD",
                  "Activator for TURBOprop Proppant", "Proppant/Friction Reduction"):
            self.assertFalse(ffz.is_proppant(p), p)

    def test_multi_value_purpose_judged_per_token(self):
        self.assertTrue(ffz.is_proppant("Additive, Breaker, Friction Reducer, Gelling Agent, Proppant, Surfactant"))
        self.assertFalse(ffz.is_proppant("Additive, Breaker, Friction Reducer, Gelling Agent, Surfactant"))
        self.assertFalse(ffz.is_proppant("Additive, Proppant Binder 1, Surfactant"))
        self.assertTrue(ffz.is_proppant("Acid, Friction Reducer, Chelating Agent, Propping Agent"))

    def test_unrelated_and_empty(self):
        self.assertFalse(ffz.is_proppant("Friction Reducer"))
        self.assertFalse(ffz.is_proppant(""))
        self.assertFalse(ffz.is_proppant(None))

    def test_propping_agent_job_gets_proppant_mass_through_the_adapter(self):
        rows_ = [row("p1", "8/1/2022", "42389457000000", "Texas", "Reeves", "HAL CO", "6000000", "Propping Agent", "9000"),
                 row("p1", "8/1/2022", "42389457000000", "Texas", "Reeves", "HAL CO", "6000000", "Propping Agent", "1000")]
        out = str(Path(tempfile.mkdtemp()) / "frac.csv")
        res = ffz.convert(make_zip({"FracFocusRegistry_1.csv": (HEADER, rows_)}), out)
        with open(out) as handle:
            r = list(csv.DictReader(handle))[0]
        self.assertEqual(float(r["proppant_lb"]), 10000.0)

    def test_multi_value_rows_are_counted_in_stats(self):
        rows_ = [row("m1", "8/1/2022", "42389457100000", "Texas", "Reeves", "OP M", "6000000",
                     "Additive, Friction Reducer, Proppant", "500")]
        out = str(Path(tempfile.mkdtemp()) / "frac.csv")
        res = ffz.convert(make_zip({"FracFocusRegistry_1.csv": (HEADER, rows_)}), out)
        self.assertEqual(res["stats"]["proppant_rows_from_multi_value_purpose"], 1)


class Diagnose(unittest.TestCase):
    def test_diagnose_breaks_unknowns_down_by_year(self):
        h = HEADER
        rows_ = [
            row("d1", "5/1/2022", "42389460010000", "Texas", "Reeves", "A", "5000000", "Proppant", "100"),
            row("d1", "5/1/2022", "42389460010000", "Texas", "Reeves", "A", "5000000", "Water", ""),
            row("d2", "6/1/2022", "42389460020000", "Texas", "Reeves", "B", "5000000", "Sand Thing", "200"),     # sand named, purpose not matched
            row("d3", "6/1/2012", "42389460030000", "Texas", "Reeves", "C", "5000000", "", ""),                  # header-only era
            row("d4", "6/1/2022", "30015000010000", "New Mexico", "Eddy", "D", "1", "Proppant", "5"),
            row("d5", "7/1/2022", "42389460050000", "Texas", "Reeves", "E", "5000000", "Proppant", ""),   # labeled proppant, no mass
        ]
        for r in rows_:
            r[12] = "Crystalline Silica" if r[11] == "Sand Thing" else r[12]
        zpath = make_zip({"FracFocusRegistry_1.csv": (h, rows_)})
        res = ffz.diagnose(zpath)
        self.assertEqual(res["jobs_total"], 4)                      # New Mexico excluded
        y22, y12 = res["by_year"]["2022"], res["by_year"]["2012"]
        self.assertEqual(y22["jobs"], 3)
        self.assertEqual(y22["proppant_purpose_match"], 2)          # d1 and d5
        self.assertEqual(y22["proppant_purpose_match_with_mass"], 1)  # only d1 has a mass
        self.assertEqual(y22["sand_like_ingredient_with_mass"], 1)
        self.assertEqual(y12["any_ingredient_mass"], 0)
        self.assertEqual(res["top_single_value_purposes_containing_propp"], {"Proppant": 2})


class EndToEndWithCore(unittest.TestCase):
    def test_adapter_output_joins_in_the_core(self):
        """8-digit RRC API and 14-digit FracFocus API must meet at the same well."""
        # the well in both files is permitted 2023-01-01, BEFORE its 2023-03-14 frac
        dpath = write_lines([record(api_unique=f"{i:05d}", date="20230101") for i in range(30)]
                            + [record(api_unique="45678", date="20230101")])
        pout = str(Path(dpath).with_name("permits.csv"))
        daf.convert(dpath, pout)
        zpath = make_zip({"FracFocusRegistry_1.csv": (HEADER, ROWS)})
        fout = str(Path(dpath).with_name("frac.csv"))
        ffz.convert(zpath, fout)
        permits, _ = core.clean_permits(core.read_csv(pout))
        fracs, _ = core.clean_fracs(core.read_csv(fout))
        joined, report = core.join(permits, fracs)
        self.assertEqual(report["joined"], 1)           # only the well that exists in both files
        self.assertEqual(joined[0]["api"], "4238945678")
        self.assertEqual(joined[0]["county"], "REEVES")
        self.assertEqual(joined[0]["lag_days"], 72)     # 2023-01-01 -> 2023-03-14


class SampleJoin(unittest.TestCase):
    def test_sample_join_prints_checkable_rows(self):
        sj = load("sj", HERE.parent / "sample_join.py")
        dpath = write_lines([record(api_unique=f"{i:05d}", date="20230101") for i in range(30)]
                            + [record(api_unique="45678", date="20230101")])
        pout = str(Path(dpath).with_name("permits.csv"))
        daf.convert(dpath, pout)
        fout = str(Path(dpath).with_name("frac.csv"))
        ffz.convert(make_zip({"FracFocusRegistry_1.csv": (HEADER, ROWS)}), fout)
        rows, report = sj.sample(core.read_csv(pout), core.read_csv(fout), county="reeves", n=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["permit_number"], "0912345")
        self.assertEqual(rows[0]["lease_name"], "SMITH UNIT")
        self.assertEqual(report["joined"], 1)


if __name__ == "__main__":
    unittest.main()
