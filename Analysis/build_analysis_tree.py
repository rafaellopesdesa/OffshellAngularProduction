#!/usr/bin/env python3
"""Build one compact, explicitly matched event tree from LHE and Delphes.

Generation injects a zero-safe pair of named auxiliary LHE weights.  Their
ratio is the stable source-event identifier; the alignment stage records their
HepMC indices and writes the matched LHE stream in HepMC order.  Delphes keeps
the HepMC weight vector but not its names, so this reducer resolves the recorded
indices, decodes both LHE and Delphes identifiers, and requires an exact
integer match for every output row.  Counts, event numbers, hashes, and the
complete source-ID sequence are independent cross-checks rather than the join
key.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import awkward as ak
import numpy as np
import pylhe
import uproot
import vector

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"
for import_root in (SOURCE_ROOT, REPO_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from offshell_production import (
    LEPTON_KEYS,
    LHEStatus,
    build_level_record,
    empty_level_record,
    empty_reco_selection_result,
    evaluate_reco_selection,
    extract_event_particles,
)

TREE_NAME = "Events"
RUN_TREE_NAME = "Runs"
SCHEMA_VERSION = 2
UID_SCHEMA_TAG = b"OffshellAngularProduction.Events.v2\0"
SAMPLE_CODES = {"gg4l": 0, "qqZZ": 1}
MARKER_ID_WEIGHT_NAME = "AUX_OAP_EVENT_ID"
MARKER_UNIT_WEIGHT_NAME = "AUX_OAP_EVENT_UNIT"
ALIGNMENT_CONTRACT = "named-weight-id-v1"
NORMALIZATION_CONTRACT = "idwtup-minus4-sample-mean-v1"
CROSS_SECTION_METHOD = (
    "mean nominal LHE weight; rejected events assigned zero for filtered estimate"
)
MAX_SOURCE_EVENT_ID = 1_000_000
MAX_EVENTS_PER_JOB = 100_000
HEPMC_RELATIVE_TOLERANCE = 5.0e-8
HEPMC_ABSOLUTE_TOLERANCE = 1.0e-7

ANALYSIS_CODE_FILES = (
    "Analysis/build_analysis_tree.py",
    "src/offshell_production/__init__.py",
    "src/offshell_production/kinematics.py",
    "src/offshell_production/lhe.py",
    "src/offshell_production/selection.py",
    "pyproject.toml",
    "uv.lock",
)

LEPTON_PDGS = {
    11: "electron_minus",
    -11: "electron_plus",
    13: "muon_minus",
    -13: "muon_plus",
}

INPUT_BRANCHES = (
    "Event.Number",
    "Event.Weight",
    "Event.CrossSection",
    "Event.CrossSectionError",
    "Weight.Weight",
    "DressedElectron.PID",
    "DressedElectron.E",
    "DressedElectron.Px",
    "DressedElectron.Py",
    "DressedElectron.Pz",
    "DressedMuon.PID",
    "DressedMuon.E",
    "DressedMuon.Px",
    "DressedMuon.Py",
    "DressedMuon.Pz",
    "RecoElectron.PT",
    "RecoElectron.Eta",
    "RecoElectron.Phi",
    "RecoElectron.Charge",
    "RecoMuon.PT",
    "RecoMuon.Eta",
    "RecoMuon.Phi",
    "RecoMuon.Charge",
)

COUNT_BRANCHES = tuple(
    f"{level}_n_{flavor}_{charge}"
    for level in ("lhe", "dressed", "reco")
    for flavor in ("electron", "muon")
    for charge in ("minus", "plus")
) + tuple(
    f"{level}_n_{flavor}s"
    for level in ("lhe", "dressed", "reco")
    for flavor in ("electron", "muon")
)


class MatchError(ValueError):
    """Raised when the LHE and Delphes streams cannot be matched exactly."""


class ProvenanceError(MatchError):
    """Raised when stage metadata do not describe one coherent job."""


@dataclass(frozen=True)
class ProvenanceBundle:
    """Validated and JSON-serializable provenance from all upstream stages."""

    generation: dict[str, object]
    lhe_contract: dict[str, object]
    alignment: dict[str, object]
    simulation: dict[str, object]
    files: dict[str, dict[str, str]]
    marker_id_weight_index: int
    marker_unit_weight_index: int
    hepmc_weight_count: int
    source_id_sequence_sha256: str


class _CompensatedSum:
    """Small Kahan accumulator for signed run-level weight diagnostics."""

    def __init__(self) -> None:
        self.total = 0.0
        self.correction = 0.0

    def add(self, value: float) -> None:
        adjusted = value - self.correction
        updated = self.total + adjusted
        self.correction = (updated - self.total) - adjusted
        self.total = updated


def available_branch_names(tree: object) -> set[str]:
    """Return recursive branch names without parent path prefixes."""

    return set(tree.keys(recursive=True, full_paths=False))  # type: ignore[attr-defined]


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: str | Path) -> str:
    """Calculate a file SHA-256 without loading it into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analysis_code_provenance() -> dict[str, object]:
    """Hash the reducer and every local source/config file that defines it."""

    files: dict[str, dict[str, str]] = {}
    for relative_path in ANALYSIS_CODE_FILES:
        path = REPO_ROOT / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"analysis code provenance file is missing: {path}")
        files[relative_path] = {"sha256": sha256_file(path)}
    return {"hash_algorithm": "sha256", "files": files}


def parse_key_value_metadata(path: str | Path) -> dict[str, str]:
    """Parse a strict ``key=value`` file, preserving ``=`` inside values."""

    path = Path(path)
    metadata: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in raw_line:
            raise ProvenanceError(
                f"{path}:{line_number} is not a key=value metadata line"
            )
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ProvenanceError(f"{path}:{line_number} has an empty key")
        if key in metadata:
            raise ProvenanceError(f"{path} contains duplicate metadata key {key}")
        metadata[key] = value
    return metadata


def _required(metadata: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in metadata:
        raise ProvenanceError(f"{label} metadata is missing {key}")
    return metadata[key]


def _metadata_int(metadata: Mapping[str, Any], key: str, label: str) -> int:
    value = _required(metadata, key, label)
    if isinstance(value, bool):
        raise ProvenanceError(f"{label}.{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProvenanceError(f"{label}.{key} must be an integer") from exc
    return parsed


def _metadata_float(metadata: Mapping[str, Any], key: str, label: str) -> float:
    value = _required(metadata, key, label)
    if isinstance(value, bool):
        raise ProvenanceError(f"{label}.{key} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ProvenanceError(f"{label}.{key} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ProvenanceError(f"{label}.{key} must be finite")
    return parsed


def _metadata_optional_float(
    metadata: Mapping[str, Any], key: str, label: str
) -> float | None:
    if _required(metadata, key, label) is None:
        return None
    return _metadata_float(metadata, key, label)


def _mean_mc_error(sumw: float, sumw2: float, count: int, label: str) -> float | None:
    if count < 2:
        return None
    variance_numerator = sumw2 - sumw * sumw / count
    scale = max(sumw2, sumw * sumw / count, 1.0)
    if variance_numerator < -1.0e-12 * scale:
        raise ProvenanceError(f"{label} nominal-weight moments are inconsistent")
    return math.sqrt(max(variance_numerator, 0.0) / (count * (count - 1)))


def _metadata_bool(metadata: Mapping[str, Any], key: str, label: str) -> bool:
    value = str(_required(metadata, key, label)).lower()
    if value not in {"true", "false"}:
        raise ProvenanceError(f"{label}.{key} must be true or false")
    return value == "true"


def _sha256_value(value: Any, label: str) -> str:
    normalized = str(value).lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ProvenanceError(f"{label} is not a lowercase SHA-256 digest")
    return normalized


def _nested(mapping: Mapping[str, Any], path: Sequence[str], label: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            raise ProvenanceError(f"{label} is missing {'.'.join(path)}")
        current = current[key]
    return current


def _expect_equal(label: str, values: Mapping[str, Any]) -> Any:
    iterator = iter(values.items())
    _first_name, first_value = next(iterator)
    for name, value in iterator:
        if value != first_value:
            rendered = ", ".join(f"{key}={item!r}" for key, item in values.items())
            raise ProvenanceError(f"{label} mismatch: {rendered}")
    return first_value


def decode_source_event_id(
    id_weight: object,
    unit_weight: object,
    *,
    label: str,
) -> int:
    """Decode a source ID from a common-factor weight pair.

    The two values have passed through floating-point LHE/HepMC serializers, so
    the ratio is allowed a small bounded roundoff error.  The tolerance is
    capped well below half an integer, and the decoded value is restricted to
    the bounded range calibrated by the generation-side HepMC2 parser.
    """

    try:
        numerator = float(id_weight)
        denominator = float(unit_weight)
    except (TypeError, ValueError) as exc:
        raise MatchError(f"{label} marker weights must be numeric") from exc
    if not math.isfinite(numerator) or not math.isfinite(denominator):
        raise MatchError(f"{label} marker weights must be finite")
    if denominator == 0.0:
        raise MatchError(f"{label} {MARKER_UNIT_WEIGHT_NAME} is zero")
    ratio = numerator / denominator
    if not math.isfinite(ratio):
        raise MatchError(f"{label} source-event ID ratio is not finite")
    rounded = round(ratio)
    tolerance = max(
        HEPMC_ABSOLUTE_TOLERANCE,
        abs(ratio) * HEPMC_RELATIVE_TOLERANCE,
    )
    if tolerance >= 0.25:
        raise MatchError(f"{label} source-event ID is too large to decode safely")
    if abs(ratio - rounded) > tolerance:
        raise MatchError(
            f"{label} source-event ID ratio {ratio!r} is not integral "
            f"within tolerance {tolerance:.3g}"
        )
    if rounded <= 0 or rounded > MAX_SOURCE_EVENT_ID:
        raise MatchError(
            f"{label} source-event ID {rounded} is outside 1..{MAX_SOURCE_EVENT_ID}"
        )
    return int(rounded)


def _source_id_sequence_hasher() -> Any:
    """Return the canonical source-ID sequence digest accumulator."""

    return hashlib.sha256()


def _update_source_id_sequence(digest: Any, source_event_id: int) -> None:
    """Hash one source ID as an unsigned big-endian 64-bit word."""

    _require_unsigned("source_event_id", source_event_id, 64)
    digest.update(source_event_id.to_bytes(8, "big"))


def _normalize_key_value(
    raw: Mapping[str, str],
    *,
    integer_keys: set[str],
    float_keys: set[str],
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key in sorted(raw):
        value = raw[key]
        if key in integer_keys:
            normalized[key] = _metadata_int(raw, key, "metadata")
        elif key in float_keys:
            normalized[key] = _metadata_float(raw, key, "metadata")
        elif value.lower() in {"true", "false"}:
            normalized[key] = value.lower() == "true"
        elif value.lower() == "none":
            normalized[key] = None
        else:
            normalized[key] = value
    return normalized


def _release_triplet(release: object, label: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(release))
    if match is None:
        raise ProvenanceError(f"{label} must have major.minor.patch form")
    parts = tuple(int(value) for value in match.groups())
    if any(value >= 1 << 16 for value in parts):
        raise ProvenanceError(f"{label} components exceed uint16")
    return parts  # type: ignore[return-value]


def load_and_validate_provenance(
    *,
    lhe_path: str | Path,
    delphes_path: str | Path,
    generation_metadata_path: str | Path,
    lhe_contract_metadata_path: str | Path,
    alignment_metadata_path: str | Path,
    simulation_metadata_path: str | Path,
    sample: str,
) -> ProvenanceBundle:
    """Load all stage metadata and prove that they describe the given files."""

    paths = {
        "lhe": Path(lhe_path).expanduser().resolve(),
        "delphes": Path(delphes_path).expanduser().resolve(),
        "generation_metadata": Path(generation_metadata_path).expanduser().resolve(),
        "lhe_contract_metadata": Path(lhe_contract_metadata_path)
        .expanduser()
        .resolve(),
        "alignment_metadata": Path(alignment_metadata_path).expanduser().resolve(),
        "simulation_metadata": Path(simulation_metadata_path).expanduser().resolve(),
    }
    for label, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label} does not exist: {path}")

    generation_raw = parse_key_value_metadata(paths["generation_metadata"])
    simulation_raw = parse_key_value_metadata(paths["simulation_metadata"])
    try:
        lhe_contract_value = json.loads(
            paths["lhe_contract_metadata"].read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ProvenanceError("LHE contract metadata is not valid JSON") from exc
    if not isinstance(lhe_contract_value, dict):
        raise ProvenanceError("LHE contract metadata must be a JSON object")
    lhe_contract: dict[str, Any] = lhe_contract_value
    try:
        alignment_value = json.loads(
            paths["alignment_metadata"].read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ProvenanceError("alignment metadata is not valid JSON") from exc
    if not isinstance(alignment_value, dict):
        raise ProvenanceError("alignment metadata must be a JSON object")
    alignment: dict[str, Any] = alignment_value

    _expect_equal(
        "generation metadata schema version",
        {
            "metadata": _metadata_int(generation_raw, "schema_version", "generation"),
            "supported": 1,
        },
    )
    _expect_equal(
        "LHE contract metadata schema version",
        {
            "metadata": _metadata_int(lhe_contract, "schema_version", "LHE contract"),
            "supported": 2,
        },
    )
    _expect_equal(
        "alignment metadata schema version",
        {
            "metadata": _metadata_int(alignment, "schema_version", "alignment"),
            "supported": 2,
        },
    )
    _expect_equal(
        "simulation metadata schema version",
        {
            "metadata": _metadata_int(simulation_raw, "schema_version", "simulation"),
            "supported": 2,
        },
    )

    actual_hashes = {name: sha256_file(path) for name, path in paths.items()}
    matched_lhe_sha = _sha256_value(
        _nested(alignment, ("files", "matched_lhe", "sha256"), "alignment"),
        "alignment.files.matched_lhe.sha256",
    )
    alignment_hepmc_sha = _sha256_value(
        _nested(alignment, ("files", "hepmc", "sha256"), "alignment"),
        "alignment.files.hepmc.sha256",
    )
    job_option_sha = _sha256_value(
        _nested(alignment, ("files", "job_option", "sha256"), "alignment"),
        "alignment.files.job_option.sha256",
    )
    lhe_contract_metadata_sha = _sha256_value(
        _nested(
            alignment,
            ("files", "lhe_contract_metadata", "sha256"),
            "alignment",
        ),
        "alignment.files.lhe_contract_metadata.sha256",
    )
    simulation_input_sha = _sha256_value(
        _required(simulation_raw, "input_sha256", "simulation"),
        "simulation.input_sha256",
    )
    simulation_output_sha = _sha256_value(
        _required(simulation_raw, "output_sha256", "simulation"),
        "simulation.output_sha256",
    )
    simulation_generation_metadata_sha = _sha256_value(
        _required(simulation_raw, "generation_metadata_sha256", "simulation"),
        "simulation.generation_metadata_sha256",
    )
    simulation_alignment_metadata_sha = _sha256_value(
        _required(simulation_raw, "alignment_metadata_sha256", "simulation"),
        "simulation.alignment_metadata_sha256",
    )

    _expect_equal(
        "matched LHE SHA-256",
        {"actual": actual_hashes["lhe"], "alignment": matched_lhe_sha},
    )
    _expect_equal(
        "Delphes SHA-256",
        {"actual": actual_hashes["delphes"], "simulation": simulation_output_sha},
    )
    _expect_equal(
        "HepMC SHA-256",
        {"alignment": alignment_hepmc_sha, "simulation": simulation_input_sha},
    )
    _expect_equal(
        "generation metadata SHA-256",
        {
            "actual": actual_hashes["generation_metadata"],
            "simulation": simulation_generation_metadata_sha,
        },
    )
    _expect_equal(
        "alignment metadata SHA-256",
        {
            "actual": actual_hashes["alignment_metadata"],
            "simulation": simulation_alignment_metadata_sha,
        },
    )
    _expect_equal(
        "LHE contract metadata SHA-256",
        {
            "actual": actual_hashes["lhe_contract_metadata"],
            "alignment": lhe_contract_metadata_sha,
        },
    )

    generation_process = str(_required(generation_raw, "process", "generation"))
    lhe_contract_process = str(_required(lhe_contract, "process", "LHE contract"))
    alignment_process = str(_required(alignment, "process", "alignment"))
    simulation_process = str(_required(simulation_raw, "process", "simulation"))
    _expect_equal(
        "process",
        {
            "CLI": sample,
            "generation": generation_process,
            "LHE_contract": lhe_contract_process,
            "alignment": alignment_process,
            "simulation": simulation_process,
        },
    )

    generation_seed = _metadata_int(generation_raw, "seed", "generation")
    alignment_seed = _metadata_int(alignment, "random_seed", "alignment")
    simulation_generation_seed = _metadata_int(
        simulation_raw, "generation_seed", "simulation"
    )
    _expect_equal(
        "generation seed",
        {
            "generation": generation_seed,
            "alignment": alignment_seed,
            "simulation": simulation_generation_seed,
        },
    )

    generation_events = _metadata_int(generation_raw, "events", "generation")
    lhe_contract_requested_events = _metadata_int(
        lhe_contract, "requested_hepmc_events", "LHE contract"
    )
    counts = _required(alignment, "counts", "alignment")
    if not isinstance(counts, Mapping):
        raise ProvenanceError("alignment.counts must be an object")
    count_values = {
        "generation": generation_events,
        "LHE_contract": lhe_contract_requested_events,
        "alignment_requested": _metadata_int(
            counts, "requested_hepmc_events", "alignment.counts"
        ),
        "alignment_hepmc": _metadata_int(counts, "hepmc_events", "alignment.counts"),
        "alignment_matched_lhe": _metadata_int(
            counts, "matched_lhe_events", "alignment.counts"
        ),
        "simulation_input": _metadata_int(simulation_raw, "input_events", "simulation"),
        "simulation_output": _metadata_int(
            simulation_raw, "output_events", "simulation"
        ),
    }
    expected_events = int(_expect_equal("event count", count_values))
    if expected_events <= 0:
        raise ProvenanceError("event count must be positive")
    if expected_events > MAX_EVENTS_PER_JOB:
        raise ProvenanceError(
            f"event count exceeds named-weight contract cap {MAX_EVENTS_PER_JOB}"
        )

    generation_first_event = _metadata_int(generation_raw, "first_event", "generation")
    alignment_first_event = _metadata_int(alignment, "first_event", "alignment")
    _expect_equal(
        "first event",
        {"generation": generation_first_event, "alignment": alignment_first_event},
    )
    generation_run_number = _metadata_int(generation_raw, "run_number", "generation")
    alignment_run_number = _metadata_int(alignment, "run_number", "alignment")
    _expect_equal(
        "run number",
        {"generation": generation_run_number, "alignment": alignment_run_number},
    )
    generation_release = str(
        _required(generation_raw, "athgeneration_release", "generation")
    )
    alignment_release = str(_required(alignment, "athgeneration_release", "alignment"))
    _expect_equal(
        "AthGeneration release",
        {"generation": generation_release, "alignment": alignment_release},
    )
    _release_triplet(generation_release, "AthGeneration release")

    generation_contract = str(
        _required(generation_raw, "alignment_contract", "generation")
    )
    alignment_contract = str(_required(alignment, "contract", "alignment"))
    _expect_equal(
        "alignment contract",
        {"generation": generation_contract, "alignment": alignment_contract},
    )
    if alignment_contract != ALIGNMENT_CONTRACT:
        raise ProvenanceError(f"unsupported alignment contract {alignment_contract!r}")
    generation_event_id_contract = str(
        _required(generation_raw, "lhe_event_id_contract", "generation")
    )
    _expect_equal(
        "LHE event-ID contract",
        {
            "generation": generation_event_id_contract,
            "alignment": alignment_contract,
            "LHE_contract": str(_required(lhe_contract, "contract", "LHE contract")),
        },
    )
    _expect_equal(
        "LHE normalization contract",
        {
            "LHE_contract": str(
                _required(lhe_contract, "normalization_contract", "LHE contract")
            ),
            "supported": NORMALIZATION_CONTRACT,
        },
    )
    _expect_equal(
        "LHE nominal-weight units",
        {
            "LHE_contract": str(
                _required(lhe_contract, "nominal_weight_units", "LHE contract")
            ),
            "required": "pb",
        },
    )
    _expect_equal(
        "LHE weighting strategy",
        {
            "LHE_contract": _metadata_int(
                lhe_contract, "lhe_weighting_strategy", "LHE contract"
            ),
            "required": -4,
        },
    )
    _expect_equal(
        "LHE cross-section method",
        {
            "LHE_contract": str(
                _required(lhe_contract, "cross_section_method", "LHE contract")
            ),
            "required": CROSS_SECTION_METHOD,
        },
    )
    lhe_init = _required(lhe_contract, "lhe_init", "LHE contract")
    if not isinstance(lhe_init, Mapping):
        raise ProvenanceError("LHE contract lhe_init must be an object")
    _expect_equal(
        "LHE init weighting strategy",
        {
            "LHE_contract": _metadata_int(lhe_init, "idwtup", "LHE contract.lhe_init"),
            "required": -4,
        },
    )
    _expect_equal(
        "LHE ID-weight name",
        {
            "LHE_contract": str(
                _required(lhe_contract, "marker_id_weight", "LHE contract")
            ),
            "required": MARKER_ID_WEIGHT_NAME,
        },
    )
    _expect_equal(
        "LHE unit-weight name",
        {
            "LHE_contract": str(
                _required(lhe_contract, "marker_unit_weight", "LHE contract")
            ),
            "required": MARKER_UNIT_WEIGHT_NAME,
        },
    )

    expected_m4l_min = _metadata_float(
        generation_raw, "generator_m4l_min_gev", "generation"
    )
    expected_m4l_max = _metadata_float(
        generation_raw, "generator_m4l_max_gev", "generation"
    )
    _expect_equal(
        "generator m4l minimum",
        {
            "generation": expected_m4l_min,
            "LHE_contract": _metadata_float(
                lhe_contract, "m4l_min_gev", "LHE contract"
            ),
        },
    )
    _expect_equal(
        "generator m4l maximum",
        {
            "generation": expected_m4l_max,
            "LHE_contract": _metadata_float(
                lhe_contract, "m4l_max_gev", "LHE contract"
            ),
        },
    )
    signed_phase_space_efficiency = _metadata_optional_float(
        lhe_contract, "signed_filter_efficiency", "LHE contract"
    )
    absolute_phase_space_efficiency = _metadata_optional_float(
        lhe_contract, "absolute_filter_efficiency", "LHE contract"
    )
    if absolute_phase_space_efficiency is not None and not (
        0.0 <= absolute_phase_space_efficiency <= 1.0 + 1.0e-12
    ):
        raise ProvenanceError(
            "LHE contract absolute_filter_efficiency is outside [0, 1]"
        )
    generated_lhe_events = _metadata_int(
        lhe_contract, "generated_lhe_events", "LHE contract"
    )
    accepted_lhe_events = _metadata_int(
        lhe_contract, "accepted_lhe_events", "LHE contract"
    )
    rejected_below_m4l = _metadata_int(
        lhe_contract, "rejected_below_m4l", "LHE contract"
    )
    rejected_above_m4l = _metadata_int(
        lhe_contract, "rejected_above_m4l", "LHE contract"
    )
    if (
        min(
            generated_lhe_events,
            accepted_lhe_events,
            rejected_below_m4l,
            rejected_above_m4l,
        )
        < 0
    ):
        raise ProvenanceError("LHE contract event counts must be non-negative")
    if generated_lhe_events != (
        accepted_lhe_events + rejected_below_m4l + rejected_above_m4l
    ):
        raise ProvenanceError("LHE contract phase-space counts do not close")
    if accepted_lhe_events < expected_events:
        raise ProvenanceError(
            "LHE contract accepted fewer events than the HepMC request"
        )
    count_phase_space_efficiency = _metadata_float(
        lhe_contract, "count_filter_efficiency", "LHE contract"
    )
    if not math.isclose(
        count_phase_space_efficiency,
        accepted_lhe_events / generated_lhe_events,
        rel_tol=1.0e-12,
        abs_tol=1.0e-15,
    ):
        raise ProvenanceError(
            "LHE contract count_filter_efficiency disagrees with event counts"
        )
    _expect_equal(
        "generated LHE count",
        {
            "LHE_contract": generated_lhe_events,
            "alignment": _metadata_int(
                counts, "generated_lhe_events", "alignment.counts"
            ),
        },
    )
    _expect_equal(
        "phase-space-selected LHE count",
        {
            "LHE_contract": accepted_lhe_events,
            "alignment": _metadata_int(
                counts, "phase_space_lhe_events", "alignment.counts"
            ),
        },
    )
    sumw_generated = _metadata_float(lhe_contract, "sumw_generated", "LHE contract")
    sumw_accepted = _metadata_float(lhe_contract, "sumw_accepted", "LHE contract")
    sumw2_generated = _metadata_float(
        lhe_contract, "sumw2_generated", "LHE contract"
    )
    sumw2_accepted = _metadata_float(
        lhe_contract, "sumw2_accepted", "LHE contract"
    )
    sumabsw_generated = _metadata_float(
        lhe_contract, "sumabsw_generated", "LHE contract"
    )
    sumabsw_accepted = _metadata_float(lhe_contract, "sumabsw_accepted", "LHE contract")
    if sumabsw_generated < 0.0 or not 0.0 <= sumabsw_accepted <= sumabsw_generated:
        raise ProvenanceError("LHE contract absolute-weight sums are inconsistent")
    if sumw2_generated < 0.0 or not 0.0 <= sumw2_accepted <= (
        sumw2_generated + 1.0e-12 * max(sumw2_generated, 1.0)
    ):
        raise ProvenanceError("LHE contract squared-weight sums are inconsistent")
    if sumw_generated == 0.0:
        if signed_phase_space_efficiency is not None:
            raise ProvenanceError(
                "LHE contract signed_filter_efficiency must be null for zero sumw"
            )
    elif signed_phase_space_efficiency is None or not math.isclose(
        signed_phase_space_efficiency,
        sumw_accepted / sumw_generated,
        rel_tol=1.0e-12,
        abs_tol=1.0e-15,
    ):
        raise ProvenanceError(
            "LHE contract signed_filter_efficiency disagrees with weight sums"
        )
    if sumabsw_generated == 0.0:
        if absolute_phase_space_efficiency is not None:
            raise ProvenanceError(
                "LHE contract absolute_filter_efficiency must be null for zero sumabsw"
            )
    elif absolute_phase_space_efficiency is None or not math.isclose(
        absolute_phase_space_efficiency,
        sumabsw_accepted / sumabsw_generated,
        rel_tol=1.0e-12,
        abs_tol=1.0e-15,
    ):
        raise ProvenanceError(
            "LHE contract absolute_filter_efficiency disagrees with weight sums"
        )

    inclusive_cross_section = _metadata_float(
        lhe_contract, "inclusive_cross_section_pb", "LHE contract"
    )
    filtered_cross_section = _metadata_float(
        lhe_contract, "filtered_cross_section_pb", "LHE contract"
    )
    for field, observed, expected_value in (
        (
            "inclusive_cross_section_pb",
            inclusive_cross_section,
            sumw_generated / generated_lhe_events,
        ),
        (
            "filtered_cross_section_pb",
            filtered_cross_section,
            sumw_accepted / generated_lhe_events,
        ),
    ):
        if not math.isclose(
            observed, expected_value, rel_tol=1.0e-12, abs_tol=1.0e-15
        ):
            raise ProvenanceError(
                f"LHE contract {field} disagrees with nominal-weight mean"
            )
    if signed_phase_space_efficiency is not None and not math.isclose(
        filtered_cross_section,
        inclusive_cross_section * signed_phase_space_efficiency,
        rel_tol=1.0e-12,
        abs_tol=1.0e-15,
    ):
        raise ProvenanceError(
            "LHE contract filtered cross section disagrees with signed efficiency"
        )

    for field, sumw, sumw2 in (
        ("inclusive_cross_section_mc_error_pb", sumw_generated, sumw2_generated),
        ("filtered_cross_section_mc_error_pb", sumw_accepted, sumw2_accepted),
    ):
        observed_error = _metadata_optional_float(lhe_contract, field, "LHE contract")
        expected_error = _mean_mc_error(
            sumw, sumw2, generated_lhe_events, "LHE contract"
        )
        if expected_error is None:
            if observed_error is not None:
                raise ProvenanceError(f"LHE contract {field} must be null for N<2")
        elif observed_error is None or observed_error < 0.0 or not math.isclose(
            observed_error,
            expected_error,
            rel_tol=1.0e-12,
            abs_tol=1.0e-15,
        ):
            raise ProvenanceError(
                f"LHE contract {field} disagrees with nominal-weight moments"
            )

    marker = _required(alignment, "marker", "alignment")
    if not isinstance(marker, Mapping):
        raise ProvenanceError("alignment.marker must be an object")
    marker_id_name = str(_required(marker, "id_weight_name", "alignment.marker"))
    marker_unit_name = str(_required(marker, "unit_weight_name", "alignment.marker"))
    _expect_equal(
        "source-ID marker name",
        {"alignment": marker_id_name, "required": MARKER_ID_WEIGHT_NAME},
    )
    _expect_equal(
        "source-ID unit marker name",
        {"alignment": marker_unit_name, "required": MARKER_UNIT_WEIGHT_NAME},
    )
    marker_recovery = str(_required(marker, "recovery", "alignment.marker"))
    if marker_recovery != "ratio":
        raise ProvenanceError(
            f"unsupported alignment.marker.recovery {marker_recovery!r}"
        )
    marker_id_index = _metadata_int(marker, "id_weight_index", "alignment.marker")
    marker_unit_index = _metadata_int(marker, "unit_weight_index", "alignment.marker")
    if marker_id_index < 0 or marker_unit_index < 0:
        raise ProvenanceError("alignment marker indices must be non-negative")
    if marker_id_index == marker_unit_index:
        raise ProvenanceError("alignment marker indices must be distinct")
    hepmc_weight_names = _required(alignment, "hepmc_weight_names", "alignment")
    if not isinstance(hepmc_weight_names, list) or not all(
        isinstance(name, str) for name in hepmc_weight_names
    ):
        raise ProvenanceError("alignment.hepmc_weight_names must be a string array")
    if len(set(hepmc_weight_names)) != len(hepmc_weight_names):
        raise ProvenanceError("alignment.hepmc_weight_names contains duplicate names")
    for index, expected_name in (
        (marker_id_index, MARKER_ID_WEIGHT_NAME),
        (marker_unit_index, MARKER_UNIT_WEIGHT_NAME),
    ):
        if index >= len(hepmc_weight_names):
            raise ProvenanceError(
                f"alignment marker index {index} exceeds HepMC weight-name array"
            )
        if hepmc_weight_names[index] != expected_name:
            raise ProvenanceError(
                f"alignment HepMC weight index {index} is "
                f"{hepmc_weight_names[index]!r}, expected {expected_name!r}"
            )
    source_id_sequence_sha = _sha256_value(
        _required(marker, "source_id_sequence_sha256", "alignment.marker"),
        "alignment.marker.source_id_sequence_sha256",
    )
    if (
        str(_required(marker, "source_id_sequence_encoding", "alignment.marker"))
        != "concatenated uint64 big-endian, no delimiter"
    ):
        raise ProvenanceError("unsupported source-ID sequence encoding")
    precision = _required(alignment, "hepmc_precision_contract", "alignment")
    if not isinstance(precision, Mapping):
        raise ProvenanceError("alignment.hepmc_precision_contract must be an object")
    _expect_equal(
        "HepMC source-ID relative tolerance",
        {
            "alignment": _metadata_float(
                precision,
                "relative_ratio_tolerance",
                "alignment.hepmc_precision_contract",
            ),
            "analysis": HEPMC_RELATIVE_TOLERANCE,
        },
    )
    _expect_equal(
        "HepMC source-ID absolute tolerance",
        {
            "alignment": _metadata_float(
                precision,
                "absolute_ratio_tolerance",
                "alignment.hepmc_precision_contract",
            ),
            "analysis": HEPMC_ABSOLUTE_TOLERANCE,
        },
    )
    _expect_equal(
        "maximum source-event ID",
        {
            "alignment": _metadata_int(
                precision,
                "maximum_source_event_id",
                "alignment.hepmc_precision_contract",
            ),
            "analysis": MAX_SOURCE_EVENT_ID,
        },
    )
    alignment_phase_space = _required(alignment, "phase_space_filter", "alignment")
    if not isinstance(alignment_phase_space, Mapping):
        raise ProvenanceError("alignment.phase_space_filter must be an object")
    for key in (
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
    ):
        _expect_equal(
            f"phase-space field {key}",
            {
                "LHE_contract": _required(lhe_contract, key, "LHE contract"),
                "alignment": _required(
                    alignment_phase_space, key, "alignment.phase_space_filter"
                ),
            },
        )
    alignment_conditions = _required(alignment, "contract_conditions", "alignment")
    if not isinstance(alignment_conditions, Mapping):
        raise ProvenanceError("alignment.contract_conditions must be an object")
    if not _metadata_bool(
        alignment_conditions,
        "hepmc_source_ids_strictly_increasing",
        "alignment.contract_conditions",
    ):
        raise ProvenanceError("alignment did not validate increasing source IDs")
    if not _metadata_bool(simulation_raw, "event_retention_validated", "simulation"):
        raise ProvenanceError("simulation did not validate event retention")
    if not _metadata_bool(simulation_raw, "event_order_preserved", "simulation"):
        raise ProvenanceError("simulation did not preserve event order")
    if _required(simulation_raw, "event_number_branch", "simulation") != "Event.Number":
        raise ProvenanceError("simulation event-number branch is not Event.Number")
    preserved_weight_branches = {
        value.strip()
        for value in str(
            _required(simulation_raw, "weight_branches_preserved", "simulation")
        ).split(",")
        if value.strip()
    }
    if not {"Event.Weight", "Weight.Weight"}.issubset(preserved_weight_branches):
        raise ProvenanceError(
            "simulation did not declare Event.Weight and Weight.Weight preservation"
        )
    if (
        str(_required(simulation_raw, "cross_section_semantics", "simulation"))
        != "conditional_on_lhe_phase_space_filter"
    ):
        raise ProvenanceError("simulation cross-section semantics are unsupported")
    if not math.isclose(
        _metadata_float(simulation_raw, "weight_scale", "simulation"),
        1.0,
        rel_tol=0.0,
        abs_tol=0.0,
    ):
        raise ProvenanceError(
            "simulation weight scale is not the direct-2e2mu identity"
        )

    generation = _normalize_key_value(
        generation_raw,
        integer_keys={
            "schema_version",
            "seed",
            "events",
            "first_event",
            "run_number",
        },
        float_keys={
            "ecm_energy_gev",
            "generator_mll_min_gev",
            "generator_m4l_min_gev",
            "generator_m4l_max_gev",
            "analysis_mz_min_gev",
            "analysis_mz_max_gev",
            "analysis_m4l_min_gev",
            "target_generation_phase_space_m4l_max_gev",
        },
    )
    simulation = _normalize_key_value(
        simulation_raw,
        integer_keys={
            "schema_version",
            "generation_seed",
            "random_seed",
            "input_events",
            "output_events",
            "max_events",
        },
        float_keys={"weight_scale"},
    )
    # Make the validated values easy to consume even if future metadata gains
    # additional string-valued fields.
    generation["events"] = expected_events
    generation["first_event"] = generation_first_event
    generation["run_number"] = generation_run_number
    generation["seed"] = generation_seed
    lhe_contract["signed_filter_efficiency"] = signed_phase_space_efficiency
    lhe_contract["absolute_filter_efficiency"] = absolute_phase_space_efficiency
    alignment["job_option_sha256"] = job_option_sha

    embedded_files = {
        name: {"path": str(paths[name]), "sha256": actual_hashes[name]}
        for name in paths
    }
    embedded_files["hepmc"] = {
        "path": str(_required(simulation_raw, "input_file", "simulation")),
        "sha256": alignment_hepmc_sha,
    }
    return ProvenanceBundle(
        generation=generation,
        lhe_contract=lhe_contract,
        alignment=alignment,
        simulation=simulation,
        files=embedded_files,
        marker_id_weight_index=marker_id_index,
        marker_unit_weight_index=marker_unit_index,
        hepmc_weight_count=len(hepmc_weight_names),
        source_id_sequence_sha256=source_id_sequence_sha,
    )


def event_uid(
    campaign_id: int,
    sample_code: int,
    job_id: int,
    source_event_id: int,
) -> tuple[int, int]:
    """Return a deterministic BLAKE2b-128 identity as two unsigned words."""

    _require_unsigned("campaign_id", campaign_id, 64)
    _require_unsigned("sample_code", sample_code, 8)
    _require_unsigned("job_id", job_id, 32)
    _require_unsigned("source_event_id", source_event_id, 64)
    payload = b"".join(
        (
            UID_SCHEMA_TAG,
            campaign_id.to_bytes(8, "big"),
            sample_code.to_bytes(1, "big"),
            job_id.to_bytes(4, "big"),
            source_event_id.to_bytes(8, "big"),
        )
    )
    digest = hashlib.blake2b(payload, digest_size=16).digest()
    return int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:], "big")


def _require_unsigned(name: str, value: int, bits: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0 or value >= 1 << bits:
        raise ValueError(f"{name} must be in [0, 2**{bits})")


def _empty_counts(level: str) -> dict[str, int]:
    return {
        f"{level}_n_electron_minus": 0,
        f"{level}_n_electron_plus": 0,
        f"{level}_n_muon_minus": 0,
        f"{level}_n_muon_plus": 0,
        f"{level}_n_electrons": 0,
        f"{level}_n_muons": 0,
    }


def _counts_from_buckets(
    level: str, buckets: Mapping[str, Sequence[object]]
) -> dict[str, int]:
    counts = _empty_counts(level)
    for key in LEPTON_KEYS:
        count = len(buckets[key])
        if count >= 1 << 16:
            raise ValueError(f"{level} {key} multiplicity exceeds uint16")
        counts[f"{level}_n_{key}"] = count
    counts[f"{level}_n_electrons"] = (
        counts[f"{level}_n_electron_minus"] + counts[f"{level}_n_electron_plus"]
    )
    counts[f"{level}_n_muons"] = (
        counts[f"{level}_n_muon_minus"] + counts[f"{level}_n_muon_plus"]
    )
    for flavor in ("electrons", "muons"):
        if counts[f"{level}_n_{flavor}"] >= 1 << 16:
            raise ValueError(f"{level} {flavor} multiplicity exceeds uint16")
    return counts


def _unique_leptons(
    buckets: Mapping[str, Sequence[object]],
) -> dict[str, object] | None:
    if any(len(buckets[key]) != 1 for key in LEPTON_KEYS):
        return None
    return {key: buckets[key][0] for key in LEPTON_KEYS}


def _level_record(
    level: str, leptons: Mapping[str, object] | None
) -> dict[str, object]:
    if leptons is None:
        record = empty_level_record(level, include_momenta=True, topology_valid=False)
    else:
        try:
            record = build_level_record(leptons, level, include_momenta=True)
        except (TypeError, ValueError, RuntimeError):
            # A kinematic or angular degeneracy must not remove the source row.
            # The raw topology still exists, while all projection-dependent
            # values are explicitly unavailable.
            record = empty_level_record(
                level, include_momenta=True, topology_valid=True
            )
            for key in LEPTON_KEYS:
                momentum = leptons[key]
                for component in ("px", "py", "pz", "E"):
                    record[f"{level}_raw_{key}_{component}"] = float(
                        getattr(momentum, component)
                    )
    record[f"{level}_candidate"] = bool(record[f"{level}_topology_valid"])
    return record


def _lhe_record(event: object, index: int) -> dict[str, object]:
    eventinfo = event.eventinfo  # type: ignore[attr-defined]
    weight = float(eventinfo.weight)
    if not math.isfinite(weight):
        raise ValueError(f"LHE event {index} has a non-finite nominal weight")

    buckets: dict[str, list[object]] = {key: [] for key in LEPTON_KEYS}
    for particle in event.particles:  # type: ignore[attr-defined]
        pid = int(particle.id)
        if int(particle.status) == 1 and pid in LEPTON_PDGS:
            buckets[LEPTON_PDGS[pid]].append(particle)

    leptons: Mapping[str, object] | None
    try:
        extracted = extract_event_particles(event)
    except (TypeError, ValueError):
        leptons = None
    else:
        leptons = extracted.leptons

    all_detailed_weights = {
        str(key): float(value)
        for key, value in (event.weights or {}).items()  # type: ignore[attr-defined]
    }
    if any(not math.isfinite(value) for value in all_detailed_weights.values()):
        raise ValueError(f"LHE event {index} has a non-finite alternative weight")
    missing_markers = {
        MARKER_ID_WEIGHT_NAME,
        MARKER_UNIT_WEIGHT_NAME,
    }.difference(all_detailed_weights)
    if missing_markers:
        raise MatchError(
            f"LHE event {index} is missing source-ID marker weight(s): "
            + ", ".join(sorted(missing_markers))
        )
    source_event_id = decode_source_event_id(
        all_detailed_weights.pop(MARKER_ID_WEIGHT_NAME),
        all_detailed_weights.pop(MARKER_UNIT_WEIGHT_NAME),
        label=f"LHE event {index}",
    )
    alternative_weights = all_detailed_weights
    alternative_weight_count = len(alternative_weights)
    if alternative_weight_count >= 1 << 16:
        raise ValueError(
            f"LHE event {index} has too many alternative weights for uint16"
        )
    record: dict[str, object] = {
        "lhe_event_index": np.uint64(index),
        "source_event_id": np.uint64(source_event_id),
        "weight_lhe": np.float64(weight),
        "has_lhe": True,
        "lhe_n_alternative_weights": alternative_weight_count,
        "_lhe_alternative_weights": alternative_weights,
    }
    record.update(_counts_from_buckets("lhe", buckets))
    record.update(_level_record("lhe", leptons))
    if leptons is None:
        record["lhe_status"] = int(LHEStatus.INVALID_TOPOLOGY)
    elif not bool(record["lhe_projection_valid"]):
        record["lhe_status"] = int(LHEStatus.PROJECTION_FAILED)
    else:
        record["lhe_status"] = int(LHEStatus.VALID)
    return record


def iter_lhe_level_records(path: str | Path) -> Iterator[dict[str, object]]:
    """Yield exactly one unfiltered record for every physical LHE event."""

    lhe_file = pylhe.LHEFile.fromfile(Path(path), with_attributes=True, generator=True)
    for index, event in enumerate(lhe_file.events):
        yield _lhe_record(event, index)


def _row_values(rows: Mapping[str, list], branch: str, event: int) -> list:
    value = rows[branch][event]
    if not isinstance(value, list):
        raise TypeError(f"{branch} is not an event-wise collection")
    return value


def _event_number(rows: Mapping[str, list], event: int) -> int:
    value = rows["Event.Number"][event]
    if isinstance(value, list):
        if len(value) != 1:
            raise MatchError(
                "Event.Number must contain exactly one value per Delphes entry"
            )
        value = value[0]
    number = int(value)
    if number < 0:
        raise MatchError(f"Delphes Event.Number must be non-negative, found {number}")
    return number


def _event_float(rows: Mapping[str, list], branch: str, event: int) -> float:
    value = rows[branch][event]
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError(f"{branch} must contain exactly one value per event")
        value = value[0]
    return float(value)


def _dressed_record(rows: Mapping[str, list], event: int) -> dict[str, object]:
    buckets: dict[str, list[object]] = {key: [] for key in LEPTON_KEYS}
    for branch in ("DressedElectron", "DressedMuon"):
        values = [
            _row_values(rows, f"{branch}.{component}", event)
            for component in ("PID", "E", "Px", "Py", "Pz")
        ]
        sizes = {len(value) for value in values}
        if len(sizes) != 1:
            raise ValueError(f"inconsistent {branch} leaf lengths in event {event}")
        for pid, energy, px, py, pz in zip(*values):
            integer_pid = int(pid)
            if integer_pid not in LEPTON_PDGS:
                continue
            buckets[LEPTON_PDGS[integer_pid]].append(
                vector.obj(E=float(energy), px=float(px), py=float(py), pz=float(pz))
            )
    record: dict[str, object] = _counts_from_buckets("dressed", buckets)
    record.update(_level_record("dressed", _unique_leptons(buckets)))
    return record


def _charge_key(flavor: str, charge: object) -> str | None:
    numeric = float(charge)
    if not math.isfinite(numeric):
        return None
    if math.isclose(numeric, -1.0, rel_tol=0.0, abs_tol=1.0e-6):
        return f"{flavor}_minus"
    if math.isclose(numeric, 1.0, rel_tol=0.0, abs_tol=1.0e-6):
        return f"{flavor}_plus"
    return None


def _reco_record(rows: Mapping[str, list], event: int) -> dict[str, object]:
    buckets: dict[str, list[object]] = {key: [] for key in LEPTON_KEYS}
    for branch, flavor, mass in (
        ("RecoElectron", "electron", 0.00051099895),
        ("RecoMuon", "muon", 0.1056583755),
    ):
        values = [
            _row_values(rows, f"{branch}.{component}", event)
            for component in ("PT", "Eta", "Phi", "Charge")
        ]
        sizes = {len(value) for value in values}
        if len(sizes) != 1:
            raise ValueError(f"inconsistent {branch} leaf lengths in event {event}")
        for pt, eta, phi, charge in zip(*values):
            key = _charge_key(flavor, charge)
            if key is None:
                continue
            buckets[key].append(
                vector.obj(pt=float(pt), eta=float(eta), phi=float(phi), mass=mass)
            )

    leptons = _unique_leptons(buckets)
    record: dict[str, object] = _counts_from_buckets("reco", buckets)
    record.update(_level_record("reco", leptons))
    if leptons is None:
        selection = empty_reco_selection_result()
    else:
        selection = evaluate_reco_selection(leptons)
    selection_record = selection.to_record(namespace="reco")
    if bool(selection_record["reco_candidate"]) != bool(record["reco_topology_valid"]):
        raise RuntimeError("reco candidate and topology flags disagree")
    record.update(selection_record)
    record["reconstructed"] = bool(record["reco_pass_selection"])
    return record


def _template_record() -> dict[str, object]:
    record: dict[str, object] = {
        "campaign_id": np.uint64(0),
        "sample_code": np.uint8(0),
        "job_id": np.uint32(0),
        "lhe_event_index": np.uint64(0),
        "source_event_id": np.uint64(0),
        "event_uid_hi": np.uint64(0),
        "event_uid_lo": np.uint64(0),
        "hepmc_event_number": np.int64(-1),
        "delphes_event_number": np.int64(-1),
        "hepmc_entry": np.uint64(0),
        "delphes_entry": np.uint64(0),
        "weight_lhe": np.float64(np.nan),
        "weight_delphes": np.float64(np.nan),
        "cross_section_pb_delphes": np.float64(np.nan),
        "cross_section_error_pb_delphes": np.float64(np.nan),
        "has_lhe": False,
        "has_hepmc": False,
        "has_delphes": False,
        "lhe_n_alternative_weights": 0,
        "lhe_status": int(LHEStatus.INVALID_TOPOLOGY),
    }
    for level in ("lhe", "dressed", "reco"):
        record.update(_empty_counts(level))
        record.update(_level_record(level, None))
    record.update(empty_reco_selection_result().to_record(namespace="reco"))
    record["reconstructed"] = False
    return record


def output_schema() -> dict[str, np.dtype]:
    """Return the fixed scalar ROOT schema for the Events tree."""

    explicit = {
        "campaign_id": np.dtype("uint64"),
        "sample_code": np.dtype("uint8"),
        "job_id": np.dtype("uint32"),
        "lhe_event_index": np.dtype("uint64"),
        "source_event_id": np.dtype("uint64"),
        "event_uid_hi": np.dtype("uint64"),
        "event_uid_lo": np.dtype("uint64"),
        "hepmc_event_number": np.dtype("int64"),
        "delphes_event_number": np.dtype("int64"),
        "hepmc_entry": np.dtype("uint64"),
        "delphes_entry": np.dtype("uint64"),
        "weight_lhe": np.dtype("float64"),
        "weight_delphes": np.dtype("float64"),
        "cross_section_pb_delphes": np.dtype("float64"),
        "cross_section_error_pb_delphes": np.dtype("float64"),
        "lhe_n_alternative_weights": np.dtype("uint16"),
        "lhe_status": np.dtype("int8"),
    }
    schema: dict[str, np.dtype] = {}
    for name, value in _template_record().items():
        if name in explicit:
            schema[name] = explicit[name]
        elif name in COUNT_BRANCHES:
            schema[name] = np.dtype("uint16")
        elif isinstance(value, (bool, np.bool_)):
            schema[name] = np.dtype("bool")
        elif isinstance(value, (int, np.integer)):
            schema[name] = np.dtype("int16")
        elif isinstance(value, (float, np.floating)):
            schema[name] = np.dtype("float64")
        else:
            raise TypeError(f"unsupported output value {name}={value!r}")
    return schema


def _records_to_arrays(
    records: Sequence[Mapping[str, object]], schema: Mapping[str, np.dtype]
) -> dict[str, np.ndarray]:
    expected = set(schema)
    for index, record in enumerate(records):
        missing = expected.difference(record)
        extra = set(record).difference(expected)
        if missing or extra:
            raise RuntimeError(
                f"output record {index} has schema mismatch; "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
    return {
        name: np.asarray([record[name] for record in records], dtype=dtype)
        for name, dtype in schema.items()
    }


def _weight_tree_schema(weight_count: int) -> dict[str, object]:
    if weight_count <= 0:
        raise ValueError("weight_count must be positive")
    return {
        "campaign_id": np.dtype("uint64"),
        "sample_code": np.dtype("uint8"),
        "job_id": np.dtype("uint32"),
        "lhe_event_index": np.dtype("uint64"),
        "source_event_id": np.dtype("uint64"),
        "event_uid_hi": np.dtype("uint64"),
        "event_uid_lo": np.dtype("uint64"),
        "values": np.dtype((np.float64, (weight_count,))),
    }


def _weight_records_to_arrays(
    records: Sequence[Mapping[str, object]], weight_count: int
) -> dict[str, np.ndarray]:
    return {
        "campaign_id": np.asarray(
            [record["campaign_id"] for record in records], dtype=np.uint64
        ),
        "sample_code": np.asarray(
            [record["sample_code"] for record in records], dtype=np.uint8
        ),
        "job_id": np.asarray([record["job_id"] for record in records], dtype=np.uint32),
        "lhe_event_index": np.asarray(
            [record["lhe_event_index"] for record in records], dtype=np.uint64
        ),
        "source_event_id": np.asarray(
            [record["source_event_id"] for record in records], dtype=np.uint64
        ),
        "event_uid_hi": np.asarray(
            [record["event_uid_hi"] for record in records], dtype=np.uint64
        ),
        "event_uid_lo": np.asarray(
            [record["event_uid_lo"] for record in records], dtype=np.uint64
        ),
        "values": np.asarray(
            [record["values"] for record in records], dtype=np.float64
        ).reshape(len(records), weight_count),
    }


def _acquire_output_lock(path: Path) -> tuple[int, Path]:
    """Claim an output name without exposing an incomplete output file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise FileExistsError(
            f"another reducer is already targeting output: {path}"
        ) from exc
    return descriptor, lock_path


def _release_output_lock(descriptor: int, _lock_path: Path) -> None:
    """Release a claim while retaining the stable lock-file inode."""

    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _temporary_output(path: Path, overwrite: bool) -> Path:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {path}; pass --overwrite")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.partial.", dir=path.parent
    )
    os.close(descriptor)
    return Path(temporary_name)


def _publish_output(temporary: Path, path: Path, overwrite: bool) -> None:
    """Atomically publish, with an atomic no-replace operation by default."""

    if overwrite:
        os.replace(temporary, path)
        return
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        raise FileExistsError(f"output appeared while processing: {path}") from exc
    temporary.unlink()


def build_analysis_tree(
    lhe_path: str | Path,
    delphes_path: str | Path,
    output_path: str | Path,
    *,
    sample: str,
    job_id: int,
    generation_metadata_path: str | Path,
    lhe_contract_metadata_path: str | Path,
    alignment_metadata_path: str | Path,
    simulation_metadata_path: str | Path,
    campaign_id: int = 0,
    delphes_tree_name: str = "Delphes",
    step_size: str = "50 MB",
    overwrite: bool = False,
) -> dict[str, int | float]:
    """Stream, validate, and write one matched job-level ROOT file."""

    if sample not in SAMPLE_CODES:
        raise ValueError(f"sample must be one of {sorted(SAMPLE_CODES)}")
    sample_code = SAMPLE_CODES[sample]
    _require_unsigned("campaign_id", campaign_id, 64)
    _require_unsigned("job_id", job_id, 32)

    lhe_path = Path(lhe_path).expanduser().resolve()
    delphes_path = Path(delphes_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    metadata_paths = {
        "generation": Path(generation_metadata_path).expanduser().resolve(),
        "LHE contract": Path(lhe_contract_metadata_path).expanduser().resolve(),
        "alignment": Path(alignment_metadata_path).expanduser().resolve(),
        "simulation": Path(simulation_metadata_path).expanduser().resolve(),
    }
    for label, path in (("LHE", lhe_path), ("Delphes", delphes_path)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} input does not exist: {path}")
    protected_inputs = {lhe_path, delphes_path, *metadata_paths.values()}
    if output_path in protected_inputs:
        raise ValueError("output must not overwrite an event or metadata input")

    provenance = load_and_validate_provenance(
        lhe_path=lhe_path,
        delphes_path=delphes_path,
        generation_metadata_path=metadata_paths["generation"],
        lhe_contract_metadata_path=metadata_paths["LHE contract"],
        alignment_metadata_path=metadata_paths["alignment"],
        simulation_metadata_path=metadata_paths["simulation"],
        sample=sample,
    )
    analysis_code = analysis_code_provenance()
    expected_metadata_events = int(provenance.generation["events"])
    expected_first_event = int(provenance.generation["first_event"])

    schema = output_schema()
    event_count = 0
    event_number_start: int | None = None
    positive_weights = 0
    negative_weights = 0
    zero_weights = 0
    sumw = _CompensatedSum()
    sumw2 = _CompensatedSum()
    sumabsw = _CompensatedSum()
    sumw_delphes = _CompensatedSum()
    sumw2_delphes = _CompensatedSum()
    sumabsw_delphes = _CompensatedSum()
    first_cross_section = float("nan")
    min_cross_section = float("inf")
    max_cross_section = float("-inf")
    final_cross_section = float("nan")
    first_cross_section_error = float("nan")
    min_cross_section_error = float("inf")
    max_cross_section_error = float("-inf")
    final_cross_section_error = float("nan")
    alternative_weight_ids: tuple[str, ...] | None = None
    validity_counts = {
        f"{level}_{kind}_count": 0
        for level in ("lhe", "dressed", "reco")
        for kind in ("candidate", "projection_valid")
    }
    reconstructed_count = 0
    source_id_digest = _source_id_sequence_hasher()
    seen_source_event_ids: set[int] = set()
    source_event_id_min = MAX_SOURCE_EVENT_ID
    source_event_id_max = 0

    lock_descriptor, lock_path = _acquire_output_lock(output_path)
    temporary: Path | None = None
    try:
        temporary = _temporary_output(output_path, overwrite)
        lhe_records = iter(iter_lhe_level_records(lhe_path))
        with uproot.open(delphes_path) as input_file:
            if delphes_tree_name not in input_file:
                raise KeyError(
                    f"{delphes_path} does not contain tree {delphes_tree_name}"
                )
            input_tree = input_file[delphes_tree_name]
            missing = sorted(
                set(INPUT_BRANCHES).difference(available_branch_names(input_tree))
            )
            if missing:
                raise KeyError(
                    f"{delphes_path} is missing required branches: "
                    + ", ".join(missing)
                )
            if int(input_tree.num_entries) != expected_metadata_events:
                raise MatchError(
                    "event-count mismatch: Delphes entries disagree with provenance: "
                    f"tree={input_tree.num_entries}, metadata={expected_metadata_events}"
                )

            with uproot.recreate(temporary) as output_file:
                output_file.mktree(
                    TREE_NAME,
                    schema,
                    title="Matched off-shell four-lepton event tree",
                )
                output_tree = output_file[TREE_NAME]
                weight_tree = None

                for arrays in input_tree.iterate(
                    expressions=INPUT_BRANCHES,
                    step_size=step_size,
                    library="ak",
                    how=dict,
                ):
                    rows = {name: ak.to_list(array) for name, array in arrays.items()}
                    chunk_size = len(rows["Event.Number"])
                    output_records: list[dict[str, object]] = []
                    output_weight_records: list[dict[str, object]] = []
                    for local_event in range(chunk_size):
                        ordinal = event_count
                        try:
                            lhe_record = next(lhe_records)
                        except StopIteration as exc:
                            raise MatchError(
                                "event-count mismatch: Delphes contains more events "
                                f"than LHE (first excess Delphes entry {ordinal})"
                            ) from exc

                        number = _event_number(rows, local_event)
                        if event_number_start is None:
                            event_number_start = number
                            if event_number_start != expected_first_event:
                                raise MatchError(
                                    "Delphes first Event.Number does not match generation "
                                    f"metadata: tree={event_number_start}, "
                                    f"generation={expected_first_event}"
                                )
                        expected_number = event_number_start + ordinal
                        if number != expected_number:
                            raise MatchError(
                                "Delphes Event.Number is not a unique contiguous "
                                f"unit-step sequence: entry {ordinal} has {number}, "
                                f"expected {expected_number}"
                            )
                        if int(lhe_record["lhe_event_index"]) != ordinal:
                            raise RuntimeError(
                                "LHE iterator did not preserve source ordinal"
                            )

                        delphes_weight_vector = _row_values(
                            rows, "Weight.Weight", local_event
                        )
                        if len(delphes_weight_vector) != provenance.hepmc_weight_count:
                            raise MatchError(
                                "Delphes Weight.Weight length differs from the aligned "
                                f"HepMC weight-name schema at entry {ordinal}: "
                                f"found {len(delphes_weight_vector)}, expected "
                                f"{provenance.hepmc_weight_count}"
                            )
                        source_event_id_lhe = int(lhe_record["source_event_id"])
                        source_event_id_delphes = decode_source_event_id(
                            delphes_weight_vector[provenance.marker_id_weight_index],
                            delphes_weight_vector[provenance.marker_unit_weight_index],
                            label=f"Delphes entry {ordinal}",
                        )
                        if source_event_id_lhe != source_event_id_delphes:
                            raise MatchError(
                                "source-event ID mismatch at matched ordinal "
                                f"{ordinal}: LHE={source_event_id_lhe}, "
                                f"Delphes={source_event_id_delphes}"
                            )
                        if source_event_id_lhe in seen_source_event_ids:
                            raise MatchError(
                                f"duplicate source-event ID {source_event_id_lhe} "
                                f"at matched ordinal {ordinal}"
                            )
                        if source_event_id_lhe <= source_event_id_max:
                            raise MatchError(
                                "source-event IDs are not strictly increasing at "
                                f"matched ordinal {ordinal}: previous="
                                f"{source_event_id_max}, current={source_event_id_lhe}"
                            )
                        seen_source_event_ids.add(source_event_id_lhe)
                        _update_source_id_sequence(
                            source_id_digest, source_event_id_lhe
                        )
                        source_event_id_min = min(
                            source_event_id_min, source_event_id_lhe
                        )
                        source_event_id_max = max(
                            source_event_id_max, source_event_id_lhe
                        )

                        uid_hi, uid_lo = event_uid(
                            campaign_id,
                            sample_code,
                            job_id,
                            source_event_id_lhe,
                        )
                        record: dict[str, object] = {
                            "campaign_id": np.uint64(campaign_id),
                            "sample_code": np.uint8(sample_code),
                            "job_id": np.uint32(job_id),
                            "event_uid_hi": np.uint64(uid_hi),
                            "event_uid_lo": np.uint64(uid_lo),
                            "hepmc_event_number": np.int64(number),
                            "delphes_event_number": np.int64(number),
                            "hepmc_entry": np.uint64(ordinal),
                            "delphes_entry": np.uint64(ordinal),
                            "has_hepmc": True,
                            "has_delphes": True,
                            "weight_delphes": np.float64(
                                _event_float(rows, "Event.Weight", local_event)
                            ),
                            "cross_section_pb_delphes": np.float64(
                                _event_float(rows, "Event.CrossSection", local_event)
                            ),
                            "cross_section_error_pb_delphes": np.float64(
                                _event_float(
                                    rows, "Event.CrossSectionError", local_event
                                )
                            ),
                        }
                        record.update(lhe_record)
                        record.update(_dressed_record(rows, local_event))
                        record.update(_reco_record(rows, local_event))
                        alternative_weights = record.pop("_lhe_alternative_weights")
                        if not isinstance(alternative_weights, Mapping):
                            raise TypeError(
                                "internal LHE weight record is not a mapping"
                            )
                        current_weight_ids = tuple(
                            sorted(str(key) for key in alternative_weights)
                        )
                        if alternative_weight_ids is None:
                            alternative_weight_ids = current_weight_ids
                            if alternative_weight_ids:
                                output_file.mktree(
                                    "LHEWeights",
                                    _weight_tree_schema(len(alternative_weight_ids)),
                                    title="Alternative LHE weights in metadata ID order",
                                )
                                weight_tree = output_file["LHEWeights"]
                        elif current_weight_ids != alternative_weight_ids:
                            raise MatchError(
                                "alternative LHE weight-ID schema changes at event "
                                f"{ordinal}: expected {list(alternative_weight_ids)}, "
                                f"found {list(current_weight_ids)}"
                            )
                        if alternative_weight_ids:
                            output_weight_records.append(
                                {
                                    "campaign_id": record["campaign_id"],
                                    "sample_code": record["sample_code"],
                                    "job_id": record["job_id"],
                                    "lhe_event_index": record["lhe_event_index"],
                                    "source_event_id": record["source_event_id"],
                                    "event_uid_hi": record["event_uid_hi"],
                                    "event_uid_lo": record["event_uid_lo"],
                                    "values": [
                                        float(alternative_weights[weight_id])
                                        for weight_id in alternative_weight_ids
                                    ],
                                }
                            )
                        output_records.append(record)

                        weight = float(record["weight_lhe"])
                        sumw.add(weight)
                        sumw2.add(weight * weight)
                        sumabsw.add(abs(weight))
                        positive_weights += int(weight > 0.0)
                        negative_weights += int(weight < 0.0)
                        zero_weights += int(weight == 0.0)
                        delphes_weight = float(record["weight_delphes"])
                        cross_section = float(record["cross_section_pb_delphes"])
                        cross_section_error = float(
                            record["cross_section_error_pb_delphes"]
                        )
                        if not all(
                            math.isfinite(value)
                            for value in (
                                delphes_weight,
                                cross_section,
                                cross_section_error,
                            )
                        ):
                            raise ValueError(
                                "Delphes Event.Weight/CrossSection diagnostics must "
                                f"be finite (entry {ordinal})"
                            )
                        sumw_delphes.add(delphes_weight)
                        sumw2_delphes.add(delphes_weight * delphes_weight)
                        sumabsw_delphes.add(abs(delphes_weight))
                        if ordinal == 0:
                            first_cross_section = cross_section
                            first_cross_section_error = cross_section_error
                        min_cross_section = min(min_cross_section, cross_section)
                        max_cross_section = max(max_cross_section, cross_section)
                        final_cross_section = cross_section
                        min_cross_section_error = min(
                            min_cross_section_error, cross_section_error
                        )
                        max_cross_section_error = max(
                            max_cross_section_error, cross_section_error
                        )
                        final_cross_section_error = cross_section_error
                        for level in ("lhe", "dressed", "reco"):
                            validity_counts[f"{level}_candidate_count"] += int(
                                bool(record[f"{level}_candidate"])
                            )
                            validity_counts[f"{level}_projection_valid_count"] += int(
                                bool(record[f"{level}_projection_valid"])
                            )
                        reconstructed_count += int(bool(record["reconstructed"]))
                        event_count += 1

                    if output_records:
                        output_tree.extend(_records_to_arrays(output_records, schema))
                    if output_weight_records:
                        if weight_tree is None or alternative_weight_ids is None:
                            raise RuntimeError(
                                "alternative-weight tree was not initialized"
                            )
                        weight_tree.extend(
                            _weight_records_to_arrays(
                                output_weight_records, len(alternative_weight_ids)
                            )
                        )

                extra_lhe_events = sum(1 for _ in lhe_records)
                if extra_lhe_events:
                    raise MatchError(
                        "event-count mismatch: LHE contains "
                        f"{event_count + extra_lhe_events} events but Delphes contains "
                        f"{event_count}"
                    )
                if event_count == 0:
                    raise MatchError("matched inputs contain no events")
                if event_count != expected_metadata_events:
                    raise MatchError(
                        "matched event-count mismatch with provenance: "
                        f"processed={event_count}, metadata={expected_metadata_events}"
                    )
                actual_source_id_sequence_sha = source_id_digest.hexdigest()
                if (
                    actual_source_id_sequence_sha
                    != provenance.source_id_sequence_sha256
                ):
                    raise MatchError(
                        "source-event ID sequence SHA-256 mismatch: "
                        f"decoded={actual_source_id_sequence_sha}, "
                        f"alignment={provenance.source_id_sequence_sha256}"
                    )
                for required_level in ("lhe", "dressed"):
                    if validity_counts[f"{required_level}_candidate_count"] == 0:
                        raise MatchError(
                            f"no {required_level} event has a valid direct-2e2mu topology"
                        )
                    if validity_counts[f"{required_level}_projection_valid_count"] == 0:
                        raise MatchError(
                            f"no {required_level} event has a valid Born projection"
                        )
                if alternative_weight_ids and (
                    weight_tree is None or weight_tree.num_entries != event_count
                ):
                    raise RuntimeError(
                        "alternative-weight tree is not one-to-one with Events"
                    )

                athgen_release = _release_triplet(
                    provenance.generation["athgeneration_release"],
                    "AthGeneration release",
                )
                signed_efficiency_value = provenance.lhe_contract[
                    "signed_filter_efficiency"
                ]
                phase_space_signed_efficiency = (
                    float(signed_efficiency_value)
                    if signed_efficiency_value is not None
                    else math.nan
                )
                phase_space_count_efficiency = float(
                    provenance.lhe_contract["count_filter_efficiency"]
                )
                absolute_efficiency_value = provenance.lhe_contract[
                    "absolute_filter_efficiency"
                ]
                phase_space_absolute_efficiency = (
                    float(absolute_efficiency_value)
                    if absolute_efficiency_value is not None
                    else math.nan
                )
                inclusive_lhe_cross_section = float(
                    provenance.lhe_contract["inclusive_cross_section_pb"]
                )
                inclusive_lhe_cross_section_error_value = provenance.lhe_contract[
                    "inclusive_cross_section_mc_error_pb"
                ]
                inclusive_lhe_cross_section_mc_error = (
                    float(inclusive_lhe_cross_section_error_value)
                    if inclusive_lhe_cross_section_error_value is not None
                    else math.nan
                )
                effective_filtered_cross_section = float(
                    provenance.lhe_contract["filtered_cross_section_pb"]
                )
                effective_filtered_cross_section_error_value = provenance.lhe_contract[
                    "filtered_cross_section_mc_error_pb"
                ]
                effective_filtered_cross_section_error = (
                    float(effective_filtered_cross_section_error_value)
                    if effective_filtered_cross_section_error_value is not None
                    else math.nan
                )

                run_schema = {
                    "schema_version": np.dtype("uint16"),
                    "campaign_id": np.dtype("uint64"),
                    "sample_code": np.dtype("uint8"),
                    "job_id": np.dtype("uint32"),
                    "event_count": np.dtype("uint64"),
                    "event_number_start": np.dtype("int64"),
                    "source_event_id_min": np.dtype("uint64"),
                    "source_event_id_max": np.dtype("uint64"),
                    "generation_seed": np.dtype("uint32"),
                    "delphes_seed": np.dtype("uint32"),
                    "run_number": np.dtype("uint32"),
                    "ecm_energy_gev": np.dtype("float64"),
                    "athgeneration_release_major": np.dtype("uint16"),
                    "athgeneration_release_minor": np.dtype("uint16"),
                    "athgeneration_release_patch": np.dtype("uint16"),
                    "alignment_contract_code": np.dtype("uint8"),
                    "positive_weight_count": np.dtype("uint64"),
                    "negative_weight_count": np.dtype("uint64"),
                    "zero_weight_count": np.dtype("uint64"),
                    "sumw": np.dtype("float64"),
                    "sumw2": np.dtype("float64"),
                    "sumabsw": np.dtype("float64"),
                    "sumw_delphes": np.dtype("float64"),
                    "sumw2_delphes": np.dtype("float64"),
                    "sumabsw_delphes": np.dtype("float64"),
                    "cross_section_first_pb_delphes": np.dtype("float64"),
                    "cross_section_min_pb_delphes": np.dtype("float64"),
                    "cross_section_max_pb_delphes": np.dtype("float64"),
                    "cross_section_final_pb_delphes": np.dtype("float64"),
                    "cross_section_error_first_pb_delphes": np.dtype("float64"),
                    "cross_section_error_min_pb_delphes": np.dtype("float64"),
                    "cross_section_error_max_pb_delphes": np.dtype("float64"),
                    "cross_section_error_final_pb_delphes": np.dtype("float64"),
                    "phase_space_signed_efficiency": np.dtype("float64"),
                    "phase_space_absolute_efficiency": np.dtype("float64"),
                    "phase_space_count_efficiency": np.dtype("float64"),
                    "normalization_generated_lhe_events": np.dtype("uint64"),
                    "normalization_accepted_lhe_events": np.dtype("uint64"),
                    "normalization_sumw_generated_pb": np.dtype("float64"),
                    "normalization_sumw2_generated_pb2": np.dtype("float64"),
                    "normalization_sumw_accepted_pb": np.dtype("float64"),
                    "normalization_sumw2_accepted_pb2": np.dtype("float64"),
                    "inclusive_lhe_cross_section_pb": np.dtype("float64"),
                    "inclusive_lhe_cross_section_mc_error_pb": np.dtype("float64"),
                    "effective_filtered_cross_section_pb": np.dtype("float64"),
                    "effective_filtered_cross_section_mc_error_pb": np.dtype(
                        "float64"
                    ),
                    **{name: np.dtype("uint64") for name in validity_counts},
                    "reconstructed_count": np.dtype("uint64"),
                }
                output_file.mktree(
                    RUN_TREE_NAME, run_schema, title="Job-level provenance and sums"
                )
                output_file[RUN_TREE_NAME].extend(
                    {
                        "schema_version": np.asarray([SCHEMA_VERSION], dtype=np.uint16),
                        "campaign_id": np.asarray([campaign_id], dtype=np.uint64),
                        "sample_code": np.asarray([sample_code], dtype=np.uint8),
                        "job_id": np.asarray([job_id], dtype=np.uint32),
                        "event_count": np.asarray([event_count], dtype=np.uint64),
                        "event_number_start": np.asarray(
                            [event_number_start], dtype=np.int64
                        ),
                        "source_event_id_min": np.asarray(
                            [source_event_id_min], dtype=np.uint64
                        ),
                        "source_event_id_max": np.asarray(
                            [source_event_id_max], dtype=np.uint64
                        ),
                        "generation_seed": np.asarray(
                            [provenance.generation["seed"]], dtype=np.uint32
                        ),
                        "delphes_seed": np.asarray(
                            [provenance.simulation["random_seed"]], dtype=np.uint32
                        ),
                        "run_number": np.asarray(
                            [provenance.generation["run_number"]], dtype=np.uint32
                        ),
                        "ecm_energy_gev": np.asarray(
                            [provenance.generation["ecm_energy_gev"]],
                            dtype=np.float64,
                        ),
                        "athgeneration_release_major": np.asarray(
                            [athgen_release[0]], dtype=np.uint16
                        ),
                        "athgeneration_release_minor": np.asarray(
                            [athgen_release[1]], dtype=np.uint16
                        ),
                        "athgeneration_release_patch": np.asarray(
                            [athgen_release[2]], dtype=np.uint16
                        ),
                        "alignment_contract_code": np.asarray([2], dtype=np.uint8),
                        "positive_weight_count": np.asarray(
                            [positive_weights], dtype=np.uint64
                        ),
                        "negative_weight_count": np.asarray(
                            [negative_weights], dtype=np.uint64
                        ),
                        "zero_weight_count": np.asarray(
                            [zero_weights], dtype=np.uint64
                        ),
                        "sumw": np.asarray([sumw.total], dtype=np.float64),
                        "sumw2": np.asarray([sumw2.total], dtype=np.float64),
                        "sumabsw": np.asarray([sumabsw.total], dtype=np.float64),
                        "sumw_delphes": np.asarray(
                            [sumw_delphes.total], dtype=np.float64
                        ),
                        "sumw2_delphes": np.asarray(
                            [sumw2_delphes.total], dtype=np.float64
                        ),
                        "sumabsw_delphes": np.asarray(
                            [sumabsw_delphes.total], dtype=np.float64
                        ),
                        "cross_section_first_pb_delphes": np.asarray(
                            [first_cross_section], dtype=np.float64
                        ),
                        "cross_section_min_pb_delphes": np.asarray(
                            [min_cross_section], dtype=np.float64
                        ),
                        "cross_section_max_pb_delphes": np.asarray(
                            [max_cross_section], dtype=np.float64
                        ),
                        "cross_section_final_pb_delphes": np.asarray(
                            [final_cross_section], dtype=np.float64
                        ),
                        "cross_section_error_first_pb_delphes": np.asarray(
                            [first_cross_section_error], dtype=np.float64
                        ),
                        "cross_section_error_min_pb_delphes": np.asarray(
                            [min_cross_section_error], dtype=np.float64
                        ),
                        "cross_section_error_max_pb_delphes": np.asarray(
                            [max_cross_section_error], dtype=np.float64
                        ),
                        "cross_section_error_final_pb_delphes": np.asarray(
                            [final_cross_section_error], dtype=np.float64
                        ),
                        "phase_space_signed_efficiency": np.asarray(
                            [phase_space_signed_efficiency], dtype=np.float64
                        ),
                        "phase_space_absolute_efficiency": np.asarray(
                            [phase_space_absolute_efficiency], dtype=np.float64
                        ),
                        "phase_space_count_efficiency": np.asarray(
                            [phase_space_count_efficiency], dtype=np.float64
                        ),
                        "normalization_generated_lhe_events": np.asarray(
                            [provenance.lhe_contract["generated_lhe_events"]],
                            dtype=np.uint64,
                        ),
                        "normalization_accepted_lhe_events": np.asarray(
                            [provenance.lhe_contract["accepted_lhe_events"]],
                            dtype=np.uint64,
                        ),
                        "normalization_sumw_generated_pb": np.asarray(
                            [provenance.lhe_contract["sumw_generated"]],
                            dtype=np.float64,
                        ),
                        "normalization_sumw2_generated_pb2": np.asarray(
                            [provenance.lhe_contract["sumw2_generated"]],
                            dtype=np.float64,
                        ),
                        "normalization_sumw_accepted_pb": np.asarray(
                            [provenance.lhe_contract["sumw_accepted"]],
                            dtype=np.float64,
                        ),
                        "normalization_sumw2_accepted_pb2": np.asarray(
                            [provenance.lhe_contract["sumw2_accepted"]],
                            dtype=np.float64,
                        ),
                        "inclusive_lhe_cross_section_pb": np.asarray(
                            [inclusive_lhe_cross_section], dtype=np.float64
                        ),
                        "inclusive_lhe_cross_section_mc_error_pb": np.asarray(
                            [inclusive_lhe_cross_section_mc_error], dtype=np.float64
                        ),
                        "effective_filtered_cross_section_pb": np.asarray(
                            [effective_filtered_cross_section], dtype=np.float64
                        ),
                        "effective_filtered_cross_section_mc_error_pb": np.asarray(
                            [effective_filtered_cross_section_error],
                            dtype=np.float64,
                        ),
                        **{
                            name: np.asarray([value], dtype=np.uint64)
                            for name, value in validity_counts.items()
                        },
                        "reconstructed_count": np.asarray(
                            [reconstructed_count], dtype=np.uint64
                        ),
                    }
                )
                output_file["analysis_metadata"] = json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "sample": sample,
                        "sample_code": sample_code,
                        "uid_schema_tag": UID_SCHEMA_TAG.rstrip(b"\0").decode("ascii"),
                        "matching": "named_weight_ratio_v1_with_ordinal_cross_checks",
                        "delphes_tree": delphes_tree_name,
                        "analysis_code": analysis_code,
                        "provenance": {
                            "generation": provenance.generation,
                            "lhe_contract": provenance.lhe_contract,
                            "alignment": provenance.alignment,
                            "simulation": provenance.simulation,
                            "files": provenance.files,
                        },
                        "lhe_alternative_weights": {
                            "tree": "LHEWeights" if alternative_weight_ids else None,
                            "ids": list(alternative_weight_ids or ()),
                            "ordering": "lexicographic_weight_id",
                            "one_row_per_event": bool(alternative_weight_ids),
                            "technical_weights_excluded": [
                                MARKER_ID_WEIGHT_NAME,
                                MARKER_UNIT_WEIGHT_NAME,
                            ],
                        },
                        "source_event_id": {
                            "encoding": (
                                "AUX_OAP_EVENT_ID/AUX_OAP_EVENT_UNIT, "
                                "rounded within bounded floating tolerance"
                            ),
                            "sequence_sha256_encoding": (
                                "concatenated positive uint64 big-endian words, "
                                "8 bytes per event, no delimiter"
                            ),
                            "sequence_sha256": actual_source_id_sequence_sha,
                        },
                    },
                    sort_keys=True,
                )

        assert temporary is not None
        _publish_output(temporary, output_path, overwrite)
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise
    finally:
        _release_output_lock(lock_descriptor, lock_path)

    return {
        "event_count": event_count,
        "event_number_start": int(event_number_start),
        "positive_weight_count": positive_weights,
        "negative_weight_count": negative_weights,
        "zero_weight_count": zero_weights,
        "sumw": sumw.total,
        "sumw2": sumw2.total,
        "sumabsw": sumabsw.total,
    }


def _unsigned_argument(name: str, bits: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        try:
            _require_unsigned(name, parsed, bits)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
        return parsed

    return parse


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lhe", type=Path, help="matched, retained LHE or LHE.GZ file")
    parser.add_argument("delphes", type=Path, help="Delphes ROOT file")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--sample", choices=tuple(SAMPLE_CODES), required=True)
    parser.add_argument(
        "--job-id", type=_unsigned_argument("job_id", 32), required=True
    )
    parser.add_argument(
        "--generation-metadata",
        type=Path,
        required=True,
        help="Generation run-metadata.txt",
    )
    parser.add_argument(
        "--alignment-metadata",
        type=Path,
        required=True,
        help="Generation alignment-metadata.json",
    )
    parser.add_argument(
        "--lhe-contract-metadata",
        type=Path,
        required=True,
        help="Generation lhe-contract-metadata.json",
    )
    parser.add_argument(
        "--simulation-metadata",
        type=Path,
        required=True,
        help="Simulation simulation-metadata.txt",
    )
    parser.add_argument(
        "--campaign-id",
        type=_unsigned_argument("campaign_id", 64),
        default=0,
        help="unsigned campaign identity (default: 0)",
    )
    parser.add_argument("--delphes-tree-name", default="Delphes")
    parser.add_argument("--step-size", default="50 MB", help="uproot input chunk size")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    summary = build_analysis_tree(
        args.lhe,
        args.delphes,
        args.output,
        sample=args.sample,
        job_id=args.job_id,
        generation_metadata_path=args.generation_metadata,
        lhe_contract_metadata_path=args.lhe_contract_metadata,
        alignment_metadata_path=args.alignment_metadata,
        simulation_metadata_path=args.simulation_metadata,
        campaign_id=args.campaign_id,
        delphes_tree_name=args.delphes_tree_name,
        step_size=args.step_size,
        overwrite=args.overwrite,
    )
    print(
        f"Wrote {summary['event_count']} matched events to "
        f"{args.output.expanduser().resolve()} "
        f"(sumw={summary['sumw']:.12g}, "
        f"negative={summary['negative_weight_count']})"
    )


if __name__ == "__main__":
    main()
