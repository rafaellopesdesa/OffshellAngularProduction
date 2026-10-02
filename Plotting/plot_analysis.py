#!/usr/bin/env python3
"""Plot a merged analysis ROOT file as a single kinematic/acceptance PDF."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from offshell_production.plotting import observable_specs, read_report
from offshell_production.plotting_pdf import write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="merged analysis ROOT file with physical weight_nominal_pb")
    parser.add_argument("--output", type=Path, help="single output PDF")
    parser.add_argument("--lumi-fb", type=float, help="override stored luminosity, in inverse femtobarns")
    parser.add_argument("--variables", nargs="+", metavar="NAME", help="observable names without a level prefix (default: all)")
    parser.add_argument("--list-variables", action="store_true", help="list all supported observables and exit")
    parser.add_argument("--bins-json", type=Path, help="JSON object mapping observable names to bin-edge arrays")
    parser.add_argument("--step-size", default="100 MB", help="uproot chunk size (default: %(default)s)")
    parser.add_argument("--log-y", action="store_true", help="logarithmic distribution axes when bins are nonnegative; ratios stay linear")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing PDF after successful rendering")
    args = parser.parse_args(argv)
    if args.list_variables:
        for name, spec in observable_specs().items():
            unit = f" [{spec.unit}]" if spec.unit else ""
            print(f"{name:40s} {spec.label}{unit}")
        return 0
    if args.input is None or args.output is None:
        parser.error("INPUT and --output are required unless --list-variables is used")
    if args.lumi_fb is not None and (not math.isfinite(args.lumi_fb) or args.lumi_fb <= 0):
        parser.error("--lumi-fb must be finite and positive")
    try:
        if args.output.exists() and not args.overwrite:
            raise FileExistsError(f"output already exists: {args.output}; pass --overwrite to replace it")
        if args.output.suffix.lower() != ".pdf":
            raise ValueError("--output must have a .pdf suffix")
        bins = None
        if args.bins_json:
            bins = json.loads(args.bins_json.read_text(encoding="utf-8"))
            if not isinstance(bins, dict):
                raise ValueError("--bins-json must contain an object mapping observable names to edge arrays")
        print(f"Reading {args.input}", flush=True)
        report = read_report(
            args.input, lumi_pb=None if args.lumi_fb is None else args.lumi_fb * 1000,
            variables=args.variables, bin_edges=bins, step_size=args.step_size,
        )
        print(f"Rendering {len(report.observables)} observables for {report.entries:,} events", flush=True)
        pages = write_report(report, args.output, overwrite=args.overwrite, log_y=args.log_y)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Plotting error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {pages} pages to {args.output}")
    print(f"Expected yield: {report.sumw_pb * report.lumi_pb:.7g} all; {report.selected_sumw_pb * report.lumi_pb:.7g} RECO-selected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
