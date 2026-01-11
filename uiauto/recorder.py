# uiauto/recorder.py
"""
Recording module for capturing user interactions into semantic YAML steps.

Strategy:
---------
Since Windows UIA doesn't provide native event recording APIs, we use a hybrid approach:

1. Use pynput to detect when mouse clicks and keyboard events occur (global hooks)
2. Poll the UIA focused element when an event is detected
3. Use inspector logic to extract element info and generate locator candidates
4. Translate interactions into semantic steps (click/type/hotkey)
5. Maintain elements.yaml incrementally with safe merging

The approach uses pynput for timing/event detection, then queries UIA for the actual element.
This works because:
- Click events: We can capture the focused element immediately after a click
- Type events: We track the focused element when typing occurs
- Hotkey events: Detected by modifier + key combinations

Tradeoffs:
----------
- Polling-based: Small lag between action and capture (acceptable for recording)
- Best-effort element identification: May miss very transient elements
- QtQuick-friendly: Prefers name/name_re locators (Accessible.name)
- No raw coordinates: All actions mapped to semantic elements
- Keystroke grouping: Consecutive typing on same element merged into single step
- Focus-based: Only captures elements that receive focus (suitable for interactive recording)

Dependencies:
-------------
- pynput: For input event detection
- pywinauto: For UIA element inspection
- comtypes (via pywinauto): For UIA automation element access
- inspector: For element info extraction and locator generation
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import yaml

# Optional imports for recording - fail gracefully if not available
try:
    from pynput import keyboard, mouse
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    keyboard = None
    mouse = None

try:
    import comtypes
    import comtypes.client
    # Pre-import UIA type library if available (comtypes generates it on first use)
    try:
        from comtypes.gen import UIAutomationClient as UIA
        UIA_AVAILABLE = True
    except ImportError:
        # Type library not yet generated - will be created on first use
        UIA_AVAILABLE = False
        UIA = None
    COMTYPES_AVAILABLE = True
except ImportError:
    COMTYPES_AVAILABLE = False
    UIA_AVAILABLE = False
    comtypes = None
    UIA = None

from pywinauto import Desktop

from .inspector import (
    extract_control_info,
    _normalize_key,
    _make_locator_candidates,
)


# Constants
MODIFIER_KEY_NAMES = frozenset([
    "ctrl", "ctrl_l", "ctrl_r",
    "alt", "alt_l", "alt_r",
    "shift", "shift_r",
    "cmd", "cmd_r"
])

# Windows POINT structure for ElementFromPoint
try:
    import ctypes
    from ctypes import wintypes
    
    # Use wintypes.POINT directly for COM compatibility with UIA
    # This is the standard Windows POINT structure that ElementFromPoint expects
    POINT = wintypes.POINT
    POINT_AVAILABLE = True
    
    # Windows API functions for hotkey registration and DPI awareness
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        
        # RegisterHotKey/UnregisterHotKey for native stop hotkey
        RegisterHotKey = user32.RegisterHotKey
        RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        RegisterHotKey.restype = wintypes.BOOL
        
        UnregisterHotKey = user32.UnregisterHotKey
        UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        UnregisterHotKey.restype = wintypes.BOOL
        
        # CreateWindowEx for message-only window
        CreateWindowEx = user32.CreateWindowExW
        CreateWindowEx.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID
        ]
        CreateWindowEx.restype = wintypes.HWND
        
        DestroyWindow = user32.DestroyWindow
        DestroyWindow.argtypes = [wintypes.HWND]
        DestroyWindow.restype = wintypes.BOOL
        
        GetModuleHandle = kernel32.GetModuleHandleW
        GetModuleHandle.argtypes = [wintypes.LPCWSTR]
        GetModuleHandle.restype = wintypes.HMODULE
        
        # PeekMessage for non-blocking message loop
        PeekMessage = user32.PeekMessageW
        PeekMessage.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        PeekMessage.restype = wintypes.BOOL
        
        # TranslateMessage and DispatchMessage for message processing
        TranslateMessage = user32.TranslateMessage
        TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        TranslateMessage.restype = wintypes.BOOL
        
        DispatchMessage = user32.DispatchMessageW
        DispatchMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        DispatchMessage.restype = wintypes.LPARAM
        
        PostQuitMessage = user32.PostQuitMessage
        PostQuitMessage.argtypes = [ctypes.c_int]
        PostQuitMessage.restype = None
        
        # DPI awareness
        SetProcessDPIAware = user32.SetProcessDPIAware
        SetProcessDPIAware.argtypes = []
        SetProcessDPIAware.restype = wintypes.BOOL
        
        WINDOWS_API_AVAILABLE = True
    except (AttributeError, OSError):
        WINDOWS_API_AVAILABLE = False
        RegisterHotKey = None
        UnregisterHotKey = None
        CreateWindowEx = None
        DestroyWindow = None
        GetModuleHandle = None
        PeekMessage = None
        TranslateMessage = None
        DispatchMessage = None
        PostQuitMessage = None
        SetProcessDPIAware = None
    
except ImportError:
    POINT = None
    POINT_AVAILABLE = False
    WINDOWS_API_AVAILABLE = False
    RegisterHotKey = None
    UnregisterHotKey = None
    CreateWindowEx = None
    DestroyWindow = None
    GetModuleHandle = None
    PeekMessage = None
    TranslateMessage = None
    DispatchMessage = None
    PostQuitMessage = None
    SetProcessDPIAware = None

# Constants for PeekMessage
PM_REMOVE = 0x0001

# Constants for CreateWindowEx
HWND_MESSAGE = -3  # Message-only window

# Hotkey constants
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
VK_F12 = 0x7B

# Recorder constants
STOP_HOTKEY_MODIFIERS = MOD_CONTROL | MOD_SHIFT  # Ctrl+Shift
STOP_HOTKEY_VK = VK_F12  # F12 key
MAX_PARENT_WALK_DEPTH = 5  # Maximum levels to walk up parent chain when refining elements


class Recorder:
    """
    Records user interactions and emits semantic scenario YAML + updated elements.yaml.
    
    Usage:
        recorder = Recorder(
            elements_yaml_path="object-maps/elements.yaml",
            window_title_re="MyApp.*",
            state="default"
        )
        recorder.start()
        # User interacts with app
        recorder.stop()
        recorder.save_scenario("scenarios/recorded.yaml")
    """

    def __init__(
        self,
        elements_yaml_path: str,
        scenario_out_path: Optional[str] = None,
        window_title_re: Optional[str] = None,
        window_name: str = "main",
        state: str = "default",
        debug_json_out: Optional[str] = None,
        backend: str = "uia",
    ):
        if not PYNPUT_AVAILABLE:
            raise ImportError(
                "pynput is required for recording but not installed.\n"
                "Install with: pip install pynput"
            )
        
        if not COMTYPES_AVAILABLE:
            raise ImportError(
                "comtypes is required for recording but not installed.\n"
                "Install with: pip install comtypes"
            )
        
        self.elements_yaml_path = os.path.abspath(elements_yaml_path)
        self.scenario_out_path = scenario_out_path
        self.window_title_re = window_title_re
        self.window_name = window_name
        self.state = state
        self.debug_json_out = debug_json_out
        self.backend = backend

        self.steps: List[Dict[str, Any]] = []
        self.elements_cache: Dict[str, Dict[str, Any]] = {}  # key -> element spec
        self.debug_snapshots: List[Dict[str, Any]] = []
        
        # Typing state tracking
        self._typing_element_key: Optional[str] = None
        self._typing_buffer: List[str] = []
        self._last_action_time = 0.0
        self._typing_timeout = 2.0  # seconds of inactivity to flush typing
        self._typing_lock = threading.Lock()  # Protect typing state from race conditions
        
        # Control flags
        self._recording = False
        self._stopping = False  # Flag to prevent recording during stop sequence
        self._stop_requested = False  # Flag set by stop hotkey or Ctrl+C (suppresses all further recording)
        self._stop_event = threading.Event()
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._mouse_listener: Optional[mouse.Listener] = None
        self._hotkey_thread: Optional[threading.Thread] = None
        
        # Modifier keys state
        self._ctrl_pressed = False
        self._alt_pressed = False
        self._shift_pressed = False
        self._win_pressed = False
        
        # Stop hotkey state
        self._stop_hotkey_pressed = False
        self._stop_hotkey_id = 1  # ID for RegisterHotKey
        
        self._desktop = Desktop(backend=self.backend)

    def start(self) -> None:
        """Start recording user interactions."""
        # Enable DPI awareness for accurate click coordinate mapping
        if WINDOWS_API_AVAILABLE and SetProcessDPIAware:
            try:
                SetProcessDPIAware()
                if self.debug_json_out:
                    print("  Debug: DPI awareness enabled")
            except Exception as e:
                if self.debug_json_out:
                    print(f"  Debug: Failed to enable DPI awareness: {e}")
        
        print("🎬 Recording started. Interact with the application.")
        print("   Press Ctrl+Shift+F12 to stop recording (or Ctrl+C in console).")
        self._recording = True
        self._stopping = False
        self._stop_event.clear()
        
        # Register native Windows hotkey (Ctrl+Shift+F12)
        if WINDOWS_API_AVAILABLE and RegisterHotKey:
            self._hotkey_thread = threading.Thread(target=self._hotkey_listener_thread, daemon=True)
            self._hotkey_thread.start()
        
        # Start input listeners (pynput for clicks and typing, not for stop hotkey)
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._mouse_listener = mouse.Listener(
            on_click=self._on_mouse_click,
        )
        
        self._keyboard_listener.start()
        self._mouse_listener.start()
        
        # Start flush checker thread (for typing timeout)
        self._flush_thread = threading.Thread(target=self._flush_checker, daemon=True)
        self._flush_thread.start()

    def stop(self) -> None:
        """Stop recording."""
        if not self._recording:
            return
        
        print("⏹️  Stopping recording...")
        self._stopping = True  # Set flag to prevent new events from being recorded
        self._recording = False
        self._stop_event.set()
        
        # Flush any pending typing
        self._flush_typing()
        
        # Stop listeners
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()
        
        # Unregister hotkey and stop hotkey thread
        if WINDOWS_API_AVAILABLE and UnregisterHotKey:
            try:
                UnregisterHotKey(None, self._stop_hotkey_id)
            except Exception as e:
                # Cleanup failure is acceptable during shutdown
                if self.debug_json_out:
                    print(f"  Debug: Failed to unregister hotkey: {e}")
            # Post quit message to exit GetMessage loop
            if PostQuitMessage:
                try:
                    PostQuitMessage(0)
                except Exception as e:
                    # Cleanup failure is acceptable during shutdown
                    if self.debug_json_out:
                        print(f"  Debug: Failed to post quit message: {e}")
        
        print(f"✅ Recording stopped. Captured {len(self.steps)} steps.")

    def save_scenario(self, out_path: Optional[str] = None) -> str:
        """Save recorded steps to scenario YAML."""
        out_path = out_path or self.scenario_out_path
        if not out_path:
            raise ValueError("No scenario output path specified")
        
        out_path = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        
        scenario = {"steps": self.steps}
        
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(scenario, f, sort_keys=False, allow_unicode=True)
        
        print(f"📝 Scenario saved to: {out_path}")
        return out_path

    def save_elements(self) -> str:
        """Save/merge elements to elements.yaml."""
        # Load existing elements.yaml if it exists
        if os.path.exists(self.elements_yaml_path):
            with open(self.elements_yaml_path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        else:
            existing = {}
        
        app_block = existing.get("app", {})
        app_block.setdefault("backend", self.backend)
        
        windows_block = existing.get("windows", {})
        if self.window_name not in windows_block:
            # Use window_title_re if provided, otherwise require user to set it manually
            title_re = self.window_title_re if self.window_title_re else ".*"
            windows_block[self.window_name] = {
                "locators": [{"title_re": title_re}]
            }
        
        elements_block = existing.get("elements", {})
        
        # Merge new elements from cache
        for key, spec in self.elements_cache.items():
            if key not in elements_block:
                elements_block[key] = spec
            else:
                # Element exists - check if state differs
                existing_state = elements_block[key].get("when", {}).get("state")
                if existing_state != self.state:
                    # Create state-specific variant
                    new_key = f"{key}__{self.state}"
                    elements_block[new_key] = spec
                else:
                    # Same state - keep existing (don't overwrite user customizations)
                    pass
        
        final_doc = {
            "app": app_block,
            "windows": windows_block,
            "elements": elements_block,
        }
        
        os.makedirs(os.path.dirname(self.elements_yaml_path) or ".", exist_ok=True)
        with open(self.elements_yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(final_doc, f, sort_keys=False, allow_unicode=True)
        
        print(f"🗺️  Elements saved to: {self.elements_yaml_path}")
        print(f"    Added/updated {len(self.elements_cache)} elements")
        return self.elements_yaml_path

    def save_debug_snapshots(self) -> Optional[str]:
        """Save debug JSON snapshots if enabled."""
        if not self.debug_json_out or not self.debug_snapshots:
            return None
        
        out_path = os.path.abspath(self.debug_json_out)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(self.debug_snapshots, f, indent=2, ensure_ascii=False)
        
        print(f"🐛 Debug snapshots saved to: {out_path}")
        return out_path

    # =========================================================
    # Native Hotkey Listener
    # =========================================================

    def _hotkey_listener_thread(self) -> None:
        """
        Dedicated thread for listening to native Windows hotkey (Ctrl+Shift+F12).
        
        Uses RegisterHotKey with a message-only window and PeekMessage to reliably
        capture the stop hotkey without blocking, preventing starvation of the hotkey event.
        
        Message-only window ensures hotkey delivery on all Windows configurations.
        """
        if not WINDOWS_API_AVAILABLE or not RegisterHotKey or not PeekMessage:
            return
        
        hwnd = None
        try:
            # Create a message-only window for reliable hotkey delivery
            # Some systems don't deliver hotkeys registered with NULL HWND
            if CreateWindowEx and GetModuleHandle:
                try:
                    hinstance = GetModuleHandle(None)
                    hwnd = CreateWindowEx(
                        0,                  # dwExStyle
                        "Message",          # lpClassName (built-in class)
                        "UIAutoRecorder",   # lpWindowName
                        0,                  # dwStyle
                        0, 0, 0, 0,        # position and size (ignored for message-only)
                        HWND_MESSAGE,      # hWndParent (HWND_MESSAGE = message-only window)
                        None,              # hMenu
                        hinstance,         # hInstance
                        None               # lpParam
                    )
                    if hwnd and self.debug_json_out:
                        print(f"  Debug: Created message-only window (HWND: {hwnd})")
                except Exception as e:
                    if self.debug_json_out:
                        print(f"  Debug: Failed to create message window: {e}, using NULL")
                    hwnd = None
            
            # Register Ctrl+Shift+F12 as a global hotkey
            # Use hwnd if available, otherwise NULL (less reliable on some systems)
            success = RegisterHotKey(hwnd, self._stop_hotkey_id, STOP_HOTKEY_MODIFIERS, STOP_HOTKEY_VK)
            
            if not success:
                if self.debug_json_out:
                    print("  Debug: Failed to register stop hotkey")
                return
            
            if self.debug_json_out:
                print("  Debug: Native stop hotkey registered (Ctrl+Shift+F12)")
            
            # Non-blocking message loop using PeekMessage
            msg = wintypes.MSG()
            while self._recording and not self._stop_event.is_set():
                # Use PeekMessage for non-blocking check
                # PM_REMOVE removes the message from queue if available
                if PeekMessage(ctypes.byref(msg), hwnd, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY and msg.wParam == self._stop_hotkey_id:
                        # Stop hotkey pressed!
                        self._stop_hotkey_pressed = True
                        print("\n  🛑 Stop hotkey detected (Ctrl+Shift+F12)")
                        
                        # Immediately set flags to suppress all further recording
                        self._stop_requested = True
                        self._stopping = True
                        
                        # Stop recording
                        self.stop()
                        break
                    else:
                        # Process other messages
                        TranslateMessage(ctypes.byref(msg))
                        DispatchMessage(ctypes.byref(msg))
                else:
                    # No message available, sleep briefly to avoid busy loop
                    time.sleep(0.01)  # 10ms sleep as specified
        
        except Exception as e:
            if self.debug_json_out:
                print(f"  Debug: Hotkey listener error: {e}")
        
        finally:
            # Clean up hotkey registration
            if WINDOWS_API_AVAILABLE and UnregisterHotKey:
                try:
                    UnregisterHotKey(hwnd, self._stop_hotkey_id)
                except Exception as e:
                    # Cleanup failure is acceptable during shutdown
                    if self.debug_json_out:
                        print(f"  Debug: Failed to unregister hotkey: {e}")
            
            # Clean up message window
            if hwnd and DestroyWindow:
                try:
                    DestroyWindow(hwnd)
                    if self.debug_json_out:
                        print(f"  Debug: Destroyed message window")
                except Exception as e:
                    if self.debug_json_out:
                        print(f"  Debug: Failed to destroy window: {e}")

    # =========================================================
    # Event Handlers
    # =========================================================

    def _on_mouse_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        """Handle mouse click events."""
        # Safety guard: don't record if stopping
        if not self._recording or not pressed or self._stopping:
            return
        
        # Flush any pending typing before processing click
        self._flush_typing()
        
        try:
            # Small delay to let UI settle after click
            time.sleep(0.05)
            
            # Capture element at click position (more reliable than focus for clicks)
            element_info = None
            for attempt in range(3):
                element_info = self._capture_element_at_point(x, y)
                if element_info:
                    break
                time.sleep(0.05)  # Wait a bit before retry
            
            if not element_info:
                print(f"  ⚠️  Click: Could not identify element")
                return
            
            # Generate element key and ensure it's in elements cache
            elem_key = self._ensure_element(element_info)
            
            # Emit click step
            self.steps.append({"click": {"element": elem_key}})
            self._last_action_time = time.time()
            
            print(f"  🖱️  Click: {elem_key}")
            
        except Exception as e:
            print(f"  ⚠️  Failed to capture click: {e}")

    def _on_key_press(self, key) -> None:
        """Handle key press events."""
        # HIGHEST PRIORITY: Check stop_requested flag first
        # If stop was requested (by native hotkey or Ctrl+C), suppress ALL key processing
        if self._stop_requested:
            return
        
        # Safety guard: don't record if stopping
        if not self._recording or self._stopping:
            return
        
        try:
            # Track modifier keys for hotkey detection
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                self._ctrl_pressed = True
            elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                self._alt_pressed = True
            elif key == keyboard.Key.shift or key == keyboard.Key.shift_r:
                self._shift_pressed = True
            elif key == keyboard.Key.cmd or key == keyboard.Key.cmd_r:
                self._win_pressed = True
            
            # SUPPRESS stop hotkey detection via pynput
            # Stop hotkey is ONLY handled by native Windows API
            # If Ctrl+Shift is pressed, check if it's F12 and suppress it
            if self._ctrl_pressed and self._shift_pressed:
                try:
                    # Check if this is F12 (or END on some keyboards)
                    key_name = getattr(key, 'name', '').lower() if hasattr(key, 'name') else ''
                    if key_name in ('f12', 'end'):
                        # This is the stop hotkey - suppress it completely
                        # Native hotkey thread will handle it
                        return
                except Exception:
                    pass
            
            # Note: Stop hotkey (Ctrl+Shift+F12) is handled by native Windows API
            # in _hotkey_listener_thread, NOT here. This prevents keyboard layout
            # issues and ensures reliable stop detection.
            
            # Check for other hotkeys (modifier + regular key)
            # Only process if not a modifier key itself and not stopping
            if (self._ctrl_pressed or self._alt_pressed or self._win_pressed) and not self._is_modifier_key(key):
                hotkey_str = self._format_hotkey(key)
                if hotkey_str:
                    # Flush any pending typing
                    self._flush_typing()
                    
                    # Emit hotkey step
                    self.steps.append({"hotkey": {"keys": hotkey_str}})
                    self._last_action_time = time.time()
                    
                    print(f"  ⌨️  Hotkey: {hotkey_str}")
                    return
            
            # Handle regular character input (typing)
            # Only type if no modifiers are pressed (except Shift for capitals)
            char = self._get_char(key)
            if char and not (self._ctrl_pressed or self._alt_pressed or self._win_pressed):
                self._handle_typing(char)
        
        except Exception as e:
            print(f"  ⚠️  Failed to capture key press: {e}")

    def _on_key_release(self, key) -> None:
        """Handle key release events (for modifier tracking)."""
        try:
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                self._ctrl_pressed = False
            elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                self._alt_pressed = False
            elif key == keyboard.Key.shift or key == keyboard.Key.shift_r:
                self._shift_pressed = False
            elif key == keyboard.Key.cmd or key == keyboard.Key.cmd_r:
                self._win_pressed = False
        except Exception:
            pass

    # =========================================================
    # Typing Handling
    # =========================================================

    def _handle_typing(self, char: str) -> None:
        """Handle character typing (buffer for grouping)."""
        try:
            # Capture focused element
            element_info = self._capture_focused_element()
            if not element_info:
                return
            
            # Generate element key
            elem_key = self._ensure_element(element_info)
            
            # Thread-safe access to typing state
            with self._typing_lock:
                # If typing on a different element, flush previous buffer
                if self._typing_element_key and self._typing_element_key != elem_key:
                    self._flush_typing_unsafe()  # Already have lock
                
                # Append to buffer
                self._typing_element_key = elem_key
                self._typing_buffer.append(char)
                self._last_action_time = time.time()
            
        except Exception as e:
            print(f"  ⚠️  Failed to capture typing: {e}")

    def _flush_typing(self) -> None:
        """Flush accumulated typing buffer into a single type step (thread-safe)."""
        with self._typing_lock:
            self._flush_typing_unsafe()
    
    def _flush_typing_unsafe(self) -> None:
        """Flush typing buffer without acquiring lock (must be called with lock held)."""
        if not self._typing_buffer or not self._typing_element_key:
            return
        
        text = "".join(self._typing_buffer)
        self.steps.append({
            "type": {
                "element": self._typing_element_key,
                "text": text
            }
        })
        
        print(f"  ⌨️  Type: {self._typing_element_key} = '{text}'")
        
        # Reset typing state
        self._typing_buffer.clear()
        self._typing_element_key = None

    def _flush_checker(self) -> None:
        """Background thread to flush typing after inactivity timeout."""
        while not self._stop_event.is_set():
            time.sleep(0.5)
            
            # Thread-safe check
            with self._typing_lock:
                if (self._typing_buffer and 
                    time.time() - self._last_action_time > self._typing_timeout):
                    self._flush_typing_unsafe()  # Already have lock

    # =========================================================
    # Element Capture & Management
    # =========================================================

    def _capture_focused_element(self) -> Optional[Dict[str, Any]]:
        """
        Capture the currently focused UIA element.
        
        Returns element info dict or None if capture failed.
        
        Uses UIA's GetFocusedElement to get the actual focused control,
        which is more reliable than trying to guess from mouse position.
        """
        try:
            # Use pywinauto's Desktop to get focused element
            # This internally uses UIA's GetFocusedElement()
            desktop = Desktop(backend=self.backend)
            
            # Get the focused element - pywinauto returns the wrapper
            # We need to be careful here as GetFocusedElement may return
            # the root window, not the specific control
            try:
                # Try to get focused element via UIA
                if COMTYPES_AVAILABLE:
                    # Import or use cached UIA module
                    if UIA_AVAILABLE and UIA is not None:
                        uia_mod = UIA
                    else:
                        # Try to import dynamically (will trigger type library generation)
                        try:
                            from comtypes.gen import UIAutomationClient as uia_mod
                        except ImportError:
                            # Type library generation failed, fall back to window()
                            uia_mod = None
                    
                    if uia_mod:
                        # Create UIA automation instance
                        uia = comtypes.client.CreateObject(
                            uia_mod.CUIAutomation._reg_clsid_,
                            interface=uia_mod.IUIAutomation
                        )
                        
                        # Get focused element
                        focused_elem = uia.GetFocusedElement()
                        if focused_elem:
                            # Wrap in pywinauto wrapper for easier manipulation
                            from pywinauto.controls.uiawrapper import UIAWrapper
                            focused = UIAWrapper(focused_elem)
                        else:
                            focused = desktop.window()
                    else:
                        focused = desktop.window()
                else:
                    focused = desktop.window()
                
            except Exception:
                # Fallback: try to get focused window from desktop
                focused = desktop.window()
                
                # If we can get descendants, try to find focused child
                try:
                    desc = focused.descendants()
                    for d in desc:
                        try:
                            # Check if element has focus via HasKeyboardFocus
                            if hasattr(d.element_info, 'has_keyboard_focus'):
                                if d.element_info.has_keyboard_focus:
                                    focused = d
                                    break
                        except Exception:
                            pass
                except Exception:
                    pass
            
            # Filter by window title if specified
            if self.window_title_re:
                try:
                    # Get the top-level window
                    parent = focused
                    while True:
                        try:
                            p = parent.parent()
                            if p and p.handle != parent.handle:
                                parent = p
                            else:
                                break
                        except Exception:
                            break
                    
                    window_title = parent.window_text()
                    if not re.search(self.window_title_re, window_title or ""):
                        # Not in target window, ignore
                        return None
                except Exception:
                    pass
            
            # Extract control info using inspector logic
            info = extract_control_info(focused)
            
            # Store debug snapshot if enabled
            if self.debug_json_out:
                self.debug_snapshots.append({
                    "timestamp": time.time(),
                    "element_info": info,
                })
            
            return info
            
        except Exception as e:
            # Failed to capture - this is expected for some transient UI states
            # Could be due to element no longer existing, window closed, etc.
            # Log for debugging but return None for graceful degradation
            if self.debug_json_out:
                print(f"  Debug: Failed to capture focused element: {type(e).__name__}: {e}")
            return None

    def _capture_element_at_point(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        """
        Capture the UIA element at the specified screen coordinates.
        
        Walks up the parent chain to find the most meaningful element
        (one with Accessible.name and non-generic control type).
        
        This is more reliable than focus-based capture for click events,
        as not all clickable elements receive keyboard focus.
        
        Args:
            x: Screen X coordinate
            y: Screen Y coordinate
        
        Returns:
            Element info dict or None if capture failed.
        """
        raw_element = None
        refined_element = None
        window_title_match = None
        
        try:
            # Use UIA's ElementFromPoint to get element at coordinates
            if COMTYPES_AVAILABLE:
                # Import or use cached UIA module
                if UIA_AVAILABLE and UIA is not None:
                    uia_mod = UIA
                else:
                    try:
                        from comtypes.gen import UIAutomationClient as uia_mod
                    except ImportError:
                        uia_mod = None
                
                if uia_mod and POINT_AVAILABLE:
                    try:
                        # Create UIA automation instance
                        uia = comtypes.client.CreateObject(
                            uia_mod.CUIAutomation._reg_clsid_,
                            interface=uia_mod.IUIAutomation
                        )
                        
                        # Create point for coordinates
                        point = POINT(x, y)
                        
                        # Get element at point
                        element_at_point = uia.ElementFromPoint(point)
                        
                        if element_at_point:
                            # Wrap in pywinauto wrapper
                            from pywinauto.controls.uiawrapper import UIAWrapper
                            wrapped_element = UIAWrapper(element_at_point)
                            raw_element = wrapped_element
                            
                            # Filter by window title if specified
                            if self.window_title_re:
                                try:
                                    # Get the top-level window
                                    parent = wrapped_element
                                    while True:
                                        try:
                                            p = parent.parent()
                                            if p and p.handle != parent.handle:
                                                parent = p
                                            else:
                                                break
                                        except Exception:
                                            break
                                    
                                    window_title = parent.window_text()
                                    window_title_match = bool(re.search(self.window_title_re, window_title or ""))
                                    if not window_title_match:
                                        # Not in target window, ignore
                                        if self.debug_json_out:
                                            print(f"  Debug: Element not in target window (title: {window_title!r})")
                                        return None
                                except Exception as e:
                                    if self.debug_json_out:
                                        print(f"  Debug: Window title check failed: {e}")
                            
                            # Refine element by walking up parent chain
                            # Look for nearest meaningful element with:
                            # - Non-empty Accessible.name
                            # - Non-generic control type
                            refined_element = self._refine_element(wrapped_element)
                            
                            # Extract control info using inspector logic
                            info = extract_control_info(refined_element)
                            
                            # Store debug snapshot if enabled
                            if self.debug_json_out:
                                raw_info = extract_control_info(raw_element) if raw_element != refined_element else None
                                self.debug_snapshots.append({
                                    "timestamp": time.time(),
                                    "type": "click",
                                    "coordinates": {"x": x, "y": y},
                                    "raw_element_info": raw_info,
                                    "refined_element_info": info,
                                    "window_title_match": window_title_match,
                                })
                            
                            return info
                    except Exception as e:
                        # ElementFromPoint failed
                        if self.debug_json_out:
                            print(f"  Debug: ElementFromPoint failed: {type(e).__name__}: {e}")
            
            # NO FALLBACK to focused element
            # Focus-based capture is unreliable and causes ElementAmbiguousError
            # Better to return None and skip the click than emit wrong/ambiguous steps
            if self.debug_json_out:
                print(f"  Debug: Element capture failed, no fallback (focus-based is unreliable)")
            return None
            
        except Exception as e:
            if self.debug_json_out:
                print(f"  Debug: Failed to capture element at point: {type(e).__name__}: {e}")
            return None

    def _refine_element(self, element) -> Any:
        """
        Walk up the parent chain to find the most meaningful element.
        
        Prefer elements with:
        - Non-empty Accessible.name
        - Non-generic control type (not Pane, Custom, Group)
        
        Args:
            element: PyWinAuto wrapped element
        
        Returns:
            Refined element (may be same as input if already good)
        """
        # Generic control types that we want to skip
        generic_types = {"Pane", "Custom", "Group", "Window"}
        
        current = element
        
        for depth in range(MAX_PARENT_WALK_DEPTH):
            try:
                # Get element info once - be defensive about QtQuick elements
                elem_info = getattr(current, 'element_info', None)
                if not elem_info:
                    # No element_info, try parent
                    try:
                        parent = current.parent()
                        if parent and hasattr(parent, 'handle') and parent.handle != current.handle:
                            current = parent
                            continue
                        else:
                            break
                    except Exception:
                        break
                
                # Safely access control_type and name - QtQuick elements may not expose these
                try:
                    control_type = getattr(elem_info, 'control_type', None)
                except (AttributeError, Exception):
                    control_type = None
                
                try:
                    name = getattr(elem_info, 'name', None)
                except (AttributeError, Exception):
                    name = None
                
                # Check if this is a meaningful element
                if name and control_type and control_type not in generic_types:
                    # Found a good element
                    return current
                
                # Try parent
                try:
                    parent = current.parent()
                    if parent and hasattr(parent, 'handle') and parent.handle != current.handle:
                        current = parent
                    else:
                        break
                except Exception:
                    break
                    
            except Exception as e:
                # If we hit any exception during refinement, log it and try parent
                if self.debug_json_out:
                    print(f"  Debug: Refinement exception at depth {depth}: {type(e).__name__}: {e}")
                try:
                    parent = current.parent()
                    if parent and hasattr(parent, 'handle') and parent.handle != current.handle:
                        current = parent
                    else:
                        break
                except Exception:
                    break
        
        # Return original if no better element found
        return element

    def _ensure_element(self, element_info: Dict[str, Any]) -> str:
        """
        Ensure element is in elements cache. Returns element key.
        
        Uses inspector normalization rules to generate a stable key.
        If element already exists, reuses existing key.
        """
        # Generate candidates
        candidates = element_info.get("locator_candidates", [])
        if not candidates:
            candidates = _make_locator_candidates(element_info)
        
        # Generate base key from element info
        base_raw = (
            element_info.get("name") or 
            element_info.get("auto_id") or 
            element_info.get("control_type") or 
            "element"
        )
        base_key = _normalize_key(base_raw)
        
        # Check if we already have this element
        # Simple heuristic: if locators match, it's the same element
        for existing_key, existing_spec in self.elements_cache.items():
            existing_locs = existing_spec.get("locators", [])
            if existing_locs and candidates and existing_locs[0] == candidates[0]:
                # Same element, reuse key
                return existing_key
        
        # New element - ensure unique key
        elem_key = base_key
        counter = 1
        while elem_key in self.elements_cache:
            elem_key = f"{base_key}_{counter}"
            counter += 1
        
        # Create element spec
        spec = {
            "window": self.window_name,
            "when": {"state": self.state},
            "locators": candidates[:3],  # Top 3 candidates
        }
        
        self.elements_cache[elem_key] = spec
        return elem_key

    # =========================================================
    # Utilities
    # =========================================================

    def _is_modifier_key(self, key) -> bool:
        """Check if a key is a modifier key (Ctrl, Alt, Shift, Win)."""
        try:
            if hasattr(key, 'name'):
                return key.name.lower() in MODIFIER_KEY_NAMES
            return False
        except Exception:
            return False

    def _get_char(self, key) -> Optional[str]:
        """Extract character from key event."""
        try:
            if hasattr(key, "char") and key.char:
                return key.char
            
            # Handle special keys
            if key == keyboard.Key.space:
                return " "
            elif key == keyboard.Key.enter:
                return "\n"
            elif key == keyboard.Key.tab:
                return "\t"
            
            # Ignore other special keys for typing
            return None
            
        except Exception:
            return None

    def _format_hotkey(self, key) -> Optional[str]:
        """
        Format hotkey in pywinauto send_keys format.
        
        Examples:
          Ctrl+L -> "^l"
          Alt+F4 -> "%{F4}"
          Win+R -> "{LWIN}r"
        """
        try:
            # Get key character or name
            if hasattr(key, "char") and key.char:
                key_str = key.char
            elif hasattr(key, "name"):
                key_str = key.name
            else:
                return None
            
            # Don't emit hotkeys for just modifiers
            if key_str.lower() in MODIFIER_KEY_NAMES:
                return None
            
            # Build modifier prefix
            parts = []
            if self._ctrl_pressed:
                parts.append("^")
            if self._alt_pressed:
                parts.append("%")
            if self._shift_pressed:
                parts.append("+")
            if self._win_pressed:
                parts.append("{LWIN}")
            
            # Special key names for pywinauto
            special_keys = {
                "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}",
                "f5": "{F5}", "f6": "{F6}", "f7": "{F7}", "f8": "{F8}",
                "f9": "{F9}", "f10": "{F10}", "f11": "{F11}", "f12": "{F12}",
                "esc": "{ESC}", "escape": "{ESC}",
                "delete": "{DELETE}", "del": "{DELETE}",
                "backspace": "{BACKSPACE}", "back": "{BACKSPACE}",
                "home": "{HOME}", "end": "{END}",
                "page_up": "{PGUP}", "page_down": "{PGDN}",
                "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
            }
            
            key_lower = key_str.lower()
            if key_lower in special_keys:
                key_str = special_keys[key_lower]
            
            return "".join(parts) + key_str
            
        except Exception:
            return None


def record_session(
    elements_yaml: str,
    scenario_out: str,
    window_title_re: Optional[str] = None,
    window_name: str = "main",
    state: str = "default",
    debug_json_out: Optional[str] = None,
) -> Recorder:
    """
    Convenience function to run a recording session.
    
    Returns the Recorder instance for further inspection.
    """
    recorder = Recorder(
        elements_yaml_path=elements_yaml,
        scenario_out_path=scenario_out,
        window_title_re=window_title_re,
        window_name=window_name,
        state=state,
        debug_json_out=debug_json_out,
    )
    
    try:
        recorder.start()
        
        # Wait for user interrupt or stop hotkey
        print("\n  Press Ctrl+Shift+F12 to stop recording (or Ctrl+C in console)...\n")
        while recorder._recording:
            time.sleep(0.5)
        
        # If stopped via hotkey, wait a moment for cleanup
        if recorder._stop_hotkey_pressed:
            time.sleep(0.5)
    
    except KeyboardInterrupt:
        print("\n")
        # Set stop_requested to suppress any pending key events (including Ctrl+C itself)
        recorder._stop_requested = True
        recorder.stop()
    
    finally:
        # Save outputs
        if recorder.steps:
            recorder.save_scenario()
            recorder.save_elements()
            if debug_json_out:
                recorder.save_debug_snapshots()
        else:
            print("⚠️  No steps recorded.")
    
    return recorder
