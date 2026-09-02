# Delphes dressed and reconstructed event simulation

This directory converts Pythia-showered HepMC events from the ATLAS generation
step into a Delphes ROOT tree. It preserves one output entry for every input
HepMC event and stores both dressed-particle and reconstructed-lepton views.
No fiducial or off-shell analysis selection is applied here.

The generation samples are direct `e-e+mu-mu+` final states:

- `gg4l`: the full gluon-initiated process, including Higgs, continuum, and
  their interference;
- `qqZZ`: the quark-initiated four-lepton process.

Both samples are already generated in the desired decay channel. Therefore
the simulation applies an identity event-weight scale of exactly `1.0` to both
processes. In particular, no Higgs branching fraction is applied to `gg4l`.
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
A dressed lepton must:

- be an electron or muon with HepMC status 1;
- descend from a W, Z, or virtual photon with mass above 5 GeV;
- have no hadron-decay ancestor;
- not come through an intermediate tau decay.

Eligible stable photons must have no hadron-decay ancestor. Each is assigned
at most once, to the nearest eligible lepton within `deltaR < 0.1`; there is no
photon-pT threshold. Mother fields `M1` and `M2` are treated as two indices,
not as an inclusive array interval. Ancestry walks are cycle-safe and stop at
incoming quarks or gluons so the beam proton does not make hard-process
leptons appear nonprompt.

The complete shower record remains in `Particle`, bare stable particles are in
`StableParticle`, and the dressed objects are in `DressedElectron` and
`DressedMuon`.

## Reconstructed response

Dedicated reconstructed collections begin from the dressed prompt leptons and
apply, in order:

1. the Delphes ATLAS-like momentum-resolution formula;
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
These are phenomenology-level Run-2 H4l proxies, not official Run-3 detector
calibrations. The setup has no pileup, nonprompt/fake-lepton model, or charge
misidentification.

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

- infers `gg4l` or `qqZZ` from adjacent `run-metadata.txt`, unless explicitly
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
policy, separate response stages, jet configuration, bounded efficiencies, and
continuity at every pT knot:

```bash
uv run --frozen --extra test python -m pytest -q Simulation/tests
```

An end-to-end Delphes smoke test additionally requires the external ROOT
environment and a small generated HepMC fixture.
