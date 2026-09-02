# Physics conventions

This file is the contract shared by generation, simulation, and analysis. A
change here is a physics change and should be accompanied by a targeted test.

## Process and final state

- `gg4l` denotes the full POWHEG `gg4l` contribution: Higgs-mediated diagrams,
  continuum diagrams, and their interference (`contr = "full"`).
- `qqZZ` denotes the POWHEG quark-initiated continuum process.
- Both samples are generated directly as exactly one final-state
  \(e^-e^+\mu^-\mu^+\) system. Tau feed-down is not part of this production.
- Finite signed generator weights are immutable. No stage takes their absolute
  value, rejects a negative-weight event, or renormalizes the stored weight.
- Neither sample receives the branching-ratio factor used for an inclusive
  `gg_H` sample. Both Delphes weight scales are exactly one.

The generation phase spaces are:

| sample | dilepton requirement | four-lepton range |
|---|---:|---:|
| `gg4l` | \(50\leq m_{\ell\ell}\leq 200\) GeV | native POWHEG and LHE check: \(150\leq m_{4\ell}\leq 3000\) GeV |
| `qqZZ` | \(m_{\ell\ell}\geq 50\) GeV | pre-shower LHE filter: \(70\leq m_{4\ell}\leq 3000\) GeV |

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

POWHEG is required to write `IDWTUP=-4`, for which each nominal `XWGTUP` is a
signed cross-section estimator in pb. If \(N\) events were generated and the
accepted weights have sum \(A\) and squared-weight sum \(Q_A\), the authoritative
filtered normalization is

\[
\sigma_{\mathrm{filtered}}=\frac{A}{N},\qquad
\delta_{\mathrm{MC}}=\sqrt{\frac{Q_A-A^2/N}{N(N-1)}}.
\]

Rejected events enter this estimator with zero weight, so the uncertainty
includes both filter acceptance and signed-weight fluctuations. The error is a
finite-LHE Monte Carlo standard error, not a POWHEG integration-grid error; it
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

## Event identity

The logical source identity is

```text
(campaign_id, sample_code, job_id, source_event_id)
```

where `sample_code` is 0 for `gg4l` and 1 for `qqZZ`. A deterministic 128-bit
digest is stored as two unsigned 64-bit words for convenient joining. Processing
failures and unmatched events are fatal pipeline errors, not detector
inefficiencies.

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
