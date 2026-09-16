# Delphes dressed and reconstructed event simulation

This directory converts Pythia-showered HepMC events from either supported
generation backend into a Delphes ROOT tree. It preserves one output entry for every input
HepMC event and stores both dressed-particle and reconstructed-lepton views.
No fiducial or off-shell analysis selection is applied here.

The generation samples are direct `e-e+mu-mu+` final states:

- `gg4l`: the full gluon-initiated process, including Higgs, continuum, and
  their interference;
- `qqZZ`: the quark-initiated four-lepton process;
- `vpolar_LL`, `vpolar_TT`, `vpolar_TL`, and `vpolar_LT`: standalone
  MadGraph/Pythia polarization components in the same exclusive final state.

All samples are already generated in the desired decay channel. Therefore the
simulation applies an identity event-weight scale of exactly `1.0` to every
process. In particular, no Higgs branching fraction is applied to `gg4l`.
`Event.CrossSection` and `Event.CrossSectionError` are preserved unchanged as
signed, running Pythia diagnostics. The authoritative filtered normalization
comes from the pre-shower `IDWTUP=-4` LHE sample mean recorded by the generation
contract, not from these Delphes fields.

## External UChicago/ATLAS environment

Delphes is built against the ROOT installation already active in the shell.
On the UChicago Analysis Facility, initialize a site-provided ROOT environment
first. An ATLAS release is one way to provide ROOT, but the simulation
environment need not be the AthGeneration release used by the separate
generation process. For example, the surrounding interactive setup or future
HTCondor wrapper can do the equivalent of:

```bash
setupATLAS
asetup YOUR_ROOT_PROVIDING_RELEASE
```

The exact release is intentionally not hard-coded here. The active ROOT version
and installation prefix must be identical when Delphes is built and when it
runs. `install_delphes.sh` neither loads a site module nor installs ROOT; it
validates `root` and `root-config`, records their version and prefix, and writes
`env.sh` for the Delphes-specific paths.

## Install

```bash
cd OffshellAngularProduction/Simulation
./install_delphes.sh --prefix /shared/path/to/offshell-delphes --jobs 8
source env.sh
```

The installation pins:

- Delphes 3.5.1, commit `28658365abeb71ee36dfc739f9670c1514c0cb10`;
- the exact SHA-256 of Delphes's bundled ATLAS card;
- five local patches implementing event-weight identity support, robust
  ancestry traversal, two-mother-index handling, prompt-lepton origin rules,
  and unique photon dressing.

The installer is incremental and checks whether every patch feature is already
present. The generated `versions.txt` records the Delphes commit, ROOT build
environment, card checksum, and patch checksums. Set `CXX` to one compiler
executable before installation when the environment's `c++` is not the desired
compiler. The installer resolves that executable to its canonical path, passes
the path explicitly to `make`, and records its version. It also records and
gates incremental builds on the exact `root-config` path, ROOT compile/link
flags and libraries, and the Delphes `ROOTBUILD` mode. Compiler wrappers or
extra flags must not be embedded in `CXX`.

## Dressed leptons

The resolved card starts from stable post-shower electrons, muons, and photons.
Every dressed lepton must:

- be an electron or muon with HepMC status 1;
- have no hadron-decay ancestor;
- not come through an intermediate tau decay.

The remaining origin requirement is selected from the resolved process, and
the two policies are deliberately mutually exclusive:

- `gg4l` and `qqZZ` retain the original rule: the lepton must descend from a
  W, Z, or virtual photon above 5 GeV. Polarized Z identifiers 230 and 231 are
  also recognized (and exempted from the generic numeric hadron-ID test).
- `vpolar_*` requires a direct hard-process lepton. Starting from the stable
  candidate, Delphes follows only exact signed-PDG copies and requires a
  status-1 or status-23 lepton attached directly to two PDG-21, status-21
  incoming gluons. The boson-origin rule is disabled in this mode, so a
  shower `gamma* -> l+l-` conversion cannot enter the hard four-lepton set.

These status values follow the pinned Pythia/HepMC2 interface: an unradiated
outgoing hard lepton is serialized as final status 1; after radiation, its
consumed hard copy is serialized as status 23. Delphes stores the HepMC status
verbatim and resolves the production vertex's two incoming particles into
`Candidate::M1` and `Candidate::M2`.

Eligible stable photons must have no hadron-decay ancestor. Each is assigned
at most once, to the nearest eligible lepton within `deltaR < 0.1`; there is no
photon-pT threshold. Mother fields `M1` and `M2` are treated as two indices,
not as an inclusive array interval. Ancestry walks are cycle-safe and stop at
incoming quarks or gluons so the beam proton does not make hard-process
leptons appear nonprompt.

The complete shower record remains in `Particle`, bare stable particles are in
`StableParticle`, and the dressed objects are in `DressedElectron` and
`DressedMuon`. For `vpolar_*`, output validation additionally requires every
event to contain exactly one dressed particle of each signed flavour
`e-`, `e+`, `mu-`, and `mu+`. Simulation metadata schema 3 records the selected
origin policy, whether direct-hard candidates were required, and whether this
exact multiplicity postcondition was validated.

## Reconstructed response

Dedicated reconstructed collections begin from the dressed prompt leptons and
apply, in order:

1. the Run-2-inspired ECAL energy response below for electrons, and the
   existing Delphes ATLAS-like momentum response for muons;
2. a loose reconstruction-plus-identification efficiency;
3. a separate loose prompt-lepton isolation efficiency.

The reconstruction and isolation decisions are independent Bernoulli stages.
The final collections are not passed through the generic Delphes cone
isolation, avoiding double counting. A 4 GeV technical pT buffer permits
upward migrations into the downstream 5 GeV analysis acceptance.

Central efficiency anchors are:

| electron pT (GeV) | reco + Loose ID | isolation proxy |
|---:|---:|---:|
| 5 | 0.85 | 0.68 |
| 7 | 0.90 | 0.77 |
| 10 | 0.92 | 0.84 |
| 15 | 0.95 | 0.91 |
| 20 | 0.953 | 0.95 |
| 25 | 0.957 | 0.97 |
| 30 | 0.96 | 0.985 |

| muon pT (GeV) | reco + Loose ID | isolation proxy |
|---:|---:|---:|
| 5 | 0.96 | 0.72 |
| 6 | 0.98 | 0.80 |
| 8 | 0.985 | 0.88 |
| 10 | 0.99 | 0.92 |
| 15 | 0.99 | 0.96 |
| 20 | 0.99 | 0.985 |
| 30 | 0.99 | 0.995 |

Efficiencies interpolate continuously between anchors and plateau above the
last anchor. Small broad eta modifiers are included; there is no phi model.
These are phenomenology-level Run-2 H4l proxies, not official detector
calibrations. The setup has no overlaid pileup events, nonprompt/fake-lepton
model, or charge misidentification. The electron response includes a fixed
effective Run-2 pileup-noise contribution as described below.

### Electron ECAL resolution

The dedicated `RecoElectron` and `RecoElectronNoIso` collections use
`H4lElectronECalSmearing`, model `atlas_run2_ecal_snc_v1`. It describes the
**total calibrated electron energy response** of the electromagnetic
calorimeter, with no track-energy combination. The former electron tracking
resolution was inappropriate as the final energy measurement and produced
excessive dielectron mass broadening, especially at high transverse momentum.
Muon smearing, dressing, efficiencies, generic Delphes `Electron`/`Muon`
diagnostic branches, and the fixed $Z_1=\mu^-\mu^+$, $Z_2=e^-e^+$ convention
are unchanged.

For dressed energy $E$ in GeV and dressed momentum pseudorapidity $\eta$, define
$T=\max(E/\cosh\eta,1\;\mathrm{GeV})$. The relative response is

$$
r_e^2(E,\eta)=\left(\frac{\sigma_E}{E}\right)^2
=\frac{S_T(\eta)^2}{T}
+\frac{N_{0,T}(\eta)^2+N_{\mathrm{PU},T}(\eta)^2}{T^2}
+C_{\mathrm{MC}}(\eta)^2+c_{\mathrm{data}}(\eta)^2.
$$

These are **effective coefficients expressed versus transverse energy**,
not bare calorimeter sampling constants expressed versus $E$. They approximate
material, clustering, noise, and calibration effects in the final electron
response. $S_T$ has units $\sqrt{\mathrm{GeV}}$, the two noise terms have units
GeV, and both constant terms are dimensionless fractions:

| $\lvert\eta\rvert$ interval | $S_T$ | $N_{0,T}$ | $N_{\mathrm{PU},T}$ | $C_{\mathrm{MC}}$ | $c_{\mathrm{data}}$ |
|---|---:|---:|---:|---:|---:|
| $0\leq\lvert\eta\rvert\leq0.8$ | 0.09 | 0.30 | 0.55 | 0.004 | 0.007 |
| $0.8<\lvert\eta\rvert\leq1.37$ | 0.12 | 0.84 | 0.55 | 0.004 | 0.009 |
| $1.37<\lvert\eta\rvert\leq1.52$ | 0.15 | 1.20 | 0.70 | 0.010 | 0.025 |
| $1.52<\lvert\eta\rvert\leq2.0$ | 0.10 | 0.55 | 0.60 | 0.004 | 0.015 |
| $2.0<\lvert\eta\rvert<2.5$ | 0.08 | 0.50 | 0.60 | 0.004 | 0.017 |

The coefficients are a reproducible phenomenological approximation, **not an
official ATLAS parameter table or precision fit**. Their basis is:

- The outer-barrel and outer-endcap baseline shapes, with $N_{\mathrm{PU},T}$
  and $c_{\mathrm{data}}$ omitted, approximate the supercluster curves in
  [ATLAS Run-2 electron performance, Fig. 7](https://arxiv.org/pdf/1908.00005#page=18).
  At $E_T=35,55,150$ GeV, they give approximately $3.17,2.26,1.20$ percent
  in the outer barrel and $2.01,1.47,0.84$ percent in the outer endcap.
- The fixed $N_{\mathrm{PU},T}$ terms approximate the broad-bin increase in
  response width between zero pileup and $30<\langle\mu\rangle<45$ in
  [Fig. 8](https://arxiv.org/pdf/1908.00005#page=19), interpreted at a
  representative $E_T=45$ GeV. That inclusive $25<E_T<100$ GeV plot does not
  uniquely determine a noise coefficient at each energy. No event-by-event
  pileup dependence is simulated.
- The $c_{\mathrm{data}}$ terms outside the transition region represent the
  typical additional smearing ranges in
  [ATLAS full Run-2 energy calibration, Section 7](https://arxiv.org/html/2309.05471v2#S7):
  below 1% in most of the barrel and 1--2% in the endcaps. They are added to
  the baseline response in quadrature, not used as the total resolution.
- The central-barrel and inner-endcap coefficients, the degraded transition
  region, and extensions beyond the plotted eta/energy ranges are modelling
  choices. The transition coefficient is not a measured ATLAS correction.
  In particular, the 4--7 GeV buffer/acceptance region and $2.47<|\eta|<2.5$
  should be treated as extrapolations. Broad eta bins introduce explicit
  step boundaries and do not model local detector structures or phi effects.

For orientation, the **total** per-electron resolutions predicted by this
model are:

| Region | $E_T=10$ GeV | $E_T=45$ GeV | $E_T=100$ GeV |
|---|---:|---:|---:|
| Central barrel | 6.93% | 2.09% | 1.36% |
| Outer barrel | 10.78% | 3.02% | 1.85% |
| Transition | 14.92% | 4.67% | 3.38% |
| Inner endcap | 8.87% | 2.81% | 2.02% |
| Outer endcap | 8.39% | 2.74% | 2.07% |

The response is applied **once**, directly to dressed prompt electrons, before
reco+ID and isolation. Both effective pileup noise and data correction are
already included; do not add another calorimeter or data-smearing stage.
`MomentumSmearing` remains the Delphes engine because it evaluates the
momentum-vector eta and supports a positive, mean-preserving log-normal
response. Its relative $p_T$ width is set to $r_e$, using
$\sigma_{p_T}/p_T\simeq\sigma_E/E$ for relativistic electrons at fixed direction.
Delphes keeps the dressed mass and direction fixed; finite dressed-mass
effects in this relation are neglected. This is a core-resolution proxy,
not a model of detailed bremsstrahlung tails. The 1 GeV transverse-energy
floor only regularizes the formula below the existing 4 GeV response buffer;
it is not a reconstruction acceptance cut.

The resolved card records the model and all coefficients, and
`simulation-metadata.txt` records `reco_electron_resolution_model` together
with the existing card/builder checksums. **Existing Delphes files must be
regenerated** to obtain the new response; changing analysis alone cannot fix
their electron momenta. No new event generation or Delphes rebuild is needed.
After updating the repository, rerun simulation on the saved HepMC input,
then regenerate the analysis trees and any merged outputs. For example:

```bash
source Simulation/env.sh
Simulation/run_simulation.sh /path/to/gg4l/job_000001 --overwrite
```

Without `--overwrite`, the existing checksum checks reject stale simulation
outputs. For response validation, compare reco and dressed quantities on the
same event set and account for the downstream $50<m_{ee}<106$ GeV selection,
which truncates mass-response tails.

The main output branches are:

| Branch | Content |
|---|---|
| `Particle` | Complete HepMC particle record and ancestry |
| `StableParticle` | Bare status-1 post-shower particles |
| `DressedElectron`, `DressedMuon` | Direct prompt dressed leptons |
| `RecoElectronNoIso`, `RecoMuonNoIso` | Smeared objects after reco+ID |
| `RecoElectron`, `RecoMuon` | Final objects after isolation efficiency |
| `Electron`, `Muon` | Unmodified generic Delphes diagnostic objects |
| `Weight.Weight` | Ordered HepMC weights, including source-ID marker pair |
| `HasTwoRecoElectronsTwoRecoMuons` | Technical multiplicity marker only |

The marker imposes no charge, mass, pT ordering, or off-shell requirement. The
downstream analysis defines the `reconstructed` flag.

## Run

Initialize the same external ROOT environment used for installation, then
source the generated Delphes paths:

```bash
source Simulation/env.sh
```

The runner accepts a direct HepMC2/3 ASCII file regardless of its basename:

```bash
Simulation/run_simulation.sh /path/to/output.events.hepmc3 --process gg4l
```

It also accepts a generation job directory containing exactly one nonempty
`*.hepmc`, `*.hepmc2`, or `*.hepmc3` file, or a campaign containing completed
`jobs/job_*` directories:

```bash
Simulation/run_simulation.sh /path/to/gg4l/job_000001
Simulation/run_simulation.sh /path/to/qqZZ/campaign --output-root /path/to/simulation
```

Compressed HepMC input is not passed directly to Delphes. Decompress it first.
The file header determines whether `DelphesHepMC2` or `DelphesHepMC3` is used;
the extension is only a fallback.

For every input, the runner:

- infers any supported process from adjacent `run-metadata.txt`, unless explicitly
  supplied with `--process`;
- obtains a deterministic Delphes seed from generation metadata, the directory
  name, or a deterministic fallback;
- generates a private resolved card with `WeightScale 1.0`;
- counts all HepMC `E` records;
- requires the Delphes tree to contain exactly that many entries;
- validates required branches and leaves, including the complete HepMC weight
  vector needed for downstream source-event matching;
- re-hashes the HepMC input after processing so a file changed in flight can
  never receive a `SUCCESS` marker;
- records complete simulation metadata, including SHA-256 digests of its HepMC
  input and Delphes output and the hashes of adjacent generation/alignment
  metadata when available; and
- writes `SUCCESS` only after every check passes.

Each output has a persistent sibling lock file. A worker holds its advisory
lock while building a complete private directory on the same filesystem and
publishes that directory by rename. Concurrent workers fail cleanly. With
`--overwrite`, a failed replacement preserves the earlier result and retains
the failed private directory for diagnosis.

Default output layout:

```text
job_000001_seed1002/
  events.hepmc
  EVNT.pool.root
  LHE.TXT.tar.gz
  events.matched.lhe.gz
  run-metadata.txt
  lhe-contract-metadata.json
  alignment-metadata.json
  delphes_ATLAS/
    delphes.root
    delphes.log
    delphes_card_ATLAS_resolved.tcl
    simulation-metadata.txt
    SUCCESS
```

`--max-events` and `--max-files` are intended for simulation smoke tests.
An output truncated with `--max-events` cannot be passed to the strict analysis
reducer, which requires the generation, alignment, simulation-input, and
simulation-output event counts to agree exactly.

## Tests

The pure-Python tests verify the resolved card structure, direct-lepton origin
policy, separate response stages, ECAL resolution values and energy dependence,
eta boundaries, jet configuration, bounded efficiencies, and continuity at
every efficiency pT knot:

```bash
uv run --frozen --extra test python -m pytest -q Simulation/tests
```

An end-to-end Delphes smoke test additionally requires the external ROOT
environment and a small generated HepMC fixture.
