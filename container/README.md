# RM-env Singularity Container

This directory contains the Singularity definition and build scripts for the **rm-env.sif** container — a reproducible environment that mirrors your `~/myvenvs/RM-env/` on ilifu.

**Made by Amani — Made to make RM-synthesis easier**
**(integrated with ilifu)**

---

## Why a container?

Currently, processRM relies on a user-specific Python venv at `~/myvenvs/RM-env/`. This is fragile because:
- It depends on your home directory layout
- It cannot be shared between users
- Version drift between environments is hard to track

A Singularity container fixes all of this. The pipeline can use `singularity exec rm-env.sif rmsynth3d ...` and works for anyone with access to the `.sif` file.

---

## What's inside rm-env.sif

- Python 3.11
- RM-Tools (`rmsynth3d`, `rmclean3d`)
- numpy, scipy, astropy, matplotlib, h5py, click
- All build dependencies (gfortran, FFTW, HDF5)

---

## Step 1: Inspect your existing RM-env (optional but recommended)

On ilifu, run this to get the **exact** package versions you currently have:

```bash
cd ~/Documents/processRM/container/
./inspect_rm_env.sh
```

Copy the output and paste it into the `%post` section of `rm-env.def` if you want to pin exact versions.

---

## Step 2: Build the container

```bash
cd ~/Documents/processRM/container/
./build_container.sh
```

This will try (in order):
1. `singularity build --fakeroot rm-env.sif rm-env.def`
2. `sudo singularity build rm-env.sif rm-env.def`
3. `singularity build --remote rm-env.sif rm-env.def` (requires Sylabs Cloud account)

On ilifu, **`--fakeroot` is usually the easiest option**.

---

## Step 3: Move to a shared location

Once built, move `rm-env.sif` to a project location accessible by your SLURM jobs:

```bash
mv rm-env.sif /idia/projects/<your-project>/containers/rm-env.sif
```

---

## Step 4: Update your config

In your processRM config file, set:

```ini
[slurm]
rm_container = '/idia/projects/<your-project>/containers/rm-env.sif'
```

When `rm_container` is set, the pipeline will use the container. If empty, it falls back to `rm_venv` (your local `~/myvenvs/RM-env/`).

---

## Test the container

```bash
singularity exec rm-env.sif rmsynth3d --help
singularity exec rm-env.sif python3 -c "import astropy; print(astropy.__version__)"
```
