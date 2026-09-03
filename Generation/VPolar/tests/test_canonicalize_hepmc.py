from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "canonicalize_hepmc.py"
SPEC = importlib.util.spec_from_file_location("canonicalize_hepmc", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
canonicalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canonicalizer)


def _event(number: int, id_value: float, unit_value: float) -> str:
    return (
        f"E {number} 0 1.0 0.0 0.0 0 0 0 0 0 0 3 1.0 {id_value} {unit_value}\n"
        'N 3 "Weight_MERGING=0.000" "id=AUX_AUX_OAP_EVENT_ID_MERGING=0.000" '
        '"id=AUX_AUX_OAP_EVENT_UNIT_MERGING=0.000"\n'
        "U GEV MM\n"
    )


def _listing(*events: str) -> str:
    return (
        "HepMC::Version 2.06.09\n"
        "HepMC::IO_GenEvent-START_EVENT_LISTING\n"
        + "".join(events)
        + "HepMC::IO_GenEvent-END_EVENT_LISTING\n"
    )


def test_canonicalizes_only_markers_and_event_numbers(tmp_path):
    source = tmp_path / "raw.hepmc"
    output = tmp_path / "events.hepmc"
    source.write_text(_listing(_event(0, 1.0, 1.0), _event(1, 2.0, 1.0)))

    assert canonicalizer.canonicalize_hepmc(
        source, output, first_event=41, expected_events=2
    ) == 2
    text = output.read_text()
    assert "E 41 " in text and "E 42 " in text
    assert text.count('"AUX_OAP_EVENT_ID"') == 2
    assert text.count('"AUX_OAP_EVENT_UNIT"') == 2
    assert text.count('"Weight_MERGING=0.000"') == 2
    assert "AUX_AUX" not in text


@pytest.mark.parametrize(
    "names",
    [
        '"Weight" "unrelated" "AUX_OAP_EVENT_UNIT"',
        '"AUX_OAP_EVENT_ID" "again_AUX_OAP_EVENT_ID" "AUX_OAP_EVENT_UNIT"',
        '"AUX_OAP_EVENT_ID" "AUX_OAP_EVENT_UNIT" "AUX_OAP_EVENT_UNIT_extra"',
    ],
)
def test_rejects_missing_or_ambiguous_markers(tmp_path, names):
    source = tmp_path / "raw.hepmc"
    output = tmp_path / "events.hepmc"
    source.write_text(
        _listing(
            "E 0 0 1.0 0.0 0.0 0 0 0 0 0 0 3 1.0 1.0 1.0\n"
            f"N 3 {names}\n"
        )
    )
    with pytest.raises(canonicalizer.CanonicalizationError, match="marker"):
        canonicalizer.canonicalize_hepmc(
            source, output, first_event=1, expected_events=1
        )
    assert not output.exists()


def test_rejects_count_mismatch_without_partial_output(tmp_path):
    source = tmp_path / "raw.hepmc"
    output = tmp_path / "events.hepmc"
    source.write_text(_listing(_event(0, 1.0, 1.0)))
    with pytest.raises(canonicalizer.CanonicalizationError, match="expected 2"):
        canonicalizer.canonicalize_hepmc(
            source, output, first_event=1, expected_events=2
        )
    assert not output.exists()
