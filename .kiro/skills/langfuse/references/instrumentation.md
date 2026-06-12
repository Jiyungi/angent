---
name: langfuse-observability
description: Instrument LLM applications with Langfuse tracing. Use when setting up Langfuse, adding observability to LLM calls, or auditing existing instrumentation.
source: https://github.com/langfuse/skills (skills/langfuse/references/instrumentation.md)
---

# Langfuse Observability

Instrument LLM applications with Langfuse tracing, following best practices.

## Workflow

1. Assess current state: Is the SDK installed? What LLM frameworks are used? Existing instrumentation?
   - No integration yet → set up using a framework integration if available (captures more context, less code).
   - Integration exists → audit against the baseline below.
2. Verify baseline requirements (every trace): model name, token usage, descriptive trace names,
   span hierarchy, correct observation types (generations marked as generations), sensitive data masked,
   meaningful + explicit trace input/output (not all function args).
3. Explore traces in the UI before adding more context.
4. Discover additional context (infer from code; only ask when unclear): session_id, user_id,
   feature tag, customer_tier tag, user-feedback scores.
5. Guide the user to the relevant UI views.

## Framework Integrations (prefer over manual instrumentation)

| Framework  | Integration         | Docs                                          |
| ---------- | ------------------- | --------------------------------------------- |
| OpenAI SDK | Drop-in replacement | https://langfuse.com/integrations/model-providers/openai-py |
| LangChain  | Callback handler    | https://langfuse.com/docs/integrations/langchain |

OpenAI drop-in: `from langfuse.openai import openai` (or `from langfuse.openai import OpenAI`).
Automatically tracks prompts/completions, latencies, API errors, model usage (tokens) and cost.

## Common Mistakes

| Mistake | Fix |
| --- | --- |
| No `flush()` in scripts | Call `langfuse.flush()` before exit |
| Flat traces | Use nested spans for distinct steps |
| Generic trace names | Use descriptive names (`qualify-candidate`, not `trace-1`) |
| Logging sensitive data | Mask PII before tracing |
| Not setting explicit input with `@observe` | Python: `langfuse.update_current_span(input=...)` — set only the relevant input |
| Manual instrumentation when integration exists | Use the framework integration |
| Langfuse import before env vars loaded | Import/init Langfuse AFTER `load_dotenv()` |
| Wrong import order with OpenAI | Import `langfuse.openai` (its wrapped client) instead of the raw OpenAI client |

## v3 Python SDK API (OpenTelemetry-based)

- `from langfuse import get_client, observe`
- `from langfuse.openai import OpenAI`  # wrapped, auto-instrumented; supports base_url/api_key
- `langfuse = get_client()`  # reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL (host)
- `with langfuse.start_as_current_observation(as_type="span", name="...") as span:` ... nested steps
- `langfuse.update_current_span(input=..., output=..., metadata=...)`  # explicit, masked input/output
- `@observe(name="...")` decorator for function-level tracing
- `langfuse.flush()`  # before process exit
- Docs: https://langfuse.com/docs/observability/overview
