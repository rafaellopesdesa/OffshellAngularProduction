# UChicago Analysis Facility runtime

This directory contains the runtime and HTCondor entry points for the UChicago
ATLAS Analysis Facility (AF). `run_in_atlas_container.sh` handles ATLAS
container setup; `condor/` prepares and runs deterministic campaigns for both
the ATLAS and standalone VPolar generation backends.

## Site conventions

The following locations and setup sequence are documented by the UChicago AF:

| Location | Intended use | Persistence |
| --- | --- | --- |
| `/home/$USER` | Source, configuration, and other small files | Shared and backed up; documented 100 GB quota |
| `/data/$USER` | LHE, HepMC, EVNT, Delphes, and analysis outputs | Shared, not backed up; documented 5 TB quota |
| `/scratch` | Temporary working data on a worker | Node-local and ephemeral; retained outputs must be copied elsewhere |

Use `/data/$USER` explicitly for normal generation and end-to-end output
directories. Repository-local `Generation/runs/...` and `Workflow/runs/...`
defaults are intended only for smoke tests and other small jobs.

ATLAS Local Root Base is provided through CVMFS at
`/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase`. The documented non-interactive
container pattern is:

```bash
source /cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase/user/atlasLocalSetup.sh
setupATLAS -c alma9 -r "source /srv/payload.sh"
```

The `-r` form matters in a script: commands written after an interactive
`setupATLAS -c alma9` invocation would run in the host shell rather than in the
container.

References:

- [UChicago AF storage](https://usatlas.github.io/af-docs/uchicago/storage/)
- [ATLAS environment setup](https://cecilia-duran.github.io/2022-04_gh_usatlas_af_qst/03-atlasenv/index.html)
- [ATLAS OS containers](https://usatlas.github.io/af-docs/containers/atlas/os/)

## Analysis environment

Create the pinned Python environment from the repository root:

```bash
uv sync --frozen --extra test
source .venv/bin/activate
```

`uv sync --frozen --extra test` uses the committed lockfile, includes the test
runner used by this repository, and fails instead of silently changing the
lockfile.

## Run a repository payload

From any directory, pass a repository-local Bash payload and its arguments to
the wrapper:

```bash
/path/to/OffshellAngularProduction/UChicagoAF/run_in_atlas_container.sh \
  Generation/run_generation.sh gg4l --events 2 --seed 101
```

The wrapper:

1. locates the repository from its own path;
2. rejects payloads outside that repository;
3. enters the repository so the ATLAS container exposes it as `/srv`;
4. initializes ATLAS Local Root Base on the host; and
5. sources the payload inside the AlmaLinux 9 container, preserving its
   arguments without evaluating them in the host shell.

The payload need not be executable, but it must be a regular Bash file. It is
sourced so it can call shell functions such as `asetup`. The wrapper does not
select an Athena release and does not read or install credentials.

`ATLAS_LOCAL_ROOT_BASE` and `ATLAS_CONTAINER_OS` can be set by the caller when
testing a different CVMFS installation or container OS. Their defaults are the
documented CVMFS location and `alma9`, respectively.

## Keep stages isolated

Run Generation, Simulation, and Analysis as three separate, clean processes.
Each stage should enter only the environment it needs:

- Generation selects its pinned AthGeneration release. The project default is
  AthGeneration 23.6.41, configured by the generation payload rather than by
  this generic wrapper.
- Simulation selects the ROOT environment used to build and run the pinned
  Delphes installation.
- Analysis selects its own Python or AnalysisBase environment.

Do not stack AthGeneration, Delphes/ROOT, and AnalysisBase setup in one shell;
mixing their library paths can make results dependent on stage order. Keep all
output locations configurable. Before relying on an absolute host path such as
`/data/$USER`, verify that it is visible in the selected container; the wrapper
only relies on the documented repository mapping at `/srv`.
