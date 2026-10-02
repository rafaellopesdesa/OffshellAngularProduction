"""Five-mode angular-density reconstruction, with correlated MC errors.

The saved truth weights are moment projectors, not additive density weights.
With dmu=dOmega1*dOmega2/(16*pi**2), the real projectors F=(1,4*pi*Y_plus)
are orthonormal.  S_i=sum(w*F_i) therefore reconstructs d sigma/dmu as
sum(S_i*F_i).  Exact bin integrals of the basis, rather than event reweighting,
give the predicted histograms.  All numerical quantities retain pb units.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import uproot

from .harmonics import TRUTH_ANGULAR_BRANCH_SLUGS, truth_angular_factors
from .plotting import RatioResult, SAMPLE_LABELS, _boolean, _numeric


COMPONENT_SLUGS = ("00_00", *TRUTH_ANGULAR_BRANCH_SLUGS)
COMPONENT_LABELS = {
    "00_00": r"$(0,0;0,0)$",
    "00_20": r"$(0,0;2,0)$",
    "20_20": r"$(2,0;2,0)$",
    "2m1_2p1": r"$(2,-1;2,1)$",
    "2m2_2p2": r"$(2,-2;2,2)$",
}
CONTEXTS = (("all", "lhe"), ("all", "dressed"),
            ("selected", "lhe"), ("selected", "dressed"), ("selected", "reco"))
ANGLE_NAMES = ("theta1", "phi1", "theta2", "phi2")


def _components(components: Sequence[str] | None) -> tuple[str, ...]:
    if components is None:
        return COMPONENT_SLUGS
    unknown = set(components) - set(COMPONENT_SLUGS)
    if unknown:
        raise ValueError(f"Unknown angular components: {', '.join(sorted(unknown))}")
    return tuple(slug for slug in COMPONENT_SLUGS if slug == "00_00" or slug in components)


@dataclass(frozen=True)
class ClosureProjection:
    name: str
    label: str
    edges: tuple[np.ndarray, ...]
    axis_labels: tuple[str, ...]
    units: tuple[str, ...]
    templates: np.ndarray

    @property
    def ndim(self) -> int:
        return len(self.edges)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(len(edges) - 1 for edges in self.edges)


def closure_projections(bins: int = 24, map_bins: int = 12) -> dict[str, ClosureProjection]:
    """Exact marginal basis integrals; map values flatten with x axis first."""
    for name, count in (("bins", bins), ("map_bins", map_bins)):
        if isinstance(count, bool) or not isinstance(count, (int, np.integer)) or count < 2:
            raise ValueError(f"{name} must be an integer of at least 2")
    specs = {}
    definitions = (
        ("theta1", r"$\theta_1$ ($\mu^+$)", "rad", 0., np.pi),
        ("phi1", r"$\phi_1$ ($\mu^+$)", "rad", -np.pi, np.pi),
        ("theta2", r"$\theta_2$ ($e^+$)", "rad", 0., np.pi),
        ("phi2", r"$\phi_2$ ($e^+$)", "rad", -np.pi, np.pi),
        ("cos_theta1", r"$\cos\theta_1$ ($\mu^+$)", "", -1., 1.),
        ("cos_theta2", r"$\cos\theta_2$ ($e^+$)", "", -1., 1.),
        ("delta_phi", r"$\Delta\phi=\mathrm{wrap}(\phi_2-\phi_1)$", "rad", -np.pi, np.pi),
        ("folded_delta_phi", r"$\Delta\phi_{\mathrm{fold}}$", "rad", -np.pi, np.pi),
    )
    for name, label, unit, low, high in definitions:
        edges = np.linspace(low, high, bins + 1)
        template = np.zeros((bins, 5))
        if name.startswith("theta") or name.startswith("cos_theta"):
            a, b = edges[:-1], edges[1:]
            if name.startswith("theta"):
                a, b = np.cos(b), np.cos(a)
            template[:, 0] = (b - a) / 2
            template[:, 1] = np.sqrt(5 / 2) * ((b**3 - b) - (a**3 - a)) / 4
        else:
            template[:, 0] = np.diff(edges) / (2 * np.pi)
            if name in ("delta_phi", "folded_delta_phi"):
                template[:, 4] = 5 * np.sqrt(2) / (24 * np.pi) * np.diff(np.sin(2 * edges))
            if name == "folded_delta_phi":
                template[:, 3] = -5 * np.sqrt(2) / (12 * np.pi) * np.diff(np.sin(edges))
        specs[name] = ClosureProjection(name, label, (edges,), (label,), (unit,), template)
    edges = np.linspace(-1, 1, map_bins + 1)
    j0, j2 = np.diff(edges) / 2, np.diff(edges**3 - edges) / 4
    template = np.zeros((map_bins, map_bins, 5))
    template[..., 0] = np.outer(j0, j0)
    template[..., 1] = np.sqrt(5 / 2) * (np.outer(j2, j0) + np.outer(j0, j2))
    template[..., 2] = 5 * np.outer(j2, j2)
    name = "cos_theta1_vs_cos_theta2"
    specs[name] = ClosureProjection(
        name, r"$\cos\theta_1$ versus $\cos\theta_2$", (edges, edges.copy()),
        (r"$\cos\theta_1$ ($\mu^+$)", r"$\cos\theta_2$ ($e^+$)"), ("", ""),
        template.reshape(-1, 5),
    )
    return specs


def angular_basis(theta1, phi1, theta2, phi2) -> np.ndarray:
    """Evaluate the five orthonormal real projectors, with component last."""
    factors = truth_angular_factors(theta1, phi1, theta2, phi2, invalid="raise")
    first = factors[TRUTH_ANGULAR_BRANCH_SLUGS[0]]
    return np.stack([np.ones_like(first), *(factors[slug] for slug in TRUTH_ANGULAR_BRANCH_SLUGS)], axis=-1)


def angular_coordinates(theta1, phi1, theta2, phi2) -> dict[str, np.ndarray]:
    """Harmonic variables, including the sign fold needed to expose m=1."""
    theta1, phi1, theta2, phi2 = np.broadcast_arrays(theta1, phi1, theta2, phi2)
    wrap = lambda value: (value + np.pi) % (2 * np.pi) - np.pi
    x, y = np.cos(theta1), np.cos(theta2)
    delta = wrap(phi2 - phi1)
    return {
        "theta1": theta1, "phi1": wrap(phi1), "theta2": theta2, "phi2": wrap(phi2),
        "cos_theta1": x, "cos_theta2": y, "delta_phi": delta,
        "folded_delta_phi": wrap(delta + np.where(x * y < 0, np.pi, 0.)),
    }


def _bin_indices(spec: ClosureProjection, coordinates: dict[str, np.ndarray]) -> np.ndarray:
    values = ((coordinates["cos_theta1"], coordinates["cos_theta2"])
              if spec.ndim == 2 else (coordinates[spec.name],))
    indices = tuple(np.clip(np.searchsorted(edges, value, side="right") - 1, 0, len(edges) - 2)
                    for edges, value in zip(spec.edges, values))
    return np.ravel_multi_index(indices, spec.shape)


def _variance_nonnegative(variance: np.ndarray, scale: np.ndarray) -> np.ndarray:
    tolerance = 128 * np.finfo(float).eps * np.maximum(scale, np.finfo(float).tiny)
    if np.any(variance < -tolerance):
        raise ValueError("Angular-closure covariance gives a negative variance")
    return np.maximum(variance, 0.)


def _correlated_ratio(n, d, vn, vd, covariance) -> RatioResult:
    n, d, vn, vd, covariance = np.broadcast_arrays(n, d, vn, vd, covariance)
    threshold = 32 * np.finfo(float).eps * np.sqrt(vd)
    valid = (np.isfinite(n) & np.isfinite(d) & np.isfinite(vn) & np.isfinite(vd)
             & np.isfinite(covariance) & (np.abs(d) > threshold))
    values, variances = np.full(n.shape, np.nan), np.full(n.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        values[valid] = n[valid] / d[valid]
        r = values[valid]
        variance = vn[valid] + r**2 * vd[valid] - 2 * r * covariance[valid]
        scale = vn[valid] + r**2 * vd[valid] + 2 * np.abs(r * covariance[valid])
        variances[valid] = _variance_nonnegative(variance, scale) / d[valid]**2
    valid &= np.isfinite(values) & np.isfinite(variances)
    values[~valid], variances[~valid] = np.nan, np.nan
    return RatioResult(values, variances, valid)


@dataclass
class ClosureHistogram:
    spec: ClosureProjection
    nominal: np.ndarray
    nominal_variances: np.ndarray
    cross_covariance: np.ndarray


@dataclass(frozen=True)
class ClosureResult:
    component_values: np.ndarray
    model: np.ndarray
    model_variances: np.ndarray
    nominal_model_covariance: np.ndarray
    residual: np.ndarray
    residual_variances: np.ndarray
    ratio: RatioResult
    pulls: np.ndarray
    pull_valid: np.ndarray


@dataclass
class ClosureContext:
    selection: str
    level: str
    projections: dict[str, ClosureHistogram]
    coefficients: np.ndarray = field(default_factory=lambda: np.zeros(5))
    covariance: np.ndarray = field(default_factory=lambda: np.zeros((5, 5)))
    total_entries: int = 0
    entries: int = 0
    excluded_entries: int = 0
    degenerate_entries: int = 0
    sumabsw_pb: float = 0.

    def accumulate(self, angles: Sequence[np.ndarray], weights: np.ndarray,
                   moment_weights: np.ndarray | None = None) -> None:
        """Accumulate already validated rows; public for numerical validation."""
        weights = np.asarray(weights, dtype=float)
        if moment_weights is None:
            moment_weights = weights[:, None] * angular_basis(*angles)
        self.entries += len(weights)
        self.sumabsw_pb += float(np.sum(np.abs(weights)))
        self.coefficients += np.sum(moment_weights, axis=0)
        self.covariance += moment_weights.T @ moment_weights
        coordinates = angular_coordinates(*angles)
        for item in self.projections.values():
            indices = _bin_indices(item.spec, coordinates)
            count = len(item.nominal)
            item.nominal += np.bincount(indices, weights=weights, minlength=count)
            item.nominal_variances += np.bincount(indices, weights=weights**2, minlength=count)
            for component in range(5):
                item.cross_covariance[:, component] += np.bincount(
                    indices, weights=weights * moment_weights[:, component], minlength=count)

    def normalized_coefficients(self) -> RatioResult:
        """S_i/S_0, accounting for the shared normalization coefficient."""
        return _correlated_ratio(self.coefficients, self.coefficients[0],
                                 np.diag(self.covariance), self.covariance[0, 0], self.covariance[:, 0])

    def evaluate(self, name: str, components: Sequence[str] | None = None) -> ClosureResult:
        item = self.projections[name]
        active = _components(components)
        template = item.spec.templates * np.array([slug in active for slug in COMPONENT_SLUGS])
        component_values = template * self.coefficients
        model = np.sum(component_values, axis=1)
        model_variances = np.einsum("bi,ij,bj->b", template, self.covariance, template)
        model_variances = np.maximum(model_variances, 0.)
        covariance = np.sum(template * item.cross_covariance, axis=1)
        residual = model - item.nominal
        scale = model_variances + item.nominal_variances + 2 * np.abs(covariance)
        residual_variances = _variance_nonnegative(
            model_variances + item.nominal_variances - 2 * covariance, scale)
        ratio = _correlated_ratio(model, item.nominal, model_variances, item.nominal_variances, covariance)
        pull_valid = residual_variances > 128 * np.finfo(float).eps * np.maximum(scale, np.finfo(float).tiny)
        pulls = np.full(model.shape, np.nan)
        pulls[pull_valid] = residual[pull_valid] / np.sqrt(residual_variances[pull_valid])
        return ClosureResult(component_values, model, model_variances, covariance,
                             residual, residual_variances, ratio, pulls, pull_valid)


def empty_context(selection: str, level: str,
                  specs: dict[str, ClosureProjection]) -> ClosureContext:
    return ClosureContext(selection, level, {
        name: ClosureHistogram(spec, np.zeros(len(spec.templates)),
                               np.zeros(len(spec.templates)), np.zeros_like(spec.templates))
        for name, spec in specs.items()
    })


@dataclass
class ClosureReport:
    input_path: Path
    projections: dict[str, ClosureProjection]
    contexts: dict[tuple[str, str], ClosureContext]
    lumi_pb: float
    components: tuple[str, ...]
    stored_lumi_pb: float | None = None
    sample_code: int = -1
    sample_label: str = "unknown"
    entries: int = 0
    selected_entries: int = 0
    sumw_pb: float = 0.
    sumw2_pb2: float = 0.


def read_closure(path: str | Path, *, lumi_pb: float | None = None,
                 step_size: str | int = "100 MB", bins: int = 24, map_bins: int = 12,
                 variables: Sequence[str] | None = None,
                 components: Sequence[str] | None = None) -> ClosureReport:
    """Read one merged local ROOT file, keeping five level/selection contexts.

    Every context uses one common finite, physical, nondegenerate angular mask
    for its nominal distribution and moments.  LHE uses and checks the stored
    truth projectors; dressed/RECO recompute moments in their own coordinates.
    RECO moments describe the selected observed density, not a forward-folded
    prediction from LHE coefficients.  'All' retains generation-level cuts.
    """
    if lumi_pb is not None and (not np.isfinite(lumi_pb) or lumi_pb <= 0):
        raise ValueError("lumi_pb must be finite and positive")
    specs = closure_projections(bins, map_bins)
    if variables is not None:
        unknown = set(variables) - set(specs)
        if unknown:
            raise ValueError(f"Unknown closure variables: {', '.join(sorted(unknown))}")
        if not variables:
            raise ValueError("At least one closure variable is required")
        specs = {name: specs[name] for name in dict.fromkeys(variables)}
    report = ClosureReport(Path(path), specs, {key: empty_context(*key, specs) for key in CONTEXTS},
                           lumi_pb or float("nan"), _components(components))
    required = {"weight_nominal_pb", "reconstructed", "sample_code", "truth_lhe_valid"}
    required.update(f"weight_truth_{slug}_pb" for slug in TRUTH_ANGULAR_BRANCH_SLUGS)
    for level in ("lhe", "dressed", "reco"):
        required.update(f"{level}_{name}" for name in (*ANGLE_NAMES, "projection_valid", "frame_degenerate"))
    with uproot.open(path, handler=uproot.source.file.MultithreadedFileSource) as root_file:
        if "Events" not in root_file:
            raise ValueError("Input is missing the merged Events tree")
        tree = root_file["Events"]
        missing = required - set(tree.keys())
        if missing:
            raise ValueError(f"Events is missing required branches: {', '.join(sorted(missing))}")
        if "lumi" in tree.keys():
            required.add("lumi")
        elif lumi_pb is None:
            raise ValueError("Events.lumi is missing; provide a luminosity override in pb^-1")
        if not tree.num_entries:
            raise ValueError("Events is empty; there are no events to plot")
        for arrays in tree.iterate(sorted(required), step_size=step_size, library="np", how=dict):
            weights = _numeric(arrays["weight_nominal_pb"], "weight_nominal_pb")
            with np.errstate(over="ignore"):
                finite_weights = np.isfinite(weights) & np.isfinite(weights**2)
            if not np.all(finite_weights):
                raise ValueError("Events.weight_nominal_pb contains nonfinite or overflowing weights")
            selected = _boolean(arrays["reconstructed"], "reconstructed")
            codes = _numeric(arrays["sample_code"], "sample_code")
            if (not np.all(np.isfinite(codes)) or not np.all(codes == np.floor(codes))
                    or not np.all(codes == codes[0])):
                raise ValueError("Events.sample_code must be one constant integer")
            code = int(codes[0])
            if report.entries and code != report.sample_code:
                raise ValueError("Events.sample_code changes between chunks")
            report.sample_code = code
            report.sample_label = SAMPLE_LABELS.get(code, f"sample {code}")
            if "lumi" in required:
                lumi = _numeric(arrays["lumi"], "lumi")
                if not np.all(np.isfinite(lumi)) or np.any(lumi <= 0) or not np.all(lumi == lumi[0]):
                    raise ValueError("Events.lumi must be finite, positive and constant")
                if report.stored_lumi_pb is not None and lumi[0] != report.stored_lumi_pb:
                    raise ValueError("Events.lumi changes between chunks")
                report.stored_lumi_pb = float(lumi[0])
                if lumi_pb is None:
                    report.lumi_pb = report.stored_lumi_pb
            report.entries += len(weights)
            report.selected_entries += int(np.count_nonzero(selected))
            report.sumw_pb += float(np.sum(weights))
            report.sumw2_pb2 += float(np.sum(weights**2))
            truth_valid = _boolean(arrays["truth_lhe_valid"], "truth_lhe_valid")
            for level in ("lhe", "dressed", "reco"):
                angles = [_numeric(arrays[f"{level}_{name}"], f"{level}_{name}") for name in ANGLE_NAMES]
                projection_valid = _boolean(arrays[f"{level}_projection_valid"], f"{level}_projection_valid")
                degenerate = _boolean(arrays[f"{level}_frame_degenerate"], f"{level}_frame_degenerate")
                valid = projection_valid & ~degenerate & np.logical_and.reduce([np.isfinite(angle) for angle in angles])
                valid &= (angles[0] >= 0) & (angles[0] <= np.pi) & (angles[2] >= 0) & (angles[2] <= np.pi)
                moment_weights = None
                if level == "lhe":
                    valid &= truth_valid
                    moment_weights = np.column_stack([weights, *(
                        _numeric(arrays[f"weight_truth_{slug}_pb"], f"weight_truth_{slug}_pb")
                        for slug in TRUTH_ANGULAR_BRANCH_SLUGS)])
                    if not np.all(np.isfinite(moment_weights[valid])):
                        raise ValueError("Stored LHE truth weights are nonfinite on valid angular rows")
                    expected = weights[valid, None] * angular_basis(*(angle[valid] for angle in angles))
                    # Relative to the event weight, so zero weights and small
                    # moments cannot hide malformed truth branches.
                    tolerance = 1e-9 * np.maximum(np.abs(weights[valid, None]), np.abs(expected))
                    if np.any(np.abs(moment_weights[valid] - expected) > tolerance):
                        raise ValueError("Stored LHE truth weights disagree with weight_nominal_pb times the angular projectors")
                for selection in (("selected",) if level == "reco" else ("all", "selected")):
                    context = report.contexts[selection, level]
                    include = selected if selection == "selected" else np.ones(len(weights), dtype=bool)
                    keep = include & valid
                    context.total_entries += int(np.count_nonzero(include))
                    context.excluded_entries += int(np.count_nonzero(include & ~valid))
                    context.degenerate_entries += int(np.count_nonzero(include & degenerate))
                    context.accumulate([angle[keep] for angle in angles], weights[keep],
                                       None if moment_weights is None else moment_weights[keep])
    for context in report.contexts.values():
        if not np.all(np.isfinite(context.coefficients)) or not np.all(np.isfinite(context.covariance)):
            raise ValueError("Angular-closure accumulated moments or variances overflowed")
    return report
