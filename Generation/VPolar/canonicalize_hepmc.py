#!/usr/bin/env python3
"""Canonicalize standalone Pythia HepMC2 marker names and event numbers.

Pythia 8 and the MG5/Pythia interface decorate detailed LHE weight names.
The exact decoration varies between compatible builds.  This tool identifies
the two project marker tokens, requires one unambiguous occurrence of each,
renames only those entries, and numbers events from the requested first event.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import re
import shlex
import tempfile


MARKER_ID_WEIGHT = "AUX_OAP_EVENT_ID"
MARKER_UNIT_WEIGHT = "AUX_OAP_EVENT_UNIT"
MARKERS = (MARKER_ID_WEIGHT, MARKER_UNIT_WEIGHT)


class CanonicalizationError(RuntimeError):
    """Raised when a HepMC file cannot satisfy the matching contract."""


def _parse_names(line: str, line_number: int) -> list[str]:
    try:
        fields = shlex.split(line, comments=False, posix=True)
    except ValueError as error:
        raise CanonicalizationError(
            f"invalid HepMC N record at line {line_number}"
        ) from error
    if len(fields) < 2 or fields[0] != "N":
        raise CanonicalizationError(f"invalid HepMC N record at line {line_number}")
    try:
        declared = int(fields[1])
    except ValueError as error:
        raise CanonicalizationError(
            f"invalid HepMC N record at line {line_number}"
        ) from error
    names = fields[2:]
    if declared < 0 or declared != len(names):
        raise CanonicalizationError(
            f"HepMC N record at line {line_number} declares {declared} names "
            f"but contains {len(names)}"
        )
    if len(set(names)) != len(names):
        raise CanonicalizationError(
            f"duplicate HepMC weight name at line {line_number}"
        )
    return names


def _rewrite_names(names: list[str], line_number: int) -> list[str]:
    rewritten = list(names)
    selected_indices: set[int] = set()
    for marker in MARKERS:
        matches = [index for index, name in enumerate(names) if marker in name]
        if len(matches) != 1:
            raise CanonicalizationError(
                f"HepMC N record at line {line_number} has {len(matches)} "
                f"names containing marker {marker}; expected exactly one"
            )
        index = matches[0]
        if index in selected_indices:
            raise CanonicalizationError(
                f"one HepMC name matched both marker tokens at line {line_number}"
            )
        selected_indices.add(index)
        rewritten[index] = marker
    if len(set(rewritten)) != len(rewritten):
        raise CanonicalizationError(
            f"canonical marker names collide at line {line_number}"
        )
    return rewritten


def _rewrite_event_line(
    line: str, event_number: int, line_number: int
) -> tuple[str, int]:
    fields = line.split()
    if len(fields) < 13 or fields[0] != "E":
        raise CanonicalizationError(f"invalid HepMC E record at line {line_number}")
    try:
        int(fields[1])
        random_count = int(fields[11])
        weight_count_index = 12 + random_count
        weight_count = int(fields[weight_count_index])
    except (IndexError, ValueError) as error:
        raise CanonicalizationError(
            f"invalid HepMC E record at line {line_number}"
        ) from error
    if random_count < 0 or weight_count < 0:
        raise CanonicalizationError(
            f"negative HepMC E-record count at line {line_number}"
        )
    if len(fields) != weight_count_index + 1 + weight_count:
        raise CanonicalizationError(
            f"HepMC E record at line {line_number} has inconsistent weight count"
        )
    fields[1] = str(event_number)
    return " ".join(fields) + "\n", weight_count


def _name_line(names: list[str]) -> str:
    return "N {} {}\n".format(
        len(names), " ".join(json.dumps(name) for name in names)
    )


def canonicalize_hepmc(
    input_path: Path,
    output_path: Path,
    *,
    first_event: int,
    expected_events: int,
) -> int:
    """Write canonical HepMC2 atomically and return its event count."""

    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if not input_path.is_file():
        raise CanonicalizationError(f"HepMC input does not exist: {input_path}")
    if input_path == output_path:
        raise CanonicalizationError("input and output HepMC paths must differ")
    if output_path.exists():
        raise CanonicalizationError(f"refusing to overwrite output: {output_path}")
    if first_event < 1 or expected_events < 1:
        raise CanonicalizationError("first-event and expected-events must be positive")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=str(output_path.parent)
    )
    temporary = Path(temporary_name)
    saw_version = saw_start = saw_footer = False
    event_count = 0
    pending_weight_count: int | None = None
    canonical_names: list[str] | None = None
    opener = gzip.open if input_path.suffix == ".gz" else open
    try:
        with opener(input_path, "rt", encoding="utf-8", errors="strict") as source:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                descriptor = -1
                for line_number, line in enumerate(source, start=1):
                    if line.startswith("HepMC::Version"):
                        saw_version = True
                    elif line.startswith("HepMC::IO_GenEvent-START_EVENT_LISTING"):
                        saw_start = True
                    elif line.startswith("HepMC::IO_GenEvent-END_EVENT_LISTING"):
                        if pending_weight_count is not None:
                            raise CanonicalizationError(
                                "last HepMC event has no N weight-name record"
                            )
                        saw_footer = True
                    elif re.match(r"^E\s", line):
                        if pending_weight_count is not None:
                            raise CanonicalizationError(
                                f"HepMC event before line {line_number} has no N record"
                            )
                        event_number = first_event + event_count
                        line, pending_weight_count = _rewrite_event_line(
                            line, event_number, line_number
                        )
                        event_count += 1
                    elif re.match(r"^N\s", line):
                        if pending_weight_count is None:
                            raise CanonicalizationError(
                                f"HepMC N record before E record at line {line_number}"
                            )
                        names = _rewrite_names(_parse_names(line, line_number), line_number)
                        if len(names) != pending_weight_count:
                            raise CanonicalizationError(
                                f"HepMC E/N weight count mismatch at line {line_number}"
                            )
                        if canonical_names is None:
                            canonical_names = names
                        elif names != canonical_names:
                            raise CanonicalizationError(
                                f"HepMC weight names change at line {line_number}"
                            )
                        line = _name_line(names)
                        pending_weight_count = None
                    destination.write(line)
                destination.flush()
                os.fsync(destination.fileno())
        if not (saw_version and saw_start and saw_footer):
            raise CanonicalizationError("input is not a complete HepMC2 ASCII listing")
        if canonical_names is None or event_count == 0:
            raise CanonicalizationError("input contains no complete HepMC events")
        if event_count != expected_events:
            raise CanonicalizationError(
                f"HepMC has {event_count} events; expected {expected_events}"
            )
        os.replace(temporary, output_path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return event_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--first-event", required=True, type=int)
    parser.add_argument("--expected-events", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    count = canonicalize_hepmc(
        args.input,
        args.output,
        first_event=args.first_event,
        expected_events=args.expected_events,
    )
    print(f"Canonicalized {count} HepMC2 events")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanonicalizationError as error:
        raise SystemExit(f"HepMC canonicalization error: {error}") from error
