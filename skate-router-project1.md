# Project 1: Skate Router — a grade-aware rollerblading route agent

## What it is
A chat agent that plans rollerblading routes in Manhattan, avoids steep stretches, and prefers protected bike lanes. Map apps filter by total elevation at best, not by **short steep segments**, which are what matter on skates. This agent routes with an open bicycle router tuned for skating, then checks each route's **uphill and downhill grade** along its length with the city's LiDAR survey, and its **bike-lane coverage** with NYC DOT's bike map. It steers around stretches that exceed the rider's limits.

Scope for Project 1: rollerblading in Manhattan; Valhalla bike routing tuned for skating; LiDAR grades and DOT bike facilities as the added data. Keep it as simple as possible. (Project 3 can replace the hosted router with a custom network that bakes in grades, surface data, and footpath connectors.)

## Requirements checklist (from the assignment)
1. **Session memory:** the conversation persists across the session, so follow-ups like "avoid that hill" or "same limits, but from my place" work.
2. **At least three tools:** four, see below.
3. **External data:** Stadia Maps (Valhalla routing, Pelias geocoding, on OpenStreetMap), NYC Geoclient v2 (addresses and intersections), NYC 2017 LiDAR bare-earth DEM (via NOAA), NYC DOT bike routes (NYC Open Data).
4. **Original tools:** `plan_skate_route`, `get_grade_details`, and `find_skate_loops` are domain-specific to this project. *(Team size: [fill in]; one original tool per member is required.)*
5. **Tool-writing guidance:** clear names, descriptions, and argument docs; errors return actionable messages to the model (examples below).
6. **Show tool calls:** keep the starter's `/chat` response shape exactly: `response`, `session_id`, and `tool_calls` (each with `name`, `args`, `result`). Show tool calls in the UI.
7. **New frontend:** different from the starter; makes clear what the agent is and how to use it.

## Data sources (and why)
| Need | Source | Why |
|---|---|---|
| Routing | Stadia Maps hosted Valhalla, `bicycle` costing | Tunable: `use_roads` (stay on bike paths), `use_hills`, `avoid_bad_surfaces`, skating speed. Open data, so it can be drawn on a Leaflet/OSM map. |
| Landmarks | Stadia Maps Pelias geocoding | Good on official names when queried bare (no ", New York" suffix). |
| Addresses, intersections | NYC Geoclient v2 (`api.nyc.gov/geoclient/v2`) | The city's own geocoder; exact where Pelias is weak. |
| Elevation | 2017 NYC Topobathy LiDAR bare-earth DEM (NOAA InPort 64732, 1 ft Float32) | Newest LiDAR covering all of Manhattan. Google Elevation's 76 m resolution produced phantom 10–12% grades, so it is not used at all. |
| Bike facilities | NYC DOT "New York City Bike Routes" (NYC Open Data mzxg-pwib) | Facility type per direction of travel: protected, painted, shared. |
| Park drive waypoints | OpenStreetMap (Overpass) geometry + Geoclient anchors | Geocoders can't find "West Drive at W 86 St"; the agent needs these to keep routes on the Central Park drives. |

Google Maps Platform was dropped: its terms (§3.2.3(e)) forbid using its routes or Places results on a non-Google map, and its elevation data was too coarse.

## Rider levels
| Level | Max uphill | Max downhill |
|---|---|---|
| beginner | 4% | 3% |
| intermediate | 6% | 5% |
| advanced | 10% | 8% |

**Downhills are strict, uphills lenient:** a climb of ≤ 3 m and ≤ 75 m is "walkable" and doesn't fail a level (reported as `short_climbs_to_walk`). Any descent over the limit fails it: braking is the danger on skates.

## Tools

### 1. `plan_skate_route(origin, destination, waypoints=[], max_uphill_pct=4, max_downhill_pct=3, avoid=[])`
Plans routes and analyzes grade and bike lanes.
- Resolve places: the gazetteer, then Geoclient, then Pelias, stopping at the first hit (cached). Results outside New York or outside the LiDAR area are dropped.
- Route with Valhalla (`bicycle`, `use_roads=0.1`, `use_hills` from the limits, 13 km/h, up to 3 alternates; Valhalla gives none when waypoints are set).
- For each route: resample every 5 m, sample the LiDAR raster (bilinear), and compute grade at **two windows**: 10 m (brief steep stretches) and 30 m (sustained hills). Report each stretch's **elevation change**: speed builds with drop.
- A stretch's elevation change is measured across the **whole hill** it sits on (while the grade stays steeper than 1.5%; gentler ground doesn't build speed on skates). Ignore stretches whose hill changes less than 1 m. Grades over 12% are flagged `possible_artifact` (bare-earth DEM reads bridges and overpasses as dropping to the ground below).
- Match each sample to DOT bike facilities within 15 m, in the direction of travel (a one-way protected lane only counts going its way).
- `avoid`: corridor names (DOT names, e.g. "Hudson River Greenway"). Report-only: every route says `miles_on_avoided_corridors`, and the agent steers off with waypoints. (Valhalla's `exclude_polygons` was tried and dropped: its 10 km perimeter cap and blocked cross streets meant it never improved a route in testing.) `find_skate_loops` never suggests loops on avoided corridors.
- All elevation lookups go through `get_elevations(points) -> meters`, so the source can be swapped.
- Store full geometry and per-point grades server-side under a `route_id`. Do **not** send route coordinates to the model.
- Return per route: `route_id`, miles, minutes, bike-facility shares, steepest climb and descent with street, stretches over the limits, `meets_limits` (true / false / "unknown" with data gaps), `suitable_for` each level, streets in order, and the resolved place names.

### 2. `get_grade_details(route_id)`
Returns the stretches over the route's limits (or the steepest few if none): street, the streets before and after, direction, grade (peak and sustained), length, elevation change, mile marker, bike facility, walkable-climb and artifact flags, plus the cue sheet. The model uses this to choose a waypoint that steers around a problem stretch.

### 3. `find_place(query)`
Candidates for a place from every source, best first, each with name, type, source, and a `lat,lng` to pass on. Resolves landmarks ("Central Park Boathouse"), addresses ("2 Broadway"), intersections ("Riverside Dr & W 96 St"), and Central Park drive points ("West Drive at W 86 St", "102nd Street Crossing").

### 4. `find_skate_loops(level="beginner", target_miles=None, near=None, avoid=[], limit=5)`
For open-ended requests ("suggest 5-mile beginner routes"). Reads a prebuilt catalog (`scripts/prep_loops.py`) of loops and out-and-backs that were routed with Valhalla and graded once, offline:
- each one-way protected DOT corridor is paired with a parallel one running the other way (8th Ave up / 9th Ave down) into a loop over the stretch they share; two-way greenways become out-and-backs
- loops that fail for beginners or intermediates are shortened to the longest version that passes
- results are ranked by how close a whole number of laps gets to `target_miles`, and by distance from `near`; each returns the places to pass to `plan_skate_route` so it can be drawn on the map

This replaced the agent's trial and error over single corridors (about 20 tool calls per open-ended request).

### Actionable error examples
- Place not found: "Could not find 'X' in Manhattan. Call find_place to look it up, or use the place's official name, a street address, or an intersection like 'Riverside Dr & W 96 St'."
- Outside coverage: "40.87, -74.03 is outside the area this router covers (Manhattan, where there is grade data)."
- No route: "No skateable route found from X to Y. Check the places with find_place, or try nearby ones."
- Unknown id: "route_id abc123 not found. Call plan_skate_route first."
- All routes over limits: return results anyway with `meets_limits: false` and the worst descent, plus: "Call get_grade_details on the best route_id and add a waypoint to steer around the problem stretch."
- Avoided corridor still used: "Some routes use the avoided corridors (see miles_on_avoided_corridors). Add waypoints on a parallel street to stay off them."
- Missing LiDAR data: `no_grade_data` lists which streets lack grades; the verdict becomes "unknown", never "yes".

## Agent behavior (system prompt essentials)
- Turn the rider's words into a level and limits; assume beginner if unstated; state the limits used.
- If no route meets the limits, inspect details, add a waypoint on a parallel street, and retry up to 3 times before reporting the best compromise.
- Open-ended requests: `find_skate_loops` first; recommend from its verified loops (with laps to reach the target distance) and plan the chosen ones in one parallel step; never suggest routes from memory alone.
- Explain the chosen route plainly: miles, time, protected-lane share, steepest up/down and where, verdict for the rider's level (exactly as the tool gives it), and tradeoffs (no-lane stretches, short climbs to walk, substituted places).
- Central Park: counterclockwise only (north on East Drive, south on West Drive); full loop 6.02 mi; keep routes on the drives with gazetteer waypoints. The drives are protected but hilly, and fail the beginner check.
- Campuses and big parks: pick a specific entrance and say which.
- "West Side Highway path" = Hudson River Greenway (and Cherry Walk); say what was assumed about Battery Park City Greenway.

## Frontend
- Header explaining what the agent does, with 2–3 clickable example prompts.
- Chat panel + map panel (Leaflet + OpenStreetMap tiles, with OSM attribution); stacked on mobile.
- Draw the chosen route colored by grade (green < 2%, yellow 2%–limit, red over limit) and show bike-facility type. Fetch geometry from `GET /routes/{route_id}`, returning GeoJSON.
- Collapsible tool-call panel showing each call's name, args, and result.
- Show the turn-by-turn steps as a cue sheet under the chat reply.

## Config and deployment
- `STADIA_API_KEY` (free, non-commercial) and `NYC_GEOCLIENT_KEY` (free); Gemini via Vertex AI per the starter (`gcloud auth application-default login`).
- One-time data prep (`data/`, gitignored): `uv run scripts/prep_data.py` runs `prep_lidar.py` (1 m Manhattan DEM, 135 MB), `prep_bike_lanes.py`, `prep_gazetteer.py`, `prep_corridors.py`, and `prep_loops.py`, skipping outputs that exist.
- Deploy to Cloud Run with the data files in a Cloud Storage bucket mounted as a volume. Routes are stored in memory, so set max instances to 1 so `route_id`s stay valid.

## Out of scope for Project 1
Biking/Citi Bike, custom routing graph, pavement quality beyond Valhalla's `avoid_bad_surfaces`, footpath connectors, GPX export, user accounts, routes outside Manhattan.
