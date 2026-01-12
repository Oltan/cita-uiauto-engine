#!/usr/bin/env python3
"""
Qt Accessibility Diagnostic Tool

This script helps diagnose why recorder cannot identify elements in Qt applications.
Run this while your Qt app is running to check if elements are accessible via UIA.

Usage:
    python test_qt_accessibility.py --window-title "MyQtApp"
"""

import sys
import argparse
from pywinauto import Desktop
from pywinauto.controls.uiawrapper import UIAWrapper

try:
    import comtypes
    from comtypes.gen import UIAutomationClient as UIA
    UIA_AVAILABLE = True
except ImportError:
    UIA_AVAILABLE = False
    print("⚠️  WARNING: comtypes or UIAutomationClient not available")
    print("   Install with: pip install comtypes")


def test_window_access(window_title_pattern: str):
    """Test if we can access the Qt window via UIA."""
    print(f"\n🔍 Searching for windows matching: '{window_title_pattern}'")
    print("=" * 70)

    desktop = Desktop(backend="uia")

    # Find all windows
    windows = desktop.windows()
    print(f"\n📊 Found {len(windows)} total windows on desktop")

    matching_windows = []
    for w in windows:
        try:
            title = w.window_text()
            if window_title_pattern.lower() in title.lower():
                matching_windows.append(w)
                print(f"  ✅ Match: '{title}'")
        except Exception:
            pass

    if not matching_windows:
        print(f"\n❌ No windows found matching '{window_title_pattern}'")
        print("\n💡 Tips:")
        print("   1. Make sure your Qt app is running")
        print("   2. Check the exact window title")
        print("   3. Try a partial match (e.g., just 'Qt' or 'App')")
        return None

    # Use first matching window
    target_window = matching_windows[0]
    print(f"\n✅ Using window: '{target_window.window_text()}'")
    return target_window


def test_element_access(window):
    """Test if we can access elements within the window."""
    print(f"\n🔍 Testing element accessibility...")
    print("=" * 70)

    try:
        descendants = window.descendants()
        print(f"✅ Found {len(descendants)} descendant elements")

        if len(descendants) == 0:
            print("\n❌ PROBLEM: No descendants found!")
            print("   This means Qt accessibility is NOT working.")
            print("\n💡 Solution:")
            print("   Set environment variable before running your Qt app:")
            print("   > set QT_ACCESSIBILITY=1")
            print("   > YourQtApp.exe")
            return False

        # Analyze elements
        print("\n📊 Element Statistics:")
        elements_with_name = 0
        elements_with_auto_id = 0
        control_types = {}

        for elem in descendants[:100]:  # Check first 100
            try:
                wrapper = elem

                # Get element_info (UIA properties)
                if hasattr(wrapper, 'element_info'):
                    info = wrapper.element_info

                    # Check for name (Accessible.name)
                    if hasattr(info, 'name') and info.name:
                        elements_with_name += 1

                    # Check for auto_id
                    if hasattr(info, 'automation_id') and info.automation_id:
                        elements_with_auto_id += 1

                    # Count control types
                    if hasattr(info, 'control_type'):
                        ct = info.control_type
                        control_types[ct] = control_types.get(ct, 0) + 1
            except Exception:
                pass

        print(f"  Elements with Accessible.name: {elements_with_name}")
        print(f"  Elements with automation_id: {elements_with_auto_id}")
        print(f"\n  Control types found:")
        for ct, count in sorted(control_types.items(), key=lambda x: -x[1])[:10]:
            print(f"    - {ct}: {count}")

        if elements_with_name == 0:
            print("\n⚠️  WARNING: No elements have Accessible.name set!")
            print("   This means your QML components need accessibility properties.")
            print("\n💡 Solution - Add to your QML components:")
            print("""
    Button {
        text: "Login"
        Accessible.name: "loginButton"
        Accessible.role: Accessible.Button
    }

    TextField {
        placeholderText: "Username"
        Accessible.name: "usernameField"
        Accessible.role: Accessible.EditableText
    }
            """)
            return False

        return True

    except Exception as e:
        print(f"\n❌ Error accessing elements: {e}")
        return False


def test_element_from_point():
    """Test if ElementFromPoint API is available."""
    print(f"\n🔍 Testing ElementFromPoint API (used for recording clicks)...")
    print("=" * 70)

    if not UIA_AVAILABLE:
        print("❌ comtypes not available - cannot test ElementFromPoint")
        return False

    try:
        from ctypes import wintypes
        import ctypes

        # Initialize COM
        comtypes.CoInitialize()

        # Create UIA client
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=UIA.IUIAutomation
        )

        # Test with screen center
        user32 = ctypes.windll.user32
        screen_width = user32.GetSystemMetrics(0)
        screen_height = user32.GetSystemMetrics(1)
        center_x = screen_width // 2
        center_y = screen_height // 2

        point = wintypes.POINT(center_x, center_y)
        element = uia.ElementFromPoint(point)

        if element:
            print(f"✅ ElementFromPoint API is working!")
            print(f"   Test coordinates: ({center_x}, {center_y})")
            try:
                name = element.CurrentName
                print(f"   Element at screen center: {name or '(unnamed)'}")
            except Exception:
                pass
            return True
        else:
            print(f"⚠️  ElementFromPoint returned None")
            return False

    except Exception as e:
        print(f"❌ ElementFromPoint test failed: {e}")
        return False
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def show_clickable_elements(window):
    """Show all clickable elements with their names."""
    print(f"\n🔍 Clickable Elements (Buttons, etc.)...")
    print("=" * 70)

    try:
        descendants = window.descendants()

        clickable_types = {'Button', 'CheckBox', 'RadioButton', 'MenuItem', 'ListItem'}
        clickable_elements = []

        for elem in descendants:
            try:
                if hasattr(elem, 'element_info'):
                    info = elem.element_info
                    control_type = getattr(info, 'control_type', None)

                    if control_type in clickable_types:
                        name = getattr(info, 'name', None)
                        class_name = getattr(info, 'class_name', None)
                        clickable_elements.append({
                            'type': control_type,
                            'name': name or '(no name)',
                            'class': class_name or '(no class)'
                        })
            except Exception:
                pass

        if clickable_elements:
            print(f"Found {len(clickable_elements)} clickable elements:\n")
            for i, elem in enumerate(clickable_elements[:20], 1):  # Show first 20
                print(f"  {i}. {elem['type']:<15} name='{elem['name']}'")
                if elem['name'] == '(no name)':
                    print(f"     └─ ⚠️  No Accessible.name! (class: {elem['class']})")
        else:
            print("❌ No clickable elements found")

    except Exception as e:
        print(f"❌ Error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Qt Accessibility Diagnostic Tool")
    parser.add_argument("--window-title", required=True, help="Window title or partial match")
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("Qt ACCESSIBILITY DIAGNOSTIC TOOL")
    print("=" * 70)

    # Test 1: Window access
    window = test_window_access(args.window_title)
    if not window:
        print("\n❌ Cannot proceed - window not found")
        return 1

    # Test 2: Element access
    if not test_element_access(window):
        print("\n❌ Element accessibility check failed")
        print("   Recording will NOT work until this is fixed!")
        return 1

    # Test 3: ElementFromPoint
    if not test_element_from_point():
        print("\n⚠️  ElementFromPoint API issue - recording may fail")

    # Test 4: Show clickable elements
    show_clickable_elements(window)

    print("\n" + "=" * 70)
    print("✅ DIAGNOSTIC COMPLETE")
    print("=" * 70)
    print("\n💡 Next Steps:")
    print("   1. If elements have Accessible.name → Try recording again!")
    print("   2. If no Accessible.name → Add them to your QML components")
    print("   3. If no elements found → Enable QT_ACCESSIBILITY=1")

    return 0


if __name__ == "__main__":
    sys.exit(main())
