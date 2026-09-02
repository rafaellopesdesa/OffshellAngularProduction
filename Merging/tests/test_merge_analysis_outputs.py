from __future__ import annotations

import hashlib
import importlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
import uproot

from Merging.merge_analysis_outputs import MergeError, ProvenanceError, merge_analysis_outputs

from conftest import JobSpec, Normalization, write_job


TRUTH_SLUGS = ("00_20", "20_20", "2m1_2p1", "2m2_2p2")
merger = importlib.import_module("Merging.merge_analysis_outputs")


def _scalar(arrays: object, name: str) -> int | float:
    return arrays[name][0].item()  # type: ignore[index]


def _replace_analysis_metadata(path: Path, metadata: dict[str, object]) -> None:
    """Publish a newer TObjString cycle without disturbing the event trees."""

    with uproot.update(path) as root_file:
        root_file["analysis_metadata"] = json.dumps(metadata, sort_keys=True)


def _replace_run_scalar(path: Path, name: str, value: int) -> None:
    """Publish a newer Runs cycle with one scalar changed for a failure test."""

    with uproot.open(path) as root_file:
        arrays = root_file["Runs"].arrays(library="np", how=dict)
    arrays[name][0] = value
    with uproot.update(path) as root_file:
        root_file.mktree("Runs", merger.BASE_RUN_SCHEMA)
        root_file["Runs"].extend(arrays)


def _replace_seed_in_run_and_metadata(path: Path, name: str, value: int) -> None:
    """Keep the two redundant seed records coherent for duplicate tests."""

    _replace_run_scalar(path, name, value)
    with uproot.open(path) as root_file:
        metadata = json.loads(str(root_file["analysis_metadata"]))
    if name == "generation_seed":
        metadata["provenance"]["generation"]["seed"] = value
    elif name == "delphes_seed":
        metadata["provenance"]["simulation"]["random_seed"] = value
    else:  # pragma: no cover - helper is deliberately seed-specific
        raise ValueError(f"unsupported seed branch {name}")
    _replace_analysis_metadata(path, metadata)


def test_two_job_merge_pools_safety_stream_and_writes_truth_weights(
    tmp_path: Path,
    two_job_inputs: tuple[JobSpec, JobSpec],
):
    first, second = two_job_inputs
    output = tmp_path / "merged.root"

    result = merge_analysis_outputs(
        [first.path, second.path], output, step_size="1 kB"
    )

    raw = np.asarray((*first.weights, *second.weights), dtype=np.float64)
    expected_scale = 0.4 / 2.5
    expected_nominal = raw * expected_scale
    assert result["effective_filtered_cross_section_pb"] == pytest.approx(0.4)
    assert result["merged_weight_scale"] == pytest.approx(expected_scale)
    assert result["sumw_nominal_pb"] == pytest.approx(0.4)

    with uproot.open(output) as root_file:
        events = root_file["Events"].arrays(library="np")
        runs = root_file["Runs"].arrays(library="np")
        summary = root_file["MergeSummary"].arrays(library="np")
        alternative = root_file["LHEWeights"].arrays(library="np")
        metadata = json.loads(str(root_file["merge_metadata"]))

    # The retained raw sum (2.5 pb) intentionally differs from the accepted
    # safety-stream sum (4.0 pb). The latter defines 4/10 = 0.4 pb, while one
    # common event scale preserves all raw relative signs and zeros.
    np.testing.assert_array_equal(events["weight_lhe"], raw)
    np.testing.assert_allclose(events["weight_nominal_pb"], expected_nominal)
    assert math.fsum(events["weight_nominal_pb"]) == pytest.approx(0.4)
    np.testing.assert_array_equal(np.signbit(events["weight_lhe"]), np.signbit(raw))
    assert events["weight_nominal_pb"][2] == 0.0
    assert _scalar(summary, "normalization_generated_lhe_events") == 10
    assert _scalar(summary, "normalization_accepted_lhe_events") == 8
    assert _scalar(summary, "normalization_sumw_accepted_pb") == pytest.approx(4.0)
    assert _scalar(summary, "retained_raw_sumw_pb") == pytest.approx(2.5)
    assert _scalar(summary, "effective_filtered_cross_section_pb") == pytest.approx(0.4)
    assert _scalar(summary, "inclusive_lhe_cross_section_pb") == pytest.approx(0.6)
    assert _scalar(summary, "effective_filtered_cross_section_mc_error_pb") == pytest.approx(
        math.sqrt((8.0 - 4.0**2 / 10.0) / (10.0 * 9.0))
    )
    assert _scalar(summary, "positive_weight_count") == 2
    assert _scalar(summary, "negative_weight_count") == 2
    assert _scalar(summary, "zero_weight_count") == 1
    assert _scalar(summary, "truth_lhe_valid_count") == 4

    np.testing.assert_array_equal(runs["job_id"], [11, 12])
    np.testing.assert_array_equal(runs["event_count"], [3, 2])
    np.testing.assert_allclose(runs["normalization_sumw_accepted_pb"], [3.0, 1.0])

    sqrt2 = math.sqrt(2.0)
    expected_factors = {
        "00_20": np.asarray(
            [math.sqrt(10.0), -math.sqrt(5.0 / 2.0), math.nan, math.sqrt(5.0 / 8.0), math.sqrt(5.0 / 8.0)]
        ),
        "20_20": np.asarray([5.0, 5.0 / 4.0, math.nan, 5.0 / 16.0, 5.0 / 16.0]),
        "2m1_2p1": np.asarray([0.0, 0.0, math.nan, -15.0 * sqrt2 / 8.0, 0.0]),
        "2m2_2p2": np.asarray([0.0, 15.0 * sqrt2 / 8.0, math.nan, 15.0 * sqrt2 / 32.0, -15.0 * sqrt2 / 32.0]),
    }
    np.testing.assert_array_equal(events["truth_lhe_valid"], [True, True, False, True, True])
    for slug, factors in expected_factors.items():
        np.testing.assert_allclose(events[f"truth_factor_{slug}"], factors, rtol=2.0e-14, atol=2.0e-14)
        np.testing.assert_allclose(events[f"truth_h_{slug}"], factors / (4.0 * math.pi), rtol=2.0e-14, atol=2.0e-14)
        np.testing.assert_allclose(events[f"weight_truth_{slug}_pb"], expected_nominal * factors, rtol=2.0e-14, atol=2.0e-14)
        assert math.isnan(events[f"weight_truth_{slug}_pb"][2])

    # Alternative LHE weights and their exact event identity columns are copied,
    # not normalized or rewritten.
    expected_alternative = np.concatenate(
        [first.alternative_values, second.alternative_values], axis=0
    )
    np.testing.assert_array_equal(alternative["values"], expected_alternative)
    for identity in (
        "campaign_id",
        "sample_code",
        "job_id",
        "lhe_event_index",
        "source_event_id",
        "event_uid_hi",
        "event_uid_lo",
    ):
        np.testing.assert_array_equal(alternative[identity], events[identity])

    assert metadata["normalization"]["values"]["effective_filtered_cross_section_pb"] == pytest.approx(0.4)
    assert metadata["merged_nominal_weight"]["raw_branch_immutable"] is True
    assert metadata["truth_angular_weights"]["angles"] == [
        "lhe_theta1",
        "lhe_phi1",
        "lhe_theta2",
        "lhe_phi2",
    ]


@pytest.mark.parametrize(
    "second_sources",
    (
        (101, 202),
        (201, 202),
    ),
)
def test_rejects_duplicate_source_job_even_with_different_source_ids(
    tmp_path: Path,
    second_sources: tuple[int, ...],
):
    normalization = Normalization(4, 2, 2.0, 3.0, 3.0, 1.0, 2.0, 2.0)
    first = write_job(
        tmp_path / "first.root",
        job_id=11,
        source_ids=(101, 102),
        weights=(1.25, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )
    second = write_job(
        tmp_path / "second.root",
        job_id=11,
        source_ids=second_sources,
        weights=(1.25, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )

    with pytest.raises(MergeError, match="duplicate .* source jobs"):
        merge_analysis_outputs([first.path, second.path], tmp_path / "merged.root")


def test_rejects_mixed_samples(tmp_path: Path):
    normalization = Normalization(4, 2, 2.0, 3.0, 3.0, 1.0, 2.0, 2.0)
    first = write_job(
        tmp_path / "gg.root",
        job_id=21,
        source_ids=(1, 2),
        weights=(1.25, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )
    second = write_job(
        tmp_path / "qq.root",
        job_id=22,
        source_ids=(3, 4),
        weights=(1.25, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
        sample_code=1,
    )

    with pytest.raises(ProvenanceError, match="one campaign and sample"):
        merge_analysis_outputs([first.path, second.path], tmp_path / "mixed.root")


def test_rejects_incompatible_alternative_lhe_weight_ids(tmp_path: Path):
    normalization = Normalization(4, 2, 2.0, 3.0, 3.0, 1.0, 2.0, 2.0)
    common = dict(
        source_ids=(1, 2),
        weights=(1.25, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )
    first = write_job(tmp_path / "first.root", job_id=31, **common)
    second = write_job(
        tmp_path / "second.root",
        job_id=32,
        alternative_ids=("1001", "3001"),
        **common,
    )

    with pytest.raises(MergeError, match="incompatible physics/provenance invariants"):
        merge_analysis_outputs([first.path, second.path], tmp_path / "merged.root")


def test_seed_resolved_card_hash_is_job_specific(
    tmp_path: Path,
    two_job_inputs: tuple[JobSpec, JobSpec],
):
    first, second = two_job_inputs
    with uproot.open(second.path) as root_file:
        metadata = json.loads(str(root_file["analysis_metadata"]))
    resolved = "f" * 64
    metadata["provenance"]["simulation"]["resolved_card_sha256"] = resolved
    _replace_analysis_metadata(second.path, metadata)

    output = tmp_path / "different-resolved-cards.root"
    merge_analysis_outputs([first.path, second.path], output)

    with uproot.open(output) as root_file:
        merged_metadata = json.loads(str(root_file["merge_metadata"]))
    embedded = [
        source["analysis_metadata"]["provenance"]["simulation"][
            "resolved_card_sha256"
        ]
        for source in merged_metadata["inputs"]
    ]
    assert embedded == ["e" * 64, resolved]


def test_rejects_nonfinite_lhe_angle_marked_projection_valid(tmp_path: Path):
    normalization = Normalization(4, 2, 2.0, 3.0, 3.0, 1.0, 2.0, 2.0)
    bad = write_job(
        tmp_path / "bad-angle.root",
        job_id=41,
        source_ids=(1, 2),
        weights=(1.25, -0.25),
        angles=((math.nan, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )
    good = write_job(
        tmp_path / "good.root",
        job_id=42,
        source_ids=(3, 4),
        weights=(1.25, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )
    output = tmp_path / "merged.root"

    with pytest.raises(MergeError, match="non-finite LHE angles.*projection-valid"):
        merge_analysis_outputs([bad.path, good.path], output)
    assert not output.exists()
    assert not list(tmp_path.glob(".merged.root.partial.*"))


@pytest.mark.parametrize(
    ("epsilon", "message"),
    (
        (0.0, "retained raw weight sum is zero"),
        (1.0e-14, "retained signed-weight sum is numerically unresolved"),
    ),
)
def test_rejects_zero_or_unresolved_retained_signed_sum(
    tmp_path: Path,
    epsilon: float,
    message: str,
):
    normalization = Normalization(4, 2, 2.0, 3.0, 3.0, 1.0, 2.0, 2.0)
    first = write_job(
        tmp_path / "positive.root",
        job_id=51,
        source_ids=(1, 2),
        weights=(1.0, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )
    second = write_job(
        tmp_path / "cancelling.root",
        job_id=52,
        source_ids=(3, 4),
        weights=(-1.0 + epsilon, 0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
    )
    output = tmp_path / "merged.root"

    with pytest.raises(MergeError, match=message):
        merge_analysis_outputs([first.path, second.path], output)
    assert not output.exists()
    assert not list(tmp_path.glob(".merged.root.partial.*"))


def test_inputs_without_alternative_lhe_weights_merge_without_weight_tree(
    tmp_path: Path,
):
    normalization = Normalization(4, 2, 2.0, 3.0, 3.0, 1.0, 2.0, 2.0)
    common = dict(
        weights=(1.25, -0.25),
        angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
        normalization=normalization,
        alternative_ids=(),
    )
    first = write_job(
        tmp_path / "first.root", job_id=61, source_ids=(1, 2), **common
    )
    second = write_job(
        tmp_path / "second.root", job_id=62, source_ids=(3, 4), **common
    )
    output = tmp_path / "merged.root"

    merge_analysis_outputs([first.path, second.path], output)

    with uproot.open(output) as root_file:
        assert "LHEWeights" not in root_file
        summary = root_file["MergeSummary"].arrays(library="np")
        metadata = json.loads(str(root_file["merge_metadata"]))
    assert _scalar(summary, "alternative_lhe_weight_count") == 0
    assert metadata["lhe_alternative_weights"]["tree"] is None


@pytest.mark.parametrize(
    ("seed_branch", "message"),
    (
        ("generation_seed", "duplicate generation seeds"),
        ("delphes_seed", "duplicate Delphes seeds"),
    ),
)
def test_rejects_duplicate_random_seeds(
    tmp_path: Path,
    two_job_inputs: tuple[JobSpec, JobSpec],
    seed_branch: str,
    message: str,
):
    first, second = two_job_inputs
    with uproot.open(first.path) as root_file:
        first_seed = int(root_file["Runs"][seed_branch].array(library="np")[0])
    _replace_seed_in_run_and_metadata(second.path, seed_branch, first_seed)
    output = tmp_path / "merged.root"

    with pytest.raises(MergeError, match=message):
        merge_analysis_outputs([first.path, second.path], output)
    assert not output.exists()


def test_rejects_run_seed_disagreeing_with_embedded_provenance(
    tmp_path: Path,
    two_job_inputs: tuple[JobSpec, JobSpec],
):
    first, second = two_job_inputs
    _replace_run_scalar(second.path, "generation_seed", 987654)
    output = tmp_path / "merged.root"

    with pytest.raises(
        ProvenanceError,
        match=r"Runs\.generation_seed disagrees with embedded metadata",
    ):
        merge_analysis_outputs([first.path, second.path], output)
    assert not output.exists()


def test_overwrite_and_failed_publish_preserve_complete_output(
    tmp_path: Path,
    two_job_inputs: tuple[JobSpec, JobSpec],
    monkeypatch: pytest.MonkeyPatch,
):
    first, second = two_job_inputs
    output = tmp_path / "merged.root"
    merge_analysis_outputs([first.path, second.path], output)
    original_digest = hashlib.sha256(output.read_bytes()).hexdigest()

    with pytest.raises(FileExistsError, match="pass --overwrite"):
        merge_analysis_outputs([first.path, second.path], output)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == original_digest

    real_publish = merger._publish_output
    with monkeypatch.context() as scoped:
        def fail_publish(_temporary: Path, _output: Path, _overwrite: bool) -> None:
            raise RuntimeError("injected publish failure")

        scoped.setattr(merger, "_publish_output", fail_publish)
        with pytest.raises(RuntimeError, match="injected publish failure"):
            merge_analysis_outputs(
                [first.path, second.path], output, overwrite=True
            )

    assert hashlib.sha256(output.read_bytes()).hexdigest() == original_digest
    assert not list(tmp_path.glob(".merged.root.partial.*"))
    lock_path = tmp_path / ".merged.root.lock"
    assert lock_path.is_file()

    monkeypatch.setattr(merger, "_publish_output", real_publish)
    result = merge_analysis_outputs(
        [first.path, second.path], output, overwrite=True
    )
    assert result["event_count"] == 5
    assert output.is_file()
    assert not list(tmp_path.glob(".merged.root.partial.*"))
