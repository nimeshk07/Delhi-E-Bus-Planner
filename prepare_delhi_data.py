import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TURNAROUND_MINUTES = 15
DEFAULT_DISTRICT_NAMES = {"Central Delhi", "North Delhi"}
GTFS_SOURCE = "https://github.com/ri5h46h/DTC_Delhi/tree/main/data/delhi_buses_static_gtfs_v1.zip"
BOUNDARY_SOURCE = (
    "https://nominatim.openstreetmap.org/lookup?"
    "osm_ids=R21181217%2CR21181219&format=geojson&polygon_geojson=1"
)


def parse_time(value: str) -> int | None:
    if not value:
        return None
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def point_in_ring(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = current
        x2, y2 = previous
        if (y1 > latitude) != (y2 > latitude):
            crossing_longitude = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing_longitude:
                inside = not inside
        previous = current
    return inside


def polygon_rings(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    raise ValueError(f"Unsupported boundary geometry: {geometry['type']}")


def point_in_district(
    longitude: float,
    latitude: float,
    districts: list[dict[str, Any]],
) -> str | None:
    for district in districts:
        for polygon in district["polygons"]:
            if not point_in_ring(longitude, latitude, polygon[0]):
                continue
            if not any(
                point_in_ring(longitude, latitude, hole) for hole in polygon[1:]
            ):
                return district["name"]
    return None


def weekday_services(gtfs_dir: Path) -> set[str]:
    calendar_path = gtfs_dir / "calendar.txt"
    if not calendar_path.is_file():
        return set()
    with calendar_path.open(encoding="utf-8-sig", newline="") as calendar_file:
        return {
            row["service_id"]
            for row in csv.DictReader(calendar_file)
            if any(row.get(day) == "1" for day in ("monday", "tuesday", "wednesday", "thursday", "friday"))
        }


def read_boundaries(
    boundary_path: Path,
    desired_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with boundary_path.open(encoding="utf-8") as boundary_file:
        collection = json.load(boundary_file)
    if desired_names is None:
        desired_names = DEFAULT_DISTRICT_NAMES
    districts = []
    features = []
    for feature in collection.get("features", []):
        properties = feature.get("properties", {})
        name = properties.get("name")
        if name not in desired_names:
            continue
        districts.append(
            {
                "name": name,
                "polygons": polygon_rings(feature["geometry"]),
            }
        )
        features.append(
            {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    "kind": "district",
                    "name": name,
                    "osm_relation": properties.get("osm_id"),
                    "source": "OpenStreetMap contributors",
                },
            }
        )
    if {district["name"] for district in districts} != desired_names:
        raise ValueError(
            "The boundary GeoJSON must contain exactly the requested districts: "
            f"{', '.join(sorted(desired_names))}."
        )
    return districts, features


def read_gtfs(
    gtfs_dir: Path, districts: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, set[str]]]:
    services = weekday_services(gtfs_dir)
    if not services:
        raise ValueError("calendar.txt does not list any weekday bus service.")

    with (gtfs_dir / "stops.txt").open(
        encoding="utf-8-sig", newline=""
    ) as stops_file:
        all_stops = list(csv.DictReader(stops_file))

    stops: dict[str, dict[str, Any]] = {}
    for row in all_stops:
        longitude = float(row["stop_lon"])
        latitude = float(row["stop_lat"])
        district_name = point_in_district(longitude, latitude, districts)
        if district_name is None:
            continue
        stops[row["stop_id"]] = {
            "stop_id": row["stop_id"],
            "name": row["stop_name"],
            "latitude": latitude,
            "longitude": longitude,
            "district": district_name,
            "routes": set(),
        }

    trips: dict[str, dict[str, str]] = {}
    shape_counts: dict[str, Counter[str]] = defaultdict(Counter)
    with (gtfs_dir / "trips.txt").open(
        encoding="utf-8-sig", newline=""
    ) as trips_file:
        for row in csv.DictReader(trips_file):
            if row["service_id"] not in services:
                continue
            trips[row["trip_id"]] = {
                "route_id": row["route_id"],
                "shape_id": row.get("shape_id", ""),
            }
            if row.get("shape_id"):
                shape_counts[row["route_id"]][row["shape_id"]] += 1

    trip_headers: dict[str, int]
    trip_schedules: dict[str, list[Any]] = {}
    with (gtfs_dir / "stop_times.txt").open(
        encoding="utf-8-sig", newline=""
    ) as stop_times_file:
        reader = csv.reader(stop_times_file)
        headers = next(reader)
        trip_headers = {name: index for index, name in enumerate(headers)}
        for row in reader:
            trip_id = row[trip_headers["trip_id"]]
            trip = trips.get(trip_id)
            if trip is None:
                continue
            route_id = trip["route_id"]
            stop_id = row[trip_headers["stop_id"]]
            sequence = int(row[trip_headers["stop_sequence"]])
            departure = parse_time(row[trip_headers["departure_time"]])
            arrival = parse_time(row[trip_headers["arrival_time"]])
            schedule = trip_schedules.get(trip_id)
            if schedule is None:
                schedule = [route_id, sequence, departure, sequence, arrival]
                trip_schedules[trip_id] = schedule
            else:
                if departure is not None and (
                    schedule[2] is None or sequence < schedule[1]
                ):
                    schedule[1], schedule[2] = sequence, departure
                if arrival is not None and sequence > schedule[3]:
                    schedule[3], schedule[4] = sequence, arrival
            if stop_id in stops:
                stops[stop_id]["routes"].add(route_id)

    with (gtfs_dir / "routes.txt").open(
        encoding="utf-8-sig", newline=""
    ) as routes_file:
        route_info = {row["route_id"]: row for row in csv.DictReader(routes_file)}

    route_schedules: dict[str, list[tuple[int, int]]] = defaultdict(list)
    route_trip_counts: Counter[str] = Counter()
    regional_routes = {
        route_id for stop in stops.values() for route_id in stop["routes"]
    }

    for schedule in trip_schedules.values():
        route_id, _, start_time, _, end_time = schedule
        if route_id not in regional_routes:
            continue
        if start_time is None or end_time is None or end_time < start_time:
            continue
        route_schedules[route_id].append(
            (start_time, end_time + TURNAROUND_MINUTES * 60)
        )
        route_trip_counts[route_id] += 1

    route_buses: dict[str, int] = {}
    for route_id, intervals in route_schedules.items():
        events = []
        for start_time, end_time in intervals:
            events.extend(((start_time, 1), (end_time, -1)))
        active = peak = 0
        for _, change in sorted(events, key=lambda event: (event[0], event[1])):
            active += change
            peak = max(peak, active)
        if peak:
            route_buses[route_id] = peak

    route_stop_ids: dict[str, list[str]] = defaultdict(list)
    for stop_id, stop in stops.items():
        stop["routes"] = sorted(
            route_id
            for route_id in stop["routes"]
            if route_id in route_buses
        )
        for route_id in stop["routes"]:
            route_stop_ids[route_id].append(stop_id)

    route_features = []
    for route_id, buses_required in route_buses.items():
        route = route_info.get(route_id)
        if route is None or not route_stop_ids[route_id]:
            continue
        candidates = shape_counts.get(route_id, {})
        shape_id = candidates.most_common(1)[0][0] if candidates else ""
        route_features.append(
            {
                "type": "Feature",
                "geometry": None,
                "properties": {
                    "kind": "route",
                    "route_id": route_id,
                    "name": route.get("route_short_name") or route.get("route_long_name") or route_id,
                    "description": route.get("route_desc", ""),
                    "shape_id": shape_id,
                    "weekday_trips": route_trip_counts[route_id],
                    "buses_required": buses_required,
                    "region_stop_ids": route_stop_ids[route_id],
                },
            }
        )

    selected_shapes = {
        feature["properties"]["shape_id"]
        for feature in route_features
        if feature["properties"]["shape_id"]
    }
    shape_coordinates: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    bounds = [
        point
        for district in districts
        for polygon in district["polygons"]
        for point in polygon[0]
    ]
    min_longitude = min(point[0] for point in bounds)
    max_longitude = max(point[0] for point in bounds)
    min_latitude = min(point[1] for point in bounds)
    max_latitude = max(point[1] for point in bounds)

    shapes_path = gtfs_dir / "shapes.txt"
    if shapes_path.is_file() and selected_shapes:
        with shapes_path.open(encoding="utf-8-sig", newline="") as shapes_file:
            reader = csv.reader(shapes_file)
            headers = next(reader)
            columns = {name: index for index, name in enumerate(headers)}
            for row in reader:
                shape_id = row[columns["shape_id"]]
                if shape_id not in selected_shapes:
                    continue
                latitude = float(row[columns["shape_pt_lat"]])
                longitude = float(row[columns["shape_pt_lon"]])
                if not (
                    min_latitude <= latitude <= max_latitude
                    and min_longitude <= longitude <= max_longitude
                ):
                    continue
                district_name = point_in_district(longitude, latitude, districts)
                if district_name:
                    sequence = int(row[columns["shape_pt_sequence"]])
                    shape_coordinates[shape_id].append(
                        (sequence, longitude, latitude)
                    )

    geometry_by_shape: dict[str, dict[str, Any] | None] = {}
    for shape_id, points in shape_coordinates.items():
        points.sort(key=lambda point: point[0])
        segments: list[list[list[float]]] = []
        current_segment: list[list[float]] = []
        previous_sequence: int | None = None
        for sequence, longitude, latitude in points:
            if previous_sequence is not None and sequence != previous_sequence + 1:
                if len(current_segment) > 1:
                    segments.append(current_segment)
                current_segment = []
            current_segment.append([longitude, latitude])
            previous_sequence = sequence
        if len(current_segment) > 1:
            segments.append(current_segment)
        geometry_by_shape[shape_id] = (
            {"type": "MultiLineString", "coordinates": segments}
            if segments
            else None
        )

    for feature in route_features:
        shape_id = feature["properties"]["shape_id"]
        feature["geometry"] = geometry_by_shape.get(shape_id)

    stop_features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [stop["longitude"], stop["latitude"]],
            },
            "properties": {
                "kind": "stop",
                "stop_id": stop["stop_id"],
                "name": stop["name"],
                "district": stop["district"],
                "routes": stop["routes"],
            },
        }
        for stop in stops.values()
        if stop["routes"]
    ]
    return route_info, route_features + stop_features, route_stop_ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a North and Central Delhi route and stop GeoJSON."
    )
    parser.add_argument("--gtfs-dir", type=Path, required=True)
    parser.add_argument("--boundaries", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("delhi_routes.geojson"),
    )
    args = parser.parse_args()

    districts, boundary_features = read_boundaries(args.boundaries)
    _, network_features, _ = read_gtfs(args.gtfs_dir, districts)
    features = boundary_features + network_features
    route_features = [
        feature for feature in features if feature["properties"].get("kind") == "route"
    ]
    stop_features = [
        feature for feature in features if feature["properties"].get("kind") == "stop"
    ]
    metadata = {
        "region": ["Central Delhi", "North Delhi"],
        "source_gtfs": GTFS_SOURCE,
        "feed_version": "v1; feed start date 2025-01-01",
        "source_boundaries": BOUNDARY_SOURCE,
        "boundary_license": "OpenStreetMap contributors, ODbL 1.0",
        "schedule_note": (
            "Delhi OTD says stop times are approximate estimates based on constant speed."
        ),
        "fleet_method": (
            f"Sum of per-route peak overlapping trips with a "
            f"{TURNAROUND_MINUTES}-minute terminal buffer; route interlining is not modeled."
        ),
        "weekday_trips": sum(
            feature["properties"]["weekday_trips"] for feature in route_features
        ),
        "routes": len(route_features),
        "stops": len(stop_features),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output_file:
        json.dump(
            {
                "type": "FeatureCollection",
                "name": "North and Central Delhi bus network",
                "metadata": metadata,
                "features": features,
            },
            output_file,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    print(
        f"Wrote {len(route_features)} routes, {len(stop_features)} stops, "
        f"and {metadata['weekday_trips']:,} weekday trips to {args.output}"
    )


if __name__ == "__main__":
    main()
