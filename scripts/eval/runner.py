"""Executes the golden set against the real chat path and scores the results.

WHAT IS REAL HERE
The system prompt, the tool specs, the prompt builder, and Bedrock's Converse
tool-use loop are all production code, imported not copied. Only the tool
RESULTS are stubbed.

ONE DELIBERATE DIFFERENCE FROM PRODUCTION
app/main.py runs `wants_invoice_check()` first: when an attached message reads
like a request to process it, the pipeline is invoked directly instead of
waiting for the model to call the tool. That regex is a safety net added
because Nova was seen narrating invoice outcomes it never computed.

This runner deliberately does NOT apply that net, so the invoice cases measure
the model's own routing. Measuring with the net on would score the regex, and
the net's whole reason for existing is that the model's routing is unreliable -
the number worth having is how unreliable, so the net's value is visible and a
future prompt change can be judged against it.

WHY EVERY CASE RUNS MORE THAN ONCE
Nova is non-deterministic. A single green run is not evidence; the failures
this project has actually hit (a skipped tool call, an answer drifting into
another language) are intermittent by nature and invisible to one-shot manual
testing. Repeats turn them into a rate.
"""

from __future__ import annotations

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.config import settings
from app.generation.prompts import SAP_MCP_SYSTEM_PROMPT, build_mcp_user_prompt
from app.mcp.tools import build_tool_specs

from scripts.eval import fixtures
from scripts.eval.cases import CASES, STRATA, Case
from scripts.eval.scorers import (
    DIMENSION_FLOORS,
    PASS_THRESHOLD,
    WEIGHTS,
    score_run,
)


@dataclass
class RunResult:
    case: str
    complexity: str
    repeat: int
    answer: str
    calls: list[str] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    latency_ms: int = 0
    error: str = ""
    score: dict = field(default_factory=dict)


def _stub_executor(case: Case, calls: list[str], results: list[dict]):
    """Records which tools the model chose and hands back the recorded result.

    Recording happens BEFORE the result is produced, so a call to an unknown
    tool still counts as a routing decision the model made.
    """
    def execute(name: str, arguments: dict) -> dict:
        calls.append(name)
        result = fixtures.result_for(name, case.fixtures)
        results.append(result)
        return result

    return execute


def run_case_once(provider, case: Case, repeat: int) -> RunResult:
    calls: list[str] = []
    tool_results: list[dict] = []

    attachments = []
    if case.attachment:
        attachments.append({
            "attachment_id": f"eval-{case.name}",
            "filename": case.attachment,
            "content_type": "application/pdf",
        })

    user_prompt = build_mcp_user_prompt(
        case.question, [], settings.history_char_budget,
        attachments=attachments, invoice_result=None,
    )
    specs = build_tool_specs(has_attachment=bool(attachments))

    started = time.perf_counter()
    answer, error = "", ""
    try:
        result = provider.generate_with_tools(
            SAP_MCP_SYSTEM_PROMPT, user_prompt, specs,
            settings.generation_max_tokens,
            _stub_executor(case, calls, tool_results),
        )
        answer = result.answer
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.perf_counter() - started) * 1000)

    run = RunResult(
        case=case.name, complexity=case.complexity, repeat=repeat,
        answer=answer, calls=calls, tool_results=tool_results,
        latency_ms=latency_ms, error=error,
    )
    run.score = (
        {"dimensions": {}, "overall": 0.0, "passed": False, "breaches": [error]}
        if error else score_run(case, answer, calls, tool_results)
    )
    return run


def run_all(provider, repeats: int = 3, workers: int = 4,
            only: str = "") -> list[RunResult]:
    cases = [c for c in CASES if not only or only in c.name or only == c.complexity]
    jobs = [(case, r) for case in cases for r in range(repeats)]

    import sys

    interactive = sys.stdout.isatty()
    results: list[RunResult] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_case_once, provider, case, r) for case, r in jobs]
        for future in futures:
            results.append(future.result())
            done += 1
            if interactive:
                print(f"\r  {done}/{len(jobs)} runs complete", end="", flush=True)
            elif done % 10 == 0 or done == len(jobs):
                # Piped/redirected output: periodic lines, not carriage returns,
                # so a captured log stays readable.
                print(f"  {done}/{len(jobs)} runs complete", flush=True)
    if interactive:
        print()
    return results


# ---- aggregation ---------------------------------------------------------


def aggregate(results: list[RunResult], repeats: int) -> dict:
    by_case: dict[str, list[RunResult]] = {}
    for run in results:
        by_case.setdefault(run.case, []).append(run)

    case_rows = []
    for name, runs in by_case.items():
        passes = sum(1 for r in runs if r.score["passed"])
        overalls = [r.score["overall"] for r in runs]
        case_rows.append({
            "case": name,
            "complexity": runs[0].complexity,
            "pass_rate": round(passes / len(runs), 4),
            "mean_score": round(statistics.mean(overalls), 4),
            "flaky": 0 < passes < len(runs),
            "runs": len(runs),
            "mean_latency_ms": int(statistics.mean(r.latency_ms for r in runs)),
            "breaches": sorted({b for r in runs for b in r.score["breaches"]}),
        })
    case_rows.sort(key=lambda r: (r["pass_rate"], r["mean_score"]))

    strata = {}
    for stratum in STRATA:
        rows = [r for r in case_rows if r["complexity"] == stratum]
        if rows:
            strata[stratum] = {
                "cases": len(rows),
                "pass_rate": round(statistics.mean(r["pass_rate"] for r in rows), 4),
                "mean_score": round(statistics.mean(r["mean_score"] for r in rows), 4),
            }

    dimensions = {}
    breached_floors = []
    for dimension in WEIGHTS:
        scored = [
            r.score["dimensions"].get(dimension)
            for r in results
            if r.score.get("dimensions", {}).get(dimension) is not None
        ]
        mean = round(statistics.mean(scored), 4) if scored else None
        floor = DIMENSION_FLOORS[dimension]
        below = mean is not None and mean < floor
        dimensions[dimension] = {
            "mean": mean,
            "floor": floor,
            "below_floor": below,
            "applicable_runs": len(scored),
        }
        if below:
            breached_floors.append(dimension)

    overalls = [r.score["overall"] for r in results]
    return {
        "overall_score": round(statistics.mean(overalls), 4) if overalls else 0.0,
        "overall_pass_rate": round(
            sum(1 for r in results if r.score["passed"]) / len(results), 4
        ) if results else 0.0,
        "threshold": PASS_THRESHOLD,
        "breached_floors": breached_floors,
        "total_runs": len(results),
        "cases": len(by_case),
        "repeats": repeats,
        "strata": strata,
        "dimensions": dimensions,
        "flaky_cases": [r["case"] for r in case_rows if r["flaky"]],
        "case_results": case_rows,
        "mean_latency_ms": int(statistics.mean(r.latency_ms for r in results)) if results else 0,
    }


# ---- reporting -----------------------------------------------------------


def render(report: dict, model: str) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 74)
    add("SAP CHAT AGENT EVALUATION")
    add("=" * 74)
    add(f"model:      {model}")
    add(f"cases:      {report['cases']} x {report['repeats']} repeats "
        f"= {report['total_runs']} runs")
    add(f"threshold:  {report['threshold']}  (per run, weighted across dimensions)")
    add("")
    add(f"OVERALL SCORE     {report['overall_score']:.3f}")
    add(f"OVERALL PASS RATE {report['overall_pass_rate']:.1%}")
    add(f"MEAN LATENCY      {report['mean_latency_ms']} ms")
    add("")

    add("BY COMPLEXITY")
    add(f"  {'stratum':<14}{'cases':>7}{'pass rate':>12}{'mean score':>13}")
    for stratum, data in report["strata"].items():
        add(f"  {stratum:<14}{data['cases']:>7}{data['pass_rate']:>11.1%}"
            f"{data['mean_score']:>13.3f}")
    add("")

    add("BY DIMENSION")
    add(f"  {'dimension':<20}{'weight':>8}{'mean':>9}{'floor':>8}{'runs':>7}")
    for dimension, data in report["dimensions"].items():
        mean = f"{data['mean']:.3f}" if data["mean"] is not None else "n/a"
        flag = "  BELOW FLOOR" if data["below_floor"] else ""
        add(f"  {dimension:<20}{WEIGHTS[dimension]:>8.2f}{mean:>9}"
            f"{data['floor']:>8.2f}{data['applicable_runs']:>7}{flag}")
    if report["breached_floors"]:
        add("")
        add(f"  {len(report['breached_floors'])} dimension(s) below floor: "
            f"{', '.join(report['breached_floors'])}")
        add("  A dimension can collapse while the weighted average stays healthy -")
        add("  that is what these floors exist to surface.")
    add("")

    failing = [r for r in report["case_results"] if r["pass_rate"] < 1.0]
    if failing:
        add(f"CASES BELOW 100% ({len(failing)} of {report['cases']})")
        add(f"  {'case':<34}{'stratum':<13}{'pass':>7}{'score':>8}")
        for row in failing:
            flag = " FLAKY" if row["flaky"] else ""
            add(f"  {row['case']:<34}{row['complexity']:<13}"
                f"{row['pass_rate']:>6.0%}{row['mean_score']:>8.3f}{flag}")
            for breach in row["breaches"][:2]:
                add(f"      - {breach}")
    else:
        add("All cases passed every repeat.")
    add("")

    if report["flaky_cases"]:
        add(f"FLAKY (passed some repeats, failed others): {len(report['flaky_cases'])}")
        for name in report["flaky_cases"]:
            add(f"  - {name}")
        add("  Non-determinism, not a fixed defect. These are exactly the")
        add("  failures a single manual test run cannot see.")
        add("")

    add("READING THIS")
    add(f"  {report['cases']} cases is below the ~50 that yields tight confidence")
    add("  intervals; treat small movements between runs as noise. Per-stratum")
    add("  numbers matter more than the headline: adversarial is where this")
    add("  system fails, and a strong overall score can hide it.")
    add("=" * 74)
    return "\n".join(lines)
