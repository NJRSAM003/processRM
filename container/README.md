# RM-env Singularity Container

This directory holds the Singularity definition and helper script for **rm-env.sif** — the container that bundles Python + RM-Tools for the processRM pipeline on ilifu.

**Made by Amani — Made to make RM-synthesis easier**
**(integrated with ilifu)**

---

## You don't need to do anything here

When you ran `./setup.sh` in the main install step, the prebuilt `rm-env.sif` was already downloaded into this directory. processRM uses it automatically.

This document only covers the rare cases below.

---

## What's inside rm-env.sif

- Python 3.11
- RM-Tools (`rmsynth3d`, `rmclean3d`)
- `fits2idia` (FITS → IDIA HDF5 for CARTA)
- numpy, scipy, astropy, matplotlib, h5py

---

## (Optional) Re-download or pin a specific version

`./download_container.sh` always fetches the latest published release:

```bash
cd ~/processRM/container/
./download_container.sh           # latest release
./download_container.sh v1.0      # pin a specific version
```

Or download manually:

```bash
wget https://github.com/NJRSAM003/processRM/releases/latest/download/rm-env.sif
```

---

## (Optional) Move the container elsewhere

By default processRM uses `~/processRM/container/rm-env.sif`. If you want to share the container across your project (so other users on the same project don't each have to redownload it), move it once:

```bash
mv ~/processRM/container/rm-env.sif /idia/projects/<your-project>/containers/rm-env.sif
```

Then either edit `[slurm] rm_container` in your `myconfig.txt`, or pass `--rm-container <path>` to `processRM` when generating new configs.

---

## Verify the container works

```bash
singularity inspect ~/processRM/container/rm-env.sif
singularity exec ~/processRM/container/rm-env.sif rmsynth3d --help
singularity exec ~/processRM/container/rm-env.sif rmclean3d --help
singularity exec ~/processRM/container/rm-env.sif fits2idia --help
```

`singularity inspect` works on the login node; the `singularity exec` calls must run on a compute node (use `small-sesh` or similar).

