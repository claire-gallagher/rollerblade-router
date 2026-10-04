import json import uuid
from pathlib import Path

import requests
import uvicorn
from agents import Agent, ModelSettings, Runner, RunHooks, SQLiteSession, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# --- Tools ---

# @function_tool builds the JSON schema from each function's signature and docstring.
# Open-Meteo is free and needs no API key.
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


@function_tool
def get_weather(location: str) -> str:
    """Get the current weather (temperature, humidity, wind) for a city.

    Args:
        location: City name, e.g. 'New York'.
    """
    try:
        places = requests.get(GEOCODE_URL, params={"name": location, "count": 1}, timeout=10).json()
        if not places.get("results"):
            return json.dumps({"error": f"City '{location}' was not found."})
        place = places["results"][0]

        current = requests.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
            timeout=10,
        ).json()["current"]
    except requests.RequestException as e:
        # The model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": f"Weather service failed: {e}"})

    return json.dumps({
        "location": place["name"],
        "temp_f": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "wind_mph": current["wind_speed_10m"],
    })


@function_tool
def lookup_contact(name: str) -> str:
    """Look up a contact's info by name.

    Args:
        name: The contact's first name, e.g. 'Alice'.
    """
    return json.dumps({"name": name, "email": f"{name.lower()}@example.com"})


# --- The Agent ---

agent = Agent(
    name="Assistant",
    instructions=(
        "You are a helpful assistant. Be concise and friendly. When a question depends on "
        "the weather or outdoor conditions, call get_weather first, then answer in a sentence."
    ),
    # LiteLLM routes the call to Gemini, the same way gemini-web-tool-calling does.
    # api_key is unused: Vertex AI authenticates with your gcloud credentials.
    model=LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused"),
    model_settings=ModelSettings(extra_args={"vertex_location": "global"}),
    tools=[get_weather, lookup_contact],
)


class RecordToolCalls(RunHooks):
    """Watches the run and records each tool call, for the page to show.

    Make a new one per request: it holds this run's calls.
    """

    def __init__(self):
        self.tool_calls = []

    async def on_tool_end(self, context, agent, tool, result):
        # For a function tool, context is a ToolContext: it carries the call's arguments.
        self.tool_calls += [{"name": tool.name, "args": json.loads(context.tool_arguments), "result": result}]


# --- Session Store ---

# Every session lives in one SQLite file. On Cloud Run the filesystem is in memory, so this
# lasts as long as the instance, the same as our old dict. Each instance has its own copy.
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


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # Get or create the session. A new SQLiteSession on the same file sees the same history.
    session_id = request.session_id or str(uuid.uuid4())
    session = SQLiteSession(session_id, SESSIONS_DB)
    hooks = RecordToolCalls()

    try:
        # The Runner is our run_agent() loop. The session loads and saves the history.
        result = await Runner.run(agent, request.message, session=session, hooks=hooks)
        response, tool_calls = result.final_output, hooks.tool_calls
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        await SQLiteSession(session_id, SESSIONS_DB).clear_session()
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
