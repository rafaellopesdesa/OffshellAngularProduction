#!/usr/bin/env python3
"""Test a truncated angular expansion against a merged sample in one PDF."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from offshell_production.angular_closure import COMPONENT_SLUGS, closure_projections, read_closure
from offshell_production.angular_closure_pdf import write_closure_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="one merged campaign ROOT file")
    parser.add_argument("--output", type=Path, help="single closure PDF")
    parser.add_argument("--lumi-fb", type=float, help="override stored luminosity in inverse femtobarns")
    parser.add_argument("--variables", nargs="+", metavar="NAME", help="angular projections to include (default: all)")
    parser.add_argument("--components", nargs="+", choices=COMPONENT_SLUGS,
                        help="terms to include; 00_00 is always present (default: all five)")
    parser.add_argument("--bins", type=int, default=24, help="bins in each 1D projection (default: %(default)s)")
    parser.add_argument("--map-bins", type=int, default=12, help="bins per axis in the cosine plane (default: %(default)s)")
    parser.add_argument("--step-size", default="100 MB", help="ROOT chunk size (default: %(default)s)")
    parser.add_argument("--list-variables", action="store_true", help="list available angular projections and exit")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing PDF after successful rendering")
    args = parser.parse_args(argv)
    if args.list_variables:
        for name, spec in closure_projections().items():
            print(f"{name:36s} {spec.label}")
        return 0
    if args.input is None or args.output is None:
        parser.error("INPUT and --output are required unless --list-variables is used")
    if args.lumi_fb is not None and (not math.isfinite(args.lumi_fb) or args.lumi_fb <= 0):
        parser.error("--lumi-fb must be finite and positive")
    try:
        if args.output.suffix.lower() != ".pdf":
            raise ValueError("--output must have a .pdf suffix")
        if args.output.exists() and not args.overwrite:
            raise FileExistsError(f"output already exists: {args.output}; pass --overwrite to replace it")
        print(f"Reading angular moments from {args.input}", flush=True)
        report = read_closure(
            args.input, lumi_pb=None if args.lumi_fb is None else args.lumi_fb * 1000,
            step_size=args.step_size, bins=args.bins, map_bins=args.map_bins,
            variables=args.variables, components=args.components,
        )
        print(f"Rendering closure for {report.entries:,} events in {len(report.contexts)} contexts", flush=True)
        pages = write_closure_report(report, args.output, overwrite=args.overwrite)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Angular closure error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {pages} pages to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
