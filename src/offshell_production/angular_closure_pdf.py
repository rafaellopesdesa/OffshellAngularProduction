"""ATLAS-style PDF diagnostics for the five-mode angular reconstruction."""

from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile
import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import Normalize, TwoSlopeNorm
from matplotlib.lines import Line2D
import hist
import mplhep as hep
import numpy as np

from .angular_closure import COMPONENT_SLUGS, ClosureReport


TERM_LABELS = (r"$00,00$", r"$00,20$", r"$20,20$", r"$2{-}1,21$", r"$2{-}2,22$")
TERM_COLORS = ("#777777", "#0072B2", "#D55E00", "#CC79A7", "#7B3F00")
MODEL_COLOR = "#00836A"


def _context_label(key) -> str:
    selection, level = key
    return f"{'All generated events' if selection == 'all' else 'RECO-selected events'} / {level.upper() if level != 'dressed' else 'Dressed'}"


def _header(fig, report, title: str, page: int) -> None:
    fig.text(0.08, 0.965, "Simulation / Delphes", fontsize=16, fontweight="bold")
    fig.text(0.08, 0.931, title, fontsize=12)
    fig.text(0.95, 0.965, rf"$\sqrt{{s}}=13.6$ TeV, ${report.lumi_pb / 1000:g}\;\mathrm{{fb}}^{{-1}}$",
             fontsize=12, ha="right")
    fig.text(0.95, 0.931, report.sample_label, fontsize=12, ha="right")
    fig.text(0.08, 0.025, "OffshellAngularProduction | ATLAS-style angular closure", fontsize=8, color="0.35")
    fig.text(0.95, 0.025, str(page), fontsize=9, ha="right", color="0.35")


def _cover(report, pages):
    fig = plt.figure(figsize=(11.7, 8.3))
    _header(fig, report, "Angular closure: nominal sample versus retained-mode reconstruction", 1)
    y = 0.84
    paragraphs = [
        (f"Input: {report.input_path.name}", 12),
        (f"{report.entries:,} event rows; {report.selected_entries:,} RECO-selected rows; {pages} PDF pages.", 12),
        ("Retained terms: " + ", ".join(report.components), 12),
        ("The stored truth weights are moment projectors. Sum them to estimate coefficients, then multiply each coefficient by the integrated angular basis to reconstruct a distribution. Their event histograms do not form an additive decomposition.", 11),
        (r"$F_0=1,\quad F_i=4\pi\,\mathrm{Re}\,\mathcal{Y}^{(+)*}_i,\quad S_i=\sum_e w_eF_i(\Omega_e)$", 14),
        (r"$d\sigma/d\mu\simeq\sum_i S_iF_i,\quad d\mu=d\Omega_1d\Omega_2/(16\pi^2)$", 14),
        ("Each context estimates its own coefficients from the same valid events as its nominal histograms. LHE uses the stored truth weights; dressed and RECO moments use their own harmonic angles. Selected-level closure tests the truncated selected angular density; it is not a detector-folded prediction from inclusive LHE coefficients.", 11),
        ("The constant term fixes the integral. Shape differences, not integral agreement, test whether the retained modes suffice. Generator cuts, RECO acceptance and flavor asymmetry can produce omitted modes. This is a same-sample consistency test, not independent validation.", 11),
        ("The cosine plane tests the 20,20 correlation. Ordinary relative azimuth hides the 2-1,21 term; the sign-folded relative azimuth exposes it. Individual phi marginals are flat within this basis. All angles follow the positive-lepton harmonic convention (mu+ for system 1, e+ for system 2).", 11),
        ("No RECO selection is applied in the all-event contexts; inherited generator cuts remain. One common finite, nondegenerate-angle mask is used for every term and nominal comparison within each context. Excluded rows are counted.", 10),
        ("Signed contributions and negative predictions remain visible. Bands show finite-MC moment errors. Model/nominal ratios and residual pulls include their same-event covariance; undefined denominators are omitted. Cross-section normalization, theory and detector systematic uncertainties are not included.", 10),
    ]
    for paragraph, size in paragraphs:
        lines = [paragraph] if paragraph.startswith("$") else textwrap.wrap(paragraph, 128 if size <= 11 else 110)
        for line in lines:
            fig.text(0.08, y, line, fontsize=size)
            y -= 0.026 if size <= 11 else 0.034
        y -= 0.015
    return fig


def _moments_page(report):
    fig = plt.figure(figsize=(11.7, 8.3))
    _header(fig, report, "Measured angular moments in each comparison sample", 2)
    rows = []
    count_rows = []
    for key, context in report.contexts.items():
        normalized = context.normalized_coefficients()
        values = [f"{context.coefficients[0]:.5g}\n+/- {math.sqrt(context.covariance[0, 0]):.2g}"]
        for index in range(1, 5):
            values.append(f"{normalized.values[index]:.4g}\n+/- {math.sqrt(normalized.variances[index]):.2g}"
                          if normalized.valid[index] else "undefined")
        rows.append([_context_label(key), *values])
        count_rows.append([_context_label(key), f"{context.total_entries:,}", f"{context.entries:,}",
                           f"{context.excluded_entries:,}", f"{context.degenerate_entries:,}"])
    ax = fig.add_axes([0.04, 0.52, 0.94, 0.30])
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=["Context", r"$S_{00,00}$ [pb]", *TERM_LABELS[1:]],
                     colWidths=[0.30, 0.14, 0.14, 0.14, 0.14, 0.14], loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    table.scale(1, 2.7)
    # All nonconstant columns are normalized moments, not additive rate fractions.
    fig.text(0.08, 0.84, "Nonconstant columns show Si / S00,00 with correlated MC errors; all five moments are measured.", fontsize=10)
    ax2 = fig.add_axes([0.06, 0.20, 0.90, 0.23])
    ax2.axis("off")
    counts = ax2.table(cellText=count_rows, colLabels=["Context", "Rows", "Valid", "Excluded", "Degenerate"],
                       colWidths=[0.40, 0.15, 0.15, 0.15, 0.15], loc="center", cellLoc="center")
    counts.auto_set_font_size(False)
    counts.set_fontsize(9)
    counts.scale(1, 1.9)
    for table in (table, counts):
        for (row, _), cell in table.get_celld().items():
            cell.set_edgecolor("0.8")
            if row == 0:
                cell.set_facecolor("0.93")
                cell.set_text_props(fontweight="bold")
    fig.text(0.08, 0.10, "Degenerate rows are included in the excluded count. Moments are refitted separately in every context.", fontsize=9)
    fig.text(0.08, 0.075, "The selected component sum always includes 00,00; unselected nonconstant terms are omitted from the prediction.", fontsize=9)
    return fig


def _legend(fig, components):
    handles = [Line2D([], [], color="black", marker="o", linestyle="none", label="Nominal", markersize=4),
               Line2D([], [], color=MODEL_COLOR, linewidth=2, label="Retained sum")]
    handles.extend(Line2D([], [], color=TERM_COLORS[i], linestyle="--", label=TERM_LABELS[i])
                   for i, slug in enumerate(COMPONENT_SLUGS) if slug in components)
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.53, 0.888), ncol=7, frameon=False, fontsize=10)


def _one_dimensional_page(report, key, names, page):
    context = report.contexts[key]
    fig = plt.figure(figsize=(11.7, 8.3))
    grid = fig.add_gridspec(2, 2, height_ratios=[3, 1], left=0.09, right=0.965,
                           bottom=0.18, top=0.80, hspace=0.10, wspace=0.30)
    _header(fig, report, _context_label(key), page)
    _legend(fig, report.components)
    for column, name in enumerate(names):
        item = context.projections[name]
        spec = item.spec
        result = context.evaluate(name, components=report.components)
        edges = spec.edges[0]
        widths = np.diff(edges)
        scale = report.lumi_pb / widths
        centers = (edges[1:] + edges[:-1]) / 2
        ax = fig.add_subplot(grid[0, column])
        ratio_ax = fig.add_subplot(grid[1, column], sharex=ax)
        ax.tick_params(labelbottom=False)
        nominal, nominal_error = item.nominal * scale, np.sqrt(item.nominal_variances) * scale
        model, model_error = result.model * scale, np.sqrt(result.model_variances) * scale
        model_hist = hist.Hist(hist.axis.Variable(edges), storage=hist.storage.Weight())
        model_hist.view().value[...] = model
        model_hist.view().variance[...] = model_error**2
        hep.histplot(model_hist, ax=ax, histtype="step", yerr=False, w2method="sqrt", color=MODEL_COLOR, linewidth=2)
        ax.set_xlabel("")  # mplhep supplies a default label; the ratio axis owns it.
        ax.fill_between(edges, np.r_[model-model_error, (model-model_error)[-1]],
                        np.r_[model+model_error, (model+model_error)[-1]], step="post", color=MODEL_COLOR, alpha=0.15)
        extrema = [nominal-nominal_error, nominal+nominal_error, model-model_error, model+model_error]
        for index, slug in enumerate(COMPONENT_SLUGS):
            if slug in report.components:
                contribution = result.component_values[:, index] * scale
                ax.stairs(contribution, edges, color=TERM_COLORS[index], linestyle="--", linewidth=1.2)
                extrema.append(contribution)
        ax.errorbar(centers, nominal, yerr=nominal_error, fmt="o", color="black", markersize=3, elinewidth=0.8)
        low, high = min(0., float(np.min(extrema))), max(0., float(np.max(extrema)))
        span = max(high-low, 1.)
        ax.set_ylim(low-0.06*span, high+0.23*span)
        ax.axhline(0, color="0.6", linewidth=0.6)
        unit = spec.units[0]
        ax.set_ylabel(f"Expected events / {unit}" if unit else "Expected events / unit", fontsize=12)
        ax.text(0.03, 0.94, name, transform=ax.transAxes, fontsize=9, color="0.35", va="top")
        valid = result.ratio.valid
        values, errors = result.ratio.values[valid], np.sqrt(result.ratio.variances[valid])
        ratio_ax.errorbar(centers[valid], values, yerr=errors, xerr=widths[valid]/2,
                          fmt="o", color=MODEL_COLOR, markersize=3, elinewidth=0.8)
        limits = np.r_[0.8, 1.2, values-errors, values+errors]
        rlow, rhigh = float(np.min(limits)), float(np.max(limits))
        ratio_ax.set_ylim(rlow-0.12*(rhigh-rlow), rhigh+0.12*(rhigh-rlow))
        ratio_ax.axhline(1, color="0.4", linewidth=0.8, linestyle="--")
        ratio_ax.set_ylabel("Sum / nominal", fontsize=10)
        ratio_ax.set_xlabel(spec.axis_labels[0] + (f" [{unit}]" if unit else ""), fontsize=12)
        ratio_ax.set_xlim(edges[0], edges[-1])
        for axis in (ax, ratio_ax):
            axis.tick_params(labelsize=9)
        ratio_ax.text(0, -0.55, f"Undefined ratio bins: {np.count_nonzero(~valid)} / {len(valid)}",
                      transform=ratio_ax.transAxes, fontsize=8, color="0.35")
    fig.text(0.09, 0.074, f"Valid rows: {context.entries:,}; excluded: {context.excluded_entries:,}. Bands: moment MC errors; ratios: correlated MC errors.", fontsize=9)
    return fig


def _map_page(report, key, name, page):
    context = report.contexts[key]
    item = context.projections[name]
    spec = item.spec
    result = context.evaluate(name, components=report.components)
    scale = report.lumi_pb / np.multiply.outer(np.diff(spec.edges[0]), np.diff(spec.edges[1])).ravel()
    nominal, model = item.nominal * scale, result.model * scale
    low, high = min(0., nominal.min(), model.min()), max(0., nominal.max(), model.max())
    norm = TwoSlopeNorm(vmin=-max(abs(low), abs(high), 1.), vcenter=0, vmax=max(abs(low), abs(high), 1.)) if low < 0 else Normalize(0, max(high, 1.))
    fractional = np.where(result.ratio.valid, result.ratio.values-1, np.nan)
    bound = max(float(np.nanmax(np.abs(fractional))) if np.any(result.ratio.valid) else 0., 0.05)
    panels = [
        (nominal, "Nominal", "Expected events / unit area", norm, "RdBu_r" if low < 0 else "viridis", "neither"),
        (model, "Retained sum", "Expected events / unit area", norm, "RdBu_r" if low < 0 else "viridis", "neither"),
        (fractional, "Fractional residual", "(Sum - nominal) / nominal", TwoSlopeNorm(vmin=-bound, vcenter=0, vmax=bound), "RdBu_r", "neither"),
        (np.where(result.pull_valid, result.pulls, np.nan), "Correlated residual pull", "(Sum - nominal) / MC error", Normalize(-5, 5), "RdBu_r", "both"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(11.7, 8.3))
    fig.subplots_adjust(left=0.09, right=0.91, bottom=0.15, top=0.82, hspace=0.43, wspace=0.55)
    _header(fig, report, _context_label(key) + " / polar-angle correlation", page)
    for ax, (values, title, label, color_norm, cmap, extend) in zip(axes.flat, panels):
        ax.set_facecolor("0.9")
        mesh = ax.pcolormesh(*spec.edges, np.ma.masked_invalid(values.reshape(spec.shape).T), cmap=cmap, norm=color_norm, shading="flat")
        ax.set_title(title, fontsize=11)
        ax.set_xlabel(spec.axis_labels[0], fontsize=11)
        ax.set_ylabel(spec.axis_labels[1], fontsize=11)
        ax.tick_params(labelsize=9)
        colorbar = fig.colorbar(mesh, ax=ax, fraction=0.045, pad=0.03, extend=extend)
        colorbar.set_label(label, fontsize=9)
        colorbar.ax.tick_params(labelsize=8)
    fig.text(0.09, 0.075, f"Valid rows: {context.entries:,}; excluded: {context.excluded_entries:,}. Gray: undefined. Pull colors saturate at +/-5.", fontsize=9)
    fig.text(0.09, 0.053, "The residual error includes nominal/moment covariance. Individual cells are statistically correlated.", fontsize=9)
    return fig


def write_closure_report(report: ClosureReport, output: Path, *, overwrite: bool = False) -> int:
    """Publish a completed PDF atomically, preserving an existing file on failure."""
    output = Path(output)
    if output.suffix.lower() != ".pdf":
        raise ValueError("output must have a .pdf suffix")
    if output.resolve() == report.input_path.resolve():
        raise ValueError("output must differ from the input")
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}; pass --overwrite to replace it")
    one_d = [name for name, spec in report.projections.items() if spec.ndim == 1]
    two_d = [name for name, spec in report.projections.items() if spec.ndim == 2]
    pages = 2 + len(report.contexts) * (math.ceil(len(one_d)/2) + len(two_d))
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=".pdf", dir=output.parent)
    os.close(fd)
    try:
        with plt.style.context(hep.style.ATLAS), plt.rc_context({
            "font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"],
            "text.usetex": False, "mathtext.fontset": "dejavusans", "pdf.fonttype": 3,
            "axes.formatter.use_mathtext": True, "axes.grid": False,
        }), PdfPages(temporary) as pdf:
            pdf.infodict().update({"Title": f"{report.sample_label}: angular closure", "Author": "OffshellAngularProduction"})
            def save(factory, *args):
                previous = set(plt.get_fignums())
                try:
                    pdf.savefig(factory(*args))
                finally:
                    for number in set(plt.get_fignums())-previous:
                        plt.close(number)
            save(_cover, report, pages)
            save(_moments_page, report)
            page = 3
            for key in report.contexts:
                for start in range(0, len(one_d), 2):
                    save(_one_dimensional_page, report, key, one_d[start:start+2], page)
                    page += 1
                for name in two_d:
                    save(_map_page, report, key, name, page)
                    page += 1
        if overwrite:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)
            os.unlink(temporary)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return pages
