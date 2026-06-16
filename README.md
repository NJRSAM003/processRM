# processRM

> **RM Synthesis Pipeline Orchestrator — integrated with ilifu**
> Made by Amani — Made to make RM-synthesis easier

A config-driven RM (Faraday Rotation Measure) synthesis pipeline for the **ilifu HPC cluster**. processRM is a **wrapper around the [RM-Tools](https://github.com/CIRADA-Tools/RM-Tools) package by CIRADA**, optimised for true SLURM-array parallelism on ilifu compute nodes (the base RM-Tools workflow assumes a single-machine multiprocessing model).

---

## Features

- **INI-style config file** (`[data]`, `[chunking]`, `[rmsynth]`, `[rmclean]`, `[slurm]`, `[merge]`)
- **One-command pipeline submission** — `processRM -C myconfig.txt`
- **SLURM array parallelism** — chunks distributed across compute nodes
- **Resume safety** — re-running picks up where it left off after timeouts/failures
- **Per-stage timing logs** — `logs/timings.csv` tracks avg time per chunk
- **Live pipeline status** — `./fullSummary` shows progress bars, SLURM jobs, errors
- **Singularity containers** — no host venv dependencies; fully reproducible
- **CARTA-ready outputs** — auto-fixes the rmsynth3d `CRVAL4=0` bug

---

## Repo layout

```
processRM/
├── processRM.py              # Main orchestrator (CLI entry point)
├── config_parser.py          # Config validation/parser
├── default_config.txt        # Annotated default config template
├── setup.sh                  # Installer — adds processRM to PATH
├── templates/                # Per-stage scripts (copied into workdir)
│   ├── create_subimage.py
│   ├── create_subimage_rmsy_cube.py
│   ├── run_parallel_rmsy.py
│   └── merge_image_parts.py
├── aux_scripts/
│   └── fullSummary           # Status monitor
└── container/                # Singularity container def + build scripts
    ├── rm-env.def
    ├── build_container.sh
    ├── inspect_rm_env.sh
    └── README.md
```

---

## Quick start (on ilifu)

```bash
# 1. Clone repo on ilifu
git clone https://github.com/NJRSAM003/processRM.git ~/processRM
cd ~/processRM && ./setup.sh && source ~/.bashrc

# 2. (One-time) Build the rm-env container
cd ~/processRM/container && ./build_container.sh

# 3. Generate a config from your FITS file
cd /path/to/your/working/directory
processRM -F NGC1097_contcube.fits -f NGC1097_contcube.freqlist.txt --chunks 100

# 4. Submit the pipeline
./submit_pipeline.sh

# 5. Monitor progress
./fullSummary           # one-shot
./fullSummary --watch   # live updates
```

---

## Config sections (overview)

| Section | Purpose |
|---|---|
| `[data]` | Input FITS file(s) and frequency list |
| `[chunking]` | How the cube is split for parallel processing |
| `[rmsynth]` | Parameters for `rmsynth3d` (Faraday depth range, weighting, RMSF) |
| `[rmclean]` | Parameters for `rmclean3d` (threshold, iterations, gain, parallelism) |
| `[slurm]` | SLURM-specific config (account, partition, memory, containers) |
| `[merge]` | Final merging/assembly settings |

See [`default_config.txt`](default_config.txt) for the fully-annotated reference.

---

## License

[GNU General Public License v3.0](LICENSE)

## Credits

- **[RM-Tools](https://github.com/CIRADA-Tools/RM-Tools) by CIRADA** — the underlying `rmsynth3d` / `rmclean3d` binaries that processRM wraps. processRM does not reimplement the science; it orchestrates RM-Tools for HPC use.
- **processMeerKAT** team at IDIA — Similar Architecture in comparison to the [processMeerKAT pipeline](https://github.com/idia-astro/pipelines) built by IDIA
- **Lennart Heino** — the foundational code for compatibility with parallelism on the ilifu cluster
