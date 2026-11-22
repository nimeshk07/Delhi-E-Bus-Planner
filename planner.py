from collections import defaultdict
from math import ceil
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_array, vstack


BUS_COST_LAKH = 50.0
CHARGER_COST_LAKH = 20.0
BUSES_PER_CHARGER = 3
CHARGER_POWER_MW = 0.25
MAX_SITE_DISTANCE_KM = 1.5
MAX_SITE_OPTIONS_PER_ROUTE = 8
EARTH_RADIUS_KM = 6371.0


def _distance_matrix_km(
    stops: list[dict[str, Any]],
) -> np.ndarray:
    coordinates = np.radians(
        np.array(
            [
                [
                    float(stop["geometry"]["coordinates"][1]),
                    float(stop["geometry"]["coordinates"][0]),
                ]
                for stop in stops
            ],
            dtype=float,
        )
    )
    latitudes = coordinates[:, 0]
    longitudes = coordinates[:, 1]
    delta_lat = latitudes[:, None] - latitudes[None, :]
    delta_lon = longitudes[:, None] - longitudes[None, :]
    haversine = (
        np.sin(delta_lat / 2) ** 2
        + np.cos(latitudes[:, None])
        * np.cos(latitudes[None, :])
        * np.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(haversine, 0, 1)))


def solve_plan(
    routes: list[dict[str, Any]],
    stops: list[dict[str, Any]],
    budget_crore: float,
    transformer_capacity_mw: float,
    mode: str = "budget_constrained",
) -> dict[str, Any]:
    if not routes or not stops:
        raise ValueError("The route and stop data must both be non-empty.")
    if budget_crore < 0 or transformer_capacity_mw < 0:
        raise ValueError("Budget and transformer capacity cannot be negative.")
    if mode not in {"budget_constrained", "full_electrification"}:
        raise ValueError(f"Unsupported planning mode: {mode}")

    route_ids = [str(route["properties"]["route_id"]) for route in routes]
    route_buses = np.array(
        [int(route["properties"]["buses_required"]) for route in routes],
        dtype=int,
    )
    route_trips = np.array(
        [int(route["properties"]["weekday_trips"]) for route in routes],
        dtype=int,
    )
    if np.any(route_buses <= 0) or np.any(route_trips <= 0):
        raise ValueError("Every route must have positive fleet and trip requirements.")

    stop_ids = [str(stop["properties"]["stop_id"]) for stop in stops]
    stop_index = {stop_id: index for index, stop_id in enumerate(stop_ids)}
    distance_matrix = _distance_matrix_km(stops)

    assignment_pairs: list[tuple[int, int, float]] = []
    for route_index, route in enumerate(routes):
        route_stop_indices = [
            stop_index[stop_id]
            for stop_id in route["properties"]["region_stop_ids"]
            if stop_id in stop_index
        ]
        if not route_stop_indices:
            raise ValueError(
                f"Route {route_ids[route_index]} has no stops in the planning area."
            )
        distances = np.min(distance_matrix[route_stop_indices, :], axis=0)
        eligible_sites = np.flatnonzero(distances <= MAX_SITE_DISTANCE_KM)
        if eligible_sites.size > MAX_SITE_OPTIONS_PER_ROUTE:
            nearest = np.argsort(distances[eligible_sites])[:MAX_SITE_OPTIONS_PER_ROUTE]
            eligible_sites = eligible_sites[nearest]
        assignment_pairs.extend(
            (route_index, int(site_index), float(distances[site_index]))
            for site_index in eligible_sites
        )

    route_count = len(routes)
    site_count = len(stops)
    pair_count = len(assignment_pairs)
    route_start = 0
    assignment_start = route_count
    station_start = assignment_start + pair_count
    variable_count = station_start + site_count
    maximum_station_units = sum(
        ceil(int(buses) / BUSES_PER_CHARGER) for buses in route_buses
    )

    row_indices: list[int] = []
    column_indices: list[int] = []
    coefficients: list[float] = []
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []

    route_assignment_rows: list[list[int]] = [[] for _ in routes]
    site_capacity_rows: list[list[int]] = [[] for _ in stops]
    for pair_index, (route_index, site_index, _) in enumerate(assignment_pairs):
        variable_index = assignment_start + pair_index
        route_assignment_rows[route_index].append(variable_index)
        site_capacity_rows[site_index].append(variable_index)

    row_index = 0
    for route_index, assignment_variables in enumerate(route_assignment_rows):
        row_indices.append(row_index)
        column_indices.append(route_start + route_index)
        coefficients.append(-float(route_buses[route_index]))
        for variable_index in assignment_variables:
            row_indices.append(row_index)
            column_indices.append(variable_index)
            coefficients.append(1.0)
        lower_bounds.append(0.0)
        upper_bounds.append(0.0)
        row_index += 1

    for site_index, assignment_variables in enumerate(site_capacity_rows):
        for variable_index in assignment_variables:
            row_indices.append(row_index)
            column_indices.append(variable_index)
            coefficients.append(1.0)
        row_indices.append(row_index)
        column_indices.append(station_start + site_index)
        coefficients.append(-float(BUSES_PER_CHARGER))
        lower_bounds.append(-np.inf)
        upper_bounds.append(0.0)
        row_index += 1

    for route_index, buses in enumerate(route_buses):
        row_indices.append(row_index)
        column_indices.append(route_start + route_index)
        coefficients.append(float(buses) * BUS_COST_LAKH)
    for site_index in range(site_count):
        row_indices.append(row_index)
        column_indices.append(station_start + site_index)
        coefficients.append(CHARGER_COST_LAKH)
    lower_bounds.append(-np.inf)
    upper_bounds.append(float(budget_crore) * 100.0)
    row_index += 1

    for site_index in range(site_count):
        row_indices.append(row_index)
        column_indices.append(station_start + site_index)
        coefficients.append(CHARGER_POWER_MW)
    lower_bounds.append(-np.inf)
    upper_bounds.append(float(transformer_capacity_mw))

    matrix = coo_array(
        (coefficients, (row_indices, column_indices)),
        shape=(row_index + 1, variable_count),
    ).tocsc()
    variable_upper_bounds = np.ones(variable_count, dtype=float)
    for pair_index, (route_index, _, _) in enumerate(assignment_pairs):
        variable_upper_bounds[assignment_start + pair_index] = route_buses[
            route_index
        ]
    variable_upper_bounds[station_start:] = maximum_station_units
    variable_lower_bounds = np.zeros(variable_count, dtype=float)
    if mode == "full_electrification":
        variable_lower_bounds[route_start:assignment_start] = 1.0
    base_lower_bounds = np.array(lower_bounds, dtype=float)
    base_upper_bounds = np.array(upper_bounds, dtype=float)
    fixed_rows: list[dict[int, float]] = []
    fixed_values: list[float] = []

    def solve_stage(objective: np.ndarray) -> Any:
        if fixed_rows:
            fix_row_indices = []
            fix_column_indices = []
            fix_coefficients = []
            for fixed_row_index, fixed_row in enumerate(fixed_rows):
                for column_index, value in fixed_row.items():
                    fix_row_indices.append(fixed_row_index)
                    fix_column_indices.append(column_index)
                    fix_coefficients.append(value)
            extra_constraints = coo_array(
                (
                    fix_coefficients,
                    (fix_row_indices, fix_column_indices),
                ),
                shape=(len(fixed_rows), variable_count),
            ).tocsc()
            constraints_matrix = vstack(
                (matrix, extra_constraints), format="csc"
            )
            extra_bounds = np.array(fixed_values, dtype=float)
            constraint_lower = np.concatenate((base_lower_bounds, extra_bounds))
            constraint_upper = np.concatenate((base_upper_bounds, extra_bounds))
        else:
            constraints_matrix = matrix
            constraint_lower = base_lower_bounds
            constraint_upper = base_upper_bounds
        return milp(
            c=objective,
            integrality=np.ones(variable_count, dtype=np.uint8),
            bounds=Bounds(variable_lower_bounds, variable_upper_bounds),
            constraints=LinearConstraint(
                constraints_matrix,
                constraint_lower,
                constraint_upper,
            ),
            options={"time_limit": 30.0, "mip_rel_gap": 0.0},
        )

    trip_objective = np.zeros(variable_count, dtype=float)
    trip_objective[route_start:assignment_start] = -route_trips
    bus_objective = np.zeros(variable_count, dtype=float)
    bus_objective[route_start:assignment_start] = route_buses
    station_objective = np.zeros(variable_count, dtype=float)
    station_objective[station_start:] = 1.0
    investment_objective = np.zeros(variable_count, dtype=float)
    investment_objective[route_start:assignment_start] = route_buses * BUS_COST_LAKH
    investment_objective[station_start:] = CHARGER_COST_LAKH
    distance_objective = np.zeros(variable_count, dtype=float)
    for pair_index, (route_index, _, distance_km) in enumerate(assignment_pairs):
        distance_objective[assignment_start + pair_index] = distance_km

    if mode == "full_electrification":
        objectives = [
            ("investment", investment_objective),
            ("charging-site distance", distance_objective),
        ]
    else:
        objectives = [
            ("weekday trips", trip_objective),
            ("bus fleet", bus_objective),
            ("charging units", station_objective),
            ("charging-site distance", distance_objective),
        ]
    result = None
    failed_stage = ""
    for stage_index, (stage_name, objective) in enumerate(objectives):
        result = solve_stage(objective)
        if result.x is None:
            if result.status == 2:
                return {
                    "feasible": False,
                    "optimal": False,
                    "mode": mode,
                    "status": result.message,
                }
            raise RuntimeError(
                f"The {stage_name} MILP found no solution: {result.message}"
            )
        if not result.success:
            failed_stage = stage_name
            break
        stage_solution = np.rint(result.x).astype(int)
        if stage_index < len(objectives) - 1:
            fixed_rows.append(
                {
                    index: float(coefficient)
                    for index, coefficient in enumerate(objective)
                    if coefficient
                }
            )
            fixed_values.append(float(np.dot(objective, stage_solution)))
    if result.x is None:
        raise RuntimeError(f"The fleet and charger MILP found no solution: {result.message}")

    solution = np.rint(result.x).astype(int)
    selected_route_indices = [
        index
        for index in range(route_count)
        if solution[route_start + index] == 1
    ]
    route_to_sites: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for pair_index, (route_index, site_index, _) in enumerate(assignment_pairs):
        assigned_route_buses = int(solution[assignment_start + pair_index])
        if assigned_route_buses > 0:
            route_to_sites[route_index].append((site_index, assigned_route_buses))

    assigned_buses: dict[int, int] = defaultdict(int)
    for route_index in selected_route_indices:
        if route_index not in route_to_sites:
            raise RuntimeError(
                f"Selected route {route_ids[route_index]} has no assigned charging location."
            )
        for site_index, buses_at_site in route_to_sites[route_index]:
            assigned_buses[site_index] += buses_at_site

    locations = []
    for site_index, stop in enumerate(stops):
        station_units = int(solution[station_start + site_index])
        if station_units <= 0:
            continue
        properties = stop["properties"]
        locations.append(
            {
                "stop_id": str(properties["stop_id"]),
                "name": properties["name"],
                "district": properties["district"],
                "latitude": float(stop["geometry"]["coordinates"][1]),
                "longitude": float(stop["geometry"]["coordinates"][0]),
                "station_units": station_units,
                "bus_capacity": station_units * BUSES_PER_CHARGER,
                "assigned_buses": assigned_buses[site_index],
                "power_mw": station_units * CHARGER_POWER_MW,
            }
        )

    selected_routes = []
    for route_index in selected_route_indices:
        route = routes[route_index]
        properties = route["properties"]
        charging_stops = [
            (
                stops[site_index]["properties"]["name"],
                buses_at_site,
                str(stops[site_index]["properties"]["stop_id"]),
            )
            for site_index, buses_at_site in route_to_sites[route_index]
        ]
        selected_routes.append(
            {
                "route_id": route_ids[route_index],
                "name": properties["name"],
                "description": properties.get("description", ""),
                "weekday_trips": int(route_trips[route_index]),
                "buses_required": int(route_buses[route_index]),
                "charging_stops": ", ".join(
                    f"{name} ({bus_count} buses)"
                    for name, bus_count, _ in charging_stops
                ),
                "charging_stop_ids": [stop_id for _, _, stop_id in charging_stops],
            }
        )
    selected_routes.sort(
        key=lambda route: (-route["weekday_trips"], route["name"], route["route_id"])
    )
    selected_trip_count = int(route_trips[selected_route_indices].sum())
    total_trip_count = int(route_trips.sum())
    selected_bus_count = int(route_buses[selected_route_indices].sum())
    station_count = int(solution[station_start:].sum())
    total_cost_lakh = (
        selected_bus_count * BUS_COST_LAKH
        + station_count * CHARGER_COST_LAKH
    )

    return {
        "feasible": True,
        "status": (
            f"{failed_stage}: {result.message}" if failed_stage else result.message
        ),
        "optimal": not failed_stage and bool(result.success),
        "mode": mode,
        "routes_served": len(selected_route_indices),
        "route_count": route_count,
        "buses": selected_bus_count,
        "stations": station_count,
        "locations": locations,
        "location_count": len(locations),
        "power_mw": station_count * CHARGER_POWER_MW,
        "investment_crore": total_cost_lakh / 100.0,
        "weekday_trips_served": selected_trip_count,
        "weekday_trips_total": total_trip_count,
        "trip_coverage_percent": (
            100.0 * selected_trip_count / total_trip_count
            if total_trip_count
            else 0.0
        ),
        "routes": selected_routes,
        "assignment_pairs": pair_count,
    }


def full_service_estimate(routes: list[dict[str, Any]]) -> dict[str, float | int]:
    buses = sum(int(route["properties"]["buses_required"]) for route in routes)
    stations = sum(
        ceil(int(route["properties"]["buses_required"]) / BUSES_PER_CHARGER)
        for route in routes
    )
    theoretical_minimum_stations = ceil(buses / BUSES_PER_CHARGER)
    cost_lakh = buses * BUS_COST_LAKH + stations * CHARGER_COST_LAKH
    return {
        "buses": buses,
        "stations": stations,
        "theoretical_minimum_stations": theoretical_minimum_stations,
        "budget_crore": cost_lakh / 100.0,
        "transformer_mw": stations * CHARGER_POWER_MW,
        "weekday_trips": sum(
            int(route["properties"]["weekday_trips"]) for route in routes
        ),
    }
