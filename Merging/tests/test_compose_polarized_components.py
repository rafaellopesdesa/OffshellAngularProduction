from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
import uproot

from Analysis.build_analysis_tree import output_schema
from Merging.compose_polarized_components import (
    COMPONENT_SLUGS,
    COMPONENT_WEIGHT_BRANCHES,
    POLARIZATION_CHANNELS,
    POLARIZATION_CHANNEL_CODES,
    POLARIZATION_COMBINATION_COEFFICIENTS,
    VPOLAR_SAMPLE_CODES,
    ProvenanceError,
    compose_polarized_components,
)
from Merging.merge_analysis_outputs import (
    BASE_RUN_SCHEMA,
    _weight_tree_schema,
    merge_analysis_outputs,
)

from conftest import Normalization, write_job


SOURCE_CROSS_SECTIONS = {
    "LL": 10.0,
    "TT": 20.0,
    "TL": 30.0,
    "LT": 40.0,
}
REALIZED_JOB_ARTIFACT_SHA256_KEYS = (
    "run_card_sha256",
    "pythia_card_sha256",
    "madgraph_command_card_sha256",
    "generation_config_sha256",
    "shower_log_sha256",
)


def _replace_analysis_metadata(path: Path, metadata: dict[str, object]) -> None:
    with uproot.open(path) as root_file:
        events = root_file["Events"].arrays(library="np", how=dict)
        runs = root_file["Runs"].arrays(library="np", how=dict)
        weights = (
            root_file["LHEWeights"].arrays(library="np", how=dict)
            if "LHEWeights" in root_file
            else None
        )
    generation = metadata["provenance"]["generation"]
    if generation.get("generator_backend") == (
        "madgraph5-pythia8-vpolar-standalone"
    ):
        generation.pop("athgeneration_release", None)
        for name in (
            "athgeneration_release_major",
            "athgeneration_release_minor",
            "athgeneration_release_patch",
        ):
            runs[name][:] = 0
    with uproot.recreate(path) as root_file:
        root_file.mktree("Events", output_schema())
        root_file["Events"].extend(events)
        root_file.mktree("Runs", BASE_RUN_SCHEMA)
        root_file["Runs"].extend(runs)
        if weights is not None:
            weight_ids = metadata["lhe_alternative_weights"]["ids"]
            root_file.mktree("LHEWeights", _weight_tree_schema(len(weight_ids)))
            root_file["LHEWeights"].extend(weights)
        root_file["analysis_metadata"] = json.dumps(metadata, sort_keys=True)


def _replace_merge_metadata(path: Path, metadata: dict[str, object]) -> None:
    with uproot.update(path) as root_file:
        root_file["merge_metadata"] = json.dumps(metadata, sort_keys=True)


def _set_polarization_metadata(
    path: Path,
    channel: str,
    *,
    interference: str | None = None,
    m4l_min_gev: float | None = None,
    bind_vpolar_role: bool = True,
    generator_backend: str = "madgraph5-pythia8-vpolar-standalone",
    generation_overrides: dict[str, object] | None = None,
) -> None:
    with uproot.open(path) as root_file:
        metadata = json.loads(str(root_file["analysis_metadata"]))
    sample = f"vpolar_{channel}"
    sample_code = VPOLAR_SAMPLE_CODES[channel]
    provenance = metadata["provenance"]
    if bind_vpolar_role:
        metadata["sample"] = sample
        metadata["sample_code"] = sample_code
        for stage in ("generation", "lhe_contract", "alignment", "simulation"):
            provenance[stage]["process"] = sample
    generation = provenance["generation"]
    job_token = f"{channel}:{generation['seed']}"

    def job_artifact_sha256(role: str) -> str:
        return hashlib.sha256(f"{job_token}:{role}".encode()).hexdigest()

    generation.update(
        {
            "generator_backend": generator_backend,
            "athgeneration_release_applicable": False,
            "generator_mll_max_gev": 200.0,
            "final_state": "e+e-mu+mu-",
            "full_amplitude": True,
            "photon_diagrams": False,
            "polarization_component": channel,
            "polarization_z1_decay": "mumu",
            "polarization_z2_decay": "ee",
            "polarization_frame": "four_lepton_rest_frame",
            "madgraph_me_frame": "3,4,5,6",
            "mixed_polarization_interference": interference
            or ("not_applicable" if channel in {"LL", "TT"} else "excluded"),
            "mixed_sample_definition": (
                "incoherent_concatenation_of_separate_TL_and_LT"
            ),
            "madgraph_version": "3.4.2",
            "pythia_version": "8.312",
            "hepmc_version": "2.06.11",
            "ufo_version": "2401.17365",
            "ufo_sha256": "1" * 64,
            "loop_filter_sha256": "2" * 64,
            "loop_filter_patch_sha256": "3" * 64,
            "installation_manifest_sha256": "4" * 64,
            "process_card_sha256": str(
                5 + POLARIZATION_CHANNELS.index(channel)
            )
            * 64,
            "run_card_sha256": job_artifact_sha256("run-card"),
            "param_card_sha256": "7" * 64,
            "pythia_card_sha256": job_artifact_sha256("pythia-card"),
            "madgraph_command_card_sha256": job_artifact_sha256(
                "madgraph-command-card"
            ),
            "generation_config": "generation-config.json",
            "generation_config_sha256": job_artifact_sha256("generation-config"),
            "run_generation_sha256": "9" * 64,
            "lhe_contract_script_sha256": "a" * 64,
            "alignment_script_sha256": "b" * 64,
            "pdf_set": "NNPDF31_nlo_as_0118_luxqed",
            "pdf_id": 324900,
            "shower_profile": "paper_monash",
            "pythia_tune_pp": 14,
            "pythia_pdf_pset": 13,
            "hepmc_weight_scaling": 2_000_000_000,
            "hepmc_file": "events.hepmc",
            "shower_log": "shower.log",
            "shower_log_sha256": job_artifact_sha256("shower-log"),
            "madloop_card_sha256": "c" * 64,
            "loop_reduction_backend": "CutTools",
            "loop_optimized_output": True,
            "madloop_reduction_lib": 1,
            "ninja_enabled": False,
            "collier_enabled": False,
            "loop_output_dependencies": "external",
            "lhe_archive": "LHE.TXT.tar.gz",
            "matched_lhe_file": "events.matched.lhe.gz",
            "alignment_metadata": "alignment-metadata.json",
            "lhe_event_id_metadata": "lhe-contract-metadata.json",
        }
    )
    alignment = provenance["alignment"]
    alignment.pop("athgeneration_release", None)
    alignment.pop("job_option_sha256", None)
    alignment.update(
        {
            "schema_version": 3,
            "generator_backend": generator_backend,
            "generation_config_sha256": generation["generation_config_sha256"],
            "shower_log_sha256": generation["shower_log_sha256"],
        }
    )
    provenance["simulation"].update(
        {
            "schema_version": 3,
            "dressed_lepton_origin": (
                "direct_hard_gg,non_hadronic,exact_signed_e_mu_copy_chain"
            ),
            "dressed_lepton_origin_policy": "vpolar_direct_hard_gg_v1",
            "dressed_lepton_direct_hard_process_candidates": True,
            "dressed_lepton_exact_2e2mu_validated": True,
        }
    )
    if m4l_min_gev is not None:
        generation["generator_m4l_min_gev"] = m4l_min_gev
        metadata["provenance"]["lhe_contract"]["m4l_min_gev"] = m4l_min_gev
    if generation_overrides:
        generation.update(generation_overrides)
    _replace_analysis_metadata(path, metadata)


def _normalization(cross_section_pb: float) -> Normalization:
    # A/N = (4*x)/4 = x for every source job.  The retained raw weights
    # deliberately sum to one rather than x, exercising the prior merger's
    # source-specific nominal scale before the polarization combination.
    return Normalization(
        generated=4,
        accepted=2,
        sumw_generated=4.0 * cross_section_pb,
        sumw2_generated=8.0 * cross_section_pb**2,
        sumabsw_generated=4.0 * cross_section_pb,
        sumw_accepted=4.0 * cross_section_pb,
        sumw2_accepted=8.0 * cross_section_pb**2,
        sumabsw_accepted=4.0 * cross_section_pb,
    )


def _make_merged_channel(
    tmp_path: Path,
    channel: str,
    *,
    interference: str | None = None,
    m4l_min_gev: float | None = None,
    alternative_ids: tuple[str, ...] = ("1001", "2001"),
    bind_vpolar_role: bool = True,
    generator_backend: str = "madgraph5-pythia8-vpolar-standalone",
    generation_overrides: dict[str, object] | None = None,
) -> Path:
    channel_index = POLARIZATION_CHANNELS.index(channel)
    job_files: list[Path] = []
    for within_channel in range(2):
        job_id = 100 + 10 * channel_index + within_channel
        path = tmp_path / f"{channel}_{job_id}.root"
        write_job(
            path,
            job_id=job_id,
            source_ids=(1, 2),
            weights=(2.0, -1.0),
            angles=((0.4, 0.1, 0.8, -0.2), (0.5, 0.2, 0.9, -0.3)),
            normalization=_normalization(SOURCE_CROSS_SECTIONS[channel]),
            sample_code=(VPOLAR_SAMPLE_CODES[channel] if bind_vpolar_role else 0),
            alternative_ids=alternative_ids,
        )
        _set_polarization_metadata(
            path,
            channel,
            interference=interference,
            m4l_min_gev=m4l_min_gev,
            bind_vpolar_role=bind_vpolar_role,
            generator_backend=generator_backend,
            generation_overrides=generation_overrides,
        )
        job_files.append(path)
    merged = tmp_path / f"merged_{channel}.root"
    merge_analysis_outputs(job_files, merged, step_size="1 kB")
    return merged


@pytest.fixture
def polarized_sources(tmp_path: Path) -> dict[str, Path]:
    return {
        channel: _make_merged_channel(tmp_path, channel)
        for channel in POLARIZATION_CHANNELS
    }


def _compose(sources: dict[str, Path], output: Path) -> dict[str, object]:
    return compose_polarized_components(
        ll=sources["LL"],
        tt=sources["TT"],
        tl=sources["TL"],
        lt=sources["LT"],
        output=output,
        step_size="1 kB",
    )


def test_coefficients_follow_symmetric_harmonic_convention() -> None:
    a_longitudinal = -1.0 / math.sqrt(5.0)
    a_transverse = 1.0 / (2.0 * math.sqrt(5.0))
    expected_single = {
        "LL": (a_longitudinal, a_longitudinal),
        "TT": (a_transverse, a_transverse),
        "TL": (a_transverse, a_longitudinal),
        "LT": (a_longitudinal, a_transverse),
    }
    for channel, (a1, a2) in expected_single.items():
        assert POLARIZATION_COMBINATION_COEFFICIENTS["00_20"][channel] == (
            pytest.approx((a1 + a2) / math.sqrt(2.0))
        )
        assert POLARIZATION_COMBINATION_COEFFICIENTS["20_20"][channel] == (
            pytest.approx(a1 * a2)
        )
    assert POLARIZATION_COMBINATION_COEFFICIENTS["mixed_incoherent"] == {
        "LL": 0.0,
        "TT": 0.0,
        "TL": 1.0,
        "LT": 1.0,
    }


def test_composes_both_signed_samples_without_renormalizing_sources(
    tmp_path: Path,
    polarized_sources: dict[str, Path],
) -> None:
    for key in REALIZED_JOB_ARTIFACT_SHA256_KEYS:
        all_hashes: list[str] = []
        for source_path in polarized_sources.values():
            with uproot.open(source_path) as root_file:
                source_metadata = json.loads(str(root_file["merge_metadata"]))
            source_hashes = [
                embedded["analysis_metadata"]["provenance"]["generation"][key]
                for embedded in source_metadata["inputs"]
            ]
            assert len(set(source_hashes)) == len(source_hashes)
            all_hashes.extend(source_hashes)
        assert len(set(all_hashes)) == len(all_hashes)

    output = tmp_path / "polarized_components.root"
    result = _compose(polarized_sources, output)

    expected_integrals = {
        slug: math.fsum(
            POLARIZATION_COMBINATION_COEFFICIENTS[slug][channel]
            * SOURCE_CROSS_SECTIONS[channel]
            for channel in POLARIZATION_CHANNELS
        )
        for slug in COMPONENT_SLUGS
    }
    assert result["event_count"] == 16
    assert result["source_job_count"] == 8
    assert result["integral_00_20_pb"] == pytest.approx(
        expected_integrals["00_20"]
    )
    assert result["integral_20_20_pb"] == pytest.approx(-4.0)
    assert result["integral_mixed_incoherent_pb"] == pytest.approx(70.0)

    with uproot.open(output) as root_file:
        events = root_file["Events"].arrays(library="np")
        runs = root_file["Runs"].arrays(library="np")
        sources = root_file["PolarizationSources"].arrays(library="np")
        summary = root_file["PolarizationCombinationSummary"].arrays(library="np")
        alternative = root_file["LHEWeights"].arrays(library="np")
        metadata = json.loads(str(root_file["polarization_combination_metadata"]))

    assert len(events["weight_lhe"]) == 16
    assert len(runs["job_id"]) == 8
    assert len(alternative["campaign_id"]) == 16
    np.testing.assert_array_equal(
        events["source_polarization_code"],
        np.repeat(
            [POLARIZATION_CHANNEL_CODES[channel] for channel in POLARIZATION_CHANNELS],
            4,
        ),
    )
    np.testing.assert_array_equal(
        events["weight_lhe"], np.tile(np.asarray([2.0, -1.0, 2.0, -1.0]), 4)
    )

    offset = 0
    for channel in POLARIZATION_CHANNELS:
        source_path = polarized_sources[channel]
        with uproot.open(source_path) as root_file:
            source_events = root_file["Events"].arrays(library="np")
        size = len(source_events["weight_lhe"])
        selected = slice(offset, offset + size)
        # Both the raw generator weight and the already normalized source
        # weight remain byte-for-byte values, while each logical sample gets
        # its own constant channel multiplier.
        np.testing.assert_array_equal(
            events["weight_lhe"][selected], source_events["weight_lhe"]
        )
        np.testing.assert_array_equal(
            events["weight_nominal_pb"][selected],
            source_events["weight_nominal_pb"],
        )
        np.testing.assert_array_equal(
            events["weight_truth_00_20_pb"][selected],
            source_events["weight_truth_00_20_pb"],
        )
        for slug in COMPONENT_SLUGS:
            coefficient = POLARIZATION_COMBINATION_COEFFICIENTS[slug][channel]
            np.testing.assert_array_equal(
                events[f"polarization_coefficient_{slug}"][selected],
                np.full(size, coefficient),
            )
            np.testing.assert_array_equal(
                events[COMPONENT_WEIGHT_BRANCHES[slug]][selected],
                source_events["weight_nominal_pb"] * coefficient,
            )
        offset += size

    for slug in COMPONENT_SLUGS:
        assert math.fsum(events[COMPONENT_WEIGHT_BRANCHES[slug]]) == pytest.approx(
            expected_integrals[slug]
        )
        assert summary[f"sumw_{slug}_pb"][0] == pytest.approx(
            expected_integrals[slug]
        )
        assert summary[f"expected_integral_{slug}_pb"][0] == pytest.approx(
            expected_integrals[slug]
        )
        assert abs(summary[f"closure_residual_{slug}_pb"][0]) <= summary[
            f"closure_tolerance_{slug}_pb"
        ][0]

    np.testing.assert_array_equal(
        sources["source_polarization_code"],
        [POLARIZATION_CHANNEL_CODES[channel] for channel in POLARIZATION_CHANNELS],
    )
    np.testing.assert_allclose(
        sources["source_cross_section_pb"],
        [SOURCE_CROSS_SECTIONS[channel] for channel in POLARIZATION_CHANNELS],
    )
    assert metadata["event_ordering"] == list(POLARIZATION_CHANNELS)
    assert metadata["z_assignment"] == {
        "Z1": "mu+mu-",
        "Z2": "e+e-",
        "polarization_label_order": "Z1,Z2",
    }
    assert metadata["normalization"]["source_weight_preserved"] is True
    assert metadata["normalization"]["component_weights_renormalized"] is False
    assert metadata["validity_scope"]["coherent_mixed_input"] == "forbidden"


def test_rejects_swapped_channel_argument(
    tmp_path: Path,
    polarized_sources: dict[str, Path],
) -> None:
    output = tmp_path / "swapped.root"
    with pytest.raises(ProvenanceError, match="--ll requires sample vpolar_LL"):
        compose_polarized_components(
            ll=polarized_sources["TT"],
            tt=polarized_sources["LL"],
            tl=polarized_sources["TL"],
            lt=polarized_sources["LT"],
            output=output,
        )
    assert not output.exists()


def test_rejects_ordinary_sample_with_injected_polarization_strings(
    tmp_path: Path,
    polarized_sources: dict[str, Path],
) -> None:
    sources = dict(polarized_sources)
    malformed_dir = tmp_path / "ordinary_source"
    malformed_dir.mkdir()
    sources["LL"] = _make_merged_channel(
        malformed_dir,
        "LL",
        bind_vpolar_role=False,
    )
    output = tmp_path / "relabeled_gg4l.root"
    with pytest.raises(ProvenanceError, match="requires sample vpolar_LL"):
        _compose(sources, output)
    assert not output.exists()


def test_rejects_non_vpolar_generator_backend(
    tmp_path: Path,
    polarized_sources: dict[str, Path],
) -> None:
    sources = dict(polarized_sources)
    malformed_dir = tmp_path / "wrong_backend_source"
    malformed_dir.mkdir()
    sources["LL"] = _make_merged_channel(
        malformed_dir,
        "LL",
        generator_backend="athgeneration",
    )
    output = tmp_path / "wrong_backend.root"
    with pytest.raises(ProvenanceError, match="must use generator backend"):
        _compose(sources, output)
    assert not output.exists()


def test_rejects_json_null_polarization_contract_value(
    tmp_path: Path,
    polarized_sources: dict[str, Path],
) -> None:
    with uproot.open(polarized_sources["TL"]) as root_file:
        metadata = json.loads(str(root_file["merge_metadata"]))
    generation = metadata["inputs"][0]["analysis_metadata"]["provenance"][
        "generation"
    ]
    generation["mixed_polarization_interference"] = None
    _replace_merge_metadata(polarized_sources["TL"], metadata)

    output = tmp_path / "null_interference.root"
    with pytest.raises(ProvenanceError, match="fields must be strings"):
        _compose(polarized_sources, output)
    assert not output.exists()


def test_rejects_duplicate_alternative_weight_ids(
    tmp_path: Path,
    polarized_sources: dict[str, Path],
) -> None:
    with uproot.open(polarized_sources["LL"]) as root_file:
        metadata = json.loads(str(root_file["merge_metadata"]))
    metadata["lhe_alternative_weights"]["ids"] = ["1001", "1001"]
    _replace_merge_metadata(polarized_sources["LL"], metadata)

    output = tmp_path / "duplicate_alternative_ids.root"
    with pytest.raises(ProvenanceError, match="unique lexicographic order"):
        _compose(polarized_sources, output)
    assert not output.exists()


def test_rejects_coherent_mixed_source(tmp_path: Path) -> None:
    sources = {
        channel: _make_merged_channel(
            tmp_path,
            channel,
            interference="included" if channel == "TL" else None,
        )
        for channel in POLARIZATION_CHANNELS
    }
    output = tmp_path / "coherent.root"
    with pytest.raises(ProvenanceError, match="not an interference-free"):
        _compose(sources, output)
    assert not output.exists()


def test_rejects_nonpolarization_physics_mismatch(tmp_path: Path) -> None:
    sources = {
        channel: _make_merged_channel(
            tmp_path,
            channel,
            m4l_min_gev=151.0 if channel == "LT" else None,
        )
        for channel in POLARIZATION_CHANNELS
    }
    output = tmp_path / "incompatible.root"
    with pytest.raises(ProvenanceError, match="incompatible VPolar generation"):
        _compose(sources, output)
    assert not output.exists()


def test_rejects_vpolar_software_mismatch(tmp_path: Path) -> None:
    sources = {
        channel: _make_merged_channel(
            tmp_path,
            channel,
            generation_overrides=(
                {"ufo_version": "different-ufo"} if channel == "LT" else None
            ),
        )
        for channel in POLARIZATION_CHANNELS
    }
    output = tmp_path / "incompatible_ufo.root"
    with pytest.raises(ProvenanceError, match="VPolar generation invariants"):
        _compose(sources, output)
    assert not output.exists()


def test_rejects_immutable_vpolar_card_mismatch(tmp_path: Path) -> None:
    sources = {
        channel: _make_merged_channel(
            tmp_path,
            channel,
            generation_overrides=(
                {"param_card_sha256": "c" * 64} if channel == "LT" else None
            ),
        )
        for channel in POLARIZATION_CHANNELS
    }
    output = tmp_path / "incompatible_param_card.root"
    with pytest.raises(ProvenanceError, match="VPolar generation invariants"):
        _compose(sources, output)
    assert not output.exists()


def test_requires_four_distinct_source_files(
    tmp_path: Path,
    polarized_sources: dict[str, Path],
) -> None:
    with pytest.raises(ValueError, match="four distinct"):
        compose_polarized_components(
            ll=polarized_sources["LL"],
            tt=polarized_sources["LL"],
            tl=polarized_sources["TL"],
            lt=polarized_sources["LT"],
            output=tmp_path / "duplicate.root",
        )


def test_composes_inputs_without_optional_lheweights(tmp_path: Path) -> None:
    sources = {
        channel: _make_merged_channel(tmp_path, channel, alternative_ids=())
        for channel in POLARIZATION_CHANNELS
    }
    output = tmp_path / "no_alternative_weights.root"
    _compose(sources, output)

    with uproot.open(output) as root_file:
        assert "LHEWeights" not in root_file
        assert int(root_file["Events"].num_entries) == 16
