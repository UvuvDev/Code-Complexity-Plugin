# Code Complexity plugin — agent handoff notes

Context for picking up work on this plugin without re-deriving the history below the hard way.
Written for an agent, not an end user: it assumes you can read the code, and focuses on *why*
things are shaped the way they are, plus a catalog of real bugs that were found and fixed so they
don't get reintroduced.

## What this is

A Binary Ninja UI plugin that computes per-function code complexity metrics and shows them as a
sidebar panel (docked bottom-left, next to Console/Log), with two tabs: "Current Function" (live,
updates on navigation) and "All Functions" (an opt-in whole-binary sweep, sortable/filterable
table). Installed at this directory. There is also a canonical, headless-safe version of the metric
engine in the main repo at `/Users/bradleyfernandez/binaryninja-api/python/complexity.py`, wired up
as `Function.get_complexity()` / `Function.complexity_metrics()`
(`/Users/bradleyfernandez/binaryninja-api/python/function.py`), plus two simpler example scripts
under `/Users/bradleyfernandez/binaryninja-api/python/examples/` (`complexity_report.py`,
`log_function_complexity.py`).

**Keep `complexity.py` in lock-step between the two locations.** The installed plugin's copy uses
absolute imports (`from binaryninja import ...`) instead of relative (`from . import ...`) so it
works as a standalone plugin file outside the `binaryninja` package; that's the only intentional
difference. When you change a metric's algorithm, change both copies together.
`dialog.py`/`sidebar.py` are UI-only and have no canonical-repo counterpart - they only exist here.

## Files

- **`complexity.py`** — the metric engine. Pure Python, no Qt/UI dependency, safe to import
  headlessly. All 11 metrics, `ComplexityCache`, `compute_all_complexity()`, `get_code_complexity()`,
  `list_complexity_metrics()`.
- **`dialog.py`** — the "engine" for the UI: table model/proxy, the whole-binary table widget
  (`ComplexityTableWidget`), all background task classes, on-disk cache, BNDB-embedded storage.
  Imports `binaryninjaui`, so importing it outside a running UI raises `UIPluginInHeadlessError`.
- **`sidebar.py`** — the actual sidebar panel (`ComplexitySidebarWidget`), the two tabs, navigation
  reactivity. Registers the sidebar widget type at import time (`Sidebar.addSidebarWidgetType(...)`
  at module scope) — this is why `__init__.py` only imports it when `core_ui_enabled()`.
- **`__init__.py`** — trivial, just the headless-safety gate above.

## Metrics (as of `code_references` being added)

`cyclomatic`, `instruction_count`, `token_count`, `branch_density`, `halstead`, `nesting_depth`,
`cognitive`, `composite` are **intraprocedural**: they only read the function's own MLIL/HLIL. Cheap,
always safe, always fast (composite score sits on top of the others).

`fan_out`, `code_references`, `transitive` are **interprocedural** — they look beyond the function's
own body, which is where the real gotchas live:

- **`fan_out`**: classifies the function's own outgoing call sites (internal/external/indirect) via
  `_classify_call_sites`. Only reads the function's own MLIL to do this - does **not** touch callee
  functions' analysis data. Cheap, safe everywhere.
- **`code_references`** (new): count of code cross-references *to* this function
  (`bv.get_code_refs(func.start)`), i.e. blast radius / fan-in, the complement of `fan_out`. This is
  a reverse-index lookup against Binary Ninja's own maintained xref database, not a walk - measured
  at ~122µs/function on a real binary. Safe everywhere, including the incremental update path. It
  is returned/stored as an integer and displayed without a `.00` suffix. Older cache rows containing
  JSON floats such as `2.0` remain compatible and display as `2`; recomputation writes integer values.
- **`transitive`**: recursively walks callees up to depth 2. A standalone one-function request may
  still materialize callee IL and supports fine-grained cancellation via `is_cancelled`. Full sweeps
  avoid that cold recursive pattern: `compute_metric_bundle` first warms every function's composite
  and outgoing edges, then transitive walks cached numbers. Live table updates use
  `_transitive_from_graph` and never touch IL outside the edit root.

`_INCREMENTAL_UPDATE_METRICS` in `dialog.py` = `METRICS` minus `transitive`. This remains deliberate:
the background flush computes only IL-backed values on the functions explicitly queued, while the
table updates transitive from its retained graph. A new cross-function metric needs an explicit
dependency/invalidation rule rather than simply being added to that list.

## Caching architecture (two layers)

1. **Local disk cache**: one file per binary under the system temp dir,
   `binaryninja_complexity_cache_v3_{sha256(abspath)[:16]}.jsonl` (`_cache_path`). **JSON Lines**, not
   a single JSON array — see "GIL-blocking JSON parse" below for why. Each record is an `upsert` or
   `delete` keyed by `(platform, architecture, address)` and includes both the metric values and the
   function's classified outgoing call sites. A full sweep writes one canonical upsert per function;
   live changes append only the touched rows/tombstones instead of parsing and rewriting the entire
   cache. Per-record nanosecond revisions make concurrent background appends deterministic. Loading
   folds the journal and requires its complete stable-key set to match the current function set.
2. **BNDB-embedded metadata** (opt-in, off by default — checkbox "Save to .BNDB" next to the Columns
   button): `bv.store_metadata('complexity_report.cache.v2', entries,
   flags=MetadataStoreFlag.MetadataStorePersistent)` — deliberately *without*
   `MetadataStoreMarksAnalysisChanged`, so saving results doesn't spuriously dirty the file. Read via
   `bv.get_metadata(...)`, always attempted as a fallback regardless of the checkbox state (the
   checkbox only gates *writing* a new snapshot). Written/read by `_save_to_bndb`/`_try_load_from_bndb`,
   used as the fallback in `_LoadComplexityTask`/`_TryLoadCacheTask` when the local cache misses.

Both loaders resolve stable keys back to `Function` objects via one bulk dict built in a single pass
over `bv.functions` — **not** one `bv.get_function_at()` call per row (that was ~139ms for 3,242
functions; the dict approach is ~9ms). Including platform and architecture prevents legitimate
same-address functions from collapsing into one row.

Row `values` dicts loaded from either cache source are **not guaranteed to have every current metric
key** — a cache written before a metric existed won't have it. `_ComplexityTableModel.raw_value`/
`data`, the incremental-flush merge, and `apply_function_metrics` all use `values.get(metric, 0.0)`,
not `values[metric]`, for exactly this reason. **Any new code that reads a row's `values` dict must
do the same** or it will `KeyError` on every pre-existing cache the instant you ship a new metric.

## Threading model / concurrency patterns

- `BackgroundTaskThread` subclasses do the real work off the main thread; results come back via
  `execute_on_main_thread(lambda: on_complete(...))`.
- **Every** `BackgroundTaskThread.run()` that can be superseded (a newer navigation, a table
  destroyed, a binary switch) must still call `on_complete` even on cancellation/failure (with a
  `None`/empty sentinel) — see "Stuck 'Running…'/'Updating…' buttons" below for what happens if you
  don't.
- **Qt widgets/models are not thread-safe.** You cannot run `QSortFilterProxyModel`/`QTableView`
  sorting on a background thread, full stop. The pattern used instead (`_SortRowsTask` +
  `_on_sort_indicator_changed` + `_row_sort_key`): compute the new *order* in plain Python off the
  main thread (no Qt objects touched), then apply it via `model.set_rows()` — a cheap reset — on the
  main thread. Same technique is used for the initial table construction (`rows = sorted(...)` in
  `ComplexityTableWidget.__init__`, done *before* wiring up `sortIndicatorChanged`, specifically to
  avoid `QTableView.setSortingEnabled(True)`'s eager synchronous sort).
- **`shiboken6.isValid(self)`** guards every `on_complete` closure that might fire after the owning
  widget was `deleteLater()`'d (e.g. you switched binaries while a background task was still running).
  Confirmed directly (with a real Qt event loop) that this correctly detects a `deleteLater()`-
  destroyed object and reproduces the exact `"libshiboken: Internal C++ object ... already deleted"`
  error it's meant to prevent.
- **`BinaryDataNotification` callbacks (`function_updated`/`symbol_updated`) can run on a
  non-UI thread** — confirmed via the class's own docstring ("the callback context holds a global
  lock"). `_FunctionChangeNotification` therefore only collects plain keys/functions while under
  that lock. Its `NotificationBarrier` callback hands one settled `_FunctionChangeBatch` to the main
  thread. Do not query the model or do analysis work from the notification callbacks themselves.
- The notifier listens only to `DataWritten`, `FunctionLifetime`, `SymbolUpdated`, and
  `NotificationBarrier`. `FunctionUpdated` and `FunctionUpdateRequested` are deliberately not
  subscribed to: Binary Ninja emits both as analysis-lifecycle signals when Linear View merely
  materializes functions during scrolling, so they do not reliably identify edits. Byte writes and
  lifetime changes are the authoritative metric roots; symbol changes only repaint names. Manual
  reanalysis or analysis-setting changes without either authoritative event require Refresh. Do
  **not** reconcile every barrier against
  `bv.functions`: that makes a one-function edit O(total functions) on the UI thread. Lifetime
  notifications carry the exact stable keys to insert/remove; a user-function removal exposing an
  auto function is a same-key update, not a row-set change.

## The cascade bug (important — read this before touching the incremental-update path)

Undefining **one** function with zero code references was observed to balloon into recomputing
several thousand unrelated functions, with the pending count actually *growing* across consecutive
flushes (1243 → 1248 → 1253 → ...) instead of ever settling. There were two coupled causes:

1. Binary Ninja legitimately emits a large downstream `FunctionUpdated` wave after a structural
   edit. The old notifier treated every one as an independent edit root and queued a full
   incremental metric set for it.
2. Incrementally calculating `transitive` touched other functions' MLIL/HLIL for the first time,
   producing still more `FunctionUpdated` events and turning the wave into a feedback loop.

The current fix is dependency-based, not just a larger queue guard:

- Every sweep retains `call_sites` and builds a reverse `_incoming` map. Function identity is
  `(platform, architecture, address)`, never address alone.
- A removal deletes its row/node immediately. Incoming callers have the removed internal calls
  reclassified as external directly from the retained graph; former callees alone refresh
  `code_references`. Only direct callers and their callers can have changed `transitive` values.
  A zero-reference leaf therefore removes one row and performs **zero metric computations**.
- A changed function recomputes only its own invalidated metrics. If its outgoing edge set changes,
  only the symmetric-difference targets refresh `code_references`.
- Incremental `transitive` never reads IL. `_transitive_from_graph` walks cached composite numbers
  and outgoing edges for the changed function's two-hop reverse closure, so it is both exact and
  incapable of generating analysis notifications.
- Analysis-only `FunctionUpdated`/`FunctionUpdateRequested` waves never enter the batching path.
  Automatic IL work rooted in actual writes is capped at eight functions per barrier: exceeding
  that marks the table stale rather than competing with Binary Ninja's own foreground analysis.
- `_FunctionChangeFlushTask` still serializes the genuinely required background work, and the old
  growth-trend circuit breaker remains as a final safety net.

If a new metric depends on other functions, explicitly describe which graph direction and maximum
depth it depends on, then add that dependency to the invalidation closure. Do not put a cross-function
IL walk back into `_FunctionChangeFlushTask`.

## Other real bugs found and fixed this history (don't reintroduce)

- **GIL-blocking JSON parse**: a single `json.load()` over a large JSON array doesn't release the
  GIL until the *entire* document is parsed — measured at 146ms of total UI-thread starvation for a
  22MB/76k-row cache, even though the load ran on a background thread. Fixed by switching to JSON
  Lines (one `json.loads()` call per row, interleaved with an ordinary — GIL-cooperative — Python
  loop). Reduced worst-case stall to ~7.6ms for the same file.
- **`bv.get_function_at(addr)` per row**: resolving cached rows back to `Function` objects one
  address at a time cost ~139ms for 3,242 rows (each call crosses the Python/core boundary). A single
  bulk stable-key dict + O(1) lookups cost ~9ms for the same data. This is now the standard pattern
  in `_try_load_cache`/`_try_load_from_bndb`.
- **Duplicate-address functions**: on some binaries (this session's test firmware had 88 of them),
  two `Function` objects can legitimately share a `.start` address (e.g. more than one
  platform/architecture mapped at the same address). Address-only model indexes, metric caches,
  cache rows, and graph nodes all silently collapsed these. `complexity.function_key(func)` now
  returns `(platform.name, arch.name, start)` and is used consistently across all four layers.
- **Repeated IL traversal**: the old full-row loop walked MLIL separately for instruction count,
  branch density, Halstead, and call classification; walked HLIL separately for nesting and
  cognitive complexity; then `composite` repeated six of those metrics again. The sweep now uses
  `compute_metric_bundle`: one MLIL pass, one HLIL pass, direct composite derivation, and a separate
  cached-graph transitive phase after every function's local values are warm. On `/bin/ls`, 30 real
  functions matched every old non-transitive metric exactly and improved from 0.614s to 0.251s
  (about 2.4x) in the headless validation run. Full values including transitive also matched exactly.
- **Whole-cache rewrite per changed row**: `_update_cache_entries` used to read, parse, and rewrite
  the entire JSONL file after every incremental flush and overlapping daemon writers could lose an
  update. It was removed. `_append_cache_changes` writes only stable-key upserts/tombstones under a
  lock; revisions preserve logical ordering even if worker threads acquire the lock out of order.
- **`QTableView.setSortingEnabled(True)` eager sort**: unconditionally performs a real sort using
  the Python comparator the instant it's enabled — measured at ~7 seconds for a ~76,000-row table,
  entirely on the main thread, with no way to background it (see threading model above). Fixed by
  pre-sorting rows in Python before model construction and wiring the header's sort indicator by
  hand (`setSortIndicatorShown` + `setSortIndicator` *before* connecting `sortIndicatorChanged`, so
  the initial value-set doesn't trigger anything) instead of calling `setSortingEnabled`/
  `sortByColumn` directly. Later extended so a *user click* to sort by a different column also goes
  through the background-sort pattern (`_SortRowsTask`) instead of Qt's synchronous one.
  **Gotcha hit while doing this**: `QHeaderView` already natively toggles its own sort indicator
  (ascending/descending) on a real click whenever `sortIndicatorShown`/`sectionsClickable` are both
  true — a hand-written click handler added to reproduce that toggle fought with Qt's native one and
  is why sorting briefly got stuck going one direction only. Don't add one; just react to whatever
  indicator state Qt lands on.
- **`resizeColumnsToContents()`**: measures every row to find each column's widest content by
  default — 383ms for ~1,300 rows, scaling with row count. `header.setResizeContentsPrecision(100)`
  before calling it caps the sample size and the cost (~8–40ms) regardless of table size, with a
  negligible accuracy tradeoff for the starting width estimate.
- **Stuck "Running…"/"Updating…" buttons/progress text**: multiple instances of a
  `BackgroundTaskThread.run()` returning early on cancellation *without* calling `on_complete` —
  the caller's callback is what resets button/label state, so skipping it left the UI permanently
  showing an in-progress state after a legitimate cancel. Fix pattern: always call `on_complete`,
  with a `None`/empty result on cancellation, and have the callback handle that case explicitly.
  Separately, `_FunctionChangeFlushTask` originally never updated `self.progress` at all during its
  loop — a slow-but-working batch was visually indistinguishable from a genuine hang. Fixed by
  reporting `(i+1)/total: {func.name}` per iteration, matching every other task in this file.
- **Cross-thread Qt access from a notification callback**: `_on_function_changed` used to touch
  `self.model` directly before ever reaching the main thread — raced against the table being
  `deleteLater()`'d on the main thread mid-callback (e.g. switching binaries while a *different*
  binary's analysis was still running and firing notifications). Manifested as
  `"libshiboken: Internal C++ object (_ComplexityTableModel) already deleted"`. Fixed by making the
  notification handler collect plain data until `NotificationBarrier`, then dispatch the batch with
  `execute_on_main_thread(...)`; all model work and `shiboken6.isValid()` checks stay on the UI thread.
- **Thread-storm from unthrottled per-notification tasks**: before coalescing existed, every
  `FunctionUpdated`/`SymbolUpdated` notification spun up its own `_FunctionMetricsTask` thread. A
  burst of them (common right after opening an already-cached large binary, while background
  analysis is still settling) could launch hundreds of simultaneous background recomputes and made
  the whole process look hung. Fixed by coalescing all pending changes into `self._pending_changes`
  and draining them with a single, generation-tracked `_FunctionChangeFlushTask` at a time
  (`_start_change_flush_if_needed`) — see "The cascade bug" above for the further fixes this itself
  needed once it existed.
- **MLIL-first-access race with the UI's own rendering**: the "Current Function" tab used to start
  computing metrics for the newly-navigated-to function *immediately*, which is the same instant
  Binary Ninja's own UI is independently materializing that function's MLIL/HLIL to render the
  disassembly/graph view — two consumers racing to build the same data. Measured at ~437ms cold vs.
  ~202ms warm for one function; the cold cost is largely the redundant first access. Fixed with a
  200ms debounce (`QTimer.singleShot`) in `sidebar.py`'s `_refresh_current_function_tab`, using the
  existing generation counter to drop the deferred call if superseded by a newer navigation before
  it fires. The deferred callback now also waits for the binary's analysis state to return to idle;
  this prevents the plugin from extending the editor's visible `Loading...` interval by requesting
  MLIL/HLIL while the editor is still materializing the newly selected function. If the All
  Functions table already contains the selected function, the Current Function tab reuses that row
  immediately and starts no analysis task at all.
- **Single-row removal performed two whole-table resets**: `remove_functions` rebuilt every row via
  `set_rows()`, then the notification path launched a background sort whose completion reset the
  model again. Even a zero-reference leaf therefore made `QTableView` and its proxy reconstruct a
  large table twice on the UI thread. Removal now uses `beginRemoveRows`/`endRemoveRows` for only the
  affected contiguous ranges, and a deletion-only batch does not re-sort because removing elements
  preserves the existing order. Addition similarly uses `beginInsertRows`/`endInsertRows`. More
  generally, an incremental update only launches the background full-table sort when the active
  sort column is among the values that actually changed.
- **O(N) undefine bookkeeping on the UI thread**: each notification barrier enumerated
  `bv.functions`, copied the complete reverse-call graph, and (for a data write) queried every
  function's address ranges. Lifetime keys are now consumed directly, the old incoming dictionary
  is retained by reference while `_rebuild_incoming` creates its replacement, and data roots use
  `get_functions_containing` at the changed range endpoints.
- **Background-sort lost update**: a sort used a shallow row snapshot; if an incremental update
  landed before it completed, `set_rows(sorted_snapshot)` could restore the old values. Every model
  mutation now invalidates the sort generation, and sort completion removes only its own task rather
  than accidentally clearing a newer sort task.
- **Stale cache from before a fix existed**: a cache written by old, buggy code doesn't get fixed
  retroactively just because the code that would have prevented it now exists on disk. When
  debugging "why does this still look wrong after the fix," check file mtimes against when the fix
  was actually shipped before assuming the fix itself is wrong.

## UI/state persistence conventions

- The Current Function view is the same native horizontal `QTableView` presentation as All
  Functions, backed by the same `_ComplexityTableModel` and `_COLUMNS` schema, but its model contains
  zero or one row. There is no separate identity/header block: Function and Address are ordinary
  columns alongside the selected metrics. This replaced the paired Metric/Value property grid and
  the taller List/Grouped experiments; the All Functions behavior itself was not changed.
- `_configure_complexity_table` owns the shared row-selection behavior, alternating colors,
  monospace font, interactive headers, bounded initial content measurement, and persisted widths.
  The Current Function view also reads the same persisted column-visibility choices as All
  Functions, so both views have the same column vocabulary and defaults without duplicating UI
  styling code.
- The Current Function table is never hidden or replaced. With no function selected it keeps its
  headers and has zero rows. On navigation, it immediately installs the new Function/Address row and
  displays em dashes in metric cells until the background result arrives; this prevents stale values
  and layout flicker simultaneously. Its first real row is measured once so names and numbers are
  not elided merely because the model was empty when the widget was constructed.
- Both function tables use standard Qt item views and no custom painting/style sheets. Do not
  reintroduce custom cards, rounded borders, fixed backgrounds, oversized values, or progress bars:
  they visibly clash with the surrounding Binary Ninja sidebars.
- `notifyOffsetChanged` continues tracking the current function while the All Functions tab is
  selected, but defers Current Function UI/analysis work until that tab becomes active again;
  switching away also cancels an in-flight current-function task. This prevents a hidden dashboard
  from doing work during Linear View navigation.
- Per-panel UI behavior (auto-reload-from-cache, save-to-bndb, column visibility, column width) uses
  `QSettings`, not `binaryninja.settings.Settings` — this isn't a documented, user-facing analysis
  setting, just local UI state for one panel. Keys live in `dialog.py` as module-level constants
  (`AUTO_RELOAD_SETTING_KEY`, `SAVE_TO_BNDB_SETTING_KEY`) even though the checkboxes for them live in
  `ComplexityTableWidget` — `sidebar.py`'s `_try_auto_reload` reads `dialog.AUTO_RELOAD_SETTING_KEY`
  directly rather than through a checkbox object, since the checkbox only exists once a sweep table
  exists, but the setting must be readable even from the intro-page state.
- Column width persistence (`_column_identifier`) keys by metric name, not column index, so it
  survives a future column reorder. Watch out for `sectionResized` firing when a column is *hidden*
  (Qt internally resizes it to 0) — `_on_section_resized` explicitly ignores `new_size <= 0` to avoid
  clobbering a real saved width with 0 every time a column is toggled off.
- The main Current Function / All Functions tabs use `QTabWidget.West`, matching the plugin's
  original left-side placement and preserving vertical space. Do not move them back to the top as
  part of Current Function layout work; their position is independent of the table presentation.
- Both settings checkboxes (auto-reload, save-to-bndb) live inline in `ComplexityTableWidget`'s
  filter row (next to Columns), not as their own row — an earlier version put them above the
  tab's stacked widget (intro page / table), always visible regardless of which was showing, which
  pushed the whole panel tall enough to crowd out the linear/graph view. Tradeoff: they're now only
  visible once a sweep table exists for the current binary, not on the intro page — considered
  acceptable since the settings only mean anything once there's a cache to reload from or a sweep to
  embed.

## Validation of the graph/incremental rewrite

Headless/offscreen tests were run against real Binary Ninja analysis of `/bin/ls`:

- `compute_metric_bundle` matched all ten original non-transitive metric implementations for 30
  functions; the two-phase full sweep also matched `transitive` exactly.
- Cache v3 round-tripped values plus graph data, rejected a tombstoned incomplete function set, and
  loaded again after the row was restored with a later revision.
- Removing a zero-code-reference leaf changed the model from 135 to 134 rows and invoked
  `compute_function_metrics` **zero times** (`pending == 0`, not stale). A separate synthetic
  100,000-row Qt-model check removed the middle row with one `rowsRemoved(50000, 50000)` signal and
  **zero** `modelReset` signals.
- An end-to-end synthetic lifetime batch over a 10,000-row widget removed exactly one middle row in
  about 3 ms, queued no metric work, and launched no sort. The same check with one incoming
  edge updated only that caller's fan-out/transitive neighborhood and still launched no sort while
  the table was ordered by the unchanged Composite column.
- Removing a function with an outgoing internal edge invoked one targeted `code_references` refresh;
  every recomputed key was in the removed function's incident-edge set.
- Removing a function with an incoming caller produced fan-out/transitive values identical to a
  fresh recomputation for the affected reverse neighborhood.
- Analysis-only `FunctionUpdated`/`FunctionUpdateRequested` waves are not subscribed to and enqueue
  no work; a one-byte `DataWritten` range invokes the metric engine only for its containing function.

The UI module still cannot be imported normally in a headless process; those tests injected only a
minimal `binaryninjaui.getMonospaceFont` stub and used a real offscreen `QApplication`. This does not
replace an in-app smoke test after reloading the plugin.

## Known gaps / things not done

- The C++ mirror (`complexity.cpp`, `binaryninjaapi.h`'s `Function::GetComplexity`/
  `GetComplexityMetricNames` declarations, validated earlier via a real BN core build) was **not**
  updated with `code_references`. It's a separate, heavier effort (needs a real BN core build to
  validate against, either `BN_ALLOW_STUBS` or the parallel checkout at
  `/Users/bradleyfernandez/binaryninja/api`) that hasn't been requested since.
- Old-cache rows missing a newer metric display as `0.0`, indistinguishable from a genuinely-zero
  value, until the next Refresh. A visually distinct placeholder (e.g. "–") was floated but not
  implemented.
- No "3-state" sort (ascending → descending → unsorted) — only the 2-state Qt-native toggle. This
  predates all the sorting-performance work in this history; never actually asked for.
- Headless testing methodology used throughout this history, if you need to validate something
  algorithmic without a full BN UI session: the real BN Python package is importable headlessly
  (`import binaryninja` works standalone); `binaryninjaui` raises `UIPluginInHeadlessError`
  immediately on import, so `dialog.py`/`sidebar.py` can't be imported that way — validate Qt-specific
  behavior (sort cost, resize cost, `shiboken6.isValid` semantics, cross-thread marshaling) with a
  standalone PySide6 script instead (a real `QApplication`, no Binary Ninja involved), and validate
  the metric engine itself (`complexity.py`) directly against a real `BinaryView` from a real binary
  on disk (e.g. `/usr/lib/dyld`, `/bin/zsh`) via plain `binaryninja.load(...)`.
