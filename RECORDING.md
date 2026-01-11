# Recording User Interactions

The `uiauto record` command allows you to record user interactions with a Windows application and automatically generate semantic YAML scenarios compatible with the existing `uiauto run` framework.

## Features

- **Semantic Recording**: Captures clicks, typing, and hotkeys as high-level semantic steps
- **No Coordinates**: All actions are mapped to elements using UIA locators (no raw mouse coordinates)
- **QtQuick Compatible**: Prefers `name`/`name_re` locators based on `Accessible.name`
- **Keystroke Grouping**: Consecutive typing on the same element is merged into a single `type` step
- **Incremental Elements.yaml**: Automatically updates your object map with newly discovered elements
- **Safe Merging**: Respects existing element definitions and state-based variants

## Installation

The recorder requires two additional dependencies:

```bash
pip install pynput comtypes
```

## Usage

### Basic Recording

```bash
uiauto record \
  --elements object-maps/elements.yaml \
  --scenario-out scenarios/recorded.yaml \
  --window-title-re "MyApp.*"
```

This will:
1. Start recording interactions with any window matching "MyApp.*"
2. Capture all clicks, typing, and hotkeys
3. On Ctrl+C, save the recorded scenario to `scenarios/recorded.yaml`
4. Update `object-maps/elements.yaml` with any new elements

### Full Options

```bash
uiauto record \
  --elements <path>              # Path to elements.yaml (required)
  --scenario-out <path>          # Output scenario YAML (required)
  --window-title-re <regex>      # Filter to specific window (optional)
  --window-name <name>           # Window name in elements.yaml (default: "main")
  --state <state>                # UI state for recorded elements (default: "default")
  --debug-json-out <path>        # Save debug snapshots (optional)
```

## Recording Workflow

1. **Start the recorder**:
   ```bash
   uiauto record --elements elements.yaml --scenario-out recorded.yaml
   ```

2. **Interact with your application**:
   - Click on buttons, fields, etc. → Generates `click` steps
   - Type into text fields → Generates `type` steps (grouped automatically)
   - Press hotkeys (Ctrl+L, etc.) → Generates `hotkey` steps

3. **Stop recording**: 
   - Press `Ctrl+Shift+F12` (works without terminal focus)
   - Or press `Ctrl+C` in the terminal (requires terminal to be focused)

4. **Review outputs**:
   - `scenarios/recorded.yaml` - Generated scenario with semantic steps
   - `object-maps/elements.yaml` - Updated with new elements

## Generated Steps

### Click Steps
```yaml
- click:
    element: loginbutton
```

### Type Steps
```yaml
- type:
    element: usernamefield
    text: "AutomationTest"
```

### Hotkey Steps
```yaml
- hotkey:
    keys: "^l"  # Ctrl+L
```

## Element Locator Strategy

The recorder generates element definitions using inspector logic:

1. **Preferred**: `name` (Accessible.name) - Best for QtQuick
2. **Fallback**: `auto_id` (WPF/WinForms automation ID)
3. **Fallback**: `title` (window text)
4. **Fallback**: `class_name` + `control_type`

Example generated element:
```yaml
loginbutton:
  window: main
  when:
    state: default
  locators:
  - name: loginButton
    control_type: Button
  - name_re: (?i)loginButton
    control_type: Button
  - class_name: Button_QMLTYPE_4
    control_type: Button
```

## State Management

Use the `--state` option to record elements in different UI states:

```bash
# Record in "login" state
uiauto record --elements elements.yaml --scenario-out login.yaml --state login

# Record in "main" state
uiauto record --elements elements.yaml --scenario-out main.yaml --state main
```

Elements with the same base name but different states will be automatically suffixed:
- `taskinput` (state: default)
- `taskinput__login` (state: login)

## Limitations

- **Windows Only**: Requires Windows with UIA support
- **Focus-Based**: Only captures elements that receive keyboard focus
- **Best-Effort**: May miss very transient UI elements
- **No Wait Steps**: Wait steps must be added manually if needed
- **No Validation**: Recorded steps are actions only, no assertions

## Tips

1. **Window Filtering**: Always use `--window-title-re` to avoid capturing interactions with other apps
2. **Slow Down**: Perform actions deliberately with small pauses between steps
3. **Stop Recording**: Use `Ctrl+Shift+F12` to stop without switching to terminal, or `Ctrl+C` in the terminal
4. **Review Output**: Always review and edit the recorded scenario before using it
5. **Add Waits**: Insert `wait` steps manually for elements that load asynchronously
6. **Test Playback**: Run the recorded scenario to verify it works correctly

## Debugging

Enable debug mode to capture detailed element snapshots:

```bash
uiauto record \
  --elements elements.yaml \
  --scenario-out recorded.yaml \
  --debug-json-out debug_snapshots.json
```

This creates a JSON file with all captured element information for troubleshooting.

## Example Session

```bash
$ uiauto record --elements object-maps/elements.yaml --scenario-out scenarios/test.yaml --window-title-re "MyApp"

🎬 Recording started. Interact with the application.
   Press Ctrl+Shift+F12 to stop recording (or Ctrl+C in console).

  Press Ctrl+Shift+F12 to stop recording (or Ctrl+C in console)...

  🖱️  Click: loginbutton
  ⌨️  Type: usernamefield = 'TestUser'
  🖱️  Click: submitbutton
  ⌨️  Hotkey: ^l

  🛑 Stop hotkey detected (Ctrl+Shift+F12)
⏹️  Stopping recording...
✅ Recording stopped. Captured 4 steps.
📝 Scenario saved to: scenarios/test.yaml
🗺️  Elements saved to: object-maps/elements.yaml
    Added/updated 3 elements
```

## Integration with Existing Scenarios

Recorded scenarios can be:
- Used as-is with `uiauto run`
- Edited to add variables, waits, or assertions
- Combined with manually written steps
- Used as templates for parameterized tests

Example editing recorded scenario:
```yaml
vars:
  username: TestUser

steps:
  # Add explicit wait before recorded click
  - wait:
      element: loginbutton
      state: visible
  
  # Recorded click (unmodified)
  - click:
      element: loginbutton
  
  # Edit recorded type to use variable
  - type:
      element: usernamefield
      text: ${username}
```
