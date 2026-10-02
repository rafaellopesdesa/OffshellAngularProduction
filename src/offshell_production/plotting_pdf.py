"""Render the analysis histograms as one multipage, ATLAS-style PDF."""

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
import mplhep as hep
import numpy as np

from .plotting import ReportData, subset_ratio


COLORS = {"lhe": "#0072B2", "dressed": "#D55E00", "reco": "#222222"}
LEVEL_LABELS = {"lhe": "LHE", "dressed": "Dressed", "reco": "RECO"}
LINE_STYLES = {"lhe": "-", "dressed": "--", "reco": ":"}
SECTIONS = (
    ("selected", "RECO-selected events", ("lhe", "dressed", "reco")),
    ("all", "All events in the generated phase space", ("lhe", "dressed")),
    ("efficiency", r"Acceptance $\times$ efficiency", ("lhe", "dressed")),
)


def _header(fig, report: ReportData, title: str, page: int) -> None:
    fig.text(0.08, 0.965, "Simulation / Delphes", fontsize=16, fontweight="bold")
    fig.text(0.08, 0.936, title, fontsize=13)
    fig.text(
        0.94, 0.965,
        rf"$\sqrt{{s}}=13.6$ TeV, ${report.lumi_pb / 1000:g}\;\mathrm{{fb}}^{{-1}}$",
        fontsize=12, ha="right",
    )
    fig.text(0.94, 0.936, report.sample_label, fontsize=12, ha="right")
    fig.text(0.08, 0.026, "OffshellAngularProduction  |  ATLAS-style phenomenological simulation", fontsize=8, color="0.35")
    fig.text(0.94, 0.026, str(page), fontsize=9, ha="right", color="0.35")


def _cover(report: ReportData, pages: int):
    fig = plt.figure(figsize=(11.7, 8.3))
    _header(fig, report, r"$ZZ\rightarrow e^-e^+\mu^-\mu^+$: kinematics and acceptance", 1)
    selected_yield = report.selected_sumw_pb * report.lumi_pb
    total_yield = report.sumw_pb * report.lumi_pb
    if abs(report.sumw_pb) > 32 * np.finfo(float).eps * math.sqrt(report.sumw2_pb2):
        ratio_text = f"{report.selected_sumw_pb / report.sumw_pb:.5g}"
    else:
        ratio_text = "undefined (cancelled or zero denominator)"
    lines = [
        f"Input: {report.input_path.name}",
        f"Sample: {report.sample_label}    |    Event rows: {report.entries:,}    |    RECO-selected rows: {report.selected_entries:,}",
        f"All-event signed cross section: {report.sumw_pb:.7g} pb",
        f"RECO-selected signed cross section: {report.selected_sumw_pb:.7g} pb",
        f"Expected events: {total_yield:,.5g} inclusive; {selected_yield:,.5g} RECO-selected",
        f"Global selected / all signed weight: {ratio_text}",
        f"{len(report.observables)} observables at common binning across levels; {pages} PDF pages.",
    ]
    y = 0.845
    for line in lines:
        for wrapped in textwrap.wrap(line, 105):
            fig.text(0.08, y, wrapped, fontsize=12)
            y -= 0.036
    blocks = [
        ("1. RECO-selected distributions", "The same reconstructed-event mask is applied at LHE, dressed and RECO levels. Curves retain their absolute expected-event normalization; they are not normalized to unit area."),
        ("2. Inclusive truth distributions", "All stored events contribute at LHE and dressed levels where the plotted observable is valid. Generation cuts and matching remain inherited from the input; no extrapolation beyond the generated phase space is made."),
        ("3. Acceptance x efficiency", "In each truth-variable bin, divide the RECO-selected weighted yield by the inclusive weighted yield at that same level. The numerator is a subset of the denominator; their covariance is included."),
    ]
    y -= 0.025
    for title, body in blocks:
        fig.text(0.08, y, title, fontsize=12, fontweight="bold")
        y -= 0.029
        for line in textwrap.wrap(body, 125):
            fig.text(0.08, y, line, fontsize=10.5)
            y -= 0.025
        y -= 0.025
    notes = (
        "Bands/bars show finite-sample MC uncertainties from signed weights, not experimental or theory uncertainties. "
        "Invalid observables are excluded individually and counted on each panel. Underflow/overflow are folded into "
        "the first/last bins and counted (U/O). Signed-weight ratios can leave [0, 1]; undefined bins are omitted. "
        "Z1 is always dimuon and Z2 dielectron. Angles use the stored Born projection. "
        "pt_ZZ and born_pt4l are projected pT; raw_pt4l is the physical four-lepton pT."
    )
    for line in textwrap.wrap(notes, 135):
        fig.text(0.08, y, line, fontsize=9, color="0.3")
        y -= 0.024
    return fig


def _flow_note(report: ReportData, selection: str, levels: tuple[str, ...], name: str) -> str:
    notes = []
    for level in levels:
        counter = report.counters[(selection, level, name)]
        notes.append(
            f"{LEVEL_LABELS[level]}: invalid {counter.invalid:,}; U/O {counter.underflow:,}/{counter.overflow:,}"
        )
    return "  |  ".join(notes)


def _yield_panel(ax, report: ReportData, selection: str, levels: tuple[str, ...], name: str, log_y: bool) -> None:
    spec = report.observables[name]
    extrema = []
    any_negative = False
    for level in levels:
        histogram = report.histograms[(selection, level, name)]
        edges = histogram.axes[0].edges
        values = histogram.values() * report.lumi_pb / np.diff(edges)
        errors = np.sqrt(histogram.variances()) * report.lumi_pb / np.diff(edges)
        scaled = histogram.copy()
        scaled.view().value[...] = values
        scaled.view().variance[...] = errors**2
        hep.histplot(
            scaled, ax=ax, histtype="step", yerr=False, w2method="sqrt",
            label=LEVEL_LABELS[level], color=COLORS[level], linestyle=LINE_STYLES[level], linewidth=1.6,
        )
        ax.fill_between(
            edges, np.r_[values - errors, (values - errors)[-1]],
            np.r_[values + errors, (values + errors)[-1]], step="post",
            color=COLORS[level], alpha=0.13, linewidth=0,
        )
        extrema.extend([values - errors, values + errors])
        any_negative |= bool(np.any(values < 0))
    ax.set_ylabel(f"Expected events / {spec.unit}" if spec.unit else "Expected events / unit", fontsize=12)
    extreme = np.concatenate(extrema)
    lower, upper = min(0.0, float(np.min(extreme))), max(0.0, float(np.max(extreme)))
    if log_y and upper > 0 and not any_negative:
        # A log display cannot represent nonpositive uncertainty-band portions.
        # Histogram zeros are gaps at this scale; default linear pages retain them.
        positives = np.concatenate([x[x > 0] for x in extrema])
        ax.set_yscale("log")
        ax.set_ylim(max(float(np.min(positives)) * 0.4, upper * 1e-7), upper * 8)
    else:
        span = max(upper - lower, 1.0)
        ax.set_ylim(lower - (0.06 * span if lower < 0 else 0), upper + 0.35 * span)
        if lower < 0:
            ax.axhline(0, color="0.5", linewidth=0.65)
        if log_y and any_negative:
            ax.text(0.02, 0.77, "Linear scale: negative bins", transform=ax.transAxes, fontsize=8)
    ax.legend(loc="upper right", fontsize=10, frameon=False, ncol=1)


def _ratio_panel(ax, report: ReportData, levels: tuple[str, ...], name: str) -> str:
    all_extrema = [0.0, 1.0]
    undefined = []
    for level in levels:
        numerator = report.histograms[("selected", level, name)]
        denominator = report.histograms[("all", level, name)]
        ratio = subset_ratio(numerator, denominator)
        valid = ratio.valid
        centers = denominator.axes[0].centers
        halfwidth = denominator.axes[0].widths / 2
        errors = np.sqrt(ratio.variances[valid])
        ax.errorbar(
            centers[valid], ratio.values[valid], yerr=errors, xerr=halfwidth[valid],
            fmt="o" if level == "lhe" else "s", markersize=3.0,
            color=COLORS[level], elinewidth=0.8, capsize=0,
            label=LEVEL_LABELS[level], alpha=0.9,
        )
        all_extrema.extend(ratio.values[valid] - errors)
        all_extrema.extend(ratio.values[valid] + errors)
        undefined.append(f"{LEVEL_LABELS[level]} undefined: {np.count_nonzero(~valid)}")
    lower, upper = min(all_extrema), max(all_extrema)
    span = max(upper - lower, 1)
    ax.set_ylim(lower - 0.08 * span, upper + 0.3 * span)
    ax.axhline(1, color="0.4", linestyle="--", linewidth=0.8)
    ax.axhline(0, color="0.6", linewidth=0.6)
    ax.set_ylabel(r"Acceptance $\times$ efficiency", fontsize=12)
    ax.legend(loc="upper right", fontsize=10, frameon=False)
    return "; ".join(undefined)


def _plot_page(report: ReportData, section, names: list[str], page: int, log_y: bool):
    selection, title, levels = section
    fig, axes = plt.subplots(2, 2, figsize=(11.7, 8.3))
    fig.subplots_adjust(left=0.09, right=0.965, bottom=0.18, top=0.84, hspace=0.80, wspace=0.30)
    _header(fig, report, title, page)
    for ax, name in zip(axes.flat, names):
        spec = report.observables[name]
        extra = ""
        if selection == "efficiency":
            extra = _ratio_panel(ax, report, levels, name)
        else:
            _yield_panel(ax, report, selection, levels, name, log_y)
        ax.set_xlabel(spec.label + (f" [{spec.unit}]" if spec.unit else ""), fontsize=12)
        ax.set_xlim(spec.edges[0], spec.edges[-1])
        ax.tick_params(axis="both", labelsize=10)
        ax.text(0.02, 0.93, name, fontsize=9, color="0.3", transform=ax.transAxes, va="top")
        count_selection = "all" if selection == "efficiency" else selection
        note = _flow_note(report, count_selection, levels, name)
        if extra:
            note += "\n" + extra
        # Keep audit text outside the plotting area, separated from axis labels.
        note = "\n".join(line for part in note.splitlines() for line in textwrap.wrap(part, 82))
        ax.text(0, -0.34, note, transform=ax.transAxes, fontsize=6.7, va="top", color="0.35")
    for ax in list(axes.flat)[len(names):]:
        ax.set_visible(False)
    return fig


def write_report(report: ReportData, output: Path, *, overwrite: bool = False, log_y: bool = False) -> int:
    """Write all pages atomically; preserve an existing report on any failure."""
    output = Path(output)
    if output.suffix.lower() != ".pdf":
        raise ValueError("output must have a .pdf suffix")
    if output.resolve() == report.input_path.resolve():
        raise ValueError("output must differ from the ROOT input")
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}; pass --overwrite to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    names = list(report.observables)
    pages = 1 + len(SECTIONS) * math.ceil(len(names) / 4)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=".pdf", dir=output.parent)
    os.close(fd)
    try:
        with plt.style.context(hep.style.ATLAS), plt.rc_context({
            "font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"],
            "text.usetex": False, "axes.formatter.use_mathtext": True,
            "mathtext.fontset": "dejavusans",
            # Embedded vector outlines avoid cross-reader clipping observed
            # with subsetted TrueType fonts in long multipage PDFs.
            "pdf.fonttype": 3, "axes.grid": False,
        }), PdfPages(temporary) as pdf:
            pdf.infodict().update({
                "Title": f"{report.sample_label}: kinematics and acceptance",
                "Author": "OffshellAngularProduction",
                "Subject": "RECO selection, inclusive generated phase space, and weighted subset efficiencies",
            })
            fig = _cover(report, pages)
            try:
                pdf.savefig(fig)
            finally:
                plt.close(fig)
            page = 2
            for section in SECTIONS:
                for start in range(0, len(names), 4):
                    fig = _plot_page(report, section, names[start:start + 4], page, log_y)
                    try:
                        pdf.savefig(fig)
                    finally:
                        plt.close(fig)
                    page += 1
        if overwrite:
            os.replace(temporary, output)
        else:
            # Exclusive publication also protects against a concurrent writer.
            os.link(temporary, output)
            os.unlink(temporary)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return pages
