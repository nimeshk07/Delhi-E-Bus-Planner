import html
import json
import math
from pathlib import Path
from typing import Any

import folium
import streamlit as st
from streamlit_folium import st_folium

from planner import (
    MAX_SITE_DISTANCE_KM,
    MAX_SITE_OPTIONS_PER_ROUTE,
    full_service_estimate,
    solve_plan,
)


ZONE_DATA_FILES = {
    "North Delhi": Path(__file__).with_name("zones") / "north_delhi.geojson",
    "Central Delhi": Path(__file__).with_name("zones") / "central_delhi.geojson",
    "East Delhi": Path(__file__).with_name("zones") / "east_delhi.geojson",
}
PLANNER_FILE = Path(__file__).with_name("planner.py")

st.set_page_config(
    page_title="Delhi E-Bus Fleet & Charging Planner",
    page_icon="🚌",
    layout="wide",
)


@st.cache_data(show_spinner=False)
def load_network(zone_name: str, data_revision: int) -> dict[str, Any]:
    data_file_path = ZONE_DATA_FILES.get(zone_name)
    if data_file_path is None:
        raise ValueError(f"Unknown planning zone: {zone_name}")
    if not data_file_path.is_file():
        raise FileNotFoundError(
            f"Missing {data_file_path.relative_to(Path(__file__).parent)}. "
            "Run the preprocessing script for this zone to generate its dataset."
        )
    if data_file_path.stat().st_mtime_ns != data_revision:
        raise ValueError(f"The {zone_name} dataset changed while it was loading.")
    with data_file_path.open("r", encoding="utf-8") as data_file:
        data = json.load(data_file)
    if data.get("type") != "FeatureCollection" or not isinstance(
        data.get("features"), list
    ):
        raise ValueError(
            f"{data_file_path.name} is not a valid GeoJSON FeatureCollection."
        )
    features = data["features"]
    if not any(
        feature.get("properties", {}).get("kind") == "route" for feature in features
    ) or not any(
        feature.get("properties", {}).get("kind") == "stop" for feature in features
    ):
        raise ValueError(f"{data_file_path.name} has no usable routes or bus stops.")
    return data


@st.cache_data(show_spinner=False)
def run_optimization(
    zone_name: str,
    budget_crore: float,
    transformer_capacity_mw: float,
    planning_mode: str,
    data_revision: int,
    planner_revision: int,
) -> dict[str, Any]:
    if PLANNER_FILE.stat().st_mtime_ns != planner_revision:
        raise ValueError("The MILP model changed while it was loading.")
    network = load_network(zone_name, data_revision)
    routes = [
        feature
        for feature in network["features"]
        if feature["properties"].get("kind") == "route"
    ]
    stops = [
        feature
        for feature in network["features"]
        if feature["properties"].get("kind") == "stop"
    ]
    return solve_plan(
        routes,
        stops,
        budget_crore,
        transformer_capacity_mw,
        mode=planning_mode,
    )


def build_map(
    boundaries: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    stops: list[dict[str, Any]],
    locations: list[dict[str, Any]],
) -> folium.Map:
    boundary_points = []
    for boundary in boundaries:
        geometry = boundary["geometry"]
        polygons = (
            [geometry["coordinates"]]
            if geometry["type"] == "Polygon"
            else geometry["coordinates"]
        )
        for polygon in polygons:
            boundary_points.extend(polygon[0])
    if not boundary_points:
        raise ValueError("The selected district boundary geometry is empty.")

    route_names = {
        str(route["properties"]["route_id"]): route["properties"]["name"]
        for route in routes
    }
    center_lat = sum(point[1] for point in boundary_points) / len(boundary_points)
    center_lon = sum(point[0] for point in boundary_points) / len(boundary_points)
    map_view = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        min_zoom=10,
        max_zoom=18,
        max_bounds=True,
        control_scale=True,
        tiles="OpenStreetMap",
    )

    folium.GeoJson(
        {
            "type": "FeatureCollection",
            "features": boundaries,
        },
        name="Selected district boundary",
        style_function=lambda _: {
            "color": "#173f74",
            "weight": 2,
            "fillColor": "#4385c5",
            "fillOpacity": 0.035,
        },
        tooltip=folium.GeoJsonTooltip(fields=["name"], aliases=["District"]),
    ).add_to(map_view)

    route_lines = [route for route in routes if route.get("geometry")]
    if route_lines:
        folium.GeoJson(
            {
                "type": "FeatureCollection",
                "features": route_lines,
            },
            name=f"Regional bus routes ({len(routes)})",
            style_function=lambda _: {
                "color": "#087e8b",
                "weight": 2,
                "opacity": 0.58,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["name", "weekday_trips", "buses_required"],
                aliases=["Route", "Weekday trips", "Estimated buses"],
                sticky=True,
            ),
        ).add_to(map_view)

    stop_features = []
    for stop in stops:
        properties = stop["properties"]
        route_labels = [
            route_names[route_id]
            for route_id in properties.get("routes", [])
            if route_id in route_names
        ]
        stop_features.append(
            {
                **stop,
                "properties": {
                    **properties,
                    "route_names": ", ".join(route_labels),
                },
            }
        )
    folium.GeoJson(
        {
            "type": "FeatureCollection",
            "features": stop_features,
        },
        name=f"Bus stops ({len(stops)})",
        marker=folium.CircleMarker(
            radius=3,
            color="#ffffff",
            weight=1,
            fill=True,
            fill_color="#bd3d45",
            fill_opacity=0.85,
        ),
        tooltip=folium.GeoJsonTooltip(
            fields=["name", "district", "route_names"],
            aliases=["Stop", "District", "Routes"],
            sticky=True,
        ),
    ).add_to(map_view)

    charger_layer = folium.FeatureGroup(
        name=f"Recommended charging locations ({len(locations)})",
        show=True,
    )
    for location in locations:
        popup = (
            f"<strong>{html.escape(location['name'])}</strong><br>"
            f"District: {html.escape(location['district'])}<br>"
            f"Charging units: {location['station_units']}<br>"
            f"Bus capacity: {location['bus_capacity']}<br>"
            f"Estimated power: {location['power_mw']:.2f} MW"
        )
        folium.CircleMarker(
            [location["latitude"], location["longitude"]],
            radius=min(12, 5 + math.sqrt(location["station_units"])),
            color="#ffffff",
            weight=2,
            fill=True,
            fill_color="#f28e2b",
            fill_opacity=0.95,
            tooltip=(
                f"{location['name']}: {location['station_units']} charging units"
            ),
            popup=folium.Popup(popup, max_width=340),
        ).add_to(charger_layer)
    charger_layer.add_to(map_view)
    folium.LayerControl(collapsed=False).add_to(map_view)

    latitudes = [point[1] for point in boundary_points]
    longitudes = [point[0] for point in boundary_points]
    map_view.fit_bounds(
        [
            [min(latitudes), min(longitudes)],
            [max(latitudes), max(longitudes)],
        ],
        padding=(18, 18),
    )
    return map_view


with st.sidebar:
    st.header("Scenario controls")
    selected_zone = st.selectbox(
        "Study zone",
        options=list(ZONE_DATA_FILES),
        index=0,
        help=(
            "North Delhi is the baseline/development zone. Central Delhi and "
            "East Delhi are separate test zones."
        ),
    )

zone_data_file = ZONE_DATA_FILES[selected_zone]
try:
    data_revision = zone_data_file.stat().st_mtime_ns
    planner_revision = PLANNER_FILE.stat().st_mtime_ns
    network_data = load_network(selected_zone, data_revision)
except (OSError, json.JSONDecodeError, ValueError) as error:
    st.error(f"Could not load the {selected_zone} network data: {error}")
    st.stop()

features = network_data["features"]
route_features = [
    feature for feature in features if feature["properties"].get("kind") == "route"
]
stop_features = [
    feature for feature in features if feature["properties"].get("kind") == "stop"
]
boundary_features = [
    feature for feature in features if feature["properties"].get("kind") == "district"
]
if not route_features or not stop_features or not boundary_features:
    st.error(f"The {selected_zone} dataset must contain routes, stops, and a boundary.")
    st.stop()

full_plan = full_service_estimate(route_features)
full_budget_slider_max = max(
    5, math.ceil(full_plan["budget_crore"] * 1.25 / 5) * 5
)
full_budget_default = math.ceil(full_plan["budget_crore"] / 5) * 5
full_capacity_slider_max = max(
    0.25, math.ceil(full_plan["transformer_mw"] * 1.25 * 4) / 4
)
zone_slug = selected_zone.casefold().replace(" ", "_")

st.title("Delhi Electric Bus Fleet & Charging Planner")
st.caption(
    f"Schedule-based MILP planning for GTFS bus routes assigned to {selected_zone}."
)
st.info(
    f"{selected_zone} dataset: {len(route_features):,} route patterns, "
    f"{len(stop_features):,} stops, and "
    f"{full_plan['weekday_trips']:,} scheduled weekday trips. "
    "This is a static-data simulation, not live vehicle tracking."
)

with st.sidebar:
    st.caption(
        "North Delhi is the baseline zone; Central Delhi and East Delhi are separate "
        "test zones."
    )
    planning_mode_label = st.radio(
        "Planning mode",
        [
            "Maximize service under limits",
            "Check full electrification",
        ],
        help=(
            "Choose whether to maximize weekday service within the selected "
            "resources or test whether every route can be electrified."
        ),
    )
    planning_mode = (
        "full_electrification"
        if planning_mode_label == "Check full electrification"
        else "budget_constrained"
    )
    budget_crore = st.slider(
        "Available investment budget (₹ crore)",
        min_value=0.0,
        max_value=float(full_budget_slider_max),
        value=float(full_budget_default),
        step=5.0,
        key=f"budget_{zone_slug}",
    )
    transformer_capacity_mw = st.slider(
        "Available transformer capacity (MW)",
        min_value=0.0,
        max_value=float(full_capacity_slider_max),
        value=float(full_plan["transformer_mw"]),
        step=0.25,
        key=f"transformer_{zone_slug}",
    )
    st.divider()
    st.markdown("**Fixed planning assumptions**")
    st.write("Electric bus: ₹50 lakh")
    st.write("Charging station/unit: ₹20 lakh; supports up to 3 buses")
    st.write("Transformer draw: 0.25 MW per charging unit")
    st.caption(
        "Site setup costs, grid upgrades, battery range, and energy use are not modeled."
    )
    st.write(
        f"Candidate stop: within {MAX_SITE_DISTANCE_KM:.1f} km; "
        f"up to {MAX_SITE_OPTIONS_PER_ROUTE} options per route"
    )

try:
    with st.spinner("Solving the fleet and charging-location MILP..."):
        plan = run_optimization(
            selected_zone,
            budget_crore,
            transformer_capacity_mw,
            planning_mode,
            data_revision,
            planner_revision,
        )
except (ValueError, RuntimeError) as error:
    st.error(f"The optimization could not be completed: {error}")
    st.stop()

if not plan["feasible"]:
    st.error(
        "Full electrification is infeasible under the selected budget and/or "
        "transformer capacity."
    )
    st.info(
        "Increase one or both resource limits, or select "
        "**Maximize service under limits** to see the best feasible partial plan."
    )
    st.stop()

if not plan["optimal"]:
    st.warning(
        "The solver returned a feasible plan but did not prove it optimal within "
        f"the time limit. Solver status: {plan['status']}"
    )
if plan["weekday_trips_served"] < plan["weekday_trips_total"]:
    st.warning(
        "Available budget or transformer capacity cannot support every route's "
        "schedule. The plan prioritizes routes with more scheduled weekday trips."
    )

st.subheader("Optimal solution for the selected scenario")
metric_columns = st.columns(4)
metric_columns[0].metric("Electric buses", f"{plan['buses']:,}")
metric_columns[1].metric("Charging stations / units", f"{plan['stations']:,}")
metric_columns[2].metric("Recommended locations", f"{plan['location_count']:,}")
metric_columns[3].metric(
    "Estimated investment", f"₹{plan['investment_crore']:,.1f} Cr"
)

detail_columns = st.columns(4)
detail_columns[0].metric(
    "Routes kept", f"{plan['routes_served']:,} / {plan['route_count']:,}"
)
detail_columns[1].metric(
    "Weekday trips retained",
    f"{plan['weekday_trips_served']:,} ({plan['trip_coverage_percent']:.1f}%)",
)
detail_columns[2].metric("Estimated charging load", f"{plan['power_mw']:.2f} MW")
detail_columns[3].metric(
    "Resources remaining",
    f"₹{max(0.0, budget_crore - plan['investment_crore']):,.1f} Cr",
    delta=f"{max(0.0, transformer_capacity_mw - plan['power_mw']):.2f} MW",
    delta_description="transformer headroom",
)

tabs = st.tabs(["Map and charging sites", "Route allocation", "Model and data"])
with tabs[0]:
    st.write(
        f"Routes assigned to {selected_zone} and its district boundary are shown. "
        "Orange markers are the selected candidate charging locations."
    )
    try:
        map_view = build_map(
            boundary_features,
            route_features,
            stop_features,
            plan["locations"],
        )
    except ValueError as error:
        st.error(f"Could not create the planning map: {error}")
    else:
        st_folium(map_view, height=680, use_container_width=True, returned_objects=[])
    if plan["locations"]:
        st.subheader("Recommended charging locations")
        st.dataframe(
            [
                {
                    "Location": location["name"],
                    "District": location["district"],
                    "Charging units": location["station_units"],
                    "Bus capacity": location["bus_capacity"],
                    "Buses assigned": location["assigned_buses"],
                    "Power (MW)": round(location["power_mw"], 2),
                    "Latitude": round(location["latitude"], 6),
                    "Longitude": round(location["longitude"], 6),
                }
                for location in sorted(
                    plan["locations"],
                    key=lambda item: (-item["assigned_buses"], item["name"]),
                )
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.write("No charging locations are feasible under the selected limits.")

with tabs[1]:
    served_by_id = {route["route_id"]: route for route in plan["routes"]}
    route_rows = []
    for route in route_features:
        properties = route["properties"]
        selected = served_by_id.get(str(properties["route_id"]))
        route_rows.append(
            {
                "Route": properties["name"],
                "Description": properties.get("description", ""),
                "Status": "Included" if selected else "Not funded",
                "Weekday trips": int(properties["weekday_trips"]),
                "Buses required": (
                    int(properties["buses_required"]) if selected else 0
                ),
                "Charging location(s)": (
                    selected["charging_stops"] if selected else ""
                ),
            }
        )
    st.dataframe(
        route_rows,
        hide_index=True,
        width="stretch",
        height=600,
    )

with tabs[2]:
    objective_description = (
        "minimize total bus and charging-unit investment while serving every route"
        if planning_mode == "full_electrification"
        else "maximize scheduled weekday one-way trips retained, then minimize "
        "buses, charging units, and bus-weighted distance to charging sites"
    )
    metadata = network_data["metadata"]
    route_assignment_summary = metadata.get("route_assignment_summary")
    if route_assignment_summary is None:
        route_assignment_summary = (
            f"The source contains "
            f"{metadata['cross_zone_route_patterns_assigned']} route patterns with "
            "stops in both districts; each is included in only one zone."
        )
    st.markdown(
        f"""
**Optimization objective:** {objective_description}.

**Constraints:** the complete route schedule is either retained or omitted; bus and
charging-unit investment must fit the selected budget; charging load must fit the
transformer limit; route buses can be distributed across the
{MAX_SITE_OPTIONS_PER_ROUTE} nearest candidate stops within
{MAX_SITE_DISTANCE_KM:.1f} km; each charging unit supports at most 3 buses.

**Selected zone:** {selected_zone} ({network_data['metadata']['zone_role']}).

**Route assignment:** {metadata['zone_assignment_rule']}
{route_assignment_summary}

**Fleet estimate:** {network_data['metadata']['fleet_method']}

**Full-service reference (conservative route-local charger allowance):**
{full_plan['buses']:,} buses, up to {full_plan['stations']:,} charging units
({full_plan['theoretical_minimum_stations']:,} if charger capacity were pooled
across the whole zone), estimated investment
₹{full_plan['budget_crore']:,.1f} crore, and
{full_plan['transformer_mw']:.2f} MW.

**Important limitations:** passenger counts, energy consumption, battery range,
vehicle blocks, deadheading, land availability, and local transformer locations are
not present in the input data. GTFS stops are used as *candidate* charging sites;
the recommended points require depot, land, grid-capacity, and field validation.
"""
    )
    st.markdown(
        f"**GTFS source:** [Delhi bus static feed]({network_data['metadata']['source_gtfs']}) "
        f"({network_data['metadata']['feed_version']}). "
        "Delhi OTD documents that stop times are approximate estimates based on "
        "constant-speed assumptions."
    )
    st.markdown(
        "**District boundary:** [Selected district in OpenStreetMap]"
        f"({network_data['metadata']['source_boundaries']}); "
        "© OpenStreetMap contributors, ODbL 1.0."
    )

st.caption(
    "This is an academic decision-support prototype. Do not treat the simulated "
    "fleet or candidate locations as an approved DTC deployment plan."
)
