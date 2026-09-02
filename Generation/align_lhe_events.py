#!/usr/bin/env python3
"""Match every showered HepMC event to its exact POWHEG LHE hard event.

The job options add two named LHE3 weights before Pythia runs.  Pythia8_i
multiplies both by the same shower factor, so their ratio recovers a small,
integral source-event ID even when events are skipped during showering.  This
tool decodes those IDs from HepMC2, selects the corresponding tagged LHE
events, and records a hash-bound alignment contract for downstream analysis.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import struct
import tarfile
import tempfile
from typing import Iterator, TextIO


CONTRACT = "named-weight-id-v1"
NORMALIZATION_CONTRACT = "idwtup-minus4-sample-mean-v1"
CROSS_SECTION_METHOD = (
    "mean nominal LHE weight; rejected events assigned zero for filtered estimate"
)
MARKER_ID_WEIGHT = "AUX_OAP_EVENT_ID"
MARKER_UNIT_WEIGHT = "AUX_OAP_EVENT_UNIT"
MAX_SOURCE_EVENT_ID = 1_000_000
HEPMC_RELATIVE_TOLERANCE = 5.0e-8
HEPMC_ABSOLUTE_TOLERANCE = 1.0e-7
EVENT_OPEN_RE = re.compile(r"<event(?:\s|>)")
FILTER_PATTERNS = {
    "GeneratorFilters include": re.compile(r"GeneratorFilters/"),
    "filter sequence": re.compile(r"\bfiltSeq\b|\bfilterSeq\b"),
    "declared filter efficiency": re.compile(r"evgenConfig\.filterEfficiency"),
}
LOG_PATTERNS = {
    "pythia_retry": "Event generation failed - re-trying.",
    "pythia_rejection": "Rejecting event.",
    "pythia_failure_limit": "Exceeded the max number of consecutive event failures.",
}


class AlignmentError(RuntimeError):
    """Raised when the named-weight alignment contract cannot be established."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_id_sequence_sha256(source_ids: list[int]) -> str:
    """Hash IDs as consecutive unsigned 64-bit big-endian integers."""

    digest = hashlib.sha256()
    for source_id in source_ids:
        digest.update(struct.pack(">Q", source_id))
    return digest.hexdigest()


def _active_job_option_text(path: Path) -> str:
    active_lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        # Project cards contain no '#' characters inside active string literals.
        active_lines.append(line.split("#", 1)[0])
    return "\n".join(active_lines)


def assert_no_post_shower_filter(path: Path) -> None:
    code = _active_job_option_text(path)
    violations = [label for label, pattern in FILTER_PATTERNS.items() if pattern.search(code)]
    if violations:
        raise AlignmentError(
            f"{path} violates {CONTRACT}: " + ", ".join(sorted(violations))
        )


def log_observations(path: Path) -> dict[str, int]:
    counts = {name: 0 for name in LOG_PATTERNS}
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            for name, marker in LOG_PATTERNS.items():
                if marker in line:
                    counts[name] += 1
    if counts["pythia_failure_limit"]:
        raise AlignmentError("transform log reports an exhausted Pythia failure allowance")
    return counts


def _decode_source_id(numerator: float, denominator: float) -> int:
    if not (math.isfinite(numerator) and math.isfinite(denominator)):
        raise AlignmentError("non-finite HepMC source-ID marker weight")
    if denominator == 0.0:
        raise AlignmentError("zero HepMC source-ID unit weight")
    ratio = numerator / denominator
    if not math.isfinite(ratio):
        raise AlignmentError("non-finite HepMC source-ID ratio")
    nearest = round(ratio)
    tolerance = max(
        HEPMC_ABSOLUTE_TOLERANCE,
        abs(ratio) * HEPMC_RELATIVE_TOLERANCE,
    )
    if tolerance >= 0.25:
        raise AlignmentError("source ID is too large for unambiguous HepMC2 precision")
    if abs(ratio - nearest) > tolerance:
        raise AlignmentError(
            f"HepMC marker ratio {ratio:.12g} is not integral within {tolerance:.3g}"
        )
    if not 1 <= nearest <= MAX_SOURCE_EVENT_ID:
        raise AlignmentError(
            f"decoded source ID {nearest} is outside 1..{MAX_SOURCE_EVENT_ID}"
        )
    return nearest


def _parse_hepmc_event_line(line: str, line_number: int) -> tuple[int, list[float]]:
    fields = line.split()
    if len(fields) < 13 or fields[0] != "E":
        raise AlignmentError(f"invalid HepMC E record at line {line_number}")
    try:
        event_number = int(fields[1])
        random_count = int(fields[11])
    except ValueError as error:
        raise AlignmentError(f"invalid HepMC E record at line {line_number}") from error
    if random_count < 0:
        raise AlignmentError(f"negative HepMC random-state count at line {line_number}")
    weight_count_index = 12 + random_count
    if weight_count_index >= len(fields):
        raise AlignmentError(f"truncated HepMC E record at line {line_number}")
    try:
        weight_count = int(fields[weight_count_index])
        weights = [float(value) for value in fields[weight_count_index + 1 :]]
    except ValueError as error:
        raise AlignmentError(f"invalid HepMC weights at line {line_number}") from error
    if weight_count < 0 or len(weights) != weight_count:
        raise AlignmentError(
            f"HepMC E record at line {line_number} declares {weight_count} weights "
            f"but contains {len(weights)}"
        )
    if not all(math.isfinite(weight) for weight in weights):
        raise AlignmentError(f"non-finite HepMC weight at line {line_number}")
    return event_number, weights


def _parse_hepmc_name_line(line: str, line_number: int) -> list[str]:
    try:
        fields = shlex.split(line, comments=False, posix=True)
    except ValueError as error:
        raise AlignmentError(f"invalid HepMC N record at line {line_number}") from error
    if len(fields) < 2 or fields[0] != "N":
        raise AlignmentError(f"invalid HepMC N record at line {line_number}")
    try:
        name_count = int(fields[1])
    except ValueError as error:
        raise AlignmentError(f"invalid HepMC N record at line {line_number}") from error
    names = fields[2:]
    if name_count < 0 or len(names) != name_count:
        raise AlignmentError(
            f"HepMC N record at line {line_number} declares {name_count} names "
            f"but contains {len(names)}"
        )
    if len(set(names)) != len(names):
        raise AlignmentError(f"duplicate HepMC weight name at line {line_number}")
    return names


def read_hepmc_source_ids(
    path: Path,
) -> tuple[list[int], list[int], list[str], int, int]:
    """Return event numbers, decoded source IDs, names, and marker indices."""

    opener = gzip.open if path.suffix == ".gz" else open
    saw_version = saw_start = saw_footer = False
    current: tuple[int, list[float], int] | None = None
    canonical_names: list[str] | None = None
    event_numbers: list[int] = []
    seen_event_numbers: set[int] = set()
    source_ids: list[int] = []
    id_index = unit_index = -1

    def finish_event(names: list[str], line_number: int) -> None:
        nonlocal canonical_names, id_index, unit_index, current
        if current is None:
            raise AlignmentError(f"HepMC N record before E record at line {line_number}")
        event_number, weights, event_line = current
        if len(names) != len(weights):
            raise AlignmentError(
                f"HepMC E/N weight count mismatch at lines {event_line}/{line_number}"
            )
        if canonical_names is None:
            canonical_names = names
            for required in (MARKER_ID_WEIGHT, MARKER_UNIT_WEIGHT):
                if required not in names:
                    raise AlignmentError(f"HepMC is missing named marker weight {required}")
            id_index = names.index(MARKER_ID_WEIGHT)
            unit_index = names.index(MARKER_UNIT_WEIGHT)
        elif names != canonical_names:
            raise AlignmentError(f"HepMC weight names change at line {line_number}")
        source_id = _decode_source_id(weights[id_index], weights[unit_index])
        if source_ids and source_id <= source_ids[-1]:
            raise AlignmentError("decoded HepMC source IDs are not strictly increasing")
        if event_number in seen_event_numbers:
            raise AlignmentError(f"duplicate HepMC event number {event_number}")
        seen_event_numbers.add(event_number)
        event_numbers.append(event_number)
        source_ids.append(source_id)
        current = None

    with opener(path, "rt", encoding="utf-8", errors="strict") as stream:
        for line_number, line in enumerate(stream, start=1):
            if line.startswith("HepMC::Version"):
                saw_version = True
            elif line.startswith("HepMC::IO_GenEvent-START_EVENT_LISTING"):
                saw_start = True
            elif line.startswith("HepMC::IO_GenEvent-END_EVENT_LISTING"):
                if current is not None:
                    raise AlignmentError("last HepMC event has no N weight-name record")
                saw_footer = True
            elif re.match(r"^E\s", line):
                if current is not None:
                    raise AlignmentError(
                        f"HepMC event at line {current[2]} has no N weight-name record"
                    )
                event_number, weights = _parse_hepmc_event_line(line, line_number)
                current = (event_number, weights, line_number)
            elif re.match(r"^N\s", line):
                finish_event(_parse_hepmc_name_line(line, line_number), line_number)

    if current is not None:
        raise AlignmentError("last HepMC event has no N weight-name record")
    if not (saw_version and saw_start and saw_footer):
        raise AlignmentError(f"{path} is not a complete HepMC2 ASCII event listing")
    if canonical_names is None or not source_ids:
        raise AlignmentError(f"{path} contains no HepMC events")
    return event_numbers, source_ids, canonical_names, id_index, unit_index


def _lhe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    suffixes = (".events", ".lhe", ".events.gz", ".lhe.gz")
    return [
        member
        for member in archive.getmembers()
        if member.isfile() and member.name.lower().endswith(suffixes)
    ]


@contextlib.contextmanager
def open_lhe_member(archive_path: Path) -> Iterator[tuple[str, TextIO]]:
    with tarfile.open(archive_path, "r:*") as archive:
        candidates = _lhe_members(archive)
        if len(candidates) != 1:
            names = ", ".join(member.name for member in candidates) or "none"
            raise AlignmentError(
                f"{archive_path} must contain exactly one LHE event file; found {names}"
            )
        member = candidates[0]
        extracted = archive.extractfile(member)
        if extracted is None:
            raise AlignmentError(f"could not read {member.name} from {archive_path}")
        buffered = io.BufferedReader(extracted)
        binary: io.BufferedIOBase
        if buffered.peek(2)[:2] == b"\x1f\x8b":
            binary = gzip.GzipFile(fileobj=buffered, mode="rb")
        else:
            binary = buffered
        text = io.TextIOWrapper(binary, encoding="utf-8", errors="strict")
        try:
            yield member.name, text
        finally:
            text.close()


@contextlib.contextmanager
def open_output(path: Path) -> Iterator[TextIO]:
    with path.open("wb") as raw:
        if path.suffix == ".gz":
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8") as text:
                    yield text
        else:
            with io.TextIOWrapper(raw, encoding="utf-8") as text:
                yield text


def _event_marker(event: str, weight_name: str) -> float:
    pattern = re.compile(
        r"<wgt\b[^>]*\bid\s*=\s*['\"]"
        + re.escape(weight_name)
        + r"['\"][^>]*>\s*([^<]+?)\s*</wgt>",
        re.IGNORECASE | re.DOTALL,
    )
    matches = pattern.findall(event)
    if len(matches) != 1:
        raise AlignmentError(
            f"LHE event must contain exactly one {weight_name} weight; found {len(matches)}"
        )
    try:
        value = float(matches[0])
    except ValueError as error:
        raise AlignmentError(f"invalid LHE marker weight {weight_name}") from error
    if not math.isfinite(value):
        raise AlignmentError(f"non-finite LHE marker weight {weight_name}")
    return value


def _source_comment_id(event: str) -> int:
    pattern = re.compile(
        r"(?m)^\s*#\s*" + re.escape(MARKER_ID_WEIGHT) + r"\s+([0-9]+)\s*$"
    )
    matches = pattern.findall(event)
    if len(matches) != 1:
        raise AlignmentError(
            f"LHE event must contain exactly one {MARKER_ID_WEIGHT} source comment"
        )
    source_id = int(matches[0])
    if not 1 <= source_id <= MAX_SOURCE_EVENT_ID:
        raise AlignmentError(f"LHE source ID {source_id} is outside the supported range")
    return source_id


def _validate_lhe_event_markers(event: str) -> int:
    source_id = _source_comment_id(event)
    id_value = _event_marker(event, MARKER_ID_WEIGHT)
    unit_value = _event_marker(event, MARKER_UNIT_WEIGHT)
    if unit_value != 1.0 or id_value != float(source_id):
        raise AlignmentError("LHE source comment and technical weights disagree")
    return source_id


def select_lhe_events(
    source: TextIO, requested_ids: set[int]
) -> tuple[str, dict[int, str], int]:
    """Read a tagged LHE document and retain only requested source IDs."""

    preamble: list[str] = []
    event_buffer = ""
    selected: dict[int, str] = {}
    total_events = 0
    previous_id = 0
    saw_event = False
    saw_root_close = False

    for line in source:
        if not event_buffer:
            opening = EVENT_OPEN_RE.search(line)
            if opening is None:
                if not saw_event:
                    if "</LesHouchesEvents>" in line:
                        raise AlignmentError("LHE document closes before its first event")
                    preamble.append(line)
                elif "</LesHouchesEvents>" in line:
                    saw_root_close = True
                continue
            if not saw_event:
                preamble.append(line[: opening.start()])
                saw_event = True
            event_buffer = line[opening.start() :]
        else:
            event_buffer += line

        if "</event>" not in event_buffer:
            continue
        closing = event_buffer.index("</event>") + len("</event>")
        trailing = event_buffer[closing:]
        if trailing.strip() and "</LesHouchesEvents>" not in trailing:
            raise AlignmentError("unexpected content after an LHE event closing tag")
        if "</LesHouchesEvents>" in trailing:
            saw_root_close = True
        event = event_buffer[:closing] + "\n"
        source_id = _validate_lhe_event_markers(event)
        if source_id <= previous_id:
            raise AlignmentError("LHE source IDs are not strictly increasing")
        previous_id = source_id
        total_events += 1
        if source_id in requested_ids:
            selected[source_id] = event
        event_buffer = ""

    if event_buffer:
        raise AlignmentError("unterminated <event> element in LHE input")
    if not saw_event:
        raise AlignmentError("no <event> elements found in LHE input")
    if not saw_root_close:
        raise AlignmentError("missing </LesHouchesEvents> in LHE input")
    preamble_text = "".join(preamble)
    for weight_name in (MARKER_ID_WEIGHT, MARKER_UNIT_WEIGHT):
        if weight_name not in preamble_text:
            raise AlignmentError(f"LHE header is missing marker definition {weight_name}")
    return preamble_text, selected, total_events


def _required_int(metadata: dict, key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AlignmentError(f"LHE contract metadata field {key} must be an integer")
    return value


def _required_number(metadata: dict, key: str) -> float:
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AlignmentError(f"LHE contract metadata field {key} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise AlignmentError(f"LHE contract metadata field {key} must be finite")
    return number


def _expected_mc_error(sumw: float, sumw2: float, count: int) -> float | None:
    if count < 2:
        return None
    variance_numerator = sumw2 - sumw * sumw / count
    scale = max(sumw2, sumw * sumw / count, 1.0)
    if variance_numerator < -1.0e-12 * scale:
        raise AlignmentError("LHE contract nominal-weight moments are inconsistent")
    return math.sqrt(max(variance_numerator, 0.0) / (count * (count - 1)))


def validate_lhe_contract_metadata(
    path: Path,
    *,
    process: str,
    expected_events: int,
    expected_m4l_min: float,
    expected_m4l_max: float,
) -> dict:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AlignmentError(f"cannot read LHE contract metadata: {path}") from error
    if not isinstance(metadata, dict):
        raise AlignmentError("LHE contract metadata must be a JSON object")
    expected = {
        "schema_version": 2,
        "contract": CONTRACT,
        "normalization_contract": NORMALIZATION_CONTRACT,
        "nominal_weight_units": "pb",
        "lhe_weighting_strategy": -4,
        "cross_section_method": CROSS_SECTION_METHOD,
        "process": process,
        "marker_id_weight": MARKER_ID_WEIGHT,
        "marker_unit_weight": MARKER_UNIT_WEIGHT,
        "requested_hepmc_events": expected_events,
    }
    mismatches = [
        f"{key}={metadata.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if metadata.get(key) != value
    ]
    for key, expected_value in (
        ("m4l_min_gev", expected_m4l_min),
        ("m4l_max_gev", expected_m4l_max),
    ):
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isclose(
            float(value), expected_value, rel_tol=0.0, abs_tol=1.0e-12
        ):
            mismatches.append(f"{key}={value!r} (expected {expected_value!r})")
    if mismatches:
        raise AlignmentError("LHE contract metadata mismatch: " + "; ".join(mismatches))

    generated = _required_int(metadata, "generated_lhe_events")
    accepted = _required_int(metadata, "accepted_lhe_events")
    rejected_below = _required_int(metadata, "rejected_below_m4l")
    rejected_above = _required_int(metadata, "rejected_above_m4l")
    if min(generated, accepted, rejected_below, rejected_above) < 0:
        raise AlignmentError("LHE contract metadata contains a negative event count")
    if accepted < expected_events:
        raise AlignmentError("LHE contract retained fewer events than requested")
    if accepted + rejected_below + rejected_above != generated:
        raise AlignmentError("LHE contract metadata event counts are inconsistent")

    lhe_init = metadata.get("lhe_init")
    if not isinstance(lhe_init, dict) or lhe_init.get("idwtup") != -4:
        raise AlignmentError("LHE contract lhe_init must declare IDWTUP=-4")

    sumw_generated = _required_number(metadata, "sumw_generated")
    sumw_accepted = _required_number(metadata, "sumw_accepted")
    sumw2_generated = _required_number(metadata, "sumw2_generated")
    sumw2_accepted = _required_number(metadata, "sumw2_accepted")
    sumabsw_generated = _required_number(metadata, "sumabsw_generated")
    sumabsw_accepted = _required_number(metadata, "sumabsw_accepted")
    count_efficiency = _required_number(metadata, "count_filter_efficiency")
    inclusive_cross_section = _required_number(
        metadata, "inclusive_cross_section_pb"
    )
    filtered_cross_section = _required_number(metadata, "filtered_cross_section_pb")
    if min(sumw2_generated, sumw2_accepted, sumabsw_generated, sumabsw_accepted) < 0.0:
        raise AlignmentError("LHE contract weight moments must be non-negative")
    if sumw2_accepted > sumw2_generated + 1.0e-12 * max(sumw2_generated, 1.0):
        raise AlignmentError("LHE contract accepted sumw2 exceeds generated sumw2")
    if sumabsw_accepted > sumabsw_generated + 1.0e-12 * max(
        sumabsw_generated, 1.0
    ):
        raise AlignmentError("LHE contract accepted sumabsw exceeds generated sumabsw")

    derived = {
        "count_filter_efficiency": accepted / generated,
        "inclusive_cross_section_pb": sumw_generated / generated,
        "filtered_cross_section_pb": sumw_accepted / generated,
    }
    observed = {
        "count_filter_efficiency": count_efficiency,
        "inclusive_cross_section_pb": inclusive_cross_section,
        "filtered_cross_section_pb": filtered_cross_section,
    }
    for key, expected_value in derived.items():
        if not math.isclose(
            observed[key], expected_value, rel_tol=1.0e-12, abs_tol=1.0e-15
        ):
            raise AlignmentError(
                f"LHE contract metadata field {key} disagrees with weight sums/counts"
            )

    for field, numerator, denominator in (
        ("signed_filter_efficiency", sumw_accepted, sumw_generated),
        ("absolute_filter_efficiency", sumabsw_accepted, sumabsw_generated),
    ):
        if field not in metadata:
            raise AlignmentError(f"LHE contract metadata is missing field {field}")
        observed_efficiency = metadata.get(field)
        if denominator == 0.0:
            if observed_efficiency is not None:
                raise AlignmentError(
                    f"LHE contract metadata field {field} must be null for "
                    "zero denominator"
                )
        else:
            value = _required_number(metadata, field)
            if not math.isclose(
                value,
                numerator / denominator,
                rel_tol=1.0e-12,
                abs_tol=1.0e-15,
            ):
                raise AlignmentError(
                    f"LHE contract metadata field {field} disagrees with weight sums"
                )

    for field, sumw, sumw2 in (
        ("inclusive_cross_section_mc_error_pb", sumw_generated, sumw2_generated),
        ("filtered_cross_section_mc_error_pb", sumw_accepted, sumw2_accepted),
    ):
        if field not in metadata:
            raise AlignmentError(f"LHE contract metadata is missing field {field}")
        expected_error = _expected_mc_error(sumw, sumw2, generated)
        observed_error = metadata.get(field)
        if expected_error is None:
            if observed_error is not None:
                raise AlignmentError(
                    f"LHE contract metadata field {field} must be null for N<2"
                )
        else:
            value = _required_number(metadata, field)
            if value < 0.0 or not math.isclose(
                value, expected_error, rel_tol=1.0e-12, abs_tol=1.0e-15
            ):
                raise AlignmentError(
                    f"LHE contract metadata field {field} disagrees with weight moments"
                )
    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lhe-archive", required=True, type=Path)
    parser.add_argument("--lhe-contract-metadata", required=True, type=Path)
    parser.add_argument("--hepmc", required=True, type=Path)
    parser.add_argument("--job-option", required=True, type=Path)
    parser.add_argument("--transform-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--expected-events", required=True, type=int)
    parser.add_argument("--expected-m4l-min", required=True, type=float)
    parser.add_argument("--expected-m4l-max", required=True, type=float)
    parser.add_argument("--process", required=True, choices=("gg4l", "qqZZ"))
    parser.add_argument("--run-number", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--first-event", required=True, type=int)
    parser.add_argument("--release", required=True)
    parser.add_argument("--contract", required=True, choices=(CONTRACT,))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.expected_events <= 100_000:
        raise AlignmentError("--expected-events must be between 1 and 100000")
    for path in (
        args.lhe_archive,
        args.lhe_contract_metadata,
        args.hepmc,
        args.job_option,
        args.transform_log,
    ):
        if not path.is_file():
            raise AlignmentError(f"required input does not exist: {path}")

    assert_no_post_shower_filter(args.job_option)
    observations = log_observations(args.transform_log)
    contract_metadata = validate_lhe_contract_metadata(
        args.lhe_contract_metadata,
        process=args.process,
        expected_events=args.expected_events,
        expected_m4l_min=args.expected_m4l_min,
        expected_m4l_max=args.expected_m4l_max,
    )
    event_numbers, source_ids, weight_names, id_index, unit_index = (
        read_hepmc_source_ids(args.hepmc)
    )
    if len(source_ids) != args.expected_events:
        raise AlignmentError(
            f"HepMC has {len(source_ids)} events; expected {args.expected_events}"
        )

    member_name = ""
    with open_lhe_member(args.lhe_archive) as (member_name, source):
        preamble, selected, phase_space_lhe_events = select_lhe_events(
            source, set(source_ids)
        )
    accepted_lhe_events = _required_int(contract_metadata, "accepted_lhe_events")
    if phase_space_lhe_events != accepted_lhe_events:
        raise AlignmentError(
            f"LHE archive has {phase_space_lhe_events} events but its contract "
            f"metadata declares {accepted_lhe_events}"
        )
    missing = [source_id for source_id in source_ids if source_id not in selected]
    if missing:
        preview = ", ".join(str(source_id) for source_id in missing[:8])
        raise AlignmentError(f"HepMC source IDs are absent from LHE archive: {preview}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.output.name}.", dir=args.output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if args.output.suffix == ".gz":
            compressed_temporary = temporary.with_suffix(temporary.suffix + ".gz")
            temporary.rename(compressed_temporary)
            temporary = compressed_temporary
        with open_output(temporary) as destination:
            destination.write(preamble)
            for source_id in source_ids:
                destination.write(selected[source_id])
            destination.write("</LesHouchesEvents>\n")
        os.replace(temporary, args.output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    filter_fields = (
        "normalization_contract",
        "nominal_weight_units",
        "lhe_weighting_strategy",
        "cross_section_method",
        "lhe_init",
        "m4l_min_gev",
        "m4l_max_gev",
        "generated_lhe_events",
        "accepted_lhe_events",
        "rejected_below_m4l",
        "rejected_above_m4l",
        "sumw_generated",
        "sumw_accepted",
        "sumw2_generated",
        "sumw2_accepted",
        "sumabsw_generated",
        "sumabsw_accepted",
        "count_filter_efficiency",
        "signed_filter_efficiency",
        "absolute_filter_efficiency",
        "inclusive_cross_section_pb",
        "inclusive_cross_section_mc_error_pb",
        "filtered_cross_section_pb",
        "filtered_cross_section_mc_error_pb",
    )
    metadata = {
        "schema_version": 2,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "contract": CONTRACT,
        "process": args.process,
        "run_number": args.run_number,
        "random_seed": args.seed,
        "first_event": args.first_event,
        "athgeneration_release": args.release,
        "marker": {
            "id_weight_name": MARKER_ID_WEIGHT,
            "id_weight_index": id_index,
            "unit_weight_name": MARKER_UNIT_WEIGHT,
            "unit_weight_index": unit_index,
            "recovery": "ratio",
            "source_id_sequence_encoding": "concatenated uint64 big-endian, no delimiter",
            "source_id_sequence_sha256": source_id_sequence_sha256(source_ids),
        },
        "hepmc_weight_names": weight_names,
        "hepmc_precision_contract": {
            "relative_ratio_tolerance": HEPMC_RELATIVE_TOLERANCE,
            "absolute_ratio_tolerance": HEPMC_ABSOLUTE_TOLERANCE,
            "maximum_source_event_id": MAX_SOURCE_EVENT_ID,
        },
        "counts": {
            "requested_hepmc_events": args.expected_events,
            "hepmc_events": len(source_ids),
            "generated_lhe_events": contract_metadata["generated_lhe_events"],
            "phase_space_lhe_events": phase_space_lhe_events,
            "matched_lhe_events": len(source_ids),
        },
        "phase_space_filter": {
            key: contract_metadata.get(key) for key in filter_fields
        },
        "contract_conditions": {
            "post_shower_generator_filter": False,
            "mapping": "HepMC source-ID ratio selects the identically tagged LHE event",
            "hepmc_source_ids_strictly_increasing": True,
        },
        "transform_log_observations": observations,
        "files": {
            "lhe_archive": {
                "path": args.lhe_archive.name,
                "path_scope": "generation_run_directory",
                "member": member_name,
                "sha256": sha256(args.lhe_archive),
            },
            "lhe_contract_metadata": {
                "path": args.lhe_contract_metadata.name,
                "path_scope": "generation_run_directory",
                "sha256": sha256(args.lhe_contract_metadata),
            },
            "hepmc": {
                "path": args.hepmc.name,
                "path_scope": "generation_run_directory",
                "sha256": sha256(args.hepmc),
            },
            "matched_lhe": {
                "path": args.output.name,
                "path_scope": "generation_run_directory",
                "sha256": sha256(args.output),
            },
            "job_option": {
                "path": f"jobOptions/{args.run_number}/{args.job_option.name}",
                "path_scope": "Generation_directory",
                "sha256": sha256(args.job_option),
            },
            "transform_log": {
                "path": args.transform_log.name,
                "path_scope": "generation_run_directory",
                "sha256": sha256(args.transform_log),
            },
        },
    }
    args.metadata.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"Matched {len(source_ids)} HepMC events to exact source IDs in "
        f"{phase_space_lhe_events} phase-space-selected LHE events"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AlignmentError as error:
        raise SystemExit(f"alignment error: {error}") from error
