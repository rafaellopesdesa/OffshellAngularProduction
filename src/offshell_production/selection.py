"""Raw-momentum RECO selection for the off-shell ``2e2mu`` analysis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations, pairwise

import numpy as np

from .kinematics import LEPTON_KEYS, build_four_lepton_candidate, wrap_to_pi

PAIR_KEYS = tuple(combinations(LEPTON_KEYS, 2))


@dataclass(frozen=True)
class OffshellSelectionConfig:
    """Configurable RECO selection thresholds, in GeV where applicable."""

    min_lepton_pt: float = 5.0
    max_abs_lepton_eta: float = 2.5
    ordered_pt_thresholds: tuple[float, float, float] = (20.0, 15.0, 10.0)
    min_dilepton_mass: float = 50.0
    max_dilepton_mass: float = 106.0
    min_delta_r: float = 0.1
    min_m4l: float = 180.0
    max_m4l: float | None = None

    def __post_init__(self):
        numeric = {
            "min_lepton_pt": self.min_lepton_pt,
            "max_abs_lepton_eta": self.max_abs_lepton_eta,
            "min_dilepton_mass": self.min_dilepton_mass,
            "max_dilepton_mass": self.max_dilepton_mass,
            "min_delta_r": self.min_delta_r,
            "min_m4l": self.min_m4l,
        }
        if any(not np.isfinite(float(value)) for value in numeric.values()):
            raise ValueError("selection thresholds must be finite")
        if any(float(value) < 0.0 for value in numeric.values()):
            raise ValueError("selection thresholds must be non-negative")
        if self.max_abs_lepton_eta <= 0.0:
            raise ValueError("max_abs_lepton_eta must be positive")
        if self.min_dilepton_mass >= self.max_dilepton_mass:
            raise ValueError("dilepton mass bounds must be strictly ordered")
        if self.max_m4l is not None:
            if not np.isfinite(float(self.max_m4l)):
                raise ValueError("max_m4l must be finite or None")
            if self.max_m4l <= self.min_m4l:
                raise ValueError("max_m4l must exceed min_m4l")

        thresholds = tuple(float(value) for value in self.ordered_pt_thresholds)
        if len(thresholds) != 3:
            raise ValueError("ordered_pt_thresholds must contain three values")
        if any(not np.isfinite(value) or value < 0.0 for value in thresholds):
            raise ValueError("ordered pT thresholds must be finite and non-negative")
        if any(left < right for left, right in pairwise(thresholds)):
            raise ValueError("ordered pT thresholds must be non-increasing")
        object.__setattr__(self, "ordered_pt_thresholds", thresholds)


@dataclass(frozen=True)
class RecoSelectionResult:
    """RECO cut values and masks for one raw four-lepton candidate."""

    candidate: bool
    lepton_pt: Mapping[str, float]
    lepton_abs_eta: Mapping[str, float]
    ordered_pt: tuple[float, float, float, float]
    dilepton_masses: Mapping[str, float]
    pair_delta_r: Mapping[tuple[str, str], float]
    m4l: float
    flags: Mapping[str, bool]
    passed: bool

    def to_record(self, namespace: str = "reco") -> dict[str, float | bool]:
        """Flatten the diagnostics and masks under an explicit namespace."""

        prefix = f"{namespace}_"
        record: dict[str, float | bool] = {
            f"{prefix}candidate": bool(self.candidate),
            f"{prefix}m4l_for_selection": float(self.m4l),
            f"{prefix}pass_selection": bool(self.passed),
        }
        for key in LEPTON_KEYS:
            record[f"{prefix}{key}_pt_for_selection"] = float(self.lepton_pt[key])
            record[f"{prefix}{key}_abs_eta_for_selection"] = float(
                self.lepton_abs_eta[key]
            )
        for index, value in enumerate(self.ordered_pt, start=1):
            record[f"{prefix}ordered_pt{index}"] = float(value)
        record[f"{prefix}m_Z1_for_selection"] = float(self.dilepton_masses["Z1"])
        record[f"{prefix}m_Z2_for_selection"] = float(self.dilepton_masses["Z2"])
        for (left, right), value in self.pair_delta_r.items():
            record[f"{prefix}delta_r_{left}_{right}"] = float(value)
        record.update(
            {f"{prefix}cut_{name}": bool(value) for name, value in self.flags.items()}
        )
        return record


def _delta_r(left, right) -> float:
    delta_eta = float(left.eta - right.eta)
    delta_phi = wrap_to_pi(float(left.phi - right.phi))
    return float(np.hypot(delta_eta, delta_phi))


def evaluate_reco_selection(
    raw_leptons: Mapping[str, object],
    config: OffshellSelectionConfig | None = None,
) -> RecoSelectionResult:
    """Evaluate the off-shell selection on unprojected RECO momenta.

    All comparisons are strict: every lepton has ``pT > 5 GeV`` and
    ``abs(eta) < 2.5``; the three leading ordered transverse momenta exceed
    ``20, 15, 10 GeV``; both fixed-flavor dilepton masses satisfy
    ``50 < mll < 106 GeV``; every lepton pair has ``DeltaR > 0.1``; and the
    four-lepton mass satisfies the strict lower requirement ``m4l > 180 GeV``.
    There is no upper RECO mass cut by default; ``max_m4l`` is an explicitly
    opt-in phase-space guard and should not normally define reconstruction.
    """

    config = config or OffshellSelectionConfig()
    candidate = build_four_lepton_candidate(raw_leptons)

    lepton_pt = {key: float(momentum.pt) for key, momentum in candidate.leptons.items()}
    lepton_abs_eta = {
        key: abs(float(momentum.eta)) for key, momentum in candidate.leptons.items()
    }
    ordered_pt = tuple(sorted(lepton_pt.values(), reverse=True))
    dilepton_masses = {
        "Z1": float(candidate.z1.mass),
        "Z2": float(candidate.z2.mass),
    }
    pair_delta_r = {
        pair: _delta_r(candidate.leptons[pair[0]], candidate.leptons[pair[1]])
        for pair in PAIR_KEYS
    }
    m4l = float(candidate.four_lepton.mass)

    finite_kinematics = bool(
        np.all(
            np.isfinite(
                [
                    *lepton_pt.values(),
                    *lepton_abs_eta.values(),
                    *dilepton_masses.values(),
                    *pair_delta_r.values(),
                    m4l,
                ]
            )
        )
    )
    flags = {
        "finite_kinematics": finite_kinematics,
        "all_lepton_pt": all(
            value > config.min_lepton_pt for value in lepton_pt.values()
        ),
        "all_lepton_eta": all(
            value < config.max_abs_lepton_eta for value in lepton_abs_eta.values()
        ),
        "ordered_pt1": ordered_pt[0] > config.ordered_pt_thresholds[0],
        "ordered_pt2": ordered_pt[1] > config.ordered_pt_thresholds[1],
        "ordered_pt3": ordered_pt[2] > config.ordered_pt_thresholds[2],
        "Z1_mass_window": (
            config.min_dilepton_mass < dilepton_masses["Z1"] < config.max_dilepton_mass
        ),
        "Z2_mass_window": (
            config.min_dilepton_mass < dilepton_masses["Z2"] < config.max_dilepton_mass
        ),
        "all_pair_delta_r": all(
            value > config.min_delta_r for value in pair_delta_r.values()
        ),
        "m4l_lower": m4l > config.min_m4l,
        "m4l_upper": config.max_m4l is None or m4l < config.max_m4l,
    }
    passed = bool(all(flags.values()))
    return RecoSelectionResult(
        candidate=True,
        lepton_pt=lepton_pt,
        lepton_abs_eta=lepton_abs_eta,
        ordered_pt=ordered_pt,
        dilepton_masses=dilepton_masses,
        pair_delta_r=pair_delta_r,
        m4l=m4l,
        flags=flags,
        passed=passed,
    )


def empty_reco_selection_result() -> RecoSelectionResult:
    """Return the selection result for an unavailable RECO candidate."""

    nan = float("nan")
    flags = {
        "finite_kinematics": False,
        "all_lepton_pt": False,
        "all_lepton_eta": False,
        "ordered_pt1": False,
        "ordered_pt2": False,
        "ordered_pt3": False,
        "Z1_mass_window": False,
        "Z2_mass_window": False,
        "all_pair_delta_r": False,
        "m4l_lower": False,
        "m4l_upper": False,
    }
    return RecoSelectionResult(
        candidate=False,
        lepton_pt={key: nan for key in LEPTON_KEYS},
        lepton_abs_eta={key: nan for key in LEPTON_KEYS},
        ordered_pt=(nan, nan, nan, nan),
        dilepton_masses={"Z1": nan, "Z2": nan},
        pair_delta_r={pair: nan for pair in PAIR_KEYS},
        m4l=nan,
        flags=flags,
        passed=False,
    )
