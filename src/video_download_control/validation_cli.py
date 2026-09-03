from __future__ import annotations

import argparse
from pathlib import Path

from .validation import (
    evaluate_capabilities,
    load_validation_manifest,
    load_validation_results,
    render_capability_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Stage 0 sample evidence without exposing raw URLs"
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    samples = load_validation_manifest(args.manifest)
    results = load_validation_results(args.results) if args.results else ()
    evidence = evaluate_capabilities(samples, results)
    report = render_capability_report(samples, evidence)
    if args.output:
        args.output.write_text(report, encoding="utf-8", newline="\n")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
