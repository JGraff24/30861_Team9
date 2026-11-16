"""AC-18 audit performance and repeatability evidence (spec 3.0.0 §12, §17).

Runs supported boundary workbooks through a private local instance of the real system:
the API (in-process ASGI client), SQLite, filesystem storage, the production six-rule
engine, the production bounded child-process runner and the production worker loop
(`run_workers`, two slots plus housekeeping) on background threads. Five runs per
workload by default, one at a time, so queue time reflects an otherwise idle system.

Every time is real elapsed monotonic time; no simulated clock is involved. Timing comes
from wrapping, inside this process only, the module-level `execute_bounded` calls used
by the upload path and the worker and the repository methods that accept, claim and
finish a job. No production code is modified. Reported separately per run:

  intakeValidationSeconds   bounded validation child during the upload request
  uploadRequestSeconds      client-observed upload request, including validation/storage
  queueSeconds              durable acceptance -> worker claim
  engineSeconds             bounded audit child (spawn, six rules, result transfer)
  workerJobSeconds          claim -> terminal state committed (integrity checks included)
  acceptanceToTerminalSeconds
  endToEndSeconds           upload request start -> terminal state observed by polling

Outcomes: SUCCESS (successful terminal audit), CONTROLLED_FAILURE (rejected upload or
PROCESSING_FAILED with a ProjectError envelope; counts toward AC-18's failure
alternative, never as a successful audit), UNCONTROLLED (anything else, including still
PROCESSING after the 80-second polling deadline). Successful runs are compared for
identical findings and limitations.

    uv run python -m scripts.ac18_performance --workload cells --mode ordinary \\
        --output docs/evidence/phase5/dylan_ac18_cells_ordinary.json

This is local evidence for the documented host only. Hosted P5-PERFORMANCE-RUN-1..5 are
measured by the independent tester against the deployment.
"""

import argparse
import json
import platform
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import app.jobs.worker as worker_module
import app.services.projects as projects_module
from app.jobs.worker import run_workers
from scripts.engine_evidence import (
    ADMIN,
    OWNER,
    environment,
    findings_digest,
    git_revision,
    local_system,
    sha256,
)
from tester.suite.workbooks import Sheet, build_workbook, cells_workbook, padded_clean_workbook

SUCCESS_STATUSES = {"AUDIT_COMPLETE", "REVIEW_REQUIRED", "REMEDIATION_AVAILABLE"}
POLL_DEADLINE = 80.0  # spec §12 audit polling deadline from acceptance
POLL_INTERVAL = 0.05
BOUND = 60.0


def _column(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def mixed_workbook() -> bytes:
    """50 generic-named worksheets x 5,000 cells = exactly 250,000 nonempty cells.

    Each sheet has a distinct text header row over a dense numeric body (TABLE_HEADER), a
    'click here' internal link (LINK_TEXT), a populated merge (MERGED_CELLS) and a generic
    name (WORKSHEET_NAME), so repeatability is checked over findings in several rules.
    """
    sheets = []
    for number in range(1, 51):
        rows = []
        for row in range(1, 101):
            cells = []
            for col in range(1, 51):
                ref = f"{_column(col)}{row}"
                if row == 1:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>Metric {col}</t></is></c>')
                elif row == 100 and col == 1:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>click here</t></is></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{(row * 31 + col * 17 + number) % 997}</v></c>')
            rows.append(f'<row r="{row}">{"".join(cells)}</row>')
        tail = (
            '<mergeCells count="1"><mergeCell ref="A100:B100"/></mergeCells>'
            f'<hyperlinks><hyperlink ref="A100" location="Sheet{number}!A1"/></hyperlinks>'
        )
        sheets.append(
            Sheet(f"Sheet{number}", number, f"sheet{number}", rows_xml="".join(rows), tail_xml=tail)
        )
    return build_workbook(sheets)


WORKLOADS: dict[str, tuple[Callable[[], bytes], str]] = {
    "cells": (
        lambda: cells_workbook(250_000),
        "tester boundary generator: one worksheet, exactly 250,000 numeric cells",
    ),
    "mixed": (
        mixed_workbook,
        "50 generic worksheets x 5,000 cells (250,000), headers, links and merges",
    ),
    "bytes": (
        lambda: padded_clean_workbook(25_000_000),
        "tester boundary generator: clean workbook of exactly 25,000,000 bytes",
    ),
}


class Recorder:
    """Monotonic event log filled by in-process wrappers around production calls."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def add(self, **event: Any) -> None:
        with self.lock:
            self.events.append(event)

    def since(self, start: float) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.events if event["at"] >= start]


def instrument(services: Any, recorder: Recorder) -> Callable[[], None]:
    """Wrap timing hooks; returns a function restoring the originals."""
    originals: list[tuple[Any, str, Any]] = []

    def wrap_execution(module: Any, origin: str) -> None:
        original = module.execute_bounded

        def timed(runner: Any, operation: str, *args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            outcome = "ok"
            try:
                return original(runner, operation, *args, **kwargs)
            except Exception as exc:
                outcome = getattr(exc, "code", type(exc).__name__)
                raise
            finally:
                ended = time.monotonic()
                recorder.add(kind=f"{origin}_{operation}", at=started, end=ended, outcome=outcome)

        originals.append((module, "execute_bounded", original))
        module.execute_bounded = timed

    wrap_execution(projects_module, "intake")
    wrap_execution(worker_module, "worker")
    repository = services.repository

    def wrap_method(name: str, after: Callable[[Any, tuple[Any, ...]], None]) -> None:
        original = getattr(repository, name)

        def timed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            after(result, args)
            return result

        originals.append((repository, name, None))
        setattr(repository, name, timed)

    wrap_method(
        "create_upload",
        lambda _, args: recorder.add(kind="accepted", at=time.monotonic(), audit=args[2].audit_id),
    )

    def claimed(job: Any, _: tuple[Any, ...]) -> None:
        if job is not None:
            recorder.add(kind="claimed", at=time.monotonic(), audit=job.audit_id, job=job.job_id)

    wrap_method("claim_next_job", claimed)
    for name in ("complete_audit", "fail_audit"):
        wrap_method(
            name, lambda _, args: recorder.add(kind="terminal", at=time.monotonic(), job=args[0])
        )

    def restore() -> None:
        for owner, name, original in reversed(originals):
            if original is None:
                delattr(owner, name)  # instance attribute shadowing the class method
            else:
                setattr(owner, name, original)

    return restore


def one_run(
    client: Any, recorder: Recorder, data: bytes, headers: dict[str, str]
) -> dict[str, Any]:
    started = time.monotonic()
    response = client.post(
        "/api/v1/workbooks", files={"file": ("boundary.xlsx", data)}, headers=headers
    )
    uploaded = time.monotonic()
    run: dict[str, Any] = {
        "uploadStatus": response.status_code,
        "uploadRequestSeconds": round(uploaded - started, 3),
    }
    if response.status_code != 202:
        body = response.json()
        enveloped = isinstance(body, dict) and {"code", "message", "requestId"} <= set(body)
        run |= {
            "outcome": "CONTROLLED_FAILURE" if enveloped else "UNCONTROLLED",
            "errorCode": body.get("code") if enveloped else None,
        }
        return run
    accepted = response.json()
    path = (
        f"/api/v1/workbooks/{accepted['workbookId']}/versions/{accepted['versionId']}"
        f"/audits/{accepted['auditId']}"
    )
    audit: dict[str, Any] = {"status": "PROCESSING"}
    while time.monotonic() - uploaded < POLL_DEADLINE:
        polled = client.get(path, headers=headers)
        if polled.status_code == 200:
            audit = polled.json()
            if audit["status"] != "PROCESSING":
                break
        time.sleep(POLL_INTERVAL)
    observed = time.monotonic()
    events = recorder.since(started)
    aid = accepted["auditId"]
    accept = next((e["at"] for e in events if e["kind"] == "accepted" and e["audit"] == aid), None)
    claim = next((e for e in events if e["kind"] == "claimed" and e["audit"] == aid), None)
    terminal = (
        next(
            (e["at"] for e in events if e["kind"] == "terminal" and e["job"] == claim["job"]), None
        )
        if claim
        else None
    )

    def duration(kind: str) -> float | None:
        matches = [e for e in events if e["kind"] == kind]
        return round(sum(e["end"] - e["at"] for e in matches), 3) if matches else None

    def span(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else round(b - a, 3)

    run |= {
        "intakeValidationSeconds": duration("intake_validate"),
        "queueSeconds": span(accept, claim["at"] if claim else None),
        "workerValidationSeconds": duration("worker_validate"),
        "engineSeconds": duration("worker_audit"),
        "workerJobSeconds": span(claim["at"] if claim else None, terminal),
        "acceptanceToTerminalSeconds": span(accept, terminal),
        "endToEndSeconds": round(observed - started, 3),
        "auditStatus": audit["status"],
    }
    if audit["status"] in SUCCESS_STATUSES:
        run |= {
            "outcome": "SUCCESS",
            "findingCount": len(audit["findings"]),
            "findingsDigest": findings_digest(audit["findings"], audit["limitations"]),
            "_findings": audit["findings"],
        }
    elif audit["status"] == "PROCESSING_FAILED" and audit.get("error"):
        run |= {"outcome": "CONTROLLED_FAILURE", "errorCode": audit["error"]["code"]}
    else:
        run |= {"outcome": "UNCONTROLLED"}
    run["withinSixtySecondsEndToEnd"] = run["endToEndSeconds"] <= BOUND
    run["ac18Satisfied"] = (
        run["outcome"] == "SUCCESS" and run["withinSixtySecondsEndToEnd"]
    ) or run["outcome"] == "CONTROLLED_FAILURE"
    return run


def measure(
    workload: str,
    mode: str,
    runs: int,
    concurrency: int,
    audit_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    build, description = WORKLOADS[workload]
    data = build()
    recorder = Recorder()
    with tempfile.TemporaryDirectory(prefix="ac18-") as state:
        with local_system(
            Path(state), measured=mode == "measured", audit_timeout_seconds=audit_timeout_seconds
        ) as client:
            app: Any = client.app
            services = app.state.services
            headers = dict(OWNER)
            if mode == "measured":
                setup = client.post(
                    "/test/setup", json={"session_id": "ac18", "setup": {}}, headers=ADMIN
                )
                if setup.status_code != 201:
                    raise SystemExit(f"test setup failed: {setup.status_code} {setup.text}")
                headers["X-Test-Session"] = "ac18"
            restore = instrument(services, recorder)
            stop = threading.Event()
            pool = threading.Thread(
                target=run_workers, args=(services, concurrency, stop), daemon=True
            )
            pool.start()
            try:
                results = [one_run(client, recorder, data, headers) for _ in range(runs)]
            finally:
                stop.set()
                pool.join(timeout=120)
                restore()
            settings = services.settings
    successful = [run for run in results if run["outcome"] == "SUCCESS"]
    reference = successful[0].pop("_findings") if successful else []
    for run in successful[1:]:
        run.pop("_findings")
    digests = sorted({run["findingsDigest"] for run in successful})
    counts: dict[str, int] = {}
    for finding in reference:
        counts[finding["ruleId"]] = counts.get(finding["ruleId"], 0) + 1
    return {
        "workload": workload,
        "description": description,
        "mode": mode,
        "modeDescription": "isolated test session with per-session code measurement"
        if mode == "measured"
        else "ordinary data, no code measurement",
        "workbook": {"sha256": sha256(data), "bytes": len(data)},
        "settings": {
            "productionBounds": audit_timeout_seconds is None,
            "workerConcurrency": concurrency,
            "auditTimeoutSeconds": settings.audit_timeout_seconds,
            "validationTimeoutSeconds": settings.validation_timeout_seconds,
            "queueTimeoutSeconds": settings.queue_timeout_seconds,
            "engineMemoryLimitMiB": settings.engine_memory_limit_mib,
            "memoryLimitEnforced": platform.system() == "Linux",
        },
        "runs": [{"run": index} | run for index, run in enumerate(results, start=1)],
        "summary": {
            "outcomes": {
                outcome: sum(1 for run in results if run["outcome"] == outcome)
                for outcome in ("SUCCESS", "CONTROLLED_FAILURE", "UNCONTROLLED")
            },
            "allRunsSatisfyAc18": all(run["ac18Satisfied"] for run in results),
            "successfulFindingsIdentical": len(digests) <= 1,
            "findingsDigests": digests,
            "findingsByRule": counts,
            "maxEndToEndSeconds": max(
                (r["endToEndSeconds"] for r in results if "endToEndSeconds" in r), default=None
            ),
            "maxEngineSeconds": max(
                (r["engineSeconds"] or 0 for r in results if "engineSeconds" in r), default=None
            ),
            "maxQueueSeconds": max(
                (r["queueSeconds"] or 0 for r in results if "queueSeconds" in r), default=None
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workload", choices=sorted(WORKLOADS), action="append", required=True)
    parser.add_argument("--mode", choices=("ordinary", "measured"), action="append", required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--audit-timeout-seconds",
        type=float,
        help="lower the 60-second audit bound to demonstrate the controlled-timeout path; "
        "such runs are labeled productionBounds=false and are not AC-18 success evidence",
    )
    args = parser.parse_args(argv)
    if args.audit_timeout_seconds is not None and not 0 < args.audit_timeout_seconds <= 60:
        parser.error("--audit-timeout-seconds must be in (0, 60]")
    started = datetime.now(UTC)
    measurements = [
        measure(workload, mode, args.runs, args.concurrency, args.audit_timeout_seconds)
        for workload in args.workload
        for mode in args.mode
    ]
    try:
        memory = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        memory = ""
    report = {
        "kind": "ac18-local-performance",
        "specificationVersion": "3.0.0",
        "rulesetVersion": "2.0.0",
        "startedAt": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "revision": git_revision(),
        "environment": environment() | {"memoryBytes": int(memory) if memory.isdigit() else None},
        "system": "local real stack: in-process API, SQLite, filesystem storage, production "
        "worker loop and bounded engine children; not a hosted deployment",
        "clock": "time.monotonic real elapsed time; no simulated time",
        "measurements": measurements,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for item in measurements:
        summary = item["summary"]
        print(
            f"{item['workload']}/{item['mode']}: {summary['outcomes']} identical="
            f"{summary['successfulFindingsIdentical']} maxE2E={summary['maxEndToEndSeconds']}s "
            f"maxEngine={summary['maxEngineSeconds']}s maxQueue={summary['maxQueueSeconds']}s"
        )
    return 0 if all(m["summary"]["allRunsSatisfyAc18"] for m in measurements) else 1


if __name__ == "__main__":
    raise SystemExit(main())
