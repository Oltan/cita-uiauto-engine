# uiauto/cli.py
from __future__ import annotations

import argparse
import json
import os
import sys

from .repository import Repository
from .runner import Runner

from .inspector import (
    inspect_window,
    write_inspect_outputs,
    emit_elements_yaml_stateful,
)

# Import recorder conditionally to avoid hard dependency on optional packages
try:
    from .recorder import record_session
    RECORDER_AVAILABLE = True
except ImportError:
    RECORDER_AVAILABLE = False
    record_session = None


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    p = argparse.ArgumentParser(prog="uiauto", description="Generic Windows UI automation runner (pywinauto).")
    sub = p.add_subparsers(dest="cmd", required=True)

    # -------------------------
    # run
    # -------------------------
    runp = sub.add_parser("run", help="Run a YAML scenario using an elements.yaml object map")
    runp.add_argument("--elements", required=True, help="Path to elements.yaml (object map)")
    runp.add_argument("--scenario", required=True, help="Path to scenario.yaml")
    runp.add_argument("--schema", default=os.path.join(os.path.dirname(__file__), "schemas", "scenario.schema.json"), help="Path to scenario schema JSON")
    runp.add_argument("--app", default=None, help="Optional app path to start (can also use open_app step)")
    runp.add_argument("--vars", default=None, help="Optional vars JSON file")
    runp.add_argument("--report", default="report.json", help="Report output path (JSON)")

    # -------------------------
    # inspect
    # -------------------------
    insp = sub.add_parser("inspect", help="Inspect Desktop UIA and dump control candidates (JSON/TXT)")
    insp.add_argument("--window-title-re", default=None, help="Optional: filter visible windows by title regex (best-effort)")
    insp.add_argument("--out", default="reports", help="Output directory for inspect reports")
    insp.add_argument("--query", default=None, help="Filter controls by contains; use 'regex:<pattern>' for regex search")
    insp.add_argument("--max-controls", type=int, default=3000, help="Max number of descendants to scan")
    insp.add_argument("--include-invisible", action="store_true", help="Include invisible controls")
    insp.add_argument("--exclude-disabled", action="store_true", help="Exclude disabled controls")
    insp.add_argument("--emit-elements-yaml", default=None, help="Optional: write generated elements.yaml to this path")
    insp.add_argument("--emit-window-name", default="main", help="Window name used in generated elements.yaml (default: main)")
    insp.add_argument("--state", default="default", help="UI state name (default: 'default')")
    insp.add_argument("--merge", action="store_true", help="Merge with existing elements.yaml")

    # -------------------------
    # record
    # -------------------------
    recp = sub.add_parser("record", help="Record user interactions into semantic YAML steps")
    recp.add_argument("--elements", required=True, help="Path to elements.yaml (will be updated with new elements)")
    recp.add_argument("--scenario-out", required=True, help="Output path for recorded scenario YAML")
    recp.add_argument("--window-title-re", default=None, help="Filter recording to window matching title regex")
    recp.add_argument("--window-name", default="main", help="Window name for element specs (default: main)")
    recp.add_argument("--state", default="default", help="UI state name for recorded elements (default: 'default')")
    recp.add_argument("--debug-json-out", default=None, help="Optional: save debug snapshots to this JSON file")

    args = p.parse_args(argv)

    if args.cmd == "run":
        repo = Repository(args.elements)
        runner = Runner(repo, schema_path=args.schema)

        variables = {}
        if args.vars:
            with open(args.vars, "r", encoding="utf-8") as f:
                variables = json.load(f)
            if not isinstance(variables, dict):
                raise ValueError("--vars must be a JSON object mapping")

        report = runner.run(
            scenario_path=args.scenario,
            app_path=args.app,
            variables=variables,
            report_path=args.report,
        )

        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("status") == "passed" else 2

    if args.cmd == "inspect":
        result = inspect_window(
            backend="uia",
            window_title_re=args.window_title_re,
            max_controls=int(args.max_controls),
            query=args.query,
            include_invisible=bool(args.include_invisible),
            include_disabled=not bool(args.exclude_disabled),
        )

        paths = write_inspect_outputs(result, out_dir=args.out)

        if args.emit_elements_yaml:
            out_yaml = emit_elements_yaml_stateful(
                result,
                out_path=args.emit_elements_yaml,
                window_name=args.emit_window_name,
                state=args.state,
                merge=args.merge,
            )
            paths["elements_yaml"] = out_yaml

        print(json.dumps({"status": "ok", "outputs": paths, "controls": len(result.get("controls", []))}, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "record":
        if not RECORDER_AVAILABLE:
            print("ERROR: Recording requires additional dependencies.", file=sys.stderr)
            print("Install with: pip install pynput comtypes", file=sys.stderr)
            return 1
        
        recorder = record_session(
            elements_yaml=args.elements,
            scenario_out=args.scenario_out,
            window_title_re=args.window_title_re,
            window_name=args.window_name,
            state=args.state,
            debug_json_out=args.debug_json_out,
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())