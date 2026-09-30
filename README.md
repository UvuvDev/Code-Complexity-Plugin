# Code Complexity

A Binary Ninja plugin that computes and displays code complexity metrics for functions, as a
docked sidebar panel (next to Console/Log at the bottom-left) rather than a popup.

- **Current Function** tab — live-updates with a full metric breakdown as you navigate.
- **All Functions** tab — an opt-in whole-binary sweep: every function ranked, sortable, and
  filterable, with results cached to disk (and optionally embedded in the `.bndb` itself) so
  reopening a binary you've already swept is instant.

## Metrics

Each metric isolates a different factor that contributes to how hard a function is to read, test,
or reason about:

| Metric | Factor measured |
|---|---|
| `cyclomatic` | Independent paths through the control flow graph (classic McCabe metric). |
| `instruction_count` | Raw code length: total MLIL instructions, including sub-expressions. |
| `token_count` | Raw lexical size: total disassembly text tokens. |
| `branch_density` | Fraction of statements that branch. |
| `halstead` | Volume from operator/operand diversity. |
| `nesting_depth` | Deepest if/while/for/switch nesting in the decompiled code. |
| `cognitive` | Nesting-weighted control-flow cost. |
| `composite` | Weighted blend of the metrics above into one score. |
| `fan_out` | How much (and how riskily) a function calls out to other code. |
| `code_references` | How many places in the binary's code reference a function — blast radius, not internal complexity. |
| `transitive` | A function's composite complexity plus everything it calls, recursively. |

`fan_out`, `code_references`, and `transitive` are interprocedural — they look past a function's own
body, which every purely intraprocedural metric misses (a `main()` that's just a flat sequence of
calls to a dozen helpers looks trivial by cyclomatic complexity or nesting alone, even though
understanding it fully means reading everything it calls).

## Installation

Clone or copy this repository into your Binary Ninja plugins folder:

```bash
git clone https://github.com/UvuvDev/Code-Complexity-Plugin.git \
  "$HOME/Library/Application Support/Binary Ninja/plugins/complexity_report"
```

(On Linux: `~/.binaryninja/plugins/`. On Windows: `%APPDATA%\Binary Ninja\plugins\`.)

Restart Binary Ninja, then open the **Code Complexity** panel from the sidebar.
