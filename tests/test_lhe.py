from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from offshell_production import (
    LHEStatus,
    extract_event_particles,
    iter_lhe_records,
    load_lhe_dataframe,
)

LHE_TEXT = """<LesHouchesEvents version=\"3.0\">
<header></header>
<init>
 2212 2212 6500.0 6500.0 0 0 0 0 3 1
 1.0 0.0 1.0 1
</init>
<event>
 9 1 -2.5 350.0 0.007297 0.118
 21 -1 0 0 501 502 0.0 0.0 6500.0 6500.0 0.0 0.0 9.0
 21 -1 0 0 502 501 0.0 0.0 -6500.0 6500.0 0.0 0.0 9.0
 25 2 1 2 0 0 15.0 10.0 15.0 115.869834 112.0 0.0 9.0
 23 2 3 3 0 0 10.0 0.0 30.0 72.360680 65.0 0.0 9.0
 13 1 4 4 0 0 30.0 0.0 40.0 50.0 0.0 0.0 9.0
 -13 1 4 4 0 0 -20.0 0.0 -10.0 22.360680 0.0 0.0 9.0
 11 1 5 5 0 0 0.0 25.0 -10.0 26.925824 0.0 0.0 9.0
 -11 1 5 5 0 0 5.0 -15.0 -5.0 16.583124 0.0 0.0 9.0
</event>
</LesHouchesEvents>
"""


def fake_particle(pdg_id, *, status=1, px=10.0, py=1.0, pz=2.0):
    energy = np.sqrt(px**2 + py**2 + pz**2)
    return SimpleNamespace(
        id=pdg_id,
        status=status,
        px=px,
        py=py,
        pz=pz,
        e=energy,
    )


def fake_event(particles, weight=-0.75):
    return SimpleNamespace(
        particles=particles,
        eventinfo=SimpleNamespace(weight=weight),
        weights={"scale_up": 1.25},
    )


def test_extractor_requires_exact_status1_charge_flavor_multiplicity():
    particles = [
        fake_particle(11),
        fake_particle(-11, py=-2.0),
        fake_particle(13, px=-7.0),
        fake_particle(-13, px=-6.0, py=-3.0),
        fake_particle(23, status=2),
    ]
    extracted = extract_event_particles(fake_event(particles))

    assert extracted.nominal_weight == -0.75
    assert extracted.alternative_weights == {"scale_up": 1.25}
    assert set(extracted.leptons) == {
        "electron_minus",
        "electron_plus",
        "muon_minus",
        "muon_plus",
    }

    with pytest.raises(ValueError, match="electron_minus"):
        extract_event_particles(fake_event(particles + [fake_particle(11)]))


def test_lhe_reader_preserves_signed_weight_and_namespace(tmp_path: Path):
    path = tmp_path / "event.lhe"
    path.write_text(LHE_TEXT)
    events = load_lhe_dataframe(path, include_momenta=True)

    assert len(events) == 1
    event = events.iloc[0]
    assert event["lhe_event_index"] == 0
    assert event["weight_lhe"] == -2.5
    assert event["lhe_topology_valid"]
    assert event["lhe_projection_valid"]
    assert event["lhe_born_pt4l"] < 1.0e-10
    np.testing.assert_allclose(event["lhe_raw_m4l"], event["lhe_born_m4l"])
    assert "lhe_raw_muon_plus_E" in events
    assert "lhe_born_electron_minus_px" in events


def test_non_strict_reader_retains_invalid_event_with_masks(monkeypatch):
    valid_particles = [
        fake_particle(11),
        fake_particle(-11, py=-2.0),
        fake_particle(13, px=-7.0),
        fake_particle(-13, px=-6.0, py=-3.0),
    ]
    invalid = fake_event(valid_particles[:-1], weight=-3.0)
    fake_file = SimpleNamespace(events=iter([invalid]))

    monkeypatch.setattr(
        "offshell_production.lhe.pylhe.LHEFile.fromfile",
        lambda *args, **kwargs: fake_file,
    )
    records = list(iter_lhe_records("unused.lhe", strict=False))

    assert len(records) == 1
    assert records[0]["lhe_event_index"] == 0
    assert records[0]["weight_lhe"] == -3.0
    assert not records[0]["lhe_topology_valid"]
    assert not records[0]["lhe_projection_valid"]
    assert np.isnan(records[0]["lhe_theta1"])
    assert records[0]["lhe_status"] == int(LHEStatus.INVALID_TOPOLOGY)


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), -float("inf")])
def test_reader_rejects_nonfinite_nominal_weights(monkeypatch, weight):
    particles = [
        fake_particle(11),
        fake_particle(-11, py=-2.0),
        fake_particle(13, px=-7.0),
        fake_particle(-13, px=-6.0, py=-3.0),
    ]
    fake_file = SimpleNamespace(events=iter([fake_event(particles, weight=weight)]))
    monkeypatch.setattr(
        "offshell_production.lhe.pylhe.LHEFile.fromfile",
        lambda *args, **kwargs: fake_file,
    )

    with pytest.raises(ValueError, match="nominal weight must be finite"):
        list(iter_lhe_records("unused.lhe", strict=False))
