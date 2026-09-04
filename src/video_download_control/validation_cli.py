from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .build_identity import (
    ProductBuildDriftError,
    ProductBuildUnavailableError,
    current_product_identity,
)
from .validation import (
    ValidationManifestError,
    evaluate_capabilities,
    load_validation_manifest,
    load_validation_results,
    render_capability_report,
)


class _ValidationArgumentError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _ValidationArgumentError("invalid_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Validate Stage 0 sample evidence without exposing raw URLs"
    )
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--print-product-identity",
        action="store_true",
        help="print the version plus exact package build digest as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.print_product_identity:
            if (
                args.manifest is not None
                or args.results is not None
                or args.output is not None
            ):
                raise _ValidationArgumentError("invalid_arguments")
            print(
                json.dumps(
                    {
                        "product_identity": current_product_identity(),
                        "product_version": __version__,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.manifest is None:
            raise _ValidationArgumentError("invalid_arguments")
        samples = load_validation_manifest(args.manifest)
        results = load_validation_results(args.results) if args.results else ()
        evidence = evaluate_capabilities(samples, results)
        report = render_capability_report(samples, evidence)
        if args.output:
            args.output.write_text(report, encoding="utf-8", newline="\n")
        else:
            print(report, end="")
    except _ValidationArgumentError:
        print(
            json.dumps(
                {"status": "error", "error_code": "invalid_arguments"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except ProductBuildDriftError:
        print(
            json.dumps(
                {"status": "error", "error_code": "product_build_drift"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 6
    except ProductBuildUnavailableError:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "product_build_unavailable",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 6
    except ValidationManifestError:
        print(
            json.dumps(
                {"status": "error", "error_code": "invalid_validation_input"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except OSError:
        print(
            json.dumps(
                {"status": "error", "error_code": "validation_io_unavailable"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 6
    except Exception:  # noqa: BLE001 - sanitize the final process boundary
        print(
            json.dumps(
                {"status": "error", "error_code": "internal_error"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
