from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import tarfile

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "python"
    / "offshell_lhe_contract.py"
)
SPEC = importlib.util.spec_from_file_location("offshell_lhe_contract", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


def _event(source_mass: float, weight: float = 1.0) -> str:
    # Four massless leptons at rest in pairs, with total invariant mass 4E.
    energy = source_mass / 4.0
    particles = [
        f" 11 1 0 0 0 0 {energy} 0 0 {energy} 0 0 9",
        f" -11 1 0 0 0 0 {-energy} 0 0 {energy} 0 0 9",
        f" 13 1 0 0 0 0 0 {energy} 0 {energy} 0 0 9",
        f" -13 1 0 0 0 0 0 {-energy} 0 {energy} 0 0 9",
    ]
    return (
        "<event>\n"
        f" 4 1 {weight} 100 0.007 0.118\n"
        + "\n".join(particles)
        + "\n<rwgt>\n <wgt id='1001'> 1.2 </wgt>\n</rwgt>\n"
        "</event>\n"
    )


def _document(events: list[str]) -> str:
    return (
        '<LesHouchesEvents version="3.0">\n'
        "<header><initrwgt><weightgroup name='scale' combine='none'>\n"
        "<weight id='1001'>scale</weight></weightgroup></initrwgt></header>\n"
        "<init>\n 1 1 1 1 0 0 0 0 -4 1\n 1 0 1 1\n</init>\n"
        + "".join(events)
        + "</LesHouchesEvents>\n"
    )


def test_filters_before_shower_and_injects_stable_source_ids(tmp_path: Path):
    lhe = tmp_path / "LHE.TXT.events"
    archive = tmp_path / "LHE.TXT.tar.gz"
    metadata = tmp_path / "lhe-contract-metadata.json"
    lhe.write_text(
        _document([_event(120.0), _event(200.0, -2.0), _event(3500.0, 3.0)])
    )

    result = CONTRACT.prepare_lhe_for_shower(
        lhe,
        archive,
        process="qqZZ",
        requested_events=1,
        min_m4l=70.0,
        max_m4l=3000.0,
        metadata_path=metadata,
    )

    output = lhe.read_text()
    assert output.count("<event>") == 2
    assert "id='AUX_OAP_EVENT_ID'" in output
    assert "id='AUX_OAP_EVENT_UNIT'" in output
    assert "# AUX_OAP_EVENT_ID 1" in output
    assert "# AUX_OAP_EVENT_ID 2" in output
    assert "# AUX_OAP_EVENT_ID 3" not in output
    assert result["rejected_above_m4l"] == 1
    assert result["normalization_contract"] == "idwtup-minus4-sample-mean-v1"
    assert result["nominal_weight_units"] == "pb"
    assert result["lhe_weighting_strategy"] == -4
    assert result["count_filter_efficiency"] == pytest.approx(2.0 / 3.0)
    assert result["signed_filter_efficiency"] == pytest.approx(-0.5)
    assert result["absolute_filter_efficiency"] == pytest.approx(0.5)
    assert result["sumw2_generated"] == pytest.approx(14.0)
    assert result["sumw2_accepted"] == pytest.approx(5.0)
    assert result["inclusive_cross_section_pb"] == pytest.approx(2.0 / 3.0)
    assert result["inclusive_cross_section_mc_error_pb"] == pytest.approx(
        math.sqrt(19.0) / 3.0
    )
    assert result["filtered_cross_section_pb"] == pytest.approx(-1.0 / 3.0)
    assert result["filtered_cross_section_mc_error_pb"] == pytest.approx(
        math.sqrt(7.0) / 3.0
    )
    assert result["lhe_init"]["idwtup"] == -4
    assert result["lhe_init"]["inclusive_cross_section_pb"] == pytest.approx(1.0)
    assert result["cross_section_method"] == CONTRACT.CROSS_SECTION_METHOD
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile("LHE.TXT.events")
        assert member is not None
        assert member.read().decode() == output


def test_fails_if_filter_safety_margin_cannot_fill_transform(tmp_path: Path):
    lhe = tmp_path / "LHE.TXT.events"
    archive = tmp_path / "LHE.TXT.tar.gz"
    lhe.write_text(_document([_event(3500.0)]))
    with pytest.raises(CONTRACT.LHEContractError, match="fewer than"):
        CONTRACT.prepare_lhe_for_shower(
            lhe,
            archive,
            process="qqZZ",
            requested_events=1,
            min_m4l=70.0,
            max_m4l=3000.0,
            metadata_path=tmp_path / "metadata.json",
        )
    assert "AUX_OAP_EVENT_ID" not in lhe.read_text()


def test_rejects_truncated_lhe_without_repairing_source(tmp_path: Path):
    lhe = tmp_path / "LHE.TXT.events"
    archive = tmp_path / "LHE.TXT.tar.gz"
    truncated = _document([_event(200.0)]).replace("</LesHouchesEvents>\n", "")
    lhe.write_text(truncated)
    with pytest.raises(CONTRACT.LHEContractError, match="missing </LesHouchesEvents>"):
        CONTRACT.prepare_lhe_for_shower(
            lhe,
            archive,
            process="gg4l",
            requested_events=1,
            min_m4l=150.0,
            max_m4l=3000.0,
            metadata_path=tmp_path / "metadata.json",
        )
    assert lhe.read_text() == truncated
    assert not archive.exists()


def test_rejects_nonfinite_lepton_momentum(tmp_path: Path):
    lhe = tmp_path / "LHE.TXT.events"
    archive = tmp_path / "LHE.TXT.tar.gz"
    invalid = _document([_event(200.0)]).replace(" 50.0 0 0 50.0", " nan 0 0 50.0", 1)
    lhe.write_text(invalid)
    with pytest.raises(CONTRACT.LHEContractError, match="non-finite"):
        CONTRACT.prepare_lhe_for_shower(
            lhe,
            archive,
            process="gg4l",
            requested_events=1,
            min_m4l=150.0,
            max_m4l=3000.0,
            metadata_path=tmp_path / "metadata.json",
        )


@pytest.mark.parametrize("process", ("gg4l", "vpolar_LL"))
def test_rejects_non_minus_four_lhe_weighting_strategy(
    tmp_path: Path, process: str
):
    lhe = tmp_path / "LHE.TXT.events"
    archive = tmp_path / "LHE.TXT.tar.gz"
    invalid = _document([_event(200.0)]).replace(" 0 0 -4 1", " 0 0 4 1")
    lhe.write_text(invalid)
    with pytest.raises(CONTRACT.LHEContractError, match="IDWTUP=-4"):
        CONTRACT.prepare_lhe_for_shower(
            lhe,
            archive,
            process=process,
            requested_events=1,
            min_m4l=150.0,
            max_m4l=3000.0,
            metadata_path=tmp_path / "metadata.json",
        )


def test_single_generated_event_has_undefined_mc_error(tmp_path: Path):
    lhe = tmp_path / "LHE.TXT.events"
    archive = tmp_path / "LHE.TXT.tar.gz"
    lhe.write_text(_document([_event(200.0, -2.0)]))
    result = CONTRACT.prepare_lhe_for_shower(
        lhe,
        archive,
        process="gg4l",
        requested_events=1,
        min_m4l=150.0,
        max_m4l=3000.0,
        metadata_path=tmp_path / "metadata.json",
    )
    assert result["inclusive_cross_section_pb"] == pytest.approx(-2.0)
    assert result["filtered_cross_section_pb"] == pytest.approx(-2.0)
    assert result["inclusive_cross_section_mc_error_pb"] is None
    assert result["filtered_cross_section_mc_error_pb"] is None


@pytest.mark.parametrize(
    "process",
    ("vpolar_LL", "vpolar_TT", "vpolar_TL", "vpolar_LT"),
)
def test_accepts_vpolar_process_modes(tmp_path: Path, process: str):
    lhe = tmp_path / "LHE.TXT.events"
    archive = tmp_path / "LHE.TXT.tar.gz"
    lhe.write_text(_document([_event(200.0)]))

    result = CONTRACT.prepare_lhe_for_shower(
        lhe,
        archive,
        process=process,
        requested_events=1,
        min_m4l=150.0,
        max_m4l=3000.0,
        metadata_path=tmp_path / "metadata.json",
    )

    assert result["process"] == process
    assert archive.is_file()
