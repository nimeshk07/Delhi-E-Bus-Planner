import argparse
import json
from pathlib import Path
from typing import Any

from prepare_delhi_data import (
    GTFS_SOURCE,
    TURNAROUND_MINUTES,
    read_boundaries,
    read_gtfs,
)


PROJECT_DIRECTORY = Path(__file__).parent
ZONE_DIRECTORY = PROJECT_DIRECTORY / "zones"
EAST_BOUNDARY_SOURCE = (
    "https://nominatim.openstreetmap.org/lookup?"
    "osm_ids=R21181221&format=geojson&polygon_geojson=1"
)


def keep_unassigned_east_routes(
    network: dict[str, Any],
    existing_route_ids: set[str],
) -> dict[str, Any]:
    features = network.get("features")
    if network.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError("The East Delhi input must be a GeoJSON FeatureCollection.")

    route_features = [
        feature
        for feature in features
        if feature.get("properties", {}).get("kind") == "route"
    ]
    retained_routes = [
        route
        for route in route_features
        if str(route["properties"]["route_id"]) not in existing_route_ids
    ]
    retained_route_ids = {
        str(route["properties"]["route_id"]) for route in retained_routes
    }
    excluded_routes = [
        route
        for route in route_features
        if str(route["properties"]["route_id"]) in existing_route_ids
    ]
    filtered_features = []
    for feature in features:
        properties = feature.get("properties", {})
        if properties.get("kind") == "route":
            continue
        if properties.get("kind") == "stop":
            filtered_features.append(
                {
                    **feature,
                    "properties": {
                        **properties,
                        "routes": sorted(
                            str(route_id)
                            for route_id in properties.get("routes", [])
                            if str(route_id) in retained_route_ids
                        ),
                    },
                }
            )
        else:
            filtered_features.append(feature)

    stop_count = sum(
        feature.get("properties", {}).get("kind") == "stop"
        for feature in filtered_features
    )
    metadata = {
        **network.get("metadata", {}),
        "routes": len(retained_routes),
        "stops": stop_count,
        "weekday_trips": sum(
            int(route["properties"]["weekday_trips"]) for route in retained_routes
        ),
        "routes_already_assigned_to_north_or_central": len(excluded_routes),
        "weekday_trips_already_assigned_to_north_or_central": sum(
            int(route["properties"]["weekday_trips"]) for route in excluded_routes
        ),
        "cross_zone_route_patterns_assigned": len(excluded_routes),
        "route_assignment_summary": (
            f"{len(excluded_routes)} East-serving route patterns were already "
            "assigned to North or Central Delhi and are not duplicated here."
        ),
    }
    return {
        **network,
        "metadata": metadata,
        "features": [*filtered_features, *retained_routes],
    }


def build_east_zone_dataset(
    gtfs_directory: Path,
    boundary_path: Path,
    existing_route_ids: set[str],
    base_metadata: dict[str, Any],
) -> dict[str, Any]:
    districts, boundary_features = read_boundaries(
        boundary_path,
        {"East Delhi"},
    )
    _, regional_features, _ = read_gtfs(gtfs_directory, districts)
    route_features = [
        feature
        for feature in regional_features
        if feature["properties"].get("kind") == "route"
    ]
    stop_features = [
        feature
        for feature in regional_features
        if feature["properties"].get("kind") == "stop"
    ]
    network = {
        "type": "FeatureCollection",
        "name": "East Delhi bus network",
        "metadata": {
            **base_metadata,
            "region": ["East Delhi"],
            "source_gtfs": GTFS_SOURCE,
            "source_boundaries": EAST_BOUNDARY_SOURCE,
            "boundary_license": "OpenStreetMap contributors, ODbL 1.0",
            "zone_role": "additional test zone",
            "zone_assignment_rule": (
                "North Delhi remains the baseline and Central Delhi remains the "
                "separate test zone; East Delhi contains only route patterns not "
                "already assigned to either existing zone."
            ),
            "fleet_method": (
                f"Sum of per-route peak overlapping trips with a "
                f"{TURNAROUND_MINUTES}-minute terminal buffer; route interlining "
                "is not modeled."
            ),
        },
        "features": [*boundary_features, *route_features, *stop_features],
    }
    return keep_unassigned_east_routes(network, existing_route_ids)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an East Delhi test-zone dataset from the public GTFS feed."
    )
    parser.add_argument("--gtfs-dir", type=Path, required=True)
    parser.add_argument(
        "--boundary",
        type=Path,
        default=ZONE_DIRECTORY / "east_delhi_boundary.geojson",
    )
    parser.add_argument(
        "--existing-zone-dir",
        type=Path,
        default=ZONE_DIRECTORY,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ZONE_DIRECTORY / "east_delhi.geojson",
    )
    arguments = parser.parse_args()

    existing_route_ids = set()
    for filename in ("north_delhi.geojson", "central_delhi.geojson"):
        zone_path = arguments.existing_zone_dir / filename
        with zone_path.open("r", encoding="utf-8") as zone_file:
            zone_data = json.load(zone_file)
        existing_route_ids.update(
            str(feature["properties"]["route_id"])
            for feature in zone_data["features"]
            if feature.get("properties", {}).get("kind") == "route"
        )

    with (arguments.existing_zone_dir / "north_delhi.geojson").open(
        "r",
        encoding="utf-8",
    ) as north_zone_file:
        base_metadata = json.load(north_zone_file)["metadata"]
    zone_data = build_east_zone_dataset(
        arguments.gtfs_dir,
        arguments.boundary,
        existing_route_ids,
        base_metadata,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("w", encoding="utf-8") as output_file:
        json.dump(
            zone_data,
            output_file,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    metadata = zone_data["metadata"]
    stop_count = sum(
        feature.get("properties", {}).get("kind") == "stop"
        for feature in zone_data["features"]
    )
    print(
        f"East Delhi: {metadata['routes']} new routes, {stop_count} stops, "
        f"{metadata['weekday_trips']:,} weekday trips; "
        f"{metadata['routes_already_assigned_to_north_or_central']} routes remain "
        f"assigned to existing zones -> {arguments.output}"
    )


if __name__ == "__main__":
    main()
