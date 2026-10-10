
import json
from pathlib import Path

ROOT = (
    Path.home()
    / "NeuroWatch"
    / "data"
    / "raw"
    / "Fit"
    / "All data"
)

KEYWORDS = ("heart_rate.bpm", "step_count.delta")


def describe(obj, depth=0):
    """Show structure and field names, not measurement values."""
    indent = "  " * depth

    if isinstance(obj, dict):
        print(indent + "Object keys:", list(obj.keys())[:30])

        for key, value in list(obj.items())[:10]:
            if isinstance(value, (dict, list)):
                print(indent + f"Field: {key}")
                describe(value, depth + 1)

    elif isinstance(obj, list):
        print(indent + f"List length: {len(obj)}")

        if obj:
            describe(obj[0], depth + 1)

    else:
        print(indent + "Type:", type(obj).__name__)


def main():
    print("Searching:", ROOT)

    if not ROOT.exists():
        print("ERROR: All data folder not found.")
        return

    files = [
        p for p in ROOT.glob("*.json")
        if any(word in p.name.lower() for word in KEYWORDS)
    ]

    print("Matching files:", len(files))

    for path in files:
        print("\n" + "=" * 60)
        print("FILE:", path.name)
        print("=" * 60)

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            describe(data)

        except Exception as exc:
            print("Could not read file:", type(exc).__name__)


if __name__ == "__main__":
    main()
