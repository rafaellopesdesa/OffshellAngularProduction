r"""Truth-level symmetric angular factors for the selected off-shell modes.

The factors in this module are the real event-level projectors

.. math::

   F_{\alpha\beta}(\Omega_1,\Omega_2)
   = 4\pi\,\operatorname{Re}\left[
       \mathcal{Y}^{(+)*}_{\alpha\beta}(\Omega_1,\Omega_2)
     \right].

Multiplying one of these dimensionless factors by an event's normalized,
signed generator weight gives that event's contribution to the corresponding
angular coefficient.  The four requested modes are real algebraically, so the
closed forms below need only NumPy and do not require a spherical-harmonic
library.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

import numpy as np
from numpy.typing import ArrayLike, NDArray


InvalidAnglePolicy = Literal["nan", "raise"]


@dataclass(frozen=True, slots=True)
class TruthAngularComponent:
    """Stable definition of one retained symmetric angular component."""

    label: str
    branch_slug: str
    l1: int
    m1: int
    l2: int
    m2: int

    @property
    def modes(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Return the two ``(ell, m)`` modes in canonical order."""

        return (self.l1, self.m1), (self.l2, self.m2)


TRUTH_ANGULAR_COMPONENTS = (
    TruthAngularComponent("(0,0;2,0)", "00_20", 0, 0, 2, 0),
    TruthAngularComponent("(2,0;2,0)", "20_20", 2, 0, 2, 0),
    TruthAngularComponent("(2,-1;2,1)", "2m1_2p1", 2, -1, 2, 1),
    TruthAngularComponent("(2,-2;2,2)", "2m2_2p2", 2, -2, 2, 2),
)
"""Requested components in stable output-branch order."""


TRUTH_ANGULAR_COMPONENT_BY_SLUG: Mapping[str, TruthAngularComponent] = (
    MappingProxyType(
        {component.branch_slug: component for component in TRUTH_ANGULAR_COMPONENTS}
    )
)
"""Read-only lookup of component definitions by branch-safe slug."""


TRUTH_ANGULAR_BRANCH_SLUGS = tuple(
    component.branch_slug for component in TRUTH_ANGULAR_COMPONENTS
)
"""Stable sequence used when constructing output schemas."""


def _broadcast_angles(
    theta1: ArrayLike,
    phi1: ArrayLike,
    theta2: ArrayLike,
    phi2: ArrayLike,
) -> tuple[NDArray[np.float64], ...]:
    try:
        arrays = np.broadcast_arrays(
            np.asarray(theta1, dtype=np.float64),
            np.asarray(phi1, dtype=np.float64),
            np.asarray(theta2, dtype=np.float64),
            np.asarray(phi2, dtype=np.float64),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "angular coordinates must be real numeric values with "
            "broadcast-compatible shapes"
        ) from exc
    return tuple(np.asarray(array, dtype=np.float64) for array in arrays)


def finite_angle_mask(
    theta1: ArrayLike,
    phi1: ArrayLike,
    theta2: ArrayLike,
    phi2: ArrayLike,
) -> NDArray[np.bool_]:
    """Return where all four harmonic coordinates are finite."""

    angles = _broadcast_angles(theta1, phi1, theta2, phi2)
    return np.logical_and.reduce(tuple(np.isfinite(angle) for angle in angles))


def _validate_invalid_policy(invalid: InvalidAnglePolicy) -> InvalidAnglePolicy:
    if invalid not in {"nan", "raise"}:
        raise ValueError("invalid must be either 'nan' or 'raise'")
    return invalid


def _component_factors(
    theta1: NDArray[np.float64],
    phi1: NDArray[np.float64],
    theta2: NDArray[np.float64],
    phi2: NDArray[np.float64],
) -> dict[str, NDArray[np.float64]]:
    """Evaluate the four closed-form ``4*pi*Re(Y_plus*)`` factors."""

    cos_theta1 = np.cos(theta1)
    cos_theta2 = np.cos(theta2)
    sin_theta1 = np.sin(theta1)
    sin_theta2 = np.sin(theta2)
    delta_phi = phi2 - phi1

    legendre1 = 3.0 * cos_theta1**2 - 1.0
    legendre2 = 3.0 * cos_theta2**2 - 1.0

    return {
        "00_20": np.sqrt(5.0 / 8.0) * (legendre1 + legendre2),
        "20_20": 5.0 / 4.0 * legendre1 * legendre2,
        "2m1_2p1": (
            -15.0
            * np.sqrt(2.0)
            / 2.0
            * sin_theta1
            * cos_theta1
            * sin_theta2
            * cos_theta2
            * np.cos(delta_phi)
        ),
        "2m2_2p2": (
            15.0
            * np.sqrt(2.0)
            / 8.0
            * sin_theta1**2
            * sin_theta2**2
            * np.cos(2.0 * delta_phi)
        ),
    }


def truth_angular_factors(
    theta1: ArrayLike,
    phi1: ArrayLike,
    theta2: ArrayLike,
    phi2: ArrayLike,
    *,
    invalid: InvalidAnglePolicy = "nan",
) -> dict[str, NDArray[np.float64]]:
    r"""Return all requested ``4*pi*Re(Y_plus*)`` projection factors.

    Inputs follow NumPy broadcasting.  By default, an event with any non-finite
    angle receives ``NaN`` for every component, which preserves invalid rows in
    an analysis tree.  Pass ``invalid="raise"`` to require a fully finite input.
    """

    invalid = _validate_invalid_policy(invalid)
    theta1_array, phi1_array, theta2_array, phi2_array = _broadcast_angles(
        theta1, phi1, theta2, phi2
    )
    valid = np.logical_and.reduce(
        (
            np.isfinite(theta1_array),
            np.isfinite(phi1_array),
            np.isfinite(theta2_array),
            np.isfinite(phi2_array),
        )
    )
    if invalid == "raise" and not bool(np.all(valid)):
        raise ValueError("angular coordinates must be finite")

    with np.errstate(invalid="ignore"):
        factors = _component_factors(
            theta1_array, phi1_array, theta2_array, phi2_array
        )
    if bool(np.all(valid)):
        return factors
    return {
        slug: np.where(valid, factor, np.nan)
        for slug, factor in factors.items()
    }


def _resolve_component(
    component: str | TruthAngularComponent,
) -> TruthAngularComponent:
    if isinstance(component, TruthAngularComponent):
        registered = TRUTH_ANGULAR_COMPONENT_BY_SLUG.get(component.branch_slug)
        if registered != component:
            raise ValueError(f"unregistered angular component {component!r}")
        return registered
    try:
        return TRUTH_ANGULAR_COMPONENT_BY_SLUG[str(component)]
    except KeyError as exc:
        raise ValueError(
            f"unknown angular component {component!r}; expected one of "
            f"{TRUTH_ANGULAR_BRANCH_SLUGS}"
        ) from exc


def truth_angular_factor(
    theta1: ArrayLike,
    phi1: ArrayLike,
    theta2: ArrayLike,
    phi2: ArrayLike,
    component: str | TruthAngularComponent,
    *,
    invalid: InvalidAnglePolicy = "nan",
) -> NDArray[np.float64]:
    """Return one requested truth angular factor."""

    definition = _resolve_component(component)
    return truth_angular_factors(
        theta1, phi1, theta2, phi2, invalid=invalid
    )[definition.branch_slug]


def truth_angular_weights(
    nominal_weight: ArrayLike,
    theta1: ArrayLike,
    phi1: ArrayLike,
    theta2: ArrayLike,
    phi2: ArrayLike,
    *,
    invalid: InvalidAnglePolicy = "nan",
) -> dict[str, NDArray[np.float64]]:
    r"""Multiply signed nominal weights by every truth projection factor.

    The nominal weights must already carry the desired merged-sample
    normalization.  No absolute value or normalization by ``S_00;00`` is
    applied here.
    """

    invalid = _validate_invalid_policy(invalid)
    factors = truth_angular_factors(
        theta1, phi1, theta2, phi2, invalid=invalid
    )
    try:
        nominal = np.asarray(nominal_weight, dtype=np.float64)
        nominal, first_factor = np.broadcast_arrays(
            nominal, factors[TRUTH_ANGULAR_BRANCH_SLUGS[0]]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "nominal weights must be real numeric values broadcast-compatible "
            "with the angular coordinates"
        ) from exc

    finite_weight = np.isfinite(nominal)
    if invalid == "raise" and not bool(np.all(finite_weight)):
        raise ValueError("nominal weights must be finite")

    output: dict[str, NDArray[np.float64]] = {}
    for slug, factor in factors.items():
        factor = np.broadcast_to(factor, first_factor.shape)
        with np.errstate(invalid="ignore", over="ignore"):
            weighted = nominal * factor
        output[slug] = np.where(finite_weight & np.isfinite(factor), weighted, np.nan)
    return output


__all__ = [
    "TRUTH_ANGULAR_BRANCH_SLUGS",
    "TRUTH_ANGULAR_COMPONENTS",
    "TRUTH_ANGULAR_COMPONENT_BY_SLUG",
    "TruthAngularComponent",
    "finite_angle_mask",
    "truth_angular_factor",
    "truth_angular_factors",
    "truth_angular_weights",
]
