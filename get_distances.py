import argparse
import json
from pathlib import Path

from data import load_overlay_model, relationships_to_rows


def main():
    parser = argparse.ArgumentParser(description="Compute PTGui control-point pair summaries.")
    parser.add_argument("project_file", help="Path to a PTGui project file (.pts / .ptgui)")
    args = parser.parse_args()

    model = load_overlay_model(args.project_file)
    rows = relationships_to_rows(model.pairs)
    print(json.dumps(rows, indent=2))

    output_path = Path(args.project_file).with_suffix(".distances.json")
    output_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"wrote={output_path}")


if __name__ == "__main__":
    main()
