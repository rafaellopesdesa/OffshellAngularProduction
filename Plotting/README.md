# Kinematic and acceptance reports

`plot_analysis.py` reads one merged campaign ROOT file and writes a single
multi-page PDF. It uses `uproot`, `hist`, `matplotlib`, and `mplhep` in the
same Python project environment as the analysis and merger. No ROOT runtime,
Athena, Delphes installation, or graphical display is required for plotting.

The report uses ATLAS-style fonts, ticks, legends, and axis labels, with
`Simulation / Delphes` labels identifying these as our simulation studies.
It does not apply a new event selection or change the production model.

## Run

From the repository root, using pixi:

```bash
pixi run plot /data/$USER/offshell/merged/qqZZ.root \
  --output /data/$USER/offshell/plots/qqZZ.pdf
```

Alternatively, use the plotting extra with uv:

```bash
uv run --frozen --extra plotting python Plotting/plot_analysis.py \
  /data/$USER/offshell/merged/qqZZ.root \
  --output /data/$USER/offshell/plots/qqZZ.pdf
```

The same command works for the other separately merged production samples,
including `gg4l`, `gg4l_h`, `gg4l_b`, and the VPolar channels. Pass
`--overwrite` to replace an existing report deliberately. The ROOT input is
read-only.

For a shorter report, give variable names without a level prefix:

```bash
pixi run plot /data/$USER/offshell/merged/qqZZ.root \
  --output /data/$USER/offshell/plots/qqZZ_masses_angles.pdf \
  --variables m_Z1 m_Z2 m_ZZ raw_pt4l cos_theta1 cos_theta2 Phi Phi1
```

Run `pixi run plot --list-variables` to list supported names without opening
a ROOT file. Optional arguments include:

| Argument | Meaning |
|---|---|
| `--lumi-fb 29` | Replace the stored luminosity with $29\,\mathrm{fb}^{-1}$ |
| `--variables NAME [NAME ...]` | Plot only these variables, in the supplied order |
| `--bins-json bins.json` | Override bin edges by variable name |
| `--step-size "100 MB"` | Set the ROOT-reading chunk size; this is the default |
| `--log-y` | Use logarithmic distribution axes where the signed content permits it |
| `--overwrite` | Replace an existing completed PDF |

A binning file is a JSON object containing finite, strictly increasing bin
edges. The same edges apply to all levels and selections for each variable:

```json
{
  "m_ZZ": [150, 180, 200, 220, 240, 300, 400, 600, 1000, 1500, 3000],
  "raw_pt4l": [0, 10, 20, 30, 50, 80, 120, 200, 400]
}
```

## Contents of the PDF

The report starts with a summary of the input, luminosity, event counts, and
signed yields. It then shows four variables per page in three sections, with
invalid-value and flow counts on each panel:

| Section | Events | Overlaid levels |
|---|---|---|
| RECO-selected distributions | `reconstructed == true` | LHE, dressed, RECO |
| All-event distributions | Every retained `Events` row, without RECO selection | LHE, dressed |
| Acceptance times efficiency | RECO-selected divided by all events at the same level | LHE, dressed |

Every histogram uses the relevant level's validity mask and finite values of
the plotted variable. Missing candidates or undefined angles therefore do not
become zero-valued entries. No RECO requirement is added to the all-event
histograms. Counts of unavailable values make this distinction visible in the
report.

Here **all events** means the generated phase space retained in the compact
analysis tree. The generator cuts, exclusive $2e2\mu$ final state, and
shower/event-matching contract still apply. These plots do not extrapolate to
phase space that was never generated. A variable requiring a valid dressed
candidate or Born projection describes the subset where that quantity exists;
its denominator cannot include events with no defined coordinate.

The default report has 61 pages: one summary page and 20 pages per section.
It covers 77 continuous kinematic observables: all 56 fields stored at each
level and the 21 additional kinematic quantities used by the RECO selection:

- The dilepton and four-lepton masses, four-lepton rapidity, and transverse
  momentum.
- The local positive-lepton helicity coordinates and the standard five-angle
  observables, including the stored cosines.
- The raw and Born-projected four-lepton mass, rapidity, and transverse
  momentum diagnostics.
- The raw and Born-projected $(E,p_x,p_y,p_z)$ of each of the four
  charge-resolved leptons.
- The raw four-lepton and dilepton masses used for selection, each lepton's
  $p_{\mathrm{T}}$ and $|\eta|$, the four ordered lepton transverse
  momenta, and all six lepton-pair $\Delta R$ separations.

The final group reads the existing RECO selection-kinematic branches. At LHE
and dressed level, the corresponding quantities are calculated from that
level's stored raw four-vectors, without applying any RECO cuts to those
objects. For example, `electron_minus_pt_for_selection` compares the raw
electron transverse momentum at all three levels, while `ordered_pt1`
compares the leading lepton transverse momentum. `--list-variables` prints
the complete names, including the six `delta_r_<lepton>_<lepton>` fields.

Event identifiers, weights, multiplicities, and Boolean selection/validity
masks are not treated as continuous kinematic observables. The existing flavor
assignment is preserved: $Z_1=\mu^+\mu^-$ and $Z_2=e^+e^-$, independently
of which pair lies closer to $m_Z$. Lowercase `theta1`, `phi1`, `theta2`, and
`phi2` describe the harmonic coordinates; `theta1_standard`,
`theta2_standard`, `Phi`, `Phi1`, and `Psi` retain their separate conventions.

The stored `pt_ZZ` is evaluated after the Born projection and is close to zero
by construction. Use `raw_pt4l` to inspect the physical four-lepton transverse
momentum before projection. The report retains both quantities so this
distinction is explicit.

## Normalization and uncertainties

The default expected-event weight is

$$
w_i=\texttt{weight\_nominal\_pb}_i\times\texttt{lumi}_i,
$$

using the merged file's constant luminosity in $\mathrm{pb}^{-1}$. The current
merger stores $312000\,\mathrm{pb}^{-1}=312\,\mathrm{fb}^{-1}$. This is the
same normalization as the existing `weight` branch; luminosity is applied
only once. An input without stored luminosity requires an explicit
`--lumi-fb` value. A job-level file without `weight_nominal_pb` must first be
merged.

The y axes show expected events per unit of the plotted variable, such as
events/GeV or events/rad: bin contents and errors are divided by their bin
widths. The distributions are not rescaled to unit area or forced to have
matching integrals. Negative event weights keep their signs.

To compare with a different integrated luminosity, pass, for example,
`--lumi-fb 29`. The report then uses
$w_i=\texttt{weight\_nominal\_pb}_i\times29000\,\mathrm{pb}^{-1}$.
This changes expected yields and their MC errors together; acceptance ratios
are unchanged by a common luminosity factor.

For a variable $x$ at a level $\ell\in\{\mathrm{LHE},\mathrm{dressed}\}$,
the reported acceptance times efficiency in bin $b$ is

$$
(A\epsilon)_{\ell,b}=\frac{N_{\ell,b}}{D_{\ell,b}},\qquad
N_{\ell,b}=\sum_i w_i\,V_{\ell,i}\,R_i\,
\mathbf{1}[x_{\ell,i}\in b],\qquad
D_{\ell,b}=\sum_i w_i\,V_{\ell,i}\,
\mathbf{1}[x_{\ell,i}\in b],
$$

where $R_i$ is `reconstructed` and $V_{\ell,i}$ means that this variable
is defined and valid at level $\ell$. Numerator and denominator use the
**same level, variable, and bin edges**. A RECO-variable histogram divided
by an LHE-variable histogram would mix migrations with acceptance and is not
the ratio plotted here. The LHE and dressed ratios quantify the combined
response and analysis selection relative to their respective denominators;
they are not detector identification efficiencies alone.

Histogram errors use $\sum_i w_i^2$. The ratio error includes the fact that
the numerator is a subset of its denominator. With
$Q_N=\sum_{\mathrm{selected}}w_i^2$ and
$Q_D=\sum_{\mathrm{all}}w_i^2$ in the same bin,

$$
\operatorname{Var}(N/D)=
\frac{Q_N}{D^2}+\frac{N^2Q_D}{D^4}
-\frac{2NQ_N}{D^3}.
$$

These are finite-simulation statistical uncertainties. They do not include
experimental systematic uncertainties or the separate uncertainty on the
generator cross-section normalization. Signed-weight cancellations can give
ratios outside $[0,1]$ or large uncertainties; these are displayed without
clipping. Bins with a zero signed denominator, or cancellation below numerical
precision, are undefined, not zero efficiency.

Underflow and overflow are folded into the first and last displayed bins,
respectively, for every level and both sides of the ratio. The report records
these contributions so the edge bins are not mistaken for strictly bounded
intervals. Invalid or non-finite coordinates are excluded rather than folded.

The default y scale is linear and includes negative bins and uncertainty
bands. With `--log-y`, distribution panels containing negative bins retain a
linear scale and are marked accordingly; zero values and nonpositive parts
of uncertainty bands cannot be displayed on logarithmic panels. Acceptance
ratios always use linear axes.

## Interpretation

The RECO-selected overlays compare LHE, dressed, and RECO kinematics for the
surviving events. Similar selected integrals do not demonstrate high
acceptance: the all-event plots and the ratio section supply that missing
information. Compare the exact signed yields in the report summary before
reading the shapes.

Comparisons to an ATLAS result also require the same luminosity, final state,
generator phase space, and event selection. These reports retain the
repository's off-shell selection and do not silently substitute the cuts of
an external measurement. One-dimensional shape agreement is a useful initial
check; a quantitative resolution measurement additionally needs matched-event
residuals or a migration matrix.
