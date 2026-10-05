import asyncio
import json
import os
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from agents import Agent, ModelSettings, Runner, RunHooks, SQLiteSession, function_tool, set_tracing_disabled
from agents.exceptions import MaxTurnsExceeded
from agents.extensions.models.litellm_model import LitellmModel
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# Read STADIA_API_KEY and NYC_GEOCLIENT_KEY from .env when running locally.
load_dotenv()

import elevation  # noqa: E402
import tools  # noqa: E402  (reads keys from the environment)

# Open the LiDAR raster now, so a missing file fails at startup rather than on the first route.
elevation.load_dem()

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

MODEL = os.environ.get("MODEL", "vertex_ai/gemini-3.5-flash")
MAX_TURNS = 30  # tool calls plus replies in one answer; open-ended requests use ~20


def vertex_project() -> str | None:
    """The GCP project for Vertex AI, stripped. Left to auto-detect, it can pick up a trailing
    carriage return on Windows and break the request URL."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        import google.auth
        _, project = google.auth.default()
    return (project or "").strip() or None


# --- The Agent ---

INSTRUCTIONS = """
You are Skate Router, a route planner for rollerblading in Manhattan. You find routes that are
gentle enough for the rider and on protected bike lanes wherever possible.

How you know things: your tools grade every stretch of a route with the city's 2017 LiDAR
elevation survey and check bike facilities against NYC DOT's bike map. Trust tool results over
your own memory. Never invent coordinates, grades, or bike-lane facts; if a tool didn't say it,
don't claim it.

## Rider limits
Turn the rider's words into a level and grade limits, and say which you used:
- beginner / nervous / new: max 4% uphill, 3% downhill
- intermediate / comfortable: 6% up, 5% down
- advanced / confident: 10% up, 8% down
If the rider doesn't say, assume beginner and say so. Steep downhills matter most: braking is the
hard part on skates. Short climbs (tools mark them short_climbs_to_walk) are OK for any level:
the rider can walk them, but say where they are.

## Planning
- Point to point: call plan_skate_route. If a place is ambiguous, or plan_skate_route can't find
  it, call find_place. For a campus or big park, pick a specific entrance (e.g. Columbia's main
  gate is Broadway & W 116 St) and say which.
- Open-ended ("suggest 5-mile beginner routes"): call find_skate_loops first, with the level,
  target_miles, near, and avoid the rider gave. Its loops are already checked; recommend from them
  and suggest laps when a loop is short. Then call plan_skate_route with plan_with for each loop
  you recommend, all in the same step, so the rider can see them on the map. Only if no
  ready-made loop fits, build your own with plan_skate_route (at most 5 tries), and say plainly
  when nothing fully meets the limits and which stretch is the problem.
- When you need several independent tool calls (checking a few candidates, planning the routes
  you recommend), make them in one step rather than one after another.
- Loops: same place as origin and destination, plus waypoints. Out-and-back: one waypoint at
  the turnaround. To hit a target distance, move waypoints and re-plan; within ~15% is fine.
- If no route meets the limits: call get_grade_details on the best route_id, pick a waypoint on a
  parallel street that skips the problem stretch, and re-plan. Try this up to 3 times, then
  report the best compromise honestly.
- Avoiding places: pass corridor names in `avoid` and check miles_on_avoided_corridors. The
  "West Side Highway path" is the Hudson River Greenway (and Cherry Walk north of it). Battery
  Park City Greenway runs alongside it; if unsure whether the rider means that too, say what you
  assumed.

## Central Park
Wheels go counterclockwise only on the park drives: north on East Drive, south on West Drive.
The full loop is 6.02 miles. To keep a route on the drives, use waypoints like "East Drive at
E 90 St", "West Drive at W 86 St", "102nd Street Crossing", or "Terrace Drive" (the 72nd St cross
drive) in counterclockwise order; otherwise the router may leave the park onto avenues. The drives
are protected and car-free but hilly; check the tool's verdict before calling them beginner-friendly.

## Answering
Lead with your recommendation. For each route give: miles and minutes, share on protected lanes,
the steepest climb and descent and where, the verdict for the rider's level, and any tradeoff
(stretches with no bike lane, short climbs to walk, places substituted, e.g. "W 80th doesn't meet
CPW, so I started at W 81st"). Bridges and overpasses can show as false steep spots
(possible_artifacts); don't call those hills. If part of a route has no grade data, say so.
Report each level's verdict exactly as the tool gives it. If the tool says "no" for a level,
don't soften it ("a confident intermediate could manage"); name the stretch that fails instead.
Keep it short: a few lines per route, no tables of raw numbers.
""".strip()

agent = Agent(
    name="Skate Router",
    instructions=INSTRUCTIONS,
    # LiteLLM routes the call to Gemini on Vertex AI, authenticated with your gcloud credentials.
    model=LitellmModel(model=MODEL, api_key="unused"),
    model_settings=ModelSettings(
        parallel_tool_calls=True,
        extra_args={"vertex_location": "global", "vertex_project": vertex_project()},
    ),
    tools=[function_tool(f) for f in (tools.plan_skate_route, tools.get_grade_details,
                                      tools.find_place, tools.find_skate_loops)],
)


class RecordToolCalls(RunHooks):
    """Watches the run and records each tool call, for the page to show.

    Make a new one per request: it holds this run's calls. Given a queue, it also reports each call
    as it starts and ends, so the page can show them while the agent is still working.
    """

    def __init__(self, events: asyncio.Queue | None = None):
        self.tool_calls = []
        self.events = events

    async def on_tool_start(self, context, agent, tool):
        if self.events:
            # For a function tool, context is a ToolContext: it carries the call's id and arguments.
            await self.events.put({"type": "tool_start", "id": context.tool_call_id, "name": tool.name,
                                   "args": json.loads(context.tool_arguments)})

    async def on_tool_end(self, context, agent, tool, result):
        call = {"name": tool.name, "args": json.loads(context.tool_arguments), "result": result}
        self.tool_calls.append(call)
        if self.events:
            await self.events.put({"type": "tool_end", "id": context.tool_call_id, **call})


# --- Session Store ---

# Every session lives in one SQLite file. On Cloud Run the filesystem is in memory, so this
# lasts as long as the instance. Planned routes live in tools.ROUTES, also in memory.
SESSIONS_DB = "conversations.db"

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


async def run_chat(request: ChatRequest, hooks: RecordToolCalls) -> ChatResponse:
    # Get or create the session. A new SQLiteSession on the same file sees the same history.
    session_id = request.session_id or str(uuid.uuid4())
    session = SQLiteSession(session_id, SESSIONS_DB)
    try:
        # The Runner is our run_agent() loop. The session loads and saves the history.
        result = await Runner.run(agent, request.message, session=session, hooks=hooks, max_turns=MAX_TURNS)
        response = result.final_output
    except MaxTurnsExceeded:
        response = ("I checked a lot of routes without settling on an answer. Try narrowing it down: "
                    "a neighborhood, a start point, or a shorter distance.")
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response = f"Model call failed: {type(e).__name__}: {str(e)[:300]}"
    return ChatResponse(response=response, session_id=session_id, tool_calls=hooks.tool_calls)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    return await run_chat(request, RecordToolCalls())


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Like /chat, but streams one JSON object per line as the agent works: tool_start and
    tool_end for each tool call, then a final "done" line carrying the same body /chat returns."""
    events: asyncio.Queue = asyncio.Queue()

    async def lines():
        task = asyncio.create_task(run_chat(request, RecordToolCalls(events)))
        while not (task.done() and events.empty()):
            try:
                event = await asyncio.wait_for(events.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            yield json.dumps(event, default=str) + "\n"
        yield json.dumps({"type": "done", **task.result().model_dump()}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/routes/{route_id}")
def get_route(route_id: str):
    """A planned route as GeoJSON for the map, colored by grade and bike facility."""
    geojson = tools.route_geojson(route_id)
    if geojson is None:
        raise HTTPException(404, f"route_id {route_id} not found (routes are kept in memory).")
    return geojson


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        await SQLiteSession(session_id, SESSIONS_DB).clear_session()
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
