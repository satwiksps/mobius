"""Mobius UI Automation Python client."""

from __future__ import annotations
import uiautomation as auto
from typing import Any, Optional

class MobiusUIClient:
    """Wrapper around uiautomation for Windows."""

    def __init__(self):
        auto.SetGlobalSearchTimeout(1.0)

    def list_windows(self) -> list[dict]:
        """List all visible windows."""
        windows = []
        for win in auto.GetRootControl().GetChildren():
            windows.append({
                "id": str(win.Handle),
                "title": win.Name,
                "pid": win.ProcessId
            })
        return windows

    def get_tree_hwnd(self, hwnd: int) -> dict:
        """Get the UI element tree by window handle."""
        win = auto.ControlFromHandle(hwnd)
        if not win:
            return {}
        # Stub implementation for tree extraction
        return {"id": str(win.Handle), "name": win.Name, "children": []}

    def click(self, element_id: str) -> dict:
        """Click an element."""
        # Stub
        return {"success": True}

    def send_keys(self, element_id: str, keys: str) -> dict:
        """Send keyboard input to an element."""
        return {"success": True}

class UIClientError(Exception):
    """Raised when the UI API returns an error."""
