# Kinematic, acceptance, and angular-closure reports

`plot_analysis.py` reads one merged campaign ROOT file and writes a single
multi-page PDF. It uses `uproot`, `hist`, `matplotlib`, and `mplhep` in the
same Python project environment as the analysis and merger. No ROOT runtime,
Athena, Delphes installation, or graphical display is required for plotting.

The report uses ATLAS-style fonts, ticks, legends, and axis labels, with
`Simulation / Delphes` labels identifying these as our simulation studies.
It does not apply a new event selection or change the production model.

For a separate test of the retained angular expansion, use
[`plot_angular_closure.py`](#angular-closure-report). It shares this environment
and plotting style but produces its own PDF.

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

## Angular-closure report

`plot_angular_closure.py` tests how well the constant angular component and
the four retained symmetric moments reproduce the nominal helicity shapes.
It reads the same merged ROOT file, writes a separate PDF, and requires no
additional dependencies:

```bash
pixi run plot-closure /data/$USER/offshell/merged/gg4l.root \
  --output /data/$USER/offshell/plots/gg4l_closure.pdf
```

The equivalent uv command is:

```bash
uv run --frozen --extra plotting python Plotting/plot_angular_closure.py \
  /data/$USER/offshell/merged/gg4l.root \
  --output /data/$USER/offshell/plots/gg4l_closure.pdf
```

Each PDF compares the nominal distribution, individual signed basis
contributions, their sum, and the reconstructed/nominal ratio. The
two-dimensional page shows the nominal density, reconstructed density,
residual, and statistical pull. Coefficient and validity summaries accompany
the shape comparisons. The same command also works for the other merged
samples; it does not assume that the four nonconstant moments are sufficient
for every process.

Five contexts are evaluated independently:

| Selection | Angular level | Coefficients used |
|---|---|---|
| All generated events | LHE | LHE moments of the valid all-event sample |
| All generated events | Dressed | Moments recomputed from dressed angles |
| `reconstructed == true` | LHE | LHE moments of the valid selected sample |
| `reconstructed == true` | Dressed | Moments recomputed from selected dressed angles |
| `reconstructed == true` | RECO | Moments recomputed from selected RECO angles |

Within each context, all four angles and every retained component share one
validity mask. Invalid projections, non-finite coordinates, and degenerate
helicity frames are excluded from both the moments and the nominal comparison.
LHE additionally requires `truth_lhe_valid`; the stored `weight_truth_*_pb`
values are checked against `weight_nominal_pb` and the harmonic factors.
The report counts exclusions. No RECO requirement is applied to the all-event
contexts, which retain the generator cuts and the phase space of the input.

### Expansion and interpretation

Let $\Omega_1=(\theta_1,\phi_1)$ follow the positive muon and
$\Omega_2=(\theta_2,\phi_2)$ the positron, using the stored Born-projected
helicity frames. With the normalized angular measure

$$
d\mu=\frac{d\Omega_1\,d\Omega_2}{16\pi^2},\qquad
F_0=1,\qquad
F_a=4\pi\operatorname{Re}\mathcal Y_a^{(+)*},
$$

the retained functions obey $\int F_aF_b\,d\mu=\delta_{ab}$, including
the constant component. The cross-section moments and truncated density are

$$
S_a=\sum_{i\in C}w_iF_a(\Omega_i),\qquad
\frac{d\sigma_C}{d\mu}\simeq S_0+\sum_{a\ne0}S_aF_a,
\qquad w_i=\texttt{weight\_nominal\_pb}_i,
$$

where $C$ is one selection/level context. For a displayed bin $b$, the
reconstructed cross section is

$$
M_b=\sum_a S_a B_{ba},\qquad B_{ba}=\int_b F_a\,d\mu.
$$

The code integrates the basis over each bin. It does **not** add the eventwise
projector histograms and call that a reconstructed angular distribution.
Stored `weight_truth_<slug>_pb` are contributions to $S_a$, not physical
component event samples. The constant `00_00` coefficient is the nominal
signed cross section of the same valid context and is always included.
The default nonconstant components are `00_20`, `20_20`, `2m1_2p1`, and
`2m2_2p2`.
The `00_20` basis element already includes its exchanged `20_00` partner;
adding that partner separately would double count it.

Because all nonconstant basis functions integrate to zero, the reconstructed
integral equals $S_0$ by construction. Matching the total yield is therefore
not evidence of closure: inspect the binwise shape residuals and ratios.
Truncation can produce negative predictions, which are retained. A discrepancy
can expose missing angular terms or distortions from cuts and reconstruction;
it does not by itself isolate a detector-model defect.

Each selected context measures its own coefficients. In particular, RECO
closure asks whether this basis describes the **selected RECO angular
density**. It does not transport inclusive LHE moments through acceptance and
resolution. That prediction would require an angular response model beyond
these separate density projections.

### Helicity projections

The default one-dimensional variables are `theta1`, `phi1`, `theta2`, `phi2`,
`cos_theta1`, `cos_theta2`, `delta_phi`, and `folded_delta_phi`. The default
two-dimensional variable is `cos_theta1_vs_cos_theta2`.

The azimuths are defined by

$$
\Delta\phi=\operatorname{wrap}(\phi_2-\phi_1),\qquad
\Delta\phi_{\mathrm{folded}}=
\operatorname{wrap}\!\left(\phi_2-\phi_1+
\pi\,\mathbf{1}[\cos\theta_1\cos\theta_2<0]\right),
$$

where wrapping places an angle in $[-\pi,\pi)$. The folding preserves the
sign dependence needed to expose `2m1_2p1`: that term cancels in the ordinary
$\Delta\phi$ marginal after integrating over both polar cosines.
Similarly, `20_20` is visible in the two-dimensional cosine map but integrates
away in each separate cosine marginal. Individual $\phi_1$ and $\phi_2$
marginals are uniform in this retained basis and supply additional checks.
One-dimensional closure alone therefore cannot establish full angular
closure. The standard negative-lepton angles `Phi`, `Phi1`, and `Psi` are not
substituted for these harmonic coordinates.

### Options and statistical errors

Run `pixi run plot-closure --list-variables` without an input to print the
available projections. Examples of optional arguments are:

| Argument | Meaning |
|---|---|
| `--variables cos_theta1 cos_theta2 folded_delta_phi cos_theta1_vs_cos_theta2` | Restrict the projections shown |
| `--components 00_20 20_20` | Retain only these nonconstant moments; `00_00` remains included |
| `--components 00_00` | Show the constant-term prediction alone |
| `--bins 24` | Number of bins in each one-dimensional projection; the default is 24 |
| `--map-bins 12` | Bins per axis in the two-dimensional cosine map; the default is 12 |
| `--lumi-fb 29` | Override the stored luminosity for displayed expected yields |
| `--step-size "100 MB"` | Set the ROOT-reading chunk size |
| `--overwrite` | Replace an existing report after rendering succeeds |

The moments retain pb normalization. Displayed yields multiply them once by
the stored luminosity, or the `--lumi-fb` override, and divide by bin width
(bin area for the map). Neither the moments nor the nominal histograms are
rescaled to unit area. Signed nominal weights are used throughout.

The reconstructed and nominal curves are correlated because their moments
and bins use the same events. With $I_{ib}$ indicating membership in bin $b$,
the report uses

$$
\operatorname{Cov}(S_a,S_c)=\sum_i w_i^2F_a(\Omega_i)F_c(\Omega_i),
\qquad
\operatorname{Cov}(S_a,D_b)=\sum_i w_i^2F_a(\Omega_i)I_{ib},
\qquad D_b=\sum_iw_iI_{ib}.
$$

These covariances are propagated into the reconstructed uncertainty, the
model/nominal ratio, and the residual pull. Treating the two curves as
independent would give the wrong uncertainty. Zero or numerically cancelled
nominal bins have undefined ratios; signed ratios and predictions are not
clipped. Errors describe finite-sample MC fluctuations, excluding experimental
systematics and the separate generator cross-section normalization uncertainty.
