#!/usr/bin/env python3
"""Print an example review comment without a model request or a checkout."""

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from final_comment import render, render_github


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "example",
        nargs="?",
        type=Path,
        help="JSON object containing render arguments; defaults to a built-in example",
    )
    parser.add_argument(
        "--github", action="store_true", help="preview the GitHub comment"
    )
    args = parser.parse_args()
    example = args.example or Path(__file__).parent / "examples" / (
        "github.json" if args.github else "mixed.json"
    )
    renderer = render_github if args.github else render
    try:
        print(renderer(**json.loads(example.read_text())))
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
