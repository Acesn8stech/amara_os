#!/usr/bin/env python3
"""RRC Horizontal Drilling Permits (DAF318) -> permits.csv for frac_forecast.py.

Layout source: RRC manual https://www.rrc.texas.gov/media/yi0p3f3j/horizontaldrillingpermits.pdf
(file S.DAF318, sequential, 360 bytes in the manual = 354 data bytes + 6 trailing FILLER
bytes that real files omit; real files are 354 bytes per record).

The file has NO planned lateral length and no lat/long, only PERMIT-TOTAL-DEPTH.
lateral_ft is therefore left BLANK and labeled UNKNOWN; the forecast core falls back
to per-well intensity. Nothing is invented.

Usage: daf318_to_permits.py --input ~/frac-data/rrc/daf318 --output permits.csv [--county REEVES]
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date

RECORD_LEN = 354
ACCEPTED_LENGTHS = (354, 360)

# name: (1-based start position, length). From the RRC manual's POSITION column.
LAYOUT = {
    "permit_number": (1, 7),
    "permit_sequence": (8, 2),
    "district": (10, 2),
    "county_name": (12, 13),
    "api_county": (25, 3),
    "api_unique": (28, 5),
    "operator_number": (33, 6),
    "operator_name": (39, 32),
    "lease_name": (71, 32),
    "issued_cc": (103, 2),
    "issued_yy": (105, 2),
    "issued_mm": (107, 2),
    "issued_dd": (109, 2),
    "total_depth": (111, 5),
}

FIELDS = ["api", "permit_date", "operator", "county", "lateral_ft", "lateral_basis",
          "total_depth_ft", "permit_number", "lease_name"]
LATERAL_BASIS = "UNKNOWN: RRC publishes no planned lateral length"


def field(line, name):
    start, length = LAYOUT[name]
    return line[start - 1:start - 1 + length].strip()


def issued_date(line):
    parts = [field(line, k) for k in ("issued_cc", "issued_yy", "issued_mm", "issued_dd")]
    if not all(p.isdigit() and len(p) == 2 for p in parts):
        return None
    cc, yy, mm, dd = parts
    year, month, day = int(cc + yy), int(mm), int(dd)
    if not 1900 <= year <= 2100:
        return None
    try:
        return date(year, month, day).isoformat()   # rejects Feb 30, month 13, day 0, etc.
    except ValueError:
        return None


def parse_record(line):
    """Return (row, None) or (None, reason)."""
    if len(line) not in ACCEPTED_LENGTHS:
        return None, "bad_length"
    county, unique = field(line, "api_county"), field(line, "api_unique")
    if not (county.isdigit() and unique.isdigit() and len(county) == 3 and len(unique) == 5):
        return None, "bad_api"
    date = issued_date(line)
    if not date:
        return None, "bad_issue_date"
    depth = field(line, "total_depth")
    return {
        "api": county + unique,
        "permit_date": date,
        "operator": field(line, "operator_name"),
        "county": field(line, "county_name"),
        "lateral_ft": "",
        "lateral_basis": LATERAL_BASIS,
        "total_depth_ft": depth if depth.isdigit() and int(depth) > 0 else "",
        "permit_number": field(line, "permit_number"),
        "lease_name": field(line, "lease_name"),
    }, None


def read_lines(path):
    # latin-1 maps one byte to one character, so byte positions stay exact.
    with open(path, "rb") as handle:
        for raw in handle:
            line = raw.rstrip(b"\r\n").decode("latin-1")
            if line.strip():
                yield line


def sanity_check(path, sample=300, min_ok=0.95):
    """Stop early if the real file does not match the manual's layout."""
    ok = total = 0
    reasons = defaultdict(int)
    for i, line in enumerate(read_lines(path)):
        if i >= sample:
            break
        total += 1
        row, why = parse_record(line)
        if row:
            ok += 1
        else:
            reasons[why] += 1
    if total == 0:
        raise SystemExit("ERROR: input file is empty.")
    if ok / total < min_ok:
        raise SystemExit(
            f"ERROR: only {ok}/{total} sample records parse with the manual's layout "
            f"({dict(reasons)}). The file does not match the layout; run with --probe "
            "to see the first record, and do not trust any output.")


def probe(path):
    for line in read_lines(path):
        print(f"length={len(line)}")
        print("".join(str(i // 10 % 10) if i % 10 == 0 else " " for i in range(len(line))))
        print(line)
        for name in LAYOUT:
            print(f"  {name:16} = {field(line, name)!r}")
        return


def convert(path, out_path, county=None):
    sanity_check(path)
    rejected, written = defaultdict(int), 0
    want = county.strip().upper() if county else None
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDS)
        writer.writeheader()
        for line in read_lines(path):
            row, why = parse_record(line)
            if not row:
                rejected[why] += 1
                continue
            if want and row["county"].upper() != want:
                continue
            writer.writerow(row)
            written += 1
    return {"rows_written": written, "rejected": dict(rejected), "county_filter": want,
            "lateral_ft": "BLANK for every row (RRC has no planned lateral length)"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--output")
    ap.add_argument("--county")
    ap.add_argument("--probe", action="store_true", help="print the first record with a ruler and parsed fields")
    args = ap.parse_args(argv)
    if args.probe:
        probe(args.input)
        return 0
    if not args.output:
        ap.error("--output is required")
    print(json.dumps(convert(args.input, args.output, args.county), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
