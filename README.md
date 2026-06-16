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

Each step below should be run **one at a time**. Read what it does before pasting it into your terminal — these commands modify your shell config, build a container, and submit SLURM jobs.

### Step 1 — Clone the repo

SSH into ilifu, then clone processRM into your home directory:

```bash
git clone https://github.com/NJRSAM003/processRM.git ~/processRM
```

### Step 2 — Install processRM into your PATH

This appends a few lines to your `~/.bashrc` so `processRM` is callable from anywhere:

```bash
cd ~/processRM
./setup.sh
source ~/.bashrc
```

Verify with:

```bash
processRM --help
```

### Step 3 — Download the rm-env Singularity container (one-time)

The container that holds Python + RM-Tools + dependencies is pre-built and attached to every processRM release. **You do not need to build it yourself.** Just download:

```bash
cd ~/processRM/container
wget https://github.com/NJRSAM003/processRM/releases/latest/download/rm-env.sif
```

Then move it to a SLURM-readable location, e.g.:

```bash
mv rm-env.sif /idia/projects/<your-project>/containers/rm-env.sif
```

Open `myconfig.txt` (created in Step 5) and set `[slurm] rm_container = '...'` to that path.

See [`container/README.md`](container/README.md) for more options (pinning a specific version, rebuilding from source, etc.).

### Step 4 — Move to your working directory

`cd` into the directory that contains (or will contain) your FITS cube. All pipeline outputs land here.

```bash
cd /idia/projects/<your-project>/<your-workdir>
```

### Step 5 — Generate the pipeline config

Either pass a full Stokes cube **or** separated Q + U cubes, plus a frequency list:

```bash
processRM -F mycube_IQUV.fits \
          -f mycube.freqlist.txt \
          --chunks 100
```

This creates `myconfig.txt`, `submit_pipeline.sh`, and copies the per-stage scripts into the current directory. Open `myconfig.txt` and review the values before submitting.

### Step 6 — Submit the pipeline

```bash
./submit_pipeline.sh
```

This validates the containers, generates the SLURM sbatch files, and submits the array jobs.

### Step 7 — Monitor progress

```bash
./fullSummary
```

Add `--watch` for live updates every 10 seconds, or `--errors` for the full error report:

```bash
./fullSummary --watch
./fullSummary --errors
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

[MIT License](LICENSE)

## Credits

- **[RM-Tools](https://github.com/CIRADA-Tools/RM-Tools) by CIRADA** — the underlying `rmsynth3d` / `rmclean3d` binaries that processRM wraps. processRM does not reimplement the science; it orchestrates RM-Tools for HPC use.
- **processMeerKAT** team at IDIA — Similar Architecture in comparison to the [processMeerKAT pipeline](https://github.com/idia-astro/pipelines) built by IDIA
- **Lennart Heino** — the foundational code for compatibility with parallelism on the ilifu cluster
