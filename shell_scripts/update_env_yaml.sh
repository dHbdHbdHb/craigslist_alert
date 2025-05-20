#!/bin/bash
source ~/miniforge3/etc/profile.d/conda.sh
conda activate craigslist
conda env export --no-builds > ../environment.yml
echo "environment.yml updated"
