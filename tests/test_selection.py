import numpy as np
import pytest
import vector

from offshell_production import (
    OffshellSelectionConfig,
    born_project_four_leptons,
    empty_reco_selection_result,
    evaluate_reco_selection,
)


def passing_event(parent_mass=250.0, z1_mass=80.0, z2_mass=80.0):
    momentum = np.sqrt(
        (parent_mass**2 - (z1_mass + z2_mass) ** 2)
        * (parent_mass**2 - (z1_mass - z2_mass) ** 2)
    ) / (2.0 * parent_mass)
    z1 = vector.obj(
        px=momentum,
        py=0.0,
        pz=0.0,
        E=np.sqrt(momentum**2 + z1_mass**2),
    )
    z2 = vector.obj(
        px=-momentum,
        py=0.0,
        pz=0.0,
        E=np.sqrt(momentum**2 + z2_mass**2),
    )

    def decay(parent, mass, sign):
        positive = vector.obj(px=0.0, py=sign * mass / 2.0, pz=0.0, E=mass / 2.0)
        negative = vector.obj(px=0.0, py=-sign * mass / 2.0, pz=0.0, E=mass / 2.0)
        beta = parent.to_beta3()
        return negative.boost_beta3(beta), positive.boost_beta3(beta)

    muon_minus, muon_plus = decay(z1, z1_mass, 1.0)
    electron_minus, electron_plus = decay(z2, z2_mass, -1.0)
    return {
        "electron_minus": electron_minus,
        "electron_plus": electron_plus,
        "muon_minus": muon_minus,
        "muon_plus": muon_plus,
    }


def test_default_offshell_selection_passes_and_is_flattenable():
    result = evaluate_reco_selection(passing_event())

    assert result.candidate
    assert result.passed
    assert all(result.flags.values())
    np.testing.assert_allclose(result.dilepton_masses["Z1"], 80.0)
    np.testing.assert_allclose(result.dilepton_masses["Z2"], 80.0)
    np.testing.assert_allclose(result.m4l, 250.0)

    record = result.to_record()
    assert record["reco_candidate"]
    assert record["reco_pass_selection"]
    assert record["reco_cut_Z1_mass_window"]
    assert "reco_delta_r_electron_minus_muon_plus" in record


def test_default_reco_selection_has_no_3000_gev_upper_cut():
    result = evaluate_reco_selection(passing_event(parent_mass=3001.0))

    assert result.flags["m4l_lower"]
    assert result.flags["m4l_upper"]
    assert result.passed


def test_all_thresholds_are_strict():
    raw = passing_event()
    nominal = evaluate_reco_selection(raw)

    assert not evaluate_reco_selection(
        raw,
        OffshellSelectionConfig(min_lepton_pt=min(nominal.lepton_pt.values())),
    ).flags["all_lepton_pt"]
    assert not evaluate_reco_selection(
        raw,
        OffshellSelectionConfig(
            ordered_pt_thresholds=(nominal.ordered_pt[0], 15.0, 10.0)
        ),
    ).flags["ordered_pt1"]
    assert not evaluate_reco_selection(
        raw,
        OffshellSelectionConfig(
            min_dilepton_mass=nominal.dilepton_masses["Z1"],
            max_dilepton_mass=106.0,
        ),
    ).flags["Z1_mass_window"]
    assert not evaluate_reco_selection(
        raw,
        OffshellSelectionConfig(min_delta_r=min(nominal.pair_delta_r.values())),
    ).flags["all_pair_delta_r"]
    assert not evaluate_reco_selection(
        raw,
        OffshellSelectionConfig(min_m4l=nominal.m4l, max_m4l=3000.0),
    ).flags["m4l_lower"]
    assert not evaluate_reco_selection(
        raw,
        OffshellSelectionConfig(min_m4l=180.0, max_m4l=nominal.m4l),
    ).flags["m4l_upper"]


def test_selection_uses_raw_not_born_projected_momenta():
    rest = passing_event()
    recoil = vector.obj(x=0.28, y=0.11, z=0.0)
    raw = {key: momentum.boost_beta3(recoil) for key, momentum in rest.items()}
    born, _ = born_project_four_leptons(raw)
    raw_min_pt = min(momentum.pt for momentum in raw.values())
    born_min_pt = min(momentum.pt for momentum in born.values())
    assert not np.isclose(raw_min_pt, born_min_pt)

    threshold = 0.5 * (raw_min_pt + born_min_pt)
    config = OffshellSelectionConfig(min_lepton_pt=threshold)
    result = evaluate_reco_selection(raw, config)

    assert result.flags["all_lepton_pt"] == (raw_min_pt > threshold)
    assert result.flags["all_lepton_pt"] != (born_min_pt > threshold)


def test_missing_candidate_is_explicitly_masked_with_nan():
    result = empty_reco_selection_result()
    record = result.to_record()

    assert not result.candidate
    assert not result.passed
    assert not any(result.flags.values())
    assert np.isnan(record["reco_m4l_for_selection"])
    assert np.isnan(record["reco_ordered_pt1"])
    assert -999.0 not in record.values()


def test_selection_configuration_rejects_inconsistent_bounds():
    with pytest.raises(ValueError, match="dilepton mass"):
        OffshellSelectionConfig(min_dilepton_mass=106.0, max_dilepton_mass=50.0)
    with pytest.raises(ValueError, match="non-increasing"):
        OffshellSelectionConfig(ordered_pt_thresholds=(10.0, 20.0, 15.0))
    with pytest.raises(ValueError, match="max_m4l"):
        OffshellSelectionConfig(min_m4l=180.0, max_m4l=180.0)
