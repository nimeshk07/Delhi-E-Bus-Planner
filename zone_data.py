import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from prepare_delhi_data import point_in_district, polygon_rings


BASELINE_ZONE = "North Delhi"
SOURCE_FILE = Path(__file__).with_name("delhi_routes.geojson")
OUTPUT_DIRECTORY = Path(__file__).with_name("zones")


def _zone_slug(zone_name: str) -> str:
    return zone_name.casefold().replace(" ", "_")


def _zone_geometry(
    geometry: dict[str, Any] | None,
    zone: dict[str, Any],
) -> dict[str, Any] | None:
    if not geometry or geometry.get("type") != "MultiLineString":
        return geometry

    segments: list[list[list[float]]] = []
    for line in geometry["coordinates"]:
        current_segment: list[list[float]] = []
        for coordinate in line:
            if point_in_district(coordinate[0], coordinate[1], [zone]) is None:
                if len(current_segment) > 1:
                    segments.append(current_segment)
                current_segment = []
                continue
            current_segment.append(coordinate)
        if len(current_segment) > 1:
            segments.append(current_segment)

    return {"type": "MultiLineString", "coordinates": segments} if segments else None


def build_zone_datasets(
    network: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    features = network.get("features")
    if network.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError("The source must be a GeoJSON FeatureCollection.")

    zone_boundaries = {
        feature["properties"]["name"]: feature
        for feature in features
        if feature.get("properties", {}).get("kind") == "district"
    }
    if not zone_boundaries:
        raise ValueError("The source dataset has no district boundaries.")

    stops_by_zone: dict[str, dict[str, dict[str, Any]]] = {
        zone_name: {} for zone_name in zone_boundaries
    }
    stops_by_id: dict[str, dict[str, Any]] = {}
    for stop in features:
        properties = stop.get("properties", {})
        if properties.get("kind") != "stop":
            continue
        zone_name = properties.get("district")
        stop_id = str(properties.get("stop_id", ""))
        if zone_name not in stops_by_zone or not stop_id:
            raise ValueError(f"Stop {stop_id!r} has no matching district.")
        if stop_id in stops_by_id:
            raise ValueError(f"Duplicate stop ID: {stop_id}")
        stops_by_zone[zone_name][stop_id] = stop
        stops_by_id[stop_id] = stop

    routes = [
        feature
        for feature in features
        if feature.get("properties", {}).get("kind") == "route"
    ]
    if not routes or not stops_by_id:
        raise ValueError("The source dataset must include routes and stops.")

    route_zone_counts: dict[str, Counter[str]] = {}
    route_by_id: dict[str, dict[str, Any]] = {}
    for route in routes:
        properties = route["properties"]
        route_id = str(properties.get("route_id", ""))
        if not route_id or route_id in route_by_id:
            raise ValueError(f"Missing or duplicate route ID: {route_id!r}")
        route_by_id[route_id] = route
        route_stop_ids = {str(stop_id) for stop_id in properties.get("region_stop_ids", [])}
        counts = Counter(
            {
                zone_name: len(route_stop_ids.intersection(zone_stops))
                for zone_name, zone_stops in stops_by_zone.items()
            }
        )
        counts = Counter({zone: count for zone, count in counts.items() if count})
        if not counts:
            raise ValueError(f"Route {route_id} has no recorded stops in any zone.")
        route_zone_counts[route_id] = counts

    assigned_zone: dict[str, str] = {}
    for route_id, counts in route_zone_counts.items():
        highest_count = max(counts.values())
        tied_zones = [
            zone_name
            for zone_name, count in counts.items()
            if count == highest_count
        ]
        if BASELINE_ZONE in tied_zones:
            assigned_zone[route_id] = BASELINE_ZONE
        else:
            assigned_zone[route_id] = min(tied_zones)
    cross_zone_route_count = sum(
        len(counts) > 1 for counts in route_zone_counts.values()
    )

    zone_datasets: dict[str, dict[str, Any]] = {}
    for zone_name, boundary in zone_boundaries.items():
        zone_route_ids = {
            route_id
            for route_id, assigned in assigned_zone.items()
            if assigned == zone_name
        }
        zone_stops = []
        for stop_id, stop in stops_by_zone[zone_name].items():
            stop_routes = sorted(
                route_id
                for route_id in stop["properties"].get("routes", [])
                if str(route_id) in zone_route_ids
            )
            zone_stops.append(
                {
                    **stop,
                    "properties": {
                        **stop["properties"],
                        "routes": stop_routes,
                    },
                }
            )
        zone_stop_ids = {stop["properties"]["stop_id"] for stop in zone_stops}

        zone_routes = []
        for route_id in sorted(zone_route_ids):
            route = route_by_id[route_id]
            route_stop_ids = sorted(
                zone_stop_ids.intersection(
                    str(stop_id)
                    for stop_id in route["properties"].get("region_stop_ids", [])
                )
            )
            if not route_stop_ids:
                raise ValueError(
                    f"Assigned route {route_id} has no stops in {zone_name}."
                )
            zone_routes.append(
                {
                    **route,
                    "geometry": _zone_geometry(
                        route.get("geometry"),
                        {
                            "name": zone_name,
                            "polygons": polygon_rings(boundary["geometry"]),
                        },
                    ),
                    "properties": {
                        **route["properties"],
                        "region_stop_ids": route_stop_ids,
                    },
                }
            )

        zone_metadata = {
            **network.get("metadata", {}),
            "region": [zone_name],
            "routes": len(zone_routes),
            "stops": len(zone_stops),
            "weekday_trips": sum(
                int(route["properties"]["weekday_trips"]) for route in zone_routes
            ),
            "cross_zone_route_patterns_assigned": cross_zone_route_count,
            "zone_assignment_rule": (
                "Each route is assigned once to the district containing the largest "
                "number of its recorded in-area stops. Ties are assigned to North "
                "Delhi when present; otherwise the district name is the tie-break."
            ),
            "zone_role": (
                "baseline/development zone"
                if zone_name == BASELINE_ZONE
                else "separate test zone"
            ),
        }
        zone_datasets[zone_name] = {
            "type": "FeatureCollection",
            "name": f"{zone_name} bus network",
            "metadata": zone_metadata,
            "features": [boundary, *zone_routes, *zone_stops],
        }

    return zone_datasets


def write_zone_datasets(
    network: dict[str, Any],
    output_directory: Path = OUTPUT_DIRECTORY,
) -> dict[str, Path]:
    datasets = build_zone_datasets(network)
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for zone_name, dataset in datasets.items():
        output_path = output_directory / f"{_zone_slug(zone_name)}.geojson"
        with output_path.open("w", encoding="utf-8") as output_file:
            json.dump(
                dataset,
                output_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        paths[zone_name] = output_path
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split the packaged Delhi network into district-zone datasets."
    )
    parser.add_argument("--source", type=Path, default=SOURCE_FILE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIRECTORY)
    arguments = parser.parse_args()

    with arguments.source.open("r", encoding="utf-8") as source_file:
        network = json.load(source_file)
    for zone_name, path in write_zone_datasets(
        network,
        arguments.output_dir,
    ).items():
        with path.open("r", encoding="utf-8") as zone_file:
            metadata = json.load(zone_file)["metadata"]
        print(
            f"{zone_name}: {metadata['routes']} routes, {metadata['stops']} stops, "
            f"{metadata['weekday_trips']:,} weekday trips -> {path}"
        )


if __name__ == "__main__":
    main()
