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

The POWHEG job creates a roughly 10% LHE safety stream before Pythia, whereas
the job-level `Events` tree contains only the requested matched HepMC events.
Consequently, neither an average of job cross sections nor division by the
number of retained events gives the authoritative campaign normalization.

For input job (j), the merger reads from `Runs`

\[
N_j=N_{\mathrm{generated},j},\qquad
A_j=\sum_{\mathrm{accepted}}w,\qquad
Q_j=\sum_{\mathrm{accepted}}w^2.
\]

It pools the primitive quantities before calculating

\[
\sigma_{\mathrm{filtered}}=
\frac{\sum_j A_j}{\sum_j N_j},
\qquad
\delta\sigma_{\mathrm{filtered}}=
\sqrt{\frac{Q-A^2/N}{N(N-1)}}.
\]

Rejected LHE trials enter this estimator with zero weight. The corresponding
inclusive values are recomputed from the generated-weight moments in the same
way.

The original signed `weight_lhe` is copied unchanged. A single common scale

\[
c=\frac{\sigma_{\mathrm{filtered}}}
        {\sum_{i\in\mathrm{Events}}w_i^{\mathrm{LHE}}}
\]

defines `weight_nominal_pb = c * weight_lhe`, so its sum closes exactly to the
pooled filtered cross section. Positive and negative events always receive the
same scale; no absolute value or sign-dependent normalization is used.

## LHE truth angular weights

The merger uses the already stored Born-projected LHE coordinates
`lhe_theta1`, `lhe_phi1`, `lhe_theta2`, and `lhe_phi2`. Here
\(\Omega_1\) follows \(\mu^+\) in the dimuon rest frame and \(\Omega_2\)
follows \(e^+\) in the dielectron rest frame. It does not recompute the Born
projection and does not use the dressed, RECO, or standard five-angle fields.

For modes \(\alpha=(\ell_1,m_1)\) and
\(\beta=(\ell_2,m_2)\),

\[
\mathcal Y^{(+)}_{\alpha\beta}=
\frac{Y_\alpha(\Omega_1)Y_\beta(\Omega_2)+
      Y_\alpha(\Omega_2)Y_\beta(\Omega_1)}
     {\sqrt{2(1+\delta_{\alpha\beta})}}.
\]

For every requested component the output stores:

- `truth_h_<slug>` = \(\operatorname{Re}\mathcal Y^{(+)*}\), the bare
  symmetric basis element used in the earlier truth-reweighting notebook;
- `truth_factor_<slug>` =
  \(4\pi\operatorname{Re}\mathcal Y^{(+)*}\), the dimensionless projector;
- `weight_truth_<slug>_pb` = `weight_nominal_pb * truth_factor_<slug>`, the
  signed event contribution in pb.

The branch-safe component slugs are:

| Component | Slug |
|---|---|
| \((0,0;2,0)\) | `00_20` |
| \((2,0;2,0)\) | `20_20` |
| \((2,-1;2,1)\) | `2m1_2p1` |
| \((2,-2;2,2)\) | `2m2_2p2` |

Thus a coefficient in any kinematic bin is the sum of the corresponding
`weight_truth_<slug>_pb` values over rows with `truth_lhe_valid=true`. There is
no division by \(S_{00;00}\). All four projectors are real algebraically.
Invalid LHE projections remain in `Events`, with `truth_lhe_valid=false` and
`NaN` truth fields. A usual sum-of-squared-event-weights uncertainty can be
formed from these contributions; it does not include the separately reported
finite-LHE uncertainty of the pooled cross-section normalization.

## Output and validation contract

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
weights, and unresolved signed normalizations. It writes to a unique partial
file under an advisory output lock, verifies the completed file, rechecks the
input hashes, and only then publishes it atomically.
