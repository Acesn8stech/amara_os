"""Tests for install_into_god_mode.py. Run: python3 -m unittest scripts/frac/test_install.py -v"""
import importlib.util, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("inst", HERE / "install_into_god_mode.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)


def fake_god_mode():
    root = Path(tempfile.mkdtemp()) / "god-mode"
    for rel, text in {
        "package.json": json.dumps({"name": "amara_os", "scripts": {"oa": "node scripts/open-acreage/run.ts", "dev": "next dev"}}, indent=2) + "\n",
        "scripts/open-acreage/run.ts": "// oa runner",
        "src/app/page.tsx": "export default function Page() { return null }",
        "src/canvas/globe.ts": "// the globe",
        "src/components/Globe.tsx": "// globe component",
        "public/globe.svg": "<svg/>",
        "hermes/open-acreage.jobs.json": "{}",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def snapshot(root, rels):
    return {p.relative_to(root).as_posix(): p.read_bytes() for r in rels if (root / r).exists()
            for p in sorted((root / r).rglob("*")) if p.is_file()}


class Install(unittest.TestCase):
    def setUp(self):
        self.t = fake_god_mode()

    def run_main(self, *extra):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = inst.main(["--target", str(self.t), *extra])
        return rc, json.loads(out.getvalue())

    def test_dry_run_changes_nothing_and_lists_the_plan(self):
        before = snapshot(self.t, [".", ])
        rc, summary = self.run_main()
        self.assertEqual((rc, summary["mode"]), (0, "DRY RUN"))
        self.assertEqual(snapshot(self.t, ["."]), before)
        paths = {f["path"] for f in summary["files"]}
        self.assertIn("scripts/frac/frac_forecast.py", paths)
        self.assertIn("hermes/frac-demand.jobs.json", paths)
        self.assertEqual(sorted(summary["npm_scripts_to_add"]), ["fd", "fd:desk", "fd:forecast", "fd:live", "fd:watch", "test:frac"])

    def test_apply_adds_files_scripts_and_hermes_lane_and_keeps_existing_scripts(self):
        self.run_main("--apply")
        for rel in ("scripts/frac/frac_forecast.py", "scripts/frac/run_suite.py", "scripts/frac/adapters/fracfocus_to_frac.py",
                    "docs/FRAC-DEMAND-PHASE0-SPEC.md", "hermes/frac-demand.jobs.json"):
            self.assertTrue((self.t / rel).is_file(), rel)
        scripts = json.loads((self.t / "package.json").read_text())["scripts"]
        self.assertEqual(scripts["oa"], "node scripts/open-acreage/run.ts")        # untouched
        self.assertEqual(scripts["dev"], "next dev")
        self.assertTrue(scripts["fd"].startswith("python3 scripts/frac/run_suite.py"))
        lane = json.loads((self.t / "hermes/frac-demand.jobs.json").read_text())
        self.assertEqual(lane["lane"], "frac-demand")
        self.assertEqual(lane["services"][0]["name"], "frac_live")
        self.assertEqual(lane["jobs"], [])                                       # nothing scheduled
        self.assertNotIn("schedule", json.dumps(lane).replace("NOT scheduled", "").replace("not scheduled", "").replace("(NOT a schedule)", ""))
        plist = (self.t / "integration/god-mode/com.amara.frac-live.plist").read_text()
        self.assertIn("KeepAlive", plist)
        self.assertNotIn("<key>StartInterval", plist)
        self.assertNotIn("<key>StartCalendarInterval", plist)

    def test_the_globe_and_ui_are_byte_identical_after_apply(self):
        protected = ["src/app", "src/canvas", "src/components", "public"]
        before = snapshot(self.t, protected)
        self.run_main("--apply")
        self.assertEqual(snapshot(self.t, protected), before)
        self.assertEqual(before["src/canvas/globe.ts"], b"// the globe")

    def test_nothing_in_the_install_set_targets_a_protected_path(self):
        for _, rel in inst.file_map(inst.SOURCE_ROOT):
            self.assertFalse(any(rel == p or rel.startswith(p + "/") for p in inst.PROTECTED), rel)

    def test_a_protected_destination_is_refused(self):
        original = inst.file_map
        inst.file_map = lambda source: [(HERE / "frac_forecast.py", "src/canvas/injected.py")]
        try:
            with self.assertRaises(SystemExit) as cm:
                inst.make_plan(self.t)
            self.assertIn("protected", str(cm.exception))
        finally:
            inst.file_map = original

    def test_a_protected_change_during_apply_is_detected(self):
        plan = inst.make_plan(self.t)
        original = inst.protected_digest
        calls = {"n": 0}
        def flaky(target):
            calls["n"] += 1
            return f"digest-{calls['n']}"                  # differs between the before and after calls
        inst.protected_digest = flaky
        try:
            with self.assertRaises(SystemExit) as cm:
                inst.apply_plan(plan)
            self.assertIn("protected path changed", str(cm.exception))
        finally:
            inst.protected_digest = original

    def test_refuses_a_target_that_is_not_god_mode(self):
        other = Path(tempfile.mkdtemp())
        (other / "package.json").write_text("{}")
        with self.assertRaises(SystemExit):
            inst.main(["--target", str(other)])

    def test_refuses_to_install_into_itself(self):
        with self.assertRaises(SystemExit) as cm:
            inst.make_plan(self.t, source=self.t)          # a God-Mode-shaped repo that is also the source
        self.assertIn("itself", str(cm.exception))
        with self.assertRaises(SystemExit):
            inst.main(["--target", str(inst.SOURCE_ROOT)])

    def test_a_conflicting_existing_script_aborts_with_nothing_written(self):
        pkg = json.loads((self.t / "package.json").read_text())
        pkg["scripts"]["fd"] = "echo my own fd"
        (self.t / "package.json").write_text(json.dumps(pkg))
        before = snapshot(self.t, ["."])
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--apply")
        self.assertIn("different scripts", str(cm.exception))
        self.assertEqual(snapshot(self.t, ["."]), before)

    def test_second_apply_is_a_no_op(self):
        self.run_main("--apply")
        before = snapshot(self.t, ["."])
        rc, summary = self.run_main("--apply")
        self.assertEqual(snapshot(self.t, ["."]), before)
        self.assertTrue(all(f["status"] == "same" for f in summary["files"]))
        self.assertEqual(summary["npm_scripts_to_add"], [])

    def test_no_bytecode_is_copied(self):
        pyc_dir = HERE / "__pycache__"
        pyc_dir.mkdir(exist_ok=True)
        (pyc_dir / "x.cpython-313.pyc").write_bytes(b"x")
        try:
            self.run_main("--apply")
            self.assertFalse(list(self.t.rglob("*.pyc")))
        finally:
            shutil.rmtree(pyc_dir, ignore_errors=True)

    def test_the_installer_and_its_own_test_are_not_copied(self):
        self.run_main("--apply")
        self.assertFalse((self.t / "scripts/frac/install_into_god_mode.py").exists())
        self.assertFalse((self.t / "scripts/frac/test_install.py").exists())

    def test_the_installed_copy_passes_all_of_its_own_tests(self):
        self.run_main("--apply")
        files = sorted(p.relative_to(self.t).as_posix() for p in (self.t / "scripts/frac").rglob("test_*.py"))
        p = subprocess.run([sys.executable, "-m", "unittest", *files], cwd=self.t, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
