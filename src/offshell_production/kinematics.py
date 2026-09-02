"""Born projection and angular conventions for ``ZZ -> 2e2mu`` events.

The functions in this module are deliberately independent of event selection.  A
caller should construct the four-lepton candidate and evaluate any detector-level
selection on the original momenta before using the Born-projected momenta for the
angular calculation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

import numpy as np
import vector

LEPTON_KEYS = (
    "electron_minus",
    "electron_plus",
    "muon_minus",
    "muon_plus",
)
MOMENTUM_COMPONENTS = ("px", "py", "pz", "E")

ANGULAR_NUMERIC_FIELDS = (
    "m_Z1",
    "m_Z2",
    "m_ZZ",
    "y_ZZ",
    "pt_ZZ",
    "theta1",
    "phi1",
    "cos_theta1",
    "theta2",
    "phi2",
    "cos_theta2",
    "cos_theta_star",
    "abs_cos_theta_star",
    "theta1_standard",
    "theta2_standard",
    "Phi",
    "Phi1",
    "Psi",
)
ANGULAR_BOOLEAN_FIELDS = ("frame_degenerate", "standard_angles_degenerate")
PROJECTION_DIAGNOSTIC_FIELDS = (
    "raw_m4l",
    "born_m4l",
    "raw_y4l",
    "born_y4l",
    "raw_pt4l",
    "born_pt4l",
)

_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class FourLeptonCandidate:
    """The fixed-flavor four-lepton candidate, constructed without cuts.

    ``Z1`` is always the dimuon system and ``Z2`` is always the dielectron
    system.  This convention is never replaced by mass ordering.
    """

    leptons: dict[str, object]
    z1: object
    z2: object
    four_lepton: object


@dataclass(frozen=True)
class BornProjectionDiagnostics:
    """Numerical diagnostics for the ISR-removing Born projection."""

    raw_m4l: float
    born_m4l: float
    raw_y4l: float
    born_y4l: float
    raw_pt4l: float
    born_pt4l: float


def _validate_namespace(namespace: str) -> str:
    namespace = str(namespace)
    if not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError(
            "namespace must start with a letter and contain only letters, "
            "digits, and underscores"
        )
    return namespace


def _sum_p4(momentum_map: Mapping[str, object]):
    iterator = iter(momentum_map.values())
    try:
        total = next(iterator)
    except StopIteration as exc:
        raise ValueError("At least one four-vector is required") from exc
    for momentum in iterator:
        total = total + momentum
    return total


def _spatial(momentum) -> np.ndarray:
    return np.array([momentum.px, momentum.py, momentum.pz], dtype=np.float64)


def _unit(value: np.ndarray, *, tolerance: float = 1.0e-14) -> np.ndarray:
    norm = float(np.linalg.norm(value))
    if not np.isfinite(norm) or norm <= tolerance:
        raise ValueError("Cannot define a direction from a non-finite or zero vector")
    return np.asarray(value, dtype=np.float64) / norm


def _clip_cosine(value: float) -> float:
    return float(np.clip(value, -1.0, 1.0))


def _signed_acos(cosine: float, orientation: float) -> float:
    angle = float(np.arccos(_clip_cosine(cosine)))
    if orientation < 0.0:
        return -angle
    return angle


def wrap_to_pi(angle: float) -> float:
    """Map an angle to ``[-pi, pi)``."""

    return float((angle + np.pi) % (2.0 * np.pi) - np.pi)


def build_four_lepton_candidate(
    leptons: Mapping[str, object],
) -> FourLeptonCandidate:
    """Construct the fixed ``2e2mu`` candidate without applying any cuts.

    A mapping can contain metadata or other particles, but it must provide one
    and only one scalar four-vector under every name in :data:`LEPTON_KEYS`.
    Mapping keys themselves are unique, so selecting one value for each required
    key encodes the exact charge/flavor multiplicity after particle collection.
    """

    missing = set(LEPTON_KEYS) - set(leptons)
    if missing:
        raise ValueError(f"Missing required leptons: {sorted(missing)}")

    selected = {key: leptons[key] for key in LEPTON_KEYS}
    for key, momentum in selected.items():
        required_attributes = (
            *MOMENTUM_COMPONENTS,
            "mass",
            "pt",
            "eta",
            "phi",
        )
        if any(not hasattr(momentum, attribute) for attribute in required_attributes):
            raise TypeError(f"{key} is not a scalar four-vector")

    z1 = selected["muon_minus"] + selected["muon_plus"]
    z2 = selected["electron_minus"] + selected["electron_plus"]
    return FourLeptonCandidate(
        leptons=selected,
        z1=z1,
        z2=z2,
        four_lepton=z1 + z2,
    )


def born_project_four_leptons(
    leptons: Mapping[str, object],
    *,
    check: bool = True,
    rtol: float = 2.0e-10,
    atol: float = 2.0e-10,
):
    r"""Map a recoiling color-singlet event to a Born-like configuration.

    Following Eqs. (2.14)--(2.18) of arXiv:2606.11083, the map first boosts
    longitudinally to set the four-lepton ``pz`` to zero, then boosts
    transversely to remove ``px`` and ``py``, and finally applies the inverse
    longitudinal boost.  The same Lorentz transformation is applied to all four
    leptons.  It preserves invariant masses and four-lepton rapidity while
    imposing zero four-lepton transverse momentum.
    """

    candidate = build_four_lepton_candidate(leptons)
    selected = candidate.leptons
    total = candidate.four_lepton
    total_components = np.array(
        [total.px, total.py, total.pz, total.E, total.mass], dtype=np.float64
    )
    if not np.all(np.isfinite(total_components)):
        raise ValueError("The four-lepton system must have finite momentum")
    if total.E <= 0.0 or total.mass <= 0.0:
        raise ValueError("The four-lepton system must be future-timelike")

    beta_l = vector.obj(x=0.0, y=0.0, z=float(total.pz / total.E))
    longitudinal_rest = {
        key: momentum.boostCM_of_beta3(beta_l) for key, momentum in selected.items()
    }
    total_l = _sum_p4(longitudinal_rest)

    beta_t = vector.obj(
        x=float(total_l.px / total_l.E),
        y=float(total_l.py / total_l.E),
        z=0.0,
    )
    zero_momentum = {
        key: momentum.boostCM_of_beta3(beta_t)
        for key, momentum in longitudinal_rest.items()
    }
    projected = {
        key: momentum.boost_beta3(beta_l) for key, momentum in zero_momentum.items()
    }
    projected_total = _sum_p4(projected)

    diagnostics = BornProjectionDiagnostics(
        raw_m4l=float(total.mass),
        born_m4l=float(projected_total.mass),
        raw_y4l=float(total.rapidity),
        born_y4l=float(projected_total.rapidity),
        raw_pt4l=float(total.pt),
        born_pt4l=float(projected_total.pt),
    )

    if check:
        scale = max(abs(float(total.E)), 1.0)
        if not np.isclose(
            diagnostics.raw_m4l,
            diagnostics.born_m4l,
            rtol=rtol,
            atol=atol,
        ):
            raise RuntimeError("Born projection did not preserve m4l")
        if not np.isclose(
            diagnostics.raw_y4l,
            diagnostics.born_y4l,
            rtol=rtol,
            atol=atol,
        ):
            raise RuntimeError("Born projection did not preserve y4l")
        if diagnostics.born_pt4l > atol * scale:
            raise RuntimeError("Born projection did not remove four-lepton pT")

    return projected, diagnostics


def _helicity_frame(z_direction: np.ndarray, beam_direction: np.ndarray):
    r"""Build a right-handed local helicity frame for one Z boson."""

    z_axis = _unit(z_direction)
    normal = np.cross(beam_direction, z_axis)
    degenerate = np.linalg.norm(normal) < 1.0e-12

    if not degenerate:
        y_axis = _unit(normal)
        x_axis = _unit(np.cross(y_axis, z_axis))
    else:
        reference = np.array([1.0, 0.0, 0.0])
        if abs(np.dot(reference, z_axis)) > 0.9:
            reference = np.array([0.0, 1.0, 0.0])
        x_axis = _unit(reference - np.dot(reference, z_axis) * z_axis)
        y_axis = _unit(np.cross(z_axis, x_axis))

    return x_axis, y_axis, z_axis, bool(degenerate)


def _positive_lepton_angles(positive_lepton, z_boson, frame):
    x_axis, y_axis, z_axis, _ = frame
    positive_in_z_rest = positive_lepton.boostCM_of_p4(z_boson)
    direction = _unit(_spatial(positive_in_z_rest))
    cos_theta = _clip_cosine(float(np.dot(direction, z_axis)))
    theta = float(np.arccos(cos_theta))
    phi = float(np.arctan2(np.dot(direction, y_axis), np.dot(direction, x_axis)))
    return theta, phi, cos_theta


def standard_five_angles(
    leptons: Mapping[str, object],
    *,
    beam_direction=(0.0, 0.0, 1.0),
) -> dict[str, float | bool]:
    r"""Compute the signed five-angle convention of arXiv:1208.4018.

    The negative lepton is the fermion ``q_i1``.  ``V1`` is always the dimuon
    system and ``V2`` the dielectron system.  The caller must pass Born-projected
    leptons if it wants the recoil-removed convention used by this project.
    """

    candidate = build_four_lepton_candidate(leptons)
    selected = candidate.leptons
    x_system = candidate.four_lepton
    in_x_rest = {
        key: momentum.boostCM_of_p4(x_system) for key, momentum in selected.items()
    }
    z1 = in_x_rest["muon_minus"] + in_x_rest["muon_plus"]
    z2 = in_x_rest["electron_minus"] + in_x_rest["electron_plus"]

    beam = _unit(np.asarray(beam_direction, dtype=np.float64))
    q1 = _spatial(z1)
    q1_hat = _unit(q1)

    q11 = _spatial(in_x_rest["muon_minus"])
    q12 = _spatial(in_x_rest["muon_plus"])
    q21 = _spatial(in_x_rest["electron_minus"])
    q22 = _spatial(in_x_rest["electron_plus"])

    decay_normal1 = np.cross(q11, q12)
    decay_normal2 = np.cross(q21, q22)
    production_normal = np.cross(beam, q1_hat)
    decay_planes_defined = (
        np.linalg.norm(decay_normal1) > 1.0e-14
        and np.linalg.norm(decay_normal2) > 1.0e-14
    )
    production_plane_defined = np.linalg.norm(production_normal) > 1.0e-14

    if decay_planes_defined:
        n1 = _unit(decay_normal1)
        n2 = _unit(decay_normal2)
        phi = _signed_acos(
            -float(np.dot(n1, n2)),
            float(np.dot(q1_hat, np.cross(n1, n2))),
        )
    else:
        n1 = None
        phi = float("nan")

    if decay_planes_defined and production_plane_defined:
        n_sc = _unit(production_normal)
        phi1 = _signed_acos(
            float(np.dot(n1, n_sc)),
            float(np.dot(q1_hat, np.cross(n1, n_sc))),
        )
        psi = wrap_to_pi(phi1 + 0.5 * phi)
    else:
        phi1 = float("nan")
        psi = float("nan")

    mu_minus_z1 = in_x_rest["muon_minus"].boostCM_of_p4(z1)
    z2_in_z1 = z2.boostCM_of_p4(z1)
    electron_minus_z2 = in_x_rest["electron_minus"].boostCM_of_p4(z2)
    z1_in_z2 = z1.boostCM_of_p4(z2)

    cos_theta1 = -float(np.dot(_unit(_spatial(z2_in_z1)), _unit(_spatial(mu_minus_z1))))
    cos_theta2 = -float(
        np.dot(_unit(_spatial(z1_in_z2)), _unit(_spatial(electron_minus_z2)))
    )

    return {
        "cos_theta_star": _clip_cosine(float(np.dot(beam, q1_hat))),
        "abs_cos_theta_star": abs(_clip_cosine(float(np.dot(beam, q1_hat)))),
        "theta1_standard": float(np.arccos(_clip_cosine(cos_theta1))),
        "theta2_standard": float(np.arccos(_clip_cosine(cos_theta2))),
        "Phi": phi,
        "Phi1": phi1,
        "Psi": psi,
        "standard_angles_degenerate": not (
            decay_planes_defined and production_plane_defined
        ),
    }


def angular_observables(
    leptons: Mapping[str, object],
    *,
    beam_direction=(0.0, 0.0, 1.0),
) -> dict[str, float | bool]:
    r"""Return fixed-flavor masses and both angular-coordinate conventions.

    For the harmonic expansion, ``Omega1=(theta1,phi1)`` uses the positive muon
    and ``Omega2=(theta2,phi2)`` uses the positron.  This function does not apply
    the Born projection; use :func:`build_level_record` for the safe combined
    operation.
    """

    candidate = build_four_lepton_candidate(leptons)
    selected = candidate.leptons
    x_system = candidate.four_lepton
    in_x_rest = {
        key: momentum.boostCM_of_p4(x_system) for key, momentum in selected.items()
    }
    z1 = in_x_rest["muon_minus"] + in_x_rest["muon_plus"]
    z2 = in_x_rest["electron_minus"] + in_x_rest["electron_plus"]

    beam = _unit(np.asarray(beam_direction, dtype=np.float64))
    frame1 = _helicity_frame(_spatial(z1), beam)
    frame2 = _helicity_frame(_spatial(z2), beam)

    theta1, phi1, cos_theta1 = _positive_lepton_angles(
        in_x_rest["muon_plus"], z1, frame1
    )
    theta2, phi2, cos_theta2 = _positive_lepton_angles(
        in_x_rest["electron_plus"], z2, frame2
    )

    output: dict[str, float | bool] = {
        "m_Z1": float(candidate.z1.mass),
        "m_Z2": float(candidate.z2.mass),
        "m_ZZ": float(x_system.mass),
        "y_ZZ": float(x_system.rapidity),
        "pt_ZZ": float(x_system.pt),
        "theta1": theta1,
        "phi1": phi1,
        "cos_theta1": cos_theta1,
        "theta2": theta2,
        "phi2": phi2,
        "cos_theta2": cos_theta2,
        "frame_degenerate": bool(frame1[3] or frame2[3]),
    }
    output.update(standard_five_angles(selected, beam_direction=beam))
    return output


def _momentum_columns(
    namespace: str,
    frame: str,
    momenta: Mapping[str, object],
) -> dict[str, float]:
    return {
        f"{namespace}_{frame}_{key}_{component}": float(getattr(momentum, component))
        for key, momentum in momenta.items()
        for component in MOMENTUM_COMPONENTS
    }


def empty_level_record(
    namespace: str,
    *,
    include_momenta: bool = True,
    topology_valid: bool = False,
) -> dict[str, float | bool]:
    """Return a schema-complete invalid-level record using NaN sentinels."""

    namespace = _validate_namespace(namespace)
    record: dict[str, float | bool] = {
        f"{namespace}_topology_valid": bool(topology_valid),
        f"{namespace}_projection_valid": False,
    }
    for field in (*PROJECTION_DIAGNOSTIC_FIELDS, *ANGULAR_NUMERIC_FIELDS):
        record[f"{namespace}_{field}"] = float("nan")
    for field in ANGULAR_BOOLEAN_FIELDS:
        record[f"{namespace}_{field}"] = False
    if include_momenta:
        for frame in ("raw", "born"):
            for key in LEPTON_KEYS:
                for component in MOMENTUM_COMPONENTS:
                    record[f"{namespace}_{frame}_{key}_{component}"] = float("nan")
    return record


def build_level_record(
    leptons: Mapping[str, object],
    namespace: str,
    *,
    include_momenta: bool = True,
    beam_direction=(0.0, 0.0, 1.0),
    check_projection: bool = True,
) -> dict[str, float | bool]:
    """Build one collision-safe raw/Born record for an analysis level.

    Typical namespaces are ``lhe``, ``dressed``, and ``reco``.  The input
    momenta are retained as ``<namespace>_raw_*`` while angles are calculated
    from a Born projection performed independently for this level.
    """

    namespace = _validate_namespace(namespace)
    candidate = build_four_lepton_candidate(leptons)
    born_leptons, diagnostics = born_project_four_leptons(
        candidate.leptons, check=check_projection
    )
    record: dict[str, float | bool] = {
        f"{namespace}_topology_valid": True,
        f"{namespace}_projection_valid": True,
    }
    record.update(
        {f"{namespace}_{key}": value for key, value in asdict(diagnostics).items()}
    )
    record.update(
        {
            f"{namespace}_{key}": value
            for key, value in angular_observables(
                born_leptons, beam_direction=beam_direction
            ).items()
        }
    )
    if include_momenta:
        record.update(_momentum_columns(namespace, "raw", candidate.leptons))
        record.update(_momentum_columns(namespace, "born", born_leptons))
    return record
