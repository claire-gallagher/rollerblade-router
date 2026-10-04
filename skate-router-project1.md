# Project 1: Skate Router — a grade-aware rollerblading route agent

## What it is
A chat agent that plans rollerblading routes in NYC (Manhattan focus) and avoids steep stretches. Google Maps and Apple Maps let you filter by total elevation at best, but not by **short steep segments**, which are what matter on skates. This agent uses Google's bicycle routing for the route itself, then checks each route's **uphill and downhill grade** along its length and steers around stretches that exceed the rider's limits.

Scope for Project 1: rollerblading only, Google bike routing as-is, elevation/grade as the only added data. Keep it as simple as possible. (Project 3 will replace Google's router with a custom network, surface data, and footpath connectors — not needed now.)

## Build on the starter
Build on `gemini-web-tool-calling.zip` (Gemini tool-calling loop). Keep its structure where possible.

## Requirements checklist (from the assignment)
1. **Session memory:** the conversation persists across the session, so follow-ups like "avoid that hill" or "same limits, but from my place" work.
2. **At least three tools:** see below.
3. **External data:** Google Routes API, Elevation API, and Geocoding/Places API.
4. **Original tools:** `plan_skate_route` and `get_grade_details` are domain-specific to this project. *(Team size: [fill in]; one original tool per member is required.)*
5. **Tool-writing guidance:** clear names, descriptions, and argument docs; errors return actionable messages to the model (examples below).
6. **Show tool calls:** keep the starter's `/chat` response shape exactly: `response`, `session_id`, and `tool_calls` (each with `name`, `args`, `result`). Show tool calls in the UI.
7. **New frontend:** different from the starter; makes clear what the agent is and how to use it.

## Tools

### 1. `plan_skate_route(origin, destination, waypoints=[], max_uphill_pct=4, max_downhill_pct=3)`
Plans a route and analyzes its grade.
- Call Google Routes API (`computeRoutes`, travel mode `BICYCLE`, alternatives on). Origin, destination, and waypoints may be addresses or place names.
- For each route: resample the polyline every ~5 m, fetch elevations (Elevation API; batch requests within its per-request sample limit), and compute grade at **two window lengths** (make both configurable):
  - **Short window (~10 m)** catches brief steep stretches. A short steep downhill is still dangerous on skates, so these count against the limits.
  - **Long window (~30 m)** measures sustained hills.
  - Also report each stretch's **total elevation drop** in meters: speed builds with drop, so a 10 m stretch at 8% matters even if the sustained grade looks fine.
- Compute **uphill and downhill separately** relative to travel direction.
- Log the Elevation API's returned `resolution` per sample. If it's coarse (well over ~10 m), short-window grades are unreliable; note it in the result, and plan to switch to NYC's LiDAR elevation model.
- Put all elevation lookups behind one function, `get_elevations(points) -> meters`, so the source can be swapped without touching the tool logic.

### Planned upgrade: NYC 2017 LiDAR elevation
- Use the **2017 LiDAR bare-earth DEM tiles** from New York State's GIS clearinghouse (1 ft resolution, tiled), downloading only tiles covering Manhattan. Do **not** use the NYC Open Data **integer** raster: it rounds to whole feet, which adds several percent of error to short-window grades.
- One-time prep script: mosaic the tiles, resample to ~1 m, convert feet to meters, and save as a compressed cloud-optimized GeoTIFF in Cloud Storage.
- `get_elevations`: reproject lat/lng to the raster's coordinate system (NY State Plane, US survey feet) with `pyproj`, then sample with `rasterio` using bilinear interpolation.
- The DEM is bare-earth and water-flattened, so bridges read as dropping to water level. Keep the bridge/artifact flag.
- Store full geometry and per-point grades server-side under a `route_id`. Do **not** send coordinates to the model.
- Return a compact summary per route: `route_id`, distance, duration, max uphill % and max downhill % (with the street name from the matching Google step), number of stretches over each limit, and whether the route meets the limits.

### 2. `get_grade_details(route_id)`
Returns the stretches of a previously planned route that exceed the limits (or the steepest few if none do): street name, direction (up/down), grade, length, and approximate cross street or location. The model uses this to choose a waypoint that steers around a problem stretch.

### 3. `find_place(query, near="Manhattan, NY")`
Geocoding/Places lookup that resolves landmarks and ambiguous names ("the boathouse in Central Park," "Pier 25," "Fort Tryon Park") and suggests nearby streets or places to use as detour waypoints. Returns name, address, and lat/lng.

### Actionable error examples
- No route: "No bicycle route found from X to Y. Check spelling or use `find_place` to resolve a landmark."
- Unknown id: "route_id abc123 not found. Call `plan_skate_route` first."
- All routes over limits: return results anyway with `meets_limits: false` and the worst stretch, plus: "Try `get_grade_details` and add a waypoint to avoid it."
- Elevation API failure or partial data: say which part of the route lacks grade data rather than failing silently.
- Implausible grades (> ~12%, often bridges or overpasses where elevation data measures the ground below): flag as "possible elevation artifact," don't treat as a real hill.

## Agent behavior (system prompt essentials)
- Turn the rider's words into limits (e.g., nervous beginner → ~3% down / 4% up; confident → higher). State the limits used.
- If no alternative meets the limits, inspect details, add a waypoint, and retry a couple of times before reporting the best compromise.
- Explain the chosen route plainly: distance, time, steepest up/down stretch and where, and any tradeoffs.
- Steep **downhills** matter more than uphills on skates (braking is the danger).
- Manhattan context: most of Midtown and Downtown is gentle, while Upper Manhattan (Harlem heights, Washington Heights, Inwood) and the Central Park drives have the real hills. Greenway ramps and bridge approaches are common steep spots.

## Frontend
- Header explaining what the agent does, with 2–3 clickable example prompts.
- Chat panel + map panel (Leaflet + OpenStreetMap tiles); stacked on mobile.
- Draw the chosen route colored by grade (e.g., green < 2%, yellow 2–limit, red over limit). Fetch geometry from a new endpoint, `GET /routes/{route_id}`, returning GeoJSON.
- Collapsible tool-call panel showing each call's name, args, and result.
- Show the turn-by-turn steps from Google as a cue sheet under the chat reply.

## Config and deployment
- Single API key env var for Google Maps Platform (enable Routes, Elevation, and Geocoding/Places in the GCP console); Gemini key per the starter.
- Deploy to Cloud Run. Routes are stored in memory for now, so set max instances to 1 so `route_id`s stay valid.

## Out of scope for Project 1
Biking/Citi Bike, custom routing graph, pavement quality, footpath connectors, GPX export, user accounts.
