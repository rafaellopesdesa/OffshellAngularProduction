from pathlib import Path
import re
import subprocess
import sys

import matplotlib.pyplot as plt
import numpy as np
import pytest
import uproot

from offshell_production.angular_closure import angular_basis, COMPONENT_SLUGS, read_closure
from offshell_production.angular_closure_pdf import write_closure_report


ROOT = Path(__file__).resolve().parents[2]


def write_input(path):
    rng = np.random.default_rng(81)
    size = 250
    theta1, theta2 = np.arccos(rng.uniform(-1, 1, (2, size)))
    phi1, phi2 = rng.uniform(-np.pi, np.pi, (2, size))
    basis = angular_basis(theta1, phi1, theta2, phi2)
    weights = 0.03 / size * (basis @ np.array([1, -0.10, 0.05, 0.02, 0.03]))
    weights[::11] *= -1
    arrays = {
        "weight_nominal_pb": weights, "lumi": np.full(size, 312000.),
        "sample_code": np.zeros(size, dtype=np.int32),
        "reconstructed": np.arange(size) % 3 != 0,
        "truth_lhe_valid": np.ones(size, dtype=bool),
    }
    for index, slug in enumerate(COMPONENT_SLUGS[1:], 1):
        arrays[f"weight_truth_{slug}_pb"] = weights * basis[:, index]
    for level in ("lhe", "dressed", "reco"):
        for name, value in zip(("theta1", "phi1", "theta2", "phi2"), (theta1, phi1, theta2, phi2)):
            arrays[f"{level}_{name}"] = value
        arrays[f"{level}_projection_valid"] = np.ones(size, dtype=bool)
        arrays[f"{level}_frame_degenerate"] = np.zeros(size, dtype=bool)
    with uproot.recreate(path) as output:
        output.mktree("Events", arrays)


def test_closure_cli_pdf_and_existing_output(tmp_path):
    source, output = tmp_path / "events.root", tmp_path / "closure.pdf"
    write_input(source)
    command = [sys.executable, str(ROOT / "Plotting/plot_angular_closure.py"), str(source),
               "--output", str(output), "--variables", "folded_delta_phi", "cos_theta1_vs_cos_theta2",
               "--bins", "8", "--map-bins", "4", "--components", "2m1_2p1", "2m2_2p2"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Wrote 12 pages" in result.stdout
    data = output.read_bytes()
    assert data.startswith(b"%PDF-")
    assert len(re.findall(rb"/Type /Page\b", data)) == 12
    refused = subprocess.run(command, capture_output=True, text=True)
    assert refused.returncode == 2
    assert "--overwrite" in refused.stderr
    assert output.read_bytes() == data


def test_empty_selected_contexts_and_atomic_render_failure(tmp_path, monkeypatch):
    source, output = tmp_path / "events.root", tmp_path / "closure.pdf"
    write_input(source)
    report = read_closure(source, variables=["phi1"], bins=4)
    from offshell_production.angular_closure import empty_context
    for key in report.contexts:
        if key[0] == "selected":
            report.contexts[key] = empty_context(*key, report.projections)
    assert write_closure_report(report, output) == 7
    assert not plt.get_fignums()
    before = output.read_bytes()
    import offshell_production.angular_closure_pdf as renderer

    def failure(*args):
        plt.figure()
        raise RuntimeError("simulated renderer failure")

    monkeypatch.setattr(renderer, "_one_dimensional_page", failure)
    with pytest.raises(RuntimeError, match="simulated renderer failure"):
        write_closure_report(report, output, overwrite=True)
    assert output.read_bytes() == before
    assert not list(tmp_path.glob(".closure.*.pdf"))
    assert not plt.get_fignums()


def test_list_projections_and_reject_bad_bin_count(tmp_path):
    command = [sys.executable, str(ROOT / "Plotting/plot_angular_closure.py")]
    result = subprocess.run(command + ["--list-variables"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 9
    assert "folded_delta_phi" in result.stdout
    output = tmp_path / "bad.pdf"
    result = subprocess.run(command + [str(tmp_path / "missing.root"), "--output", str(output), "--bins", "1"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "at least 2" in result.stderr
    assert not output.exists()
