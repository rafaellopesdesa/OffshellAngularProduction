"""Small, self-contained ROOT fixtures for merger integration tests."""

from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import uproot

from Analysis.build_analysis_tree import SCHEMA_VERSION, event_uid, output_schema
from Merging.merge_analysis_outputs import BASE_RUN_SCHEMA, _weight_tree_schema


@dataclass(frozen=True)
class Normalization:
    """Pre-shower normalization primitives embedded in one source job."""

    generated: int
    accepted: int
    sumw_generated: float
    sumw2_generated: float
    sumabsw_generated: float
    sumw_accepted: float
    sumw2_accepted: float
    sumabsw_accepted: float


@dataclass(frozen=True)
class JobSpec:
    path: Path
    job_id: int
    source_ids: tuple[int, ...]
    weights: tuple[float, ...]
    angles: tuple[tuple[float, float, float, float], ...]
    invalid_lhe_indices: tuple[int, ...]
    normalization: Normalization
    alternative_ids: tuple[str, ...]
    alternative_values: np.ndarray


def _mc_error(sumw: float, sumw2: float, count: int) -> float:
    if count < 2:
        return math.nan
    return math.sqrt(max(sumw2 - sumw * sumw / count, 0.0) / (count * (count - 1)))


def _empty_arrays(schema: dict[str, np.dtype], size: int) -> dict[str, np.ndarray]:
    arrays: dict[str, np.ndarray] = {}
    for name, dtype in schema.items():
        if dtype.kind == "f":
            arrays[name] = np.full(size, np.nan, dtype=dtype)
        else:
            arrays[name] = np.zeros(size, dtype=dtype)
    return arrays


def _analysis_metadata(
    *,
    sample: str,
    sample_code: int,
    generation_seed: int,
    event_count: int,
    event_number_start: int,
    delphes_seed: int,
    normalization: Normalization,
    alternative_ids: tuple[str, ...],
) -> dict[str, object]:
    run_number = 100001 if sample == "gg4l" else 100002
    filtered = normalization.sumw_accepted / normalization.generated
    inclusive = normalization.sumw_generated / normalization.generated
    return {
        "schema_version": SCHEMA_VERSION,
        "sample": sample,
        "sample_code": sample_code,
        "uid_schema_tag": "OffshellAngularProduction.Events.v2",
        "matching": "named_weight_ratio_v1_with_ordinal_cross_checks",
        "delphes_tree": "Delphes",
        "analysis_code": {
            "hash_algorithm": "sha256",
            "files": {"fixture": {"sha256": "a" * 64}},
        },
        "provenance": {
            "generation": {
                "schema_version": 1,
                "process": sample,
                "seed": generation_seed,
                "events": event_count,
                "first_event": event_number_start,
                "run_number": run_number,
                "ecm_energy_gev": 13600.0,
                "athgeneration_release": "23.6.41",
                "generator_mll_min_gev": 50.0,
                "generator_m4l_min_gev": 150.0 if sample == "gg4l" else 70.0,
                "generator_m4l_max_gev": 3000.0,
                "analysis_mz_min_gev": 50.0,
                "analysis_mz_max_gev": 106.0,
                "analysis_m4l_min_gev": 180.0,
                "analysis_m4l_max_gev": "none",
                "target_generation_phase_space_m4l_max_gev": 3000.0,
                "alignment_contract": "named-weight-id-v1",
                "lhe_event_id_contract": "named-weight-id-v1",
            },
            "lhe_contract": {
                "schema_version": 2,
                "contract": "named-weight-id-v1",
                "normalization_contract": "idwtup-minus4-sample-mean-v1",
                "nominal_weight_units": "pb",
                "lhe_weighting_strategy": -4,
                "cross_section_method": (
                    "mean nominal LHE weight; rejected events assigned zero for "
                    "filtered estimate"
                ),
                "process": sample,
                "marker_id_weight": "AUX_OAP_EVENT_ID",
                "marker_unit_weight": "AUX_OAP_EVENT_UNIT",
                "m4l_min_gev": 150.0 if sample == "gg4l" else 70.0,
                "m4l_max_gev": 3000.0,
                "lhe_init": {"idwtup": -4},
                "generated_lhe_events": normalization.generated,
                "accepted_lhe_events": normalization.accepted,
                "sumw_generated": normalization.sumw_generated,
                "sumw2_generated": normalization.sumw2_generated,
                "sumabsw_generated": normalization.sumabsw_generated,
                "sumw_accepted": normalization.sumw_accepted,
                "sumw2_accepted": normalization.sumw2_accepted,
                "sumabsw_accepted": normalization.sumabsw_accepted,
                "inclusive_cross_section_pb": inclusive,
                "inclusive_cross_section_mc_error_pb": _mc_error(
                    normalization.sumw_generated,
                    normalization.sumw2_generated,
                    normalization.generated,
                ),
                "filtered_cross_section_pb": filtered,
                "filtered_cross_section_mc_error_pb": _mc_error(
                    normalization.sumw_accepted,
                    normalization.sumw2_accepted,
                    normalization.generated,
                ),
            },
            "alignment": {
                "schema_version": 2,
                "contract": "named-weight-id-v1",
                "process": sample,
                "run_number": run_number,
                "athgeneration_release": "23.6.41",
                "job_option_sha256": "b" * 64,
                "hepmc_precision_contract": {"relative_ratio_tolerance": 5.0e-8},
                "contract_conditions": {"post_shower_generator_filter": False},
            },
            "simulation": {
                "schema_version": 2,
                "process": sample,
                "random_seed": delphes_seed,
                "weight_scale": 1.0,
                "weight_scale_policy": "identity_for_direct_2e2mu_generation",
                "weight_branches_preserved": "Event.Weight,Weight.Weight",
                "cross_section_semantics": "conditional_on_lhe_phase_space_filter",
                "delphes_version": "3.5.1",
                "delphes_commit": "c" * 40,
                "card_sha256": "d" * 64,
                "resolved_card_sha256": "e" * 64,
            },
        },
        "lhe_alternative_weights": {
            "tree": "LHEWeights" if alternative_ids else None,
            "ids": list(alternative_ids),
            "ordering": "lexicographic_weight_id",
            "one_row_per_event": bool(alternative_ids),
            "technical_weights_excluded": [
                "AUX_OAP_EVENT_ID",
                "AUX_OAP_EVENT_UNIT",
            ],
        },
    }


def write_job(
    path: Path,
    *,
    job_id: int,
    source_ids: tuple[int, ...],
    weights: tuple[float, ...],
    angles: tuple[tuple[float, float, float, float], ...],
    normalization: Normalization,
    campaign_id: int = 42,
    sample_code: int = 0,
    invalid_lhe_indices: tuple[int, ...] = (),
    alternative_ids: tuple[str, ...] = ("1001", "2001"),
    alternative_values: np.ndarray | None = None,
) -> JobSpec:
    """Write the smallest exact-schema job file accepted by the merger."""

    size = len(weights)
    if not (len(source_ids) == len(angles) == size):
        raise ValueError("source IDs, weights, and angles must have equal lengths")
    if normalization.accepted < size:
        raise ValueError("the accepted safety stream must contain all retained events")
    sample = "gg4l" if sample_code == 0 else "qqZZ"
    schema = output_schema()
    events = _empty_arrays(schema, size)
    ordinal = np.arange(size, dtype=np.uint64)
    event_number_start = 100_000 + 100 * job_id

    events["campaign_id"][:] = campaign_id
    events["sample_code"][:] = sample_code
    events["job_id"][:] = job_id
    events["lhe_event_index"] = ordinal.copy()
    events["source_event_id"] = np.asarray(source_ids, dtype=np.uint64)
    events["hepmc_event_number"] = event_number_start + ordinal.astype(np.int64)
    events["delphes_event_number"] = events["hepmc_event_number"].copy()
    events["hepmc_entry"] = ordinal.copy()
    events["delphes_entry"] = ordinal.copy()
    for index, source_id in enumerate(source_ids):
        uid_hi, uid_lo = event_uid(campaign_id, sample_code, job_id, source_id)
        events["event_uid_hi"][index] = uid_hi
        events["event_uid_lo"][index] = uid_lo

    raw_weights = np.asarray(weights, dtype=np.float64)
    events["weight_lhe"] = raw_weights.copy()
    events["weight_delphes"] = raw_weights.copy()
    events["cross_section_pb_delphes"][:] = (
        normalization.sumw_accepted / normalization.generated
    )
    events["cross_section_error_pb_delphes"][:] = _mc_error(
        normalization.sumw_accepted,
        normalization.sumw2_accepted,
        normalization.generated,
    )
    for name in ("has_lhe", "has_hepmc", "has_delphes"):
        events[name][:] = True
    events["lhe_n_alternative_weights"][:] = len(alternative_ids)

    for level in ("lhe", "dressed", "reco"):
        events[f"{level}_candidate"][:] = True
        events[f"{level}_topology_valid"][:] = True
        events[f"{level}_projection_valid"][:] = True
    events["reco_pass_selection"][:] = True
    events["reconstructed"][:] = True

    angle_array = np.asarray(angles, dtype=np.float64)
    for column, name in enumerate(("theta1", "phi1", "theta2", "phi2")):
        events[f"lhe_{name}"] = angle_array[:, column].copy()
    # Deliberately different detector-level angles catch accidental use of
    # dressed or reconstructed rather than LHE Born-projected coordinates.
    events["dressed_theta1"][:] = 0.17
    events["dressed_phi1"][:] = -1.31
    events["dressed_theta2"][:] = 2.43
    events["dressed_phi2"][:] = 0.66
    events["reco_theta1"][:] = 2.71
    events["reco_phi1"][:] = 1.07
    events["reco_theta2"][:] = 0.29
    events["reco_phi2"][:] = -2.19

    for index in invalid_lhe_indices:
        events["lhe_projection_valid"][index] = False
        for name in ("theta1", "phi1", "theta2", "phi2"):
            events[f"lhe_{name}"][index] = np.nan

    positive = int(np.count_nonzero(raw_weights > 0.0))
    negative = int(np.count_nonzero(raw_weights < 0.0))
    zero = int(np.count_nonzero(raw_weights == 0.0))
    run = _empty_arrays(BASE_RUN_SCHEMA, 1)
    integer_values = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "sample_code": sample_code,
        "job_id": job_id,
        "event_count": size,
        "event_number_start": event_number_start,
        "source_event_id_min": min(source_ids),
        "source_event_id_max": max(source_ids),
        "generation_seed": job_id + 7,
        "delphes_seed": job_id + 17,
        "run_number": 100001 if sample == "gg4l" else 100002,
        "athgeneration_release_major": 23,
        "athgeneration_release_minor": 6,
        "athgeneration_release_patch": 41,
        "alignment_contract_code": 2,
        "positive_weight_count": positive,
        "negative_weight_count": negative,
        "zero_weight_count": zero,
        "normalization_generated_lhe_events": normalization.generated,
        "normalization_accepted_lhe_events": normalization.accepted,
    }
    for name, value in integer_values.items():
        run[name][0] = value
    run["ecm_energy_gev"][0] = 13600.0
    run["sumw"][0] = math.fsum(weights)
    run["sumw2"][0] = math.fsum(weight * weight for weight in weights)
    run["sumabsw"][0] = math.fsum(abs(weight) for weight in weights)
    run["sumw_delphes"][0] = run["sumw"][0]
    run["sumw2_delphes"][0] = run["sumw2"][0]
    run["sumabsw_delphes"][0] = run["sumabsw"][0]
    filtered = normalization.sumw_accepted / normalization.generated
    filtered_error = _mc_error(
        normalization.sumw_accepted,
        normalization.sumw2_accepted,
        normalization.generated,
    )
    for name in (
        "cross_section_first_pb_delphes",
        "cross_section_min_pb_delphes",
        "cross_section_max_pb_delphes",
        "cross_section_final_pb_delphes",
    ):
        run[name][0] = filtered
    for name in (
        "cross_section_error_first_pb_delphes",
        "cross_section_error_min_pb_delphes",
        "cross_section_error_max_pb_delphes",
        "cross_section_error_final_pb_delphes",
    ):
        run[name][0] = filtered_error
    run["phase_space_signed_efficiency"][0] = (
        normalization.sumw_accepted / normalization.sumw_generated
        if normalization.sumw_generated != 0.0
        else math.nan
    )
    run["phase_space_absolute_efficiency"][0] = (
        normalization.sumabsw_accepted / normalization.sumabsw_generated
        if normalization.sumabsw_generated != 0.0
        else math.nan
    )
    run["phase_space_count_efficiency"][0] = (
        normalization.accepted / normalization.generated
    )
    run["normalization_sumw_generated_pb"][0] = normalization.sumw_generated
    run["normalization_sumw2_generated_pb2"][0] = normalization.sumw2_generated
    run["normalization_sumw_accepted_pb"][0] = normalization.sumw_accepted
    run["normalization_sumw2_accepted_pb2"][0] = normalization.sumw2_accepted
    run["inclusive_lhe_cross_section_pb"][0] = (
        normalization.sumw_generated / normalization.generated
    )
    run["inclusive_lhe_cross_section_mc_error_pb"][0] = _mc_error(
        normalization.sumw_generated,
        normalization.sumw2_generated,
        normalization.generated,
    )
    run["effective_filtered_cross_section_pb"][0] = filtered
    run["effective_filtered_cross_section_mc_error_pb"][0] = filtered_error
    for level in ("lhe", "dressed", "reco"):
        run[f"{level}_candidate_count"][0] = np.count_nonzero(
            events[f"{level}_candidate"]
        )
        run[f"{level}_projection_valid_count"][0] = np.count_nonzero(
            events[f"{level}_projection_valid"]
        )
    run["reconstructed_count"][0] = np.count_nonzero(events["reconstructed"])

    metadata = _analysis_metadata(
        sample=sample,
        sample_code=sample_code,
        generation_seed=job_id + 7,
        event_count=size,
        event_number_start=event_number_start,
        delphes_seed=job_id + 17,
        normalization=normalization,
        alternative_ids=alternative_ids,
    )
    source_digest = hashlib.sha256()
    for source_id in source_ids:
        source_digest.update(source_id.to_bytes(8, "big"))
    metadata["source_event_id"] = {
        "sequence_sha256": source_digest.hexdigest(),
    }
    if alternative_values is None:
        alternative_values = np.asarray(
            [[weight * 0.9, weight * 1.1] for weight in weights],
            dtype=np.float64,
        )
        if len(alternative_ids) != 2:
            alternative_values = np.empty((size, len(alternative_ids)), dtype=np.float64)
    alternative_values = np.asarray(alternative_values, dtype=np.float64)
    if alternative_values.shape != (size, len(alternative_ids)):
        raise ValueError("alternative values do not match the declared schema")

    with uproot.recreate(path) as root_file:
        root_file.mktree("Events", schema)
        root_file["Events"].extend(events)
        root_file.mktree("Runs", BASE_RUN_SCHEMA)
        root_file["Runs"].extend(run)
        if alternative_ids:
            weight_schema = _weight_tree_schema(len(alternative_ids))
            root_file.mktree("LHEWeights", weight_schema)
            root_file["LHEWeights"].extend(
                {
                    name: events[name].copy()
                    for name in weight_schema
                    if name != "values"
                }
                | {"values": alternative_values.copy()}
            )
        root_file["analysis_metadata"] = json.dumps(metadata, sort_keys=True)

    return JobSpec(
        path=path,
        job_id=job_id,
        source_ids=source_ids,
        weights=weights,
        angles=angles,
        invalid_lhe_indices=invalid_lhe_indices,
        normalization=normalization,
        alternative_ids=alternative_ids,
        alternative_values=alternative_values.copy(),
    )


@pytest.fixture
def two_job_inputs(tmp_path: Path) -> tuple[JobSpec, JobSpec]:
    """Two unequal jobs whose safety-stream and retained sums intentionally differ."""

    first = write_job(
        tmp_path / "job-a.root",
        job_id=11,
        source_ids=(101, 102, 103),
        weights=(2.0, -0.5, 0.0),
        angles=(
            (0.0, 0.0, 0.0, 0.0),
            (math.pi / 2.0, 0.0, math.pi / 2.0, 0.0),
            (math.nan, math.nan, math.nan, math.nan),
        ),
        invalid_lhe_indices=(2,),
        normalization=Normalization(
            generated=6,
            accepted=5,
            sumw_generated=4.0,
            sumw2_generated=8.0,
            sumabsw_generated=7.0,
            sumw_accepted=3.0,
            sumw2_accepted=6.0,
            sumabsw_accepted=5.0,
        ),
        alternative_values=np.asarray(
            [[1.8, 2.2], [-0.45, -0.55], [0.0, 0.0]], dtype=np.float64
        ),
    )
    second = write_job(
        tmp_path / "job-b.root",
        job_id=12,
        source_ids=(201, 202),
        weights=(1.25, -0.25),
        angles=(
            (math.pi / 4.0, 0.0, math.pi / 4.0, 0.0),
            (math.pi / 4.0, 0.0, math.pi / 4.0, math.pi / 2.0),
        ),
        normalization=Normalization(
            generated=4,
            accepted=3,
            sumw_generated=2.0,
            sumw2_generated=3.0,
            sumabsw_generated=3.0,
            sumw_accepted=1.0,
            sumw2_accepted=2.0,
            sumabsw_accepted=2.0,
        ),
        alternative_values=np.asarray(
            [[1.125, 1.375], [-0.225, -0.275]], dtype=np.float64
        ),
    )
    return first, second
