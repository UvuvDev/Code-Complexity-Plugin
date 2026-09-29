# Copyright (c) 2015-2026 Vector 35 Inc
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

"""
The engine behind the Code Complexity sidebar panel: the table model/proxy backing the
whole-binary view, the small per-function metric table, and the cache-aware background tasks that
compute or load either. This module imports `binaryninjaui`/PySide6, so it's only ever imported
lazily from inside a UI command handler - importing it in a headless context raises
`binaryninja.UIPluginInHeadlessError`. The actual sidebar widget lives in sidebar.py.
"""

import hashlib
import json
import os
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

import shiboken6
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QSettings, QSortFilterProxyModel, QTimer
from PySide6.QtWidgets import (
	QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel, QMenu, QCheckBox, QPushButton,
	QTableView, QHeaderView,
	QAbstractItemView
)
from binaryninjaui import getMonospaceFont

import binaryninja
from binaryninja.binaryview import BinaryDataNotification, BinaryView, NotificationType
from binaryninja.enums import AnalysisState, MetadataStoreFlag
from binaryninja.function import Function
from binaryninja.plugin import BackgroundTaskThread
from binaryninja.mainthread import execute_on_main_thread

from . import complexity

METRICS = complexity.list_complexity_metrics()
FunctionKey = complexity.FunctionKey
Row = Tuple[Function, Dict[str, float]]


@dataclass
class SweepData:
	rows: List[Row]
	call_sites: Dict[FunctionKey, complexity._CallSites]


def _key_to_json(key: FunctionKey) -> List[Union[str, int]]:
	return [key[0], key[1], key[2]]


def _key_from_json(value) -> FunctionKey:
	if not isinstance(value, list) or len(value) != 3:
		raise ValueError('invalid function key')
	return str(value[0]), str(value[1]), int(value[2])


def _call_sites_to_json(calls: complexity._CallSites) -> Dict[str, object]:
	return {
		'internal': [_key_to_json(key) for key in calls.internal_callee_keys],
		'external': calls.external_count,
		'indirect': calls.indirect_count,
	}


def _call_sites_from_json(value) -> complexity._CallSites:
	return complexity._CallSites(
		[_key_from_json(key) for key in value.get('internal', [])],
		int(value.get('external', 0)),
		int(value.get('indirect', 0)),
	)

# Used by _FunctionChangeFlushTask instead of METRICS - see compute_function_metrics's docs on why
# `transitive` specifically is excluded from the incremental per-function-change update path.
_INCREMENTAL_UPDATE_METRICS = [m for m in METRICS if m != 'transitive']

# Used by _start_change_flush_if_needed's growth check below: a queue only counts as "not settling"
# once it's already at least this large - small, ordinary fluctuations (a handful of renames arriving
# in slightly different batches) shouldn't trip a check meant to catch runaway growth.
_RUNAWAY_CHANGE_QUEUE_MIN_SIZE = 50

# A notification batch with more roots than this is almost certainly Binary Ninja propagating one
# edit through analysis, not the user independently editing that many functions. Materializing
# MLIL/HLIL for all of them in the plugin competes with the main UI's own analysis and is much worse
# than leaving the table visibly stale until Refresh is clicked.
_MAX_AUTOMATIC_IL_ROOTS = 8

METRIC_LABELS = {
	'cyclomatic': 'Cyclomatic',
	'instruction_count': 'Instructions',
	'token_count': 'Tokens',
	'branch_density': 'Branch Density',
	'halstead': 'Halstead',
	'nesting_depth': 'Nesting',
	'cognitive': 'Cognitive',
	'composite': 'Composite',
	'fan_out': 'Fan-Out',
	'code_references': 'Code Refs',
	'transitive': 'Transitive',
}

# Shown as a tooltip on each metric's column header (whole-binary table) or row label (single
# function view) - re-derived here rather than pulled from complexity.py's docstrings so they stay
# short enough to read as a tooltip.
METRIC_DESCRIPTIONS = {
	'cyclomatic': 'Independent paths through the control flow graph (classic McCabe metric).',
	'instruction_count': 'Raw code length: total MLIL instructions, including sub-expressions.',
	'token_count': 'Raw lexical size: total disassembly text tokens (mnemonics, operands, punctuation).',
	'branch_density': 'Fraction of statements that branch - how "branchy" the code is, independent of its size.',
	'halstead': 'Volume from operator/operand diversity - how varied the instruction vocabulary is.',
	'nesting_depth': 'Deepest if/while/for/switch nesting in the decompiled code.',
	'cognitive': 'Nesting-weighted control-flow cost - branches nested deeper cost more than flat ones.',
	'composite': 'Weighted blend of the metrics above into one score.',
	'fan_out': 'How much (and how riskily) this function calls out to other code.',
	'code_references': 'How many places in the code call/reference this function - blast radius if you change it.',
	'transitive': "This function's composite complexity plus everything it calls, recursively - "
	              "\"how much do I actually have to read\".",
}

def _cache_path(bv: BinaryView) -> str:
	"""
	A scratch file under the system temp directory, named deterministically from the binary's file
	path - not tied to the running Python `BinaryView` object, so it survives closing and
	reopening the dialog (or even Binary Ninja itself) without holding anything in memory in the
	meantime. It's a plain temp file, not part of the .bndb: closing the dialog frees the
	in-memory rows same as before, and this is only ever consulted again if you reopen the report
	for the same file.

	Stored as JSON Lines (see `_try_load_cache`/`_save_cache`), not a single JSON document - hence
	the `_v3`/`.jsonl` naming, which also means an old-format cache from before this change is
	simply never found (and gets rewritten in the new format on the next scan) rather than
	misparsed as a line-oriented file it isn't.
	"""
	key = hashlib.sha256(os.path.abspath(bv.file.filename).encode('utf-8')).hexdigest()[:16]
	return os.path.join(tempfile.gettempdir(), f'binaryninja_complexity_cache_v3_{key}.jsonl')


_CACHE_IO_LOCK = threading.Lock()


def _try_load_cache(
	bv: BinaryView, task: Optional['BackgroundTaskThread'] = None
) -> Optional[SweepData]:
	"""
	Returns cached rows for `bv`, or None if there's no usable cache (missing, unreadable, or
	older than the binary itself, e.g. because it was reanalyzed since) - or if `task` is given and
	gets cancelled partway through.

	Resolving each cached stable key back to a live `Function` object is done through one dict built
	from a single pass over `bv.functions`, not one core lookup per cached row:
	each of those is its own call across the Python/core boundary, and measured back to back over
	tens of thousands of rows that adds up to seconds, not milliseconds - the dominant cost of
	loading a large cache once the JSON parsing itself was fixed to stop blocking the GIL (see
	below). One bulk key-to-function map plus a plain Python dict lookup per row is tens of times
	faster and, being pure Python, cooperates with the GIL throughout instead
	of taking one long stretch per lookup. Still runs off the main thread regardless (pass the
	calling `BackgroundTaskThread` as `task` to report progress and allow cancellation, same as a
	fresh scan) since building that dict is still real work on a binary with tens of thousands of
	functions.

	Callers should only call this once analysis has settled (see `_wait_for_analysis`) - not
	because this function does anything wrong otherwise, but because its own validity check below
	compares against the binary's *current* function count, which only means something once that
	count has actually stopped changing.

	Parses the cache one line at a time rather than with a single `json.load` over the whole file,
	even though this runs on a background thread: `json.load`'s C parser doesn't release the GIL
	until it finishes the *entire* document, so for a multi-megabyte cache (tens of thousands of
	functions) that one call can itself take on the order of a hundred milliseconds during which
	nothing else in the process - including the UI thread trying to paint whatever triggered this
	load, e.g. switching to a different binary's tab - can run at all. Many small `json.loads` calls
	interleaved with an ordinary Python loop (which *does* cooperate with the GIL) fixes that
	without giving up the C-accelerated parser.
	"""
	path = _cache_path(bv)
	try:
		if os.path.getmtime(path) < os.path.getmtime(bv.file.filename):
			return None
		with _CACHE_IO_LOCK:
			with open(path, 'r') as f:
				lines = f.readlines()
	except OSError:
		return None

	if task is not None and task.cancelled:
		return None
	all_functions = list(bv.functions)
	key_to_func = {complexity.function_key(func): func for func in all_functions}

	entries: Dict[FunctionKey, Tuple[Dict[str, float], complexity._CallSites]] = {}
	revisions: Dict[FunctionKey, int] = {}
	total = len(lines)
	for i, line in enumerate(lines):
		if task is not None:
			if task.cancelled:
				return None
			if i % 1000 == 0:
				task.progress = f'Loading cached complexity data... ({i}/{total})'
		line = line.strip()
		if not line:
			continue
		try:
			entry = json.loads(line)
			key = _key_from_json(entry['key'])
			revision = int(entry.get('revision', 0))
			if revision < revisions.get(key, -1):
				continue
			revisions[key] = revision
			if entry.get('op', 'upsert') == 'delete':
				entries.pop(key, None)
				continue
			entries[key] = (entry['values'], _call_sites_from_json(entry.get('calls', {})))
		except (KeyError, TypeError, ValueError):
			return None  # not a cache in this format

	if set(entries) != set(key_to_func):
		return None

	rows = [(func, entries[key][0]) for key, func in key_to_func.items()]
	call_sites = {key: entries[key][1] for key in key_to_func}
	return SweepData(rows, call_sites)


def _save_cache(bv: BinaryView, data: SweepData, revision: Optional[int] = None) -> None:
	"""Writes one JSON object per line - see `_try_load_cache` for why this isn't a single `json.dump`."""
	path = _cache_path(bv)
	revision = time.time_ns() if revision is None else revision
	try:
		with _CACHE_IO_LOCK:
			with open(path, 'w') as f:
				for func, values in data.rows:
					key = complexity.function_key(func)
					entry = {
						'op': 'upsert', 'key': _key_to_json(key), 'revision': revision, 'values': values,
						'calls': _call_sites_to_json(data.call_sites.get(key, complexity._CallSites([], 0, 0))),
					}
					f.write(json.dumps(entry))
					f.write('\n')
	except OSError as e:
		binaryninja.log_warn(f'complexity_report: could not write cache to {path}: {e}')


def _append_cache_changes(
	bv: BinaryView,
	updates: Dict[FunctionKey, Tuple[Dict[str, float], complexity._CallSites]],
	deletes: Set[FunctionKey],
	revision: int,
) -> None:
	"""Append incremental upserts/tombstones without reparsing and rewriting the whole cache."""
	if not updates and not deletes:
		return
	path = _cache_path(bv)
	try:
		with _CACHE_IO_LOCK:
			with open(path, 'a') as f:
				for key in deletes:
					f.write(json.dumps({'op': 'delete', 'key': _key_to_json(key), 'revision': revision}))
					f.write('\n')
				for key, (values, calls) in updates.items():
					f.write(json.dumps({
						'op': 'upsert', 'key': _key_to_json(key), 'revision': revision, 'values': values,
						'calls': _call_sites_to_json(calls),
					}))
					f.write('\n')
	except OSError as e:
		binaryninja.log_warn(f'complexity_report: could not append cache changes at {path}: {e}')


# Persisted the same way as the Columns dropdown's choices (QSettings, not
# binaryninja.settings.Settings - local UI-behavior state for one panel, not a documented,
# user-facing analysis setting). Read by ComplexitySidebarWidget._try_auto_reload (sidebar.py) and
# written by the checkbox next to ComplexityTableWidget's Columns button, below.
AUTO_RELOAD_SETTING_KEY = 'complexity_report/auto_reload_from_cache'

# Same persistence mechanism as AUTO_RELOAD_SETTING_KEY above. Off by default: unlike the disk cache
# in `_cache_path`, this makes the .bndb itself larger and slower to save on a big binary, which
# isn't a cost every user wants paid automatically just for having this panel open.
SAVE_TO_BNDB_SETTING_KEY = 'complexity_report/save_to_bndb'

# Bumped independently of the on-disk cache's own `_v3` (they're different storage locations with no
# reason to share a version number), but for the same reason: lets a future format change make an
# old embedded snapshot simply not be found, rather than misinterpreted.
_BNDB_METADATA_KEY = 'complexity_report.cache.v2'


def _save_to_bndb(bv: BinaryView, data: SweepData) -> None:
	"""
	Embeds a full sweep's results directly in the .bndb itself, via `BinaryView.store_metadata` -
	so they survive being shared to another machine, or the local disk cache in `_cache_path` being
	lost (a cleared temp directory, a fresh machine) - without needing to redo a sweep that can take
	a long time on a large binary. Only called when the "Also save results in the .bndb" setting
	(see `SAVE_TO_BNDB_SETTING_KEY`) is on - see its comment for why this isn't automatic.

	Uses `MetadataStorePersistent` without `MetadataStoreMarksAnalysisChanged`: per
	`BinaryView.store_metadata`'s own documentation, that combination is specifically for caching
	re-derivable data like this - it's written into the next save of the .bndb, but doesn't
	spuriously mark the file as having unsaved analysis changes just because this was computed.
	"""
	entries = []
	for func, values in data.rows:
		key = complexity.function_key(func)
		entries.append({
			'key': _key_to_json(key),
			'values': values,
			'calls': _call_sites_to_json(data.call_sites.get(key, complexity._CallSites([], 0, 0))),
		})
	try:
		bv.store_metadata(_BNDB_METADATA_KEY, entries, flags=MetadataStoreFlag.MetadataStorePersistent)
	except Exception as e:
		binaryninja.log_warn(f'complexity_report: could not save results to the .bndb: {e}')


def _try_load_from_bndb(
	bv: BinaryView, task: Optional['BackgroundTaskThread'] = None
) -> Optional[SweepData]:
	"""
	Mirrors `_try_load_cache`, but reads a previous sweep's results back from the .bndb's own
	embedded metadata (see `_save_to_bndb`) instead of the local disk cache - the fallback for when
	the local cache is missing but a previous sweep was saved into this specific .bndb (e.g. this is
	a fresh machine, or the local cache was cleared). Always attempted regardless of whether the
	"Also save results in the .bndb" setting is currently on - that setting only controls writing a
	new snapshot going forward, not whether an existing one already embedded gets read back.
	"""
	entries = bv.get_metadata(_BNDB_METADATA_KEY)
	if not isinstance(entries, list):
		return None  # nothing embedded, or not in a format this version recognizes

	# Stable keys include platform/architecture, so duplicate start addresses remain distinct.
	all_functions = list(bv.functions)
	key_to_func = {complexity.function_key(func): func for func in all_functions}

	rows = []
	call_sites = {}
	total = len(entries)
	for i, entry in enumerate(entries):
		if task is not None:
			if task.cancelled:
				return None
			if i % 1000 == 0:
				task.progress = f'Loading results embedded in the .bndb... ({i}/{total})'
		try:
			key = _key_from_json(entry['key'])
			func = key_to_func.get(key)
			values = entry['values']
			calls = _call_sites_from_json(entry.get('calls', {}))
		except (KeyError, TypeError, ValueError):
			return None  # not a shape this version recognizes
		if func is not None:
			rows.append((func, values))
			call_sites[key] = calls

	# Same staleness check as _try_load_cache: the complete stable-key set must still match.
	if len(rows) != len(all_functions) or set(call_sites) != set(key_to_func):
		return None

	return SweepData(rows, call_sites)


def _format_metric_value(metric: str, value: float) -> str:
	"""Format inherently discrete counts without a misleading fractional suffix."""
	if metric == 'code_references':
		return str(int(value))
	return f'{value:.2f}'


_COLUMNS = ['Function', 'Address'] + [METRIC_LABELS[m] for m in METRICS]
_ADDRESS_COL = 1
_FIRST_METRIC_COL = 2
# Private marker used only by the one-row Current Function table while its metrics are in flight.
# A real result dictionary can never contain this object key, including one loaded from JSON.
_PENDING_VALUES_KEY = object()


def _column_identifier(col: int) -> str:
	"""A stable, restart-safe key for column `col`, for persisting its width/visibility in
	QSettings - stable because it's the metric's own name (or 'function'/'address'), not the column
	index, which would silently shift meaning if a column were ever added, removed, or reordered."""
	if col == 0:
		return 'function'
	if col == _ADDRESS_COL:
		return 'address'
	return METRICS[col - _FIRST_METRIC_COL]

# Shown by default in the whole-binary table; everything else (plus Address) is available from the
# Columns dropdown but starts hidden, so a first look at a large binary isn't an overwhelming wall
# of numbers. `Function` itself is never in this set - it's not optional, so it's not in the
# dropdown at all.
DEFAULT_VISIBLE_METRICS = {
	'instruction_count', 'token_count', 'nesting_depth', 'cognitive', 'composite', 'fan_out', 'code_references'
}


class _ComplexityTableModel(QAbstractTableModel):
	"""
	Holds one row per function with no per-cell objects: a QTableWidget with tens of thousands of
	rows allocates a real QTableWidgetItem (plus its Python wrapper) for every cell - for a
	76,000-function binary with 11 columns that's the better part of a million heavyweight
	objects, which is exactly what drives multi-gigabyte memory use on a large firmware image.
	A model just answers `data()` for whatever the view is currently painting, so the only memory
	this actually holds is `rows` itself - the same list the background scan already produced.
	"""

	def __init__(self, rows: List[Tuple[Function, Dict[str, float]]], parent=None):
		super().__init__(parent)
		self._rows = rows
		self._row_by_key = {complexity.function_key(func): i for i, (func, _) in enumerate(rows)}

	def set_rows(self, rows: List[Tuple[Function, Dict[str, float]]]) -> None:
		self.beginResetModel()
		self._rows = rows
		self._row_by_key = {complexity.function_key(func): i for i, (func, _) in enumerate(rows)}
		self.endResetModel()

	def rows_snapshot(self) -> List[Tuple[Function, Dict[str, float]]]:
		"""
		A shallow copy of the current rows - safe to hand to a background thread (see
		`_SortRowsTask`), independent of whatever happens to `self._rows` afterward (a rename's
		`update_row`, another `set_rows`, ...) while that thread is still running.
		"""
		return list(self._rows)

	def function_keys(self) -> Set[FunctionKey]:
		return set(self._row_by_key)

	def function_for(self, key: FunctionKey) -> Optional[Function]:
		row = self._row_by_key.get(key)
		return None if row is None else self._rows[row][0]

	def has_function(self, key: FunctionKey) -> bool:
		return key in self._row_by_key

	def values_for(self, key: FunctionKey) -> Optional[Dict[str, float]]:
		"""The current metric values for `key`, or None if it is not part of this table."""
		row = self._row_by_key.get(key)
		return None if row is None else self._rows[row][1]

	def update_row(self, key: FunctionKey, values: Dict[str, float]) -> bool:
		"""
		Replaces one function's values in place - no reload, no full-table reset - and returns
		whether that function was actually part of this table. Used when a function's name or
		type changes: neither affects any complexity metric, so there's no reason a rename should
		force recomputing (or even just redisplaying) every other function too.
		"""
		row = self._row_by_key.get(key)
		if row is None:
			return False
		func, _ = self._rows[row]
		self._rows[row] = (func, values)
		self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
		return True

	def remove_functions(self, keys: Set[FunctionKey]) -> None:
		if not keys:
			return
		# Removing one function used to rebuild the complete row list under beginResetModel(). Besides
		# the O(N) Python work, a reset makes QTableView/QSortFilterProxyModel discard and reconstruct
		# all of their internal state. An undefine should be a one-row operation, so remove contiguous
		# groups in place and rebuild only the lightweight key-to-index dictionary afterward.
		rows = sorted((self._row_by_key[key] for key in keys if key in self._row_by_key), reverse=True)
		groups = []
		for row in rows:
			if groups and row == groups[-1][0] - 1:
				groups[-1] = (row, groups[-1][1])
			else:
				groups.append((row, row))
		for first, last in groups:
			self.beginRemoveRows(QModelIndex(), first, last)
			del self._rows[first:last + 1]
			self.endRemoveRows()
		self._row_by_key = {complexity.function_key(func): i for i, (func, _) in enumerate(self._rows)}

	def add_function(self, func: Function, values: Dict[str, float]) -> None:
		key = complexity.function_key(func)
		if key in self._row_by_key:
			self.update_row(key, values)
			return
		row = len(self._rows)
		self.beginInsertRows(QModelIndex(), row, row)
		self._rows.append((func, values))
		self._row_by_key[key] = row
		self.endInsertRows()

	def touch_names(self, keys: Set[FunctionKey]) -> None:
		for key in keys:
			row = self._row_by_key.get(key)
			if row is not None:
				self.dataChanged.emit(self.index(row, 0), self.index(row, 0))

	def rowCount(self, parent=QModelIndex()) -> int:
		return 0 if parent.isValid() else len(self._rows)

	def columnCount(self, parent=QModelIndex()) -> int:
		return 0 if parent.isValid() else len(_COLUMNS)

	def headerData(self, section, orientation, role=Qt.DisplayRole):
		if orientation != Qt.Horizontal:
			return None
		if role == Qt.DisplayRole:
			return _COLUMNS[section]
		if role == Qt.ToolTipRole and section >= _FIRST_METRIC_COL:
			return METRIC_DESCRIPTIONS[METRICS[section - _FIRST_METRIC_COL]]
		return None

	def function_at(self, row: int) -> Function:
		return self._rows[row][0]

	def raw_value(self, index) -> Union[str, int, float]:
		"""The underlying value for a cell, for sorting - as opposed to `data()`'s display string."""
		func, values = self._rows[index.row()]
		col = index.column()
		if col == 0:
			return func.name
		if col == _ADDRESS_COL:
			return func.start
		# .get(..., 0.0), not values[...]: a row loaded from a cache (disk or embedded in the .bndb)
		# written before a metric existed simply won't have that key - see METRICS, which is whatever
		# this version of the plugin currently knows about, not whatever was around when the cache was
		# written. Without this, adding a new metric made every pre-existing cache raise a KeyError
		# the moment this table tried to display or measure that column, which looked like the cache
		# failing to load at all rather than what it actually was: one column showing a placeholder
		# until the next Refresh recomputes it for real.
		return values.get(METRICS[col - _FIRST_METRIC_COL], 0.0)

	def data(self, index, role=Qt.DisplayRole):
		if not index.isValid():
			return None
		func, values = self._rows[index.row()]
		col = index.column()

		if role == Qt.DisplayRole:
			if col == 0:
				return func.name
			if col == _ADDRESS_COL:
				return f'{func.start:#x}'
			if values.get(_PENDING_VALUES_KEY, False):
				return '—'
			metric = METRICS[col - _FIRST_METRIC_COL]
			return _format_metric_value(metric, values.get(metric, 0.0))
		if role == Qt.TextAlignmentRole and col != 0:
			return Qt.AlignRight | Qt.AlignVCenter
		if role == Qt.ToolTipRole and col >= _FIRST_METRIC_COL:
			return METRIC_DESCRIPTIONS[METRICS[col - _FIRST_METRIC_COL]]
		return None


class _ComplexitySortFilterProxy(QSortFilterProxyModel):
	"""Filters by function name (column 0) and sorts every column by its raw numeric/string value."""

	def __init__(self, parent=None):
		super().__init__(parent)
		self.setFilterCaseSensitivity(Qt.CaseInsensitive)
		self.setFilterKeyColumn(0)

	def lessThan(self, left, right) -> bool:
		return self.sourceModel().raw_value(left) < self.sourceModel().raw_value(right)


def _configure_complexity_table(table: QTableView, model: QAbstractTableModel) -> QHeaderView:
	"""Apply the shared native table presentation used by both function views."""
	table.setModel(model)
	table.setSelectionBehavior(QAbstractItemView.SelectRows)
	table.setSelectionMode(QAbstractItemView.SingleSelection)
	table.setEditTriggers(QAbstractItemView.NoEditTriggers)
	table.verticalHeader().setVisible(False)
	table.setAlternatingRowColors(True)
	table.setFont(getMonospaceFont(table))

	# Interactive (not Stretch/ResizeToContents) on every column, so you can drag to resize.
	# ResizeToContents continuously re-measures every row after model changes; do one bounded
	# initial measurement instead and then leave the widths under the user's control.
	header = table.horizontalHeader()
	for col in range(model.columnCount()):
		header.setSectionResizeMode(col, QHeaderView.Interactive)
	header.setResizeContentsPrecision(100)
	table.resizeColumnsToContents()

	for col in range(model.columnCount()):
		saved_width = QSettings().value(
			f'complexity_report/column_width/{_column_identifier(col)}', -1, type=int
		)
		if saved_width > 0:
			header.resizeSection(col, saved_width)

	def _on_section_resized(col: int, old_size: int, new_size: int) -> None:
		if new_size <= 0:
			return
		QSettings().setValue(f'complexity_report/column_width/{_column_identifier(col)}', new_size)

	header.sectionResized.connect(_on_section_resized)
	return header


def _apply_saved_column_visibility(table: QTableView) -> None:
	"""Mirror the All Functions Columns choices in another complexity table."""
	address_visible = QSettings().value('complexity_report/column_visible/address', True, type=bool)
	table.setColumnHidden(_ADDRESS_COL, not address_visible)
	for i, metric in enumerate(METRICS):
		visible = QSettings().value(
			f'complexity_report/column_visible/{metric}', metric in DEFAULT_VISIBLE_METRICS, type=bool
		)
		table.setColumnHidden(_FIRST_METRIC_COL + i, not visible)


def _row_sort_key(col: int) -> Callable[[Tuple[Function, Dict[str, float]]], Union[str, int, float]]:
	"""
	A plain-Python key function for column `col`, matching `_ComplexityTableModel.raw_value` but
	operating directly on a `(Function, Dict[str, float])` row tuple rather than a `QModelIndex` into
	a live model - usable from `_SortRowsTask`, entirely off the main thread.
	"""
	if col == 0:
		return lambda row: row[0].name
	if col == _ADDRESS_COL:
		return lambda row: row[0].start
	metric = METRICS[col - _FIRST_METRIC_COL]
	return lambda row: row[1].get(metric, 0.0)


@dataclass
class _FunctionChangeBatch:
	added: Dict[FunctionKey, Function]
	removed: Set[FunctionKey]
	symbol_addresses: Set[int]
	data_ranges: List[Tuple[int, int]]


class _FunctionChangeNotification(BinaryDataNotification):
	"""
	Batches authoritative complexity-changing events: byte writes, function lifetime changes, and
	function-symbol changes. Deliberately does not subscribe to FunctionUpdated or
	FunctionUpdateRequested: Binary Ninja emits those analysis-lifecycle signals while Linear View
	materializes functions during ordinary scrolling, so they cannot distinguish edits from reads.
	"""

	def __init__(self, on_batch):
		super().__init__(
			NotificationType.NotificationBarrier | NotificationType.DataWritten | NotificationType.FunctionLifetime |
			NotificationType.SymbolUpdated
		)
		self.on_batch = on_batch
		self.added: Dict[FunctionKey, Function] = {}
		self.removed: Set[FunctionKey] = set()
		self.symbol_addresses: Set[int] = set()
		self.data_ranges: List[Tuple[int, int]] = []

	def data_written(self, view: BinaryView, offset: int, length: int) -> None:
		self.data_ranges.append((offset, length))

	def function_added(self, view: BinaryView, func: Function) -> None:
		self.added[complexity.function_key(func)] = func

	def function_removed(self, view: BinaryView, func: Function) -> None:
		self.removed.add(complexity.function_key(func))

	def symbol_updated(self, view: BinaryView, sym) -> None:
		self.symbol_addresses.add(sym.address)

	def notification_barrier(self, view: BinaryView) -> int:
		if not (
			self.added or self.removed or self.symbol_addresses or self.data_ranges
		):
			return 0
		batch = _FunctionChangeBatch(
			self.added, self.removed, self.symbol_addresses, self.data_ranges
		)
		self.added, self.removed = {}, set()
		self.symbol_addresses = set()
		self.data_ranges = []
		execute_on_main_thread(lambda: self.on_batch(batch))
		return 0


class ComplexityTableWidget(QWidget):
	"""
	Sortable, filterable list of every function in a binary, ranked by code complexity. A plain
	embeddable widget (not a QDialog) so it can be docked - e.g. as a tab of the sidebar panel -
	rather than only shown as a popup.
	"""

	def __init__(self, bv: BinaryView, data: SweepData, from_cache: bool, parent=None):
		super().__init__(parent)
		self.bv = bv
		self._call_sites = dict(data.call_sites)
		rows = data.rows

		# Sorted here, in plain Python, rather than left in whatever order `rows` arrived in and
		# handed to Qt's sort machinery below - see the header-wiring block for why: a native
		# `sorted()` call over even tens of thousands of rows costs tens of milliseconds, and doing
		# it here is what lets construction skip an expensive Qt-level sort entirely for the common
		# case (the default view, composite descending) instead of merely making that Qt-level sort
		# faster by handing it already-ordered data.
		rows = sorted(rows, key=lambda row: row[1].get('composite', 0.0), reverse=True)

		layout = QVBoxLayout(self)

		filter_row = QHBoxLayout()
		self.filter_edit = QLineEdit()
		self.filter_edit.setPlaceholderText('Filter by function name…')
		self.filter_edit.textChanged.connect(self._on_filter_changed)
		filter_row.addWidget(self.filter_edit)
		self.refresh_button = QPushButton('Refresh')
		self.refresh_button.setToolTip('Recompute from scratch (e.g. if the binary changed since this was cached).')
		self.refresh_button.clicked.connect(self._refresh)
		filter_row.addWidget(self.refresh_button)
		self.columns_button = QPushButton('Columns')
		filter_row.addWidget(self.columns_button)

		# Lives here, next to Columns, rather than as its own row above this table (or above the
		# intro page shown before any sweep has run) - a settings row that's always present
		# regardless of which one is showing was pushing this whole panel tall enough to crowd out
		# the linear/graph view next to it. This does mean it's only visible once a sweep table
		# exists for the current binary, not on the intro page - reasonable, since the setting only
		# means anything once there's a cache to reload from in the first place.
		self.auto_reload_checkbox = QCheckBox('Auto-reload from cache')
		self.auto_reload_checkbox.setToolTip(
			'When switching to a different binary that has already been swept, silently show its '
			'cached results instead of the Run Sweep button. When off, switching to any binary '
			"always shows the Run Sweep button, even if it's already been swept before."
		)
		self.auto_reload_checkbox.setChecked(QSettings().value(AUTO_RELOAD_SETTING_KEY, True, type=bool))
		self.auto_reload_checkbox.toggled.connect(
			lambda checked: QSettings().setValue(AUTO_RELOAD_SETTING_KEY, checked)
		)
		filter_row.addWidget(self.auto_reload_checkbox)

		# Off by default: see SAVE_TO_BNDB_SETTING_KEY/_save_to_bndb for what this controls and why
		# it isn't automatic. Placed next to auto-reload for the same space reason, not because the
		# two are related.
		self.save_to_bndb_checkbox = QCheckBox('Save to .BNDB')
		self.save_to_bndb_checkbox.setToolTip(
			"Embeds sweep results directly in this binary's .bndb file, so they survive being shared "
			"to another machine or the local cache being lost - at the cost of a larger, slower-to-"
			'save .bndb. Only affects sweeps computed from now on, and only takes effect on the next '
			'save.'
		)
		self.save_to_bndb_checkbox.setChecked(QSettings().value(SAVE_TO_BNDB_SETTING_KEY, False, type=bool))
		self.save_to_bndb_checkbox.toggled.connect(
			lambda checked: QSettings().setValue(SAVE_TO_BNDB_SETTING_KEY, checked)
		)
		filter_row.addWidget(self.save_to_bndb_checkbox)

		self.count_label = QLabel()
		filter_row.addWidget(self.count_label)
		layout.addLayout(filter_row)

		self.model = _ComplexityTableModel(rows, parent=self)
		self.proxy = _ComplexitySortFilterProxy(parent=self)
		self.proxy.setSourceModel(self.model)

		self.table = QTableView()
		header = _configure_complexity_table(self.table, self.proxy)

		self._build_columns_menu()

		self.table.doubleClicked.connect(self._navigate_to_selected)
		layout.addWidget(self.table)

		# Sorting is wired up by hand here instead of QTableView.setSortingEnabled(True) /
		# header.sortIndicatorChanged.connect(self.table.sortByColumn): either of those runs Qt's own
		# sort synchronously on the main thread the moment the indicator changes, calling back into a
		# Python comparator for every pairwise comparison - measured at over 7 seconds for a
		# ~76,000-row table, and that comparator-based sort cannot be moved to a background thread at
		# all (QSortFilterProxyModel/QTableView are not thread-safe). `_on_sort_indicator_changed`
		# below instead computes the new order in plain Python off the main thread (see
		# `_SortRowsTask`) and applies it with a single cheap model reset - the same technique already
		# used to make constructing this table itself fast (see `rows = sorted(...)` above) - so a
		# click to sort by a different column no longer blocks the UI for that whole duration.
		#
		# QHeaderView already toggles its own sort indicator (ascending/descending) on a real click by
		# itself whenever sortIndicatorShown and sectionsClickable are both true - this is native Qt
		# behavior, verified directly, and needs no code of ours to drive it. An earlier version of
		# this also connected a hand-written click handler to reproduce that toggle, which instead
		# fought with Qt's own native toggle on every click and is why sorting got stuck only ever
		# going one direction - removed; all that's actually needed is reacting to whatever indicator
		# state Qt itself ends up in.
		self._sort_generation = 0
		self._sort_task: Optional[BackgroundTaskThread] = None
		header.setSortIndicatorShown(True)
		header.setSortIndicator(_FIRST_METRIC_COL + METRICS.index('composite'), Qt.DescendingOrder)
		header.sortIndicatorChanged.connect(self._on_sort_indicator_changed)
		header.setSectionsClickable(True)

		self._from_cache = from_cache
		# Set once a batch is dropped for being implausibly large (see _start_change_flush_if_needed)
		# - reflected in the count label so it's visible without needing to have been watching the
		# progress bar when it happened. Initialized before the first _update_count_label() call
		# below, which reads it.
		self._stale = False
		self._update_count_label()

		# Keep this table in sync with renames/type changes without a full rescan: see
		# _FunctionChangeNotification/_on_function_changed and _FunctionChangeFlushTask. Tracks every
		# background task this table owns so they can all be cancelled if it's destroyed (e.g. you
		# switched to a different binary) before they finish, rather than left to complete and post a
		# result into a table that's no longer there. Also holds the Refresh button's own task (see
		# _refresh) for the same reason.
		self._change_tasks: Set[BackgroundTaskThread] = set()
		self._refresh_task: Optional[BackgroundTaskThread] = None
		self._data_generation = 0

		# Each queued function carries the exact metrics invalidated by the changed call-graph edges;
		# analysis-only FunctionUpdated waves are not subscribed to at all.
		self._pending_changes: Dict[FunctionKey, Tuple[Function, Set[str]]] = {}
		self._change_flush_task: Optional[BackgroundTaskThread] = None
		# How many functions were queued the *previous* time a flush started - see
		# _start_change_flush_if_needed's growth check. None once things have settled (nothing left
		# pending), so an unrelated future burst starts its own comparison from scratch.
		self._last_flush_pending_count: Optional[int] = None

		# Unregistered/cancelled on destruction so a closed/replaced table doesn't keep listening
		# (or keep dangling Python-side references alive) - this closure captures only
		# `bv`/`notifier`/`change_tasks`, not `self`, for the same reason `_track` elsewhere avoids
		# capturing the object whose `destroyed` signal it's reacting to: capturing `self` here would
		# be a reference cycle.
		self._incoming: Dict[FunctionKey, Set[FunctionKey]] = {}
		self._rebuild_incoming()
		self._notifier = _FunctionChangeNotification(self._on_function_change_batch)
		self.bv.register_notification(self._notifier)
		bv, notifier, change_tasks = self.bv, self._notifier, self._change_tasks

		def _on_destroyed():
			bv.unregister_notification(notifier)
			for task in change_tasks:
				task.cancel()

		self.destroyed.connect(_on_destroyed)

	def _rebuild_incoming(self) -> None:
		self._incoming = {}
		for caller, calls in self._call_sites.items():
			for callee in set(calls.internal_callee_keys):
				self._incoming.setdefault(callee, set()).add(caller)

	def _reverse_closure(
		self, seeds: Set[FunctionKey], depth: int = 2,
		incoming: Optional[Dict[FunctionKey, Set[FunctionKey]]] = None,
	) -> Set[FunctionKey]:
		incoming = self._incoming if incoming is None else incoming
		seen = set(seeds)
		frontier = set(seeds)
		for _ in range(depth):
			frontier = {caller for key in frontier for caller in incoming.get(key, set())} - seen
			seen.update(frontier)
		return seen

	def _transitive_from_graph(self, root: FunctionKey) -> float:
		visited: Set[FunctionKey] = set()

		def walk(key: FunctionKey, depth: int) -> float:
			if key in visited:
				return 0.0
			visited.add(key)
			values = self.model.values_for(key)
			if values is None:
				return 0.0
			total = values.get('composite', 0.0)
			if depth <= 0:
				return total
			for callee in set(self._call_sites.get(key, complexity._CallSites([], 0, 0)).internal_callee_keys):
				total += 0.5 * walk(callee, depth - 1)
			return total

		return walk(root, 2)

	def _apply_transitive_updates(
		self, keys: Set[FunctionKey]
	) -> Dict[FunctionKey, Tuple[Dict[str, float], complexity._CallSites]]:
		updates = {}
		for key in keys:
			old_values = self.model.values_for(key)
			if old_values is None:
				continue
			values = dict(old_values)
			values['transitive'] = self._transitive_from_graph(key)
			self.model.update_row(key, values)
			updates[key] = (values, self._call_sites.get(key, complexity._CallSites([], 0, 0)))
		return updates

	def _queue_metrics(self, func: Function, metrics: Set[str]) -> None:
		key = complexity.function_key(func)
		if not metrics:
			return
		old = self._pending_changes.get(key)
		self._pending_changes[key] = (func, set(metrics) if old is None else old[1] | metrics)

	def _active_sort_needs_update(
		self, changed_metrics: Set[str], names_changed: bool = False, rows_added: bool = False
	) -> bool:
		"""Return whether this mutation can have changed the active row order."""
		if rows_added:
			return True  # appended rows have not been placed into the active order yet
		col = self.table.horizontalHeader().sortIndicatorSection()
		if col == 0:
			return names_changed
		if col == _ADDRESS_COL:
			return False  # incremental updates never change an existing function's start
		return METRICS[col - _FIRST_METRIC_COL] in changed_metrics

	def _on_function_change_batch(self, batch: _FunctionChangeBatch) -> None:
		if not shiboken6.isValid(self):
			return
		if self.bv.analysis_state != AnalysisState.IdleState:
			QTimer.singleShot(100, lambda: self._on_function_change_batch(batch))
			return

		# FunctionLifetime already supplies exact identities. Enumerating every function here made a
		# one-function undefine O(total functions) on the UI thread and was only needed as a defensive
		# reconciliation for an old user-vs-auto-function edge case. FunctionUpdated keeps the same
		# stable identity in that case, so there is no row-set change to reconcile.
		removed = {key for key in batch.removed if self.model.has_function(key)}
		added = {key for key in batch.added if not self.model.has_function(key)}
		structural = bool(removed or added)
		# _rebuild_incoming replaces (rather than mutates) this dictionary, so retaining the old object
		# gives closures an immutable-enough snapshot without copying the entire reverse graph on the
		# UI thread for every notification batch.
		old_incoming = self._incoming
		cache_updates: Dict[FunctionKey, Tuple[Dict[str, float], complexity._CallSites]] = {}
		transitive_dirty: Set[FunctionKey] = set()
		sort_metrics: Set[str] = set()
		# Symbol changes only affect the displayed name. They do not invalidate any complexity metric.
		# Resolve only the changed symbol addresses; scanning all table keys made a rename O(N).
		name_keys = set()
		for address in batch.symbol_addresses:
			for func in self.bv.get_functions_at(address):
				key = complexity.function_key(func)
				if self.model.has_function(key):
					name_keys.add(key)
		self.model.touch_names(name_keys)

		# Removing a function is handled directly from the retained graph. Incoming call sites become
		# external, former callees get a cheap code-reference refresh, and only the two-hop reverse
		# closure can have a different transitive score. Analysis-only update waves are not edit roots.
		for key in removed:
			for caller in old_incoming.get(key, set()):
				calls = self._call_sites.get(caller, complexity._CallSites([], 0, 0))
				removed_count = sum(1 for callee in calls.internal_callee_keys if callee == key)
				if not removed_count:
					continue
				new_calls = complexity._CallSites(
					[callee for callee in calls.internal_callee_keys if callee != key],
					calls.external_count + removed_count,
					calls.indirect_count,
				)
				self._call_sites[caller] = new_calls
				values = dict(self.model.values_for(caller) or {})
				distinct = len(set(new_calls.internal_callee_keys))
				values['fan_out'] = float(
					distinct + 0.25 * (len(new_calls.internal_callee_keys) - distinct) +
					1.5 * new_calls.external_count + 2.5 * new_calls.indirect_count
				)
				self.model.update_row(caller, values)
				cache_updates[caller] = (values, new_calls)
				sort_metrics.add('fan_out')
			for callee in set(self._call_sites.get(key, complexity._CallSites([], 0, 0)).internal_callee_keys):
				func = self.model.function_for(callee)
				if func is not None:
					self._queue_metrics(func, {'code_references'})
			transitive_dirty.update(
				self._reverse_closure(old_incoming.get(key, set()), depth=1, incoming=old_incoming)
			)
			self._call_sites.pop(key, None)

		if removed:
			self.model.remove_functions(removed)
			self._rebuild_incoming()

		for key in added:
			func = batch.added.get(key)
			if func is None:
				continue
			self._queue_metrics(func, set(_INCREMENTAL_UPDATE_METRICS))
			for ref in self.bv.get_code_refs(func.start):
				caller = getattr(ref, 'function', None)
				if caller is not None and self.model.has_function(complexity.function_key(caller)):
					self._queue_metrics(caller, {'fan_out'})

		# DataWritten identifies its root by asking the view only about the written endpoints. This is
		# constant-time for the usual instruction-sized patch; the previous implementation crossed the
		# core boundary for every function's address_ranges on the UI thread.
		data_roots = {}
		for start, length in batch.data_ranges:
			addresses = {start, start + max(length, 1) - 1}
			for address in addresses:
				for func in self.bv.get_functions_containing(address):
					data_roots[complexity.function_key(func)] = func

		if structural:
			# The retained graph above already handled the exact incident edges.
			roots = data_roots
		elif data_roots:
			roots = data_roots
		else:
			# FunctionUpdated and FunctionUpdateRequested are analysis lifecycle signals, not edit
			# signals. Linear View emits them simply by materializing IL while the user scrolls. Treating
			# either as a metric root created a feedback loop: scrolling queued MLIL/HLIL work, which
			# emitted more updates and repeatedly displayed "Updating changed functions"/"Sorting".
			# Byte writes and lifetime events above are the authoritative complexity-changing roots.
			roots = {}
		if len(roots) > _MAX_AUTOMATIC_IL_ROOTS:
			binaryninja.log_warn(
				f'complexity_report: ignored {len(roots)} automatic IL recomputations after one change; '
				'click Refresh to bring the table fully up to date.'
			)
			self._stale = True
			roots = {}
		for key, func in roots.items():
			if self.model.has_function(key) or key in added:
				self._queue_metrics(func, set(_INCREMENTAL_UPDATE_METRICS))
				transitive_dirty.update(self._reverse_closure({key}, incoming=old_incoming))

		self._sort_generation += 1  # invalidate a background sort snapshot taken before these edits
		cache_updates.update(self._apply_transitive_updates(transitive_dirty))
		if transitive_dirty:
			sort_metrics.add('transitive')
		if cache_updates or removed:
			threading.Thread(
				target=_append_cache_changes,
				args=(
					self.bv,
					{key: (dict(values), calls) for key, (values, calls) in cache_updates.items()},
					set(removed), time.time_ns(),
				),
				daemon=True,
			).start()
		self._update_count_label()
		self._start_change_flush_if_needed()
		# Deleting rows preserves their existing order. In particular, a zero-reference undefine now
		# stops here instead of launching a full-table sort whose completion resets the whole model.
		# Real metric changes/additions are sorted by the flush completion below.
		if self._change_flush_task is None and self._active_sort_needs_update(
			sort_metrics, names_changed=bool(name_keys)
		):
			header = self.table.horizontalHeader()
			self._on_sort_indicator_changed(header.sortIndicatorSection(), header.sortIndicatorOrder())

	def _start_change_flush_if_needed(self) -> None:
		"""
		Starts a single background task draining every function currently in `self._pending_changes`,
		unless one is already running - in which case this does nothing, and whatever's already
		queued gets picked up by that task's own completion handler once it finishes (see below).

		This, not `start_function_metrics`/`_FunctionMetricsTask` (one thread per function, meant for
		the "Current Function" tab's single active navigation), is what backs every reaction to
		`_on_function_changed`: a burst of many changed functions arriving close together - whether
		from a flurry of deliberate renames or from analysis catching up right at the edge of the
		idle-state check in `_start_function_change_update` - is coalesced into one thread processing
		them one at a time, instead of one thread per change. Bounding this to a single concurrent
		task is what actually prevents the "opening the binary looks like it hung" failure mode, not
		just reducing its odds.

		Also re-checks analysis state here, not just at the original enqueue point: this is called
		again from a completed flush's own `on_complete` to pick up anything that arrived while it was
		running, and that chain has no idle check of its own. Without one here too, a binary whose
		analysis keeps producing new changes about as fast as they're drained - even if each
		individual one slips past the enqueue-time check by arriving during a brief gap back to
		Idle - could have one flush's completion immediately trigger the next, indefinitely, which is
		exactly what would look like "Updating changed functions..." that never finishes. Skipping
		here just leaves the work queued; the next genuine change notification (or, worst case, the
		next time this table is reset - see _reset_all_functions_tab) tries again.
		"""
		if self._change_flush_task is not None:
			return
		if not self._pending_changes:
			self._last_flush_pending_count = None  # fully settled - an unrelated future burst starts fresh
			return
		if self.bv.analysis_state != AnalysisState.IdleState:
			return

		pending_count = len(self._pending_changes)
		if (
			pending_count >= _RUNAWAY_CHANGE_QUEUE_MIN_SIZE
			and self._last_flush_pending_count is not None
			and pending_count >= self._last_flush_pending_count
		):
			# Not a size check on its own: a single large but finite incident-edge set (for example, a
			# removed dispatcher that called many distinct functions) can legitimately queue many cheap
			# code-reference refreshes. What this actually catches is the queue staying the same size or *growing*
			# from one flush to the next despite continuously processing it - the real "never settles"
			# signature (observed directly: 1243, then 1248, then 1253, ...) - which no amount of
			# patience fixes, unlike an ordinary large-but-finite burst.
			binaryninja.log_warn(
				f'complexity_report: the changed-function queue reached {pending_count} without '
				f'shrinking from the previous flush\'s {self._last_flush_pending_count} - that looks '
				'like it will never settle on its own, so this table is not trying to keep chasing it. '
				'Click Refresh to bring it up to date.'
			)
			self._pending_changes.clear()
			self._last_flush_pending_count = None
			self._stale = True
			self._update_count_label()
			return

		self._last_flush_pending_count = pending_count
		pending, self._pending_changes = self._pending_changes, {}
		generation = self._data_generation

		def on_complete(
			results: Dict[FunctionKey, Tuple[Dict[str, float], Optional[complexity._CallSites]]]
		) -> None:
			self._change_tasks.discard(self._change_flush_task)
			self._change_flush_task = None
			if not shiboken6.isValid(self):
				return
			if generation != self._data_generation:
				return
			# _rebuild_incoming assigns a new dictionary below; retain the old object directly instead
			# of copying every reverse-edge set on the UI thread.
			old_incoming = self._incoming
			changed_graph_keys: Set[FunctionKey] = set()
			changed_targets: Set[FunctionKey] = set()
			cache_updates: Dict[FunctionKey, Tuple[Dict[str, float], complexity._CallSites]] = {}
			transitive_dirty: Set[FunctionKey] = set()
			changed_metrics: Set[str] = set()
			rows_added = False
			for key, (partial_values, new_calls) in results.items():
				changed_metrics.update(partial_values)
				old_values = self.model.values_for(key)
				values = {metric: 0.0 for metric in METRICS} if old_values is None else dict(old_values)
				values.update(partial_values)
				if old_values is None:
					func = pending[key][0]
					self.model.add_function(func, values)
					rows_added = True
				else:
					self.model.update_row(key, values)

				old_calls = self._call_sites.get(key, complexity._CallSites([], 0, 0))
				if new_calls is not None:
					self._call_sites[key] = new_calls
					old_counts = Counter(old_calls.internal_callee_keys)
					new_counts = Counter(new_calls.internal_callee_keys)
					old_targets = set(old_counts)
					new_targets = set(new_counts)
					changed_targets.update(
						key for key in old_targets | new_targets
						if old_counts[key] != new_counts[key]
					)
					if new_targets != old_targets:
						changed_graph_keys.add(key)
						transitive_dirty.update(self._reverse_closure({key}, incoming=old_incoming))
				calls = self._call_sites.get(key, old_calls)
				cache_updates[key] = (values, calls)
				if old_values is None or 'composite' in pending[key][1]:
					transitive_dirty.update(self._reverse_closure({key}, incoming=old_incoming))

			self._rebuild_incoming()
			for key in changed_graph_keys:
				transitive_dirty.update(self._reverse_closure({key}))
			for target in changed_targets:
				func = self.model.function_for(target)
				if func is not None:
					self._queue_metrics(func, {'code_references'})

			cache_updates.update(self._apply_transitive_updates(transitive_dirty))
			if transitive_dirty:
				changed_metrics.add('transitive')
			if cache_updates:
				threading.Thread(
					target=_append_cache_changes,
					args=(
						self.bv,
						{key: (dict(values), calls) for key, (values, calls) in cache_updates.items()},
						set(), time.time_ns(),
					),
					daemon=True,
				).start()
			self._update_count_label()
			# Anything that arrived (via _on_function_changed) while this batch was running is still
			# sitting in self._pending_changes - pick it up now rather than waiting for the next
			# unrelated change to trigger it.
			self._start_change_flush_if_needed()
			if self._change_flush_task is None and self._active_sort_needs_update(
				changed_metrics, rows_added=rows_added
			):
				header = self.table.horizontalHeader()
				self._on_sort_indicator_changed(header.sortIndicatorSection(), header.sortIndicatorOrder())

		self._change_flush_task = _FunctionChangeFlushTask(pending, on_complete)
		self._change_tasks.add(self._change_flush_task)
		self._change_flush_task.start()

	def _build_columns_menu(self) -> None:
		"""
		A checkable dropdown for which columns to show. `Function` (column 0) is deliberately not
		in here at all - it's not optional - everything else defaults to `DEFAULT_VISIBLE_METRICS`
		(plus Address) so a first look at a large binary isn't an overwhelming wall of numbers, but
		stays one click away.

		Choices persist across restarts via QSettings, the same mechanism Binary Ninja's own
		Triage view uses for exactly this kind of "just remember this for next time" UI state (its
		most-recently-opened file) rather than `binaryninja.settings.Settings`: this isn't a
		semantically meaningful, user-documented setting that belongs in BN's Settings UI, just
		local column-visibility state for one panel.
		"""
		menu = QMenu(self.columns_button)

		def add_toggle(identifier: str, col: int, label: str, default_visible: bool) -> None:
			key = f'complexity_report/column_visible/{identifier}'
			visible = QSettings().value(key, default_visible, type=bool)

			action = menu.addAction(label)
			action.setCheckable(True)
			action.setChecked(visible)
			self.table.setColumnHidden(col, not visible)

			def on_toggled(checked, key=key, col=col):
				self.table.setColumnHidden(col, not checked)
				QSettings().setValue(key, checked)

			action.toggled.connect(on_toggled)

		add_toggle('address', _ADDRESS_COL, 'Address', True)
		menu.addSeparator()
		for i, metric in enumerate(METRICS):
			add_toggle(metric, _FIRST_METRIC_COL + i, METRIC_LABELS[metric], metric in DEFAULT_VISIBLE_METRICS)

		self.columns_button.setMenu(menu)

	def values_for_function(self, func: Function) -> Optional[Dict[str, float]]:
		"""Return a copy of an already-swept row for the Current Function tab."""
		values = self.model.values_for(complexity.function_key(func))
		return None if values is None else dict(values)

	def _update_count_label(self) -> None:
		shown, total = self.proxy.rowCount(), self.model.rowCount()
		text = f'{total} function{"s" if total != 1 else ""}' if shown == total else f'{shown} of {total} functions'
		if self._from_cache:
			text += ' (from cache)'
		if self._stale:
			text += ' - click Refresh, results may be outdated'
		self.count_label.setText(text)

	def _on_filter_changed(self, text: str) -> None:
		self.proxy.setFilterFixedString(text)
		self._update_count_label()

	def _on_sort_indicator_changed(self, col: int, order: Qt.SortOrder) -> None:
		"""
		Reacts to the header's own indicator change (a real click, or the initial one set up in
		__init__) by computing the new order in the background - see `_SortRowsTask` for why this,
		rather than just letting Qt's own comparator-based sort run synchronously here.
		"""
		self._sort_generation += 1
		generation = self._sort_generation
		key = _row_sort_key(col)
		reverse = order == Qt.DescendingOrder

		task = None

		def on_complete(sorted_rows: List[Tuple[Function, Dict[str, float]]]) -> None:
			self._change_tasks.discard(task)
			if self._sort_task is task:
				self._sort_task = None
			if generation != self._sort_generation:
				return  # superseded by a later header click before this one finished
			if not shiboken6.isValid(self):
				return
			self.model.set_rows(sorted_rows)

		task = _SortRowsTask(self.model.rows_snapshot(), key, reverse, on_complete)
		self._sort_task = task
		self._change_tasks.add(task)
		task.start()

	def _navigate_to_selected(self, proxy_index) -> None:
		source_index = self.proxy.mapToSource(proxy_index)
		if not source_index.isValid():
			return
		func = self.model.function_at(source_index.row())
		self.bv.navigate(self.bv.view, func.start)

	def _refresh(self) -> None:
		self._data_generation += 1
		generation = self._data_generation
		self._pending_changes.clear()
		if self._change_flush_task is not None:
			self._change_flush_task.cancel()
		self.refresh_button.setEnabled(False)
		self.refresh_button.setText('Refreshing…')

		def on_complete(data: Optional[SweepData]) -> None:
			self._change_tasks.discard(self._refresh_task)
			self._refresh_task = None
			# Same race as _on_function_changed's on_complete: this table can be destroyed (e.g. by
			# switching to a different binary's tab) while a Refresh is still running, and cancelling
			# it at that point can't unpost a callback that had already been queued via
			# execute_on_main_thread.
			if not shiboken6.isValid(self):
				return
			if generation != self._data_generation:
				return
			if data is None:
				self.refresh_button.setEnabled(True)
				self.refresh_button.setText('Refresh')
				return
			# _ComputeComplexityTask already saved this to disk on its own thread; no need to do it
			# again here on the main thread.
			# Re-sorted by whatever column is currently shown as active (defaulting to composite
			# descending, same as a fresh table - see __init__'s initial header.setSortIndicator call)
			# rather than unconditionally by composite: header clicks no longer go through Qt's own
			# proxy-based sort at all (see _on_sort_indicator_changed), so there's no automatic
			# re-application of a previously chosen sort column left to fall back on here - without
			# this, Refresh would silently discard whatever you'd actually sorted by.
			header = self.table.horizontalHeader()
			key = _row_sort_key(header.sortIndicatorSection())
			reverse = header.sortIndicatorOrder() == Qt.DescendingOrder
			self._call_sites = dict(data.call_sites)
			self._rebuild_incoming()
			self.model.set_rows(sorted(data.rows, key=key, reverse=reverse))
			self._from_cache = False
			self._stale = False  # a full Refresh is exactly what recovers from a dropped batch
			self._update_count_label()
			self.refresh_button.setEnabled(True)
			self.refresh_button.setText('Refresh')

		self._refresh_task = _ComputeComplexityTask(self.bv, on_complete)
		self._change_tasks.add(self._refresh_task)
		self._refresh_task.start()


class FunctionMetricsWidget(QTableView):
	"""The All Functions table presentation with zero or one source-model rows."""

	def __init__(self, parent=None):
		super().__init__(parent)
		self.model = _ComplexityTableModel([], parent=self)
		_configure_complexity_table(self, self.model)
		_apply_saved_column_visibility(self)
		self._sized_for_first_row = False

	def set_function(self, func: Optional[Function], values: Optional[Dict[str, float]]) -> None:
		if func is None:
			self.model.set_rows([])
			return
		row_values = {_PENDING_VALUES_KEY: True} if values is None else values
		self.model.set_rows([(func, row_values)])
		if not self._sized_for_first_row:
			# This model was empty during construction, unlike All Functions, so its initial content
			# measurement could only see the headers. Size Function/Address as soon as the pending row
			# appears, then all columns once its real metrics arrive. Preserve any widths the user
			# already chose in either table and do not record automatic measurements as manual choices.
			header = self.horizontalHeader()
			previously_blocked = header.blockSignals(True)
			try:
				columns = range(self.model.columnCount()) if values is not None else range(_FIRST_METRIC_COL)
				for col in columns:
					saved_width = QSettings().value(
						f'complexity_report/column_width/{_column_identifier(col)}', -1, type=int
					)
					if saved_width <= 0 and not self.isColumnHidden(col):
						self.resizeColumnToContents(col)
			finally:
				header.blockSignals(previously_blocked)
			self._sized_for_first_row = values is not None


def new_function_metric_table() -> FunctionMetricsWidget:
	"""Create the one-row Current Function view using the All Functions schema."""
	return FunctionMetricsWidget()


def clear_function_metrics(table, func: Optional[Function] = None) -> None:
	"""Keep the table mounted; show either no row or a pending row for `func`."""
	table.set_function(func, None)


def compute_function_metrics(
	func: Function,
	task: Optional[BackgroundTaskThread] = None,
	cache: Optional[complexity.ComplexityCache] = None,
	metrics: Optional[List[str]] = None,
) -> Optional[Dict[str, float]]:
	"""
	Computes every metric for a single function. Most functions are cheap, but `transitive`/
	`fan_out` are interprocedural - for a highly-connected function (e.g. one many other functions
	call through, or a hub near the entry point) `transitive` in particular can end up touching a
	meaningful chunk of the binary's call graph. Call this off the UI thread (see
	`start_function_metrics`) rather than inline from a navigation event handler: a synchronous
	call there is exactly what turned "open a ~1000-function binary" into a multi-second freeze -
	navigating to even one moderately-connected function blocked the UI for as long as its
	transitive walk took.

	Pass `task` to allow this to be interrupted, both between metrics (checked before each one) and,
	for `transitive` specifically, *during* it - its recursive call-graph walk is the one metric
	whose single computation can itself take a very long time (a function embedded in a large,
	tightly-interconnected cluster, e.g. a parser's mutually-calling helpers), so a between-metrics
	check alone isn't enough to keep a cancellation prompt. Without either, a superseded computation
	- you navigated to another function, or switched binaries, before this one finished - ran to
	completion regardless of how long it took, competing the whole time for the same core-level
	analysis locks as whatever superseded it and slowing both down; that combination is what could
	make a stale computation look permanently "stuck" rather than just slow. Returns None if
	cancelled before finishing.

	Pass `cache` to reuse a `ComplexityCache` across several calls, e.g. one flush of several
	functions changed at once in `_FunctionChangeFlushTask` - a shared callee reached by more than
	one of them then only gets its own complexity computed once. Defaults to a private one-off cache
	otherwise (still shared across this one function's own composite/fan_out/transitive below, just
	not across separate calls to this function).

	Pass `metrics` to compute only a subset (default: all of them) - see `_FunctionChangeFlushTask`
	for why it deliberately excludes `transitive`: unlike every other metric, `transitive` reads
	*other* functions' own MLIL/HLIL (walking callees - see `transitive_complexity`), which can force
	Binary Ninja to finalize analysis for a function that had never been touched before. Historically
	those analysis notifications fed back into this same incremental path and snowballed; they are no
	longer subscribed to, but the graph-only incremental implementation remains substantially cheaper.
	"""
	if metrics is None:
		metrics = METRICS
	values = {}
	# Shared across composite/fan_out/transitive: transitive's walk starts by computing this same
	# function's own composite_complexity, which is otherwise entirely redundant with computing
	# the `composite` metric separately just above it in METRICS - a shared cache means that work
	# happens once instead of twice per navigation.
	if cache is None:
		cache = complexity.ComplexityCache([func])
	else:
		cache.remember_function(func)
	is_cancelled = (lambda: task.cancelled) if task is not None else None
	bundle_metrics = set(METRICS) - {'transitive'}
	if bundle_metrics.issubset(metrics):
		try:
			values = complexity.compute_metric_bundle(func, cache=cache)
			if 'transitive' in metrics:
				values['transitive'] = complexity.get_code_complexity(
					func, 'transitive', cache=cache, is_cancelled=is_cancelled
				)
			return {metric: values[metric] for metric in metrics}
		except Exception as e:
			binaryninja.log_warn(f'complexity_report: bundled metric pass failed for {func.name}: {e}')
			# Preserve the old per-metric fault isolation as a fallback: one malformed IL expression
			# should not discard every otherwise-computable value in the row.
			values = {}
	for metric in metrics:
		if task is not None and task.cancelled:
			return None
		try:
			if metric == 'composite' and all(name in values for name in complexity.DEFAULT_COMPOSITE_WEIGHTS):
				values[metric] = complexity.composite_from_metrics(values)
				cache.composite[complexity.function_key(func)] = values[metric]
			else:
				values[metric] = complexity.get_code_complexity(func, metric, cache=cache, is_cancelled=is_cancelled)
		except Exception as e:
			binaryninja.log_warn(f'complexity_report: failed to compute {metric} for {func.name}: {e}')
			values[metric] = 0 if metric == 'code_references' else 0.0
	return values


def apply_function_metrics(table, func: Function, values: Dict[str, float]) -> None:
	"""Populate the Current Function row without triggering any metric computation."""
	table.set_function(func, values)


class _SortRowsTask(BackgroundTaskThread):
	"""
	Computes a new sort order for the whole-binary table's rows entirely in plain Python - no Qt
	objects touched anywhere in `run()`, which is what makes this safe to run off the main thread at
	all: `QSortFilterProxyModel`/`QTableView` are not thread-safe, so there's no way to run Qt's own
	comparator-based sort itself on a background thread, safely or otherwise. See
	`ComplexityTableWidget`'s header-click wiring for why a click goes through this instead of the
	far simpler `header.sortIndicatorChanged.connect(self.table.sortByColumn)`: that runs Qt's own
	sort synchronously on the main thread, calling back into a Python comparator for every pairwise
	comparison - measured at over 7 seconds for a ~76,000-row table. `sorted()` here does the same
	number of comparisons, just off the main thread and without the per-comparison Qt/Python
	round-trip, and the result is applied with a single cheap model reset instead.
	"""

	def __init__(
		self, rows: List[Tuple[Function, Dict[str, float]]], key: Callable, reverse: bool, on_complete
	):
		super().__init__('Sorting...', can_cancel=False)
		self.rows = rows
		self.key = key
		self.reverse = reverse
		self.on_complete = on_complete  # on_complete(sorted_rows)

	def run(self):
		sorted_rows = sorted(self.rows, key=self.key, reverse=self.reverse)
		execute_on_main_thread(lambda: self.on_complete(sorted_rows))


class _FunctionMetricsTask(BackgroundTaskThread):
	"""Computes one function's metrics off the UI thread; see `compute_function_metrics`."""

	def __init__(self, func: Function, on_complete):
		super().__init__(f'Computing complexity for {func.name}...', can_cancel=True)
		self.func = func
		self.on_complete = on_complete  # on_complete(values)

	def run(self):
		values = compute_function_metrics(self.func, task=self)
		if values is not None and not self.cancelled:
			execute_on_main_thread(lambda: self.on_complete(values))


def start_function_metrics(func: Function, on_complete) -> BackgroundTaskThread:
	"""
	Starts a background task computing `func`'s metrics, calling `on_complete(values)` on the main
	thread when done. The one entry point other modules (the sidebar widget) should need for the
	"current function" tab.

	Returns the task so the caller can `.cancel()` it if it's superseded before finishing (e.g. by
	a later navigation) - important since, left uncancelled, it would otherwise keep running (and
	competing for core-level analysis locks) for as long as it takes regardless of whether anything
	still needs its result. The caller is expected to do this; this function doesn't track previous
	calls itself.
	"""
	task = _FunctionMetricsTask(func, on_complete)
	task.start()
	return task


class _FunctionChangeFlushTask(BackgroundTaskThread):
	"""
	Recomputes the exact metric subset queued for each function in `pending` (a snapshot handed off by
	`_start_change_flush_if_needed` - never touched again after that handoff) one at a time in this
	one thread, then returns values plus any refreshed outgoing-edge snapshot on the main thread.
	It preserves everything it did not recompute by merging into the existing row. See
	`_start_change_flush_if_needed` for why this
	exists instead of one `_FunctionMetricsTask` per changed function: bounding the whole-binary
	table's reaction to function-change notifications to a single concurrent background task,
	regardless of how many functions changed at once, is what keeps a burst of them (a flurry of
	renames, or a large binary's own analysis still settling right as this table opened) from
	spinning up an unbounded pile of simultaneous recomputes.

	`transitive` is deliberately never queued here - see `compute_function_metrics`'s docs for why:
	reading other functions'
	MLIL/HLIL for the first time can itself fire more change notifications, and chasing those through
	this same path is what turned undefining one function with no code references into an
	ever-growing queue that recomputed several thousand unrelated ones and never settled.
	The widget instead recomputes transitive values from its retained numeric composite map and call
	graph for only the changed function's two-hop reverse closure.
	"""

	def __init__(self, pending: Dict[FunctionKey, Tuple[Function, Set[str]]], on_complete):
		super().__init__('Updating changed functions...', can_cancel=True)
		self.pending = pending
		self.on_complete = on_complete

	def run(self):
		cache = complexity.ComplexityCache(func for func, _metrics in self.pending.values())
		results: Dict[FunctionKey, Tuple[Dict[str, float], Optional[complexity._CallSites]]] = {}
		total = len(self.pending)
		for i, (key, (func, metrics)) in enumerate(self.pending.items()):
			if self.cancelled:
				break
			# Without this, the progress text stays frozen on "Updating changed functions..." for
			# this task's entire run, whether it's about to finish or still grinding through a large
			# backlog (each function here can itself cost anywhere from sub-millisecond to tens of
			# seconds - see compute_function_metrics's own docs on `transitive`) - indistinguishable
			# from a genuine hang. Naming the current function too, since that's exactly the detail
			# needed to tell "slow because of one very well-connected function" apart from "stuck."
			self.progress = f'Updating changed functions... ({i + 1}/{total}: {func.name})'
			ordered_metrics = [metric for metric in _INCREMENTAL_UPDATE_METRICS if metric in metrics]
			values = compute_function_metrics(func, task=self, cache=cache, metrics=ordered_metrics)
			if values is not None:
				calls = cache.call_sites.get(key) if 'fan_out' in metrics else None
				results[key] = (values, calls)
		execute_on_main_thread(lambda: self.on_complete(results))


def _scan_all_rows(bv: BinaryView, task: BackgroundTaskThread) -> Optional[SweepData]:
	"""
	Computes every metric for every function in `bv`, reporting progress through `task` and
	honoring cancellation. Returns None if cancelled partway through. Shared by both background
	tasks below (a fresh scan on first open with no cache, and an explicit Refresh) so the actual
	scan loop only exists once.
	"""
	rows: List[Tuple[Function, Dict[str, float]]] = []
	functions = list(bv.functions)
	# Shared across every function in this scan: a helper reachable from many callers (a logging
	# routine, an allocator wrapper, ...) has its own cyclomatic/instruction/etc. complexity
	# computed once here, not once per caller that reaches it within `transitive`'s hop limit. See
	# ComplexityCache's docstring - this is what makes scanning a binary with tens of thousands of
	# functions and shared utility code tractable instead of redoing the same handful of common
	# callees' analysis over and over.
	cache = complexity.ComplexityCache(functions)
	is_cancelled = lambda: task.cancelled
	non_transitive_metrics = [metric for metric in METRICS if metric != 'transitive']
	for i, func in enumerate(functions):
		if task.cancelled:
			return None
		task.progress = f'Computing code complexity... ({i + 1}/{len(functions)})'
		try:
			values = compute_function_metrics(func, task=task, cache=cache, metrics=non_transitive_metrics)
		except Exception as e:
			binaryninja.log_warn(f'complexity_report: skipping {func.name}: {e}')
			continue
		if values is not None:
			rows.append((func, values))

	# Run the graph-dependent metric only after every function's own composite and call-site data
	# is warm. This makes transitive a graph walk over cached numbers instead of recursively forcing
	# first access to other functions' IL in whichever order the function list happened to use.
	for i, (func, values) in enumerate(rows):
		if task.cancelled:
			return None
		task.progress = f'Computing transitive complexity... ({i + 1}/{len(rows)})'
		values['transitive'] = complexity.get_code_complexity(
			func, 'transitive', cache=cache, is_cancelled=is_cancelled
		)
	if task.cancelled:
		return None
	return SweepData(
		rows,
		{
			complexity.function_key(func): cache.call_sites.get(
				complexity.function_key(func), complexity._CallSites([], 0, 0)
			)
			for func, _values in rows
		},
	)


def _wait_for_analysis(bv: BinaryView, task: BackgroundTaskThread) -> None:
	"""
	Blocks until analysis settles - safe here since this only ever runs on a background task
	thread, never the UI thread, where `update_analysis_and_wait` is explicitly disallowed by the
	core. Without this, a sweep started while a large binary is still being auto-analyzed would
	only see whatever functions had been discovered so far: not just an incomplete table, but one
	that would look permanently "done" and correct even after analysis finished discovering more
	functions behind it. A no-op (returns immediately) if analysis is already idle.
	"""
	if bv.analysis_state != AnalysisState.IdleState:
		task.progress = 'Waiting for analysis to complete...'
	bv.update_analysis_and_wait()


def _save_all(bv: BinaryView, data: SweepData, revision: Optional[int] = None) -> None:
	"""
	Writes a freshly computed sweep to the local disk cache, and - if the "Also save results in the
	.bndb" setting is on - also embeds it in the .bndb itself (see `_save_to_bndb`). The one place
	both `_ComputeComplexityTask` and `_LoadComplexityTask` save a fresh sweep from, so the two
	destinations can't drift out of sync with each other.
	"""
	_save_cache(bv, data, revision=revision)
	if QSettings().value(SAVE_TO_BNDB_SETTING_KEY, False, type=bool):
		_save_to_bndb(bv, data)


class _ComputeComplexityTask(BackgroundTaskThread):
	"""Used by the Refresh button: always recomputes from scratch (never consults the cache) and
	re-saves it, reporting progress and supporting cancellation like any other background scan."""

	def __init__(self, bv: BinaryView, on_complete):
		super().__init__('Computing code complexity...', can_cancel=True)
		self.bv = bv
		self.on_complete = on_complete
		self.revision = time.time_ns()

	def run(self):
		_wait_for_analysis(self.bv, self)
		data = _scan_all_rows(self.bv, self)
		if data is None:
			execute_on_main_thread(lambda: self.on_complete(None))
			return
		_save_all(self.bv, data, revision=self.revision)
		execute_on_main_thread(lambda: self.on_complete(data))


class _LoadComplexityTask(BackgroundTaskThread):
	"""
	Used when first opening the report. Tries the on-disk cache before falling back to a full
	scan, entirely off the UI thread: for a large binary, just reading the cache file and
	resolving tens of thousands of addresses back to `Function` objects is itself slow enough to
	freeze the window if done inline on the main thread, which is exactly what a fresh cache hit
	used to do.
	"""

	def __init__(self, bv: BinaryView, on_complete):
		super().__init__('Loading code complexity...', can_cancel=True)
		self.bv = bv
		self.on_complete = on_complete  # on_complete(SweepData_or_None, from_cache)

	def run(self):
		# Wait before even considering the cache, not just before a fresh scan: a binary can reach
		# this panel with analysis still in flight (e.g. it was only just opened), in which case
		# there may be no cache yet - going through the cache-check first wouldn't skip this wait,
		# it would just move it later, after uselessly failing to find a cache.
		_wait_for_analysis(self.bv, self)

		cached_data = _try_load_cache(self.bv, self)
		if cached_data is not None:
			execute_on_main_thread(lambda: self.on_complete(cached_data, True))
			return
		if self.cancelled:
			# Always call on_complete, even on cancellation (with rows=None) - the caller's callback
			# is what resets the Run Sweep button back to clickable. Returning silently here used to
			# leave the button stuck reading "Running..." and disabled forever whenever this was
			# cancelled from Binary Ninja's own progress/cancel UI rather than superseded by a binary
			# switch (which resets the button a different way, via _reset_all_functions_tab).
			execute_on_main_thread(lambda: self.on_complete(None, False))
			return

		# No usable local cache - before falling back to a full scan, check whether a previous sweep
		# was embedded directly in this .bndb (see _try_load_from_bndb): the local cache is per-
		# machine (a temp file, keyed off the binary's path), so this is what lets a sweep survive
		# being shared to a different machine, or the local cache simply being cleared, without
		# redoing a scan that can take a long time on a large binary. Written back to the local cache
		# too, so the *next* load on this machine hits the fast path instead of re-querying the .bndb.
		bndb_data = _try_load_from_bndb(self.bv, self)
		if bndb_data is not None:
			_save_cache(self.bv, bndb_data)
			execute_on_main_thread(lambda: self.on_complete(bndb_data, True))
			return
		if self.cancelled:
			execute_on_main_thread(lambda: self.on_complete(None, False))
			return

		data = _scan_all_rows(self.bv, self)
		if data is None:
			execute_on_main_thread(lambda: self.on_complete(None, False))
			return
		_save_all(self.bv, data)
		execute_on_main_thread(lambda: self.on_complete(data, False))


def start_loading(bv: BinaryView, on_complete) -> BackgroundTaskThread:
	"""
	Starts a background task that loads cached complexity rows for `bv`, or runs a fresh scan if
	there's no usable cache, calling `on_complete(data, from_cache)` on the main thread when done
	(or never, if the task is cancelled). Use this rather than `_LoadComplexityTask` directly -
	it's the one entry point other modules (the sidebar widget) should need.

	Returns the task so the caller can `.cancel()` it if it's superseded before finishing - e.g.
	the user switched to a different binary while this one's sweep was still running. The caller
	is expected to do this; this function doesn't track previous calls itself.
	"""
	task = _LoadComplexityTask(bv, on_complete)
	task.start()
	return task


class _TryLoadCacheTask(BackgroundTaskThread):
	"""
	Used for silently auto-reloading a previously-computed sweep when switching to a different
	binary. Unlike `_LoadComplexityTask`, this never falls back to a fresh scan if there's no
	usable cache - auto-reload is a convenience for a binary you've already swept, and should never
	itself trigger an expensive whole-binary recompute; that stays behind the explicit Run Sweep
	button. Still has to run in the background rather than inline, though: resolving tens of
	thousands of cached addresses back to `Function` objects is itself slow enough to freeze the UI
	if done on the main thread (see `_LoadComplexityTask`).

	Deliberately skips `_wait_for_analysis`: this is a best-effort, non-blocking convenience, not
	something that should make switching binaries feel slow while analysis is still catching up.
	If analysis genuinely is incomplete, `_try_load_cache`'s own row-count check will most likely
	reject the cache anyway (a cache written when analysis was complete won't match an
	still-incomplete function list), which is the right outcome here regardless: silently do
	nothing rather than show a table that might be missing functions.
	"""

	def __init__(self, bv: BinaryView, on_complete):
		super().__init__('Checking for cached code complexity...', can_cancel=True)
		self.bv = bv
		self.on_complete = on_complete  # on_complete(SweepData_or_None)

	def run(self):
		data = _try_load_cache(self.bv, self)
		if data is None and not self.cancelled:
			# Same fallback as _LoadComplexityTask: a sweep embedded in this .bndb (see
			# _try_load_from_bndb) but not yet mirrored into the local cache - e.g. this binary was
			# just opened for the first time on this machine - still counts as "already swept" for
			# auto-reload's purposes. Written back to the local cache so the next switch is fast.
			data = _try_load_from_bndb(self.bv, self)
			if data is not None:
				_save_cache(self.bv, data)
		# Always call on_complete, even on cancellation (with rows=None) - see the matching comment
		# in _LoadComplexityTask.run(). No button is tied to this particular task, but leaving
		# on_complete uncalled would leak self._sweep_task in the sidebar widget until the next
		# binary switch happens to clean it up.
		execute_on_main_thread(lambda: self.on_complete(None if self.cancelled else data))


def start_cache_check(bv: BinaryView, on_complete) -> BackgroundTaskThread:
	"""
	Starts a background task that silently checks for (and loads, if present and valid) a cached
	sweep for `bv`, calling `on_complete(data_or_None)` on the main thread when done. Never
	triggers a fresh scan - see `_TryLoadCacheTask`. Returns the task so the caller can `.cancel()`
	it if superseded before finishing, same as `start_loading`.
	"""
	task = _TryLoadCacheTask(bv, on_complete)
	task.start()
	return task
