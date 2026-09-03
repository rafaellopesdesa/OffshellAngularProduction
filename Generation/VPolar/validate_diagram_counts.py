#!/usr/bin/env python3
"""Validate the four exclusive VPolar ``gg -> e+e-mu+mu-`` processes.

This is an installation-time smoke test, not a process build.  It imports the
downloaded ``SM_Loop_ZPolar`` UFO into the installed and patched MG5_aMC
3.4.2 tree, asks MadGraph to generate each amplitude in memory, and refuses
to continue unless every channel has the audited diagram count and flavor
routing.

Run, for example, with::

    python Generation/VPolar/validate_diagram_counts.py \
        --mg5-root /path/to/MG5_aMC_v3_4_2 \
        --model /path/to/SM_Loop_ZPolar
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any, Mapping, Sequence

import loop_filter_patch
import loop_filter_runtime


EXPECTED_LOOP_DIAGRAMS = 44
EXPECTED_RAW_EQUIVALENT_LOOP_DIAGRAMS = 86

# No Higgs exclusion appears here: all four definitions retain the full
# Higgs-plus-continuum loop-induced amplitude within the selected Z0/ZT route.
BASE_PROCESS = "g g > e+ e- mu+ mu- QED=4 QCD=2 [noborn = QCD]"
PROCESS_LINES = {
    channel.upper(): f"{BASE_PROCESS} {fragment}"
    for channel, fragment in loop_filter_runtime.PROCESS_FILTERS.items()
}

# The repository convention is Z1=mumu and Z2=ee.  Keep the dictionaries in
# flavor form so the validation is independent of final-leg ordering.
EXPECTED_ROUTES = {
    "LL": {
        "mumu": loop_filter_runtime.Z0_PDG,
        "ee": loop_filter_runtime.Z0_PDG,
    },
    "TT": {
        "mumu": loop_filter_runtime.ZT_PDG,
        "ee": loop_filter_runtime.ZT_PDG,
    },
    "TL": {
        "mumu": loop_filter_runtime.ZT_PDG,
        "ee": loop_filter_runtime.Z0_PDG,
    },
    "LT": {
        "mumu": loop_filter_runtime.Z0_PDG,
        "ee": loop_filter_runtime.ZT_PDG,
    },
}


class DiagramValidationError(RuntimeError):
    """Raised when an installed VPolar process differs from the audit."""


def _inside(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves inside ``root`` (including itself)."""

    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _validate_model_path(model: Path) -> Path:
    resolved = model.expanduser().resolve()
    if not resolved.is_dir() or not (resolved / "__init__.py").is_file():
        raise DiagramValidationError(
            "--model must be the SM_Loop_ZPolar UFO directory containing "
            f"__init__.py: {resolved}"
        )
    if resolved.name != "SM_Loop_ZPolar":
        raise DiagramValidationError(
            "unexpected UFO directory name; expected SM_Loop_ZPolar, got "
            f"{resolved.name!r}"
        )
    return resolved


def _load_master_command(mg5_root: Path) -> type[Any]:
    """Load ``MasterCmd`` from exactly the requested MadGraph tree."""

    root = mg5_root.expanduser().resolve()
    try:
        state = loop_filter_patch.inspect_installation(root)
    except (OSError, UnicodeError, loop_filter_patch.PatchError) as exc:
        raise DiagramValidationError(str(exc)) from exc
    if not state["patched"] or not state["runtime_matches"]:
        raise DiagramValidationError(
            "MadGraph is compatible but the OAP VPolar loop-filter patch is "
            "not installed"
        )

    root_text = str(root)
    if root_text in sys.path:
        sys.path.remove(root_text)
    sys.path.insert(0, root_text)

    madgraph = importlib.import_module("madgraph")
    module_file = getattr(madgraph, "__file__", None)
    if module_file is None or not _inside(Path(module_file), root):
        raise DiagramValidationError(
            "Python imported MadGraph from a different installation: "
            f"{module_file!r}"
        )
    master_interface = importlib.import_module(
        "madgraph.interface.master_interface"
    )
    return master_interface.MasterCmd


def _diagram_route(diagram: Any, structures: Any) -> dict[str, int] | None:
    """Resolve one diagram through the same audited helper as the MG hook."""

    referenced = tuple(
        loop_filter_runtime._referenced_structures(diagram, structures)
    )
    if not referenced:
        return None
    return loop_filter_runtime._decay_routes(referenced)


def validate_channel_amplitudes(
    channel: str,
    amplitudes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate one generated channel and return a compact report.

    This helper deliberately accepts MadGraph-like mappings so its count and
    routing contract can be tested without importing MadGraph.
    """

    normalized = str(channel).strip().upper()
    if normalized not in PROCESS_LINES:
        raise DiagramValidationError(f"unknown polarization channel {channel!r}")
    if len(amplitudes) != 1:
        raise DiagramValidationError(
            f"{normalized}: expected one amplitude, found {len(amplitudes)}"
        )

    amplitude = amplitudes[0]
    loop_diagrams = amplitude.get("loop_diagrams") or ()
    born_diagrams = amplitude.get("born_diagrams") or ()
    if amplitude.get("has_born") or born_diagrams:
        raise DiagramValidationError(
            f"{normalized}: process unexpectedly contains Born diagrams"
        )
    if len(loop_diagrams) != EXPECTED_LOOP_DIAGRAMS:
        raise DiagramValidationError(
            f"{normalized}: expected {EXPECTED_LOOP_DIAGRAMS} loop diagrams, "
            f"found {len(loop_diagrams)}"
        )

    multipliers: list[int] = []
    for index, diagram in enumerate(loop_diagrams):
        value = diagram.get("multiplier")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise DiagramValidationError(
                f"{normalized}: loop diagram #{index} has invalid flavor "
                f"multiplier {value!r}"
            )
        multipliers.append(value)
    raw_equivalent_count = sum(multipliers)
    if raw_equivalent_count != EXPECTED_RAW_EQUIVALENT_LOOP_DIAGRAMS:
        raise DiagramValidationError(
            f"{normalized}: expected {EXPECTED_RAW_EQUIVALENT_LOOP_DIAGRAMS} "
            "raw-equivalent loop diagrams after flavor multiplicities, "
            f"found {raw_equivalent_count}"
        )

    structures = amplitude.get("structure_repository")
    if structures is None:
        raise DiagramValidationError(
            f"{normalized}: amplitude has no FDStructure repository"
        )
    expected_route = EXPECTED_ROUTES[normalized]
    mismatches: list[tuple[int, dict[str, int] | None]] = []
    for index, diagram in enumerate(loop_diagrams):
        observed = _diagram_route(diagram, structures)
        if observed != expected_route:
            mismatches.append((index, observed))
    if mismatches:
        preview = ", ".join(
            f"#{index}={route!r}" for index, route in mismatches[:5]
        )
        suffix = " ..." if len(mismatches) > 5 else ""
        raise DiagramValidationError(
            f"{normalized}: {len(mismatches)} diagrams violate flavor route "
            f"{expected_route!r}: {preview}{suffix}"
        )

    return {
        "channel": normalized,
        "process": PROCESS_LINES[normalized],
        "loop_diagrams": len(loop_diagrams),
        "raw_equivalent_loop_diagrams": raw_equivalent_count,
        "route": dict(expected_route),
    }


def validate_installation(
    mg5_root: str | Path,
    model: str | Path,
) -> list[dict[str, Any]]:
    """Generate and validate all four amplitudes in one MadGraph session."""

    root = Path(mg5_root).expanduser().resolve()
    model_path = _validate_model_path(Path(model))
    master_command = _load_master_command(root)

    # MG5 3.4.2's MasterCmd creates/truncates ``./additional_command`` during
    # initialization. Keep every implicit MG scratch file away from the
    # caller's working directory.
    previous_directory = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="oap-vpolar-diagrams-") as scratch:
        reports: list[dict[str, Any]] = []
        try:
            os.chdir(scratch)
            command = master_command()
            # The pinned public UFO still contains Python-2 exception syntax.
            # Enable MG's deterministic Python-3 conversion in memory.
            command.options["auto_convert_model"] = True
            command.exec_cmd(
                f"import model {shlex.quote(str(model_path))}",
                errorhandling=False,
                printcmd=False,
                precmd=True,
                postcmd=True,
            )
            for channel, process in PROCESS_LINES.items():
                command.exec_cmd(
                    f"generate {process}",
                    errorhandling=False,
                    printcmd=False,
                    precmd=True,
                    postcmd=True,
                )
                reports.append(
                    validate_channel_amplitudes(channel, command._curr_amps)
                )
        finally:
            os.chdir(previous_directory)
    return reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mg5-root",
        required=True,
        type=Path,
        help="root of the installed and OAP-patched MG5_aMC 3.4.2 tree",
    )
    parser.add_argument(
        "--model",
        required=True,
        type=Path,
        help="downloaded SM_Loop_ZPolar UFO directory",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="write an atomic JSON validation report",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        reports = validate_installation(args.mg5_root, args.model)
    except Exception as exc:
        logging.shutdown()
        raise SystemExit(f"VPolar diagram validation failed: {exc}") from exc

    names = {
        loop_filter_runtime.Z0_PDG: "Z0",
        loop_filter_runtime.ZT_PDG: "ZT",
    }
    for report in reports:
        route = report["route"]
        print(
            f"{report['channel']}: {report['loop_diagrams']} representatives / "
            f"{report['raw_equivalent_loop_diagrams']} raw-equivalent loop "
            "diagrams; "
            f"Z1(mumu)={names[route['mumu']]}, "
            f"Z2(ee)={names[route['ee']]} [ok]"
        )
    if args.report is not None:
        report_path = args.report.expanduser().resolve()
        if report_path.exists():
            raise SystemExit(f"refusing to overwrite diagram report: {report_path}")
        payload = {
            "schema_version": 1,
            "final_state": "e+ e- mu+ mu-",
            "amplitude": "full Higgs plus continuum boxes plus interference",
            "z1_decay": "mu+ mu-",
            "z2_decay": "e+ e-",
            "diagram_counts": {
                f"vpolar_{report['channel']}": {
                    "representatives": report["loop_diagrams"],
                    "raw_equivalent": report["raw_equivalent_loop_diagrams"],
                }
                for report in reports
            },
            "channels": reports,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{report_path.name}.", dir=str(report_path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, report_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    print("VPolar diagram validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
