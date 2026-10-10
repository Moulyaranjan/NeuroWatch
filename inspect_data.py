
import json
from pathlib import Path

# NeuroWatch Google Fit export folder
ROOT = (
    Path.home()
    / "NeuroWatch"
    / "data"
    / "raw"
    / "Fit"
)

def inspect_json_file(file_path):
    """Print the structure of a Google Fit JSON file."""
    print("\n" + "=" * 70)
    print("FILE:", file_path.name)
    print("=" * 70)

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        print("JSON type:", type(data).__name__)

        if isinstance(data, dict):
            print("Top-level keys:", list(data.keys()))

            # Google Fit data often stores records in 'point'
            points = data.get("point", [])

            if isinstance(points, list):
                print("Number of data points:", len(points))

                if points:
                    first = points[0]

                    if isinstance(first, dict):
                        print("First point fields:", list(first.keys()))

                        # Show the structure, not personal measurements
                        values = first.get("value", [])
                        if isinstance(values, list) and values:
                            if isinstance(values[0], dict):
                                print(
                                    "Measurement fields:",
                                    list(values[0].keys())
                                )

            # Some exports use different structures
            if "data" in data:
                print(
                    "'data' field type:",
                    type(data["data"]).__name__
                )

        elif isinstance(data, list):
            print("Number of items:", len(data))

            if data and isinstance(data[0], dict):
                print("First item fields:", list(data[0].keys()))

        else:
            print("Data structure:", type(data).__name__)

    except json.JSONDecodeError:
        print("Could not read: invalid JSON format.")
    except Exception as error:
        print("Could not inspect:", type(error).__name__, str(error))


def main():
    print("=" * 70)
    print("NEUROWATCH - GOOGLE FIT DATA INSPECTOR")
    print("=" * 70)
    print("Searching folder:")
    print(ROOT)

    if not ROOT.exists():
        print("\nERROR: Fit folder not found.")
        print("Check that the export is inside:")
        print(Path.home() / "NeuroWatch" / "data" / "raw")
        return

    # Find every JSON file inside all nested folders
    json_files = sorted(ROOT.rglob("*.json"))

    print("\nJSON files found:", len(json_files))

    if not json_files:
        print("No JSON files found. Check the extracted folder.")
        return

    for file_path in json_files:
        inspect_json_file(file_path)

    print("\n" + "=" * 70)
    print("INSPECTION COMPLETE")
    print("No source files were modified.")
    print("=" * 70)


if __name__ == "__main__":
    main()
