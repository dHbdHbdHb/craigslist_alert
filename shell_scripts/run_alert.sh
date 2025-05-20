#!/bin/bash
source /home/pi/miniforge3/etc/profile.d/conda.sh
conda activate craigslist
python /home/pi/craigslist_alert/craigslist_alert_robust.py
