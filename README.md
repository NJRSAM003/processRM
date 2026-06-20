# processRM

> **RM Synthesis Pipeline Orchestrator**
> RM-synthesis made simple

A config-driven RM (Faraday Rotation Measure) synthesis pipeline for the **ilifu HPC cluster**. processRM is a **wrapper around the [RM-Tools](https://github.com/CIRADA-Tools/RM-Tools) package by CIRADA**, optimised for true SLURM-array parallelism on ilifu compute nodes (the base RM-Tools workflow assumes a single-machine multiprocessing model).

---

## Features

- **INI-style config file** (`[data]`, `[chunking]`, `[rmsynth]`, `[rmclean]`, `[slurm]`, `[merge]`)
- **Two-phase workflow** — `-F` builds a config you can review, `-R` runs it
- **SLURM array parallelism** — chunks distributed across compute nodes
- **CARTA region-file support** — drop in a `.crtf` or `.ds9` region (pixel or world); each box becomes an independent processing branch under `region<N>/`
- **Cube structural validation** — refuses 3D cubes, checks that the freqlist line count matches the cube's frequency-axis length, and auto-transposes `(RA,DEC,STOKES,FREQ)` cubes back into the expected order (on a local copy, never the original)
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
├── region_parser.py          # CARTA region-file parser (CRTF/DS9, pixel/world)
├── cube_validator.py         # NAXIS / axis-order / freqlist checks + auto-transpose
├── default_config.txt        # Annotated default config template
├── setup.sh                  # Installer — adds processRM to PATH
├── templates/                # Per-stage scripts (copied into workdir)
│   ├── create_subimage.py
│   ├── create_subimage_rmsy_cube.py
│   ├── run_parallel_rmsy.py
│   └── merge_image_parts.py
├── aux_scripts/
│   └── fullSummary           # Status monitor
└── container/                # Singularity container
    ├── download_container.sh  # Fetches rm-env.sif from GitHub Release
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

### Step 2 — Install processRM into your PATH and download the container

This one command appends a few lines to your `~/.bashrc` (so `processRM` is callable from anywhere) AND downloads the prebuilt `rm-env.sif` Singularity container into `~/processRM/container/`:

```bash
cd ~/processRM
./setup.sh
source ~/.bashrc
```

Verify with:

```bash
processRM --help
```

processRM will use `~/processRM/container/rm-env.sif` by default. You can leave it there, or move it to a project-shared location and either edit `[slurm] rm_container` in your config or pass `--rm-container <path>` to `processRM`. See [`container/README.md`](container/README.md) for more.

### Step 3 — Move to your working directory

`cd` into the directory that contains (or will contain) your FITS cube. All pipeline outputs land here.

```bash
cd /idia/projects/<your-project>/<your-workdir>
```

### Step 4 — BUILD: generate a config from your inputs

Pass a **full-Stokes IQUV radio-continuum cube** plus a frequency list. processRM extracts Stokes I, Q, and U from it internally — standalone Q/U inputs are not accepted. Optionally pass a CARTA region file (`-r`) so each box becomes an independent processing branch.

```bash
# Whole cube
processRM -F mycube_IQUV.fits -f mycube.freqlist.txt --chunks 100

# One or more boxed regions (CRTF or DS9, pixel or world)
processRM -F mycube_IQUV.fits -f mycube.freqlist.txt -r myregions.crtf --chunks 100
```

This step **only writes `myconfig.txt`** (and prints a preview of the chunk geometry and detected regions). It runs cube structural checks up front — 3D cubes, mismatched freqlist lengths, and unsupported region shapes will fail here before any SLURM job is created. Open `myconfig.txt` and review the values.

### Step 5 — RUN: materialise the workdir and generate `submit_pipeline.sh`

```bash
processRM -R myconfig.txt          # set up workdir; submit manually
processRM -R myconfig.txt -s       # set up workdir AND submit immediately
```

RUN symlinks the inputs into the workdir, copies the per-stage scripts in, re-runs cube validation inside the container (auto-transposing `(RA,DEC,STOKES,FREQ)` cubes if needed), and writes `submit_pipeline.sh` plus `processRM_orchestrate.sbatch`. Without `-s`, submit later with:

```bash
./submit_pipeline.sh
```

### Step 6 — Monitor progress

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
| `[data]` | Input FITS file(s), frequency list, and optional CARTA region file (overrides `crop`/`pointing` when set) |
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
