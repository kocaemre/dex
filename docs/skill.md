---
name: dex
description: Install and use dex, the agent-native analytics engineering toolkit, from its real skills rather than from this documentation site. Use when a user wants to explore a warehouse, author dbt models or semantic layers, or detect drift with dex.
---

# dex

This documentation site describes dex. It is not where dex's skills come from,
and a skill generated from these pages would carry none of the guardrails dex
enforces, because those live in the engine.

Install the real skills instead:

- Claude Code: `/plugin marketplace add exmergo/exmergo-agent-plugins`, then
  `/plugin install dex@exmergo`.
- Any other agent: `npx skills add exmergo/dex`.
- The engine on its own: `pip install "exmergo-dex-core[duckdb]"`, which needs
  `uv` on the PATH.

Then, with no warehouse and no credentials, `dex demo` creates a local DuckDB
warehouse and `dex explore map` maps it.

Documentation: https://docs.exmergo.com. Source: https://github.com/exmergo/dex.
