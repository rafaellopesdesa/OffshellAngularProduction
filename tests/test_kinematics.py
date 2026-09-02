import numpy as np
import pytest
import vector

from offshell_production import (
    LEPTON_KEYS,
    angular_observables,
    born_project_four_leptons,
    build_four_lepton_candidate,
    build_level_record,
    empty_level_record,
)


def two_body_system(parent_mass=350.0, mass1=90.0, mass2=60.0):
    momentum = np.sqrt(
        (parent_mass**2 - (mass1 + mass2) ** 2)
        * (parent_mass**2 - (mass1 - mass2) ** 2)
    ) / (2.0 * parent_mass)
    energy1 = np.sqrt(mass1**2 + momentum**2)
    energy2 = np.sqrt(mass2**2 + momentum**2)
    direction = np.array([0.8, 0.3, np.sqrt(1.0 - 0.8**2 - 0.3**2)])
    z1 = vector.obj(
        px=momentum * direction[0],
        py=momentum * direction[1],
        pz=momentum * direction[2],
        E=energy1,
    )
    z2 = vector.obj(px=-z1.px, py=-z1.py, pz=-z1.pz, E=energy2)
    return z1, z2


def decay_to_massless_leptons(parent, cos_theta, phi):
    momentum = parent.mass / 2.0
    sin_theta = np.sqrt(1.0 - cos_theta**2)
    positive = vector.obj(
        px=momentum * sin_theta * np.cos(phi),
        py=momentum * sin_theta * np.sin(phi),
        pz=momentum * cos_theta,
        E=momentum,
    )
    negative = vector.obj(
        px=-positive.px,
        py=-positive.py,
        pz=-positive.pz,
        E=momentum,
    )
    beta = parent.to_beta3()
    return negative.boost_beta3(beta), positive.boost_beta3(beta)


def recoiling_event():
    z1, z2 = two_body_system()
    muon_minus, muon_plus = decay_to_massless_leptons(z1, 0.25, 0.8)
    electron_minus, electron_plus = decay_to_massless_leptons(z2, -0.4, -1.2)
    rest_event = {
        "muon_minus": muon_minus,
        "muon_plus": muon_plus,
        "electron_minus": electron_minus,
        "electron_plus": electron_plus,
    }
    recoil = vector.obj(x=0.12, y=-0.08, z=0.35)
    return {key: value.boost_beta3(recoil) for key, value in rest_event.items()}


def test_candidate_is_fixed_flavor_and_cut_independent():
    candidate = build_four_lepton_candidate(recoiling_event())

    np.testing.assert_allclose(
        candidate.z1.mass,
        (candidate.leptons["muon_minus"] + candidate.leptons["muon_plus"]).mass,
    )
    np.testing.assert_allclose(
        candidate.z2.mass,
        (candidate.leptons["electron_minus"] + candidate.leptons["electron_plus"]).mass,
    )
    assert set(candidate.leptons) == set(LEPTON_KEYS)

    incomplete = dict(candidate.leptons)
    incomplete.pop("electron_plus")
    with pytest.raises(ValueError, match="electron_plus"):
        build_four_lepton_candidate(incomplete)


def test_born_projection_preserves_invariants_and_removes_recoil():
    raw = recoiling_event()
    born, diagnostics = born_project_four_leptons(raw)

    np.testing.assert_allclose(diagnostics.born_m4l, diagnostics.raw_m4l)
    np.testing.assert_allclose(diagnostics.born_y4l, diagnostics.raw_y4l)
    assert diagnostics.raw_pt4l > 1.0
    assert diagnostics.born_pt4l < 1.0e-10

    raw_candidate = build_four_lepton_candidate(raw)
    born_candidate = build_four_lepton_candidate(born)
    np.testing.assert_allclose(
        [born_candidate.z1.mass, born_candidate.z2.mass],
        [raw_candidate.z1.mass, raw_candidate.z2.mass],
    )


def test_angle_ranges_and_charge_conventions():
    born, _ = born_project_four_leptons(recoiling_event())
    observables = angular_observables(born)

    assert 0.0 <= observables["theta1"] <= np.pi
    assert 0.0 <= observables["theta2"] <= np.pi
    assert -np.pi <= observables["phi1"] <= np.pi
    assert -np.pi <= observables["phi2"] <= np.pi
    assert -1.0 <= observables["cos_theta_star"] <= 1.0
    assert not observables["frame_degenerate"]
    assert not observables["standard_angles_degenerate"]
    np.testing.assert_allclose(
        observables["theta1_standard"], np.pi - observables["theta1"]
    )
    np.testing.assert_allclose(
        observables["theta2_standard"], np.pi - observables["theta2"]
    )


def test_level_records_are_namespaced_and_empty_records_use_nan():
    record = build_level_record(recoiling_event(), "dressed")

    assert record["dressed_topology_valid"]
    assert record["dressed_projection_valid"]
    assert "dressed_raw_muon_plus_E" in record
    assert "dressed_born_electron_minus_px" in record
    assert "dressed_theta1" in record
    assert record["dressed_born_pt4l"] < 1.0e-10
    assert not any(key.startswith("lhe_") for key in record)

    empty = empty_level_record("reco")
    assert not empty["reco_topology_valid"]
    assert not empty["reco_projection_valid"]
    assert np.isnan(empty["reco_theta1"])
    assert np.isnan(empty["reco_raw_muon_plus_E"])
    assert -999.0 not in empty.values()
