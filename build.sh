#!/usr/bin/env bash
# Render build step
set -o errexit
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py reset_lanes
python manage.py seed_sim
python manage.py setup_my_work
python manage.py add_example_cards
python manage.py setup_agency_board
python manage.py redo_agency_board
