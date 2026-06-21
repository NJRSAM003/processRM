# RM-env Singularity Container

This directory holds the Singularity definition and helper script for **rm-env.sif** — the container that bundles Python + RM-Tools for the processRM pipeline on ilifu.

**RM-synthesis made simple**

---

## You don't need to do anything here

When you ran `./setup.sh` in the main install step, the prebuilt `rm-env.sif` was already downloaded into this directory. processRM uses it automatically.

This document only covers the rare cases below.

---

## What's inside rm-env.sif

- Python 3.11 (system, from the `python:3.11-slim` base image)
- **RM-Tools-sigma** — a lightly modified RM-Tools that adds 2D per-pixel
  noise-map support (`rmsynth3d -N`, `rmclean3d -N -c -<sigma>`) and
  ships the `RMtools_3D.make_noise_map` module
- **PyBDSF (`bdsf`)** — pip-installed, used by `make_noise_map` to estimate per-channel RMS
- `fits2idia` (FITS → IDIA HDF5 for CARTA)
- numpy, scipy, astropy, matplotlib, h5py

---

## Upgrading to a newer container

When a new container is published as a GitHub Release, your local
`rm-env.sif` is **not** updated automatically. To pull the new one:

```bash
cd ~/processRM/container/
rm rm-env.sif                     # delete the old image
./download_container.sh           # fetches whatever Release is marked "Latest"
```

Pin a specific version instead:

```bash
./download_container.sh v2.0-sigma
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

## (Maintainer) Rebuild rm-env.sif from the recipe

The recipe (`rm-env.def`) bundles the **RM-Tools-sigma** source from a tree
that you must stage next to the recipe before building. The %files section
uses a *relative* path so the build works regardless of whether you run it
with `sudo`, `--fakeroot`, or `--remote` (a bare `~` would expand to `/root`
under sudo and fail).

```bash
cd ~/Documents/processRM/container/
ln -s ~/Downloads/RM-Tools-sigma RM-Tools-sigma     # or: cp -r ...
sudo singularity build rm-env.sif rm-env.def        # ~10-20 min
rm RM-Tools-sigma                                    # tidy up
```

Then attach the resulting `rm-env.sif` to a new GitHub Release marked
"Latest" — see the project root README for the release steps.

---

## Verify the container works

```bash
singularity inspect ~/processRM/container/rm-env.sif
singularity exec ~/processRM/container/rm-env.sif rmsynth3d --help
singularity exec ~/processRM/container/rm-env.sif rmclean3d --help
singularity exec ~/processRM/container/rm-env.sif fits2idia --help
```

`singularity inspect` works on the login node; the `singularity exec` calls must run on a compute node (use `small-sesh` or similar).

