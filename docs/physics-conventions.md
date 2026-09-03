# Physics conventions

This file is the contract shared by generation, simulation, and analysis. A
change here is a physics change and should be accompanied by a targeted test.

## Process and final state

- `gg4l` denotes the full POWHEG `gg4l` contribution: Higgs-mediated diagrams,
  continuum diagrams, and their interference (`contr = "full"`).
- `qqZZ` denotes the POWHEG quark-initiated continuum process.
- `vpolar_LL`, `vpolar_TT`, `vpolar_TL`, and `vpolar_LT` denote the four
  standalone VPolarized MadGraph components. Each retains the full
  Higgs-mediated plus continuum-box amplitude and their interference; the
  mixed channels are generated separately so `TL+LT` is an incoherent sum.
- Every sample is generated directly as exactly one final-state
  \(e^-e^+\mu^-\mu^+\) system. Tau feed-down is not part of this production.
- Finite signed generator weights are immutable. No stage takes their absolute
  value, rejects a negative-weight event, or renormalizes the stored weight.
- No sample receives the branching-ratio factor used for an inclusive `gg_H`
  sample. Every Delphes weight scale is exactly one.

The generation phase spaces are:

| sample | dilepton requirement | four-lepton range |
|---|---:|---:|
| `gg4l` | \(50\leq m_{\ell\ell}\leq 200\) GeV | native POWHEG and LHE check: \(150\leq m_{4\ell}\leq 3000\) GeV |
| `qqZZ` | \(m_{\ell\ell}\geq 50\) GeV | pre-shower LHE filter: \(70\leq m_{4\ell}\leq 3000\) GeV |
| `vpolar_LL/TT/TL/LT` | \(50\leq m_{\ell\ell}\leq 200\) GeV | native MadGraph and LHE check: \(150\leq m_{4\ell}\leq 3000\) GeV |

The POWHEG `ZZ` implementation has no native four-lepton-mass keyword. With two
50 GeV dilepton minima, its 70 GeV lower bound is kinematically redundant. A
local helper applies both qqZZ bounds to the hard-event LHE stream after POWHEG
and before Pythia. A post-shower ATLAS `FourLeptonInvMassFilter` is deliberately
not used. The same helper checks the native gg4l range before showering.

## Generation filtering and normalization

Before Pythia, every accepted LHE event receives two technical named weights:
`AUX_OAP_EVENT_ID` contains its original positive source-event index and
`AUX_OAP_EVENT_UNIT` contains one. They are excluded from physics alternative
weights. `lhe-contract-metadata.json` records generated, accepted, below-range,
and above-range counts together with

\[
\epsilon_{\mathrm{signed}}=
\frac{\sum_{\mathrm{accepted}}w}{\sum_{\mathrm{generated}}w},
\qquad
\epsilon_{\mathrm{abs}}=
\frac{\sum_{\mathrm{accepted}}|w|}{\sum_{\mathrm{generated}}|w|}.
\]

Every backend is required to write `IDWTUP=-4`; the standalone MadGraph path
obtains it from `event_norm=average`. Each nominal `XWGTUP` is then a signed
cross-section estimator in pb. If \(N\) events were generated and the accepted
weights have sum \(A\) and squared-weight sum \(Q_A\), the authoritative filtered
normalization is

\[
\sigma_{\mathrm{filtered}}=\frac{A}{N},\qquad
\delta_{\mathrm{MC}}=\sqrt{\frac{Q_A-A^2/N}{N(N-1)}}.
\]

Rejected events enter this estimator with zero weight, so the uncertainty
includes both filter acceptance and signed-weight fluctuations. The error is a
finite-LHE Monte Carlo standard error, not a generator integration-grid error; it
is undefined for \(N<2\). The inclusive estimator uses the same expressions
with all generated weights. LHE `<init>` `XSECUP/XERRUP` values and the running
cross-section fields carried through HepMC and Delphes are preserved only as
diagnostics.

`Runs` exposes the generated/accepted counts and the four sums needed to pool
jobs before recomputing these expressions. Its
`effective_filtered_cross_section_pb` and
`effective_filtered_cross_section_mc_error_pb` come directly from this LHE
contract. The absolute efficiency remains a filter
diagnostic, not a replacement normalization factor.

When several job-level analysis files are merged, first sum the primitive
counts and moments over jobs and only then recompute the two cross sections and
their errors. The retained `Events` rows contain the requested HepMC sample,
not the complete LHE safety stream, so their raw signed sum does not itself
define the cross section. The merger leaves `weight_lhe` unchanged and adds

\[
w_i^{\mathrm{nominal}} = w_i^{\mathrm{LHE}}
\frac{\sigma_{\mathrm{filtered}}}
     {\sum_{k\in\mathrm{retained}} w_k^{\mathrm{LHE}}}.
\]

The same finite scale multiplies positive, negative, and zero weights. A
vanishing, numerically unresolved, or sign-inconsistent retained sum is an
error; it is never repaired with absolute weights.

## Fixed-flavor candidate

At every level, build the candidate only from the four charge-ordered leptons:

\[
Z_1=\mu^-+\mu^+ ,\qquad
Z_2=e^-+e^+ ,\qquad
X_{4\ell}=Z_1+Z_2.
\]

`Z1` is always the dimuon system and `Z2` is always the dielectron system. No
mass ordering and no closest-to-\(m_Z\) pairing are used. Intermediate particles
with PDG IDs 23 or 25 never define the LHE candidate.

Candidate construction and event selection are separate. If a candidate exists
but fails selection, its variables remain available. If it cannot be built, its
floating-point values are NaN and explicit validity masks are false.

## Reconstruction selection

Apply cuts to the unprojected RECO momenta, using strict inequalities:

- every lepton: \(p_T>5\) GeV and \(|\eta|<2.5\);
- three leading ordered leptons: \(p_T>20,15,10\) GeV;
- both fixed-flavor pairs: \(50<m_{\ell\ell}<106\) GeV;
- every pair of selected leptons: \(\Delta R>0.1\);
- \(m_{4\ell}>180\) GeV.

There is no fiducial flag. `reconstructed` means that a RECO candidate exists
and passes this full selection. LHE and dressed variables are not subjected to
the reconstruction cuts. There is no upper RECO \(m_{4\ell}\) cut.

## Born projection and angles

The Born map is evaluated independently for LHE, dressed, and RECO momenta.
For a level-specific four-lepton momentum \(k\), apply \(B_L\), then \(B_T\),
then \(B_L^{-1}\) to all four leptons. This preserves \(m_{4\ell}\),
\(y_{4\ell}\), and internal invariant masses while setting
\(p_{T,4\ell}=0\). Never reuse the LHE transformation at dressed or RECO level.

The spherical-harmonic coordinates use the positive lepton:

- \(\Omega_1=(\theta_1,\phi_1)\) follows \(\mu^+\) in the dimuon rest frame;
- \(\Omega_2=(\theta_2,\phi_2)\) follows \(e^+\) in the dielectron rest frame.

In the Born-projected four-lepton rest frame,
\(\hat z_i\parallel Z_i\), \(\hat y_i\parallel \hat b\times\hat z_i\), and
\(\hat x_i=\hat y_i\times\hat z_i\), with beam direction
\(\hat b=(0,0,+1)\). The standard five-angle variables are stored separately
and use the negative-lepton polar convention. In particular, lowercase
`phi1` and uppercase `Phi1` are different observables.

## Symmetric truth angular weights

For \(\alpha=(\ell_1,m_1)\), \(\beta=(\ell_2,m_2)\), and the LHE-level
Born-projected coordinates above, use the orthonormal exchange-symmetric basis

\[
\mathcal Y^{(+)}_{\alpha\beta}=
\frac{Y_\alpha(\Omega_1)Y_\beta(\Omega_2)+
      Y_\alpha(\Omega_2)Y_\beta(\Omega_1)}
     {\sqrt{2(1+\delta_{\alpha\beta})}}.
\]

The expansion convention is

\[
p(\Omega_1,\Omega_2,x)=\frac{1}{4\pi}
\sum_{\alpha\preceq\beta}\mathcal S_{\alpha\beta}(x)
\mathcal Y^{(+)}_{\alpha\beta},
\]

so the per-event projector and cross-section contribution are

\[
F_{\alpha\beta}=4\pi\operatorname{Re}
\mathcal Y^{(+)*}_{\alpha\beta},\qquad
w^{\mathrm{truth}}_{\alpha\beta}=w^{\mathrm{nominal}}F_{\alpha\beta}.
\]

No division by \(S_{00;00}\) is applied. The current merge stage stores the
real components \((0,0;2,0)\), \((2,0;2,0)\),
\((2,-1;2,1)\), and \((2,-2;2,2)\). All four are real algebraically in the
Condon--Shortley convention. Invalid LHE projections are retained with a false
validity mask and NaN truth values.

## Event identity

The logical source identity is

```text
(campaign_id, sample_code, job_id, source_event_id)
```

where `sample_code` is 0 for `gg4l`, 1 for `qqZZ`, and 10 through 13 for
`vpolar_LL`, `vpolar_TT`, `vpolar_TL`, and `vpolar_LT`, respectively. A
deterministic 128-bit digest is stored as two unsigned 64-bit words for
convenient joining. Processing failures and unmatched events are fatal pipeline
errors, not detector inefficiencies.

The `named-weight-id-v1` contract recovers each HepMC source ID from
`AUX_OAP_EVENT_ID/AUX_OAP_EVENT_UNIT` within bounded floating-point tolerance.
`align_lhe_events.py` selects the LHE record carrying the identical ID and
writes exactly one matched LHE event per HepMC event, in HepMC order. Source
IDs must be unique and strictly increasing but need not be contiguous: gaps
from the LHE phase-space filter or Pythia skips/retries are valid. Analysis
then requires exact LHE/Delphes source-ID equality at every matched ordinal,
the recorded source-ID sequence hash, exact event counts, and a contiguous
Delphes event-number sequence. `lhe_event_index` is the zero-based ordinal in
the matched file; it is not the original source identity.
