# Delhi Electric Bus Fleet & Charging Planner

A Streamlit decision-support prototype that uses the static Delhi bus GTFS feed
to plan electric-bus fleet size and charging locations in **North Delhi** and
**Central Delhi**, with **East Delhi** as an additional test zone. Select a
district from the app's study-zone dropdown; each zone has its own routes, stops,
boundary, default full-service reference, and MILP result.

## Run locally

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

Keep `app.py`, `planner.py`, the `zones` folder, and `requirements.txt` together.
The root `delhi_routes.geojson` is the source snapshot used to regenerate
district datasets with `zone_data.py`. `prepare_delhi_data.py` rebuilds
that source snapshot from GTFS files and North/Central district boundaries.
`prepare_east_zone.py` builds the East Delhi test dataset separately, using the
East boundary in `zones/east_delhi_boundary.geojson`.

## Publish on Streamlit Community Cloud

1. Push `app.py`, `planner.py`, `zone_data.py`, `zones/`, and
   `requirements.txt` to a GitHub repository. Include `delhi_routes.geojson` if
   you also want to regenerate the zone files after deployment.
2. In Streamlit Community Cloud, create an app from that repository.
3. Set the main file path to `app.py` and deploy.

The map uses OpenStreetMap tiles and needs an internet connection.

## What the simulation currently uses

- Three study zones using OpenStreetMap district boundaries: North Delhi
  (baseline/development zone), Central Delhi (separate test zone), and East
  Delhi (additional test zone). This is not a full-NCT bus network.
- Each GTFS route pattern is assigned to exactly one zone: the district with
  the most recorded in-area stops. Ties go to North Delhi when applicable,
  otherwise the district name is the deterministic tie-break. Route-level
  fleet demand is not duplicated across the zone test cases. The MILP is not a
  machine-learning model; North Delhi is the baseline/development zone. East
  Delhi is built separately and excludes route patterns already assigned to
  North or Central Delhi.
- The original combined snapshot contains 851 route patterns, 541 stops, and
  23,426 scheduled weekday trips across North and Central Delhi. The East
  boundary intersects 389 source route patterns; 99 already assigned to North
  or Central are excluded, leaving 290 East-only patterns, 617 in-area stops,
  and 7,757 scheduled weekday trips. Per-zone totals are recorded in each file
  under `zones/`.
- A schedule-based estimate of required buses: for each route, count the peak
  overlapping trips after adding a 15-minute terminal turnaround, then sum those
  route-level peaks.
- Two MILP modes: maximize scheduled weekday one-way trips within the selected
  resource limits, or test full electrification while minimizing investment.
  The partial mode then minimizes buses, charging units, and bus-weighted
  distance; full electrification reports infeasibility when the limits are too
  restrictive.
- Budget and transformer constraints. Route buses can be distributed across
  up to the 8 nearest candidate stops within 1.5 km of a route stop. Each
  charging unit costs ₹20 lakh, supports up to three buses, and draws 0.25 MW.
- Fixed bus purchase cost of ₹50 lakh.

With the included feed and assumptions, the North-zone full-service plan
serves 427 route patterns and 11,861 weekday trips with **1,974 buses, 658
charging units at 128 candidate locations**, an estimated investment of
**₹1,118.6 crore**, and **164.5 MW**. The Central-zone plan serves 424 route
patterns and 11,565 weekday trips with **1,828 buses, 610 charging units at 135
candidate locations**, an estimated investment of **₹1,036 crore**, and
**152.5 MW**. These are separate zone runs; slider changes rerun the selected
zone's MILP. The original combined-data fleet estimate is not a zone result.
At the East Delhi default controls (₹655 crore and 121.5 MW), the MILP serves all
290 route patterns and 7,757 weekday trips with **1,112 buses, 372 charging
units at 138 locations**, an estimated investment of **₹630.4 crore**, and
**93 MW**. Its conservative route-local reference allows up to 486 units; the
MILP reduces this by sharing charging capacity across routes.
Charger locations are one optimal placement; multiple site layouts can have the
same optimized fleet, unit count, and distance objective.

This is a planning simulation, not a validated DTC fleet plan. The GTFS feed
does not provide passenger counts or vehicle blocks, and Delhi OTD warns that
the timetable is approximate. Route-level fleet peaks do not model bus sharing
between routes. Candidate stops have not been checked for depot space, land
ownership, grid connections, or transformer availability. Battery range,
energy use, site setup costs, grid upgrades, and setup duration are not yet
modeled. Bus and charging-unit costs are fixed assumptions shown in the app.

## Rebuild the packaged route data

Download and extract the [public Delhi GTFS snapshot](https://github.com/ri5h46h/DTC_Delhi/tree/main/data/delhi_buses_static_gtfs_v1.zip),
then download the North and Central Delhi district boundaries as GeoJSON from
the [OpenStreetMap Nominatim lookup](https://nominatim.openstreetmap.org/lookup?osm_ids=R21181217%2CR21181219&format=geojson&polygon_geojson=1).
Run:

```bash
python prepare_delhi_data.py --gtfs-dir path/to/gtfs --boundaries path/to/districts.geojson
```

The script writes `delhi_routes.geojson` beside `app.py`.

To regenerate the North/Central test-case datasets from that snapshot, run:

```bash
python zone_data.py
```

This writes `zones/north_delhi.geojson` and `zones/central_delhi.geojson`.

To rebuild East Delhi, extract the same GTFS feed, keep the East Delhi boundary
file in `zones/east_delhi_boundary.geojson`, and run:

```bash
python prepare_east_zone.py --gtfs-dir path/to/gtfs
```

This writes `zones/east_delhi.geojson` without regenerating or changing the
North/Central data. It requires the existing North and Central zone files to
exclude duplicate route patterns.

## Sources

- [Delhi Open Transit Data documentation](https://otd.delhi.gov.in/documentation)
  and [static-data page](https://otd.delhi.gov.in/data/static/). The OTD
  documentation states that stop times are approximate estimates based on a
  constant-speed assumption.
- [Public GTFS snapshot mirror](https://github.com/ri5h46h/DTC_Delhi/tree/main/gtfs)
  (feed metadata starts on 2025-01-01; verify current route operation before
  operational use).
- District boundaries: [OpenStreetMap contributors](https://www.openstreetmap.org/copyright),
  ODbL 1.0.
