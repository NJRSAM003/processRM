# RM-env Singularity Container

This directory contains the Singularity definition and build scripts for **rm-env.sif** — a reproducible container that bundles Python + RM-Tools for the processRM pipeline on ilifu.

**Made by Amani — Made to make RM-synthesis easier**
**(integrated with ilifu)**

---

## Important: you cannot build this container on ilifu

ilifu does **not** allow `singularity build`:
- The login & transfer nodes block it outright (`STOP !! You are trying to run Singularity on the login or transfer node`).
- Compute nodes do not have `newuidmap` / `newgidmap`, so `--fakeroot` fails.
- `sudo` is not available to users.
- `--remote` requires you to have logged in to Sylabs Cloud first (no token by default).

**The container must be built elsewhere and uploaded to ilifu.** Two options below.

---

## What's inside rm-env.sif

- Python 3.11
- RM-Tools (`rmsynth3d`, `rmclean3d`)
- numpy, scipy, astropy, matplotlib, h5py, click
- All build dependencies (gfortran, FFTW, HDF5)

---

## Option A — Sylabs Cloud (recommended, no local install required)

This builds the container in Sylabs' cloud and gives you the `.sif` to download. You only need a working `singularity` command on **any** machine (even your laptop) to kick it off.

1. **Sign up at https://cloud.sylabs.io/** and create an access token (Account → Access Tokens → Create New Token).
2. **Authenticate** from any machine that has singularity installed:
   ```bash
   singularity remote login
   # paste your token when prompted
   ```
3. **Build remotely:**
   ```bash
   cd <wherever you cloned processRM>/container/
   singularity build --remote rm-env.sif rm-env.def
   ```
4. **Upload to ilifu:**
   ```bash
   scp rm-env.sif amani@transfer.ilifu.ac.za:/idia/projects/<your-project>/containers/
   ```

---

## Option B — Local Linux machine with sudo

If you have a personal Linux machine where you can run `sudo`:

1. **Install singularity-ce** following https://docs.sylabs.io/guides/latest/admin-guide/installation.html
2. **Build locally:**
   ```bash
   cd <wherever you cloned processRM>/container/
   sudo singularity build rm-env.sif rm-env.def
   ```
3. **Upload to ilifu:**
   ```bash
   scp rm-env.sif amani@transfer.ilifu.ac.za:/idia/projects/<your-project>/containers/
   ```

Build typically takes 10–15 minutes either way.

---

## Step 3 — Point your config at the uploaded container

In the working-directory config file processRM generates (e.g. `myconfig.txt`), set:

```ini
[slurm]
rm_container = '/idia/projects/<your-project>/containers/rm-env.sif'
```

Use the **same absolute path** you uploaded to. Anywhere under `/idia/projects/<your-project>/` or `/scratch/<user>/` works.

---

## Test the container (run this on ilifu)

```bash
singularity exec /idia/projects/<your-project>/containers/rm-env.sif rmsynth3d --help
singularity exec /idia/projects/<your-project>/containers/rm-env.sif python3 -c "import astropy; print(astropy.__version__)"
```

If both commands succeed, the container is ready and processRM will use it for every SLURM job it submits.

---

## Inspect an existing local RM-env (optional)

If you already have a working Python venv for RM synthesis and want the container to match its versions exactly, run on the machine that has the venv:

```bash
cd <wherever you cloned processRM>/container/
./inspect_rm_env.sh
```

Copy the pinned package list it prints, and paste it into the `%post` section of `rm-env.def` before building.
