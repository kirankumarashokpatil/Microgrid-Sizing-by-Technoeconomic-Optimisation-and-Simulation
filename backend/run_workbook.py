"""
run_workbook.py — backend-only Excel-in / Excel-out entry point
================================================================
No frontend, no JSON. One project workbook goes in; the same results come back
out as extra sheets. It is a thin CLI over core/workbook_io.py — all the real
logic lives there, so the API and this CLI share one contract.

Usage
-----
  # 1. Make a starter workbook you can edit:
  python run_workbook.py --make-template project.xlsx

  # 2. Fill in the 'Inputs' sheet, then run it:
  python run_workbook.py --in project.xlsx
  python run_workbook.py --in project.xlsx --out project_result.xlsx

  # Optional: override the profiles dataset for this run:
  python run_workbook.py --in project.xlsx --profiles "data/8760_PV&Load Profiles.xlsx"

Run from the backend/ directory (same as main.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.workbook_io import build_example, build_template, run_workbook


def parse_args():
    p = argparse.ArgumentParser(description="DIP — Excel-in / Excel-out backend runner")
    p.add_argument("--make-template", metavar="PATH",
                   help="Write a ready-to-edit starter workbook to PATH and exit.")
    p.add_argument("--make-example", metavar="PATH",
                   help="Write a fully-filled single-file example input (Inputs + Parcels "
                        "+ Loads + embedded profiles) to PATH and exit.")
    p.add_argument("--in", dest="in_path", metavar="PATH",
                   help="Project workbook to run (its 'Inputs' sheet).")
    p.add_argument("--out", dest="out_path", metavar="PATH", default=None,
                   help="Where to write results. Default: <in>_out.xlsx.")
    p.add_argument("--profiles", metavar="PATH", default=None,
                   help="Override the profiles dataset for this run.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.make_template:
        path = build_template(args.make_template)
        print(f"✅ Template written: {path}")
        print("   Edit the 'Inputs' sheet, then run:  "
              f"python run_workbook.py --in {path}")
        return 0

    if args.make_example:
        path = build_example(args.make_example)
        print(f"✅ Example input written: {path}")
        print(f"   Run it as-is (self-contained):  python run_workbook.py --in {path}")
        return 0

    if not args.in_path:
        print("❌ Nothing to do. Pass --in <workbook> or --make-template <path>.")
        print("   Try:  python run_workbook.py --make-template project.xlsx")
        return 2

    in_path = Path(args.in_path)
    if not in_path.exists():
        print(f"❌ Input workbook not found: {in_path}")
        return 1

    print(f"📂 Reading inputs: {in_path.name}")
    try:
        out = run_workbook(in_path, args.out_path, profiles_override=args.profiles)
    except Exception as exc:  # keep the CLI friendly — no stack dump for input errors
        print(f"❌ Run failed: {exc}")
        return 1

    print(f"✅ Done. Results written to: {out}")
    print("   Sheets added: Run Info · Design · KPIs · Frontier · Flows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
