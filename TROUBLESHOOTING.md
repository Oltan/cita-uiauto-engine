# Troubleshooting Recorder Issues

## Problem: "Click: Could not identify element"

When running `uiauto record`, you see:
```
⚠️  Click: Could not identify element
⚠️  Click: Could not identify element
```

This means the recorder cannot capture UI elements at click positions. This is usually a Qt accessibility configuration issue.

---

## Quick Diagnostic

Run the diagnostic tool first to identify the exact problem:

```bash
python dev/test_qt_accessibility.py --window-title "YourAppName"
```

This will check:
- ✅ If your Qt app window is accessible
- ✅ If UIA can see elements inside your app
- ✅ If elements have Accessible.name properties
- ✅ If ElementFromPoint API works (used for recording)

---

## Solution 1: Enable Qt Accessibility

### The Problem
Qt applications **do not expose UI elements to Windows UIA by default**. You must explicitly enable accessibility.

### The Fix

**Option A: Environment Variable (Easiest)**
```bash
# On Windows Command Prompt:
set QT_ACCESSIBILITY=1
YourQtApp.exe

# On PowerShell:
$env:QT_ACCESSIBILITY = "1"
.\YourQtApp.exe
```

**Option B: System-Wide Setting**
1. Right-click "This PC" → Properties → Advanced System Settings
2. Click "Environment Variables"
3. Under "User variables", click "New"
4. Variable name: `QT_ACCESSIBILITY`
5. Variable value: `1`
6. Click OK and restart your app

**Option C: In Your Qt Application Code**
```cpp
// Add to main.cpp BEFORE creating QApplication:
#include <QApplication>

int main(int argc, char *argv[])
{
    QCoreApplication::setAttribute(Qt::AA_UseAccessibility, true);
    QApplication app(argc, argv);
    // ... rest of code
}
```

### Verification
After enabling, run:
```bash
python dev/test_qt_accessibility.py --window-title "YourAppName"
```

You should see: `✅ Found X descendant elements` (where X > 0)

---

## Solution 2: Add Accessible.name to QML Components

### The Problem
Even with accessibility enabled, if your QML components don't have `Accessible.name` properties, the recorder cannot identify them semantically.

### Check If This Is Your Issue
Run diagnostic:
```bash
python dev/test_qt_accessibility.py --window-title "YourAppName"
```

If you see:
```
⚠️  WARNING: No elements have Accessible.name set!
Elements with Accessible.name: 0
```

Then you need to add accessibility properties to your QML.

### The Fix

Add `Accessible.name` to ALL interactive QML components:

**Buttons:**
```qml
Button {
    id: loginButton
    text: "Login"

    // ✅ ADD THIS:
    Accessible.name: "loginButton"
    Accessible.role: Accessible.Button
    Accessible.description: "Login to application"
}
```

**Text Fields:**
```qml
TextField {
    id: usernameField
    placeholderText: "Username"

    // ✅ ADD THIS:
    Accessible.name: "usernameField"
    Accessible.role: Accessible.EditableText
    Accessible.description: "Enter your username"
}
```

**Other Controls:**
```qml
// CheckBox
CheckBox {
    Accessible.name: "rememberMeCheckbox"
    Accessible.role: Accessible.CheckBox
}

// ComboBox
ComboBox {
    Accessible.name: "languageSelector"
    Accessible.role: Accessible.ComboBox
}

// ListView items
ListView {
    Accessible.name: "taskList"
    Accessible.role: Accessible.List

    delegate: Rectangle {
        // For dynamic items, use dynamic names:
        Accessible.name: "taskItem_" + index
        Accessible.role: Accessible.ListItem
    }
}
```

### Naming Best Practices

1. **Use camelCase**: `loginButton`, `usernameField`
2. **Be descriptive**: `submitFormButton` not just `button1`
3. **Unique names**: Each element needs a unique name
4. **Dynamic elements**: Use index/id: `taskItem_0`, `taskItem_1`

---

## Solution 3: Check Window Title Filter

### The Problem
If your `--window-title-re` doesn't match your app window, recording won't work.

### Check Window Title
```bash
# List all windows:
python -c "from pywinauto import Desktop; [print(w.window_text()) for w in Desktop(backend='uia').windows()]"
```

Find your app in the list and note the **exact title**.

### Fix Recording Command
```bash
# If your window title is "MyApp v1.0"
uiauto record \
  --elements elements.yaml \
  --scenario-out recorded.yaml \
  --window-title-re "MyApp"  # Partial match is OK
```

---

## Solution 4: Check DPI Settings

### The Problem
On high-DPI displays (125%, 150%, 200%), click coordinates may be wrong if DPI awareness isn't enabled.

### The Fix
The recorder already enables DPI awareness automatically, but if it's still not working:

**Option A: Run Python with DPI awareness**
Create a `.manifest` file for Python.exe (advanced)

**Option B: Test with 100% DPI**
1. Right-click desktop → Display Settings
2. Temporarily set Scale to 100%
3. Restart your Qt app
4. Try recording again

---

## Solution 5: Verify Dependencies

### Check Installation
```bash
pip list | grep -E "pywinauto|pynput|comtypes"
```

Should show:
```
comtypes      1.x.x
pynput        1.x.x
pywinauto     0.6.x
```

### Reinstall if Needed
```bash
pip uninstall pywinauto pynput comtypes
pip install pywinauto pynput comtypes
```

---

## Complete Diagnostic Workflow

Follow these steps in order:

### Step 1: Enable Qt Accessibility
```bash
set QT_ACCESSIBILITY=1
YourQtApp.exe
```

### Step 2: Run Diagnostic
```bash
python dev/test_qt_accessibility.py --window-title "YourApp"
```

**Expected Output:**
```
✅ Found 50 descendant elements
✅ Elements with Accessible.name: 20
✅ ElementFromPoint API is working!
```

### Step 3: Add Accessible.name (if needed)
If diagnostic shows `Elements with Accessible.name: 0`:
- Edit your QML files
- Add `Accessible.name` to all interactive components
- Rebuild and restart your app

### Step 4: Test Recording
```bash
uiauto record \
  --elements elements.yaml \
  --scenario-out test.yaml \
  --window-title-re "YourApp" \
  --debug-json-out debug.json
```

Click on elements and check console output:
```
🖱️  Click: loginButton     ← ✅ SUCCESS!
```

### Step 5: Verify with Inspector
```bash
uiauto inspect \
  --window-title-re "YourApp" \
  --emit-elements-yaml elements.yaml
```

Check `elements.yaml` to see if elements have proper locators.

---

## Still Not Working?

### Enable Debug Mode
```bash
uiauto record \
  --elements elements.yaml \
  --scenario-out test.yaml \
  --window-title-re "YourApp" \
  --debug-json-out debug.json  # Enable debug output
```

This creates `debug.json` with detailed element capture information.

### Check Debug Output
Look in `debug.json` for entries like:
```json
{
  "event": "click",
  "coordinates": [500, 300],
  "element_captured": {...},  // Should have element details
  "error": "..."  // If null, capture worked
}
```

### Common Issues in Debug Output

**Issue: `"element_captured": null`**
- ElementFromPoint found nothing at those coordinates
- Likely: Qt accessibility not enabled

**Issue: `"element_captured": {..., "name": null, "automation_id": null}`**
- Element found but has no identifying properties
- Fix: Add `Accessible.name` to QML component

**Issue: `"window_match": false`**
- Clicked element is in a different window
- Fix: Adjust `--window-title-re` filter

---

## Example: Complete Qt 5.12 Setup

### 1. Qt Application (main.cpp)
```cpp
#include <QApplication>
#include "mainwindow.h"

int main(int argc, char *argv[])
{
    // Enable accessibility
    QCoreApplication::setAttribute(Qt::AA_UseAccessibility, true);

    QApplication app(argc, argv);
    MainWindow w;
    w.show();
    return app.exec();
}
```

### 2. QML Components (Login.qml)
```qml
import QtQuick 2.12
import QtQuick.Controls 2.12

Page {
    Accessible.name: "loginPage"
    Accessible.role: Accessible.Pane

    Column {
        TextField {
            id: usernameField
            placeholderText: "Username"
            Accessible.name: "usernameField"
            Accessible.role: Accessible.EditableText
        }

        TextField {
            id: passwordField
            placeholderText: "Password"
            echoMode: TextInput.Password
            Accessible.name: "passwordField"
            Accessible.role: Accessible.EditableText
        }

        Button {
            text: "Login"
            Accessible.name: "loginButton"
            Accessible.role: Accessible.Button
            onClicked: { /* login logic */ }
        }
    }
}
```

### 3. Launch with Accessibility
```batch
@echo off
set QT_ACCESSIBILITY=1
MyQtApp.exe
```

### 4. Record Actions
```bash
uiauto record --elements elements.yaml --scenario-out login.yaml --window-title-re "MyQtApp"
```

### 5. Playback
```bash
uiauto run --elements elements.yaml --scenario login.yaml
```

---

## Getting Help

If you're still stuck, gather this information:

1. **Diagnostic output:**
   ```bash
   python dev/test_qt_accessibility.py --window-title "YourApp" > diagnostic.txt
   ```

2. **Inspector output:**
   ```bash
   uiauto inspect --window-title-re "YourApp" --out reports
   ```

3. **Qt version:**
   ```bash
   # Check your Qt version
   qmake --version
   ```

4. **Recording with debug:**
   ```bash
   uiauto record --elements elements.yaml --scenario-out test.yaml --debug-json-out debug.json
   ```

Share these files when asking for help!

---

## Summary Checklist

Before recording:
- [ ] Qt app is running
- [ ] `QT_ACCESSIBILITY=1` is set
- [ ] QML components have `Accessible.name` properties
- [ ] Diagnostic tool shows elements are accessible
- [ ] Window title filter matches your app
- [ ] Dependencies are installed (pywinauto, pynput, comtypes)

If all checked, recording should work! 🎉
