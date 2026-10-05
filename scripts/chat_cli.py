"""Talk to the agent from the terminal, one session across messages.

Run:  uv run scripts/chat_cli.py "first message" "follow-up" ...
      uv run scripts/chat_cli.py            (interactive; blank line to quit)
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
from agents import Runner, SQLiteSession  # noqa: E402


class CountTurns(app.RecordToolCalls):
    """Also counts model replies: parallel tool calls should mean fewer turns for the same calls."""

    def __init__(self):
        super().__init__()
        self.turns = 0

    async def on_llm_end(self, context, agent, response):
        self.turns += 1


async def ask(session, message):
    hooks = CountTurns()
    start = time.time()
    print(f"\n>>> {message}")
    try:
        reply = (await Runner.run(app.agent, message, session=session, hooks=hooks, max_turns=app.MAX_TURNS)).final_output
    except Exception as e:  # show the calls that happened before the failure
        reply = f"[run failed: {type(e).__name__}: {e}]"
    for call in hooks.tool_calls:
        result = call["result"] if isinstance(call["result"], str) else json.dumps(call["result"])
        error = json.loads(result).get("error") if result.startswith('{"error"') else None
        print(f"  [tool] {call['name']}({json.dumps(call['args'])[:160]}) -> {len(result)} chars"
              + (f"  ERROR: {error[:120]}" if error else ""))
    print(f"--- reply ({time.time() - start:.0f}s, {len(hooks.tool_calls)} tool calls, {hooks.turns} model turns)\n{reply}")


async def main():
    session = SQLiteSession(str(uuid.uuid4()), ":memory:")
    if len(sys.argv) > 1:
        for message in sys.argv[1:]:
            await ask(session, message)
    else:
        while message := input("\nyou> ").strip():
            await ask(session, message)


asyncio.run(main())
