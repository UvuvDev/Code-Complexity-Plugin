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
Code Complexity sidebar panel - docked in the same bottom-left area as Console/Log (this is what
GlobalAreaWidget used to be for; as of Binary Ninja 4.0 that's implemented as a SidebarWidget with
`SidebarWidgetLocation.LeftBottom` instead) rather than a popup dialog.

Two tabs:

* "Current Function" - metrics for whichever function contains the current cursor/selection,
  refreshed as navigation changes. Usually fast, but `transitive`/`fan_out` can walk a meaningful
  chunk of the call graph for a well-connected function, so the computation runs in the background
  (see dialog.py's `start_function_metrics`) rather than blocking navigation - and only runs at
  all while this panel is visible, so it doesn't cost anything while collapsed.
* "All Functions" - the whole-binary sweep. Deliberately opt-in: scoring every function in a large
  binary is comparatively expensive, so this tab starts idle with an explanation and a button, and
  only starts the background scan (or loads the cache - see dialog.py's `start_loading`) once you
  click it.
"""

from typing import Optional

from PySide6.QtCore import Qt, QRectF, QSettings, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import (
	QLabel, QPushButton, QStackedWidget, QTabWidget, QVBoxLayout, QWidget
)

from binaryninjaui import (
	Sidebar, SidebarContextSensitivity, SidebarWidget, SidebarWidgetLocation, SidebarWidgetType,
)

from binaryninja.binaryview import BinaryView
from binaryninja.enums import AnalysisState
from binaryninja.function import Function

from . import dialog


class ComplexitySidebarWidget(SidebarWidget):
	def __init__(self, name, frame, data):
		SidebarWidget.__init__(self, name)
		self.bv: Optional[BinaryView] = data if isinstance(data, BinaryView) else None
		self.current_func: Optional[Function] = None
		# Bumped on every navigation; a background computation started for an older generation is
		# discarded when it completes if a newer one has since superseded it (see
		# _refresh_current_function_tab), so rapid navigation can't pile up stale in-flight work or
		# apply an out-of-order result.
		self._func_generation = 0
		# The currently in-flight per-function background task, if any - cancelled (see
		# _refresh_current_function_tab) as soon as it's superseded, rather than left to run to
		# completion uncancelled. Without this, navigating repeatedly (or switching binaries
		# entirely) could pile up several of these all still computing at once, each competing for
		# the same core-level analysis locks - which is what actually made things look "stuck"
		# rather than just stale.
		self._current_func_task = None
		# The whole-binary sweep is per-binary, but this widget instance is not (see
		# contextSensitivity below) - these two fields are what make the "All Functions" tab
		# track whichever binary is actually active instead of just showing whatever it last
		# showed regardless of which tab/file you've since switched to. See
		# _reset_all_functions_tab.
		self._sweep_task = None
		self._sweep_table = None

		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		self.tabs = QTabWidget()
		# Match Binary Ninja's original sidebar treatment and keep the metric views vertically compact.
		self.tabs.setTabPosition(QTabWidget.West)
		layout.addWidget(self.tabs)

		self._build_current_function_tab()
		self._build_all_functions_tab()
		self.tabs.currentChanged.connect(self._on_metric_tab_changed)
		self._refresh_current_function_tab()

	def _build_current_function_tab(self) -> None:
		tab = QWidget()
		self._current_function_tab = tab
		layout = QVBoxLayout(tab)
		layout.setContentsMargins(8, 5, 8, 5)
		layout.setSpacing(4)

		self.current_func_table = dialog.new_function_metric_table()
		layout.addWidget(self.current_func_table)
		self.tabs.addTab(tab, 'Current Function')

	def _clear_current_metrics(self) -> None:
		dialog.clear_function_metrics(self.current_func_table, self.current_func)

	def _apply_current_metrics(self, func, values) -> None:
		dialog.apply_function_metrics(self.current_func_table, func, values)

	def _on_metric_tab_changed(self, _index: int) -> None:
		if self.tabs.currentWidget() is self._current_function_tab:
			self._refresh_current_function_tab()
			return
		# A computation started while Current Function was visible is no longer useful once the user
		# switches away. Cancel it and invalidate any already-posted completion callback.
		if self._current_func_task is not None:
			self._current_func_task.cancel()
			self._current_func_task = None
		self._func_generation += 1

	def _build_all_functions_tab(self) -> None:
		tab = QWidget()
		tab_layout = QVBoxLayout(tab)

		# Both the auto-reload toggle and the "also save in the .bndb" toggle live on the sweep
		# table itself now (see dialog.py's ComplexityTableWidget - the checkbox next to its Columns
		# button, and an entry in that same dropdown, respectively) rather than as their own rows
		# here: this tab only ever shows either the intro page or that table, and a settings row
		# that's always present regardless of which one is showing was pushing the whole panel down
		# tall enough to crowd out the linear/graph view next to it.
		self.all_functions_stack = QStackedWidget()

		# Kept alive permanently (unlike a sweep table, which is destroyed and rebuilt per binary -
		# see _reset_all_functions_tab) so switching back to it is just setCurrentWidget, not a
		# rebuild.
		self._intro_page = QWidget()
		intro_layout = QVBoxLayout(self._intro_page)
		intro_layout.addStretch()
		intro_label = QLabel(
			'Computing code complexity for every function can take a while on large binaries.\n\n'
			'Run the sweep to see every function ranked, sortable, and filterable.'
		)
		intro_label.setWordWrap(True)
		intro_label.setAlignment(Qt.AlignCenter)
		intro_layout.addWidget(intro_label)
		self.run_sweep_button = QPushButton('Run Sweep')
		self.run_sweep_button.clicked.connect(self._run_sweep)
		intro_layout.addWidget(self.run_sweep_button, alignment=Qt.AlignCenter)
		intro_layout.addStretch()
		self.all_functions_stack.addWidget(self._intro_page)  # index 0, shown until a sweep runs

		tab_layout.addWidget(self.all_functions_stack)
		self.tabs.addTab(tab, 'All Functions')

	def _run_sweep(self) -> None:
		if self.bv is None:
			return
		self.run_sweep_button.setEnabled(False)
		self.run_sweep_button.setText('Running…')
		bv = self.bv  # captured: if the active binary changes before this finishes, compare against
		# this, not self.bv, to tell whether the result is still for the binary that's now active.

		def on_complete(data, from_cache):
			self._sweep_task = None
			if bv != self.bv:
				return  # switched to a different binary before this finished - not our problem
				# anymore; _reset_all_functions_tab already put things back for whichever binary
				# is now active.
			# Always restore the button, even when rows is None (the sweep was cancelled, e.g. via
			# Binary Ninja's own progress/cancel UI, rather than completed or superseded by a binary
			# switch) - otherwise it's left reading "Running..." and disabled with no way to retry.
			self.run_sweep_button.setEnabled(True)
			self.run_sweep_button.setText('Run Sweep')
			if data is None:
				return
			table = dialog.ComplexityTableWidget(bv, data, from_cache)
			self.all_functions_stack.addWidget(table)
			self.all_functions_stack.setCurrentWidget(table)
			self._sweep_table = table

		self._sweep_task = dialog.start_loading(bv, on_complete)

	def _reset_all_functions_tab(self) -> None:
		"""
		Called whenever the active binary changes (see notifyViewChanged). This widget instance is
		shared across every open binary (SelfManagedSidebarContext - see the class below), but a
		built sweep table is for one specific binary; without this, switching binaries left
		whichever table you'd last built sitting there, silently showing the wrong binary's data.

		Discards the old table rather than keeping it around per binary: the on-disk cache (see
		dialog.py) already makes clicking Run Sweep again on a binary you've swept before a near-
		instant reload, not a real recompute, so there's little to gain from keeping multiple
		binaries' full tables - potentially tens of thousands of rows each - resident in memory at
		once just to avoid that one click.
		"""
		if self._sweep_task is not None:
			self._sweep_task.cancel()
			self._sweep_task = None

		if self._sweep_table is not None:
			self.all_functions_stack.removeWidget(self._sweep_table)
			self._sweep_table.deleteLater()
			self._sweep_table = None

		self.all_functions_stack.setCurrentWidget(self._intro_page)
		self.run_sweep_button.setEnabled(True)
		self.run_sweep_button.setText('Run Sweep')

		self._try_auto_reload()

	def _try_auto_reload(self) -> None:
		"""
		If enabled (see the checkbox next to the sweep table's Columns button, in
		dialog.ComplexityTableWidget) and the newly-active binary already has a valid cached sweep,
		silently loads and shows it - no button click needed. If there's no cache, or the setting is
		off, this just leaves the intro page showing, exactly as before this existed. Never triggers a
		fresh scan (see `dialog.start_cache_check`): a silent auto-action should never be the thing
		that kicks off an expensive whole-binary recompute - that stays behind the explicit Run Sweep
		button.
		"""
		if self.bv is None or not QSettings().value(dialog.AUTO_RELOAD_SETTING_KEY, True, type=bool):
			return

		bv = self.bv  # captured: compare against this, not self.bv, when the check finishes

		def on_complete(data):
			self._sweep_task = None
			if data is None or bv != self.bv:
				return  # no valid cache, or switched to yet another binary before this finished
			table = dialog.ComplexityTableWidget(bv, data, from_cache=True)
			self.all_functions_stack.addWidget(table)
			self.all_functions_stack.setCurrentWidget(table)
			self._sweep_table = table

		self._sweep_task = dialog.start_cache_check(bv, on_complete)

	def _refresh_current_function_tab(self) -> None:
		# Cancel whatever's still computing for the previous function before starting the next one,
		# rather than just letting it run to completion in the background: leaving it uncancelled
		# is exactly what let repeated navigation (or switching binaries) pile up several of these
		# at once, all competing for the same core-level analysis locks.
		if self._current_func_task is not None:
			self._current_func_task.cancel()
			self._current_func_task = None

		self._func_generation += 1
		generation = self._func_generation
		func = self.current_func

		if func is None:
			self._clear_current_metrics()
			return

		# Install the new one-row identity immediately, with metric cells shown as em dashes until
		# computation completes. The table and row therefore never jump around, and stale numbers
		# from the previously selected function are never displayed under the new function name.
		self._clear_current_metrics()
		if self._sweep_table is not None and self._sweep_table.bv == self.bv:
			cached_values = self._sweep_table.values_for_function(func)
			if cached_values is not None:
				self._apply_current_metrics(func, cached_values)
				return

		def on_complete(values):
			if generation != self._func_generation:
				return  # superseded by a newer navigation before this finished
			self._apply_current_metrics(func, values)

		def start_computation():
			if generation != self._func_generation:
				return  # superseded before the delay below even elapsed - nothing to start
			if self.bv is not None and self.bv.analysis_state != AnalysisState.IdleState:
				# Do not ask for MLIL/HLIL while Binary Ninja is still materializing the function the
				# user just navigated to. Retrying is cheap and prevents this panel from extending the
				# editor's own visible "Loading..." interval by contending for analysis resources.
				QTimer.singleShot(100, start_computation)
				return

			self._current_func_task = dialog.start_function_metrics(func, on_complete)

		# Deliberately delayed rather than started immediately: landing on a function is exactly the
		# moment Binary Ninja's own UI is *also* materializing that function's MLIL/HLIL for the
		# first time, to render the disassembly/graph view you just navigated to. A function's first
		# MLIL access is real, measurably expensive work (building and wrapping it - every access
		# after that first one is essentially free), so starting our own background computation of
		# the same function's metrics at that same instant means two consumers racing to build the
		# same data at once - measured to cost on the order of a few hundred milliseconds right at
		# the point of navigation, which is exactly what read as "lag," and only ever showed up with
		# this panel open because nothing else forces that same redundant access. A short delay lets
		# the UI's own access happen first; by the time this fires, the same data is normally already
		# warm, so what we do is a cheap cache hit instead of a second real computation.
		QTimer.singleShot(200, start_computation)

	def notifyOffsetChanged(self, offset: int) -> None:
		# Only recompute while actually visible - a collapsed/hidden panel shouldn't cost anything
		# on every navigation, and this is exactly what makes that possible: nothing here runs
		# unless someone's looking at it.
		if self.bv is None or not self.isVisible():
			return
		functions = self.bv.get_functions_containing(offset)
		func = functions[0] if functions else None
		if func == self.current_func:
			return
		self.current_func = func
		# Keep the identity current while this tab is hidden, but defer its UI/analysis work until the
		# user actually switches back to Current Function.
		if self.tabs.currentWidget() is self._current_function_tab:
			self._refresh_current_function_tab()

	def notifyViewChanged(self, view_frame) -> None:
		# Binary Ninja calls this on more than just "switched to a different file/tab" - e.g. it
		# also fires on ordinary clicks within the same view. Without this guard, every one of
		# those wiped current_func back to None and forced a fresh computation, which also defeats
		# notifyOffsetChanged's own "same function" check right after (comparing the new function
		# against None never matches) - that combination was the actual cause of the jumpy,
		# constantly-recomputing UI, not just repeated navigation within one function.
		new_bv = None if view_frame is None else view_frame.getCurrentViewInterface().getData()
		if new_bv == self.bv:
			return
		self.bv = new_bv
		self.current_func = None
		if self.tabs.currentWidget() is self._current_function_tab:
			self._refresh_current_function_tab()
		self._reset_all_functions_tab()


class ComplexitySidebarWidgetType(SidebarWidgetType):
	def __init__(self):
		# Sidebar icons are 28x28 points (56x56 pixels for HiDPI); a plain white glyph on a
		# transparent background, made theme-aware automatically by Binary Ninja.
		icon = QImage(56, 56, QImage.Format_RGB32)
		icon.fill(0)
		painter = QPainter()
		painter.begin(icon)
		painter.setFont(QFont('Open Sans', 28))
		painter.setPen(QColor(255, 255, 255, 255))
		painter.drawText(QRectF(0, 0, 56, 56), Qt.AlignCenter, 'Cx')
		painter.end()

		SidebarWidgetType.__init__(self, icon, 'Code Complexity')

	def createWidget(self, frame, data):
		return ComplexitySidebarWidget('Code Complexity', frame, data)

	def defaultLocation(self):
		# Replicates the old GlobalAreaWidget placement (bottom, next to Console/Log) via the
		# current sidebar system - see the module docstring.
		return SidebarWidgetLocation.LeftBottom

	def contextSensitivity(self):
		# A single instance for the whole session, tracking the active view itself via
		# notifyViewChanged/notifyOffsetChanged, rather than one instance per tab/pane.
		return SidebarContextSensitivity.SelfManagedSidebarContext


Sidebar.addSidebarWidgetType(ComplexitySidebarWidgetType())
