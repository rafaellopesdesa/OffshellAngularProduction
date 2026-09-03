# Merging analysis outputs

`merge_analysis_outputs.py` combines the compact `analysis.root` files from
many independent generation jobs into one campaign-level ROOT file. It is a
two-pass physics merger rather than a call to `hadd`: the first pass validates
the inputs and pools their normalization, and the second writes the events and
the LHE truth angular weights.

Run it from the repository root:

```bash
uv run python Merging/merge_analysis_outputs.py \
  --output /data/$USER/offshell/merged/gg4l.root \
  /data/$USER/offshell/jobs/gg4l_job*/analysis.root
```

A completed workflow job directory can be passed in place of its ROOT file;
the merger then requires both `analysis.root` and the `SUCCESS` marker. Inputs
must belong to one campaign and one physics sample, and every source job must
use a distinct generation seed and a distinct Delphes seed. Existing output is
never replaced unless `--overwrite` is supplied.

Merged campaign files are final products, not valid inputs to another merge.
To extend a campaign after more small jobs finish, rerun the command over all
of the original job-level `analysis.root` files and replace the prior output
explicitly with `--overwrite`.

## Cross-section normalization

Each generator job creates a roughly 10% LHE safety stream before Pythia, whereas
the job-level `Events` tree contains only the requested matched HepMC events.
Consequently, neither an average of job cross sections nor division by the
number of retained events gives the authoritative campaign normalization.

For input job (j), the merger reads from `Runs`

$$
N_j=N_{\mathrm{generated},j},\qquad
A_j=\sum_{\mathrm{accepted}}w,\qquad
Q_j=\sum_{\mathrm{accepted}}w^2.
$$

It pools the primitive quantities before calculating

$$
\sigma_{\mathrm{filtered}}=
\frac{\sum_j A_j}{\sum_j N_j},
\qquad
\delta\sigma_{\mathrm{filtered}}=
\sqrt{\frac{Q-A^2/N}{N(N-1)}}.
$$

Rejected LHE trials enter this estimator with zero weight. The corresponding
inclusive values are recomputed from the generated-weight moments in the same
way.

The original signed `weight_lhe` is copied unchanged. A single common scale

$$
c=\frac{\sigma_{\mathrm{filtered}}}
        {\sum_{i\in\mathrm{Events}}w_i^{\mathrm{LHE}}}
$$

defines `weight_nominal_pb = c * weight_lhe`, so its sum closes exactly to the
pooled filtered cross section. Positive and negative events always receive the
same scale; no absolute value or sign-dependent normalization is used.

## LHE truth angular weights

The merger uses the already stored Born-projected LHE coordinates
`lhe_theta1`, `lhe_phi1`, `lhe_theta2`, and `lhe_phi2`. Here
$\Omega_1$ follows $\mu^+$ in the dimuon rest frame and $\Omega_2$
follows $e^+$ in the dielectron rest frame. It does not recompute the Born
projection and does not use the dressed, RECO, or standard five-angle fields.

For modes $\alpha=(\ell_1,m_1)$ and
$\beta=(\ell_2,m_2)$,

$$
\mathcal Y^{(+)}_{\alpha\beta}=
\frac{Y_\alpha(\Omega_1)Y_\beta(\Omega_2)+
      Y_\alpha(\Omega_2)Y_\beta(\Omega_1)}
     {\sqrt{2(1+\delta_{\alpha\beta})}}.
$$

For every requested component the output stores:

- `truth_h_<slug>` = $\operatorname{Re}\mathcal Y^{(+)*}$, the bare
  symmetric basis element used in the earlier truth-reweighting notebook;
- `truth_factor_<slug>` =
  $4\pi\operatorname{Re}\mathcal Y^{(+)*}$, the dimensionless projector;
- `weight_truth_<slug>_pb` = `weight_nominal_pb * truth_factor_<slug>`, the
  signed event contribution in pb.

The branch-safe component slugs are:

| Component | Slug |
|---|---|
| $(0,0;2,0)$ | `00_20` |
| $(2,0;2,0)$ | `20_20` |
| $(2,-1;2,1)$ | `2m1_2p1` |
| $(2,-2;2,2)$ | `2m2_2p2` |

Thus a coefficient in any kinematic bin is the sum of the corresponding
`weight_truth_<slug>_pb` values over rows with `truth_lhe_valid=true`. There is
no division by $S_{00;00}$. All four projectors are real algebraically.
Invalid LHE projections remain in `Events`, with `truth_lhe_valid=false` and
`NaN` truth fields. A usual sum-of-squared-event-weights uncertainty can be
formed from these contributions; it does not include the separately reported
finite-LHE uncertainty of the pooled cross-section normalization.

## Composing the polarized validation samples

`compose_polarized_components.py` takes four campaign-level outputs from the
ordinary merger, one for each separately generated VPolar channel. The label
order is fixed throughout the repository:

$$
Z_1\to\mu^+\mu^-,\qquad Z_2\to e^+e^-.
$$

Thus `TL` means a transverse dimuon system and a longitudinal dielectron
system, while `LT` means the converse. The four inputs must be separately
generated `LL`, `TT`, `TL`, and `LT` samples. A coherent one-amplitude
`TL+LT` file is deliberately rejected. Here “interference-free” refers to the
separation of the longitudinal/transverse channels: VPolar's `ZT` propagator
remains the intended coherent sum of the $\lambda=+1$ and $-1$ transverse
helicities.

Run the composer after merging the jobs within each polarization channel:

```bash
uv run python Merging/compose_polarized_components.py \
  --ll /data/$USER/offshell/merged/vpolar_LL.root \
  --tt /data/$USER/offshell/merged/vpolar_TT.root \
  --tl /data/$USER/offshell/merged/vpolar_TL.root \
  --lt /data/$USER/offshell/merged/vpolar_LT.root \
  --output /data/$USER/offshell/merged/vpolar_components.root
```

For a single vector-boson decay, define

$$
a_L=-\frac{1}{\sqrt{5}},\qquad
a_T=\frac{1}{2\sqrt{5}}.
$$

The exchange-symmetric basis then gives

$$
C_{00;20}(h_1h_2)=\frac{a_{h_1}+a_{h_2}}{\sqrt{2}},
\qquad
C_{20;20}(h_1h_2)=a_{h_1}a_{h_2}.
$$

Equivalently, with
$\sigma_M=\sigma_{TL}+\sigma_{LT}$,

$$
S_{00;20}=-\sqrt{\frac{2}{5}}\,\sigma_{LL}
 +\frac{1}{\sqrt{10}}\,\sigma_{TT}
 -\frac{1}{\sqrt{40}}\,\sigma_M,
$$

$$
S_{20;20}=\frac{1}{5}\,\sigma_{LL}
 +\frac{1}{20}\,\sigma_{TT}
 -\frac{1}{10}\,\sigma_M.
$$

The output contains one concatenated `Events` tree, two signed angular-component
samples, and one direct incoherent mixed-polarization sample:

| Branch | Meaning |
|---|---|
| `source_polarization_code` | `0=LL`, `1=TT`, `2=TL`, `3=LT` |
| `polarization_coefficient_00_20` | Constant source-channel multiplier for $(0,0;2,0)$ |
| `polarization_coefficient_20_20` | Constant source-channel multiplier for $(2,0;2,0)$ |
| `polarization_coefficient_mixed_incoherent` | `1` for TL/LT and `0` for LL/TT |
| `weight_polcomb_00_20_pb` | Signed $(0,0;2,0)$ sample weight |
| `weight_polcomb_20_20_pb` | Signed $(2,0;2,0)$ sample weight |
| `weight_mixed_incoherent_pb` | Direct incoherent TL+LT sample weight |

Every original event branch, including `weight_lhe`, `weight_nominal_pb`, and
the direct `weight_truth_*_pb` projectors, is preserved unchanged. The new
weights are each source's already normalized `weight_nominal_pb` multiplied by
the corresponding constant above; the signed results are never renormalized.
For direct mixed-polarization use, `weight_mixed_incoherent_pb` preserves the
nominal TL and LT weights and assigns zero to LL and TT, so its integral is
exactly $\sigma_{TL}+\sigma_{LT}$ without a coherent TL/LT interference term.
`PolarizationSources` records the four source cross sections and their
contributions, while `PolarizationCombinationSummary` records the resulting
signed integrals and sum-of-squared-weight diagnostics. The original `Runs`
and optional `LHEWeights` rows are concatenated without modification.
`polarization_combination_metadata` embeds the source hashes and merge metadata,
the coefficient map, normalization and validity contracts, VPolar invariants,
and code provenance.

Each input is also bound to its registered source role: `vpolar_LL` through
`vpolar_LT` use permanent sample codes 10 through 13, respectively, and the
standalone backend is `madgraph5-pythia8-vpolar-standalone`. Relabeling a gg4l
or qqZZ file with polarization strings is rejected.

Each job's generation metadata must declare the VPolar contract through
`provenance.generation`: `polarization_component`, `polarization_z1_decay`,
`polarization_z2_decay`, `polarization_frame`, and
`mixed_polarization_interference`, together with
`madgraph_me_frame="3,4,5,6"`. The accepted polarization frame is the explicit
four-lepton rest frame used by the LHE Born projection. The composer checks the
declared input role, flavor assignment, common frame and campaign,
non-polarization physics settings, disjoint source-job identities, schemas, and
source normalization. It additionally fingerprints the VPolar-specific cuts,
software/UFO versions, cards, installation manifest, loop filter, PDF, and
shower settings across the four inputs. Per-source integration uncertainties
are retained in `PolarizationSources`; they are not combined because the
cross-channel covariance is generally unknown.

This is principally a production-level closure construction. The constant
coefficients map the polarized rates to the two symmetric $m=0$ moments after
complete decay-angle integration. Once angle-dependent lepton acceptance or a
detector response is imposed, it need not equal the direct event-by-event
harmonic projection in `weight_truth_*_pb`. The acceptance-free LHE comparison
is therefore the authoritative validation; differences after RECO selection
probe acceptance migration rather than the algebraic polarization identity.

## Ordinary merger output and validation contract

The merged file contains:

- `Events`, with every original scalar branch plus the derived nominal and
  truth-weight branches;
- `Runs`, with the original row from every source job;
- `MergeSummary`, with pooled normalization, raw/normalized sums, and counts;
- `LHEWeights`, when every input has the same ordered alternative-weight
  schema; and
- `merge_metadata`, including source SHA-256 digests, embedded job metadata,
  formulas, angular conventions, and code provenance.

The merger rejects mixed samples or campaigns, incompatible analysis or
alternative-weight schemas, duplicate source jobs or seeds, malformed event
identities, inconsistent run counts or weight moments, non-finite nominal
weights, unresolved signed normalizations, and incompatible dressed-lepton
origin policies. The origin fingerprint includes both field presence and the
direct-hard/exact-`2e2mu` values, while simulation schema version is mandatory;
therefore legacy schema-2 files remain mergeable with one another but cannot
be mixed into a schema-3 campaign. It writes to a unique partial
file under an advisory output lock, verifies the completed file, rechecks the
input hashes, and only then publishes it atomically.
