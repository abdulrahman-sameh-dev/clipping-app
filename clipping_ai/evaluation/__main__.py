from __future__ import annotations

import argparse
from pathlib import Path

from clipping_ai.evaluation.runner import (
    DEFAULT_OUTPUT_DIR,
    run_evaluation,
    write_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic offline highlight evaluation.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    json_path, markdown_path = write_reports(run_evaluation(), args.output_dir)
    print(f"Wrote {json_path}")
    print(f"Wrote {markdown_path}")


if __name__ == "__main__":
    main()
