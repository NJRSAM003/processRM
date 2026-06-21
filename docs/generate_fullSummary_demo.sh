#!/bin/bash
# ==================================================================
#  generate_fullSummary_demo.sh
# ==================================================================
#
# Builds a fake 3-region workdir under /tmp/fs_demo populated with
# enough stub files to make every progress bar in fullSummary show
# something interesting (region 1 nearly done, region 2 mid-pipeline,
# region 3 only past the noise stage). No real cubes or SLURM jobs
# are involved -- the files are zero-byte placeholders that just
# satisfy the glob patterns fullSummary looks for.
#
# After it runs, you'll see the same output that's referenced from
# the README. Take a screenshot of your terminal window and save it
# to docs/fullSummary_demo.png in this repo.
#
# Usage:
#   ./docs/generate_fullSummary_demo.sh
# ==================================================================

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DIR="${1:-/tmp/fs_demo}"

if [ ! -x "$REPO_DIR/aux_scripts/fullSummary" ]; then
    echo "ERROR: $REPO_DIR/aux_scripts/fullSummary not found or not executable."
    exit 1
fi

echo "Building fake multi-region workdir at: $DEMO_DIR"
rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"
cd "$DEMO_DIR"

cat > myconfig.txt <<EOF
[chunking]
chunks = 50
EOF

# Stub out each region with the bits fullSummary looks for
for R in 1 2 3; do
    mkdir -p "region$R/processing" "region$R/logs" "region$R/errors"
    touch "region$R/cube.stokesI.fits" \
          "region$R/cube.stokesQ.fits" \
          "region$R/cube.stokesU.fits" \
          "region$R/cube.freqlist.txt" \
          "region$R/noise_map.fits"
done

# Region 1: every stage close to done, some merged outputs landed
for i in $(seq 1 50); do
    touch "region1/processing/part_${i}_cube.stokesQ.fits"
    touch "region1/processing/part_${i}_FDF_tot_dirty.fits"
done
for i in $(seq 1 48); do
    touch "region1/processing/part_${i}_FDF_clean_tot.fits"
done
touch region1/FDF_clean_tot.fits region1/FDF_tot_dirty.fits \
      region1/RMSF_tot.fits region1/RMSF_FWHM.fits \
      region1/FDF_maxPI.fits region1/FDF_peakRM.fits

# Region 2: mid-pipeline, no merged outputs yet
for i in $(seq 1 50); do touch "region2/processing/part_${i}_cube.stokesQ.fits"; done
for i in $(seq 1 22); do touch "region2/processing/part_${i}_FDF_tot_dirty.fits"; done
for i in $(seq 1 14); do touch "region2/processing/part_${i}_FDF_clean_tot.fits"; done

# Region 3: just past the noise stage

# Realistic per-stage timings for region 1
cat > region1/logs/timings.csv <<EOF
jobid,taskid,stage,duration_sec,status,timestamp
1001,1,chunking,42,OK,2026-06-21T11:00:00+00:00
1001,2,chunking,38,OK,2026-06-21T11:00:30+00:00
1001,3,chunking,41,OK,2026-06-21T11:01:00+00:00
1001,1,rmsynth,615,OK,2026-06-21T11:10:00+00:00
1001,2,rmsynth,602,OK,2026-06-21T11:11:00+00:00
1001,3,rmsynth,1024,OK,2026-06-21T11:12:00+00:00
1001,1,rmclean,1820,OK,2026-06-21T11:42:00+00:00
1001,2,rmclean,1758,OK,2026-06-21T11:43:00+00:00
1001,3,rmclean,2104,OK,2026-06-21T11:44:00+00:00
1002,1,merge,310,OK,2026-06-21T12:30:00+00:00
EOF

# Region 2 with one rmsynth FAIL so the timings row goes yellow
cat > region2/logs/timings.csv <<EOF
jobid,taskid,stage,duration_sec,status,timestamp
1003,1,chunking,40,OK,2026-06-21T11:15:00+00:00
1003,2,chunking,39,OK,2026-06-21T11:15:30+00:00
1003,1,rmsynth,650,OK,2026-06-21T11:25:00+00:00
1003,2,rmsynth,0,FAIL,2026-06-21T11:25:30+00:00
EOF

echo ""
echo "Running fullSummary against the fake workdir..."
echo ""
python3 "$REPO_DIR/aux_scripts/fullSummary" --workdir "$DEMO_DIR"
echo ""
echo "Done. Take a screenshot of your terminal and save it to:"
echo "  $REPO_DIR/docs/fullSummary_demo.png"
