#!/usr/bin/env python3
"""Merge validated job-level analysis ROOT files and add truth-basis weights.

The job-level ``weight_lhe`` branch is immutable.  A common additional scale
closes the retained merged sample to the authoritative cross section obtained
by pooling the pre-shower IDWTUP=-4 normalization primitives in ``Runs``.
Truth-component weights are evaluated from the LHE Born-projected helicity
angles and multiply this separately stored merged nominal weight.
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import uproot

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"
for import_root in (SOURCE_ROOT, REPO_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from Analysis.build_analysis_tree import (  # noqa: E402
    ATHGENERATION_BACKEND,
    SCHEMA_VERSION as ANALYSIS_SCHEMA_VERSION,
    SAMPLE_CODES,
    STANDALONE_GENERATOR_BACKEND,
    event_uid,
    output_schema,
)
from offshell_production.harmonics import (  # noqa: E402
    TRUTH_ANGULAR_COMPONENTS,
    truth_angular_factors,
)

MERGE_SCHEMA_VERSION = 1
EVENT_TREE_NAME = "Events"
RUN_TREE_NAME = "Runs"
LHE_WEIGHT_TREE_NAME = "LHEWeights"
MERGE_SUMMARY_TREE_NAME = "MergeSummary"
ANALYSIS_METADATA_NAME = "analysis_metadata"
MERGE_METADATA_NAME = "merge_metadata"
NORMALIZATION_CONTRACT = "idwtup-minus4-sample-mean-v1"
NOMINAL_WEIGHT_UNITS = "pb"
EXPECTED_TRUTH_SLUGS = ("00_20", "20_20", "2m1_2p1", "2m2_2p2")


BASE_RUN_SCHEMA: dict[str, np.dtype] = {
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
    "effective_filtered_cross_section_mc_error_pb": np.dtype("float64"),
    "lhe_candidate_count": np.dtype("uint64"),
    "lhe_projection_valid_count": np.dtype("uint64"),
    "dressed_candidate_count": np.dtype("uint64"),
    "dressed_projection_valid_count": np.dtype("uint64"),
    "reco_candidate_count": np.dtype("uint64"),
    "reco_projection_valid_count": np.dtype("uint64"),
    "reconstructed_count": np.dtype("uint64"),
}

IDENTITY_BRANCHES = (
    "campaign_id",
    "sample_code",
    "job_id",
    "lhe_event_index",
    "source_event_id",
    "event_uid_hi",
    "event_uid_lo",
)

VALIDITY_COUNT_BRANCHES = tuple(
    f"{level}_{kind}_count"
    for level in ("lhe", "dressed", "reco")
    for kind in ("candidate", "projection_valid")
)

PREFLIGHT_EVENT_BRANCHES = (
    *IDENTITY_BRANCHES,
    "hepmc_event_number",
    "delphes_event_number",
    "hepmc_entry",
    "delphes_entry",
    "weight_lhe",
    "weight_delphes",
    "cross_section_pb_delphes",
    "cross_section_error_pb_delphes",
    "lhe_n_alternative_weights",
    "has_lhe",
    "has_hepmc",
    "has_delphes",
    "lhe_candidate",
    "lhe_projection_valid",
    "dressed_candidate",
    "dressed_projection_valid",
    "reco_candidate",
    "reco_projection_valid",
    "reco_pass_selection",
    "reconstructed",
    "lhe_theta1",
    "lhe_phi1",
    "lhe_theta2",
    "lhe_phi2",
)

PHYSICS_INVARIANT_PATHS = (
    ("schema_version",),
    ("sample",),
    ("sample_code",),
    ("uid_schema_tag",),
    ("matching",),
    ("delphes_tree",),
    ("analysis_code",),
    ("provenance", "generation", "schema_version"),
    ("provenance", "generation", "process"),
    ("provenance", "generation", "run_number"),
    ("provenance", "generation", "ecm_energy_gev"),
    ("provenance", "generation", "generator_backend"),
    ("provenance", "generation", "generator_mll_min_gev"),
    ("provenance", "generation", "generator_m4l_min_gev"),
    ("provenance", "generation", "generator_m4l_max_gev"),
    ("provenance", "generation", "analysis_mz_min_gev"),
    ("provenance", "generation", "analysis_mz_max_gev"),
    ("provenance", "generation", "analysis_m4l_min_gev"),
    ("provenance", "generation", "analysis_m4l_max_gev"),
    (
        "provenance",
        "generation",
        "target_generation_phase_space_m4l_max_gev",
    ),
    ("provenance", "generation", "alignment_contract"),
    ("provenance", "generation", "lhe_event_id_contract"),
    ("provenance", "lhe_contract", "schema_version"),
    ("provenance", "lhe_contract", "contract"),
    ("provenance", "lhe_contract", "normalization_contract"),
    ("provenance", "lhe_contract", "nominal_weight_units"),
    ("provenance", "lhe_contract", "lhe_weighting_strategy"),
    ("provenance", "lhe_contract", "cross_section_method"),
    ("provenance", "lhe_contract", "process"),
    ("provenance", "lhe_contract", "marker_id_weight"),
    ("provenance", "lhe_contract", "marker_unit_weight"),
    ("provenance", "lhe_contract", "m4l_min_gev"),
    ("provenance", "lhe_contract", "m4l_max_gev"),
    ("provenance", "lhe_contract", "lhe_init", "idwtup"),
    ("provenance", "alignment", "schema_version"),
    ("provenance", "alignment", "contract"),
    ("provenance", "alignment", "process"),
    ("provenance", "alignment", "run_number"),
    ("provenance", "alignment", "hepmc_precision_contract"),
    ("provenance", "alignment", "contract_conditions"),
    ("provenance", "simulation", "schema_version"),
    ("provenance", "simulation", "process"),
    ("provenance", "simulation", "weight_scale"),
    ("provenance", "simulation", "weight_scale_policy"),
    ("provenance", "simulation", "weight_branches_preserved"),
    ("provenance", "simulation", "cross_section_semantics"),
    ("provenance", "simulation", "delphes_version"),
    ("provenance", "simulation", "delphes_commit"),
    ("provenance", "simulation", "card_sha256"),
    ("lhe_alternative_weights", "ids"),
    ("lhe_alternative_weights", "ordering"),
    ("lhe_alternative_weights", "technical_weights_excluded"),
)

OPTIONAL_PHYSICS_INVARIANT_PATHS = (
    # Backend-specific generation/alignment records.  Presence itself is part
    # of the fingerprint, so a campaign cannot mix AthGeneration and the
    # standalone MadGraph/Pythia VPolar backend.
    ("provenance", "generation", "athgeneration_release"),
    ("provenance", "generation", "atlas_project"),
    ("provenance", "generation", "atlas_version"),
    ("provenance", "generation", "job_option_sha256"),
    ("provenance", "generation", "athgeneration_release_applicable"),
    ("provenance", "generation", "generator_mll_max_gev"),
    ("provenance", "generation", "final_state"),
    ("provenance", "generation", "full_amplitude"),
    ("provenance", "generation", "photon_diagrams"),
    ("provenance", "generation", "polarization_component"),
    ("provenance", "generation", "polarization_z1_decay"),
    ("provenance", "generation", "polarization_z2_decay"),
    ("provenance", "generation", "polarization_frame"),
    ("provenance", "generation", "madgraph_me_frame"),
    ("provenance", "generation", "mixed_polarization_interference"),
    ("provenance", "generation", "mixed_sample_definition"),
    ("provenance", "generation", "madgraph_version"),
    ("provenance", "generation", "pythia_version"),
    ("provenance", "generation", "hepmc_version"),
    ("provenance", "generation", "ufo_version"),
    ("provenance", "generation", "ufo_sha256"),
    ("provenance", "generation", "loop_filter_sha256"),
    ("provenance", "generation", "loop_filter_patch_sha256"),
    ("provenance", "generation", "installation_manifest_sha256"),
    ("provenance", "generation", "process_card_sha256"),
    ("provenance", "generation", "madloop_card_sha256"),
    ("provenance", "generation", "param_card_sha256"),
    ("provenance", "generation", "loop_reduction_backend"),
    ("provenance", "generation", "loop_optimized_output"),
    ("provenance", "generation", "madloop_reduction_lib"),
    ("provenance", "generation", "ninja_enabled"),
    ("provenance", "generation", "collier_enabled"),
    ("provenance", "generation", "loop_output_dependencies"),
    ("provenance", "generation", "pdf_set"),
    ("provenance", "generation", "pdf_id"),
    ("provenance", "generation", "shower_profile"),
    ("provenance", "generation", "pythia_tune_pp"),
    ("provenance", "generation", "pythia_pdf_pset"),
    ("provenance", "generation", "run_generation_sha256"),
    ("provenance", "generation", "lhe_contract_script_sha256"),
    ("provenance", "generation", "alignment_script_sha256"),
    ("provenance", "alignment", "athgeneration_release"),
    ("provenance", "alignment", "job_option_sha256"),
    ("provenance", "alignment", "generator_backend"),
    ("provenance", "simulation", "hepmc_format"),
    ("provenance", "simulation", "cross_section_fields_preserved"),
    ("provenance", "simulation", "event_retention_validated"),
    ("provenance", "simulation", "event_order_preserved"),
    ("provenance", "simulation", "event_number_branch"),
    ("provenance", "simulation", "dressed_particles"),
    ("provenance", "simulation", "dressed_lepton_origin"),
    ("provenance", "simulation", "dressed_lepton_origin_policy"),
    (
        "provenance",
        "simulation",
        "dressed_lepton_direct_hard_process_candidates",
    ),
    ("provenance", "simulation", "dressed_lepton_exact_2e2mu_validated"),
    ("provenance", "simulation", "dressed_lepton_tau_decay_chains"),
    ("provenance", "simulation", "dressed_lepton_photons"),
    ("provenance", "simulation", "reco_leptons"),
    ("provenance", "simulation", "reco_leptons_before_isolation"),
    ("provenance", "simulation", "reco_efficiency_model"),
    ("provenance", "simulation", "reco_isolation_model"),
    ("provenance", "simulation", "response_buffer"),
    ("provenance", "simulation", "jet_model"),
    ("provenance", "simulation", "reconstruction_marker"),
    ("provenance", "simulation", "delphes_version_manifest_sha256"),
    ("provenance", "simulation", "delphes_patched_diff_sha256"),
    ("provenance", "simulation", "delphes_hepmc2_sha256"),
    ("provenance", "simulation", "delphes_hepmc3_sha256"),
    ("provenance", "simulation", "delphes_library_sha256"),
    ("provenance", "simulation", "active_root_version"),
    ("provenance", "simulation", "card_policy"),
    ("provenance", "simulation", "run_simulation_sha256"),
    ("provenance", "simulation", "card_builder_sha256"),
    ("provenance", "simulation", "check_delphes_output_sha256"),
)


class MergeError(ValueError):
    """Raised when job outputs cannot be merged without changing semantics."""


class SchemaError(MergeError):
    """Raised when an input does not have the exact job-level ROOT schema."""


class ProvenanceError(MergeError):
    """Raised when source metadata are missing, inconsistent, or incompatible."""


class _CompensatedSum:
    def __init__(self) -> None:
        self.total = 0.0
        self.correction = 0.0

    def add(self, value: float) -> None:
        adjusted = value - self.correction
        updated = self.total + adjusted
        self.correction = (updated - self.total) - adjusted
        self.total = updated

    def add_array(self, values: np.ndarray) -> None:
        for value in np.ravel(values):
            self.add(float(value))


@dataclass(frozen=True)
class JobInspection:
    path: Path
    sha256: str
    metadata: dict[str, Any]
    run_arrays: dict[str, np.ndarray]
    run: dict[str, int | float]
    event_count: int
    campaign_id: int
    sample_code: int
    job_id: int
    alternative_weight_ids: tuple[str, ...]
    alternative_values_sha256: str | None
    positive_weight_count: int
    negative_weight_count: int
    zero_weight_count: int
    sumw: float
    sumw2: float
    sumabsw: float
    validity_counts: dict[str, int]
    reconstructed_count: int
    truth_lhe_valid_count: int
    normalization_sumabsw_generated_pb: float
    normalization_sumabsw_accepted_pb: float


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dtype_signature(dtype: object) -> tuple[str, int, tuple[int, ...]]:
    parsed = np.dtype(dtype)
    return parsed.base.kind, parsed.base.itemsize, parsed.shape


def _tree_schema(tree: Any) -> dict[str, np.dtype]:
    schema: dict[str, np.dtype] = {}
    for name in tree.keys():
        interpretation = tree[name].interpretation
        dtype = getattr(interpretation, "numpy_dtype", None)
        if dtype is None:
            raise SchemaError(f"branch {name} has unsupported interpretation {interpretation}")
        schema[str(name)] = np.dtype(dtype)
    return schema


def _require_exact_schema(
    tree: Any,
    expected: Mapping[str, np.dtype],
    *,
    label: str,
) -> None:
    actual = _tree_schema(tree)
    if tuple(actual) != tuple(expected):
        missing = sorted(set(expected).difference(actual))
        extra = sorted(set(actual).difference(expected))
        raise SchemaError(
            f"{label} branch sequence differs from the required schema; "
            f"missing={missing}, extra={extra}"
        )
    mismatches = {
        name: (str(actual[name]), str(expected[name]))
        for name in expected
        if _dtype_signature(actual[name]) != _dtype_signature(expected[name])
    }
    if mismatches:
        raise SchemaError(f"{label} branch dtypes differ: {mismatches}")


def _nested(mapping: Mapping[str, Any], path: Sequence[str]) -> Any:
    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            raise ProvenanceError("analysis metadata is missing " + ".".join(path))
        value = value[key]
    return value


def _physics_fingerprint(metadata: Mapping[str, Any]) -> dict[str, Any]:
    fingerprint = {
        ".".join(path): _nested(metadata, path)
        for path in PHYSICS_INVARIANT_PATHS
    }
    for path in OPTIONAL_PHYSICS_INVARIANT_PATHS:
        value: Any = metadata
        present = True
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                present = False
                break
            value = value[key]
        fingerprint[".".join(path)] = (
            {"present": True, "value": value}
            if present
            else {"present": False}
        )
    return fingerprint


def _read_analysis_metadata(root_file: Any, path: Path) -> dict[str, Any]:
    if ANALYSIS_METADATA_NAME not in root_file:
        raise SchemaError(f"{path} does not contain {ANALYSIS_METADATA_NAME}")
    try:
        value = json.loads(str(root_file[ANALYSIS_METADATA_NAME]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"{path} has invalid analysis_metadata JSON") from exc
    if not isinstance(value, dict):
        raise ProvenanceError(f"{path} analysis_metadata must be a JSON object")
    return value


def _scalar_run_arrays(
    tree: Any, path: Path
) -> tuple[dict[str, np.ndarray], dict[str, int | float]]:
    if int(tree.num_entries) != 1:
        raise SchemaError(f"{path}: Runs must contain exactly one job row")
    arrays = tree.arrays(library="np", how=dict)
    copied: dict[str, np.ndarray] = {}
    row: dict[str, int | float] = {}
    for name in BASE_RUN_SCHEMA:
        array = np.asarray(arrays[name])
        if array.shape != (1,):
            raise SchemaError(f"{path}: Runs.{name} is not a scalar one-row branch")
        copied[name] = array.copy()
        scalar = array[0].item()
        row[name] = int(scalar) if array.dtype.kind in "uib" else float(scalar)
    return copied, row


def _close(observed: float, expected: float, label: str) -> None:
    if not math.isclose(observed, expected, rel_tol=1.0e-11, abs_tol=1.0e-13):
        raise MergeError(f"{label} mismatch: observed={observed!r}, expected={expected!r}")


def _nominal_closure(
    observed: float,
    expected: float,
    *,
    scale: float,
    raw_sumabsw: float,
    label: str,
) -> tuple[float, float]:
    """Check a cancellation-aware closure after one common float64 scale."""

    residual = observed - expected
    roundoff_bound = (
        16.0 * np.finfo(np.float64).eps * abs(scale) * raw_sumabsw
    )
    tolerance = max(
        1.0e-11 * max(abs(expected), 1.0),
        roundoff_bound,
    )
    if not math.isfinite(observed) or abs(residual) > tolerance:
        raise MergeError(
            f"{label} mismatch: residual={residual!r}, tolerance={tolerance!r}"
        )
    return residual, tolerance


def _mean_mc_error(sumw: float, sumw2: float, count: int, label: str) -> float:
    if count < 2:
        return math.nan
    variance_numerator = sumw2 - sumw * sumw / count
    tolerance = 1.0e-12 * max(sumw2, sumw * sumw / count, 1.0)
    if variance_numerator < -tolerance:
        raise MergeError(f"{label} normalization moments have negative variance")
    return math.sqrt(max(variance_numerator, 0.0) / (count * (count - 1)))


def _validate_optional_error(observed: float, expected: float, label: str) -> None:
    if math.isnan(expected):
        if not math.isnan(observed):
            raise MergeError(f"{label} must be NaN for fewer than two generated events")
    else:
        if not math.isfinite(observed) or observed < 0.0:
            raise MergeError(f"{label} must be finite and non-negative")
        _close(observed, expected, label)


def _validate_job_normalization(
    run: Mapping[str, int | float], metadata: Mapping[str, Any], path: Path
) -> tuple[float, float]:
    contract = _nested(metadata, ("provenance", "lhe_contract"))
    if not isinstance(contract, Mapping):
        raise ProvenanceError(f"{path}: embedded LHE contract must be an object")
    if contract.get("normalization_contract") != NORMALIZATION_CONTRACT:
        raise ProvenanceError(f"{path}: unsupported normalization contract")
    if contract.get("nominal_weight_units") != NOMINAL_WEIGHT_UNITS:
        raise ProvenanceError(f"{path}: nominal LHE weights are not in pb")
    if contract.get("lhe_weighting_strategy") != -4:
        raise ProvenanceError(f"{path}: LHE weighting strategy is not IDWTUP=-4")
    lhe_init = contract.get("lhe_init")
    if not isinstance(lhe_init, Mapping) or lhe_init.get("idwtup") != -4:
        raise ProvenanceError(f"{path}: LHE init weighting strategy is not IDWTUP=-4")

    pairs = {
        "normalization_generated_lhe_events": "generated_lhe_events",
        "normalization_accepted_lhe_events": "accepted_lhe_events",
        "normalization_sumw_generated_pb": "sumw_generated",
        "normalization_sumw2_generated_pb2": "sumw2_generated",
        "normalization_sumw_accepted_pb": "sumw_accepted",
        "normalization_sumw2_accepted_pb2": "sumw2_accepted",
        "inclusive_lhe_cross_section_pb": "inclusive_cross_section_pb",
        "effective_filtered_cross_section_pb": "filtered_cross_section_pb",
    }
    for run_name, metadata_name in pairs.items():
        observed = run[run_name]
        expected = contract.get(metadata_name)
        if expected is None:
            raise ProvenanceError(f"{path}: LHE contract is missing {metadata_name}")
        if isinstance(observed, int):
            if observed != int(expected):
                raise MergeError(f"{path}: Runs.{run_name} disagrees with metadata")
        else:
            _close(float(observed), float(expected), f"{path}: Runs.{run_name}")

    generated = int(run["normalization_generated_lhe_events"])
    accepted = int(run["normalization_accepted_lhe_events"])
    if generated <= 0 or not 0 <= accepted <= generated:
        raise MergeError(f"{path}: invalid generated/accepted normalization counts")
    if accepted < int(run["event_count"]):
        raise MergeError(f"{path}: accepted LHE count is smaller than retained events")
    sumw_generated = float(run["normalization_sumw_generated_pb"])
    sumw2_generated = float(run["normalization_sumw2_generated_pb2"])
    sumw_accepted = float(run["normalization_sumw_accepted_pb"])
    sumw2_accepted = float(run["normalization_sumw2_accepted_pb2"])
    if not all(
        math.isfinite(value)
        for value in (sumw_generated, sumw2_generated, sumw_accepted, sumw2_accepted)
    ):
        raise MergeError(f"{path}: non-finite normalization primitive")
    if sumw2_generated < 0.0 or sumw2_accepted < 0.0:
        raise MergeError(f"{path}: negative squared-weight normalization sum")
    if sumw2_accepted > sumw2_generated + 1.0e-12 * max(
        sumw2_generated, 1.0
    ):
        raise MergeError(
            f"{path}: accepted squared-weight sum exceeds the generated sum"
        )
    _close(
        float(run["inclusive_lhe_cross_section_pb"]),
        sumw_generated / generated,
        f"{path}: inclusive cross section",
    )
    _close(
        float(run["effective_filtered_cross_section_pb"]),
        sumw_accepted / generated,
        f"{path}: filtered cross section",
    )
    _validate_optional_error(
        float(run["inclusive_lhe_cross_section_mc_error_pb"]),
        _mean_mc_error(sumw_generated, sumw2_generated, generated, str(path)),
        f"{path}: inclusive MC error",
    )
    _validate_optional_error(
        float(run["effective_filtered_cross_section_mc_error_pb"]),
        _mean_mc_error(sumw_accepted, sumw2_accepted, generated, str(path)),
        f"{path}: filtered MC error",
    )
    _close(
        float(run["phase_space_count_efficiency"]),
        accepted / generated,
        f"{path}: count efficiency",
    )

    sumabsw_generated = float(contract.get("sumabsw_generated", math.nan))
    sumabsw_accepted = float(contract.get("sumabsw_accepted", math.nan))
    if (
        not math.isfinite(sumabsw_generated)
        or not math.isfinite(sumabsw_accepted)
        or sumabsw_generated < 0.0
        or not 0.0 <= sumabsw_accepted <= sumabsw_generated
    ):
        raise MergeError(f"{path}: invalid absolute-weight normalization sums")
    expected_signed = (
        sumw_accepted / sumw_generated if sumw_generated != 0.0 else math.nan
    )
    observed_signed = float(run["phase_space_signed_efficiency"])
    if math.isnan(expected_signed):
        if not math.isnan(observed_signed):
            raise MergeError(f"{path}: signed efficiency must be NaN")
    else:
        _close(observed_signed, expected_signed, f"{path}: signed efficiency")
    expected_absolute = (
        sumabsw_accepted / sumabsw_generated
        if sumabsw_generated != 0.0
        else math.nan
    )
    observed_absolute = float(run["phase_space_absolute_efficiency"])
    if math.isnan(expected_absolute):
        if not math.isnan(observed_absolute):
            raise MergeError(f"{path}: absolute efficiency must be NaN")
    else:
        _close(observed_absolute, expected_absolute, f"{path}: absolute efficiency")
    return sumabsw_generated, sumabsw_accepted


def _validate_run_provenance(
    run: Mapping[str, int | float], metadata: Mapping[str, Any], path: Path
) -> None:
    """Cross-check mutable ROOT run fields against embedded stage metadata."""

    generation = _nested(metadata, ("provenance", "generation"))
    simulation = _nested(metadata, ("provenance", "simulation"))
    if not isinstance(generation, Mapping) or not isinstance(simulation, Mapping):
        raise ProvenanceError(f"{path}: generation/simulation provenance is invalid")
    integer_pairs = (
        ("generation_seed", generation, "seed"),
        ("event_count", generation, "events"),
        ("event_number_start", generation, "first_event"),
        ("run_number", generation, "run_number"),
        ("delphes_seed", simulation, "random_seed"),
    )
    for run_name, source, metadata_name in integer_pairs:
        if metadata_name not in source:
            raise ProvenanceError(
                f"{path}: embedded metadata is missing {metadata_name}"
            )
        try:
            embedded = int(source[metadata_name])
        except (TypeError, ValueError) as exc:
            raise ProvenanceError(
                f"{path}: embedded {metadata_name} is not an integer"
            ) from exc
        if int(run[run_name]) != embedded:
            raise ProvenanceError(
                f"{path}: Runs.{run_name} disagrees with embedded metadata"
            )
    _close(
        float(run["ecm_energy_gev"]),
        float(generation.get("ecm_energy_gev", math.nan)),
        f"{path}: Runs.ecm_energy_gev",
    )
    run_release = (
        int(run["athgeneration_release_major"]),
        int(run["athgeneration_release_minor"]),
        int(run["athgeneration_release_patch"]),
    )
    backend = str(generation.get("generator_backend", ""))
    if backend == ATHGENERATION_BACKEND:
        release = str(generation.get("athgeneration_release", ""))
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", release)
        if match is None:
            raise ProvenanceError(f"{path}: invalid embedded AthGeneration release")
        release_parts = tuple(int(part) for part in match.groups())
        if run_release != release_parts:
            raise ProvenanceError(
                f"{path}: Runs AthGeneration release disagrees with metadata"
            )
    elif backend == STANDALONE_GENERATOR_BACKEND:
        if "athgeneration_release" in generation:
            raise ProvenanceError(
                f"{path}: standalone generation metadata declares an "
                "AthGeneration release"
            )
        if run_release != (0, 0, 0):
            raise ProvenanceError(
                f"{path}: standalone Runs AthGeneration release must be 0.0.0"
            )
    else:
        raise ProvenanceError(f"{path}: unsupported generator backend {backend!r}")


def _alternative_weight_ids(
    root_file: Any, metadata: Mapping[str, Any], path: Path, event_count: int
) -> tuple[str, ...]:
    value = metadata.get("lhe_alternative_weights")
    if not isinstance(value, Mapping):
        raise ProvenanceError(f"{path}: missing LHE alternative-weight metadata")
    ids_value = value.get("ids")
    if not isinstance(ids_value, list) or not all(isinstance(item, str) for item in ids_value):
        raise ProvenanceError(f"{path}: alternative weight IDs must be a string array")
    ids = tuple(ids_value)
    if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
        raise ProvenanceError(f"{path}: alternative weight IDs are not unique lexicographic order")
    has_tree = LHE_WEIGHT_TREE_NAME in root_file
    if bool(ids) != has_tree:
        raise SchemaError(f"{path}: LHEWeights presence disagrees with analysis metadata")
    if not ids:
        if value.get("tree") is not None:
            raise ProvenanceError(f"{path}: empty alternative weights must declare a null tree")
        return ids
    if value.get("tree") != LHE_WEIGHT_TREE_NAME or value.get("one_row_per_event") is not True:
        raise ProvenanceError(f"{path}: invalid LHEWeights metadata contract")
    tree = root_file[LHE_WEIGHT_TREE_NAME]
    if getattr(tree, "classname", None) != "TTree":
        raise SchemaError(f"{path}: LHEWeights is not a TTree")
    expected_schema = _weight_tree_schema(len(ids))
    _require_exact_schema(tree, expected_schema, label=f"{path}: LHEWeights")
    if int(tree.num_entries) != event_count:
        raise SchemaError(f"{path}: LHEWeights is not one-to-one with Events")
    return ids


def _weight_tree_schema(weight_count: int) -> dict[str, np.dtype]:
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


def _inspect_job(
    path: Path,
    *,
    step_size: str,
) -> JobInspection:
    digest = sha256_file(path)
    expected_keys = {EVENT_TREE_NAME, RUN_TREE_NAME, ANALYSIS_METADATA_NAME}
    with uproot.open(path) as root_file:
        metadata = _read_analysis_metadata(root_file, path)
        if EVENT_TREE_NAME not in root_file or RUN_TREE_NAME not in root_file:
            raise SchemaError(f"{path} is missing Events or Runs")
        event_tree = root_file[EVENT_TREE_NAME]
        run_tree = root_file[RUN_TREE_NAME]
        if (
            getattr(event_tree, "classname", None) != "TTree"
            or getattr(run_tree, "classname", None) != "TTree"
        ):
            raise SchemaError(f"{path}: Events and Runs must be TTrees")
        _require_exact_schema(event_tree, output_schema(), label=f"{path}: Events")
        _require_exact_schema(run_tree, BASE_RUN_SCHEMA, label=f"{path}: Runs")
        run_arrays, run = _scalar_run_arrays(run_tree, path)
        event_count = int(event_tree.num_entries)
        if event_count <= 0 or event_count != int(run["event_count"]):
            raise MergeError(f"{path}: Events count disagrees with its Runs row")
        alternative_ids = _alternative_weight_ids(root_file, metadata, path, event_count)
        if alternative_ids:
            expected_keys.add(LHE_WEIGHT_TREE_NAME)
        actual_keys = set(root_file.keys(cycle=False))
        if actual_keys != expected_keys:
            raise SchemaError(
                f"{path}: expected a job-level analysis file; unexpected ROOT keys "
                f"{sorted(actual_keys.difference(expected_keys))}"
            )

        campaign_id = int(run["campaign_id"])
        sample_code = int(run["sample_code"])
        job_id = int(run["job_id"])
        if int(run["schema_version"]) != ANALYSIS_SCHEMA_VERSION:
            raise SchemaError(f"{path}: unsupported Runs schema version")
        if metadata.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
            raise ProvenanceError(f"{path}: unsupported analysis metadata schema version")
        if metadata.get("sample_code") != sample_code:
            raise ProvenanceError(f"{path}: sample code disagrees between Runs and metadata")
        expected_sample = {code: name for name, code in SAMPLE_CODES.items()}.get(
            sample_code
        )
        if expected_sample is None or metadata.get("sample") != expected_sample:
            raise ProvenanceError(
                f"{path}: sample name/code is not a supported process mapping"
            )
        _validate_run_provenance(run, metadata, path)

        positive = negative = zero = reconstructed = truth_valid_count = 0
        validity = {name: 0 for name in VALIDITY_COUNT_BRANCHES}
        sumw = _CompensatedSum()
        sumw2 = _CompensatedSum()
        sumabsw = _CompensatedSum()
        sumw_delphes = _CompensatedSum()
        sumw2_delphes = _CompensatedSum()
        sumabsw_delphes = _CompensatedSum()
        first_cross_section: float | None = None
        minimum_cross_section = math.inf
        maximum_cross_section = -math.inf
        final_cross_section: float | None = None
        first_cross_section_error: float | None = None
        minimum_cross_section_error = math.inf
        maximum_cross_section_error = -math.inf
        final_cross_section_error: float | None = None
        source_sequence_digest = hashlib.sha256()
        previous_source_id = 0
        source_min: int | None = None
        source_max: int | None = None
        offset = 0
        alternative_tree = root_file[LHE_WEIGHT_TREE_NAME] if alternative_ids else None
        alternative_values_digest = hashlib.sha256() if alternative_ids else None
        for arrays in event_tree.iterate(
            expressions=PREFLIGHT_EVENT_BRANCHES,
            step_size=step_size,
            library="np",
            how=dict,
        ):
            size = len(arrays["weight_lhe"])
            expected_ordinal = np.arange(offset, offset + size, dtype=np.uint64)
            for name in ("lhe_event_index", "hepmc_entry", "delphes_entry"):
                if not np.array_equal(np.asarray(arrays[name], dtype=np.uint64), expected_ordinal):
                    raise MergeError(f"{path}: {name} is not its original contiguous ordinal")
            if not np.array_equal(arrays["hepmc_event_number"], arrays["delphes_event_number"]):
                raise MergeError(f"{path}: HepMC and Delphes event numbers disagree")
            expected_numbers = np.arange(
                int(run["event_number_start"]) + offset,
                int(run["event_number_start"]) + offset + size,
                dtype=np.int64,
            )
            if not np.array_equal(np.asarray(arrays["hepmc_event_number"]), expected_numbers):
                raise MergeError(f"{path}: event numbers are not the recorded contiguous sequence")
            for name, expected_value in (
                ("campaign_id", campaign_id),
                ("sample_code", sample_code),
                ("job_id", job_id),
            ):
                if not np.all(np.asarray(arrays[name]) == expected_value):
                    raise MergeError(f"{path}: Events.{name} disagrees with Runs")
            for name in ("has_lhe", "has_hepmc", "has_delphes"):
                if not np.all(np.asarray(arrays[name], dtype=np.bool_)):
                    raise MergeError(f"{path}: Events.{name} contains false values")
            if not np.all(
                np.asarray(arrays["lhe_n_alternative_weights"])
                == len(alternative_ids)
            ):
                raise MergeError(
                    f"{path}: per-event alternative-weight count disagrees "
                    "with metadata"
                )
            if not np.array_equal(arrays["reconstructed"], arrays["reco_pass_selection"]):
                raise MergeError(f"{path}: reconstructed alias disagrees with selection")

            weights = np.asarray(arrays["weight_lhe"], dtype=np.float64)
            if not np.all(np.isfinite(weights)):
                raise MergeError(f"{path}: weight_lhe contains non-finite values")
            sumw.add_array(weights)
            sumw2.add_array(weights * weights)
            sumabsw.add_array(np.abs(weights))
            positive += int(np.count_nonzero(weights > 0.0))
            negative += int(np.count_nonzero(weights < 0.0))
            zero += int(np.count_nonzero(weights == 0.0))
            delphes_weights = np.asarray(arrays["weight_delphes"], dtype=np.float64)
            cross_sections = np.asarray(
                arrays["cross_section_pb_delphes"], dtype=np.float64
            )
            cross_section_errors = np.asarray(
                arrays["cross_section_error_pb_delphes"], dtype=np.float64
            )
            if not (
                np.all(np.isfinite(delphes_weights))
                and np.all(np.isfinite(cross_sections))
                and np.all(np.isfinite(cross_section_errors))
            ):
                raise MergeError(f"{path}: non-finite Delphes weight diagnostic")
            sumw_delphes.add_array(delphes_weights)
            sumw2_delphes.add_array(delphes_weights * delphes_weights)
            sumabsw_delphes.add_array(np.abs(delphes_weights))
            if size:
                if first_cross_section is None:
                    first_cross_section = float(cross_sections[0])
                    first_cross_section_error = float(cross_section_errors[0])
                minimum_cross_section = min(
                    minimum_cross_section, float(np.min(cross_sections))
                )
                maximum_cross_section = max(
                    maximum_cross_section, float(np.max(cross_sections))
                )
                final_cross_section = float(cross_sections[-1])
                minimum_cross_section_error = min(
                    minimum_cross_section_error, float(np.min(cross_section_errors))
                )
                maximum_cross_section_error = max(
                    maximum_cross_section_error, float(np.max(cross_section_errors))
                )
                final_cross_section_error = float(cross_section_errors[-1])
            for level in ("lhe", "dressed", "reco"):
                for kind in ("candidate", "projection_valid"):
                    name = f"{level}_{kind}_count"
                    validity[name] += int(np.count_nonzero(arrays[f"{level}_{kind}"]))
            reconstructed += int(np.count_nonzero(arrays["reconstructed"]))
            finite_angles = np.ones(size, dtype=np.bool_)
            for name in ("lhe_theta1", "lhe_phi1", "lhe_theta2", "lhe_phi2"):
                finite_angles &= np.isfinite(np.asarray(arrays[name], dtype=np.float64))
            lhe_candidate = np.asarray(arrays["lhe_candidate"], dtype=np.bool_)
            lhe_projection_valid = np.asarray(
                arrays["lhe_projection_valid"], dtype=np.bool_
            )
            if np.any(lhe_projection_valid & ~lhe_candidate):
                raise MergeError(
                    f"{path}: LHE projection is marked valid without a candidate"
                )
            if np.any(lhe_projection_valid & ~finite_angles):
                raise MergeError(
                    f"{path}: non-finite LHE angles on a projection-valid row"
                )
            truth_valid_count += int(
                np.count_nonzero(
                    lhe_candidate & lhe_projection_valid & finite_angles
                )
            )

            sources = np.asarray(arrays["source_event_id"], dtype=np.uint64)
            for local_index, source_value in enumerate(sources):
                source_id = int(source_value)
                if source_id <= 0:
                    raise MergeError(f"{path}: invalid source_event_id {source_id}")
                if source_id <= previous_source_id:
                    raise MergeError(f"{path}: source_event_id sequence is not strictly increasing")
                previous_source_id = source_id
                source_sequence_digest.update(source_id.to_bytes(8, "big"))
                source_min = source_id if source_min is None else min(source_min, source_id)
                source_max = source_id if source_max is None else max(source_max, source_id)
                expected_hi, expected_lo = event_uid(campaign_id, sample_code, job_id, source_id)
                observed_uid = (
                    int(arrays["event_uid_hi"][local_index]),
                    int(arrays["event_uid_lo"][local_index]),
                )
                if observed_uid != (expected_hi, expected_lo):
                    raise MergeError(f"{path}: stored event UID does not match its logical key")
            if alternative_tree is not None:
                weight_arrays = alternative_tree.arrays(
                    expressions=(*IDENTITY_BRANCHES, "values"),
                    entry_start=offset,
                    entry_stop=offset + size,
                    library="np",
                    how=dict,
                )
                for name in IDENTITY_BRANCHES:
                    if not np.array_equal(weight_arrays[name], arrays[name]):
                        raise MergeError(f"{path}: LHEWeights.{name} is not aligned with Events")
                values = np.asarray(weight_arrays["values"], dtype=np.float64)
                if values.shape != (size, len(alternative_ids)) or not np.all(np.isfinite(values)):
                    raise MergeError(f"{path}: invalid LHEWeights.values payload")
                assert alternative_values_digest is not None
                alternative_values_digest.update(
                    np.asarray(values, dtype="<f8").tobytes(order="C")
                )
            offset += size

        if offset != event_count:
            raise MergeError(f"{path}: chunked preflight did not visit every event")
        if (
            source_min != int(run["source_event_id_min"])
            or source_max != int(run["source_event_id_max"])
        ):
            raise MergeError(f"{path}: source-event extrema disagree with Runs")
        recorded_sequence_digest = _nested(
            metadata, ("source_event_id", "sequence_sha256")
        )
        if source_sequence_digest.hexdigest() != recorded_sequence_digest:
            raise MergeError(f"{path}: source-event sequence digest disagrees with metadata")
        observed_counts = {
            "positive_weight_count": positive,
            "negative_weight_count": negative,
            "zero_weight_count": zero,
            **validity,
            "reconstructed_count": reconstructed,
        }
        for name, value in observed_counts.items():
            if value != int(run[name]):
                raise MergeError(f"{path}: recomputed {name} disagrees with Runs")
        _close(sumw.total, float(run["sumw"]), f"{path}: retained sumw")
        _close(sumw2.total, float(run["sumw2"]), f"{path}: retained sumw2")
        _close(sumabsw.total, float(run["sumabsw"]), f"{path}: retained sumabsw")
        _close(
            sumw_delphes.total,
            float(run["sumw_delphes"]),
            f"{path}: retained Delphes sumw",
        )
        _close(
            sumw2_delphes.total,
            float(run["sumw2_delphes"]),
            f"{path}: retained Delphes sumw2",
        )
        _close(
            sumabsw_delphes.total,
            float(run["sumabsw_delphes"]),
            f"{path}: retained Delphes sumabsw",
        )
        delphes_cross_section_checks = {
            "cross_section_first_pb_delphes": first_cross_section,
            "cross_section_min_pb_delphes": minimum_cross_section,
            "cross_section_max_pb_delphes": maximum_cross_section,
            "cross_section_final_pb_delphes": final_cross_section,
            "cross_section_error_first_pb_delphes": first_cross_section_error,
            "cross_section_error_min_pb_delphes": minimum_cross_section_error,
            "cross_section_error_max_pb_delphes": maximum_cross_section_error,
            "cross_section_error_final_pb_delphes": final_cross_section_error,
        }
        for name, value in delphes_cross_section_checks.items():
            if value is None:
                raise MergeError(f"{path}: missing Delphes cross-section diagnostics")
            _close(float(value), float(run[name]), f"{path}: Runs.{name}")
        normalization_abs_generated, normalization_abs_accepted = _validate_job_normalization(
            run, metadata, path
        )

    return JobInspection(
        path=path,
        sha256=digest,
        metadata=metadata,
        run_arrays=run_arrays,
        run=run,
        event_count=event_count,
        campaign_id=campaign_id,
        sample_code=sample_code,
        job_id=job_id,
        alternative_weight_ids=alternative_ids,
        alternative_values_sha256=(
            alternative_values_digest.hexdigest()
            if alternative_values_digest is not None
            else None
        ),
        positive_weight_count=positive,
        negative_weight_count=negative,
        zero_weight_count=zero,
        sumw=sumw.total,
        sumw2=sumw2.total,
        sumabsw=sumabsw.total,
        validity_counts=validity,
        reconstructed_count=reconstructed,
        truth_lhe_valid_count=truth_valid_count,
        normalization_sumabsw_generated_pb=normalization_abs_generated,
        normalization_sumabsw_accepted_pb=normalization_abs_accepted,
    )


def pool_normalization(jobs: Sequence[JobInspection]) -> dict[str, int | float]:
    """Pool IDWTUP=-4 primitive moments and recompute campaign estimates."""

    if not jobs:
        raise ValueError("at least one inspected job is required")
    generated = sum(int(job.run["normalization_generated_lhe_events"]) for job in jobs)
    accepted = sum(int(job.run["normalization_accepted_lhe_events"]) for job in jobs)
    sumw_generated = math.fsum(
        float(job.run["normalization_sumw_generated_pb"]) for job in jobs
    )
    sumw2_generated = math.fsum(
        float(job.run["normalization_sumw2_generated_pb2"]) for job in jobs
    )
    sumw_accepted = math.fsum(
        float(job.run["normalization_sumw_accepted_pb"]) for job in jobs
    )
    sumw2_accepted = math.fsum(
        float(job.run["normalization_sumw2_accepted_pb2"]) for job in jobs
    )
    sumabsw_generated = math.fsum(
        job.normalization_sumabsw_generated_pb for job in jobs
    )
    sumabsw_accepted = math.fsum(
        job.normalization_sumabsw_accepted_pb for job in jobs
    )
    if generated <= 0 or not 0 <= accepted <= generated:
        raise MergeError("pooled normalization counts are inconsistent")
    inclusive = sumw_generated / generated
    filtered = sumw_accepted / generated
    return {
        "normalization_generated_lhe_events": generated,
        "normalization_accepted_lhe_events": accepted,
        "normalization_sumw_generated_pb": sumw_generated,
        "normalization_sumw2_generated_pb2": sumw2_generated,
        "normalization_sumabsw_generated_pb": sumabsw_generated,
        "normalization_sumw_accepted_pb": sumw_accepted,
        "normalization_sumw2_accepted_pb2": sumw2_accepted,
        "normalization_sumabsw_accepted_pb": sumabsw_accepted,
        "inclusive_lhe_cross_section_pb": inclusive,
        "inclusive_lhe_cross_section_mc_error_pb": _mean_mc_error(
            sumw_generated, sumw2_generated, generated, "pooled inclusive"
        ),
        "effective_filtered_cross_section_pb": filtered,
        "effective_filtered_cross_section_mc_error_pb": _mean_mc_error(
            sumw_accepted, sumw2_accepted, generated, "pooled filtered"
        ),
        "phase_space_count_efficiency": accepted / generated,
        "phase_space_signed_efficiency": (
            sumw_accepted / sumw_generated if sumw_generated != 0.0 else math.nan
        ),
        "phase_space_absolute_efficiency": (
            sumabsw_accepted / sumabsw_generated
            if sumabsw_generated != 0.0
            else math.nan
        ),
    }


def _resolve_inputs(inputs: Sequence[str | Path]) -> tuple[Path, ...]:
    if len(inputs) < 2:
        raise ValueError("at least two job-level inputs are required")
    resolved: list[Path] = []
    for original in inputs:
        source = Path(original).expanduser().resolve()
        if source.is_dir():
            if not (source / "SUCCESS").is_file():
                raise FileNotFoundError(f"completed job directory lacks SUCCESS: {source}")
            candidates = [path for path in source.rglob("analysis.root") if path.is_file()]
            if len(candidates) != 1:
                raise FileNotFoundError(
                    f"completed job directory must contain exactly one analysis.root: {source}"
                )
            source = candidates[0].resolve()
        if not source.is_file() or source.stat().st_size == 0:
            raise FileNotFoundError(f"analysis input is not a nonempty regular file: {source}")
        resolved.append(source)
    if len(set(resolved)) != len(resolved):
        raise ValueError("duplicate analysis input paths are not allowed")
    return tuple(resolved)


def _acquire_output_lock(path: Path) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise FileExistsError(f"another merger is targeting output: {path}") from exc
    return descriptor, lock_path


def _temporary_output(path: Path, overwrite: bool) -> Path:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {path}; pass --overwrite")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.partial.", dir=path.parent)
    os.close(descriptor)
    return Path(name)


def _publish_output(temporary: Path, output: Path, overwrite: bool) -> None:
    if overwrite:
        os.replace(temporary, output)
        return
    try:
        os.link(temporary, output)
    except FileExistsError as exc:
        raise FileExistsError(f"output appeared while merging: {output}") from exc
    temporary.unlink()


def _output_event_schema() -> dict[str, np.dtype]:
    schema = dict(output_schema())
    schema["weight_nominal_pb"] = np.dtype("float64")
    schema["truth_lhe_valid"] = np.dtype("bool")
    for slug in EXPECTED_TRUTH_SLUGS:
        schema[f"truth_h_{slug}"] = np.dtype("float64")
        schema[f"truth_factor_{slug}"] = np.dtype("float64")
        schema[f"weight_truth_{slug}_pb"] = np.dtype("float64")
    return schema


def _merge_summary_schema() -> dict[str, np.dtype]:
    return {
        "schema_version": np.dtype("uint16"),
        "analysis_schema_version": np.dtype("uint16"),
        "input_file_count": np.dtype("uint32"),
        "source_job_count": np.dtype("uint32"),
        "campaign_id": np.dtype("uint64"),
        "sample_code": np.dtype("uint8"),
        "event_count": np.dtype("uint64"),
        "positive_weight_count": np.dtype("uint64"),
        "negative_weight_count": np.dtype("uint64"),
        "zero_weight_count": np.dtype("uint64"),
        "retained_raw_sumw_pb": np.dtype("float64"),
        "retained_raw_sumw2_pb2": np.dtype("float64"),
        "retained_raw_sumabsw_pb": np.dtype("float64"),
        "merged_weight_scale": np.dtype("float64"),
        "sumw_nominal_pb": np.dtype("float64"),
        "nominal_closure_residual_pb": np.dtype("float64"),
        "nominal_closure_tolerance_pb": np.dtype("float64"),
        "truth_lhe_valid_count": np.dtype("uint64"),
        "alternative_lhe_weight_count": np.dtype("uint16"),
        "truth_component_count": np.dtype("uint8"),
        "normalization_generated_lhe_events": np.dtype("uint64"),
        "normalization_accepted_lhe_events": np.dtype("uint64"),
        "normalization_sumw_generated_pb": np.dtype("float64"),
        "normalization_sumw2_generated_pb2": np.dtype("float64"),
        "normalization_sumabsw_generated_pb": np.dtype("float64"),
        "normalization_sumw_accepted_pb": np.dtype("float64"),
        "normalization_sumw2_accepted_pb2": np.dtype("float64"),
        "normalization_sumabsw_accepted_pb": np.dtype("float64"),
        "inclusive_lhe_cross_section_pb": np.dtype("float64"),
        "inclusive_lhe_cross_section_mc_error_pb": np.dtype("float64"),
        "effective_filtered_cross_section_pb": np.dtype("float64"),
        "effective_filtered_cross_section_mc_error_pb": np.dtype("float64"),
        "phase_space_signed_efficiency": np.dtype("float64"),
        "phase_space_absolute_efficiency": np.dtype("float64"),
        "phase_space_count_efficiency": np.dtype("float64"),
        **{name: np.dtype("uint64") for name in VALIDITY_COUNT_BRANCHES},
        "reconstructed_count": np.dtype("uint64"),
    }


def _one_row(
    schema: Mapping[str, np.dtype], values: Mapping[str, int | float]
) -> dict[str, np.ndarray]:
    return {
        name: np.asarray([values[name]], dtype=dtype)
        for name, dtype in schema.items()
    }


def _code_provenance() -> dict[str, Any]:
    relative_paths = (
        "Merging/merge_analysis_outputs.py",
        "Analysis/build_analysis_tree.py",
        "src/offshell_production/harmonics.py",
        "pyproject.toml",
        "uv.lock",
    )
    files: dict[str, dict[str, str]] = {}
    for relative in relative_paths:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"merge provenance file is missing: {path}")
        files[relative] = {"sha256": sha256_file(path)}
    return {"hash_algorithm": "sha256", "files": files}


def _component_metadata() -> list[dict[str, int | str]]:
    output: list[dict[str, int | str]] = []
    for component in TRUTH_ANGULAR_COMPONENTS:
        output.append(
            {
                "label": str(component.label),
                "branch_slug": str(component.branch_slug),
                "l1": int(component.l1),
                "m1": int(component.m1),
                "l2": int(component.l2),
                "m2": int(component.m2),
            }
        )
    return output


def _json_normalization_values(
    values: Mapping[str, int | float],
) -> dict[str, int | float | None]:
    """Represent undefined floating summaries as JSON null, never bare NaN."""

    return {
        name: (
            None
            if isinstance(value, float) and not math.isfinite(value)
            else value
        )
        for name, value in values.items()
    }


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with RFC-compliant JSON null."""

    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (float, np.floating)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def _verify_temporary_output(
    path: Path,
    *,
    jobs: Sequence[JobInspection],
    alternative_ids: Sequence[str],
    event_count: int,
    raw_sumw: float,
    raw_sumabsw: float,
    merged_scale: float,
    nominal_target: float,
    step_size: str,
) -> None:
    """Reopen the complete temporary artifact and check its merge invariants."""

    expected_keys = {
        EVENT_TREE_NAME,
        RUN_TREE_NAME,
        MERGE_SUMMARY_TREE_NAME,
        MERGE_METADATA_NAME,
    }
    if alternative_ids:
        expected_keys.add(LHE_WEIGHT_TREE_NAME)
    with uproot.open(path) as root_file:
        actual_keys = set(root_file.keys(cycle=False))
        if actual_keys != expected_keys:
            raise MergeError(
                "temporary merged output has unexpected ROOT keys: "
                f"{sorted(actual_keys.symmetric_difference(expected_keys))}"
            )
        events = root_file[EVENT_TREE_NAME]
        runs = root_file[RUN_TREE_NAME]
        summary = root_file[MERGE_SUMMARY_TREE_NAME]
        _require_exact_schema(events, _output_event_schema(), label="merged Events")
        _require_exact_schema(runs, BASE_RUN_SCHEMA, label="merged Runs")
        _require_exact_schema(
            summary, _merge_summary_schema(), label="merged MergeSummary"
        )
        if int(events.num_entries) != event_count:
            raise MergeError("temporary merged Events entry count is incorrect")
        if int(runs.num_entries) != len(jobs):
            raise MergeError("temporary merged Runs entry count is incorrect")
        if int(summary.num_entries) != 1:
            raise MergeError("temporary MergeSummary is not a one-row tree")
        run_arrays = runs.arrays(library="np", how=dict)
        for name in BASE_RUN_SCHEMA:
            expected = np.concatenate([job.run_arrays[name] for job in jobs])
            if not np.array_equal(run_arrays[name], expected, equal_nan=True):
                raise MergeError(f"temporary merged Runs.{name} changed a source row")
        if alternative_ids:
            weights = root_file[LHE_WEIGHT_TREE_NAME]
            _require_exact_schema(
                weights,
                _weight_tree_schema(len(alternative_ids)),
                label="merged LHEWeights",
            )
            if int(weights.num_entries) != event_count:
                raise MergeError("temporary merged LHEWeights entry count is incorrect")
            job_entry_start = 0
            for job in jobs:
                digest = hashlib.sha256()
                job_entry_stop = job_entry_start + job.event_count
                for weight_arrays in weights.iterate(
                    expressions=("values",),
                    entry_start=job_entry_start,
                    entry_stop=job_entry_stop,
                    step_size=step_size,
                    library="np",
                    how=dict,
                ):
                    digest.update(
                        np.asarray(weight_arrays["values"], dtype="<f8").tobytes(
                            order="C"
                        )
                    )
                if digest.hexdigest() != job.alternative_values_sha256:
                    raise MergeError(
                        "temporary LHEWeights values differ from their source job"
                    )
                job_entry_start = job_entry_stop

        raw = _CompensatedSum()
        nominal = _CompensatedSum()
        truth_valid_count = 0
        output_offset = 0
        expressions = [
            *IDENTITY_BRANCHES,
            "weight_lhe",
            "weight_nominal_pb",
            "truth_lhe_valid",
        ]
        for slug in EXPECTED_TRUTH_SLUGS:
            expressions.extend(
                (
                    f"truth_h_{slug}",
                    f"truth_factor_{slug}",
                    f"weight_truth_{slug}_pb",
                )
            )
        for arrays in events.iterate(
            expressions=expressions,
            step_size=step_size,
            library="np",
            how=dict,
        ):
            raw_weight = np.asarray(arrays["weight_lhe"], dtype=np.float64)
            nominal_weight = np.asarray(
                arrays["weight_nominal_pb"], dtype=np.float64
            )
            valid = np.asarray(arrays["truth_lhe_valid"], dtype=np.bool_)
            raw.add_array(raw_weight)
            nominal.add_array(nominal_weight)
            truth_valid_count += int(np.count_nonzero(valid))
            if not np.array_equal(nominal_weight, raw_weight * merged_scale):
                raise MergeError(
                    "temporary weight_nominal_pb is not the common-scale "
                    "multiple of weight_lhe"
                )
            if alternative_ids:
                weight_arrays = root_file[LHE_WEIGHT_TREE_NAME].arrays(
                    expressions=IDENTITY_BRANCHES,
                    entry_start=output_offset,
                    entry_stop=output_offset + len(raw_weight),
                    library="np",
                    how=dict,
                )
                for name in IDENTITY_BRANCHES:
                    if not np.array_equal(weight_arrays[name], arrays[name]):
                        raise MergeError(
                            f"temporary LHEWeights.{name} is not aligned "
                            "with Events"
                        )
            for slug in EXPECTED_TRUTH_SLUGS:
                bare = np.asarray(arrays[f"truth_h_{slug}"], dtype=np.float64)
                factor = np.asarray(
                    arrays[f"truth_factor_{slug}"], dtype=np.float64
                )
                contribution = np.asarray(
                    arrays[f"weight_truth_{slug}_pb"], dtype=np.float64
                )
                if not np.allclose(
                    bare[valid] * (4.0 * np.pi),
                    factor[valid],
                    rtol=2.0e-15,
                    atol=2.0e-15,
                ):
                    raise MergeError(f"temporary truth_h_{slug} factor mismatch")
                if not np.allclose(
                    nominal_weight[valid] * factor[valid],
                    contribution[valid],
                    rtol=2.0e-15,
                    atol=2.0e-15,
                ):
                    raise MergeError(f"temporary truth contribution {slug} mismatch")
                if not (
                    np.all(np.isnan(bare[~valid]))
                    and np.all(np.isnan(factor[~valid]))
                    and np.all(np.isnan(contribution[~valid]))
                ):
                    raise MergeError(
                        f"temporary invalid truth rows for {slug} are not NaN"
                    )
            output_offset += len(raw_weight)
        if output_offset != event_count:
            raise MergeError("temporary output verification did not visit every event")
        _close(raw.total, raw_sumw, "temporary retained raw sumw")
        residual, tolerance = _nominal_closure(
            nominal.total,
            nominal_target,
            scale=merged_scale,
            raw_sumabsw=raw_sumabsw,
            label="temporary nominal cross section",
        )
        summary_arrays = summary.arrays(library="np", how=dict)
        _nominal_closure(
            float(summary_arrays["sumw_nominal_pb"][0]),
            nominal_target,
            scale=merged_scale,
            raw_sumabsw=raw_sumabsw,
            label="temporary MergeSummary nominal cross section",
        )
        _close(
            float(summary_arrays["nominal_closure_residual_pb"][0]),
            residual,
            "temporary MergeSummary nominal closure residual",
        )
        _close(
            float(summary_arrays["nominal_closure_tolerance_pb"][0]),
            tolerance,
            "temporary MergeSummary nominal closure tolerance",
        )
        if int(summary_arrays["truth_lhe_valid_count"][0]) != truth_valid_count:
            raise MergeError("temporary MergeSummary truth-valid count is incorrect")
        try:
            metadata = json.loads(str(root_file[MERGE_METADATA_NAME]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise MergeError("temporary merge_metadata is invalid JSON") from exc
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("schema_version") != MERGE_SCHEMA_VERSION
            or len(metadata.get("inputs", [])) != len(jobs)
        ):
            raise MergeError("temporary merge_metadata does not describe its inputs")


def merge_analysis_outputs(
    inputs: Sequence[str | Path],
    output: str | Path,
    *,
    step_size: str = "50 MB",
    overwrite: bool = False,
) -> dict[str, int | float | str]:
    """Validate, merge, normalize, and truth-reweight job analysis outputs."""

    component_slugs = tuple(
        str(component.branch_slug) for component in TRUTH_ANGULAR_COMPONENTS
    )
    if component_slugs != EXPECTED_TRUTH_SLUGS:
        raise RuntimeError(
            f"harmonic component schema is {component_slugs}, expected {EXPECTED_TRUTH_SLUGS}"
        )
    input_paths = _resolve_inputs(inputs)
    output_path = Path(output).expanduser().resolve()
    if output_path in input_paths:
        raise ValueError("output must not alias an input analysis file")

    lock_descriptor, _lock_path = _acquire_output_lock(output_path)
    temporary: Path | None = None
    try:
        jobs = [
            _inspect_job(path, step_size=step_size)
            for path in input_paths
        ]
        input_hashes = [job.sha256 for job in jobs]
        if len(input_hashes) != len(set(input_hashes)):
            raise MergeError("byte-identical analysis inputs are not allowed")
        job_keys = [(job.campaign_id, job.sample_code, job.job_id) for job in jobs]
        if len(job_keys) != len(set(job_keys)):
            raise MergeError("duplicate (campaign_id, sample_code, job_id) source jobs")
        campaigns = {job.campaign_id for job in jobs}
        samples = {job.sample_code for job in jobs}
        if len(campaigns) != 1 or len(samples) != 1:
            raise ProvenanceError("all inputs must belong to one campaign and sample")
        generation_seeds = [int(job.run["generation_seed"]) for job in jobs]
        if any(seed <= 0 for seed in generation_seeds):
            raise MergeError("generation seeds must be positive")
        if len(generation_seeds) != len(set(generation_seeds)):
            raise MergeError(
                "duplicate generation seeds across source jobs would double count "
                "or correlate hard events"
            )
        delphes_seeds = [int(job.run["delphes_seed"]) for job in jobs]
        if any(seed <= 0 for seed in delphes_seeds):
            raise MergeError("Delphes seeds must be positive")
        if len(delphes_seeds) != len(set(delphes_seeds)):
            raise MergeError(
                "duplicate Delphes seeds across source jobs would correlate "
                "detector-response decisions"
            )
        fingerprints = [_physics_fingerprint(job.metadata) for job in jobs]
        for job, fingerprint in zip(jobs[1:], fingerprints[1:]):
            if fingerprint != fingerprints[0]:
                changed = sorted(
                    key
                    for key in set(fingerprints[0]) | set(fingerprint)
                    if fingerprints[0].get(key) != fingerprint.get(key)
                )
                raise ProvenanceError(
                    f"{job.path}: incompatible physics/provenance invariants: {changed}"
                )
        weight_id_schemas = {job.alternative_weight_ids for job in jobs}
        if len(weight_id_schemas) != 1:
            raise SchemaError("all inputs must have the same optional LHEWeights ID schema")
        alternative_ids = jobs[0].alternative_weight_ids

        pooled = pool_normalization(jobs)
        raw_sumw = math.fsum(job.sumw for job in jobs)
        raw_sumabsw = math.fsum(job.sumabsw for job in jobs)
        if not math.isfinite(raw_sumw) or raw_sumw == 0.0:
            raise MergeError(
                "retained raw weight sum is zero or non-finite; "
                "no common scale exists"
            )
        if not math.isfinite(raw_sumabsw) or raw_sumabsw <= 0.0:
            raise MergeError("retained absolute-weight sum is zero or non-finite")
        if abs(raw_sumw) <= 1.0e-12 * raw_sumabsw:
            raise MergeError(
                "retained signed-weight sum is numerically unresolved relative to sumabsw"
            )
        pooled_cross_section = float(pooled["effective_filtered_cross_section_pb"])
        if pooled_cross_section * raw_sumw < 0.0:
            raise MergeError(
                "retained raw sumw and pooled filtered cross section have opposite signs"
            )
        merged_scale = pooled_cross_section / raw_sumw
        if not math.isfinite(merged_scale) or merged_scale == 0.0:
            raise MergeError("merged nominal weight scale is zero or non-finite")

        totals = {
            "event_count": sum(job.event_count for job in jobs),
            "positive_weight_count": sum(job.positive_weight_count for job in jobs),
            "negative_weight_count": sum(job.negative_weight_count for job in jobs),
            "zero_weight_count": sum(job.zero_weight_count for job in jobs),
            "retained_raw_sumw_pb": raw_sumw,
            "retained_raw_sumw2_pb2": math.fsum(job.sumw2 for job in jobs),
            "retained_raw_sumabsw_pb": raw_sumabsw,
            "truth_lhe_valid_count": sum(job.truth_lhe_valid_count for job in jobs),
            "reconstructed_count": sum(job.reconstructed_count for job in jobs),
            **{
                name: sum(job.validity_counts[name] for job in jobs)
                for name in VALIDITY_COUNT_BRANCHES
            },
        }

        temporary = _temporary_output(output_path, overwrite)
        nominal_sum = _CompensatedSum()
        with uproot.recreate(temporary) as output_file:
            output_event_schema = _output_event_schema()
            output_file.mktree(
                EVENT_TREE_NAME,
                output_event_schema,
                title="Merged off-shell four-lepton event tree with truth weights",
            )
            output_events = output_file[EVENT_TREE_NAME]
            output_weights = None
            if alternative_ids:
                output_file.mktree(
                    LHE_WEIGHT_TREE_NAME,
                    _weight_tree_schema(len(alternative_ids)),
                    title="Alternative LHE weights in metadata ID order",
                )
                output_weights = output_file[LHE_WEIGHT_TREE_NAME]

            written_events = 0
            written_weights = 0
            for job in jobs:
                with uproot.open(job.path) as input_file:
                    input_events = input_file[EVENT_TREE_NAME]
                    for arrays in input_events.iterate(
                        expressions=tuple(output_schema()),
                        step_size=step_size,
                        library="np",
                        how=dict,
                    ):
                        raw_weight = np.asarray(arrays["weight_lhe"], dtype=np.float64)
                        nominal_weight = raw_weight * merged_scale
                        nominal_sum.add_array(nominal_weight)
                        angles = {
                            name: np.asarray(arrays[f"lhe_{name}"], dtype=np.float64)
                            for name in ("theta1", "phi1", "theta2", "phi2")
                        }
                        valid = (
                            np.asarray(arrays["lhe_candidate"], dtype=np.bool_)
                            & np.asarray(arrays["lhe_projection_valid"], dtype=np.bool_)
                            & np.isfinite(angles["theta1"])
                            & np.isfinite(angles["phi1"])
                            & np.isfinite(angles["theta2"])
                            & np.isfinite(angles["phi2"])
                        )
                        factors = truth_angular_factors(
                            angles["theta1"],
                            angles["phi1"],
                            angles["theta2"],
                            angles["phi2"],
                            invalid="nan",
                        )
                        output_arrays = dict(arrays)
                        output_arrays["weight_nominal_pb"] = nominal_weight
                        output_arrays["truth_lhe_valid"] = valid
                        for slug in EXPECTED_TRUTH_SLUGS:
                            factor = np.asarray(factors[slug], dtype=np.float64)
                            if factor.shape != raw_weight.shape:
                                raise MergeError(f"harmonic factor {slug} has an invalid shape")
                            factor = np.where(valid, factor, np.nan)
                            if not np.all(np.isfinite(factor[valid])):
                                raise MergeError(
                                    f"harmonic factor {slug} is non-finite for "
                                    "a valid event"
                                )
                            output_arrays[f"truth_h_{slug}"] = factor / (4.0 * np.pi)
                            output_arrays[f"truth_factor_{slug}"] = factor
                            output_arrays[f"weight_truth_{slug}_pb"] = nominal_weight * factor
                        output_events.extend(output_arrays)
                        written_events += len(raw_weight)
                    if output_weights is not None:
                        input_weights = input_file[LHE_WEIGHT_TREE_NAME]
                        for arrays in input_weights.iterate(
                            step_size=step_size,
                            library="np",
                            how=dict,
                        ):
                            output_weights.extend(arrays)
                            written_weights += len(arrays["campaign_id"])
            if written_events != int(totals["event_count"]):
                raise MergeError("written Events count differs from preflight")
            if alternative_ids and written_weights != written_events:
                raise MergeError("written LHEWeights count differs from Events")

            nominal_closure_residual, nominal_closure_tolerance = _nominal_closure(
                nominal_sum.total,
                pooled_cross_section,
                scale=merged_scale,
                raw_sumabsw=raw_sumabsw,
                label="merged nominal-weight cross-section closure",
            )

            output_file.mktree(
                RUN_TREE_NAME,
                BASE_RUN_SCHEMA,
                title="Original job-level provenance and sums",
            )
            for job in jobs:
                output_file[RUN_TREE_NAME].extend(job.run_arrays)

            summary_schema = _merge_summary_schema()
            summary_values: dict[str, int | float] = {
                "schema_version": MERGE_SCHEMA_VERSION,
                "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
                "input_file_count": len(jobs),
                "source_job_count": len(jobs),
                "campaign_id": jobs[0].campaign_id,
                "sample_code": jobs[0].sample_code,
                "merged_weight_scale": merged_scale,
                "sumw_nominal_pb": nominal_sum.total,
                "nominal_closure_residual_pb": nominal_closure_residual,
                "nominal_closure_tolerance_pb": nominal_closure_tolerance,
                "alternative_lhe_weight_count": len(alternative_ids),
                "truth_component_count": len(EXPECTED_TRUTH_SLUGS),
                **totals,
                **pooled,
            }
            output_file.mktree(
                MERGE_SUMMARY_TREE_NAME,
                summary_schema,
                title="Campaign-level pooled normalization and merge totals",
            )
            output_file[MERGE_SUMMARY_TREE_NAME].extend(
                _one_row(summary_schema, summary_values)
            )

            source_sample = str(jobs[0].metadata["sample"])
            merge_metadata = {
                "schema_version": MERGE_SCHEMA_VERSION,
                "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
                "sample": source_sample,
                "sample_code": jobs[0].sample_code,
                "campaign_id": jobs[0].campaign_id,
                "input_ordering": "command_line_order",
                "inputs": [
                    {
                        "index": index,
                        "path": str(job.path),
                        "sha256": job.sha256,
                        "event_count": job.event_count,
                        "job_id": job.job_id,
                        "lhe_alternative_values_sha256": (
                            job.alternative_values_sha256
                        ),
                        "analysis_metadata": job.metadata,
                    }
                    for index, job in enumerate(jobs)
                ],
                "physics_invariants": fingerprints[0],
                "normalization": {
                    "contract": NORMALIZATION_CONTRACT,
                    "primitive_pooling": "sum counts and first/second signed moments across jobs",
                    "filtered_cross_section_formula": "sum(A_j) / sum(N_j)",
                    "mc_error_formula": "sqrt((Q-A^2/N)/(N*(N-1)))",
                    "rejected_event_weight": 0.0,
                    "values": _json_normalization_values(pooled),
                },
                "merged_nominal_weight": {
                    "raw_branch": "weight_lhe",
                    "output_branch": "weight_nominal_pb",
                    "raw_branch_immutable": True,
                    "scale": merged_scale,
                    "formula": (
                        "weight_nominal_pb = weight_lhe * "
                        "effective_filtered_cross_section_pb / retained_raw_sumw_pb"
                    ),
                    "retained_raw_sumw_pb": raw_sumw,
                    "sumw_nominal_pb": nominal_sum.total,
                    "target_cross_section_pb": pooled_cross_section,
                    "closure_residual_pb": nominal_closure_residual,
                    "closure_tolerance_pb": nominal_closure_tolerance,
                    "closure_tolerance_formula": (
                        "max(1e-11*max(abs(target),1), "
                        "16*float64_eps*abs(scale)*retained_raw_sumabsw_pb)"
                    ),
                },
                "truth_angular_weights": {
                    "angle_level": "LHE after independent Born projection",
                    "angle_units": "radians",
                    "angles": ["lhe_theta1", "lhe_phi1", "lhe_theta2", "lhe_phi2"],
                    "positive_lepton_convention": (
                        "Omega1 follows mu+ in Z1=mumu; "
                        "Omega2 follows e+ in Z2=ee"
                    ),
                    "spherical_harmonic_convention": (
                        "complex Condon-Shortley spherical harmonics"
                    ),
                    "exchange_symmetrization": (
                        "(Y_alpha(Omega1)Y_beta(Omega2)+"
                        "Y_alpha(Omega2)Y_beta(Omega1)) / "
                        "sqrt(2*(1+delta_alpha_beta))"
                    ),
                    "cross_section_expansion_normalization": (
                        "1/(4*pi); projector is 4*pi*Re(Y_plus conjugate)"
                    ),
                    "validity_branch": "truth_lhe_valid",
                    "invalid_policy": "NaN factors and weights",
                    "bare_basis_branch": (
                        "truth_h_<slug> = Re(symmetric spherical-harmonic "
                        "basis element conjugate)"
                    ),
                    "factor_definition": (
                        "4*pi*Re(symmetric spherical-harmonic basis element "
                        "conjugate)"
                    ),
                    "weight_formula": (
                        "weight_truth_<slug>_pb = weight_nominal_pb * "
                        "truth_factor_<slug>"
                    ),
                    "components": _component_metadata(),
                },
                "lhe_alternative_weights": {
                    "tree": LHE_WEIGHT_TREE_NAME if alternative_ids else None,
                    "ids": list(alternative_ids),
                    "preserved_unscaled": True,
                },
                "merge_code": _code_provenance(),
            }
            output_file[MERGE_METADATA_NAME] = json.dumps(
                _json_safe(merge_metadata), sort_keys=True, allow_nan=False
            )

        _verify_temporary_output(
            temporary,
            jobs=jobs,
            alternative_ids=alternative_ids,
            event_count=int(totals["event_count"]),
            raw_sumw=raw_sumw,
            raw_sumabsw=raw_sumabsw,
            merged_scale=merged_scale,
            nominal_target=pooled_cross_section,
            step_size=step_size,
        )
        for job in jobs:
            if sha256_file(job.path) != job.sha256:
                raise MergeError(f"input changed while it was being merged: {job.path}")
        assert temporary is not None
        _publish_output(temporary, output_path, overwrite)
        temporary = None
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    return {
        "output": str(output_path),
        "input_file_count": len(jobs),
        "event_count": int(totals["event_count"]),
        "merged_weight_scale": merged_scale,
        "effective_filtered_cross_section_pb": pooled_cross_section,
        "sumw_nominal_pb": nominal_sum.total,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="job analysis.root files or completed workflow job directories",
    )
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--step-size", default="50 MB", help="uproot chunk size")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if len(args.inputs) < 2:
        parser.error("at least two inputs are required")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    summary = merge_analysis_outputs(
        args.inputs,
        args.output,
        step_size=args.step_size,
        overwrite=args.overwrite,
    )
    print(
        f"Merged {summary['input_file_count']} jobs and {summary['event_count']} events "
        f"to {summary['output']} "
        f"(cross section={summary['effective_filtered_cross_section_pb']:.12g} pb, "
        f"scale={summary['merged_weight_scale']:.12g})"
    )


if __name__ == "__main__":
    main()
