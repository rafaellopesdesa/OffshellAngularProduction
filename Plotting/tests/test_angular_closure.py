"""Angular reconstruction normalization, omissions, covariance and ROOT input."""

from pathlib import Path

import numpy as np
import pytest
import uproot

from offshell_production.angular_closure import (
    COMPONENT_SLUGS, CONTEXTS, angular_basis, angular_coordinates,
    closure_projections, empty_context, read_closure,
)


def quadrature(bins=8, order=8):
    """Bin-aligned quadrature; sin(psi) removes polar endpoint square roots."""
    nodes, weights = np.polynomial.legendre.leggauss(order)
    edges = np.arcsin(np.linspace(-1, 1, bins + 1))
    psi = ((edges[:-1, None] + edges[1:, None]) / 2
           + np.diff(edges)[:, None] * nodes / 2).ravel()
    wx = (np.diff(edges)[:, None] * weights / 2).ravel() * np.cos(psi) / 2
    x = np.sin(psi)
    edges = np.linspace(-np.pi, np.pi, bins + 1)
    delta = ((edges[:-1, None] + edges[1:, None]) / 2
             + np.diff(edges)[:, None] * nodes / 2).ravel()
    wd = (np.diff(edges)[:, None] * weights / (4 * np.pi)).ravel()
    xx, yy, dd = np.meshgrid(x, x, delta, indexing="ij")
    measure = (wx[:, None, None] * wx[None, :, None] * wd[None, None, :]).ravel()
    angles = (np.arccos(xx.ravel()), np.zeros(xx.size), np.arccos(yy.ravel()), dd.ravel())
    return angles, measure


@pytest.fixture(scope="module")
def angular_quadrature():
    return quadrature()


def test_basis_is_orthonormal_including_opposite_m_modes(angular_quadrature):
    angles, measure = angular_quadrature
    basis = angular_basis(*angles)
    np.testing.assert_allclose(basis.T @ (measure[:, None] * basis), np.eye(5), atol=2e-12)
    assert np.all(basis[:, 0] == 1)


def test_templates_and_retained_density_close_in_all_informative_marginals(angular_quadrature):
    angles, measure = angular_quadrature
    basis = angular_basis(*angles)
    specs = closure_projections(8, 8)
    # Single azimuths are not integrated by this three-coordinate quadrature.
    names = ("cos_theta1", "cos_theta2", "delta_phi", "folded_delta_phi", "cos_theta1_vs_cos_theta2")
    context = empty_context("all", "lhe", {name: specs[name] for name in names})
    coefficients = np.array([1., .12, -.09, .06, .08]) * 3.2
    context.accumulate(angles, measure * (basis @ coefficients))
    np.testing.assert_allclose(context.coefficients, coefficients, atol=2e-11)
    for name in names:
        result = context.evaluate(name)
        np.testing.assert_allclose(result.model, context.projections[name].nominal, atol=2e-11)
        np.testing.assert_allclose(result.ratio.values, 1., atol=2e-10)
        np.testing.assert_allclose(specs[name].templates.sum(axis=0), [1, 0, 0, 0, 0], atol=5e-16)
    # The folded azimuth is essential: m=1 cancels from ordinary marginals.
    assert np.all(specs["delta_phi"].templates[:, 3] == 0)
    assert np.max(np.abs(specs["folded_delta_phi"].templates[:, 3])) > .1
    omitted = context.evaluate("folded_delta_phi", components=["00_20", "20_20", "2m2_2p2"])
    assert np.max(np.abs(omitted.residual)) > .01


def test_omitted_l4_density_fails_shape_closure(angular_quadrature):
    angles, measure = angular_quadrature
    x = np.cos(angles[0])
    p4 = (35 * x**4 - 30 * x**2 + 3) / 8
    spec = closure_projections(8)["cos_theta1"]
    context = empty_context("all", "lhe", {spec.name: spec})
    context.accumulate(angles, measure * (1 + .5 * p4))
    np.testing.assert_allclose(context.coefficients, [1, 0, 0, 0, 0], atol=2e-11)
    result = context.evaluate(spec.name)
    assert np.max(np.abs(result.residual)) > .01
    # The fitted constant guarantees global yield, not shape, closure.
    assert abs(result.residual.sum()) < 1e-12


def test_theta_jacobian_and_single_azimuth_templates():
    specs = closure_projections(12)
    for name in ("theta1", "theta2"):
        spec = specs[name]
        np.testing.assert_allclose(spec.templates[:, 0], -np.diff(np.cos(spec.edges[0])) / 2)
        np.testing.assert_allclose(spec.templates.sum(axis=0), [1, 0, 0, 0, 0], atol=5e-16)
        assert np.all(spec.templates[:, 2:] == 0)
    for name in ("phi1", "phi2"):
        np.testing.assert_allclose(specs[name].templates[:, 0], 1 / 12)
        assert np.all(specs[name].templates[:, 1:] == 0)


def test_signed_covariances_match_eventwise_residual_and_ratio():
    rng = np.random.default_rng(8921)
    angles = (np.arccos(rng.uniform(-1, 1, 31)), rng.uniform(-4, 4, 31),
              np.arccos(rng.uniform(-1, 1, 31)), rng.uniform(-4, 4, 31))
    weights = rng.uniform(.1, 2, 31) * rng.choice([-1, 1], 31)
    spec = closure_projections(6)["folded_delta_phi"]
    context = empty_context("selected", "reco", {spec.name: spec})
    context.accumulate(angles, weights)
    basis = angular_basis(*angles)
    indices = np.searchsorted(spec.edges[0], angular_coordinates(*angles)[spec.name], side="right") - 1
    indicator = (np.arange(6)[None, :] == indices[:, None]).astype(float)
    predicted = basis @ spec.templates.T
    result = context.evaluate(spec.name)
    np.testing.assert_allclose(result.model, (weights[:, None] * predicted).sum(axis=0))
    np.testing.assert_allclose(result.model_variances, ((weights[:, None] * predicted)**2).sum(axis=0))
    np.testing.assert_allclose(result.residual_variances,
                               ((weights[:, None] * (predicted - indicator))**2).sum(axis=0))
    np.testing.assert_allclose(result.nominal_model_covariance,
                               (weights[:, None]**2 * predicted * indicator).sum(axis=0))
    valid, ratio = result.ratio.valid, result.ratio.values
    denominator = context.projections[spec.name].nominal
    expected = ((weights[:, None] * (predicted[:, valid] - ratio[valid] * indicator[:, valid]))**2).sum(axis=0)
    np.testing.assert_allclose(result.ratio.variances[valid], expected / denominator[valid]**2)
    normalized = context.normalized_coefficients()
    np.testing.assert_allclose(normalized.values, context.coefficients / context.coefficients[0])
    assert normalized.values[0] == 1 and normalized.variances[0] == 0


def test_empty_and_cancelled_denominators_are_masked():
    spec = closure_projections(4)["cos_theta1"]
    context = empty_context("selected", "reco", {spec.name: spec})
    result = context.evaluate(spec.name)
    assert not np.any(result.ratio.valid)
    assert not np.any(result.pull_valid)
    context.accumulate([np.array([1., 1.]), np.zeros(2), np.ones(2), np.zeros(2)], np.array([1., -1.]))
    assert not np.any(context.evaluate(spec.name).ratio.valid)
    assert not np.any(context.normalized_coefficients().valid)


def columns():
    n = 6
    weights = np.array([2., -1., 3., 0., .5, 1.])
    columns = {"weight_nominal_pb": weights, "lumi": np.full(n, 312000.),
               "sample_code": np.zeros(n, dtype=np.uint8),
               "reconstructed": np.array([True, False, True, False, True, True]),
               "truth_lhe_valid": np.ones(n, dtype=bool)}
    angles = [np.linspace(.2, 2.8, n), np.linspace(-4, 4, n), np.linspace(.4, 2.4, n), np.linspace(3, -3, n)]
    for level in ("lhe", "dressed", "reco"):
        columns[f"{level}_projection_valid"] = np.ones(n, dtype=bool)
        columns[f"{level}_frame_degenerate"] = np.zeros(n, dtype=bool)
        for name, values in zip(("theta1", "phi1", "theta2", "phi2"), angles):
            columns[f"{level}_{name}"] = values.copy()
    moments = weights[:, None] * angular_basis(*angles)
    for index, slug in enumerate(COMPONENT_SLUGS[1:], 1):
        columns[f"weight_truth_{slug}_pb"] = moments[:, index]
    return columns


def write_root(tmp_path: Path, columns):
    path = tmp_path / "merged.root"
    with uproot.recreate(path) as root:
        tree = root.mktree("Events", {key: value.dtype for key, value in columns.items()})
        tree.extend(columns)
    return path


def test_root_chunking_and_level_validity_use_common_masks(tmp_path):
    arrays = columns()
    arrays["lhe_frame_degenerate"][0] = True
    arrays["truth_lhe_valid"][1] = False
    arrays["weight_truth_20_20_pb"][1] = np.nan  # Invalid truth may retain NaN.
    arrays["dressed_theta1"][2] = np.pi + .01
    arrays["reco_phi2"][4] = np.nan
    arrays["reco_projection_valid"][5] = False
    path = write_root(tmp_path, arrays)
    report = read_closure(path, bins=4, map_bins=3, step_size=2)
    one_chunk = read_closure(path, bins=4, map_bins=3)
    assert report.entries == 6 and report.selected_entries == 4
    assert report.sample_label == "gg4l" and report.lumi_pb == 312000
    assert tuple(report.contexts) == CONTEXTS
    expected_counts = [4, 5, 3, 3, 2]
    for key, count in zip(CONTEXTS, expected_counts):
        context = report.contexts[key]
        assert context.entries == count
        assert context.entries + context.excluded_entries == context.total_entries
        np.testing.assert_allclose(context.coefficients, one_chunk.contexts[key].coefficients, atol=2e-15)
        np.testing.assert_allclose(context.covariance, one_chunk.contexts[key].covariance, atol=2e-14)
        for projection in context.projections.values():
            assert projection.nominal.sum() == pytest.approx(context.coefficients[0])
    assert report.contexts["all", "lhe"].degenerate_entries == 1
    override = read_closure(path, lumi_pb=1000, components=[], variables=["theta1"])
    assert override.lumi_pb == 1000 and override.stored_lumi_pb == 312000
    assert override.components == ("00_00",)


@pytest.mark.parametrize("alteration,match", [
    ("truth_mismatch", "disagree"), ("truth_nonfinite", "nonfinite"),
    ("truth_missing", "required branches"), ("weight_nonfinite", "nonfinite"),
    ("lumi_changes", "lumi"), ("sample_changes", "sample_code"),
])
def test_invalid_merged_schema_rejected(tmp_path, alteration, match):
    arrays = columns()
    if alteration == "truth_mismatch":
        arrays["weight_truth_20_20_pb"][2] += .01
    elif alteration == "truth_nonfinite":
        arrays["weight_truth_20_20_pb"][2] = np.nan
    elif alteration == "truth_missing":
        del arrays["weight_truth_20_20_pb"]
    elif alteration == "weight_nonfinite":
        arrays["weight_nominal_pb"][2] = np.inf
    elif alteration == "lumi_changes":
        arrays["lumi"][2:] = 1000.
    elif alteration == "sample_changes":
        arrays["sample_code"][2:] = 1
    with pytest.raises(ValueError, match=match):
        read_closure(write_root(tmp_path, arrays), step_size=2)


def test_lumi_override_is_required_for_older_merged_files(tmp_path):
    arrays = columns()
    del arrays["lumi"]
    path = write_root(tmp_path, arrays)
    with pytest.raises(ValueError, match="luminosity override"):
        read_closure(path)
    assert read_closure(path, lumi_pb=1500).lumi_pb == 1500
    with pytest.raises(ValueError, match="positive"):
        read_closure(path, lumi_pb=-1)
    with pytest.raises(ValueError, match="Unknown angular components"):
        read_closure(path, lumi_pb=1500, components=["fake"])
    with pytest.raises(ValueError, match="Unknown closure variables"):
        read_closure(path, lumi_pb=1500, variables=["Phi"])
