# Compact matched analysis tree

`build_analysis_tree.py` combines one retained LHE file with the corresponding
Delphes ROOT file.  The output contains the LHE, dressed-particle, and
reconstruction-level four-lepton descriptions in one `Events` tree, with one
row for every retained HepMC event.

The reducer does not apply a truth-level fiducial definition and does not skim
events.  In particular, an event with no reconstructed four-lepton candidate
is retained with finite LHE/dressed information, false reconstruction masks,
and `NaN` reconstruction-level floating-point fields.

## Event matching contract

Generation injects two nonphysics detailed LHE weights before showering:
`AUX_OAP_EVENT_ID` contains the positive source LHE ordinal and
`AUX_OAP_EVENT_UNIT` contains one. Pythia applies any shower factor to both, so
their ratio remains an integer even when the nominal event weight is negative
or zero. The generation alignment step reads the named HepMC weight schema,
writes the matched LHE events in HepMC order, and records both marker indices
under the `named-weight-id-v1` contract. Delphes preserves the ordered weight
values in `Weight.Weight`, although it does not preserve their names.

For every candidate row, the reducer decodes the marker ratio independently
from the matched LHE and Delphes weight vectors and requires the two integer
IDs to agree exactly. It also verifies that:

1. the LHE, HepMC metadata, and Delphes event counts are identical;
2. every source ID is positive and unique, and the complete source-ID sequence
   has the SHA-256 digest recorded during alignment;
3. every Delphes weight vector has the recorded HepMC weight-schema length;
4. `Event.Number` contains exactly one value per Delphes row; and
5. those event numbers form a unique, contiguous, unit-step sequence.

The first event number is inferred (the ATLAS wrapper normally requests
`firstEvent=1`).  Any missing, duplicated, or reordered event makes the job
fail and the temporary output is deleted.  A missing processing-stage event is
never turned into `reconstructed=false`, since that would mistake a workflow
failure for detector inefficiency.

The stable logical key is

```text
(campaign_id, sample_code, job_id, source_event_id)
```

with `sample_code=0` for gg4l and `sample_code=1` for qqZZ.  Two deterministic
BLAKE2b-128 words, `event_uid_hi` and `event_uid_lo`, make that key convenient
to carry through later merges. `lhe_event_index` remains the matched-file
ordinal and is only a diagnostic. Merging must preserve the source identity;
it must not replace it with a file-order-based event ID. Reordering or removing
events therefore does not change the UID of a surviving source event.

The alignment sequence digest is defined exactly as SHA-256 over consecutive
positive source IDs encoded as unsigned 64-bit big-endian words (8 bytes per
event), with no delimiter. A missing marker, changed marker order, duplicate,
or row shift fails the job; positional recovery is never attempted.
The HepMC2 decoder uses the alignment calibration
`max(1e-7, 5e-8 * abs(ratio))`, requires that tolerance to remain below 0.25,
and limits a source ID to 1000000.

## Run

From the repository root, after installing the project environment:

```bash
python Analysis/build_analysis_tree.py \
  /path/to/job/events.matched.lhe.gz \
  /path/to/job/delphes_ATLAS/delphes.root \
  --sample gg4l \
  --job-id 17 \
  --campaign-id 20260902 \
  --generation-metadata /path/to/job/run-metadata.txt \
  --lhe-contract-metadata /path/to/job/lhe-contract-metadata.json \
  --alignment-metadata /path/to/job/alignment-metadata.json \
  --simulation-metadata /path/to/job/delphes_ATLAS/simulation-metadata.txt \
  --output /path/to/job/analysis.root
```

The four metadata inputs are mandatory. Before opening the event streams, the
reducer validates the actual matched-LHE and Delphes SHA-256 digests; the
HepMC digest shared by alignment and simulation; the generation and alignment
metadata digests recorded by simulation; and the process, generation seed,
run number, AthGeneration release, first event, alignment contract, and event
counts across all stages. The LHE-contract metadata hash, process, phase-space
bounds, and requested count are validated against generation and alignment as
well. This rejects accidentally mixing files from two jobs even when their
entry counts happen to agree. Paths may contain `=`.

The embedded `analysis_metadata` object also records SHA-256 hashes for the
reducer, the four local `offshell_production` source modules, `pyproject.toml`,
and `uv.lock`. These hashes identify the exact analysis implementation and
dependency lock used without relying on repository state.

Both plain and gzip-compressed LHE files are supported by `pylhe`. Input is
processed in Delphes chunks (`--step-size "50 MB"` by default), while the LHE
iterator advances once per output row.  Use `--overwrite` only when replacing
a known job output. An adjacent exclusive lock prevents two reducers from
targeting the same output concurrently. Each reducer writes a unique partial
file and publishes it atomically. The hidden lock file remains beside the
output so every retry locks the same inode; only the advisory lock is released
when a run ends. A failed run deletes its partial file. Without `--overwrite`,
publication is an atomic no-replace operation; with it, an existing completed
output is atomically replaced. The output path may not alias either event
input or any of the four metadata inputs.

## Event schema

The ROOT file contains an `Events` tree and a one-row `Runs` tree.  Every
`Events` branch is scalar.

Identity and provenance fields include:

- `campaign_id`, `sample_code`, `job_id`, `source_event_id`, and the matched
  ordinal `lhe_event_index`;
- `event_uid_hi` and `event_uid_lo`;
- `hepmc_event_number` and `delphes_event_number` (both originate from the
  validated Delphes `Event.Number` under the current contract);
- `hepmc_entry` and `delphes_entry`, retained as diagnostics rather than keys;
- `has_lhe`, `has_hepmc`, and `has_delphes`.

`weight_lhe` is the authoritative, unmodified `float64` nominal generator
weight. Negative weights and every zero-weight event that survives showering
are retained. Pythia itself may skip or retry an LHE trial before the matched
HepMC stream is formed. The reducer never takes an absolute value, drops a
negative-weight event, or rescales weights so their sum equals the event count.
`Runs` records positive, negative, and zero counts and
`sumw`, `sumw2`, and `sumabsw`. `weight_delphes`,
`cross_section_pb_delphes`, and `cross_section_error_pb_delphes` preserve the
corresponding Delphes event fields as diagnostics; they do not replace or
modify `weight_lhe`.

When the LHE contains PDF, scale, or other alternative weights, a separate
`LHEWeights` tree has one row per `Events` row, the same stable event identity,
and a fixed-size `values` array. The exact lexicographically ordered weight-ID
mapping is embedded in `analysis_metadata`. Every event must expose the same
set of IDs; schema drift fails the job instead of shifting weight columns. The
two `AUX_OAP_*` matching weights are validated and then excluded from this
physics-systematics tree and its count.

For each `lhe`, `dressed`, and `reco` namespace the tree stores:

- charge-resolved electron and muon multiplicities;
- candidate/topology and Born-projection validity flags;
- raw and Born-projected `(E, px, py, pz)` for `e-`, `e+`, `mu-`, and `mu+`;
- projection diagnostics;
- `m_Z1` (dimuon), `m_Z2` (dielectron), `m_ZZ`, `y_ZZ`, and `pt_ZZ`
  kinematics;
- the local positive-lepton helicity coordinates used by the harmonic
  decomposition; and
- the standard five-angle observables.

At all three levels, `Z1` is the dimuon system and `Z2` is the dielectron
system.  Each Born projection is calculated from the four-vectors at that same
level; the LHE transformation is never reused for dressed or reconstructed
objects.

`reco_candidate` records whether exactly one lepton of each requested flavor
and charge is present, independently of analysis cuts.  The detailed
`reco_cut_*` masks and `reco_pass_selection` then apply the strict off-shell
selection.  `reconstructed` is an explicit compatibility alias for
`reco_pass_selection`, including `m4l > 180 GeV` with no upper analysis cut.

Unavailable continuous quantities use IEEE `NaN`, guarded by the corresponding
candidate and projection masks.  Downstream ML preprocessing may impute a
finite value only in a derived dataset and should retain the masks as inputs;
the canonical ROOT file does not use artificial values such as `-999`.

The `Runs` tree contains topology and projection-valid counts for all three
levels, the selected-event count, generation and Delphes seeds, run and release
identifiers, signed-weight summaries at LHE and Delphes levels, and Delphes
cross-section diagnostics. A job with no valid LHE or dressed candidate or no
valid Born projection at either of those levels is rejected. Extra prompt
leptons invalidate the direct-2e2mu candidate and are counted; the reducer does
not invent a pairing rule.

The generation LHE contract supplies the authoritative `IDWTUP=-4` sample-mean
normalization. `effective_filtered_cross_section_pb` is
`normalization_sumw_accepted_pb / normalization_generated_lhe_events`; its
`effective_filtered_cross_section_mc_error_pb` is the finite-LHE Monte Carlo
standard error with rejected events treated as zero. `Runs` carries both
derived values and the generated/accepted counts,
signed sums, and squared-weight sums needed to pool jobs before recomputation.
The signed and absolute phase-space efficiencies are diagnostics.

First/minimum/maximum/final Delphes cross-section values remain separate,
signed diagnostics so time evolution or corruption cannot silently collapse to
an unexplained last value. They are never multiplied by a filter efficiency to
define the sample normalization.

The `analysis_metadata` JSON object embeds normalized copies of generation,
LHE-contract, alignment, and simulation metadata plus SHA-256 and original-path
provenance for every consumed file. Thus `analysis.root` remains auditable by
itself.
