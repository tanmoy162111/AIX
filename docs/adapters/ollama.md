# Ollama adapter

Text-only local models over HTTP (`POST /api/chat`, NDJSON). Verified against a scripted local
server in `tests/adapter/test_ollama.py`; **not yet run against a real Ollama in this repository**.

- **What it can do:** `research`, `review`, `summarize` (and a little `design`). It never edits
  files: the workspace is untouched, so it enforces `read_only`, `write_scope` and `network_deny`
  trivially and is eligible for high-risk read-only tasks.
- **Enable it:** it is built in but not in the default `agents.enabled`; add it with
  `aix agent enable ollama`. `aix agent test ollama --live` sends a tiny prompt.
- **Host:** `OLLAMA_HOST` (`host`, `host:port` or a URL; default `http://127.0.0.1:11434`). Plain
  HTTP to a remote host sends your prompts unencrypted; use `https://` or keep it on localhost.
- **Models:** the first model reported by `/api/tags` is the default; pin one with
  `agents.overrides.ollama.model`. Health is `unavailable` when the server does not answer and
  `degraded` when no model is pulled.
- **Cost:** tokens are reported (`prompt_eval_count`, `eval_count`), cost is 0.0.
- **Failures:** connection errors and 5xx are `network_failure`, an unknown model (404) is
  `agent_failure`, a stalled stream is `timeout`. There are no sessions (`resume` is unsupported).
- **Cancel:** the socket is shut down, so a blocked read returns immediately.
