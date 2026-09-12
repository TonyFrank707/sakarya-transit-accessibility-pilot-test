"""
geojson_to_gtfs_m1.py

Converts an Overpass Turbo GeoJSON export (bus route relations + stop nodes)
into a minimal GTFS feed for the Sakarya M1 Metrobüs pilot route.

Input:  export.geojson  (Overpass export containing the M1 route relations
        and their stop nodes, as pulled via overpass-turbo.eu)
Output: gtfs_m1/  folder containing agency.txt, stops.txt, routes.txt,
        trips.txt, stop_times.txt, calendar.txt

IMPORTANT LIMITATIONS (read before using the output):
- OSM stop nodes in this export carry no 'name' tag, so stop_name is a
  placeholder ("M1 Stop 1", "M1 Stop 2", ...). Replace these with the real
  stop names from the SAKUS map or portal if you want readable output.
- Stop order is inferred by projecting each stop point onto the route
  line and sorting by distance along it. This is a solid approximation
  but not a substitute for the true OSM relation member order - spot check
  a few stops on a map before trusting it fully.
- stop_times.txt currently contains NO real clock times, only placeholder
  10-minute increments so the file is structurally valid. Real timetable
  data for M1 needs to come from the portal's "Metrobüs Saatleri" page -
  swap in the real departure times once you have them.
- This is a pilot / proof-of-pipeline dataset for ONE route. It does not
  cover the other 40 municipal bus lines, which still need to be digitized
  separately (no OSM data exists for those, as confirmed earlier).
"""

import json
import math
import os
import csv

INPUT_FILE = "export.geojson"
OUTPUT_DIR = "gtfs_m1"

# Two direction relation ids for M1, based on the export inspected earlier
DIRECTIONS = {
    20188079: {"trip_id": "M1_outbound", "headsign": "Korucuk"},   # Gar Meydan -> Korucuk
    20188080: {"trip_id": "M1_inbound", "headsign": "Gar Meydan"}, # Korucuk -> Gar Meydan
}


def haversine(lon1, lat1, lon2, lat2):
    """Distance in meters between two lon/lat points."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def flatten_linestring(geometry):
    """Return a single ordered list of [lon, lat] coords from a LineString
    or MultiLineString geometry. For MultiLineString, Overpass does not
    guarantee the parts are listed in travel order, so we stitch them
    together by matching endpoints (nearest-endpoint chaining)."""
    if geometry["type"] == "LineString":
        return geometry["coordinates"]

    parts = [list(p) for p in geometry["coordinates"]]
    if len(parts) <= 1:
        return parts[0] if parts else []

    def dist(a, b):
        return haversine(a[0], a[1], b[0], b[1])

    # Start the chain with the first part, then repeatedly attach whichever
    # remaining part connects best (by nearest endpoint) to the chain's end.
    chain = parts.pop(0)
    while parts:
        best_i, best_flip, best_d = None, False, float("inf")
        for i, part in enumerate(parts):
            d_start = dist(chain[-1], part[0])
            d_end = dist(chain[-1], part[-1])
            if d_start < best_d:
                best_d, best_i, best_flip = d_start, i, False
            if d_end < best_d:
                best_d, best_i, best_flip = d_end, i, True
        part = parts.pop(best_i)
        if best_flip:
            part = part[::-1]
        chain.extend(part)
    return chain


def cumulative_distances(coords):
    """Cumulative distance (m) along a coordinate sequence."""
    dists = [0.0]
    for i in range(1, len(coords)):
        lon1, lat1 = coords[i - 1]
        lon2, lat2 = coords[i]
        dists.append(dists[-1] + haversine(lon1, lat1, lon2, lat2))
    return dists


def project_point_onto_line(pt, coords, cum_dist):
    """Find the distance-along-line value for the point closest to pt,
    by nearest vertex (good enough at bus-stop spacing for this pilot)."""
    lon, lat = pt
    best_i, best_d = 0, float("inf")
    for i, (clon, clat) in enumerate(coords):
        d = haversine(lon, lat, clon, clat)
        if d < best_d:
            best_d = d
            best_i = i
    return cum_dist[best_i]


def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    feats = data["features"]

    # 1. Get the route line geometry for each direction
    route_geoms = {}
    for f in feats:
        props = f.get("properties", {})
        rel_id = props.get("@id", "")
        if rel_id.startswith("relation/"):
            rid = int(rel_id.split("/")[1])
            if rid in DIRECTIONS and f["geometry"]["type"] in ("LineString", "MultiLineString"):
                route_geoms[rid] = flatten_linestring(f["geometry"])

    # 2. Get stop nodes belonging to each direction relation
    stops_by_dir = {rid: [] for rid in DIRECTIONS}
    all_stops = {}  # node_id -> (lon, lat)
    for f in feats:
        if f["geometry"]["type"] != "Point":
            continue
        props = f.get("properties", {})
        node_id = props.get("@id", "").replace("node/", "")
        lon, lat = f["geometry"]["coordinates"]
        for rel in props.get("@relations", []):
            rid = rel.get("rel")
            if rid in DIRECTIONS and rel.get("role") == "stop":
                stops_by_dir[rid].append(node_id)
                all_stops[node_id] = (lon, lat)

    # 3. Order stops along each direction's line, assign sequential stop_ids
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stop_id_map = {}   # node_id -> GTFS stop_id
    stop_rows = []
    next_num = 1

    ordered_by_dir = {}
    for rid, coords in route_geoms.items():
        cum = cumulative_distances(coords)
        node_ids = stops_by_dir[rid]
        with_dist = []
        for nid in node_ids:
            lon, lat = all_stops[nid]
            d = project_point_onto_line((lon, lat), coords, cum)
            with_dist.append((d, nid))
        with_dist.sort()
        ordered_by_dir[rid] = [nid for _, nid in with_dist]

    for rid, ordered_nodes in ordered_by_dir.items():
        for nid in ordered_nodes:
            if nid not in stop_id_map:
                gtfs_id = f"stop_{next_num}"
                stop_id_map[nid] = gtfs_id
                lon, lat = all_stops[nid]
                stop_rows.append({
                    "stop_id": gtfs_id,
                    "stop_name": f"M1 Stop {next_num} (name TBD)",
                    "stop_lat": lat,
                    "stop_lon": lon,
                })
                next_num += 1

    # 4. Write agency.txt
    with open(f"{OUTPUT_DIR}/agency.txt", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["agency_id", "agency_name", "agency_url", "agency_timezone"])
        w.writerow(["SUKAS", "Sakarya Metrobüs (SUKAS)", "https://ulasim.sakarya.bel.tr", "Europe/Istanbul"])

    # 5. Write stops.txt
    with open(f"{OUTPUT_DIR}/stops.txt", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stop_id", "stop_name", "stop_lat", "stop_lon"])
        w.writeheader()
        w.writerows(stop_rows)

    # 6. Write routes.txt
    with open(f"{OUTPUT_DIR}/routes.txt", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"])
        w.writerow(["M1", "SUKAS", "M1", "Gar Meydan - Korucuk Metrobüs", "3"])  # route_type 3 = bus

    # 7. Write calendar.txt (weekday/Saturday/Sunday services, matching the
    #    portal's three schedule tabs - fill exact date ranges as needed)
    with open(f"{OUTPUT_DIR}/calendar.txt", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday", "start_date", "end_date"])
        w.writerow(["WEEKDAY", 1, 1, 1, 1, 1, 0, 0, "20260101", "20261231"])
        w.writerow(["SATURDAY", 0, 0, 0, 0, 0, 1, 0, "20260101", "20261231"])
        w.writerow(["SUNDAY", 0, 0, 0, 0, 0, 0, 1, "20260101", "20261231"])

    # 8. Write trips.txt
    with open(f"{OUTPUT_DIR}/trips.txt", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"])
        for i, (rid, info) in enumerate(DIRECTIONS.items()):
            w.writerow(["M1", "WEEKDAY", info["trip_id"], info["headsign"], i])

    # 9. Write stop_times.txt with PLACEHOLDER times (10-min spacing).
    #    Replace with real times from the portal's Metrobüs Saatleri page.
    with open(f"{OUTPUT_DIR}/stop_times.txt", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
        for rid, info in DIRECTIONS.items():
            ordered_nodes = ordered_by_dir[rid]
            base_minutes = 7 * 60  # placeholder start: 07:00
            for seq, nid in enumerate(ordered_nodes, start=1):
                t = base_minutes + (seq - 1) * 3  # placeholder: 3 min between stops
                hh, mm = divmod(t, 60)
                time_str = f"{hh:02d}:{mm:02d}:00"
                w.writerow([info["trip_id"], time_str, time_str, stop_id_map[nid], seq])

    print(f"Done. {len(stop_rows)} unique stops written.")
    for rid, info in DIRECTIONS.items():
        print(f"  {info['trip_id']}: {len(ordered_by_dir[rid])} stops in sequence")
    print(f"GTFS files written to ./{OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
