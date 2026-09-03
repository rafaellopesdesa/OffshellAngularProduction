#!/usr/bin/env python3
"""Compose symmetric m=0 angular samples from LL, TT, TL, and LT outputs.

Each input is a campaign-level ROOT file produced by
``merge_analysis_outputs.py``.  Its normalized ``weight_nominal_pb`` branch is
preserved verbatim.  The output concatenates the four independent polarized
event streams and adds two signed angular-component weights whose sums are the
requested ``(0,0;2,0)`` and ``(2,0;2,0)`` coefficients, plus a direct
incoherent ``TL+LT`` weight.

The first polarization label always refers to ``Z1 -> mu+ mu-`` and the
second to ``Z2 -> e+ e-``.  Separate TL and LT files are mandatory: a coherent
``TL+LT`` matrix element contains an m=+/-1 interference that is not safe to
carry through angle-dependent acceptance as an incoherent mixed sample.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import uproot

REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT / "src", REPO_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from Merging.merge_analysis_outputs import (  # noqa: E402
    ANALYSIS_SCHEMA_VERSION,
    BASE_RUN_SCHEMA,
    EVENT_TREE_NAME,
    IDENTITY_BRANCHES,
    LHE_WEIGHT_TREE_NAME,
    MERGE_METADATA_NAME,
    MERGE_SCHEMA_VERSION,
    MERGE_SUMMARY_TREE_NAME,
    RUN_TREE_NAME,
    MergeError,
    ProvenanceError,
    SchemaError,
    _acquire_output_lock,
    _publish_output,
    _merge_summary_schema,
    _mean_mc_error,
    _output_event_schema,
    _physics_fingerprint,
    _temporary_output,
    _tree_schema,
    _weight_tree_schema,
    sha256_file,
)

COMPOSITION_SCHEMA_VERSION = 1
COMPOSITION_SUMMARY_NAME = "PolarizationCombinationSummary"
SOURCE_SUMMARY_NAME = "PolarizationSources"
COMPOSITION_METADATA_NAME = "polarization_combination_metadata"

POLARIZATION_CHANNELS = ("LL", "TT", "TL", "LT")
POLARIZATION_CHANNEL_CODES = {
    channel: code for code, channel in enumerate(POLARIZATION_CHANNELS)
}
VPOLAR_SAMPLE_CODES = {
    "LL": 10,
    "TT": 11,
    "TL": 12,
    "LT": 13,
}
VPOLAR_GENERATOR_BACKEND = "madgraph5-pythia8-vpolar-standalone"
COMPONENT_SLUGS = ("00_20", "20_20", "mixed_incoherent")
COMPONENT_LABELS = {
    "00_20": "(0,0;2,0)",
    "20_20": "(2,0;2,0)",
    "mixed_incoherent": "TL+LT (incoherent)",
}
COMPONENT_WEIGHT_BRANCHES = {
    "00_20": "weight_polcomb_00_20_pb",
    "20_20": "weight_polcomb_20_20_pb",
    "mixed_incoherent": "weight_mixed_incoherent_pb",
}

# For one Z decay, <sqrt(4*pi) Y_20> is -1/sqrt(5) for L and
# +1/(2*sqrt(5)) for T.  With the repository's exchange-symmetric basis,
# C_00;20=(a_1+a_2)/sqrt(2) and C_20;20=a_1*a_2.
POLARIZATION_COMBINATION_COEFFICIENTS: dict[str, dict[str, float]] = {
    "00_20": {
        "LL": -math.sqrt(2.0 / 5.0),
        "TT": 1.0 / math.sqrt(10.0),
        "TL": -1.0 / (2.0 * math.sqrt(10.0)),
        "LT": -1.0 / (2.0 * math.sqrt(10.0)),
    },
    "20_20": {
        "LL": 1.0 / 5.0,
        "TT": 1.0 / 20.0,
        "TL": -1.0 / 10.0,
        "LT": -1.0 / 10.0,
    },
    "mixed_incoherent": {
        "LL": 0.0,
        "TT": 0.0,
        "TL": 1.0,
        "LT": 1.0,
    },
}

COMPOSED_EVENT_BRANCHES: dict[str, np.dtype] = {
    "source_polarization_code": np.dtype("uint8"),
    "polarization_coefficient_00_20": np.dtype("float64"),
    "polarization_coefficient_20_20": np.dtype("float64"),
    "polarization_coefficient_mixed_incoherent": np.dtype("float64"),
    "weight_polcomb_00_20_pb": np.dtype("float64"),
    "weight_polcomb_20_20_pb": np.dtype("float64"),
    "weight_mixed_incoherent_pb": np.dtype("float64"),
}

_CHANNEL_VARIANT_FINGERPRINT_KEYS = {
    "sample",
    "sample_code",
    "provenance.generation.process",
    "provenance.lhe_contract.process",
    "provenance.alignment.process",
    "provenance.simulation.process",
    "provenance.generation.job_option_sha256",
    "provenance.alignment.job_option_sha256",
    "provenance.generation.run_number",
    "provenance.alignment.run_number",
}

_VPOLAR_REQUIRED_CHANNEL_GENERATION_KEYS = {
    "process",
    "run_number",
    "process_card_sha256",
}
_VPOLAR_REQUIRED_REALIZED_ARTIFACT_GENERATION_KEYS = {
    # These hashes prove which per-job artifacts were consumed, but the
    # artifacts intentionally encode seeds, event counts, process names, or
    # stochastic logs.  Require and validate them without treating equality as
    # a physics-compatibility condition.
    "run_card_sha256",
    "pythia_card_sha256",
    "madgraph_command_card_sha256",
    "generation_config_sha256",
    "shower_log_sha256",
}
_VPOLAR_REQUIRED_COMMON_GENERATION_KEYS = {
    "generator_backend",
    "ecm_energy_gev",
    "generator_mll_min_gev",
    "generator_mll_max_gev",
    "generator_m4l_min_gev",
    "generator_m4l_max_gev",
    "final_state",
    "full_amplitude",
    "photon_diagrams",
    "mixed_sample_definition",
    "madgraph_version",
    "pythia_version",
    "hepmc_version",
    "ufo_version",
    "ufo_sha256",
    "loop_filter_sha256",
    "loop_filter_patch_sha256",
    "installation_manifest_sha256",
    "madloop_card_sha256",
    "param_card_sha256",
    "run_generation_sha256",
    "lhe_contract_script_sha256",
    "alignment_script_sha256",
    "loop_reduction_backend",
    "loop_optimized_output",
    "madloop_reduction_lib",
    "ninja_enabled",
    "collier_enabled",
    "loop_output_dependencies",
    "pdf_set",
    "pdf_id",
    "shower_profile",
    "pythia_tune_pp",
    "pythia_pdf_pset",
    "alignment_contract",
    "lhe_event_id_contract",
}
_VPOLAR_SHA256_GENERATION_KEYS = {
    "ufo_sha256",
    "loop_filter_sha256",
    "loop_filter_patch_sha256",
    "installation_manifest_sha256",
    "process_card_sha256",
    "run_card_sha256",
    "param_card_sha256",
    "pythia_card_sha256",
    "madgraph_command_card_sha256",
    "generation_config_sha256",
    "shower_log_sha256",
    "madloop_card_sha256",
    "run_generation_sha256",
    "lhe_contract_script_sha256",
    "alignment_script_sha256",
}


class PolarizationCompositionError(MergeError):
    """Raised when polarized campaign outputs cannot be combined safely."""


class _CompensatedSum:
    """Chunk-pairwise float64 accumulator used for signed output diagnostics."""

    def __init__(self) -> None:
        self.total = 0.0
        self.correction = 0.0

    def add_array(self, values: np.ndarray) -> None:
        array = np.ravel(np.asarray(values, dtype=np.float64))
        if not np.all(np.isfinite(array)):
            raise PolarizationCompositionError(
                "non-finite value in normalization accumulator"
            )
        extended_total = np.sum(array, dtype=np.longdouble)
        if not np.isfinite(extended_total) or abs(extended_total) > np.finfo(
            np.float64
        ).max:
            raise PolarizationCompositionError("normalization accumulator overflowed")
        numeric = float(extended_total)
        adjusted = numeric - self.correction
        updated = self.total + adjusted
        correction = (updated - self.total) - adjusted
        if not all(math.isfinite(value) for value in (adjusted, updated, correction)):
            raise PolarizationCompositionError("normalization accumulator overflowed")
        self.correction = correction
        self.total = updated


@dataclass(frozen=True)
class PolarizedSource:
    """Validated metadata and schemas for one campaign-level source."""

    channel: str
    path: Path
    sha256: str
    metadata: dict[str, Any]
    event_schema: dict[str, np.dtype]
    run_schema: dict[str, np.dtype]
    weight_schema: dict[str, np.dtype] | None
    event_count: int
    run_count: int
    analysis_schema_version: int
    campaign_id: int
    sample_code: int
    source_job_count: int
    cross_section_pb: float
    cross_section_mc_error_pb: float
    nominal_sumabsw_pb: float
    alternative_weight_ids: tuple[str, ...]
    job_keys: tuple[tuple[int, int, int], ...]
    polarization_contract: dict[str, str]
    vpolar_fingerprint: dict[str, Any]
    process_card_sha256: str


@dataclass
class _ComponentAccumulator:
    sumw: _CompensatedSum
    sumw2: _CompensatedSum
    sumabsw: _CompensatedSum
    positive: int = 0
    negative: int = 0
    zero: int = 0

    @classmethod
    def create(cls) -> "_ComponentAccumulator":
        return cls(_CompensatedSum(), _CompensatedSum(), _CompensatedSum())

    def add(self, values: np.ndarray) -> None:
        values = np.asarray(values, dtype=np.float64)
        self.sumw.add_array(values)
        with np.errstate(over="ignore", invalid="ignore"):
            squared = values * values
        self.sumw2.add_array(squared)
        self.sumabsw.add_array(np.abs(values))
        self.positive += int(np.count_nonzero(values > 0.0))
        self.negative += int(np.count_nonzero(values < 0.0))
        self.zero += int(np.count_nonzero(values == 0.0))


def _dtype_signature(dtype: object) -> tuple[str, int, tuple[int, ...]]:
    parsed = np.dtype(dtype)
    return parsed.base.kind, parsed.base.itemsize, parsed.shape


def _schemas_equal(
    first: Mapping[str, np.dtype], second: Mapping[str, np.dtype]
) -> bool:
    return tuple(first) == tuple(second) and all(
        _dtype_signature(first[name]) == _dtype_signature(second[name])
        for name in first
    )


def _require_branches(
    schema: Mapping[str, np.dtype], required: Sequence[str], *, label: str
) -> None:
    missing = sorted(set(required).difference(schema))
    if missing:
        raise SchemaError(f"{label} is missing required branches: {missing}")


def _json_object(root_file: Any, name: str, path: Path) -> dict[str, Any]:
    if name not in root_file:
        raise SchemaError(f"{path} does not contain {name}")
    try:
        value = json.loads(str(root_file[name]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"{path}: {name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProvenanceError(f"{path}: {name} must contain a JSON object")
    return value


def _scalar_summary(tree: Any, path: Path) -> dict[str, int | float]:
    if int(tree.num_entries) != 1:
        raise SchemaError(f"{path}: MergeSummary must contain exactly one row")
    arrays = tree.arrays(library="np", how=dict)
    result: dict[str, int | float] = {}
    for name, array in arrays.items():
        values = np.asarray(array)
        if values.shape != (1,) or values.dtype.kind not in "uifb":
            raise SchemaError(f"{path}: MergeSummary.{name} is not numeric scalar")
        scalar = values[0].item()
        result[str(name)] = (
            int(scalar) if values.dtype.kind in "uib" else float(scalar)
        )
    return result


def _normalise_channel(value: object) -> str:
    if not isinstance(value, str):
        raise ProvenanceError("polarization channel must be a string")
    text = value.strip().upper().replace("0", "L")
    aliases = {
        "LL": "LL",
        "TT": "TT",
        "TL": "TL",
        "LT": "LT",
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ProvenanceError(f"unsupported polarization channel {value!r}") from exc


def _polarization_contract_from_generation(
    generation: Mapping[str, Any], path: Path
) -> dict[str, str]:
    raw = generation.get("polarization")
    if isinstance(raw, Mapping):
        required = (
            "channel",
            "z1_decay",
            "z2_decay",
            "frame",
            "interference",
            "madgraph_me_frame",
        )
        missing = [name for name in required if name not in raw]
        if missing:
            raise ProvenanceError(
                f"{path}: generation polarization contract is missing {missing}"
            )
        non_strings = [name for name in required if not isinstance(raw[name], str)]
        if non_strings:
            raise ProvenanceError(
                f"{path}: polarization fields must be strings: {non_strings}"
            )
        contract = {name: raw[name].strip() for name in required}
    else:
        # The standalone VPolar generation backend records this contract as
        # flat generation fields so shell-produced key/value metadata remains
        # easy to audit.  The nested form above is also accepted for fixtures
        # and future structured producers.
        channel_key = (
            "polarization_component"
            if "polarization_component" in generation
            else "polarization"
        )
        flat_keys = {
            "channel": channel_key,
            "z1_decay": "polarization_z1_decay",
            "z2_decay": "polarization_z2_decay",
            "frame": "polarization_frame",
            "interference": "mixed_polarization_interference",
            "madgraph_me_frame": "madgraph_me_frame",
        }
        missing = [source for source in flat_keys.values() if source not in generation]
        if missing:
            raise ProvenanceError(
                f"{path}: generation metadata is missing polarization fields {missing}"
            )
        non_strings = [
            source
            for source in flat_keys.values()
            if not isinstance(generation[source], str)
        ]
        if non_strings:
            raise ProvenanceError(
                f"{path}: polarization fields must be strings: {non_strings}"
            )
        contract = {
            target: generation[source].strip()
            for target, source in flat_keys.items()
        }
    contract["channel"] = _normalise_channel(contract["channel"])
    z1_decay = contract["z1_decay"].lower().replace("+", "").replace("-", "")
    z2_decay = contract["z2_decay"].lower().replace("+", "").replace("-", "")
    if z1_decay not in {"mumu", "muonmuon"}:
        raise ProvenanceError(f"{path}: Z1 must be the dimuon system")
    if z2_decay not in {"ee", "electronelectron"}:
        raise ProvenanceError(f"{path}: Z2 must be the dielectron system")
    contract["z1_decay"] = "mumu"
    contract["z2_decay"] = "ee"
    interference = contract["interference"].lower()
    allowed_interference = {"none", "absent", "incoherent", "excluded"}
    if contract["channel"] in {"LL", "TT"}:
        allowed_interference.add("not_applicable")
    if interference not in allowed_interference:
        raise ProvenanceError(
            f"{path}: source is not an interference-free polarization channel"
        )
    contract["interference"] = (
        "not_applicable" if interference == "not_applicable" else "excluded"
    )
    if not contract["frame"]:
        raise ProvenanceError(f"{path}: empty polarization frame")
    allowed_frames = {
        "four_lepton_rest_frame",
        "four_lepton_rest_frame_me_frame_3_4_5_6",
    }
    if contract["frame"] not in allowed_frames:
        raise ProvenanceError(
            f"{path}: polarization frame is not the four-lepton rest frame"
        )
    contract["frame"] = "four_lepton_rest_frame"
    me_frame = (
        contract["madgraph_me_frame"]
        .replace("[", "")
        .replace("]", "")
        .replace(" ", "")
    )
    if me_frame != "3,4,5,6":
        raise ProvenanceError(f"{path}: MadGraph me_frame must be [3,4,5,6]")
    contract["madgraph_me_frame"] = me_frame
    return contract


def _vpolar_generation_fingerprints(
    generation: Mapping[str, Any], path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    required = (
        _VPOLAR_REQUIRED_COMMON_GENERATION_KEYS
        | _VPOLAR_REQUIRED_CHANNEL_GENERATION_KEYS
        | _VPOLAR_REQUIRED_REALIZED_ARTIFACT_GENERATION_KEYS
    )
    missing = sorted(required.difference(generation))
    if missing:
        raise ProvenanceError(
            f"{path}: VPolar generation metadata is missing invariants {missing}"
        )
    if str(generation["final_state"]).replace(" ", "") != "e+e-mu+mu-":
        raise ProvenanceError(f"{path}: VPolar source is not exclusive 2e2mu")
    if generation["full_amplitude"] is not True:
        raise ProvenanceError(f"{path}: VPolar source does not use the full amplitude")
    if generation["photon_diagrams"] is not False:
        raise ProvenanceError(f"{path}: VPolar source includes photon diagrams")
    for key in (
        "ecm_energy_gev",
        "generator_mll_min_gev",
        "generator_mll_max_gev",
        "generator_m4l_min_gev",
        "generator_m4l_max_gev",
    ):
        try:
            numeric = float(generation[key])
        except (TypeError, ValueError) as exc:
            raise ProvenanceError(f"{path}: invalid VPolar invariant {key}") from exc
        if not math.isfinite(numeric):
            raise ProvenanceError(f"{path}: non-finite VPolar invariant {key}")
    for key in _VPOLAR_SHA256_GENERATION_KEYS:
        value = generation.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ProvenanceError(f"{path}: invalid VPolar SHA-256 field {key}")
    reduction_contract = {
        "loop_reduction_backend": "CutTools",
        "loop_optimized_output": True,
        "madloop_reduction_lib": 1,
        "ninja_enabled": False,
        "collier_enabled": False,
        "loop_output_dependencies": "external",
    }
    observed_reduction = {
        key: generation[key] for key in reduction_contract
    }
    if observed_reduction != reduction_contract:
        raise ProvenanceError(
            f"{path}: VPolar source does not use optimized CutTools-only reduction"
        )

    # Compare an explicit, versioned contract rather than every metadata key.
    # Full per-job provenance is retained in the output metadata, while hashes
    # of realized command/config/log artifacts are deliberately absent here:
    # they change with seeds, event counts, and source-channel names.
    common = {
        key: generation[key]
        for key in sorted(_VPOLAR_REQUIRED_COMMON_GENERATION_KEYS)
    }
    channel_static = {
        **common,
        **{
            key: generation[key]
            for key in sorted(_VPOLAR_REQUIRED_CHANNEL_GENERATION_KEYS)
        },
    }
    return channel_static, common


def _embedded_polarization_contract(
    metadata: Mapping[str, Any], path: Path, channel: str
) -> tuple[dict[str, str], dict[str, Any], str]:
    inputs = metadata.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ProvenanceError(f"{path}: merge_metadata has no source-job inputs")
    expected_sample = f"vpolar_{channel}"
    expected_sample_code = VPOLAR_SAMPLE_CODES[channel]
    contracts: list[dict[str, str]] = []
    channel_fingerprints: list[dict[str, Any]] = []
    common_fingerprints: list[dict[str, Any]] = []
    for source in inputs:
        if not isinstance(source, Mapping):
            raise ProvenanceError(f"{path}: malformed merge_metadata input")
        analysis = source.get("analysis_metadata")
        if not isinstance(analysis, Mapping):
            raise ProvenanceError(f"{path}: input lacks embedded analysis metadata")
        if analysis.get("sample") != expected_sample:
            raise ProvenanceError(
                f"{path}: embedded source sample must be {expected_sample}"
            )
        try:
            embedded_sample_code = int(analysis["sample_code"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProvenanceError(
                f"{path}: embedded source has invalid sample code"
            ) from exc
        if embedded_sample_code != expected_sample_code:
            raise ProvenanceError(
                f"{path}: {expected_sample} must use sample code "
                f"{expected_sample_code}"
            )
        provenance = analysis.get("provenance")
        generation = (
            provenance.get("generation")
            if isinstance(provenance, Mapping)
            else None
        )
        if not isinstance(generation, Mapping):
            raise ProvenanceError(f"{path}: input lacks generation provenance")
        if generation.get("generator_backend") != VPOLAR_GENERATOR_BACKEND:
            raise ProvenanceError(
                f"{path}: {expected_sample} must use generator backend "
                f"{VPOLAR_GENERATOR_BACKEND}"
            )
        for stage in ("generation", "lhe_contract", "alignment", "simulation"):
            stage_metadata = provenance.get(stage)
            if not isinstance(stage_metadata, Mapping):
                raise ProvenanceError(f"{path}: input lacks {stage} provenance")
            if stage_metadata.get("process") != expected_sample:
                raise ProvenanceError(
                    f"{path}: embedded {stage} process must be {expected_sample}"
                )
        lhe_contract = provenance["lhe_contract"]
        if (
            lhe_contract.get("normalization_contract")
            != "idwtup-minus4-sample-mean-v1"
        ):
            raise ProvenanceError(
                f"{path}: unexpected polarized LHE normalization contract"
            )
        if lhe_contract.get("lhe_weighting_strategy") != -4:
            raise ProvenanceError(
                f"{path}: polarized LHE weighting strategy must be IDWTUP=-4"
            )
        lhe_init = lhe_contract.get("lhe_init")
        if not isinstance(lhe_init, Mapping) or lhe_init.get("idwtup") != -4:
            raise ProvenanceError(
                f"{path}: polarized LHE init must declare IDWTUP=-4"
            )
        contracts.append(_polarization_contract_from_generation(generation, path))
        channel_fingerprint, common_fingerprint = _vpolar_generation_fingerprints(
            generation, path
        )
        channel_fingerprints.append(channel_fingerprint)
        common_fingerprints.append(common_fingerprint)
    if any(contract != contracts[0] for contract in contracts[1:]):
        raise ProvenanceError(
            f"{path}: source jobs disagree on the polarization contract"
        )
    if any(
        fingerprint != channel_fingerprints[0]
        for fingerprint in channel_fingerprints[1:]
    ):
        raise ProvenanceError(
            f"{path}: source jobs disagree on VPolar generation invariants"
        )
    if any(
        fingerprint != common_fingerprints[0]
        for fingerprint in common_fingerprints[1:]
    ):
        raise ProvenanceError(
            f"{path}: source jobs disagree on common VPolar generation invariants"
        )
    return (
        contracts[0],
        common_fingerprints[0],
        str(channel_fingerprints[0]["process_card_sha256"]),
    )


def _close(observed: float, expected: float, *, scale: float, label: str) -> None:
    if not all(math.isfinite(value) for value in (observed, expected, scale)):
        raise PolarizationCompositionError(
            f"{label} has a non-finite normalization aggregate"
        )
    tolerance = max(
        1.0e-11 * max(abs(expected), 1.0),
        32.0 * np.finfo(np.float64).eps * max(scale, 1.0),
    )
    if abs(observed - expected) > tolerance:
        raise PolarizationCompositionError(
            f"{label} mismatch: observed={observed!r}, expected={expected!r}, "
            f"tolerance={tolerance!r}"
        )


def _optional_close(observed: float, expected: float, *, label: str) -> None:
    if math.isnan(expected):
        if not math.isnan(observed):
            raise PolarizationCompositionError(f"{label} must be NaN")
        return
    _close(observed, expected, scale=max(abs(expected), 1.0), label=label)


def _json_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ProvenanceError(f"{label} must be an integer")
    return int(value)


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise ProvenanceError(f"{label} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ProvenanceError(f"{label} must be finite")
    return numeric


def _finite_or_nan_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise ProvenanceError(f"{label} must be numeric")
    numeric = float(value)
    if math.isinf(numeric):
        raise ProvenanceError(f"{label} must not be infinite")
    return numeric


def _pool_source_normalization(
    run_arrays: Mapping[str, np.ndarray],
    inputs: Sequence[Mapping[str, Any]],
    path: Path,
) -> dict[str, int | float]:
    integer_pairs = {
        "normalization_generated_lhe_events": "generated_lhe_events",
        "normalization_accepted_lhe_events": "accepted_lhe_events",
    }
    float_pairs = {
        "normalization_sumw_generated_pb": "sumw_generated",
        "normalization_sumw2_generated_pb2": "sumw2_generated",
        "normalization_sumw_accepted_pb": "sumw_accepted",
        "normalization_sumw2_accepted_pb2": "sumw2_accepted",
        "inclusive_lhe_cross_section_pb": "inclusive_cross_section_pb",
        "effective_filtered_cross_section_pb": "filtered_cross_section_pb",
    }
    optional_error_pairs = {
        "inclusive_lhe_cross_section_mc_error_pb": (
            "inclusive_cross_section_mc_error_pb"
        ),
        "effective_filtered_cross_section_mc_error_pb": (
            "filtered_cross_section_mc_error_pb"
        ),
    }
    sumabsw_generated: list[float] = []
    sumabsw_accepted: list[float] = []
    for index, source in enumerate(inputs):
        analysis = source.get("analysis_metadata")
        provenance = analysis.get("provenance") if isinstance(analysis, Mapping) else None
        contract = (
            provenance.get("lhe_contract")
            if isinstance(provenance, Mapping)
            else None
        )
        if not isinstance(contract, Mapping):
            raise ProvenanceError(f"{path}: input {index} lacks an LHE contract")
        for run_name, metadata_name in integer_pairs.items():
            embedded = _json_integer(
                contract.get(metadata_name),
                label=f"{path}: input {index} LHE {metadata_name}",
            )
            if int(run_arrays[run_name][index]) != embedded:
                raise ProvenanceError(
                    f"{path}: Runs.{run_name} disagrees with input {index}"
                )
        for run_name, metadata_name in float_pairs.items():
            embedded = _finite_number(
                contract.get(metadata_name),
                label=f"{path}: input {index} LHE {metadata_name}",
            )
            _close(
                float(run_arrays[run_name][index]),
                embedded,
                scale=max(abs(embedded), 1.0),
                label=f"{path}: Runs.{run_name} input {index}",
            )
        for run_name, metadata_name in optional_error_pairs.items():
            embedded = _finite_or_nan_number(
                contract.get(metadata_name),
                label=f"{path}: input {index} LHE {metadata_name}",
            )
            _optional_close(
                float(run_arrays[run_name][index]),
                embedded,
                label=f"{path}: Runs.{run_name} input {index}",
            )
        sumabsw_generated.append(
            _finite_number(
                contract.get("sumabsw_generated"),
                label=f"{path}: input {index} LHE sumabsw_generated",
            )
        )
        sumabsw_accepted.append(
            _finite_number(
                contract.get("sumabsw_accepted"),
                label=f"{path}: input {index} LHE sumabsw_accepted",
            )
        )

    generated = sum(int(value) for value in run_arrays["normalization_generated_lhe_events"])
    accepted = sum(int(value) for value in run_arrays["normalization_accepted_lhe_events"])
    sumw_generated = math.fsum(
        float(value) for value in run_arrays["normalization_sumw_generated_pb"]
    )
    sumw2_generated = math.fsum(
        float(value) for value in run_arrays["normalization_sumw2_generated_pb2"]
    )
    sumw_accepted = math.fsum(
        float(value) for value in run_arrays["normalization_sumw_accepted_pb"]
    )
    sumw2_accepted = math.fsum(
        float(value) for value in run_arrays["normalization_sumw2_accepted_pb2"]
    )
    sumabs_generated = math.fsum(sumabsw_generated)
    sumabs_accepted = math.fsum(sumabsw_accepted)
    aggregates = (
        sumw_generated,
        sumw2_generated,
        sumw_accepted,
        sumw2_accepted,
        sumabs_generated,
        sumabs_accepted,
    )
    if not all(math.isfinite(value) for value in aggregates):
        raise PolarizationCompositionError(f"{path}: non-finite pooled normalization")
    if generated <= 0 or not 0 <= accepted <= generated:
        raise PolarizationCompositionError(f"{path}: invalid pooled event counts")
    if (
        sumw2_generated < 0.0
        or sumw2_accepted < 0.0
        or sumabs_generated < abs(sumw_generated)
        or sumabs_accepted < abs(sumw_accepted)
    ):
        raise PolarizationCompositionError(f"{path}: invalid pooled weight moments")
    inclusive = sumw_generated / generated
    filtered = sumw_accepted / generated
    return {
        "normalization_generated_lhe_events": generated,
        "normalization_accepted_lhe_events": accepted,
        "normalization_sumw_generated_pb": sumw_generated,
        "normalization_sumw2_generated_pb2": sumw2_generated,
        "normalization_sumabsw_generated_pb": sumabs_generated,
        "normalization_sumw_accepted_pb": sumw_accepted,
        "normalization_sumw2_accepted_pb2": sumw2_accepted,
        "normalization_sumabsw_accepted_pb": sumabs_accepted,
        "inclusive_lhe_cross_section_pb": inclusive,
        "inclusive_lhe_cross_section_mc_error_pb": _mean_mc_error(
            sumw_generated, sumw2_generated, generated, f"{path}: inclusive"
        ),
        "effective_filtered_cross_section_pb": filtered,
        "effective_filtered_cross_section_mc_error_pb": _mean_mc_error(
            sumw_accepted, sumw2_accepted, generated, f"{path}: filtered"
        ),
        "phase_space_count_efficiency": accepted / generated,
        "phase_space_signed_efficiency": (
            sumw_accepted / sumw_generated if sumw_generated != 0.0 else math.nan
        ),
        "phase_space_absolute_efficiency": (
            sumabs_accepted / sumabs_generated
            if sumabs_generated != 0.0
            else math.nan
        ),
    }
def _alternative_ids(metadata: Mapping[str, Any], path: Path) -> tuple[str, ...]:
    raw = metadata.get("lhe_alternative_weights")
    if not isinstance(raw, Mapping):
        raise ProvenanceError(f"{path}: missing alternative-weight metadata")
    ids = raw.get("ids")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ProvenanceError(f"{path}: invalid alternative-weight ID list")
    result = tuple(ids)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ProvenanceError(
            f"{path}: alternative-weight IDs must be unique lexicographic order"
        )
    expected_tree = LHE_WEIGHT_TREE_NAME if result else None
    if raw.get("tree") != expected_tree:
        raise ProvenanceError(
            f"{path}: alternative-weight metadata has the wrong tree"
        )
    if result and raw.get("preserved_unscaled") is not True:
        raise ProvenanceError(
            f"{path}: alternative weights are not declared unscaled"
        )
    return result


def _inspect_source(path: Path, channel: str, *, step_size: str) -> PolarizedSource:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"polarized input is not a nonempty ROOT file: {path}")
    input_sha256 = sha256_file(path)
    with uproot.open(path) as root_file:
        required_keys = {
            EVENT_TREE_NAME,
            RUN_TREE_NAME,
            MERGE_SUMMARY_TREE_NAME,
            MERGE_METADATA_NAME,
        }
        missing = sorted(required_keys.difference(root_file.keys(cycle=False)))
        if missing:
            raise SchemaError(f"{path} is not a campaign merge; missing {missing}")
        unexpected = sorted(
            set(root_file.keys(cycle=False))
            .difference(required_keys)
            .difference({LHE_WEIGHT_TREE_NAME})
        )
        if unexpected:
            raise SchemaError(f"{path}: unexpected ROOT objects {unexpected}")
        if COMPOSITION_METADATA_NAME in root_file:
            raise SchemaError(f"{path} is already a polarization composition")

        events = root_file[EVENT_TREE_NAME]
        runs = root_file[RUN_TREE_NAME]
        summary_tree = root_file[MERGE_SUMMARY_TREE_NAME]
        if not all(
            getattr(tree, "classname", "") == "TTree"
            for tree in (events, runs, summary_tree)
        ):
            raise SchemaError(f"{path}: Events, Runs, and MergeSummary must be TTrees")
        event_schema = _tree_schema(events)
        run_schema = _tree_schema(runs)
        summary_schema = _tree_schema(summary_tree)
        if not _schemas_equal(event_schema, _output_event_schema()):
            raise SchemaError(f"{path}: Events does not use the current merge schema")
        if not _schemas_equal(run_schema, BASE_RUN_SCHEMA):
            raise SchemaError(f"{path}: Runs does not use the current merge schema")
        if not _schemas_equal(summary_schema, _merge_summary_schema()):
            raise SchemaError(
                f"{path}: MergeSummary does not use the current merge schema"
            )
        _require_branches(
            event_schema,
            (*IDENTITY_BRANCHES, "weight_lhe", "weight_nominal_pb"),
            label=f"{path}: Events",
        )
        _require_branches(run_schema, tuple(BASE_RUN_SCHEMA), label=f"{path}: Runs")
        if set(COMPOSED_EVENT_BRANCHES).intersection(event_schema):
            raise SchemaError(f"{path}: Events already has polarization-composition branches")

        metadata = _json_object(root_file, MERGE_METADATA_NAME, path)
        summary = _scalar_summary(summary_tree, path)
        event_count = int(events.num_entries)
        if event_count <= 0 or int(summary.get("event_count", -1)) != event_count:
            raise PolarizationCompositionError(
                f"{path}: Events count disagrees with MergeSummary"
            )
        if int(runs.num_entries) <= 0:
            raise SchemaError(f"{path}: Runs is empty")

        try:
            merge_schema_version = _json_integer(
                metadata["schema_version"], label=f"{path}: merge schema version"
            )
            campaign_id = _json_integer(
                metadata["campaign_id"], label=f"{path}: campaign ID"
            )
            sample_code = _json_integer(
                metadata["sample_code"], label=f"{path}: sample code"
            )
            analysis_schema_version = _json_integer(
                metadata["analysis_schema_version"],
                label=f"{path}: analysis schema version",
            )
            source_job_count = _json_integer(
                summary["source_job_count"], label=f"{path}: source-job count"
            )
            declared_cross_section = _finite_number(
                summary["effective_filtered_cross_section_pb"],
                label=f"{path}: declared filtered cross section",
            )
            declared_cross_section_error = _finite_or_nan_number(
                summary["effective_filtered_cross_section_mc_error_pb"],
                label=f"{path}: declared filtered cross-section error",
            )
        except KeyError as exc:
            raise ProvenanceError(f"{path}: incomplete campaign merge metadata") from exc
        if merge_schema_version != MERGE_SCHEMA_VERSION:
            raise ProvenanceError(f"{path}: unsupported merge metadata schema version")
        if analysis_schema_version != ANALYSIS_SCHEMA_VERSION:
            raise ProvenanceError(f"{path}: unsupported analysis schema version")
        if int(summary.get("schema_version", -1)) != MERGE_SCHEMA_VERSION:
            raise ProvenanceError(f"{path}: unsupported MergeSummary schema version")
        if int(summary.get("analysis_schema_version", -1)) != analysis_schema_version:
            raise ProvenanceError(
                f"{path}: analysis schema version disagrees with MergeSummary"
            )
        if int(summary.get("campaign_id", -1)) != campaign_id:
            raise ProvenanceError(f"{path}: campaign ID disagrees with MergeSummary")
        if int(summary.get("sample_code", -1)) != sample_code:
            raise ProvenanceError(f"{path}: sample code disagrees with MergeSummary")
        expected_sample = f"vpolar_{channel}"
        sample_name = str(metadata.get("sample", ""))
        if sample_name != expected_sample:
            raise ProvenanceError(
                f"{path}: --{channel.lower()} requires sample {expected_sample}, "
                f"not {sample_name!r}"
            )
        expected_sample_code = VPOLAR_SAMPLE_CODES[channel]
        if sample_code != expected_sample_code:
            raise ProvenanceError(
                f"{path}: {expected_sample} must use sample code "
                f"{expected_sample_code}, not {sample_code}"
            )
        if declared_cross_section <= 0.0:
            raise PolarizationCompositionError(
                f"{path}: polarized source cross section must be finite and positive"
            )
        if not (
            math.isnan(declared_cross_section_error)
            or declared_cross_section_error >= 0.0
        ):
            raise PolarizationCompositionError(f"{path}: invalid cross-section MC error")
        if source_job_count != int(runs.num_entries):
            raise PolarizationCompositionError(
                f"{path}: source-job count disagrees with Runs"
            )
        inputs = metadata.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != source_job_count:
            raise ProvenanceError(
                f"{path}: embedded input count disagrees with source-job count"
            )
        if int(summary.get("input_file_count", -1)) != source_job_count:
            raise ProvenanceError(
                f"{path}: MergeSummary input count disagrees with source jobs"
            )
        if any(not isinstance(embedded, Mapping) for embedded in inputs):
            raise ProvenanceError(f"{path}: malformed embedded merge input")

        run_arrays = runs.arrays(library="np", how=dict)
        if not np.all(run_arrays["schema_version"] == ANALYSIS_SCHEMA_VERSION):
            raise ProvenanceError(f"{path}: Runs has an unsupported schema version")
        if not np.all(run_arrays["campaign_id"] == campaign_id):
            raise ProvenanceError(f"{path}: Runs campaign ID disagrees with metadata")
        if not np.all(run_arrays["sample_code"] == sample_code):
            raise ProvenanceError(f"{path}: Runs sample code disagrees with metadata")
        if sum(int(value) for value in run_arrays["event_count"]) != event_count:
            raise ProvenanceError(f"{path}: Runs event counts disagree with Events")
        for index, (embedded, run_job_id, run_event_count) in enumerate(
            zip(inputs, run_arrays["job_id"], run_arrays["event_count"])
        ):
            embedded_job_id = _json_integer(
                embedded.get("job_id"),
                label=f"{path}: embedded input {index} job ID",
            )
            embedded_event_count = _json_integer(
                embedded.get("event_count"),
                label=f"{path}: embedded input {index} event count",
            )
            if (
                embedded_job_id != int(run_job_id)
                or embedded_event_count != int(run_event_count)
            ):
                raise ProvenanceError(
                    f"{path}: embedded input {index} disagrees with Runs ordering"
                )

        pooled_normalization = _pool_source_normalization(
            run_arrays,
            inputs,
            path,
        )
        for name, expected in pooled_normalization.items():
            if name not in summary:
                raise ProvenanceError(f"{path}: MergeSummary is missing {name}")
            if isinstance(expected, int):
                observed_integer = _json_integer(
                    summary[name], label=f"{path}: MergeSummary.{name}"
                )
                if observed_integer != expected:
                    raise PolarizationCompositionError(
                        f"{path}: MergeSummary.{name} disagrees with Runs"
                    )
            else:
                observed_number = _finite_or_nan_number(
                    summary[name], label=f"{path}: MergeSummary.{name}"
                )
                _optional_close(
                    observed_number,
                    float(expected),
                    label=f"{path}: MergeSummary.{name}",
                )
        cross_section = float(
            pooled_normalization["effective_filtered_cross_section_pb"]
        )
        cross_section_error = float(
            pooled_normalization["effective_filtered_cross_section_mc_error_pb"]
        )
        _close(
            declared_cross_section,
            cross_section,
            scale=max(abs(cross_section), 1.0),
            label=f"{path}: declared filtered cross section",
        )
        _optional_close(
            declared_cross_section_error,
            cross_section_error,
            label=f"{path}: declared filtered cross-section error",
        )

        (
            declared_contract,
            vpolar_fingerprint,
            process_card_sha256,
        ) = _embedded_polarization_contract(metadata, path, channel)
        if declared_contract["channel"] != channel:
            raise ProvenanceError(
                f"{path}: --{channel.lower()} input declares "
                f"{declared_contract['channel']}"
            )

        alternative_ids = _alternative_ids(metadata, path)
        has_weights = LHE_WEIGHT_TREE_NAME in root_file
        if has_weights != bool(alternative_ids):
            raise SchemaError(f"{path}: LHEWeights presence disagrees with metadata")
        weight_schema = (
            _tree_schema(root_file[LHE_WEIGHT_TREE_NAME]) if has_weights else None
        )
        if weight_schema is not None and not _schemas_equal(
            weight_schema, _weight_tree_schema(len(alternative_ids))
        ):
            raise SchemaError(f"{path}: LHEWeights schema is inconsistent with IDs")
        if has_weights and int(root_file[LHE_WEIGHT_TREE_NAME].num_entries) != event_count:
            raise SchemaError(f"{path}: LHEWeights is not one-to-one with Events")

        nominal = _CompensatedSum()
        nominal_abs = _CompensatedSum()
        raw_accumulator = _ComponentAccumulator.create()
        try:
            source_scale = _finite_number(
                summary["merged_weight_scale"],
                label=f"{path}: merged nominal scale",
            )
        except KeyError as exc:
            raise ProvenanceError(f"{path}: missing merged nominal scale") from exc
        if source_scale == 0.0:
            raise PolarizationCompositionError(f"{path}: invalid merged nominal scale")
        seen = 0
        event_expressions = (
            *IDENTITY_BRANCHES,
            "weight_lhe",
            "weight_nominal_pb",
        )

        def inspect_chunk(
            arrays: Mapping[str, np.ndarray],
            weight_arrays: Mapping[str, np.ndarray] | None = None,
        ) -> None:
            nonlocal seen
            source_nominal = np.asarray(arrays["weight_nominal_pb"], dtype=np.float64)
            source_raw = np.asarray(arrays["weight_lhe"], dtype=np.float64)
            if not np.all(np.isfinite(source_nominal)) or not np.all(np.isfinite(source_raw)):
                raise PolarizationCompositionError(f"{path}: non-finite event weight")
            if not np.array_equal(source_nominal, source_raw * source_scale):
                raise PolarizationCompositionError(
                    f"{path}: weight_nominal_pb is not weight_lhe times the merged scale"
                )
            if not np.all(np.asarray(arrays["campaign_id"]) == campaign_id):
                raise ProvenanceError(f"{path}: Events campaign ID disagrees with metadata")
            if not np.all(np.asarray(arrays["sample_code"]) == sample_code):
                raise ProvenanceError(f"{path}: Events sample code disagrees with metadata")
            if weight_arrays is not None:
                for name in IDENTITY_BRANCHES:
                    if not np.array_equal(weight_arrays[name], arrays[name]):
                        raise SchemaError(
                            f"{path}: LHEWeights.{name} is not aligned with Events"
                        )
                alternative_values = np.asarray(
                    weight_arrays["values"], dtype=np.float64
                )
                if alternative_values.shape != (
                    len(source_nominal),
                    len(alternative_ids),
                ) or not np.all(np.isfinite(alternative_values)):
                    raise SchemaError(f"{path}: invalid LHEWeights.values payload")
            nominal.add_array(source_nominal)
            nominal_abs.add_array(np.abs(source_nominal))
            raw_accumulator.add(source_raw)
            seen += len(source_nominal)

        if has_weights:
            weight_tree = root_file[LHE_WEIGHT_TREE_NAME]
            for weight_arrays in weight_tree.iterate(
                expressions=(*IDENTITY_BRANCHES, "values"),
                step_size=step_size,
                library="np",
                how=dict,
            ):
                size = len(weight_arrays["campaign_id"])
                event_arrays = events.arrays(
                    expressions=event_expressions,
                    entry_start=seen,
                    entry_stop=seen + size,
                    library="np",
                    how=dict,
                )
                inspect_chunk(event_arrays, weight_arrays)
        else:
            for event_arrays in events.iterate(
                expressions=event_expressions,
                step_size=step_size,
                library="np",
                how=dict,
            ):
                inspect_chunk(event_arrays)
        if seen != event_count:
            raise PolarizationCompositionError(f"{path}: failed to visit all Events")
        run_raw_totals = {
            "sumw": math.fsum(float(value) for value in run_arrays["sumw"]),
            "sumw2": math.fsum(float(value) for value in run_arrays["sumw2"]),
            "sumabsw": math.fsum(float(value) for value in run_arrays["sumabsw"]),
        }
        observed_raw_totals = {
            "sumw": raw_accumulator.sumw.total,
            "sumw2": raw_accumulator.sumw2.total,
            "sumabsw": raw_accumulator.sumabsw.total,
        }
        for name, observed in observed_raw_totals.items():
            _close(
                observed,
                run_raw_totals[name],
                scale=max(abs(run_raw_totals["sumabsw"]), 1.0),
                label=f"{path}: retained raw {name} against Runs",
            )
        count_pairs = {
            "positive_weight_count": raw_accumulator.positive,
            "negative_weight_count": raw_accumulator.negative,
            "zero_weight_count": raw_accumulator.zero,
        }
        for name, observed in count_pairs.items():
            run_count = sum(int(value) for value in run_arrays[name])
            summary_count = _json_integer(
                summary[name], label=f"{path}: MergeSummary.{name}"
            )
            if observed != run_count or observed != summary_count:
                raise PolarizationCompositionError(
                    f"{path}: {name} disagrees across Events, Runs, and summary"
                )
        raw_summary_names = {
            "retained_raw_sumw_pb": "sumw",
            "retained_raw_sumw2_pb2": "sumw2",
            "retained_raw_sumabsw_pb": "sumabsw",
        }
        for summary_name, raw_name in raw_summary_names.items():
            observed = _finite_number(
                summary[summary_name], label=f"{path}: MergeSummary.{summary_name}"
            )
            _close(
                observed,
                observed_raw_totals[raw_name],
                scale=max(observed_raw_totals["sumabsw"], 1.0),
                label=f"{path}: MergeSummary.{summary_name}",
            )
        if raw_accumulator.sumw.total == 0.0 or (
            cross_section * raw_accumulator.sumw.total < 0.0
        ):
            raise PolarizationCompositionError(
                f"{path}: raw sum cannot normalize to the polarized cross section"
            )
        expected_scale = cross_section / raw_accumulator.sumw.total
        _close(
            source_scale,
            expected_scale,
            scale=max(abs(expected_scale), 1.0),
            label=f"{path}: merged nominal scale from raw sum",
        )
        _close(
            nominal.total,
            cross_section,
            scale=nominal_abs.total,
            label=f"{path}: nominal cross-section closure",
        )
        if "sumw_nominal_pb" in summary:
            _close(
                float(summary["sumw_nominal_pb"]),
                cross_section,
                scale=nominal_abs.total,
                label=f"{path}: MergeSummary nominal closure",
            )

        job_keys = tuple(
            (int(campaign), int(sample), int(job))
            for campaign, sample, job in zip(
                run_arrays["campaign_id"],
                run_arrays["sample_code"],
                run_arrays["job_id"],
            )
        )
        if len(job_keys) != len(set(job_keys)):
            raise ProvenanceError(f"{path}: duplicate source job identity")

    if sha256_file(path) != input_sha256:
        raise PolarizationCompositionError(f"{path}: input changed during inspection")

    return PolarizedSource(
        channel=channel,
        path=path,
        sha256=input_sha256,
        metadata=metadata,
        event_schema=event_schema,
        run_schema=run_schema,
        weight_schema=weight_schema,
        event_count=event_count,
        run_count=source_job_count,
        analysis_schema_version=analysis_schema_version,
        campaign_id=campaign_id,
        sample_code=sample_code,
        source_job_count=source_job_count,
        cross_section_pb=cross_section,
        cross_section_mc_error_pb=cross_section_error,
        nominal_sumabsw_pb=nominal_abs.total,
        alternative_weight_ids=alternative_ids,
        job_keys=job_keys,
        polarization_contract=declared_contract,
        vpolar_fingerprint=vpolar_fingerprint,
        process_card_sha256=process_card_sha256,
    )


def _compatibility_fingerprint(metadata: Mapping[str, Any]) -> dict[str, Any]:
    cached = metadata.get("physics_invariants")
    if not isinstance(cached, Mapping):
        raise ProvenanceError("merge_metadata lacks physics_invariants")
    inputs = metadata.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ProvenanceError("merge_metadata lacks embedded inputs")
    recomputed: list[dict[str, Any]] = []
    for index, source in enumerate(inputs):
        analysis = source.get("analysis_metadata") if isinstance(source, Mapping) else None
        if not isinstance(analysis, Mapping):
            raise ProvenanceError(
                f"merge_metadata input {index} lacks analysis metadata"
            )
        recomputed.append(_physics_fingerprint(analysis))
    if any(value != recomputed[0] for value in recomputed[1:]):
        raise ProvenanceError(
            "embedded source jobs disagree on generic physics invariants"
        )
    if dict(cached) != recomputed[0]:
        raise ProvenanceError(
            "cached physics_invariants disagree with embedded analysis metadata"
        )
    raw = recomputed[0]
    output: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key)
        leaf = name.rsplit(".", 1)[-1]
        channel_variant = (
            name in _CHANNEL_VARIANT_FINGERPRINT_KEYS
            or leaf in {
                "polarization",
                "polarization_channel",
                "polarization_component",
            }
            or leaf == "mixed_polarization_interference"
            or "polarization.channel" in name
            or "loop_filter" in leaf
            or ("process" in leaf and leaf.endswith("sha256"))
        )
        if not channel_variant:
            output[name] = value
    return output


def _validate_sources(sources: Sequence[PolarizedSource]) -> None:
    if tuple(source.channel for source in sources) != POLARIZATION_CHANNELS:
        raise RuntimeError("internal polarization source ordering changed")
    if len({source.sha256 for source in sources}) != len(sources):
        raise PolarizationCompositionError("byte-identical polarized inputs are not allowed")
    if len({source.campaign_id for source in sources}) != 1:
        raise ProvenanceError("polarized inputs must belong to one campaign")
    if len({source.analysis_schema_version for source in sources}) != 1:
        raise ProvenanceError("polarized inputs use different analysis schema versions")
    if len({source.process_card_sha256 for source in sources}) != len(sources):
        raise ProvenanceError(
            "LL, TT, TL, and LT must use four distinct polarized process cards"
        )
    first = sources[0]
    for source in sources[1:]:
        if not _schemas_equal(source.event_schema, first.event_schema):
            raise SchemaError(f"{source.path}: Events schema differs from LL")
        if not _schemas_equal(source.run_schema, first.run_schema):
            raise SchemaError(f"{source.path}: Runs schema differs from LL")
        if (source.weight_schema is None) != (first.weight_schema is None):
            raise SchemaError("all polarized inputs must agree on LHEWeights presence")
        if source.weight_schema is not None and not _schemas_equal(
            source.weight_schema, first.weight_schema or {}
        ):
            raise SchemaError(f"{source.path}: LHEWeights schema differs from LL")
        if source.alternative_weight_ids != first.alternative_weight_ids:
            raise SchemaError("polarized inputs have different alternative-weight IDs")

    for source in sources[1:]:
        if source.vpolar_fingerprint != first.vpolar_fingerprint:
            changed = sorted(
                key
                for key in set(first.vpolar_fingerprint)
                | set(source.vpolar_fingerprint)
                if first.vpolar_fingerprint.get(key)
                != source.vpolar_fingerprint.get(key)
            )
            raise ProvenanceError(
                f"{source.path}: incompatible VPolar generation invariants: {changed}"
            )

    fingerprints = [_compatibility_fingerprint(source.metadata) for source in sources]
    for source, fingerprint in zip(sources[1:], fingerprints[1:]):
        if fingerprint != fingerprints[0]:
            changed = sorted(
                key
                for key in set(fingerprints[0]) | set(fingerprint)
                if fingerprints[0].get(key) != fingerprint.get(key)
            )
            raise ProvenanceError(
                f"{source.path}: incompatible non-polarization invariants: {changed}"
            )

    frames = {source.polarization_contract["frame"] for source in sources}
    if len(frames) != 1:
        raise ProvenanceError("polarized inputs use different helicity frames")
    all_job_keys = [key for source in sources for key in source.job_keys]
    if len(all_job_keys) != len(set(all_job_keys)):
        raise ProvenanceError("polarized inputs contain overlapping source-job identities")


def _event_output_schema(source: PolarizedSource) -> dict[str, np.dtype]:
    return {**source.event_schema, **COMPOSED_EVENT_BRANCHES}


def _source_summary_schema() -> dict[str, np.dtype]:
    schema = {
        "schema_version": np.dtype("uint16"),
        "source_polarization_code": np.dtype("uint8"),
        "campaign_id": np.dtype("uint64"),
        "sample_code": np.dtype("uint8"),
        "event_count": np.dtype("uint64"),
        "source_job_count": np.dtype("uint32"),
        "source_cross_section_pb": np.dtype("float64"),
        "source_cross_section_mc_error_pb": np.dtype("float64"),
    }
    for slug in COMPONENT_SLUGS:
        schema[f"coefficient_{slug}"] = np.dtype("float64")
        schema[f"contribution_{slug}_pb"] = np.dtype("float64")
    return schema


def _composition_summary_schema() -> dict[str, np.dtype]:
    schema = {
        "schema_version": np.dtype("uint16"),
        "analysis_schema_version": np.dtype("uint16"),
        "input_file_count": np.dtype("uint8"),
        "source_job_count": np.dtype("uint32"),
        "campaign_id": np.dtype("uint64"),
        "event_count": np.dtype("uint64"),
        "component_count": np.dtype("uint8"),
    }
    for slug in COMPONENT_SLUGS:
        schema.update(
            {
                f"sumw_{slug}_pb": np.dtype("float64"),
                f"sumw2_{slug}_pb2": np.dtype("float64"),
                f"sumabsw_{slug}_pb": np.dtype("float64"),
                f"expected_integral_{slug}_pb": np.dtype("float64"),
                f"closure_residual_{slug}_pb": np.dtype("float64"),
                f"closure_tolerance_{slug}_pb": np.dtype("float64"),
                f"positive_weight_count_{slug}": np.dtype("uint64"),
                f"negative_weight_count_{slug}": np.dtype("uint64"),
                f"zero_weight_count_{slug}": np.dtype("uint64"),
            }
        )
    return schema


def _one_row(
    schema: Mapping[str, np.dtype], values: Mapping[str, int | float]
) -> dict[str, np.ndarray]:
    return {
        name: np.asarray([values[name]], dtype=dtype)
        for name, dtype in schema.items()
    }


def _closure_tolerance(expected: float, sumabsw: float) -> float:
    return max(
        1.0e-11 * max(abs(expected), 1.0),
        32.0 * np.finfo(np.float64).eps * max(sumabsw, 1.0),
    )


def _json_safe(value: Any) -> Any:
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


def _code_provenance() -> dict[str, Any]:
    paths = (
        "Merging/compose_polarized_components.py",
        "Merging/merge_analysis_outputs.py",
        "src/offshell_production/harmonics.py",
        "pyproject.toml",
        "uv.lock",
    )
    return {
        "hash_algorithm": "sha256",
        "files": {
            relative: {"sha256": sha256_file(REPO_ROOT / relative)}
            for relative in paths
        },
    }


def _verify_output(
    path: Path,
    *,
    sources: Sequence[PolarizedSource],
    totals: Mapping[str, int | float],
    metadata: Mapping[str, Any],
    step_size: str,
) -> None:
    expected_keys = {
        EVENT_TREE_NAME,
        RUN_TREE_NAME,
        SOURCE_SUMMARY_NAME,
        COMPOSITION_SUMMARY_NAME,
        COMPOSITION_METADATA_NAME,
    }
    if sources[0].weight_schema is not None:
        expected_keys.add(LHE_WEIGHT_TREE_NAME)
    with uproot.open(path) as root_file:
        actual_keys = set(root_file.keys(cycle=False))
        if actual_keys != expected_keys:
            raise PolarizationCompositionError(
                f"temporary output has unexpected ROOT keys: {sorted(actual_keys)}"
            )
        events = root_file[EVENT_TREE_NAME]
        if not _schemas_equal(_tree_schema(events), _event_output_schema(sources[0])):
            raise SchemaError("temporary composed Events schema is incorrect")
        if not _schemas_equal(_tree_schema(root_file[RUN_TREE_NAME]), sources[0].run_schema):
            raise SchemaError("temporary composed Runs schema is incorrect")
        if not _schemas_equal(
            _tree_schema(root_file[SOURCE_SUMMARY_NAME]), _source_summary_schema()
        ):
            raise SchemaError("temporary polarization-source schema is incorrect")
        if not _schemas_equal(
            _tree_schema(root_file[COMPOSITION_SUMMARY_NAME]),
            _composition_summary_schema(),
        ):
            raise SchemaError("temporary combination-summary schema is incorrect")
        if sources[0].weight_schema is not None and not _schemas_equal(
            _tree_schema(root_file[LHE_WEIGHT_TREE_NAME]),
            sources[0].weight_schema,
        ):
            raise SchemaError("temporary LHEWeights schema is incorrect")
        if int(events.num_entries) != int(totals["event_count"]):
            raise PolarizationCompositionError("temporary Events count is incorrect")
        if int(root_file[RUN_TREE_NAME].num_entries) != int(totals["source_job_count"]):
            raise PolarizationCompositionError("temporary Runs count is incorrect")
        if int(root_file[SOURCE_SUMMARY_NAME].num_entries) != len(sources):
            raise PolarizationCompositionError("temporary source summary count is incorrect")
        if int(root_file[COMPOSITION_SUMMARY_NAME].num_entries) != 1:
            raise PolarizationCompositionError("temporary combination summary is not one row")

        # The derived file is deliberately a concatenation, not a rewrite of
        # any source identity or source weight.  Compare those values after the
        # file has been closed and reopened, rather than relying on the write
        # loop alone.
        preserved_event_branches = tuple(sources[0].event_schema)
        output_entry_start = 0
        for source in sources:
            source_entry_start = 0
            with uproot.open(source.path) as source_file:
                source_events = source_file[EVENT_TREE_NAME]
                for source_arrays in source_events.iterate(
                    expressions=preserved_event_branches,
                    step_size=step_size,
                    library="np",
                    how=dict,
                ):
                    size = len(source_arrays["weight_lhe"])
                    output_arrays = events.arrays(
                        expressions=preserved_event_branches,
                        entry_start=output_entry_start + source_entry_start,
                        entry_stop=output_entry_start + source_entry_start + size,
                        library="np",
                        how=dict,
                    )
                    for name in preserved_event_branches:
                        if not np.array_equal(
                            output_arrays[name], source_arrays[name], equal_nan=True
                        ):
                            raise PolarizationCompositionError(
                                f"temporary Events.{name} changed source values"
                            )
                    source_entry_start += size
                if source_entry_start != source.event_count:
                    raise PolarizationCompositionError(
                        "temporary verification did not visit every source event"
                    )
            output_entry_start += source.event_count

        output_runs = root_file[RUN_TREE_NAME]
        output_run_start = 0
        for source in sources:
            with uproot.open(source.path) as source_file:
                source_runs = source_file[RUN_TREE_NAME].arrays(
                    library="np", how=dict
                )
            output_arrays = output_runs.arrays(
                entry_start=output_run_start,
                entry_stop=output_run_start + source.run_count,
                library="np",
                how=dict,
            )
            for name in source.run_schema:
                if not np.array_equal(
                    output_arrays[name], source_runs[name], equal_nan=True
                ):
                    raise PolarizationCompositionError(
                        f"temporary Runs.{name} changed source rows"
                    )
            output_run_start += source.run_count

        if sources[0].weight_schema is not None:
            output_weights = root_file[LHE_WEIGHT_TREE_NAME]
            output_weight_start = 0
            for source in sources:
                source_weight_start = 0
                with uproot.open(source.path) as source_file:
                    source_weights = source_file[LHE_WEIGHT_TREE_NAME]
                    for source_arrays in source_weights.iterate(
                        step_size=step_size, library="np", how=dict
                    ):
                        size = len(source_arrays["campaign_id"])
                        output_arrays = output_weights.arrays(
                            entry_start=output_weight_start + source_weight_start,
                            entry_stop=(
                                output_weight_start + source_weight_start + size
                            ),
                            library="np",
                            how=dict,
                        )
                        for name in source.weight_schema or {}:
                            if not np.array_equal(
                                output_arrays[name],
                                source_arrays[name],
                                equal_nan=True,
                            ):
                                raise PolarizationCompositionError(
                                    f"temporary LHEWeights.{name} changed "
                                    "source values"
                                )
                        source_weight_start += size
                if source_weight_start != source.event_count:
                    raise PolarizationCompositionError(
                        "temporary verification did not visit every LHEWeights row"
                    )
                output_weight_start += source.event_count

        source_summary = root_file[SOURCE_SUMMARY_NAME].arrays(
            library="np", how=dict
        )
        source_schema = _source_summary_schema()
        expected_source_rows: dict[str, list[int | float]] = {
            name: [] for name in source_schema
        }
        for source in sources:
            values: dict[str, int | float] = {
                "schema_version": COMPOSITION_SCHEMA_VERSION,
                "source_polarization_code": POLARIZATION_CHANNEL_CODES[
                    source.channel
                ],
                "campaign_id": source.campaign_id,
                "sample_code": source.sample_code,
                "event_count": source.event_count,
                "source_job_count": source.source_job_count,
                "source_cross_section_pb": source.cross_section_pb,
                "source_cross_section_mc_error_pb": (
                    source.cross_section_mc_error_pb
                ),
            }
            for slug in COMPONENT_SLUGS:
                coefficient = POLARIZATION_COMBINATION_COEFFICIENTS[slug][
                    source.channel
                ]
                values[f"coefficient_{slug}"] = coefficient
                values[f"contribution_{slug}_pb"] = (
                    coefficient * source.cross_section_pb
                )
            for name in source_schema:
                expected_source_rows[name].append(values[name])
        for name, dtype in source_schema.items():
            expected_values = np.asarray(expected_source_rows[name], dtype=dtype)
            if not np.array_equal(
                source_summary[name], expected_values, equal_nan=True
            ):
                raise PolarizationCompositionError(
                    f"temporary PolarizationSources.{name} is incorrect"
                )

        accumulators = {
            slug: _ComponentAccumulator.create() for slug in COMPONENT_SLUGS
        }
        entry_start = 0
        expressions = (
            "weight_lhe",
            "weight_nominal_pb",
            "source_polarization_code",
            *(f"polarization_coefficient_{slug}" for slug in COMPONENT_SLUGS),
            *(COMPONENT_WEIGHT_BRANCHES[slug] for slug in COMPONENT_SLUGS),
        )
        for source in sources:
            entry_stop = entry_start + source.event_count
            for arrays in events.iterate(
                expressions=expressions,
                entry_start=entry_start,
                entry_stop=entry_stop,
                step_size=step_size,
                library="np",
                how=dict,
            ):
                code = POLARIZATION_CHANNEL_CODES[source.channel]
                if not np.all(arrays["source_polarization_code"] == code):
                    raise PolarizationCompositionError("source polarization code changed")
                nominal = np.asarray(arrays["weight_nominal_pb"], dtype=np.float64)
                for slug in COMPONENT_SLUGS:
                    coefficient = POLARIZATION_COMBINATION_COEFFICIENTS[slug][
                        source.channel
                    ]
                    coefficient_branch = np.asarray(
                        arrays[f"polarization_coefficient_{slug}"], dtype=np.float64
                    )
                    output_weight = np.asarray(
                        arrays[COMPONENT_WEIGHT_BRANCHES[slug]], dtype=np.float64
                    )
                    if not np.all(coefficient_branch == coefficient):
                        raise PolarizationCompositionError(
                            f"temporary {slug} coefficient is incorrect"
                        )
                    if not np.array_equal(output_weight, nominal * coefficient):
                        raise PolarizationCompositionError(
                            f"temporary {slug} component weight is incorrect"
                        )
                    accumulators[slug].add(output_weight)
            entry_start = entry_stop

        summary = root_file[COMPOSITION_SUMMARY_NAME].arrays(library="np", how=dict)
        for slug, accumulator in accumulators.items():
            expected = float(totals[f"expected_integral_{slug}_pb"])
            tolerance = float(totals[f"closure_tolerance_{slug}_pb"])
            if abs(accumulator.sumw.total - expected) > tolerance:
                raise PolarizationCompositionError(
                    f"temporary {slug} integral does not close"
                )
            if not math.isclose(
                float(summary[f"sumw_{slug}_pb"][0]),
                accumulator.sumw.total,
                rel_tol=0.0,
                abs_tol=tolerance,
            ):
                raise PolarizationCompositionError(
                    f"temporary {slug} summary is inconsistent"
                )
        for name, dtype in _composition_summary_schema().items():
            expected_value = np.asarray([totals[name]], dtype=dtype)
            if not np.array_equal(summary[name], expected_value, equal_nan=True):
                raise PolarizationCompositionError(
                    f"temporary PolarizationCombinationSummary.{name} is incorrect"
                )
        observed_metadata = _json_object(root_file, COMPOSITION_METADATA_NAME, path)
        if observed_metadata != _json_safe(metadata):
            raise PolarizationCompositionError(
                "temporary polarization combination metadata is incorrect"
            )


def compose_polarized_components(
    *,
    ll: str | Path,
    tt: str | Path,
    tl: str | Path,
    lt: str | Path,
    output: str | Path,
    step_size: str = "50 MB",
    overwrite: bool = False,
) -> dict[str, int | float | str]:
    """Create two signed symmetric m=0 samples and direct incoherent TL+LT."""

    paths = {
        "LL": Path(ll).expanduser().resolve(),
        "TT": Path(tt).expanduser().resolve(),
        "TL": Path(tl).expanduser().resolve(),
        "LT": Path(lt).expanduser().resolve(),
    }
    if len(set(paths.values())) != len(paths):
        raise ValueError("LL, TT, TL, and LT must be four distinct files")
    output_path = Path(output).expanduser().resolve()
    if output_path in paths.values():
        raise ValueError("output must not alias a polarized input")

    lock_descriptor, _lock_path = _acquire_output_lock(output_path)
    temporary: Path | None = None
    try:
        sources = [
            _inspect_source(paths[channel], channel, step_size=step_size)
            for channel in POLARIZATION_CHANNELS
        ]
        _validate_sources(sources)

        event_count = sum(source.event_count for source in sources)
        source_job_count = sum(source.source_job_count for source in sources)
        expected_integrals = {
            slug: math.fsum(
                POLARIZATION_COMBINATION_COEFFICIENTS[slug][source.channel]
                * source.cross_section_pb
                for source in sources
            )
            for slug in COMPONENT_SLUGS
        }
        accumulators = {
            slug: _ComponentAccumulator.create() for slug in COMPONENT_SLUGS
        }
        temporary = _temporary_output(output_path, overwrite)
        with uproot.recreate(temporary) as output_file:
            event_schema = _event_output_schema(sources[0])
            output_file.mktree(
                EVENT_TREE_NAME,
                event_schema,
                title="Polarized event streams with symmetric component weights",
            )
            output_events = output_file[EVENT_TREE_NAME]

            output_weights = None
            if sources[0].weight_schema is not None:
                output_file.mktree(
                    LHE_WEIGHT_TREE_NAME,
                    sources[0].weight_schema,
                    title="Unmodified alternative LHE weights from polarized sources",
                )
                output_weights = output_file[LHE_WEIGHT_TREE_NAME]

            written_events = 0
            written_weights = 0
            for source in sources:
                coefficients = {
                    slug: POLARIZATION_COMBINATION_COEFFICIENTS[slug][source.channel]
                    for slug in COMPONENT_SLUGS
                }
                with uproot.open(source.path) as input_file:
                    for arrays in input_file[EVENT_TREE_NAME].iterate(
                        step_size=step_size, library="np", how=dict
                    ):
                        nominal = np.asarray(arrays["weight_nominal_pb"], dtype=np.float64)
                        size = len(nominal)
                        output_arrays = dict(arrays)
                        output_arrays["source_polarization_code"] = np.full(
                            size,
                            POLARIZATION_CHANNEL_CODES[source.channel],
                            dtype=np.uint8,
                        )
                        for slug, coefficient in coefficients.items():
                            output_arrays[f"polarization_coefficient_{slug}"] = np.full(
                                size, coefficient, dtype=np.float64
                            )
                            component_weight = nominal * coefficient
                            output_arrays[
                                COMPONENT_WEIGHT_BRANCHES[slug]
                            ] = component_weight
                            accumulators[slug].add(component_weight)
                        output_events.extend(output_arrays)
                        written_events += size

                    if output_weights is not None:
                        for arrays in input_file[LHE_WEIGHT_TREE_NAME].iterate(
                            step_size=step_size, library="np", how=dict
                        ):
                            output_weights.extend(arrays)
                            written_weights += len(arrays["campaign_id"])

            if written_events != event_count:
                raise PolarizationCompositionError("written Events count differs from inputs")
            if output_weights is not None and written_weights != written_events:
                raise PolarizationCompositionError(
                    "written LHEWeights count differs from Events"
                )

            output_file.mktree(
                RUN_TREE_NAME,
                sources[0].run_schema,
                title="Original job-level rows from all polarized sources",
            )
            for source in sources:
                with uproot.open(source.path) as input_file:
                    for arrays in input_file[RUN_TREE_NAME].iterate(
                        step_size=step_size, library="np", how=dict
                    ):
                        output_file[RUN_TREE_NAME].extend(arrays)

            source_schema = _source_summary_schema()
            output_file.mktree(
                SOURCE_SUMMARY_NAME,
                source_schema,
                title="One normalization row per polarized campaign source",
            )
            for source in sources:
                values: dict[str, int | float] = {
                    "schema_version": COMPOSITION_SCHEMA_VERSION,
                    "source_polarization_code": POLARIZATION_CHANNEL_CODES[
                        source.channel
                    ],
                    "campaign_id": source.campaign_id,
                    "sample_code": source.sample_code,
                    "event_count": source.event_count,
                    "source_job_count": source.source_job_count,
                    "source_cross_section_pb": source.cross_section_pb,
                    "source_cross_section_mc_error_pb": (
                        source.cross_section_mc_error_pb
                    ),
                }
                for slug in COMPONENT_SLUGS:
                    coefficient = POLARIZATION_COMBINATION_COEFFICIENTS[slug][
                        source.channel
                    ]
                    values[f"coefficient_{slug}"] = coefficient
                    values[f"contribution_{slug}_pb"] = (
                        coefficient * source.cross_section_pb
                    )
                output_file[SOURCE_SUMMARY_NAME].extend(
                    _one_row(source_schema, values)
                )

            totals: dict[str, int | float] = {
                "schema_version": COMPOSITION_SCHEMA_VERSION,
                "analysis_schema_version": sources[0].analysis_schema_version,
                "input_file_count": len(sources),
                "source_job_count": source_job_count,
                "campaign_id": sources[0].campaign_id,
                "event_count": event_count,
                "component_count": len(COMPONENT_SLUGS),
            }
            for slug, accumulator in accumulators.items():
                expected = expected_integrals[slug]
                tolerance = _closure_tolerance(expected, accumulator.sumabsw.total)
                residual = accumulator.sumw.total - expected
                if abs(residual) > tolerance:
                    raise PolarizationCompositionError(
                        f"{slug} combination does not close: residual={residual!r}, "
                        f"tolerance={tolerance!r}"
                    )
                totals.update(
                    {
                        f"sumw_{slug}_pb": accumulator.sumw.total,
                        f"sumw2_{slug}_pb2": accumulator.sumw2.total,
                        f"sumabsw_{slug}_pb": accumulator.sumabsw.total,
                        f"expected_integral_{slug}_pb": expected,
                        f"closure_residual_{slug}_pb": residual,
                        f"closure_tolerance_{slug}_pb": tolerance,
                        f"positive_weight_count_{slug}": accumulator.positive,
                        f"negative_weight_count_{slug}": accumulator.negative,
                        f"zero_weight_count_{slug}": accumulator.zero,
                    }
                )
            combination_schema = _composition_summary_schema()
            output_file.mktree(
                COMPOSITION_SUMMARY_NAME,
                combination_schema,
                title="Signed symmetric angular-component normalization",
            )
            output_file[COMPOSITION_SUMMARY_NAME].extend(
                _one_row(combination_schema, totals)
            )

            metadata = {
                "schema_version": COMPOSITION_SCHEMA_VERSION,
                "analysis_schema_version": sources[0].analysis_schema_version,
                "campaign_id": sources[0].campaign_id,
                "event_ordering": list(POLARIZATION_CHANNELS),
                "z_assignment": {
                    "Z1": "mu+mu-",
                    "Z2": "e+e-",
                    "polarization_label_order": "Z1,Z2",
                },
                "helicity_frame": sources[0].polarization_contract["frame"],
                "vpolar_common_invariants": sources[0].vpolar_fingerprint,
                "process_cards_sha256": {
                    source.channel: source.process_card_sha256 for source in sources
                },
                "lhe_alternative_weights": {
                    "tree": (
                        LHE_WEIGHT_TREE_NAME
                        if sources[0].alternative_weight_ids
                        else None
                    ),
                    "ids": list(sources[0].alternative_weight_ids),
                    "ordering": "lexicographic_weight_id",
                    "one_row_per_event": bool(sources[0].alternative_weight_ids),
                    "preserved_unscaled": True,
                },
                "source_requirement": (
                    "four separately generated, interference-free LL, TT, TL, "
                    "and LT campaign outputs"
                ),
                "transverse_state": (
                    "VPolar ZT is the coherent lambda=+1 plus lambda=-1 state; "
                    "the excluded interference is between L/T source channels"
                ),
                "components": {
                    slug: {
                        "label": COMPONENT_LABELS[slug],
                        "weight_branch": COMPONENT_WEIGHT_BRANCHES[slug],
                        "coefficient_branch": f"polarization_coefficient_{slug}",
                        "coefficients": POLARIZATION_COMBINATION_COEFFICIENTS[slug],
                        "expected_integral_pb": expected_integrals[slug],
                        "formula": (
                            "sum_channel coefficient[channel] * "
                            "source weight_nominal_pb"
                        ),
                    }
                    for slug in COMPONENT_SLUGS
                },
                "normalization": {
                    "source_weight_branch": "weight_nominal_pb",
                    "source_weight_preserved": True,
                    "component_weights_renormalized": False,
                    "units": "pb",
                    "source_cross_section_errors": (
                        "not combined because cross-channel covariance is not "
                        "known; per-source MC errors are stored in "
                        "PolarizationSources and event sumw2 is reported separately"
                    ),
                },
                "validity_scope": {
                    "exact_statement": (
                        "The coefficients map diagonal LL, TT, TL, and LT rates "
                        "to the symmetric m=0 angular moments after complete "
                        "decay-angle integration."
                    ),
                    "acceptance_caveat": (
                        "Angle-dependent cuts or detector response need not make "
                        "this constant-weight construction equal to a direct "
                        "event-by-event harmonic projection. Compare at LHE level "
                        "without angular acceptance for the closure validation."
                    ),
                    "coherent_mixed_input": "forbidden",
                },
                "inputs": [
                    {
                        "channel": source.channel,
                        "channel_code": POLARIZATION_CHANNEL_CODES[source.channel],
                        "path": str(source.path),
                        "sha256": source.sha256,
                        "event_count": source.event_count,
                        "source_job_count": source.source_job_count,
                        "source_cross_section_pb": source.cross_section_pb,
                        "source_cross_section_mc_error_pb": (
                            source.cross_section_mc_error_pb
                        ),
                        "polarization_contract": source.polarization_contract,
                        "merge_metadata": source.metadata,
                    }
                    for source in sources
                ],
                "code": _code_provenance(),
            }
            output_file[COMPOSITION_METADATA_NAME] = json.dumps(
                _json_safe(metadata), sort_keys=True, allow_nan=False
            )

        _verify_output(
            temporary,
            sources=sources,
            totals=totals,
            metadata=metadata,
            step_size=step_size,
        )
        for source in sources:
            if sha256_file(source.path) != source.sha256:
                raise PolarizationCompositionError(
                    f"input changed while composing: {source.path}"
                )
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
        "event_count": event_count,
        "source_job_count": source_job_count,
        "integral_00_20_pb": float(totals["sumw_00_20_pb"]),
        "integral_20_20_pb": float(totals["sumw_20_20_pb"]),
        "integral_mixed_incoherent_pb": float(
            totals["sumw_mixed_incoherent_pb"]
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ll", required=True, type=Path, help="merged LL ROOT file")
    parser.add_argument("--tt", required=True, type=Path, help="merged TT ROOT file")
    parser.add_argument("--tl", required=True, type=Path, help="merged TL ROOT file")
    parser.add_argument("--lt", required=True, type=Path, help="merged LT ROOT file")
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--step-size", default="50 MB", help="uproot chunk size")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    result = compose_polarized_components(
        ll=args.ll,
        tt=args.tt,
        tl=args.tl,
        lt=args.lt,
        output=args.output,
        step_size=args.step_size,
        overwrite=args.overwrite,
    )
    print(
        f"Composed {result['event_count']} polarized events into {result['output']} "
        f"(S00;20={result['integral_00_20_pb']:.12g} pb, "
        f"S20;20={result['integral_20_20_pb']:.12g} pb, "
        f"TL+LT={result['integral_mixed_incoherent_pb']:.12g} pb)"
    )


if __name__ == "__main__":
    main()
