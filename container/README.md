# RM-env Singularity Container

This directory holds the Singularity definition for **rm-env.sif** — the container that bundles Python + RM-Tools for the processRM pipeline on ilifu.

**Made by Amani — Made to make RM-synthesis easier**
**(integrated with ilifu)**

---

## You do not need to build this container

A pre-built `rm-env.sif` is published with every processRM release on GitHub. **Just run `./download_container.sh`.** The recipe `rm-env.def` lives here only so the container can be rebuilt or modified if needed.

---

## What's inside rm-env.sif

- Python 3.11
- RM-Tools (`rmsynth3d`, `rmclean3d`)
- `fits2idia` (FITS → IDIA HDF5 for CARTA)
- numpy, scipy, astropy, matplotlib, h5py

---

## Step 1 — Download the container on ilifu

After cloning the processRM repo, run:

```bash
cd ~/processRM/container/
wget https://github.com/NJRSAM003/processRM/releases/latest/download/rm-env.sif
```

(The `latest` URL always resolves to the most recent published release. To pin a specific version, use `releases/download/v1.0/rm-env.sif`.)

Verify the file:

```bash
ls -lh rm-env.sif
singularity inspect rm-env.sif
```

You should see a ~370MB file and metadata showing `Author: Amani` etc.

---

## Step 2 — Move it to a SLURM-readable location

SLURM compute nodes need to be able to read the `.sif`. Anywhere under `/idia/projects/<your-project>/` or `/scratch/<user>/` works:

```bash
mv rm-env.sif /idia/projects/<your-project>/containers/rm-env.sif
```

(`/idia/software/containers/` is admin-only — don't try there.)

---

## Step 3 — Point your config at it

In the working-directory config file processRM generates (e.g. `myconfig.txt`), set:

```ini
[slurm]
rm_container = '/idia/projects/<your-project>/containers/rm-env.sif'
```

That's it. Every subsequent SLURM job processRM submits will `singularity exec` this container.

---

## Test the container on ilifu

```bash
singularity exec /idia/projects/<your-project>/containers/rm-env.sif rmsynth3d --help
singularity exec /idia/projects/<your-project>/containers/rm-env.sif rmclean3d --help
singularity exec /idia/projects/<your-project>/containers/rm-env.sif fits2idia --help
```

If all three print usage text, the container is ready and processRM will use it for every SLURM job it submits.

---

## (Maintainers only) Rebuilding the container

Most users will never do this. ilifu does **not** allow `singularity build` (no sudo, no fakeroot support, login/transfer nodes block it outright), so you must build elsewhere:

```bash
sudo singularity build rm-env.sif rm-env.def              # local Linux machine with sudo
# OR
singularity remote login && \
singularity build --remote rm-env.sif rm-env.def          # Sylabs Cloud, no local install
```

Then attach the new `rm-env.sif` to a new GitHub Release (drag-drop in the Release UI).
