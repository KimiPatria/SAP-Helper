"""Entry point:  python -m scripts.eval [options]

    --repeats N     runs per case (default 3). More repeats = tighter
                    flakiness measurement, linearly more Bedrock calls.
    --workers N     concurrent runs (default 4).
    --only X        restrict to cases whose name contains X, or to a whole
                    stratum ("adversarial").
    --baseline      write data/eval_baseline.json for later comparison.
    --compare       diff this run against the stored baseline.
    --dry-run       exercise the harness with a scripted fake model. No AWS
                    calls, no cost. Use it to check the scorers behave before
                    spending a real run.
    --json PATH     write the full report (including every answer) to PATH.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval.fake import FakeProvider          # noqa: E402
from scripts.eval.runner import aggregate, render, run_all  # noqa: E402

BASELINE_PATH = ROOT / "data" / "eval_baseline.json"


def build_provider(dry_run: bool):
    if dry_run:
        return FakeProvider()
    from app.config import settings
    from app.generation.bedrock import BedrockProvider

    return BedrockProvider(
        aws_region=settings.aws_region,
        model_id=settings.bedrock_model_id,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def compare_to_baseline(report: dict) -> str:
    if not BASELINE_PATH.exists():
        return ("No baseline stored yet. Run with --baseline once you are happy "
                "with a result, then later runs can be diffed against it.")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    lines = ["", "=" * 74, "COMPARISON TO BASELINE", "=" * 74,
             f"baseline recorded: {baseline.get('recorded_at', 'unknown')}", ""]

    def delta(label: str, now: float, before: float, pct: bool = False) -> str:
        change = now - before
        arrow = "+" if change > 0 else ""
        fmt = (lambda v: f"{v:.1%}") if pct else (lambda v: f"{v:.3f}")
        marker = ""
        if change <= -0.05:
            marker = "   <-- REGRESSION"
        elif change >= 0.05:
            marker = "   <-- improved"
        return f"  {label:<24}{fmt(before):>9} -> {fmt(now):>9}  ({arrow}{fmt(change)}){marker}"

    lines.append(delta("overall score", report["overall_score"], baseline["overall_score"]))
    lines.append(delta("overall pass rate", report["overall_pass_rate"],
                       baseline["overall_pass_rate"], pct=True))
    lines.append("")
    for stratum, data in report["strata"].items():
        before = baseline.get("strata", {}).get(stratum)
        if before:
            lines.append(delta(f"{stratum} pass rate", data["pass_rate"],
                               before["pass_rate"], pct=True))
    lines.append("")
    for dimension, data in report["dimensions"].items():
        before = baseline.get("dimensions", {}).get(dimension, {}).get("mean")
        if before is not None and data["mean"] is not None:
            lines.append(delta(dimension, data["mean"], before))

    now_failing = {r["case"] for r in report["case_results"] if r["pass_rate"] < 1.0}
    was_failing = {r["case"] for r in baseline.get("case_results", []) if r["pass_rate"] < 1.0}
    newly_broken = sorted(now_failing - was_failing)
    newly_fixed = sorted(was_failing - now_failing)
    if newly_broken:
        lines += ["", "NEWLY FAILING:"] + [f"  - {c}" for c in newly_broken]
    if newly_fixed:
        lines += ["", "NEWLY PASSING:"] + [f"  - {c}" for c in newly_fixed]
    lines.append("=" * 74)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.eval")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--only", default="")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", dest="json_path", default="")
    args = parser.parse_args()

    provider = build_provider(args.dry_run)
    model = getattr(provider, "model", "fake")
    if args.dry_run:
        print("DRY RUN - scripted fake model, no Bedrock calls, results are not "
              "evidence about Nova.\n")

    print(f"Running {args.repeats} repeat(s) per case against {model} ...")
    results = run_all(provider, repeats=args.repeats, workers=args.workers,
                      only=args.only)
    report = aggregate(results, args.repeats)
    print()
    print(render(report, model))

    if args.compare:
        print(compare_to_baseline(report))

    if args.json_path:
        detail = dict(report)
        detail["runs"] = [
            {"case": r.case, "repeat": r.repeat, "answer": r.answer,
             "calls": r.calls, "score": r.score, "latency_ms": r.latency_ms,
             "error": r.error}
            for r in results
        ]
        Path(args.json_path).write_text(json.dumps(detail, indent=2), encoding="utf-8")
        print(f"\nFull report written to {args.json_path}")

    if args.baseline:
        from datetime import datetime, timezone

        stored = dict(report)
        stored["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stored["model"] = model
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        print(f"\nBaseline written to {BASELINE_PATH}")

    # Non-zero exit so this can gate a change. BOTH conditions must hold: the
    # weighted average clears the threshold AND no single dimension collapsed.
    # Averaging alone would pass an agent that routes flawlessly and fabricates
    # every figure.
    ok = (
        report["overall_score"] >= report["threshold"]
        and not report["breached_floors"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
