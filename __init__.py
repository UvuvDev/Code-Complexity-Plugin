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
Code Complexity
================

Computes and displays code complexity metrics for functions, as a docked sidebar panel (next to
Console/Log at the bottom-left) rather than a popup: a "Current Function" tab that live-updates as
you navigate, and an opt-in "All Functions" tab for a whole-binary sweep with a sortable,
filterable table.

The metrics themselves live in `complexity.py` (self-contained, no dependency on anything beyond
the standard public Binary Ninja API - safe to import headlessly). `dialog.py` (the table/model
and background scan logic) and `sidebar.py` (the actual panel) both need `binaryninjaui`/PySide6,
so they're only imported when a UI is actually running: a sidebar widget type has to register
itself as soon as the plugin loads (there's no user action to hang a lazy import off, unlike a
PluginCommand), so this file checks `core_ui_enabled()` itself instead.
"""

import binaryninja

if binaryninja.core_ui_enabled():
	from . import sidebar  # noqa: F401 - importing this registers the sidebar widget type.
