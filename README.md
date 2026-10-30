# Budget prompts or delivery rules? — Stage 1 materials (SANER 2027 Registered Reports track)

Supplementary material for the Stage 1 report *Budget Prompts or Delivery Rules? A Registered Replication and
Decomposition of Budget-Awareness Gains in Coding Agents on SWE-bench Verified* (EasyChair submission 440, report v4,
2026-10-31). Everything here was written before the confirmatory data exist; the confirmatory run starts only after
in-principle acceptance. Trajectories, patches and per-slot verdicts of the prior study are released at Stage 2.

## What is where (report section in brackets)

| Path | Content |
|---|---|
| `bridge/` | The sealed pipeline of the prior study: agent wrapper, prompts, extractor, judge driver, **analysis rules** (`analysis_rules.py`, `analyze.py`), tests, tools. Its identity (SHA-256 over the top-level `*.py` and `*.json` except `pricing_registry.json`) is `d1da6e7a…`; run `python3 -I verify_seal.py` to recompute it. [§V-A, §V-C] |
| `docs/prereg/` | The prior study's pre-registration as sealed (`2026-10-18_bridge_prereg_DRAFT.md`, Chinese; "DRAFT" is the file name it was sealed under), the task selection, the two addenda, and the three seal records with file hashes. The first seal record (2026-10-20 morning) was superseded the same day by `seal2` after the pre-run fixes listed in addendum 1; the formal runs carry the `seal2` identity. [§I, §V-F] |
| `data/bridge_formal_analysis/` | The prior study's analysis output under its sealed rules (`primary_report.json`: all four arms, descriptives, labels, sensitivity analyses). [§VI, Table V] |
| `analysis/bridge_rr/` | Registered additions and the operating-characteristics simulation: `tied_fill.py` (a submitted trajectory with unknown verdict is filled with S = H), `g_interval.py` (prior-study intervals for T, Δ_H, G under the sealed and the registered fill; output `g_interval_pilot.json`), `pilot_se.py` (sampling SE under each worst-case fill vs. fill range, and the prior study's task-level joint distribution; output `pilot_se.json`), `sim_calibration.py` (generator vs. prior study at n = 100; output `sim_calibration.json`), `sim_rr.py` (full-procedure simulation at n = 394, v2.1 rules with the pilot-matched unknown pattern), `sim_table_v21.py` (Table IV rows from the simulation files: seeds 7/5/6 = none and MCAR columns, 17/15/16 = pilot column, 27 = rho 0.6 rows), `sim_rr_v2_*.json` and logs (the runs behind Table IV; `sim_table_v2.py` and `sim_rr_*.json` without `v2` are the superseded v1/v2 runs, kept for the record). [§V-C, §V-D, Table IV] |
| `docs/reviews/2026-10-19_bridge_v3_review_sim/` | The ordered latent-difficulty generator that `sim_rr.py` imports (`sim.py`, `v4/sim_v4.py`). [§V-D] |
| `rr/task_lists_2026-10-31.json` | The 499-task population split into: prior study (100), dry runs (5), week-one development task (1), the **confirmatory sample (394 = 226 + 168 by stratum)**, the 175 confirmatory tasks whose public trajectories were replayed in exploratory week one, and the 219 untouched tasks. `rr/task_lists_sha256.json` has the hash of the sorted confirmatory list. [§IV] |
| `data/d1_k2_v4/K2_final.json`, `data/d1_k3_v4/K3_final.json` | Exploratory week-one replays of the public entry's trajectories (K2: 96/100 reproduced the submitted file tree and verdict; K3: rescue-rate estimate). [§V-E] |
| `docs/d1_bridge_dryrun_report_2026-10-20.md`, `data/bridge_dev_dryrun/bridge_tasks_meta.json` | Acceptance record of the development dry run (5 tasks × 4 arms) and the sealed image/task metadata. [§V-E] |
| `data/bridge_b1_check_2026-10-20/result.json` | Docker-level check of the extractor with residual processes. [§V-E] |

## Reproducing the numbers in the report

```
python3 -I analysis/bridge_rr/g_interval.py          # needs the prior study's per-slot results (Stage 2); output file is included
python3 -I analysis/bridge_rr/sim_rr.py 1000 2000 7 --only i_T18_viaDelivery,ii_T18_beyondDelivery,vi_T12_J0.4T,ix_both_T18_J0.6T,x_reversal_T18_dHneg,iv_T8_harmH
python3 -I analysis/bridge_rr/sim_rr.py 10000 2000 5 --only iii_null
python3 -I analysis/bridge_rr/sim_rr.py 4000 2000 6 --only xiii_boundary_J0.5T,xiv_boundary_dH0,xvii_boundary_G0
python3 -I analysis/bridge_rr/sim_rr.py 1000 2000 17 --only i_T18_viaDelivery,ii_T18_beyondDelivery,vi_T12_J0.4T,ix_both_T18_J0.6T,x_reversal_T18_dHneg,iv_T8_harmH --unk pilot
python3 -I analysis/bridge_rr/sim_rr.py 10000 2000 15 --only iii_null --unk pilot
python3 -I analysis/bridge_rr/sim_rr.py 4000 2000 16 --only xiii_boundary_J0.5T,xiv_boundary_dH0,xvii_boundary_G0 --unk pilot
python3 -I analysis/bridge_rr/sim_rr.py 1000 2000 27 --only i_T18_viaDelivery,ix_both_T18_J0.6T,ii_T18_beyondDelivery,xiii_boundary_J0.5T,xvii_boundary_G0,xiv_boundary_dH0 --unk u0,pilot --rho 0.6
python3 -I analysis/bridge_rr/sim_calibration.py
python3 -I analysis/bridge_rr/sim_table_v21.py > analysis/bridge_rr/sim_table_v21.tex
```
Requirements: Python 3.11+, numpy. The simulation runs take about 15, 10 and 25 minutes on a laptop.

Local absolute paths in logs and analysis outputs were replaced by `<repo>`; no other content was changed. Sealed files are byte-identical to their seal-record hashes.

## Notes on process

The pre-registration and its addenda record the review rounds that preceded sealing, including rounds with AI assistants
(named in the documents). The registered hypotheses, rules and code are the author's responsibility.

`bridge/README.md` (documentation only, not part of the code identity) was edited after sealing; every other sealed file
matches its seal-record hash (`verify_seal.py` checks the code identity; the file hashes are in the seal records).
