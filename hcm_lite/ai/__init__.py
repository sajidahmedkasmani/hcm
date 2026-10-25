"""M8 — AI assistant.

Architecture: single-agent **ReAct-style tool-calling loop** (reason -> act via a scoped tool ->
observe -> answer), plus two structured-output pipelines (smart leave extraction and grounded
dashboard summaries) that follow a *validate -> retry once -> deterministic fallback* pattern.
The model never touches the database: tools reuse the same services and RBAC as the REST API.
"""
