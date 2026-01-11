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
        self._stop_event = threading.Event()
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._mouse_listener: Optional[mouse.Listener] = None
        
        # Modifier keys state
        self._ctrl_pressed = False
        self._alt_pressed = False
        self._shift_pressed = False
        self._win_pressed = False
        
        # Stop hotkey state (Ctrl+Shift+F12)
        self._stop_hotkey_pressed = False
        
        self._desktop = Desktop(backend=self.backend)

    def start(self) -> None:
        """Start recording user interactions."""
        print("🎬 Recording started. Interact with the application.")
        print("   Press Ctrl+Shift+F12 to stop recording (or Ctrl+C in console).")
        self._recording = True
        self._stop_event.clear()
        
        # Start input listeners
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
        self._recording = False
        self._stop_event.set()
        
        # Flush any pending typing
        self._flush_typing()
        
        # Stop listeners
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()
        
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
    # Event Handlers
    # =========================================================

    def _on_mouse_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        """Handle mouse click events."""
        if not self._recording or not pressed:
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
        if not self._recording:
            return
        
        try:
            # Track modifier keys FIRST (before stop check)
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                self._ctrl_pressed = True
            elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                self._alt_pressed = True
            elif key == keyboard.Key.shift or key == keyboard.Key.shift_r:
                self._shift_pressed = True
            elif key == keyboard.Key.cmd or key == keyboard.Key.cmd_r:
                self._win_pressed = True
            
            # HIGHEST PRIORITY: Check for stop hotkey (Ctrl+Shift+F12)
            # This must be checked BEFORE any other hotkey processing to prevent emission
            if self._ctrl_pressed and self._shift_pressed:
                try:
                    if hasattr(key, 'name') and key.name == 'f12':
                        self._stop_hotkey_pressed = True
                        print("\n  🛑 Stop hotkey detected (Ctrl+Shift+F12)")
                        # Stop recording in a separate thread to avoid blocking
                        threading.Thread(target=self.stop, daemon=True).start()
                        # CRITICAL: Return immediately to suppress this hotkey from being recorded
                        return
                except Exception:
                    pass
            
            # Check for other hotkeys (modifier + regular key)
            # Only process if not a modifier key itself
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
        
        This is more reliable than focus-based capture for click events,
        as not all clickable elements receive keyboard focus.
        
        Args:
            x: Screen X coordinate
            y: Screen Y coordinate
        
        Returns:
            Element info dict or None if capture failed.
        """
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
                
                if uia_mod:
                    try:
                        # Create UIA automation instance
                        uia = comtypes.client.CreateObject(
                            uia_mod.CUIAutomation._reg_clsid_,
                            interface=uia_mod.IUIAutomation
                        )
                        
                        # Create POINT structure
                        import ctypes
                        class POINT(ctypes.Structure):
                            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                        
                        point = POINT(x, y)
                        
                        # Get element at point
                        element_at_point = uia.ElementFromPoint(point)
                        
                        if element_at_point:
                            # Wrap in pywinauto wrapper
                            from pywinauto.controls.uiawrapper import UIAWrapper
                            wrapped_element = UIAWrapper(element_at_point)
                            
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
                                    if not re.search(self.window_title_re, window_title or ""):
                                        # Not in target window, ignore
                                        return None
                                except Exception:
                                    pass
                            
                            # Extract control info using inspector logic
                            info = extract_control_info(wrapped_element)
                            
                            # Store debug snapshot if enabled
                            if self.debug_json_out:
                                self.debug_snapshots.append({
                                    "timestamp": time.time(),
                                    "type": "click",
                                    "coordinates": {"x": x, "y": y},
                                    "element_info": info,
                                })
                            
                            return info
                    except Exception as e:
                        # ElementFromPoint failed, try fallback
                        if self.debug_json_out:
                            print(f"  Debug: ElementFromPoint failed: {type(e).__name__}: {e}")
            
            # Fallback: Use focused element capture
            # This is less accurate but better than nothing
            return self._capture_focused_element()
            
        except Exception as e:
            if self.debug_json_out:
                print(f"  Debug: Failed to capture element at point: {type(e).__name__}: {e}")
            return None

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
                key_name = key.name.lower()
                return key_name in ("ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", 
                                   "shift", "shift_r", "cmd", "cmd_r")
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
            if key_str in ("ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", 
                          "shift", "shift_r", "cmd", "cmd_r"):
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
