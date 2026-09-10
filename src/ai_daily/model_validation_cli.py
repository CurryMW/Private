from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import SecretStr

from ai_daily.config import DEFAULT_AI_BASE_URL, validate_ai_base_url
from ai_daily.model_validation import (
    PRODUCTION_MODEL,
    REQUIRED_VALIDATION_REPETITIONS,
    ManualReview,
    ModelCatalogueError,
    ModelValidationReport,
    finalize_validation,
    run_model_validation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the selected TeamoRouter production model."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser(
        "run", help="Verify the live catalogue and run the fixed sample."
    )
    run_parser.add_argument(
        "--output",
        type=Path,
        default=Path(".state/teamorouter-model-validation.json"),
    )
    run_parser.add_argument(
        "--repetitions",
        type=int,
        choices=(REQUIRED_VALIDATION_REPETITIONS,),
        default=REQUIRED_VALIDATION_REPETITIONS,
    )

    finalize_parser = commands.add_parser(
        "finalize",
        help="Apply the fixed manual rubric to the validation report.",
    )
    finalize_parser.add_argument("--report", type=Path, required=True)
    finalize_parser.add_argument("--review", type=Path, required=True)
    finalize_parser.add_argument(
        "--output",
        type=Path,
        default=Path(".state/teamorouter-model-validation-final.json"),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "finalize":
        return _finalize(args.report, args.review, args.output)

    source = _environment(env)
    raw_api_key = source.get("AI_API_KEY", "").strip()
    if not raw_api_key:
        print("status=blocked reason=AI_API_KEY_missing")
        return 2
    model = source.get("AI_MODEL", "").strip()
    if model != PRODUCTION_MODEL:
        print("status=failed reason=AI_MODEL_not_approved")
        return 1

    try:
        report = asyncio.run(
            _run(
                api_key=SecretStr(raw_api_key),
                base_url=validate_ai_base_url(
                    source.get("AI_BASE_URL", DEFAULT_AI_BASE_URL)
                ),
                model=model,
                repetitions=args.repetitions,
            )
        )
    except (ModelCatalogueError, ValueError):
        print("status=failed reason=model_validation_unavailable")
        return 1

    _write_report(args.output, report.model_dump_json(indent=2))
    if report.validation_status == "failed":
        print(f"status=failed report={args.output}")
        return 1
    print(f"status=pending-manual-review report={args.output}")
    return 0


def _environment(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is not None:
        return env
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    return os.environ


async def _run(
    *,
    api_key: SecretStr,
    base_url: str,
    model: str,
    repetitions: int,
) -> ModelValidationReport:
    async with httpx.AsyncClient(
        headers={"User-Agent": "dingtalk-ai-daily-model-validation/0.1"}
    ) as client:
        return await run_model_validation(
            client,
            api_key=api_key,
            base_url=base_url,
            model=model,
            repetitions=repetitions,
        )


def _write_report(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(f"{payload}\n", encoding="utf-8")
    temporary_path.replace(path)


def _finalize(report_path: Path, review_path: Path, output_path: Path) -> int:
    try:
        report = ModelValidationReport.model_validate_json(
            report_path.read_text(encoding="utf-8")
        )
        review = ManualReview.model_validate_json(
            review_path.read_text(encoding="utf-8")
        )
        finalized = finalize_validation(report, review)
    except (OSError, ValueError):
        print("status=failed reason=manual_review_invalid")
        return 1

    _write_report(output_path, finalized.model_dump_json(indent=2))
    print(
        f"status=verified model={finalized.production_model} "
        f"report={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
