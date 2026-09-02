# Copyright (C) 2002-2026 CERN for the benefit of the ATLAS collaboration

# This local job option is derived from ATLAS PMG DSID 602686:
# https://gitlab.cern.ch/atlas-physics/pmg/mcjoboptions/-/blob/
# 1be4bbe9601b521451b2c22523c0377223b61b94/602xxx/602686/
# mc.PhPy8_NNPDF30_gg4l_m4l_70_3000.py

# -----------------------------------------------------------------------------
# EVGEN configuration
# -----------------------------------------------------------------------------
evgenConfig.description = (
    "POWHEG gg->4l full (Higgs + continuum + interference) production, "
    "exclusive 2e2mu, 150 < m4l < 3000 GeV"
)
evgenConfig.keywords = ["SM", "Higgs"]
evgenConfig.contact = ["andrej.saibel@cern.ch", "guglielmo.frattari@cern.ch"]
evgenConfig.generators = ["Powheg"]
evgenConfig.nEventsPerJob = 50

# -----------------------------------------------------------------------------
# Load ATLAS defaults for POWHEG-BOX-RES gg4l.
# -----------------------------------------------------------------------------
include("PowhegControl/PowhegControl_gg4l_Common.py")
PowhegConfig.mass_b = 0
PowhegConfig.proc = "ZZ"
PowhegConfig.contr = "full"
PowhegConfig.ubexcess_correct = 0

# gg4l directly supports only the exclusive 2e2mu channel for ZZ production.
# Using ll/ll would activate the gg4l_emu2all LHE postprocessor and make the
# output flavour-inclusive, which is not wanted in this project.
PowhegConfig.vdecaymodeV1 = 11
PowhegConfig.vdecaymodeV2 = 13
PowhegConfig.mllmin = 50
PowhegConfig.mllmax = 200
PowhegConfig.m4lmin = 150
PowhegConfig.m4lmax = 3000

# Integration settings retained from the validated PMG job option.
PowhegConfig.ncall1 = 4000
PowhegConfig.itmx1 = 2
PowhegConfig.ncall2 = 3000
PowhegConfig.itmx2 = 2
PowhegConfig.foldcsi = 2
PowhegConfig.foldy = 2
PowhegConfig.foldphi = 5
PowhegConfig.nubound = 75000
PowhegConfig.icsimax = 2
PowhegConfig.iymax = 2
PowhegConfig.xupbound = 2
PowhegConfig.storemintupb = 1
PowhegConfig.fastbtlbound = 1
PowhegConfig.ncall1btlbrn = 50000
PowhegConfig.ncall2btlbrn = 100000
PowhegConfig.manyseeds = 1
PowhegConfig.parallelstage = 4
PowhegConfig.allrad = 1
PowhegConfig.withdamp = 1
PowhegConfig.m4l_sampling = 2
PowhegConfig.massiveloops = 1

PowhegConfig.mu_F = [1.0, 0.5, 0.5, 0.5, 2.0, 2.0, 2.0, 1.0, 1.0]
PowhegConfig.mu_R = [1.0, 0.5, 1.0, 2.0, 0.5, 1.0, 2.0, 0.5, 2.0]
PowhegConfig.PDF = 260000

# -----------------------------------------------------------------------------
# Generate events.
# -----------------------------------------------------------------------------
PowhegConfig.generate()

# Tag the exact hard events consumed by Pythia. The helper also enforces the
# requested hard-process bounds before showering and rewrites the TXT sidecar
# created inside PowhegConfig.generate().
from offshell_lhe_contract import prepare_lhe_for_shower

prepare_lhe_for_shower(
    runArgs.inputGeneratorFile,
    runArgs.outputTXTFile,
    process="gg4l",
    requested_events=runArgs.maxEvents,
    min_m4l=150.0,
    max_m4l=3000.0,
)

# -----------------------------------------------------------------------------
# Pythia8 showering with the A14 NNPDF2.3 tune.
# -----------------------------------------------------------------------------
include("Pythia8_i/Pythia8_A14_NNPDF23LO_EvtGen_Common.py")
include("Pythia8_i/Pythia8_Powheg_Main31.py")
genSeq.Pythia8.Commands += ["Powheg:NFinal = -1"]
