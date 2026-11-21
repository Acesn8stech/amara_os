#!/usr/bin/env python3
"""Install the frac-demand tooling into the AMARA God Mode project (the repo with scripts/open-acreage).

Adds, and nothing else:
  scripts/frac/**                        the forecast, adapters, evaluation suite and tests
  docs/FRAC-DEMAND-PHASE0-SPEC.md        the spec
  hermes/frac-demand.jobs.json           a Hermes lane in the same format as open-acreage.jobs.json
  package.json scripts                   fd, fd:forecast, test:frac  (existing scripts are never changed)

HARD RULE: the globe and UI stay untouched. The installer refuses to write under src/app, src/canvas,
src/components or public, and checks they are byte-identical after an apply.

Dry run by default; add --apply to write.   python3 scripts/frac/install_into_god_mode.py --target ~/god-mode
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROTECTED = ("src/app", "src/canvas", "src/components", "public")
NPM_SCRIPTS = {
    "fd": "python3 scripts/frac/run_suite.py",
    "fd:forecast": "python3 scripts/frac/frac_forecast.py",
    "test:frac": "python3 -m unittest scripts/frac/test_frac_forecast.py scripts/frac/adapters/test_adapters.py "
                 "scripts/frac/test_run_suite.py scripts/frac/test_install.py",
}
HERMES_SRC = "integration/god-mode/frac-demand.jobs.json"
HERMES_DST = "hermes/frac-demand.jobs.json"
SPEC = "docs/FRAC-DEMAND-PHASE0-SPEC.md"


def file_map(source):
    pairs = []
    for p in sorted((source / "scripts" / "frac").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            pairs.append((p, p.relative_to(source).as_posix()))
    for rel_src, rel_dst in ((SPEC, SPEC), (HERMES_SRC, HERMES_DST)):
        if (source / rel_src).is_file():
            pairs.append((source / rel_src, rel_dst))
    return pairs


def protected_digest(target):
    h = hashlib.sha256()
    for rel in PROTECTED:
        base = target / rel
        if base.exists():
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    h.update(p.relative_to(target).as_posix().encode())
                    h.update(p.read_bytes())
    return h.hexdigest()


def check_target(target, source):
    target = target.resolve()
    if target == source.resolve():
        raise SystemExit("REFUSING: the target is this repository itself.")
    if not (target / "package.json").is_file() or not (target / "scripts" / "open-acreage" / "run.ts").is_file():
        raise SystemExit(f"REFUSING: {target} does not look like AMARA God Mode "
                         "(needs package.json and scripts/open-acreage/run.ts).")
    return target


def make_plan(target, source=SOURCE_ROOT):
    target = check_target(target, source)
    files = []
    for src, rel in file_map(source):
        if any(rel == pr or rel.startswith(pr + "/") for pr in PROTECTED):
            raise SystemExit(f"REFUSING to write a protected path: {rel}")
        dst = target / rel
        status = "add" if not dst.exists() else ("same" if dst.read_bytes() == src.read_bytes() else "update")
        files.append({"path": rel, "status": status, "_src": src})
    pkg = json.loads((target / "package.json").read_text())
    existing = pkg.get("scripts", {})
    conflicts = {k: {"existing": existing[k], "wanted": v} for k, v in NPM_SCRIPTS.items()
                 if k in existing and existing[k] != v}
    return {"target": str(target), "files": files, "npm_scripts_to_add": [k for k in NPM_SCRIPTS if k not in existing],
            "npm_script_conflicts": conflicts, "pkg": pkg}


def apply_plan(plan):
    if plan["npm_script_conflicts"]:
        raise SystemExit("REFUSING: package.json already has different scripts with these names: "
                         + json.dumps(plan["npm_script_conflicts"]))
    target = Path(plan["target"])
    before = protected_digest(target)
    for f in plan["files"]:
        if f["status"] == "same":
            continue
        dst = target / f["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(f["_src"].read_bytes())
    if plan["npm_scripts_to_add"]:
        pkg = plan["pkg"]
        pkg.setdefault("scripts", {})
        for k in plan["npm_scripts_to_add"]:
            pkg["scripts"][k] = NPM_SCRIPTS[k]
        (target / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    if protected_digest(target) != before:
        raise SystemExit("ERROR: a protected path changed. This must never happen; inspect with git status.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="path to the AMARA God Mode repo (or worktree)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    args = ap.parse_args(argv)
    plan = make_plan(Path(args.target).expanduser())
    summary = {"target": plan["target"], "mode": "APPLY" if args.apply else "DRY RUN",
               "files": [{"path": f["path"], "status": f["status"]} for f in plan["files"]],
               "npm_scripts_to_add": plan["npm_scripts_to_add"], "npm_script_conflicts": plan["npm_script_conflicts"],
               "protected_untouched": list(PROTECTED)}
    if args.apply:
        apply_plan(plan)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
