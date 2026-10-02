"""Numerical closure and ROOT-input checks for plotting merged events."""

from pathlib import Path

import hist
import numpy as np
import pytest
import uproot
import vector

from offshell_production.kinematics import (
    ANGULAR_NUMERIC_FIELDS, LEPTON_KEYS, MOMENTUM_COMPONENTS,
    PROJECTION_DIAGNOSTIC_FIELDS,
)
from offshell_production.plotting import (
    _raw_selection_observables, observable_specs, read_report, subset_ratio,
)
from offshell_production.selection import evaluate_reco_selection, empty_reco_selection_result


def columns(n=4):
    result = {
        "weight_nominal_pb": np.array([2., -1., 3., 1.])[:n],
        "lumi": np.full(n, 312000.),
        "sample_code": np.full(n, 1, dtype=np.uint8),
        "reconstructed": np.array([True, False, False, True])[:n],
    }
    for level in ("lhe", "dressed", "reco"):
        for flag in ("topology_valid", "projection_valid"):
            result[f"{level}_{flag}"] = np.ones(n, dtype=bool)
        for flag in ("frame_degenerate", "standard_angles_degenerate"):
            result[f"{level}_{flag}"] = np.zeros(n, dtype=bool)
        for name, spec in observable_specs().items():
            if level != "reco" and spec.category == "selection":
                continue
            result[f"{level}_{name}"] = np.full(n, (spec.edges[0] + spec.edges[-1]) / 2)
    return result


def write_file(tmp_path: Path, arrays):
    path = tmp_path / "merged.root"
    with uproot.recreate(path) as root:
        tree = root.mktree("Events", {key: value.dtype for key, value in arrays.items()})
        tree.extend(arrays)
    return path


def weighted_hist(weights):
    histogram = hist.Hist(hist.axis.Regular(1, 0, 1), storage=hist.storage.Weight())
    histogram.fill(np.full(len(weights), .5), weight=weights)
    return histogram


def test_registry_matches_entire_stored_numeric_schema():
    expected = set(ANGULAR_NUMERIC_FIELDS) | set(PROJECTION_DIAGNOSTIC_FIELDS)
    expected |= {
        f"{frame}_{lepton}_{component}"
        for frame in ("raw", "born") for lepton in LEPTON_KEYS
        for component in MOMENTUM_COMPONENTS
    }
    expected |= {
        name.removeprefix("reco_")
        for name, value in empty_reco_selection_result().to_record().items()
        if isinstance(value, float)
    }
    specs = observable_specs()
    assert set(specs) == expected
    assert len(specs) == 77
    for spec in specs.values():
        assert spec.label
        assert np.all(np.diff(spec.edges) > 0)
    assert "Born" in specs["pt_ZZ"].label
    assert specs["raw_pt4l"].validity == "topology_valid"


def test_signed_yield_and_subset_covariance(tmp_path):
    path = write_file(tmp_path, columns())
    report = read_report(path, variables=["m_Z1"], bin_edges={"m_Z1": [0, 200]})
    assert report.entries == 4
    assert report.selected_entries == 2
    assert report.sample_label == "qqZZ"
    assert report.lumi_pb == 312000
    assert report.sumw_pb == 5
    assert report.sumw2_pb2 == 15
    assert report.sumabsw_pb == 7
    assert report.selected_sumw_pb == 3
    assert report.selected_sumw2_pb2 == 5
    numerator = report.histograms["selected", "lhe", "m_Z1"]
    denominator = report.histograms["all", "lhe", "m_Z1"]
    np.testing.assert_array_equal(denominator.values(), [5])
    np.testing.assert_array_equal(denominator.variances(), [15])
    ratio = subset_ratio(numerator, denominator)
    np.testing.assert_allclose(ratio.values, [.6])
    # Selected weights 2,1; complement weights -1,3.
    np.testing.assert_allclose(ratio.variances, [(5*.4**2 + 10*.6**2)/25])
    assert ratio.valid[0]


def test_unweighted_efficiency_has_binomial_variance():
    ratio = subset_ratio(weighted_hist([1, 1]), weighted_hist([1]*5))
    np.testing.assert_allclose(ratio.values, [.4])
    np.testing.assert_allclose(ratio.variances, [.4*.6/5])


def test_signed_efficiency_is_not_clipped_and_empty_bins_are_masked():
    ratio = subset_ratio(weighted_hist([2]), weighted_hist([2, -1]))
    np.testing.assert_allclose(ratio.values, [2])
    np.testing.assert_allclose(ratio.variances, [8])
    negative = subset_ratio(weighted_hist([1]), weighted_hist([1, -3]))
    np.testing.assert_allclose(negative.values, [-.5])
    for weights in ([1, -1], []):
        result = subset_ratio(weighted_hist([]), weighted_hist(weights))
        assert not result.valid[0]
        assert np.isnan(result.values[0])
    with pytest.raises(ValueError, match="not a subset"):
        subset_ratio(weighted_hist([4]), weighted_hist([1]))


def test_empty_selection_is_zero_yield_not_an_error(tmp_path):
    arrays = columns()
    arrays["reconstructed"][:] = False
    report = read_report(write_file(tmp_path, arrays), variables=["m_Z1"])
    assert report.selected_entries == 0
    assert report.histograms["selected", "reco", "m_Z1"].sum().value == 0
    ratio = subset_ratio(report.histograms["selected", "lhe", "m_Z1"],
                         report.histograms["all", "lhe", "m_Z1"])
    assert np.count_nonzero(ratio.valid) == 1
    assert ratio.values[ratio.valid][0] == 0
    assert ratio.variances[ratio.valid][0] == 0


def test_validity_masks_are_per_observable_and_level(tmp_path):
    arrays = columns()
    arrays["lhe_projection_valid"][0] = False
    arrays["lhe_frame_degenerate"][1] = True
    arrays["lhe_standard_angles_degenerate"][2] = True
    arrays["lhe_theta1"][3] = np.nan
    arrays["dressed_topology_valid"][3] = False
    names = ["raw_electron_minus_px", "born_electron_minus_px", "theta1", "phi1", "Phi", "Phi1"]
    report = read_report(write_file(tmp_path, arrays), variables=names)
    counts = report.counters
    assert counts["all", "lhe", "raw_electron_minus_px"].filled == 4
    assert counts["all", "lhe", "born_electron_minus_px"].filled == 3
    assert counts["all", "lhe", "theta1"].filled == 2
    assert counts["all", "lhe", "phi1"].filled == 2
    # A finite Phi depends only on the two decay planes; undefined production
    # plane invalidates Phi1/Psi but must not remove Phi.
    assert counts["all", "lhe", "Phi"].filled == 3
    assert counts["all", "lhe", "Phi1"].filled == 2
    assert counts["all", "dressed", "raw_electron_minus_px"].filled == 3
    assert counts["selected", "lhe", "theta1"].filled == 0
    # An LHE invalidity must not remove otherwise valid dressed values.
    assert counts["selected", "dressed", "theta1"].filled == 2


def test_folding_keeps_flow_weights_and_covariance(tmp_path):
    arrays = columns()
    arrays["lhe_m_Z1"] = np.array([-1., .5, 1., 2.])
    report = read_report(write_file(tmp_path, arrays), variables=["m_Z1"],
                         bin_edges={"m_Z1": [0, .75, 1.]})
    all_hist = report.histograms["all", "lhe", "m_Z1"]
    selected_hist = report.histograms["selected", "lhe", "m_Z1"]
    np.testing.assert_array_equal(all_hist.values(), [1, 4])
    np.testing.assert_array_equal(all_hist.variances(), [5, 10])
    np.testing.assert_array_equal(selected_hist.values(), [2, 1])
    counter = report.counters["all", "lhe", "m_Z1"]
    assert counter.underflow == 1 and counter.overflow == 1
    assert counter.underflow_sumw_pb == 2 and counter.overflow_sumw_pb == 1
    assert counter.sumw_pb == 5
    np.testing.assert_allclose(subset_ratio(selected_hist, all_hist).values, [2, .25])


def test_chunking_and_full_registry(tmp_path):
    path = write_file(tmp_path, columns())
    whole = read_report(path)
    chunked = read_report(path, step_size=1)
    assert len(chunked.histograms) == 77*5
    assert chunked.entries == whole.entries
    for key, histogram in whole.histograms.items():
        np.testing.assert_allclose(histogram.values(), chunked.histograms[key].values())
        np.testing.assert_allclose(histogram.variances(), chunked.histograms[key].variances())
        assert whole.counters[key] == chunked.counters[key]


@pytest.mark.parametrize("name", ["weight_nominal_pb", "reconstructed", "lhe_m_Z1", "reco_projection_valid"])
def test_missing_required_branches_fail(tmp_path, name):
    arrays = columns()
    arrays.pop(name)
    with pytest.raises(ValueError, match="missing required branches"):
        read_report(write_file(tmp_path, arrays), variables=["m_Z1"])


def test_legacy_file_requires_explicit_luminosity_and_override_is_respected(tmp_path):
    arrays = columns()
    arrays.pop("lumi")
    path = write_file(tmp_path, arrays)
    with pytest.raises(ValueError, match="luminosity override"):
        read_report(path, variables=["m_Z1"])
    report = read_report(path, lumi_pb=29000, variables=["m_Z1"])
    assert report.lumi_pb == 29000 and report.stored_lumi_pb is None
    arrays["lumi"] = np.full(4, 312000.)
    report = read_report(write_file(tmp_path, arrays), lumi_pb=29000, variables=["m_Z1"])
    assert report.lumi_pb == 29000 and report.stored_lumi_pb == 312000


@pytest.mark.parametrize("name,value", [("weight_nominal_pb", np.nan),
                                       ("weight_nominal_pb", np.inf),
                                       ("lumi", 0.), ("lumi", 1.),
                                       ("lumi", np.nan), ("sample_code", 2)])
def test_malformed_weights_or_constant_metadata_fail(tmp_path, name, value):
    arrays = columns()
    arrays[name][2] = value
    with pytest.raises(ValueError, match=name):
        read_report(write_file(tmp_path, arrays), variables=["m_Z1"], step_size=1)


@pytest.mark.parametrize("edges", [[0, 0, 1], [0, np.nan], [2, 1], [0]])
def test_bad_bin_edges_fail(edges):
    with pytest.raises(ValueError, match="Bin edges"):
        observable_specs({"m_Z1": edges})


def test_unknown_observable_fails_before_file_access(tmp_path):
    with pytest.raises(ValueError, match="Unknown observables"):
        read_report(tmp_path / "absent.root", variables=["not_a_branch"])


def test_truth_selection_kinematics_match_existing_selection_values(tmp_path):
    # Two nearly adjacent electrons straddle the +/-pi seam. Their separation
    # must use wrapped delta-phi, while pT ordering is independent of flavour.
    leptons = {
        "electron_minus": vector.obj(pt=31., eta=.4, phi=np.pi-.01, mass=.000511),
        "electron_plus": vector.obj(pt=17., eta=.4, phi=-np.pi+.02, mass=.000511),
        "muon_minus": vector.obj(pt=56., eta=-.9, phi=1., mass=.105),
        "muon_plus": vector.obj(pt=12., eta=.8, phi=-1.8, mass=.105),
    }
    expected = evaluate_reco_selection(leptons).to_record("truth")
    arrays = columns(n=1)
    for level in ("lhe", "dressed", "reco"):
        for lepton, momentum in leptons.items():
            for component in MOMENTUM_COMPONENTS:
                arrays[f"{level}_raw_{lepton}_{component}"] = np.array([getattr(momentum, component)])
    values = _raw_selection_observables(arrays, "lhe")
    assert len(values) == 21
    for name, actual in values.items():
        np.testing.assert_allclose(actual, [expected[f"truth_{name}"]], rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(values["delta_r_electron_minus_electron_plus"], [.03])
    np.testing.assert_allclose([values[f"ordered_pt{i}"][0] for i in range(1, 5)], [56, 31, 17, 12])
    # The reader uses raw truth vectors and the already-stored RECO diagnostic.
    # There are deliberately no LHE/dressed *_for_selection branches.
    arrays["reco_ordered_pt1"] = np.array([90.])
    report = read_report(write_file(tmp_path, arrays), variables=["ordered_pt1"],
                         bin_edges={"ordered_pt1": [0, 70, 100]})
    for level in ("lhe", "dressed"):
        np.testing.assert_array_equal(report.histograms["all", level, "ordered_pt1"].values(), [2, 0])
    np.testing.assert_array_equal(report.histograms["selected", "reco", "ordered_pt1"].values(), [0, 2])


def test_derived_diagnostics_require_raw_inputs_but_not_projection(tmp_path):
    arrays = columns()
    arrays["lhe_projection_valid"][:] = False
    report = read_report(write_file(tmp_path, arrays), variables=["ordered_pt1"])
    assert report.counters["all", "lhe", "ordered_pt1"].filled == 4
    arrays.pop("lhe_raw_muon_plus_py")
    with pytest.raises(ValueError, match="lhe_raw_muon_plus_py"):
        read_report(write_file(tmp_path, arrays), variables=["ordered_pt1"])
