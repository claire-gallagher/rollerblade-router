# agents-sdk-web

`gemini-web-tool-calling`, rebuilt with the OpenAI Agents SDK: `@function_tool` replaces the tool JSON, `Runner` replaces `run_agent()`, a `SQLiteSession` file replaces the sessions dict, and a `RunHooks` subclass records the tool calls the page shows. Run `gcloud auth application-default login`, then `uv run app.py`, and open http://localhost:8000.
