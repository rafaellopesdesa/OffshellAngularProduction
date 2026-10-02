from pathlib import Path
import re
import subprocess
import sys

import matplotlib.pyplot as plt
import numpy as np
import pytest
import uproot

from offshell_production.plotting import read_report
from offshell_production.plotting_pdf import write_report


ROOT = Path(__file__).resolve().parents[2]


def write_input(path: Path) -> None:
    n = 100
    rng = np.random.default_rng(321)
    arrays = {
        "weight_nominal_pb": np.full(n, 0.0001),
        "weight": np.full(n, 999.0),  # Deliberately not used or multiplied twice.
        "lumi": np.full(n, 312000.0),
        "sample_code": np.ones(n, dtype=np.uint8),
        "reconstructed": np.arange(n) % 2 == 0,
    }
    for level in ("lhe", "dressed", "reco"):
        arrays[f"{level}_projection_valid"] = np.ones(n, dtype=bool)
        arrays[f"{level}_m_Z1"] = rng.normal(91, 3, n)
        arrays[f"{level}_m_Z2"] = rng.normal(91, 4, n)
        arrays[f"{level}_m_ZZ"] = 180 + rng.exponential(100, n)
        arrays[f"{level}_theta1"] = rng.uniform(0, np.pi, n)
    with uproot.recreate(path) as output:
        output.mktree("Events", arrays)


def test_cli_writes_three_sections_and_preserves_existing_pdf(tmp_path):
    source, output = tmp_path / "input.root", tmp_path / "report.pdf"
    write_input(source)
    command = [
        sys.executable, str(ROOT / "Plotting/plot_analysis.py"), str(source),
        "--output", str(output), "--variables", "m_Z1", "m_Z2", "m_ZZ", "theta1",
    ]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "Expected yield: 3120 all; 1560 RECO-selected" in run.stdout
    data = output.read_bytes()
    assert data.startswith(b"%PDF-")
    # Matplotlib writes ordinary page dictionaries, not compressed object streams.
    assert len(re.findall(rb"/Type /Page\b", data)) == 4
    refused = subprocess.run(command, capture_output=True, text=True)
    assert refused.returncode == 2
    assert "--overwrite" in refused.stderr
    assert output.read_bytes() == data
    override = subprocess.run(command + ["--lumi-fb", "10", "--overwrite"], capture_output=True, text=True)
    assert override.returncode == 0, override.stderr
    assert "Expected yield: 100 all; 50 RECO-selected" in override.stdout


def test_signed_empty_selected_report_and_atomic_failure(tmp_path, monkeypatch):
    source = tmp_path / "input.root"
    write_input(source)
    report = read_report(source, variables=["m_Z1"])
    selected = report.histograms[("selected", "lhe", "m_Z1")]
    selected.reset()
    inclusive = report.histograms[("all", "lhe", "m_Z1")]
    inclusive.view().value[0] = -0.002
    inclusive.view().variance[0] = 0.000004
    output = tmp_path / "signed.pdf"
    assert write_report(report, output, log_y=True) == 4
    assert not plt.get_fignums()
    saved = output.read_bytes()

    import offshell_production.plotting_pdf as renderer

    def fail(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(renderer, "_plot_page", fail)
    with pytest.raises(RuntimeError, match="render failed"):
        write_report(report, output, overwrite=True)
    assert output.read_bytes() == saved
    assert not list(tmp_path.glob(".signed.*.pdf"))


def test_list_variables_and_bad_binning(tmp_path):
    command = [sys.executable, str(ROOT / "Plotting/plot_analysis.py")]
    result = subprocess.run(command + ["--list-variables"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 77
    assert "raw_muon_plus_E" in result.stdout
    source = tmp_path / "input.root"
    write_input(source)
    bins = tmp_path / "bins.json"
    bins.write_text('{"m_Z1": [90, 80]}')
    output = tmp_path / "bad.pdf"
    result = subprocess.run(command + [str(source), "--output", str(output), "--bins-json", str(bins)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "strictly increasing" in result.stderr
    assert not output.exists()
