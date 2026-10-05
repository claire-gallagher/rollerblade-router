# Skate Router

As I've learned to inline skate in the last year, I get bored of skating the same path all the time but am wary of venturing into the streets of NYC unprepared like I might on a bike. Apple & Google Maps bike routing is a start, but not transparent enough. Strava does not allow for the granularity of elevation filtering for me to feel safe to try a new route.

This tool-calling agent plans **rollerblading routes in Manhattan**: steep hills (especially downhills) are avoided and protected bike lanes are prioritized.

Skate Router works by starting with an route from the open-source navigation API, and then fine tunes the result for inline skating. 

- The proposed routes are split into 5m segments and graded with the city's most recent LiDAR elevation data, and checked against NYC DOT's bike route segments.

- It reports each route's protected-lane share, its steepest climb and descent and where they are, and a verdict for beginner, intermediate and advanced riders. 
- When a route fails, the agent adds waypoints to steer around the problem stretch. 
- For open-ended requests ("suggest some 5-mile beginner routes") it recommends from a catalog of loops that were verified ahead of time.

Built on FastAPI, the OpenAI Agents SDK and Gemini (via LiteLLM on Vertex AI), with a Leaflet map frontend.

## Data sources

| Need | Source | Notes |
|---|---|---|
| Elevation | **2017 NYC Topobathymetric LiDAR**, bare-earth DEM, 1 ft Float32 ([NOAA InPort 64732](https://www.fisheries.noaa.gov/inport/item/64732)) | Streamed from [NOAA's cloud-optimized copy](https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/NYC_topobathy_BE_DEM_2017_9307/index.html) (only the Manhattan window, from the 2 ft overview) and averaged to a 1 m raster. Same survey as [NYC Open Data's 1 ft DEM](https://data.cityofnewyork.us/City-Government/1-foot-Digital-Elevation-Model-DEM-/dpc8-z3jc/about_data), without the 121 GB download. |
| Bike facilities | NYC DOT, [New York City Bike Routes](https://data.cityofnewyork.us/dataset/New-York-City-Bike-Routes/mzxg-pwib) | Facility type for each direction of travel (protected / painted / shared), so a one-way protected lane only counts when you skate it the right way. |
| Routing | [Stadia Maps](https://stadiamaps.com/) hosted [Valhalla](https://valhalla.github.io/valhalla/api/route/api-reference/), `bicycle` costing on OpenStreetMap | Tuned for skating: `use_roads=0.1` (stay on paths and lanes), `use_hills`, `avoid_bad_surfaces`, 13 km/h. |
| Landmarks | Stadia Maps geocoding (Pelias) | Best with official names sent bare (no ", New York" suffix). |
| Addresses, intersections | [NYC Geoclient v2](https://api-portal.nyc.gov/api-details#api=geoclient-current-v2&operation=get-address-housenumber-housenumber-street-street) (NYC Department of City Planning) | The city's own geocoder; exact where Pelias is weak. Base URL `https://api.nyc.gov/geoclient/v2/`. |
| Central Park drive waypoints | OpenStreetMap via [Overpass](https://overpass-api.de/), anchored with Geoclient | Points like "West Drive at W 86 St" that geocoders can't find, so routes can stay on the car-free drives. |


## Tools

| Tool | What it does |
|---|---|
| `plan_skate_route(origin, destination, waypoints, max_uphill_pct, max_downhill_pct, avoid)` | Routes with Valhalla, grades each route with LiDAR (10 m and 30 m windows, each hill's drop measured over the whole hill), matches bike facilities, and returns compact summaries with a `route_id`. Route geometry stays on the server. Loops are origin = destination plus waypoints. |
| `get_grade_details(route_id)` | The stretches over the limits: street, direction (uphill/downhill), grade, length, drop, bike facility, and whether a climb is short enough to walk. The agent uses this to pick detour waypoints. |
| `find_place(query)` | Candidates from the Central Park gazetteer, NYC Geoclient and Pelias, best first. |
| `find_skate_loops(level, target_miles, near, avoid)` | Ready-made loops (one-way protected lanes paired up, like 8th Ave up / 9th Ave down) and greenway out-and-backs, verified offline and ranked by laps to the target distance. Cuts open-ended requests from about 20 tool calls to about 5. |

**Rider levels:**

| Level | Max uphill | Max downhill |
|---|---|---|
| beginner | 4% | 3% |
| intermediate | 6% | 5% |
| advanced | 10% | 8% |

These are hyperparemeters that can be adjusted in `grade.py`

## Running Locally

1. Get two free API keys and put them in `.env` (see `.env.example`):
   - `STADIA_API_KEY` from [Stadia Maps](https://client.stadiamaps.com/signup/)
   - `NYC_GEOCLIENT_KEY` from the [NYC API Portal](https://api-portal.nyc.gov/): subscribe to *Geoclient v2 User*
2. Sign in for Gemini on Vertex AI: `gcloud auth application-default login`
3. Build the data in `data/` once, in about 10 minutes: `uv run scripts/prep_data.py`
4. Check the keys and data: `uv run scripts/smoke.py`
5. Start the app with `uv run app.py`, then open http://localhost:8000

Tests: `uv run pytest`. Terminal chat: `uv run scripts/chat_cli.py "your question"`.

## Deploying

Continuous deployment from GitHub to Cloud Run (Developer Connect, buildpack, IAP limited to columbia.edu). `data/` is too big for GitHub, so it lives in a Cloud Storage bucket mounted into the service:

1. `gcloud auth login`, then `bash scripts/setup_gcp.sh`. This uploads `data/` to a bucket, stores the two API keys in Secret Manager, and gives the service's account access to both and to Vertex AI.
2. When creating the service, set:
   - **Entrypoint:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Volume:** the Cloud Storage bucket, read-only, mounted at `/data`, plus the env var `DATA_DIR=/data`
   - **Secrets as env vars:** `STADIA_API_KEY` and `NYC_GEOCLIENT_KEY`
   - **Max instances 1**, because planned routes live in memory
   - **Memory 1 GiB**

## Limitations and Next Steps

I was surprised that to get the resolution needed (sub meter) for elevation data, the [latest update for Manhattan was 2017.](https://data.gis.ny.gov/maps/nys-latest-lidar-collections/explore?location=40.845985%2C-73.999717%2C9) There has surely been lots of changes to the streetscape in the last decade, so these routes still have an asterisk: known your skating ability and never be afraid to scout the route out on a bike or on foot first!

I wish to revisit this project later in the semester to build out more functions other than skating, namely personalized, transparent bike routing across the NYC metro area. This general idea was inspired by [BikeButler](https://dl.acm.org/doi/10.1145/3772318.3791292), a pilot research project from the University of Washington.

