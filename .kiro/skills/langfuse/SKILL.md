---
name: langfuse
description: Interact with Langfuse and access its documentation. Use when needing to (1) query or modify Langfuse data programmatically via the CLI — traces, prompts, datasets, scores, sessions, and any other API resource, (2) look up Langfuse documentation, concepts, integration guides, or SDK usage, or (3) understand how any Langfuse feature works. This skill covers CLI-based API access (via npx) and multiple documentation retrieval methods.
source: https://github.com/langfuse/skills (skills/langfuse)
allowed-tools:
  - WebFetch(domain:langfuse.com)
  - Bash(curl *langfuse.com/*)
  - Bash(npx langfuse-cli api __schema *)
  - Bash(npx langfuse-cli api * --help *)
  - Bash(npx langfuse-cli api * list *)
  - Bash(npx langfuse-cli api * get *)
---

# Langfuse

This skill helps you use Langfuse effectively across all common workflows: instrumenting applications, migrating prompts, debugging traces, and accessing data programmatically.

## Core Principles

1. **Documentation First**: NEVER implement based on memory. Always fetch current docs before writing code (Langfuse updates frequently).
2. **CLI for Data Access**: Use `langfuse-cli` when querying/modifying Langfuse data.
3. **Best Practices by Use Case**: Check the relevant reference file before implementing.
4. **Use latest Langfuse versions**: Unless told otherwise, always use the latest Langfuse SDKs/APIs.

## Use case specific references

- instrumenting an existing function/application: references/instrumentation.md

## Documentation access

- Index: `curl -s https://langfuse.com/llms.txt`
- Page as markdown: append `.md` to a docs path, e.g. `https://langfuse.com/integrations/model-providers/openai-py.md`
- Search: `curl -s "https://langfuse.com/api/search-docs?query=<url-encoded-query>"`

## CLI

```
npx langfuse-cli api __schema
npx langfuse-cli api <resource> --help
npx langfuse-cli api <resource> <action> --help
```

Credentials (env): `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` (also export `LANGFUSE_HOST=$LANGFUSE_BASE_URL` if a tool needs HOST). Keys are in Langfuse UI → Settings → API Keys.
