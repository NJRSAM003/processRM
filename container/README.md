# RM-env Singularity Container

This directory contains the Singularity definition and build scripts for **rm-env.sif** — a reproducible container that bundles Python + RM-Tools for the processRM pipeline on ilifu.

**Made by Amani — Made to make RM-synthesis easier**
**(integrated with ilifu)**

> The paths in this guide assume you cloned processRM to `~/processRM` (the path used in the main [README](../README.md) install steps). If you cloned it elsewhere, substitute that location wherever `~/processRM` appears below.

---

## Why a container?

Without a container, processRM depends on a user-specific Python venv. This is fragile because:
- It depends on the user's home directory layout
- It cannot be shared between users
- Version drift between environments is hard to track

A Singularity container fixes all of this. The pipeline uses `singularity exec rm-env.sif rmsynth3d ...` and works for anyone with access to the `.sif` file.

---

## What's inside rm-env.sif

- Python 3.11
- RM-Tools (`rmsynth3d`, `rmclean3d`)
- numpy, scipy, astropy, matplotlib, h5py, click
- All build dependencies (gfortran, FFTW, HDF5)

---

## Step 1 — Inspect your existing RM-env (optional)

If you already have a local `RM-env` Python venv on ilifu and want to mirror its versions, run:

```bash
cd ~/processRM/container/
./inspect_rm_env.sh
```

Copy the output and paste it into the `%post` section of `rm-env.def` if you want to pin exact versions. Skip this step if you don't have an existing venv — the container's defaults are fine.

---

## Step 2 — Build the container

```bash
cd ~/processRM/container/
./build_container.sh
```

The build script tries (in order):
1. `singularity build --fakeroot rm-env.sif rm-env.def`
2. `sudo singularity build rm-env.sif rm-env.def`
3. `singularity build --remote rm-env.sif rm-env.def` (requires Sylabs Cloud account)

On ilifu, **`--fakeroot` is usually the only option that works** (sudo isn't available; remote build requires a Sylabs account).

Build typically takes 10–15 minutes.

---

## Step 3 — Move to a shared location

Once built, move `rm-env.sif` to a path that your SLURM compute nodes can read. Anywhere under `/idia/projects/<your-project>/` or `/scratch/<user>/` works — pick whatever location matches your project's conventions:

```bash
mv ~/processRM/container/rm-env.sif <destination>/rm-env.sif
```

For example:

```bash
mv ~/processRM/container/rm-env.sif /idia/projects/<your-project>/containers/rm-env.sif
```

---

## Step 4 — Point your config at it

In the working-directory config file processRM generates (e.g. `myconfig.txt`), set:

```ini
[slurm]
rm_container = '<destination>/rm-env.sif'
```

Use the **same absolute path** from Step 3.

---

## Test the container

From anywhere on ilifu:

```bash
singularity exec <destination>/rm-env.sif rmsynth3d --help
singularity exec <destination>/rm-env.sif python3 -c "import astropy; print(astropy.__version__)"
```

If both commands succeed, the container is ready and processRM will use it for every SLURM job it submits.
