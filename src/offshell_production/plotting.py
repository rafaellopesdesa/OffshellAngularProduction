"""Signed-weight histograms for the merged analysis output.

Histogram contents and variances retain pb and pb**2 units.  Luminosity is
carried separately so rendering cannot accidentally normalize each level to a
different yield.  The efficiency numerator is an exact subset of its own
level's denominator, including identical validity masks and flow folding.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import hist
import numpy as np
import uproot
import vector

from .kinematics import (
    ANGULAR_NUMERIC_FIELDS,
    LEPTON_KEYS,
    MOMENTUM_COMPONENTS,
    PROJECTION_DIAGNOSTIC_FIELDS,
)
from .selection import PAIR_KEYS

LEVELS = ("lhe", "dressed", "reco")
SAMPLE_LABELS = {
    0: "gg4l", 1: "qqZZ", 2: "gg4l_h", 3: "gg4l_b",
    10: "vpolar_LL", 11: "vpolar_TT", 12: "vpolar_TL", 13: "vpolar_LT",
}


@dataclass(frozen=True)
class ObservableSpec:
    name: str
    label: str
    unit: str
    edges: np.ndarray
    validity: str
    degeneracy_flag: str | None = None
    category: str = "kinematics"

    @property
    def axis_label(self) -> str:
        return f"{self.label} [{self.unit}]" if self.unit else self.label


@dataclass
class HistogramCounters:
    total: int = 0
    filled: int = 0
    invalid: int = 0
    underflow: int = 0
    overflow: int = 0
    sumw_pb: float = 0.0
    sumw2_pb2: float = 0.0
    underflow_sumw_pb: float = 0.0
    overflow_sumw_pb: float = 0.0


@dataclass
class ReportData:
    input_path: Path
    observables: dict[str, ObservableSpec]
    histograms: dict[tuple[str, str, str], hist.Hist]
    counters: dict[tuple[str, str, str], HistogramCounters]
    lumi_pb: float
    sample_code: int
    sample_label: str
    entries: int = 0
    selected_entries: int = 0
    sumw_pb: float = 0.0
    sumw2_pb2: float = 0.0
    selected_sumw_pb: float = 0.0
    selected_sumw2_pb2: float = 0.0
    sumabsw_pb: float = 0.0
    stored_lumi_pb: float | None = None


@dataclass(frozen=True)
class RatioResult:
    values: np.ndarray
    variances: np.ndarray
    valid: np.ndarray

    @property
    def errors(self) -> np.ndarray:
        return np.sqrt(self.variances)


def observable_specs(
    bin_edges: Mapping[str, Sequence[float]] | None = None,
) -> dict[str, ObservableSpec]:
    """All numeric kinematics, with common bins at every level.

    The 56 level-record fields are supplemented by 21 raw selection diagnostics
    stored at RECO and calculated from the raw momenta at LHE/dressed level.

    Values beyond the displayed ranges are folded into the boundary bins; the
    report counters retain both their event counts and their signed weights.
    """
    definitions = {
        "m_Z1": (r"$m_{\mu\mu}$", "GeV", 40, 200, 80),
        "m_Z2": (r"$m_{ee}$", "GeV", 40, 200, 80),
        "m_ZZ": (r"$m_{4\ell}$ (Born)", "GeV", 150, 1500, 54),
        "y_ZZ": (r"$y_{4\ell}$ (Born)", "", -5, 5, 50),
        "pt_ZZ": (r"$p_{\mathrm{T},4\ell}$ (Born)", "GeV", 0, 5, 25),
        "theta1": (r"$\theta_1$ (harmonic, $\mu^+$)", "rad", 0, np.pi, 32),
        "phi1": (r"$\phi_1$ (harmonic, $\mu^+$)", "rad", -np.pi, np.pi, 32),
        "cos_theta1": (r"$\cos\theta_1$ (harmonic, $\mu^+$)", "", -1, 1, 40),
        "theta2": (r"$\theta_2$ (harmonic, $e^+$)", "rad", 0, np.pi, 32),
        "phi2": (r"$\phi_2$ (harmonic, $e^+$)", "rad", -np.pi, np.pi, 32),
        "cos_theta2": (r"$\cos\theta_2$ (harmonic, $e^+$)", "", -1, 1, 40),
        "cos_theta_star": (r"$\cos\theta^*$", "", -1, 1, 40),
        "abs_cos_theta_star": (r"$|\cos\theta^*|$", "", 0, 1, 40),
        "theta1_standard": (r"$\theta_1$ (standard, $\mu^-$)", "rad", 0, np.pi, 32),
        "theta2_standard": (r"$\theta_2$ (standard, $e^-$)", "rad", 0, np.pi, 32),
        "Phi": (r"$\Phi$ (standard)", "rad", -np.pi, np.pi, 32),
        "Phi1": (r"$\Phi_1$ (standard)", "rad", -np.pi, np.pi, 32),
        "Psi": (r"$\Psi$ (standard)", "rad", -np.pi, np.pi, 32),
    }
    specs = {}
    for name in ANGULAR_NUMERIC_FIELDS:
        label, unit, low, high, bins = definitions[name]
        flag = "frame_degenerate" if name in ("phi1", "phi2") else None
        if name in ("Phi1", "Psi"):
            flag = "standard_angles_degenerate"
        specs[name] = ObservableSpec(
            name, label, unit, np.linspace(low, high, bins + 1),
            "projection_valid", flag,
        )
    for name in PROJECTION_DIAGNOSTIC_FIELDS:
        frame, quantity = name.split("_", 1)
        label, unit, low, high, bins = {
            "m4l": (r"$m_{4\ell}$", "GeV", 150, 1500, 54),
            "y4l": (r"$y_{4\ell}$", "", -5, 5, 50),
            "pt4l": (r"$p_{\mathrm{T},4\ell}$", "GeV", 0, 400, 40),
        }[quantity]
        if frame == "born" and quantity == "pt4l":
            high, bins = 5, 25
        specs[name] = ObservableSpec(
            name, f"{label} ({frame.capitalize()})", unit,
            np.linspace(low, high, bins + 1),
            "topology_valid" if frame == "raw" else "projection_valid",
            category="projection",
        )
    lepton_labels = {
        "electron_minus": r"e^-", "electron_plus": r"e^+",
        "muon_minus": r"\mu^-", "muon_plus": r"\mu^+",
    }
    for frame in ("raw", "born"):
        for lepton in LEPTON_KEYS:
            for component in MOMENTUM_COMPONENTS:
                name = f"{frame}_{lepton}_{component}"
                axis = "E" if component == "E" else f"p_{component[-1]}"
                low, high, bins = {
                    "px": (-300, 300, 60), "py": (-300, 300, 60),
                    "pz": (-1000, 1000, 50), "E": (0, 1000, 50),
                }[component]
                specs[name] = ObservableSpec(
                    name, rf"${axis}({lepton_labels[lepton]})$ ({frame.capitalize()})",
                    "GeV", np.linspace(low, high, bins + 1),
                    "topology_valid" if frame == "raw" else "projection_valid",
                    category="momenta",
                )
    selection_definitions = {
        "m4l_for_selection": (r"$m_{4\ell}$ (raw)", "GeV", 150, 1500, 54),
        "m_Z1_for_selection": (r"$m_{\mu\mu}$ (raw)", "GeV", 40, 200, 80),
        "m_Z2_for_selection": (r"$m_{ee}$ (raw)", "GeV", 40, 200, 80),
    }
    for lepton in LEPTON_KEYS:
        symbol = lepton_labels[lepton]
        selection_definitions[f"{lepton}_pt_for_selection"] = (
            rf"$p_{{\mathrm{{T}}}}({symbol})$ (raw)", "GeV", 0, 300, 60)
        selection_definitions[f"{lepton}_abs_eta_for_selection"] = (
            rf"$|\eta({symbol})|$ (raw)", "", 0, 5, 50)
    for index in range(1, 5):
        selection_definitions[f"ordered_pt{index}"] = (
            rf"$p_{{\mathrm{{T}},{index}}}$ (raw, ordered)", "GeV", 0, 300, 60)
    for left, right in PAIR_KEYS:
        selection_definitions[f"delta_r_{left}_{right}"] = (
            rf"$\Delta R({lepton_labels[left]}, {lepton_labels[right]})$ (raw)",
            "", 0, 6, 60)
    for name, (label, unit, low, high, bins) in selection_definitions.items():
        specs[name] = ObservableSpec(
            name, label, unit, np.linspace(low, high, bins + 1),
            "topology_valid", category="selection",
        )
    for name, edges in (bin_edges or {}).items():
        if name not in specs:
            raise ValueError(f"Unknown observable for bin override: {name}")
        values = np.asarray(edges, dtype=np.float64)
        if (values.ndim != 1 or len(values) < 2 or not np.all(np.isfinite(values))
                or not np.all(np.diff(values) > 0)):
            raise ValueError(f"Bin edges for {name} must be finite and strictly increasing")
        old = specs[name]
        specs[name] = ObservableSpec(
            name, old.label, old.unit, values.copy(), old.validity,
            old.degeneracy_flag, old.category,
        )
    return specs


def subset_ratio(numerator: hist.Hist, denominator: hist.Hist) -> RatioResult:
    """Subset efficiency with covariance Cov(N,D)=Var(N), including signs.

    The nonnegative form of the variance is
    ``[VN*(1-r)**2 + (VD-VN)*r**2] / D**2``.  Negative denominators and ratios
    outside [0,1] are retained.  Cancellation below floating-point precision
    (32*eps*sqrt(VD)) is masked, as are zero/nonfinite denominators.
    """
    if not np.array_equal(numerator.axes[0].edges, denominator.axes[0].edges):
        raise ValueError("Efficiency numerator and denominator must have identical bins")
    n, d = numerator.values(), denominator.values()
    vn, vd = numerator.variances(), denominator.variances()
    if vn is None or vd is None:
        raise ValueError("Efficiency requires histograms with weight variances")
    complement = vd - vn
    tolerance = 32 * np.finfo(float).eps * np.maximum(vd, vn)
    if np.any(complement < -tolerance):
        raise ValueError("Numerator sumw2 exceeds denominator: not a subset")
    complement = np.maximum(complement, 0.0)
    threshold = 32 * np.finfo(float).eps * np.sqrt(vd)
    valid = (np.isfinite(n) & np.isfinite(d) & np.isfinite(vn) & np.isfinite(vd)
             & (np.abs(d) > threshold))
    values = np.full(n.shape, np.nan, dtype=float)
    variances = np.full(n.shape, np.nan, dtype=float)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        values[valid] = n[valid] / d[valid]
        r = values[valid]
        variances[valid] = (vn[valid] * (1 - r)**2 + complement[valid] * r**2) / d[valid]**2
    valid &= np.isfinite(values) & np.isfinite(variances)
    values[~valid], variances[~valid] = np.nan, np.nan
    return RatioResult(values, variances, valid)


def _boolean(values: np.ndarray, name: str) -> np.ndarray:
    values = np.asarray(values)
    if values.ndim != 1 or not np.all((values == 0) | (values == 1)):
        raise ValueError(f"Events.{name} must contain scalar booleans")
    return values.astype(bool, copy=False)


def _numeric(values: np.ndarray, name: str) -> np.ndarray:
    values = np.asarray(values)
    if values.ndim != 1 or values.dtype.kind not in "fiu":
        raise ValueError(f"Events.{name} must contain scalar real numbers")
    return values.astype(np.float64, copy=False)


def _raw_selection_observables(
    arrays: Mapping[str, np.ndarray], level: str,
) -> dict[str, np.ndarray]:
    """Vectorized truth counterparts of ``RecoSelectionResult.to_record``.

    This calculates kinematic values only.  It does not impose RECO cuts on the
    truth denominator, and it uses unprojected momenta throughout.
    """
    leptons = {}
    for lepton in LEPTON_KEYS:
        components = {}
        for component in MOMENTUM_COMPONENTS:
            branch = f"{level}_raw_{lepton}_{component}"
            components[component] = _numeric(arrays[branch], branch)
        leptons[lepton] = vector.array(components)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        z1 = leptons["muon_minus"] + leptons["muon_plus"]
        z2 = leptons["electron_minus"] + leptons["electron_plus"]
        values = {
            "m4l_for_selection": np.asarray((z1 + z2).mass),
            "m_Z1_for_selection": np.asarray(z1.mass),
            "m_Z2_for_selection": np.asarray(z2.mass),
        }
        for lepton, momentum in leptons.items():
            values[f"{lepton}_pt_for_selection"] = np.asarray(momentum.pt)
            values[f"{lepton}_abs_eta_for_selection"] = np.abs(momentum.eta)
        ordered_pt = np.sort(np.column_stack([momentum.pt for momentum in leptons.values()]), axis=1)[:, ::-1]
        for index in range(1, 5):
            values[f"ordered_pt{index}"] = ordered_pt[:, index - 1]
        for left, right in PAIR_KEYS:
            a, b = leptons[left], leptons[right]
            delta_phi = (a.phi - b.phi + np.pi) % (2 * np.pi) - np.pi
            values[f"delta_r_{left}_{right}"] = np.hypot(a.eta - b.eta, delta_phi)
    return values


def read_report(
    path: str | Path,
    *,
    lumi_pb: float | None = None,
    step_size: str | int = "100 MB",
    bin_edges: Mapping[str, Sequence[float]] | None = None,
    variables: Sequence[str] | None = None,
) -> ReportData:
    """Read one merged ROOT file without requiring all events in memory.

    ``lumi_pb`` is an optional plotting-luminosity override. Otherwise a positive
    constant ``Events.lumi`` is required. Unnormalized per-job files are rejected
    by the mandatory ``weight_nominal_pb`` branch.
    """
    if lumi_pb is not None and (not np.isfinite(lumi_pb) or lumi_pb <= 0):
        raise ValueError("lumi_pb must be finite and positive")
    specs = observable_specs(bin_edges)
    if variables is not None:
        if not variables:
            raise ValueError("At least one observable is required")
        unknown = set(variables) - set(specs)
        if unknown:
            raise ValueError(f"Unknown observables: {', '.join(sorted(unknown))}")
        specs = {name: specs[name] for name in dict.fromkeys(variables)}
    histogram_map = {}
    counters = {}
    for selection, levels in (("all", ("lhe", "dressed")), ("selected", LEVELS)):
        for level in levels:
            for name, spec in specs.items():
                key = (selection, level, name)
                histogram_map[key] = hist.Hist(
                    hist.axis.Variable(spec.edges, name=name, label=spec.axis_label,
                                       underflow=False, overflow=False),
                    storage=hist.storage.Weight(),
                )
                counters[key] = HistogramCounters()
    result = ReportData(Path(path), specs, histogram_map, counters,
                        lumi_pb or float("nan"), -1, "unknown")
    required = {"weight_nominal_pb", "reconstructed", "sample_code"}
    needs_derived = any(spec.category == "selection" for spec in specs.values())
    for level in LEVELS:
        for spec in specs.values():
            if level != "reco" and spec.category == "selection":
                required.update(
                    f"{level}_raw_{lepton}_{component}"
                    for lepton in LEPTON_KEYS for component in MOMENTUM_COMPONENTS
                )
            else:
                required.add(f"{level}_{spec.name}")
            required.add(f"{level}_{spec.validity}")
            if spec.degeneracy_flag:
                required.add(f"{level}_{spec.degeneracy_flag}")
    # The command accepts local files (including mounted storage).  Use uproot's
    # local-file source directly rather than introducing an asynchronous fsspec
    # event loop for local reads.
    with uproot.open(path, handler=uproot.source.file.MultithreadedFileSource) as root_file:
        if "Events" not in root_file:
            raise ValueError("Input is missing the merged Events tree")
        tree = root_file["Events"]
        available = set(tree.keys())
        missing = required - available
        if missing:
            raise ValueError(f"Events is missing required branches: {', '.join(sorted(missing))}")
        if "lumi" in available:
            required.add("lumi")
        elif lumi_pb is None:
            raise ValueError("Events.lumi is missing; provide a luminosity override in pb^-1")
        if not tree.num_entries:
            raise ValueError("Events is empty; there are no events to plot")
        for arrays in tree.iterate(sorted(required), step_size=step_size, library="np", how=dict):
            weight = _numeric(arrays["weight_nominal_pb"], "weight_nominal_pb")
            if not np.all(np.isfinite(weight)) or not np.all(np.isfinite(weight * weight)):
                raise ValueError("Events.weight_nominal_pb contains nonfinite or overflowing weights")
            selected = _boolean(arrays["reconstructed"], "reconstructed")
            codes = _numeric(arrays["sample_code"], "sample_code")
            if (not np.all(np.isfinite(codes)) or not np.all(codes == codes.astype(np.int64))
                    or not np.all(codes == codes[0])):
                raise ValueError("Events.sample_code must be one constant integer")
            code = int(codes[0])
            if result.entries and code != result.sample_code:
                raise ValueError("Events.sample_code changes between chunks")
            result.sample_code = code
            result.sample_label = SAMPLE_LABELS.get(code, f"sample {code}")
            if "lumi" in required:
                stored_lumi = _numeric(arrays["lumi"], "lumi")
                if (not np.all(np.isfinite(stored_lumi)) or np.any(stored_lumi <= 0)
                        or not np.all(stored_lumi == stored_lumi[0])):
                    raise ValueError("Events.lumi must be finite, positive and constant")
                if result.stored_lumi_pb is not None and stored_lumi[0] != result.stored_lumi_pb:
                    raise ValueError("Events.lumi changes between chunks")
                result.stored_lumi_pb = float(stored_lumi[0])
                if lumi_pb is None:
                    result.lumi_pb = result.stored_lumi_pb
            result.entries += len(weight)
            result.selected_entries += int(np.count_nonzero(selected))
            result.sumw_pb += float(np.sum(weight))
            result.sumw2_pb2 += float(np.sum(weight**2))
            result.sumabsw_pb += float(np.sum(np.abs(weight)))
            result.selected_sumw_pb += float(np.sum(weight[selected]))
            result.selected_sumw2_pb2 += float(np.sum(weight[selected]**2))
            for level in LEVELS:
                derived = _raw_selection_observables(arrays, level) if needs_derived and level != "reco" else {}
                for name, spec in specs.items():
                    values = derived[name] if name in derived else _numeric(arrays[f"{level}_{name}"], f"{level}_{name}")
                    flag = f"{level}_{spec.validity}"
                    valid = _boolean(arrays[flag], flag) & np.isfinite(values)
                    if spec.degeneracy_flag:
                        flag = f"{level}_{spec.degeneracy_flag}"
                        valid &= ~_boolean(arrays[flag], flag)
                    for selection in (("selected",) if level == "reco" else ("all", "selected")):
                        key = (selection, level, name)
                        include = selected if selection == "selected" else np.ones(len(weight), dtype=bool)
                        mask = include & valid
                        chosen, weights = values[mask], weight[mask]
                        low, high = spec.edges[0], spec.edges[-1]
                        # The upper edge belongs to the final displayed bin;
                        # strictly out-of-range values alone count as flows.
                        under, over = chosen < low, chosen > high
                        counts = counters[key]
                        counts.total += int(np.count_nonzero(include))
                        counts.filled += len(chosen)
                        counts.invalid += int(np.count_nonzero(include & ~valid))
                        counts.underflow += int(np.count_nonzero(under))
                        counts.overflow += int(np.count_nonzero(over))
                        counts.sumw_pb += float(np.sum(weights))
                        counts.sumw2_pb2 += float(np.sum(weights**2))
                        counts.underflow_sumw_pb += float(np.sum(weights[under]))
                        counts.overflow_sumw_pb += float(np.sum(weights[over]))
                        folded = np.clip(chosen, low, np.nextafter(high, low))
                        histogram_map[key].fill(folded, weight=weights)
    return result
