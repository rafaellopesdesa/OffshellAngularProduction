from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import threading

import awkward as ak
import numpy as np
import pytest
import uproot

from Analysis.build_analysis_tree import (
    ANALYSIS_CODE_FILES,
    MARKER_ID_WEIGHT_NAME,
    MARKER_UNIT_WEIGHT_NAME,
    SAMPLE_CODES,
    MatchError,
    ProvenanceError,
    build_analysis_tree,
    event_uid,
)

LEPTONS = {
    11: (70.0, -60.0, 0.0, math.sqrt(1300.0)),
    -11: (50.0, -30.0, 10.0, -math.sqrt(1500.0)),
    13: (80.0, 70.0, 0.0, math.sqrt(1500.0)),
    -13: (50.0, 22.0, 0.0, -math.sqrt(2016.0)),
}


HEPMC_WEIGHT_NAMES = [
    "Default",
    "1001",
    MARKER_ID_WEIGHT_NAME,
    "2001",
    MARKER_UNIT_WEIGHT_NAME,
]


def _lhe_event(weight: float, source_event_id: int) -> str:
    particles = [
        " 21 -1 0 0 501 502 0 0 6500 6500 0 0 9",
        " 21 -1 0 0 502 501 0 0 -6500 6500 0 0 9",
    ]
    for pid in (11, -11, 13, -13):
        energy, px, py, pz = LEPTONS[pid]
        particles.append(
            f" {pid} 1 1 2 0 0 {px:.15g} {py:.15g} {pz:.15g} {energy:.15g} 0 0 9"
        )
    return (
        "<event>\n"
        f" 6 1 {weight:.17g} 350.0 0.007297 0.118\n"
        + "\n".join(particles)
        + "\n<rwgt>\n"
        + f" <wgt id='1001'>{weight * 0.9:.17g}</wgt>\n"
        + f" <wgt id='{MARKER_ID_WEIGHT_NAME}'>{source_event_id}</wgt>\n"
        + f" <wgt id='2001'>{weight * 1.1:.17g}</wgt>\n"
        + f" <wgt id='{MARKER_UNIT_WEIGHT_NAME}'>1</wgt>\n"
        + "</rwgt>"
        + "\n</event>"
    )


def _write_lhe(
    path: Path,
    weights: tuple[float, ...] = (-2.5, 1.5),
    source_ids: tuple[int, ...] = (2, 7),
) -> None:
    if len(weights) != len(source_ids):
        raise ValueError("weights and source_ids must have equal length")
    events = "\n".join(
        _lhe_event(weight, source_event_id)
        for weight, source_event_id in zip(weights, source_ids)
    )
    path.write_text(
        '<LesHouchesEvents version="3.0">\n'
        "<header></header>\n"
        "<init>\n"
        " 2212 2212 6500 6500 0 0 0 0 3 1\n"
        " 1.0 0.0 1.0 1\n"
        "</init>\n"
        f"{events}\n"
        "</LesHouchesEvents>\n"
    )


def _pt_eta_phi(pid: int) -> tuple[float, float, float]:
    _, px, py, pz = LEPTONS[pid]
    pt = math.hypot(px, py)
    return pt, math.asinh(pz / pt), math.atan2(py, px)


def _write_delphes(
    path: Path,
    event_numbers: tuple[int, ...],
    *,
    missing_reco_last: bool = False,
    source_ids: tuple[int, ...] | None = None,
    marker_pairs: tuple[tuple[float, float], ...] | None = None,
    cross_sections: tuple[float, ...] | None = None,
    cross_section_errors: tuple[float, ...] | None = None,
) -> None:
    size = len(event_numbers)
    if source_ids is None:
        source_ids = (2, 7)[:size]
    if len(source_ids) != size:
        raise ValueError("source_ids and event_numbers must have equal length")
    if marker_pairs is None:
        marker_pairs = tuple(
            (source_id * (3.0 + index), 3.0 + index)
            for index, source_id in enumerate(source_ids)
        )
    if len(marker_pairs) != size:
        raise ValueError("marker_pairs and event_numbers must have equal length")
    if cross_sections is None:
        cross_sections = tuple(0.25 + 0.05 * index for index in range(size))
    if cross_section_errors is None:
        cross_section_errors = tuple(0.01 + 0.005 * index for index in range(size))
    if len(cross_sections) != size or len(cross_section_errors) != size:
        raise ValueError(
            "cross-section diagnostics and event_numbers must have equal length"
        )
    electron_pid = [11, -11]
    muon_pid = [13, -13]

    def dressed(component: int, pids: list[int]):
        return ak.Array(
            [[LEPTONS[pid][component] for pid in pids] for _ in range(size)]
        )

    reco: dict[str, ak.Array] = {}
    for branch, pids in (("RecoElectron", electron_pid), ("RecoMuon", muon_pid)):
        values = [_pt_eta_phi(pid) for pid in pids]
        for component_index, component in enumerate(("PT", "Eta", "Phi")):
            rows = [[value[component_index] for value in values] for _ in range(size)]
            if missing_reco_last and rows:
                rows[-1] = []
            reco[f"{branch}.{component}"] = ak.Array(rows)
        charges = [[-1, 1] for _ in range(size)]
        if missing_reco_last and charges:
            charges[-1] = []
        reco[f"{branch}.Charge"] = ak.Array(charges)

    branches = {
        "Event.Number": ak.Array([[number] for number in event_numbers]),
        "Event.Weight": ak.Array([[(-2.5, 1.5)[index]] for index in range(size)]),
        "Event.CrossSection": ak.Array([[value] for value in cross_sections]),
        "Event.CrossSectionError": ak.Array(
            [[value] for value in cross_section_errors]
        ),
        # Delphes keeps the ordered HepMC weight collection but not its names.
        # A non-unit common factor exercises the zero-safe ratio decoder.
        "Weight.Weight": ak.Array(
            [
                [
                    (-2.5, 1.5)[index],
                    (-2.25, 1.35)[index],
                    marker_pairs[index][0],
                    (-2.75, 1.65)[index],
                    marker_pairs[index][1],
                ]
                for index in range(size)
            ]
        ),
        "DressedElectron.PID": ak.Array([electron_pid for _ in range(size)]),
        "DressedElectron.E": dressed(0, electron_pid),
        "DressedElectron.Px": dressed(1, electron_pid),
        "DressedElectron.Py": dressed(2, electron_pid),
        "DressedElectron.Pz": dressed(3, electron_pid),
        "DressedMuon.PID": ak.Array([muon_pid for _ in range(size)]),
        "DressedMuon.E": dressed(0, muon_pid),
        "DressedMuon.Px": dressed(1, muon_pid),
        "DressedMuon.Py": dressed(2, muon_pid),
        "DressedMuon.Pz": dressed(3, muon_pid),
        **reco,
    }
    with uproot.recreate(path) as root_file:
        root_file.mktree("Delphes", branches)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_id_sequence_sha256(source_ids: tuple[int, ...]) -> str:
    digest = hashlib.sha256()
    for source_event_id in source_ids:
        digest.update(source_event_id.to_bytes(8, "big"))
    return digest.hexdigest()


def _write_provenance(
    directory: Path,
    lhe: Path,
    delphes: Path,
    *,
    process: str,
    event_count: int = 2,
    source_ids: tuple[int, ...] = (2, 7),
    zero_sum_normalization: bool = False,
    single_generated_normalization: bool = False,
    standalone: bool = False,
) -> dict[str, Path]:
    if len(source_ids) != event_count:
        raise ValueError("source_ids and event_count must agree")
    generation = directory / "run-metadata.txt"
    lhe_contract = directory / "lhe-contract-metadata.json"
    alignment = directory / "alignment-metadata.json"
    simulation = directory / "simulation-metadata.txt"
    run_numbers = {
        "gg4l": 100001,
        "qqZZ": 100002,
        "vpolar_LL": 100003,
        "vpolar_TT": 100004,
        "vpolar_TL": 100005,
        "vpolar_LT": 100006,
    }
    run_number = run_numbers[process]
    m4l_min = 150
    backend_metadata = (
        "generator_backend=madgraph5-pythia8-vpolar-standalone\n"
        "matrix_element_seed=17\n"
        "shower_seed=17\n"
        f"polarization_component={process.removeprefix('vpolar_')}\n"
        "polarization_z1_decay=mumu\n"
        "polarization_z2_decay=ee\n"
        "polarization_frame=four_lepton_rest_frame\n"
        "madgraph_me_frame=3,4,5,6\n"
        "mixed_polarization_interference=not_applicable\n"
        "loop_reduction_backend=CutTools\n"
        "loop_optimized_output=true\n"
        "madloop_reduction_lib=1\n"
        "ninja_enabled=false\n"
        "collier_enabled=false\n"
        "loop_output_dependencies=external\n"
        if standalone
        else "athgeneration_release=23.6.41\njob_option=/tmp/job=option.py\n"
    )
    generation.write_text(
        "schema_version=1\n"
        f"process={process}\n"
        "seed=17\n"
        f"events={event_count}\n"
        "first_event=1\n"
        f"run_number={run_number}\n"
        "ecm_energy_gev=13600\n"
        f"{backend_metadata}"
        "generator_mll_min_gev=50\n"
        "generator_mll_max_gev=200\n"
        f"generator_m4l_min_gev={m4l_min}\n"
        "generator_m4l_max_gev=3000\n"
        "analysis_mz_min_gev=50\n"
        "analysis_mz_max_gev=106\n"
        "analysis_m4l_min_gev=180\n"
        "analysis_m4l_max_gev=none\n"
        "target_generation_phase_space_m4l_max_gev=3000\n"
        "alignment_contract=named-weight-id-v1\n"
        "lhe_event_id_contract=named-weight-id-v1\n",
        encoding="utf-8",
    )
    accepted_lhe_events = event_count
    if single_generated_normalization:
        if event_count != 1 or zero_sum_normalization:
            raise ValueError(
                "single-generated normalization requires one nonzero event"
            )
        generated_lhe_events = 1
        sumw_accepted = sumw_generated = 0.36
        sumw2_accepted = sumw2_generated = 0.36**2
        sumabsw_accepted = sumabsw_generated = 0.36
        signed_filter_efficiency = absolute_filter_efficiency = 1.0
    elif zero_sum_normalization:
        generated_lhe_events = event_count + 1
        accepted_weight = 0.2 / accepted_lhe_events
        sumw_accepted = 0.2
        sumw_generated = 0.0
        sumw2_accepted = accepted_weight**2 * accepted_lhe_events
        sumw2_generated = sumw2_accepted + sumw_accepted**2
        sumabsw_accepted = 0.2
        sumabsw_generated = 0.4
        signed_filter_efficiency = None
        absolute_filter_efficiency = 0.5
    else:
        generated_lhe_events = event_count + 1
        accepted_weight = 0.36
        sumw_accepted = accepted_weight * accepted_lhe_events
        sumw_generated = sumw_accepted / 0.8
        rejected_weight = sumw_generated - sumw_accepted
        sumw2_accepted = accepted_weight**2 * accepted_lhe_events
        sumw2_generated = sumw2_accepted + rejected_weight**2
        sumabsw_accepted = sumw_accepted
        sumabsw_generated = sumw_generated
        signed_filter_efficiency = 0.8
        absolute_filter_efficiency = 0.8

    def mc_error(sumw: float, sumw2: float) -> float | None:
        if generated_lhe_events < 2:
            return None
        return math.sqrt(
            max(sumw2 - sumw * sumw / generated_lhe_events, 0.0)
            / (generated_lhe_events * (generated_lhe_events - 1))
        )

    lhe_contract_payload = {
        "schema_version": 2,
        "contract": "named-weight-id-v1",
        "normalization_contract": "idwtup-minus4-sample-mean-v1",
        "nominal_weight_units": "pb",
        "lhe_weighting_strategy": -4,
        "cross_section_method": (
            "mean nominal LHE weight; rejected events assigned zero for filtered "
            "estimate"
        ),
        "process": process,
        "marker_id_weight": MARKER_ID_WEIGHT_NAME,
        "marker_unit_weight": MARKER_UNIT_WEIGHT_NAME,
        "hepmc_recovery_formula": ("AUX_OAP_EVENT_ID / AUX_OAP_EVENT_UNIT"),
        "requested_hepmc_events": event_count,
        "m4l_min_gev": float(m4l_min),
        "m4l_max_gev": 3000.0,
        "generated_lhe_events": generated_lhe_events,
        "accepted_lhe_events": accepted_lhe_events,
        "rejected_below_m4l": 0,
        "rejected_above_m4l": generated_lhe_events - accepted_lhe_events,
        "sumw_generated": sumw_generated,
        "sumw_accepted": sumw_accepted,
        "sumw2_generated": sumw2_generated,
        "sumw2_accepted": sumw2_accepted,
        "sumabsw_generated": sumabsw_generated,
        "sumabsw_accepted": sumabsw_accepted,
        "count_filter_efficiency": accepted_lhe_events / generated_lhe_events,
        "signed_filter_efficiency": signed_filter_efficiency,
        "absolute_filter_efficiency": absolute_filter_efficiency,
        "lhe_init": {
            "idwtup": -4,
            "process_count": 1,
            "processes": [
                {
                    "process_id": 1,
                    "cross_section_pb": 0.31,
                    "cross_section_error_pb": 0.01,
                    "maximum_weight_pb": 0.5,
                }
            ],
            "inclusive_cross_section_pb": 0.31,
            "inclusive_cross_section_error_pb": 0.01,
        },
        "inclusive_cross_section_pb": sumw_generated / generated_lhe_events,
        "inclusive_cross_section_mc_error_pb": mc_error(
            sumw_generated, sumw2_generated
        ),
        "filtered_cross_section_pb": sumw_accepted / generated_lhe_events,
        "filtered_cross_section_mc_error_pb": mc_error(
            sumw_accepted, sumw2_accepted
        ),
    }
    lhe_contract.write_text(
        json.dumps(lhe_contract_payload, indent=2) + "\n", encoding="utf-8"
    )
    generation_config_payload: dict[str, object] | None = None
    generation_config: Path | None = None
    shower_log: Path | None = None
    if standalone:
        card_paths = {
            "process": directory / "madgraph-process-card.dat",
            "run": directory / "madgraph-run-card.dat",
            "param": directory / "madgraph-param-card.dat",
            "madloop": directory / "madgraph-madloop-card.dat",
            "pythia": directory / "pythia8-card.cmnd",
        }
        for role, card_path in card_paths.items():
            card_path.write_text(
                (
                    "#MLReductionLib\n1\n"
                    if role == "madloop"
                    else f"synthetic realized {role} card\n"
                ),
                encoding="utf-8",
            )
        shower_log = directory / "pythia8.log"
        shower_log.write_text("synthetic Pythia log\n", encoding="utf-8")
        generation_config = directory / "generation-config.json"
        generation_config_payload = {
            "schema_version": 1,
            "contract": "oap-vpolar-generation-config-v1",
            "generator_backend": "madgraph5-pythia8-vpolar-standalone",
            "process": process,
            "polarization_component": process.removeprefix("vpolar_"),
            "run_number": run_number,
            "ecm_energy_gev": 13600.0,
            "requested_events": event_count,
            "generated_lhe_events": generated_lhe_events,
            "matrix_element_seed": 17,
            "shower_seed": 17,
            "mll_min_gev": 50.0,
            "mll_max_gev": 200.0,
            "m4l_min_gev": float(m4l_min),
            "m4l_max_gev": 3000.0,
            "loop_reduction": {
                "backend": "CutTools",
                "collier": None,
                "loop_optimized_output": True,
                "madloop_reduction_lib": "1",
                "ninja": None,
                "output_dependencies": "external",
            },
            "run_card_validation": {
                "exact_contract_checked": True,
                "automatic_pt_eta_dr_cuts_disabled": True,
            },
            "cards": {
                role: {
                    "path": card_path.name,
                    "path_scope": "generation_run_directory",
                    "sha256": _sha256(card_path),
                }
                for role, card_path in card_paths.items()
            },
        }
        generation_config.write_text(
            json.dumps(generation_config_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        with generation.open("a", encoding="utf-8") as stream:
            stream.write(
                f"generation_config={generation_config.name}\n"
                f"generation_config_sha256={_sha256(generation_config)}\n"
                f"shower_log={shower_log.name}\n"
                f"shower_log_sha256={_sha256(shower_log)}\n"
                + "".join(
                    f"{role}_card_sha256={_sha256(card_path)}\n"
                    for role, card_path in card_paths.items()
                )
            )
    hepmc_sha = hashlib.sha256(b"synthetic HepMC job").hexdigest()
    alignment_payload = {
        "schema_version": 3 if standalone else 2,
        "contract": "named-weight-id-v1",
        "process": process,
        "run_number": run_number,
        "random_seed": 17,
        "first_event": 1,
        "counts": {
            "requested_hepmc_events": event_count,
            "hepmc_events": event_count,
            "generated_lhe_events": generated_lhe_events,
            "phase_space_lhe_events": event_count,
            "matched_lhe_events": event_count,
        },
        "marker": {
            "id_weight_name": MARKER_ID_WEIGHT_NAME,
            "id_weight_index": 2,
            "unit_weight_name": MARKER_UNIT_WEIGHT_NAME,
            "unit_weight_index": 4,
            "recovery": "ratio",
            "source_id_sequence_encoding": (
                "concatenated uint64 big-endian, no delimiter"
            ),
            "source_id_sequence_sha256": _source_id_sequence_sha256(source_ids),
        },
        "hepmc_weight_names": HEPMC_WEIGHT_NAMES,
        "hepmc_precision_contract": {
            "relative_ratio_tolerance": 5.0e-8,
            "absolute_ratio_tolerance": 1.0e-7,
            "maximum_source_event_id": 1_000_000,
        },
        "phase_space_filter": {
            key: lhe_contract_payload[key]
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
            )
        },
        "contract_conditions": {
            "post_shower_generator_filter": False,
            "mapping": (
                "HepMC source-ID ratio selects the identically tagged LHE event"
            ),
            "hepmc_source_ids_strictly_increasing": True,
        },
        "files": {
            "matched_lhe": {"path": str(lhe), "sha256": _sha256(lhe)},
            "hepmc": {"path": "/tmp/hepmc=events", "sha256": hepmc_sha},
            "lhe_contract_metadata": {
                "path": str(lhe_contract),
                "sha256": _sha256(lhe_contract),
            },
        },
    }
    if standalone:
        assert generation_config is not None
        assert generation_config_payload is not None
        assert shower_log is not None
        alignment_payload["generator_backend"] = (
            "madgraph5-pythia8-vpolar-standalone"
        )
        alignment_payload["matrix_element_seed"] = 17
        alignment_payload["shower_seed"] = 17
        alignment_payload["generation_config"] = generation_config_payload
        alignment_payload["contract_conditions"][
            "generation_config_validated"
        ] = True
        alignment_payload["files"].update(
            {
                "generation_config": {
                    "path": generation_config.name,
                    "path_scope": "generation_run_directory",
                    "sha256": _sha256(generation_config),
                },
                "shower_log": {
                    "path": shower_log.name,
                    "path_scope": "generation_run_directory",
                    "sha256": _sha256(shower_log),
                },
            }
        )
    else:
        alignment_payload["athgeneration_release"] = "23.6.41"
        alignment_payload["files"]["job_option"] = {
            "path": "/tmp/job=option.py",
            "sha256": "d" * 64,
        }
    alignment.write_text(
        json.dumps(alignment_payload, indent=2) + "\n", encoding="utf-8"
    )
    dressed_origin = (
        "direct_hard_gg,non_hadronic,exact_signed_e_mu_copy_chain"
        if standalone
        else "W_or_Z_or_gammaStar_mass_gt_5,non_hadronic,direct_e_mu_only"
    )
    dressed_origin_policy = (
        "vpolar_direct_hard_gg_v1"
        if standalone
        else "resonant_boson_origin_v1"
    )
    direct_hard_required = str(standalone).lower()
    simulation.write_text(
        "schema_version=3\n"
        f"input_file=/tmp/hepmc=events\n"
        f"input_sha256={hepmc_sha}\n"
        f"output_file={delphes}\n"
        f"output_sha256={_sha256(delphes)}\n"
        f"process={process}\n"
        "generation_seed=17\n"
        f"generation_metadata_file={generation}\n"
        f"generation_metadata_sha256={_sha256(generation)}\n"
        f"alignment_metadata_file={alignment}\n"
        f"alignment_metadata_sha256={_sha256(alignment)}\n"
        "hepmc_format=2\n"
        "random_seed=91\n"
        "weight_scale=1.0\n"
        "weight_scale_policy=identity_for_direct_2e2mu_generation\n"
        "weight_branches_preserved=Event.Weight,Weight.Weight\n"
        "cross_section_semantics=conditional_on_lhe_phase_space_filter\n"
        f"input_events={event_count}\n"
        f"output_events={event_count}\n"
        "event_retention_validated=true\n"
        "event_order_preserved=true\n"
        "event_number_branch=Event.Number\n"
        f"dressed_lepton_origin={dressed_origin}\n"
        f"dressed_lepton_origin_policy={dressed_origin_policy}\n"
        "dressed_lepton_direct_hard_process_candidates="
        f"{direct_hard_required}\n"
        f"dressed_lepton_exact_2e2mu_validated={direct_hard_required}\n"
        "delphes_version=3.5.1\n"
        "delphes_commit=0123456789abcdef\n"
        "card_sha256=" + "a" * 64 + "\n"
        "resolved_card_sha256=" + "b" * 64 + "\n"
        "max_events=0\n",
        encoding="utf-8",
    )
    return {
        "generation_metadata_path": generation,
        "lhe_contract_metadata_path": lhe_contract,
        "alignment_metadata_path": alignment,
        "simulation_metadata_path": simulation,
    }


def test_event_uid_is_stable_and_uses_complete_logical_key():
    nominal = event_uid(42, SAMPLE_CODES["gg4l"], 7, 9)
    assert nominal == event_uid(42, SAMPLE_CODES["gg4l"], 7, 9)
    assert nominal != event_uid(43, SAMPLE_CODES["gg4l"], 7, 9)
    assert nominal != event_uid(42, SAMPLE_CODES["qqZZ"], 7, 9)
    assert nominal != event_uid(42, SAMPLE_CODES["gg4l"], 8, 9)
    assert nominal != event_uid(42, SAMPLE_CODES["gg4l"], 7, 10)


def test_vpolar_sample_codes_are_permanent_and_distinct():
    assert SAMPLE_CODES == {
        "gg4l": 0,
        "qqZZ": 1,
        "vpolar_LL": 10,
        "vpolar_TT": 11,
        "vpolar_TL": 12,
        "vpolar_LT": 13,
    }


def test_writes_one_row_per_lhe_and_retains_negative_unreconstructed_event(
    tmp_path: Path,
):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(
        delphes,
        (1, 2),
        missing_reco_last=True,
        cross_sections=(-0.25, 0.30),
        cross_section_errors=(-0.01, 0.015),
    )
    metadata = _write_provenance(tmp_path, lhe, delphes, process="gg4l")

    summary = build_analysis_tree(
        lhe,
        delphes,
        output,
        sample="gg4l",
        job_id=17,
        campaign_id=20260902,
        step_size="1 kB",
        **metadata,
    )

    assert summary["event_count"] == 2
    assert summary["negative_weight_count"] == 1
    assert summary["sumw"] == pytest.approx(-1.0)
    assert summary["sumw2"] == pytest.approx(8.5)
    assert summary["sumabsw"] == pytest.approx(4.0)

    with uproot.open(output) as root_file:
        events = root_file["Events"].arrays(library="np")
        runs = root_file["Runs"].arrays(library="np")
        lhe_weights = root_file["LHEWeights"].arrays(library="np")
        embedded = json.loads(str(root_file["analysis_metadata"]))

    np.testing.assert_array_equal(events["lhe_event_index"], [0, 1])
    np.testing.assert_array_equal(events["source_event_id"], [2, 7])
    np.testing.assert_array_equal(events["delphes_event_number"], [1, 2])
    np.testing.assert_allclose(events["weight_lhe"], [-2.5, 1.5])
    np.testing.assert_allclose(events["weight_delphes"], [-2.5, 1.5])
    np.testing.assert_allclose(events["cross_section_pb_delphes"], [-0.25, 0.30])
    np.testing.assert_allclose(events["cross_section_error_pb_delphes"], [-0.01, 0.015])
    assert np.all(events["has_lhe"])
    assert np.all(events["has_hepmc"])
    assert np.all(events["has_delphes"])
    assert np.all(events["lhe_candidate"])
    assert np.all(events["dressed_candidate"])
    np.testing.assert_array_equal(events["reco_candidate"], [True, False])
    np.testing.assert_array_equal(events["reconstructed"], [True, False])
    np.testing.assert_array_equal(
        events["reconstructed"], events["reco_pass_selection"]
    )
    assert np.isfinite(events["reco_raw_electron_minus_E"][0])
    assert np.isnan(events["reco_raw_electron_minus_E"][1])
    assert np.isfinite(events["lhe_born_muon_plus_E"]).all()
    assert np.isfinite(events["dressed_born_muon_plus_E"]).all()
    assert events["event_uid_hi"][0] != events["event_uid_hi"][1]
    assert runs["negative_weight_count"][0] == 1
    assert runs["lhe_candidate_count"][0] == 2
    assert runs["dressed_projection_valid_count"][0] == 2
    assert runs["reco_candidate_count"][0] == 1
    assert runs["reconstructed_count"][0] == 1
    assert runs["generation_seed"][0] == 17
    assert runs["delphes_seed"][0] == 91
    assert runs["athgeneration_release_patch"][0] == 41
    assert runs["schema_version"][0] == 2
    assert runs["source_event_id_min"][0] == 2
    assert runs["source_event_id_max"][0] == 7
    assert runs["cross_section_first_pb_delphes"][0] == pytest.approx(-0.25)
    assert runs["cross_section_error_first_pb_delphes"][0] == pytest.approx(-0.01)
    assert runs["phase_space_signed_efficiency"][0] == pytest.approx(0.8)
    assert runs["phase_space_absolute_efficiency"][0] == pytest.approx(0.8)
    assert runs["phase_space_count_efficiency"][0] == pytest.approx(2.0 / 3.0)
    assert runs["normalization_generated_lhe_events"][0] == 3
    assert runs["normalization_accepted_lhe_events"][0] == 2
    assert runs["normalization_sumw_generated_pb"][0] == pytest.approx(0.9)
    assert runs["normalization_sumw2_generated_pb2"][0] == pytest.approx(0.2916)
    assert runs["normalization_sumw_accepted_pb"][0] == pytest.approx(0.72)
    assert runs["normalization_sumw2_accepted_pb2"][0] == pytest.approx(0.2592)
    assert runs["inclusive_lhe_cross_section_pb"][0] == pytest.approx(0.3)
    assert runs["inclusive_lhe_cross_section_mc_error_pb"][0] == pytest.approx(0.06)
    assert runs["effective_filtered_cross_section_pb"][0] == pytest.approx(0.24)
    assert runs["effective_filtered_cross_section_mc_error_pb"][0] == pytest.approx(
        0.12
    )
    np.testing.assert_array_equal(lhe_weights["lhe_event_index"], [0, 1])
    np.testing.assert_array_equal(lhe_weights["source_event_id"], [2, 7])
    np.testing.assert_allclose(lhe_weights["values"], [[-2.25, -2.75], [1.35, 1.65]])
    assert embedded["lhe_alternative_weights"]["ids"] == ["1001", "2001"]
    assert embedded["lhe_alternative_weights"]["technical_weights_excluded"] == [
        MARKER_ID_WEIGHT_NAME,
        MARKER_UNIT_WEIGHT_NAME,
    ]
    assert embedded["source_event_id"]["sequence_sha256"] == (
        _source_id_sequence_sha256((2, 7))
    )
    assert embedded["provenance"]["generation"]["job_option"] == ("/tmp/job=option.py")
    assert embedded["provenance"]["files"]["lhe"]["sha256"] == _sha256(lhe)
    assert embedded["provenance"]["files"]["delphes"]["sha256"] == _sha256(delphes)
    assert set(embedded["analysis_code"]["files"]) == set(ANALYSIS_CODE_FILES)
    assert embedded["analysis_code"]["hash_algorithm"] == "sha256"
    repository = Path(__file__).resolve().parents[2]
    for relative_path in ANALYSIS_CODE_FILES:
        assert embedded["analysis_code"]["files"][relative_path]["sha256"] == (
            _sha256(repository / relative_path)
        )


def test_standalone_provenance_uses_generic_backend_and_zero_release_triplet(
    tmp_path: Path,
):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(
        tmp_path,
        lhe,
        delphes,
        process="vpolar_LL",
        standalone=True,
    )

    build_analysis_tree(
        lhe,
        delphes,
        output,
        sample="vpolar_LL",
        job_id=18,
        campaign_id=20260902,
        **metadata,
    )

    with uproot.open(output) as root_file:
        runs = root_file["Runs"].arrays(library="np")
        embedded = json.loads(str(root_file["analysis_metadata"]))
    assert runs["sample_code"][0] == 10
    assert runs["run_number"][0] == 100003
    assert runs["athgeneration_release_major"][0] == 0
    assert runs["athgeneration_release_minor"][0] == 0
    assert runs["athgeneration_release_patch"][0] == 0
    generation = embedded["provenance"]["generation"]
    assert generation["generator_backend"] == (
        "madgraph5-pythia8-vpolar-standalone"
    )
    assert "athgeneration_release" not in generation
    assert generation["loop_reduction_backend"] == "CutTools"
    assert generation["loop_optimized_output"] is True
    assert generation["madloop_reduction_lib"] == 1
    assert generation["ninja_enabled"] is False
    assert generation["collier_enabled"] is False
    alignment = embedded["provenance"]["alignment"]
    assert alignment["schema_version"] == 3
    assert alignment["generation_config_sha256"] == _sha256(
        tmp_path / "generation-config.json"
    )
    assert alignment["shower_log_sha256"] == _sha256(tmp_path / "pythia8.log")
    assert "job_option_sha256" not in alignment
    simulation = embedded["provenance"]["simulation"]
    assert simulation["dressed_lepton_origin_policy"] == (
        "vpolar_direct_hard_gg_v1"
    )
    assert simulation["dressed_lepton_direct_hard_process_candidates"] is True
    assert simulation["dressed_lepton_exact_2e2mu_validated"] is True


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        (
            "dressed_lepton_origin_policy=vpolar_direct_hard_gg_v1",
            "dressed_lepton_origin_policy=resonant_boson_origin_v1",
            "dressed-lepton origin policy mismatch",
        ),
        (
            "dressed_lepton_direct_hard_process_candidates=true",
            "dressed_lepton_direct_hard_process_candidates=false",
            "direct-hard dressed-lepton requirement mismatch",
        ),
        (
            "dressed_lepton_exact_2e2mu_validated=true",
            "dressed_lepton_exact_2e2mu_validated=false",
            "exact dressed 2e2mu validation mismatch",
        ),
    ),
)
def test_rejects_weakened_vpolar_dressed_origin_contract(
    tmp_path: Path, old: str, new: str, message: str
):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(
        tmp_path,
        lhe,
        delphes,
        process="vpolar_LL",
        standalone=True,
    )
    simulation_path = metadata["simulation_metadata_path"]
    simulation_text = simulation_path.read_text(encoding="utf-8")
    assert old in simulation_text
    simulation_path.write_text(
        simulation_text.replace(old, new), encoding="utf-8"
    )

    with pytest.raises(ProvenanceError, match=message):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="vpolar_LL",
            job_id=18,
            **metadata,
        )


def test_legacy_simulation_schema_two_remains_athgeneration_only(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="gg4l")
    simulation_path = metadata["simulation_metadata_path"]
    legacy_lines = [
        "schema_version=2" if line == "schema_version=3" else line
        for line in simulation_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(
            (
                "dressed_lepton_origin=",
                "dressed_lepton_origin_policy=",
                "dressed_lepton_direct_hard_process_candidates=",
                "dressed_lepton_exact_2e2mu_validated=",
            )
        )
    ]
    simulation_path.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")

    build_analysis_tree(
        lhe,
        delphes,
        output,
        sample="gg4l",
        job_id=18,
        **metadata,
    )
    assert output.is_file()


def test_rejects_simulation_schema_two_for_vpolar(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(
        tmp_path,
        lhe,
        delphes,
        process="vpolar_LL",
        standalone=True,
    )
    simulation_path = metadata["simulation_metadata_path"]
    simulation_path.write_text(
        simulation_path.read_text(encoding="utf-8").replace(
            "schema_version=3", "schema_version=2", 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="schema 2 is supported only"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="vpolar_LL",
            job_id=18,
            **metadata,
        )


def test_rejects_standalone_backend_in_legacy_alignment_schema(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(
        tmp_path,
        lhe,
        delphes,
        process="vpolar_LL",
        standalone=True,
    )
    alignment_path = metadata["alignment_metadata_path"]
    alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
    alignment["schema_version"] = 2
    alignment.pop("generator_backend")
    alignment_path.write_text(json.dumps(alignment) + "\n", encoding="utf-8")
    simulation_path = metadata["simulation_metadata_path"]
    simulation_text = simulation_path.read_text(encoding="utf-8")
    simulation_path.write_text(
        simulation_text.replace(
            next(
                line
                for line in simulation_text.splitlines()
                if line.startswith("alignment_metadata_sha256=")
            ),
            f"alignment_metadata_sha256={_sha256(alignment_path)}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="legacy alignment metadata"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="vpolar_LL",
            job_id=18,
            **metadata,
        )


def test_failure_cleans_partial_and_releases_persistent_lock_for_retry(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1,))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="qqZZ", event_count=2)

    with pytest.raises(MatchError, match="event-count mismatch"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="qqZZ",
            job_id=1,
            campaign_id=5,
            **metadata,
        )
    assert not output.exists()
    lock_path = tmp_path / ".analysis.root.lock"
    assert lock_path.is_file()
    lock_inode = lock_path.stat().st_ino
    assert not list(tmp_path.glob(".analysis.root.partial.*"))

    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="qqZZ")
    build_analysis_tree(
        lhe,
        delphes,
        output,
        sample="qqZZ",
        job_id=1,
        campaign_id=5,
        **metadata,
    )
    assert output.is_file()
    assert lock_path.stat().st_ino == lock_inode


@pytest.mark.parametrize(
    "metadata_key",
    (
        "generation_metadata_path",
        "lhe_contract_metadata_path",
        "alignment_metadata_path",
        "simulation_metadata_path",
    ),
)
def test_output_cannot_alias_any_metadata_input(
    tmp_path: Path, metadata_key: str
):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="qqZZ")
    protected_path = metadata[metadata_key]
    original = protected_path.read_bytes()

    with pytest.raises(ValueError, match="event or metadata input"):
        build_analysis_tree(
            lhe,
            delphes,
            protected_path,
            sample="qqZZ",
            job_id=20,
            overwrite=True,
            **metadata,
        )
    assert protected_path.read_bytes() == original


def test_concurrent_reducers_cannot_claim_same_absent_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="gg4l")

    reducer_globals = build_analysis_tree.__globals__
    original_temporary_output = reducer_globals["_temporary_output"]
    claim_acquired = threading.Event()
    allow_first_reducer = threading.Event()
    first_errors: list[BaseException] = []

    def blocking_temporary_output(path: Path, overwrite: bool) -> Path:
        claim_acquired.set()
        if not allow_first_reducer.wait(timeout=10):
            raise TimeoutError("test did not release the first reducer")
        return original_temporary_output(path, overwrite)

    monkeypatch.setitem(
        reducer_globals, "_temporary_output", blocking_temporary_output
    )

    def run_first_reducer() -> None:
        try:
            build_analysis_tree(
                lhe,
                delphes,
                output,
                sample="gg4l",
                job_id=21,
                **metadata,
            )
        except BaseException as exc:  # pragma: no cover - asserted in main thread
            first_errors.append(exc)

    first = threading.Thread(target=run_first_reducer)
    first.start()
    assert claim_acquired.wait(timeout=10)
    try:
        with pytest.raises(FileExistsError, match="another reducer"):
            build_analysis_tree(
                lhe,
                delphes,
                output,
                sample="gg4l",
                job_id=21,
                overwrite=True,
                **metadata,
            )
    finally:
        allow_first_reducer.set()
        first.join(timeout=20)

    assert not first.is_alive()
    assert first_errors == []
    assert output.is_file()
    assert (tmp_path / ".analysis.root.lock").is_file()
    assert not list(tmp_path.glob(".analysis.root.partial.*"))


def test_overwrite_replaces_existing_output_atomically(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="qqZZ")

    build_analysis_tree(
        lhe, delphes, output, sample="qqZZ", job_id=22, **metadata
    )
    lock_path = tmp_path / ".analysis.root.lock"
    lock_inode = lock_path.stat().st_ino
    first_digest = _sha256(output)
    with pytest.raises(FileExistsError, match="pass --overwrite"):
        build_analysis_tree(
            lhe, delphes, output, sample="qqZZ", job_id=23, **metadata
        )
    build_analysis_tree(
        lhe,
        delphes,
        output,
        sample="qqZZ",
        job_id=23,
        overwrite=True,
        **metadata,
    )
    assert output.is_file()
    assert _sha256(output) != first_digest
    assert lock_path.stat().st_ino == lock_inode
    assert not list(tmp_path.glob(".analysis.root.partial.*"))


def test_event_number_gap_fails_before_ordinal_join(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 3))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="gg4l")

    with pytest.raises(MatchError, match="not a unique contiguous"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="gg4l",
            job_id=2,
            campaign_id=5,
            **metadata,
        )
    assert not output.exists()


def test_rejects_cross_job_lhe_hash_before_pairing(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="gg4l")
    _write_lhe(lhe, weights=(-3.0, 2.0))

    with pytest.raises(ProvenanceError, match="matched LHE SHA-256 mismatch"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="gg4l",
            job_id=3,
            campaign_id=5,
            **metadata,
        )
    assert not output.exists()


def test_rejects_hepmc_hash_disagreement_between_alignment_and_simulation(
    tmp_path: Path,
):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="qqZZ")
    simulation = metadata["simulation_metadata_path"]
    text = simulation.read_text(encoding="utf-8")
    simulation.write_text(
        text.replace(
            "input_sha256=" + hashlib.sha256(b"synthetic HepMC job").hexdigest(),
            "input_sha256=" + "e" * 64,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="HepMC SHA-256 mismatch"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="qqZZ",
            job_id=4,
            campaign_id=5,
            **metadata,
        )
    assert not output.exists()


def test_named_marker_rejects_equal_count_but_wrong_event_pairing(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe, source_ids=(2, 7))
    _write_delphes(delphes, (1, 2), source_ids=(2, 8))
    # The alignment digest describes the HepMC/Delphes stream.  The matched-LHE
    # file hash is nevertheless internally consistent, reproducing the class
    # of cross-job pairing that count and provenance checks alone cannot find.
    metadata = _write_provenance(
        tmp_path,
        lhe,
        delphes,
        process="gg4l",
        source_ids=(2, 8),
    )

    with pytest.raises(MatchError, match="source-event ID mismatch"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="gg4l",
            job_id=9,
            campaign_id=5,
            **metadata,
        )
    assert not output.exists()


def test_uid_follows_source_id_when_earlier_sources_are_removed(tmp_path: Path):
    uid_maps: list[dict[int, tuple[int, int]]] = []
    for label, source_ids in (("full", (2, 7)), ("filtered", (7, 11))):
        directory = tmp_path / label
        directory.mkdir()
        lhe = directory / "events.lhe"
        delphes = directory / "delphes.root"
        output = directory / "analysis.root"
        _write_lhe(lhe, source_ids=source_ids)
        _write_delphes(delphes, (1, 2), source_ids=source_ids)
        metadata = _write_provenance(
            directory,
            lhe,
            delphes,
            process="qqZZ",
            source_ids=source_ids,
        )
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="qqZZ",
            job_id=11,
            campaign_id=6,
            **metadata,
        )
        with uproot.open(output) as root_file:
            events = root_file["Events"].arrays(
                ["source_event_id", "event_uid_hi", "event_uid_lo"], library="np"
            )
        uid_maps.append(
            {
                int(source_id): (int(uid_hi), int(uid_lo))
                for source_id, uid_hi, uid_lo in zip(
                    events["source_event_id"],
                    events["event_uid_hi"],
                    events["event_uid_lo"],
                )
            }
        )

    assert uid_maps[0][7] == uid_maps[1][7]


def test_missing_lhe_marker_fails_without_publishing(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    lhe.write_text(
        lhe.read_text(encoding="utf-8").replace(
            f" <wgt id='{MARKER_UNIT_WEIGHT_NAME}'>1</wgt>\n", "", 1
        ),
        encoding="utf-8",
    )
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="gg4l")

    with pytest.raises(MatchError, match="missing source-ID marker"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="gg4l",
            job_id=12,
            **metadata,
        )
    assert not output.exists()


def test_zero_nominal_and_hepmc2_rounded_marker_decode_safely(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    source_ids = (100_001,)
    _write_lhe(lhe, weights=(0.0,), source_ids=source_ids)
    _write_delphes(
        delphes,
        (1,),
        source_ids=source_ids,
        # Representative %.8e-style HepMC2 rounding accepted by the aligner.
        marker_pairs=((100_001.000350009, 1.0),),
    )
    metadata = _write_provenance(
        tmp_path,
        lhe,
        delphes,
        process="gg4l",
        event_count=1,
        source_ids=source_ids,
        zero_sum_normalization=True,
    )

    summary = build_analysis_tree(
        lhe,
        delphes,
        output,
        sample="gg4l",
        job_id=13,
        **metadata,
    )

    assert summary["zero_weight_count"] == 1
    with uproot.open(output) as root_file:
        events = root_file["Events"].arrays(
            ["source_event_id", "weight_lhe"], library="np"
        )
        runs = root_file["Runs"].arrays(library="np")
    assert events["source_event_id"][0] == 100_001
    assert events["weight_lhe"][0] == 0.0
    assert math.isnan(runs["phase_space_signed_efficiency"][0])
    assert runs["normalization_sumw_generated_pb"][0] == 0.0
    assert runs["normalization_sumw_accepted_pb"][0] == pytest.approx(0.2)
    assert runs["effective_filtered_cross_section_pb"][0] == pytest.approx(0.1)


def test_single_generated_event_writes_nan_mc_errors(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    source_ids = (2,)
    _write_lhe(lhe, weights=(0.36,), source_ids=source_ids)
    _write_delphes(delphes, (1,), source_ids=source_ids)
    metadata = _write_provenance(
        tmp_path,
        lhe,
        delphes,
        process="qqZZ",
        event_count=1,
        source_ids=source_ids,
        single_generated_normalization=True,
    )

    build_analysis_tree(
        lhe,
        delphes,
        output,
        sample="qqZZ",
        job_id=14,
        **metadata,
    )
    with uproot.open(output) as root_file:
        runs = root_file["Runs"].arrays(library="np")
    assert runs["inclusive_lhe_cross_section_pb"][0] == pytest.approx(0.36)
    assert math.isnan(runs["inclusive_lhe_cross_section_mc_error_pb"][0])
    assert runs["effective_filtered_cross_section_pb"][0] == pytest.approx(0.36)
    assert math.isnan(runs["effective_filtered_cross_section_mc_error_pb"][0])


def test_physics_weight_schema_drift_is_rejected(tmp_path: Path):
    lhe = tmp_path / "events.lhe"
    delphes = tmp_path / "delphes.root"
    output = tmp_path / "analysis.root"
    _write_lhe(lhe)
    text = lhe.read_text(encoding="utf-8")
    second_event = text.find("<event>", text.find("<event>") + 1)
    weight_start = text.find(" <wgt id='2001'>", second_event)
    weight_end = text.find("\n", weight_start) + 1
    assert second_event >= 0 and weight_start >= 0 and weight_end > weight_start
    lhe.write_text(text[:weight_start] + text[weight_end:], encoding="utf-8")
    _write_delphes(delphes, (1, 2))
    metadata = _write_provenance(tmp_path, lhe, delphes, process="qqZZ")

    with pytest.raises(MatchError, match="alternative LHE weight-ID schema changes"):
        build_analysis_tree(
            lhe,
            delphes,
            output,
            sample="qqZZ",
            job_id=14,
            **metadata,
        )
    assert not output.exists()
