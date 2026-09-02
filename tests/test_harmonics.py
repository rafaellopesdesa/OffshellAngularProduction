import numpy as np
import pytest

from offshell_production import (
    TRUTH_ANGULAR_BRANCH_SLUGS,
    TRUTH_ANGULAR_COMPONENTS,
    TRUTH_ANGULAR_COMPONENT_BY_SLUG,
    finite_angle_mask,
    truth_angular_factor,
    truth_angular_factors,
    truth_angular_weights,
)


def test_component_definitions_and_branch_slugs_are_stable():
    assert TRUTH_ANGULAR_BRANCH_SLUGS == (
        "00_20",
        "20_20",
        "2m1_2p1",
        "2m2_2p2",
    )
    assert tuple(TRUTH_ANGULAR_COMPONENT_BY_SLUG) == TRUTH_ANGULAR_BRANCH_SLUGS
    assert tuple(component.modes for component in TRUTH_ANGULAR_COMPONENTS) == (
        ((0, 0), (2, 0)),
        ((2, 0), (2, 0)),
        ((2, -1), (2, 1)),
        ((2, -2), (2, 2)),
    )
    assert tuple(component.label for component in TRUTH_ANGULAR_COMPONENTS) == (
        "(0,0;2,0)",
        "(2,0;2,0)",
        "(2,-1;2,1)",
        "(2,-2;2,2)",
    )


def test_analytic_factors_at_beam_axis_and_equator():
    axis = truth_angular_factors(0.0, 0.4, 0.0, -2.0)
    assert float(axis["00_20"]) == pytest.approx(np.sqrt(10.0))
    assert float(axis["20_20"]) == pytest.approx(5.0)
    assert float(axis["2m1_2p1"]) == pytest.approx(0.0, abs=1.0e-15)
    assert float(axis["2m2_2p2"]) == pytest.approx(0.0, abs=1.0e-15)

    equator = truth_angular_factors(np.pi / 2.0, 0.0, np.pi / 2.0, 0.0)
    assert float(equator["00_20"]) == pytest.approx(-np.sqrt(5.0 / 2.0))
    assert float(equator["20_20"]) == pytest.approx(5.0 / 4.0)
    assert float(equator["2m1_2p1"]) == pytest.approx(0.0, abs=1.0e-15)
    assert float(equator["2m2_2p2"]) == pytest.approx(15.0 * np.sqrt(2.0) / 8.0)


def test_mixed_m_factors_at_quarter_turn_hand_point():
    factors = truth_angular_factors(
        np.pi / 4.0,
        0.0,
        np.pi / 4.0,
        0.0,
    )
    assert float(factors["2m1_2p1"]) == pytest.approx(
        -15.0 * np.sqrt(2.0) / 8.0
    )
    assert float(factors["2m2_2p2"]) == pytest.approx(
        15.0 * np.sqrt(2.0) / 32.0
    )


def test_all_factors_are_exchange_symmetric_and_vectorized():
    theta1 = np.array([0.2, 0.7, 1.6, 2.4])
    phi1 = np.array([-2.4, -0.3, 0.8, 2.1])
    theta2 = np.array([2.7, 1.2, 0.5, 1.9])
    phi2 = np.array([1.1, -2.2, 2.7, -0.4])

    direct = truth_angular_factors(theta1, phi1, theta2, phi2)
    exchanged = truth_angular_factors(theta2, phi2, theta1, phi1)

    assert tuple(direct) == TRUTH_ANGULAR_BRANCH_SLUGS
    for slug in TRUTH_ANGULAR_BRANCH_SLUGS:
        assert direct[slug].shape == theta1.shape
        np.testing.assert_allclose(direct[slug], exchanged[slug], rtol=2.0e-15)


def test_broadcasting_and_nan_policy_preserve_invalid_rows():
    theta1 = np.array([0.3, np.nan, 1.1])
    theta2 = np.array([[0.5], [1.7]])
    mask = finite_angle_mask(theta1, 0.2, theta2, -0.4)
    np.testing.assert_array_equal(
        mask,
        [[True, False, True], [True, False, True]],
    )

    factors = truth_angular_factors(theta1, 0.2, theta2, -0.4)
    for factor in factors.values():
        assert factor.shape == (2, 3)
        assert np.isnan(factor[:, 1]).all()
        assert np.isfinite(factor[:, [0, 2]]).all()

    with pytest.raises(ValueError, match="must be finite"):
        truth_angular_factors(theta1, 0.2, theta2, -0.4, invalid="raise")
    with pytest.raises(ValueError, match="must be either"):
        truth_angular_factors(0.2, 0.3, 0.4, 0.5, invalid="ignore")


def test_single_component_lookup_accepts_slug_or_registered_definition():
    expected = truth_angular_factors(0.3, -0.7, 1.2, 2.4)["2m1_2p1"]
    by_slug = truth_angular_factor(0.3, -0.7, 1.2, 2.4, "2m1_2p1")
    definition = TRUTH_ANGULAR_COMPONENT_BY_SLUG["2m1_2p1"]
    by_definition = truth_angular_factor(0.3, -0.7, 1.2, 2.4, definition)
    np.testing.assert_allclose(by_slug, expected)
    np.testing.assert_allclose(by_definition, expected)

    with pytest.raises(ValueError, match="unknown angular component"):
        truth_angular_factor(0.3, -0.7, 1.2, 2.4, "not_a_component")


def test_truth_weights_multiply_signed_post_normalization_weight():
    nominal = np.array([2.0, -0.5, 0.0, np.nan])
    factors = truth_angular_factors(np.pi / 2.0, 0.0, np.pi / 2.0, 0.0)
    weighted = truth_angular_weights(
        nominal,
        np.pi / 2.0,
        0.0,
        np.pi / 2.0,
        0.0,
    )
    for slug in TRUTH_ANGULAR_BRANCH_SLUGS:
        np.testing.assert_allclose(
            weighted[slug][:3],
            nominal[:3] * float(factors[slug]),
        )
        assert np.isnan(weighted[slug][3])

    with pytest.raises(ValueError, match="nominal weights must be finite"):
        truth_angular_weights(
            nominal,
            np.pi / 2.0,
            0.0,
            np.pi / 2.0,
            0.0,
            invalid="raise",
        )
