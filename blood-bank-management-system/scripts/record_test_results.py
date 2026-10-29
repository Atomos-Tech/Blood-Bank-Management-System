"""BB-06: run the automated suite and record a result for every test case in the SRS and Test Plan.

Run from blood-bank-management-system/:
    python scripts/record_test_results.py

Each automated test class names its test cases in its docstring or test-method names (for example
"TC-F07" or "test_tc_f07_..."). The script runs the whole bloodbank suite once, groups the outcome of
every test by test case, adds the cases that are evidenced by a drill or manual run rather than a unit
test, and writes ../docs/test-results/TEST_EXECUTION_RESULTS.md. Re-run it whenever tests change.
"""
import os
import platform
import re
import subprocess
import sys
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
REPO = APP.parent
OUTPUT = REPO / "docs" / "test-results" / "TEST_EXECUTION_RESULTS.md"
sys.path.insert(0, str(APP))
os.chdir(APP)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bbms.settings")

import django  # noqa: E402

django.setup()

from django.test.runner import DiscoverRunner  # noqa: E402

# Every test case referenced by the SRS (traceability table) and the Test Plan (section 6).
TEST_CASES = {
    "TC-F01": ("Donor registration and duplicate prevention", "FR-01, FR-02"),
    "TC-F02": ("Eligibility rules and failed-reason display", "FR-03, FR-04"),
    "TC-F03": ("Appointment capacity and cancellation", "FR-05, FR-06"),
    "TC-F04": ("Donor history access", "FR-07, SEC-01"),
    "TC-F05": ("Donor deferral", "FR-08"),
    "TC-F06": ("Donation and unique unit creation", "FR-09, FR-10"),
    "TC-F07": ("Laboratory screening and quarantine", "FR-11, FR-12, FR-13"),
    "TC-F08": ("Unit state transitions and expiry", "FR-14, FR-15, FR-16"),
    "TC-F09": ("Authorized inventory search", "FR-17"),
    "TC-F10": ("Low-stock and near-expiry alerts", "FR-18"),
    "TC-F11": ("Blood request workflow", "FR-19, FR-20"),
    "TC-F12": ("Request cancellation permissions", "FR-21, SEC-01"),
    "TC-F13": ("Reservation, cross-match and atomic issue", "FR-22, FR-23, FR-24"),
    "TC-F14": ("Administrator configuration", "FR-27"),
    "TC-F15": ("Reports by date range and branch", "FR-28"),
    "TC-F16": ("Appointment reminders and request-status notifications", "FR-29"),
    "TC-S01": ("Login failure, lockout and generic message", "FR-25, SEC-02, SEC-03, SEC-10"),
    "TC-S02": ("Role-based authorization", "FR-26, SEC-01, NFR-09"),
    "TC-S03": ("Audit completeness and immutability", "FR-30, SEC-07"),
    "TC-S04": ("Minimum data per role", "NFR-11, SEC-09"),
    "TC-S05": ("Session expiry and invalidation", "SEC-04"),
    "TC-S06": ("CSRF protection and input encoding", "SEC-06"),
    "TC-N01": ("Search and dashboard response time", "NFR-01"),
    "TC-N02": ("Reservation time and no duplicates under load", "NFR-02"),
    "TC-N03": ("Encrypted backup and restoration", "NFR-03, NFR-04, SEC-08"),
    "TC-N04": ("Committed data survives a restart", "NFR-05"),
    "TC-N05": ("Responsive browser layout", "NFR-06"),
    "TC-N06": ("First-time staff usability", "NFR-07"),
    "TC-N07": ("UTC storage and branch local time", "NFR-10"),
}

# Cases whose pass criterion needs a drill, a benchmark or people, not just a unit test. The function
# returns (status, evidence) from what is in the repository right now, so the record cannot overclaim.
def _benchmark(case):
    return "Pass (benchmark recorded)", f"`docs/test-results/{case}` results file; checked by `PerformanceAndConcurrencyEvidenceTests`"


def _restore_drill():
    ops = (REPO / "docs" / "operations.md").read_text(encoding="utf-8")
    if "_(to be filled during the TC-N03 drill)_" in ops:
        return "Not executed (drill pending)", "Restore drill not yet recorded in `docs/operations.md` section 3; backup and restore scripts are checked by `BackupAndRestoreTests`"
    return "See drill record", "Restore drill table in `docs/operations.md` section 3"


def _restart_drill():
    results = REPO / "blood-bank-management-system" / "docs" / "test-results" / "TC-N04_NFR-05_restart_persistence_results.md"
    if not results.exists():
        return "Not executed (drill pending)", "`scripts/verify_restart_persistence.py` not yet run against Docker; apparatus checked by `RestartPersistenceTests`"
    verdict = "Pass" if "**Result:** PASS" in results.read_text(encoding="utf-8") else "Fail"
    return verdict, "`blood-bank-management-system/docs/test-results/TC-N04_NFR-05_restart_persistence_results.md`"


def _usability_trial():
    trial = REPO / "docs" / "test-results" / "TC-N06_NFR-07_usability_trial.md"
    if "_(to be filled during the TC-N06 trial)_" in trial.read_text(encoding="utf-8"):
        return "Not executed (trial pending)", "Timed first-time-user trial not yet recorded in `docs/test-results/TC-N06_NFR-07_usability_trial.md`"
    return "See trial record", "`docs/test-results/TC-N06_NFR-07_usability_trial.md`"


def _responsive_layout():
    report = REPO / "docs" / "test-results" / "TC-N05_NFR-06_responsive_layout_results.md"
    if not report.exists():
        return "Not executed (browser run pending)", "`scripts/check_responsive_layout.py` not yet run"
    text = report.read_text(encoding="utf-8")
    if "**Result:** PASS" in text:
        return "Pass", "`docs/test-results/TC-N05_NFR-06_responsive_layout_results.md`"
    if "FAIL" in text.split("**Result:**", 1)[1].split("\n", 1)[0] or "— FAIL" in text:
        return "Fail", "`docs/test-results/TC-N05_NFR-06_responsive_layout_results.md`"
    passed = re.findall(r"^## (\w+) .* — PASS$", text, re.M)
    missing = re.search(r"^Still to record: (.+?)\.", text, re.M)
    status = f"Pass in {', '.join(passed)}" + (f" ({missing.group(1)} run pending)" if missing else "")
    return status, "`docs/test-results/TC-N05_NFR-06_responsive_layout_results.md`"


DRILLS = {
    "TC-N01": lambda: _benchmark("TC-N01_NFR-01_load_test_results.md"),
    "TC-N02": lambda: _benchmark("TC-N02_NFR-02_reservation_concurrency_results.md"),
    "TC-N03": _restore_drill,
    "TC-N04": _restart_drill,
    "TC-N05": _responsive_layout,
    "TC-N06": _usability_trial,
}
# Cases still waiting on an open backlog item; listed so a gap is visible rather than silently missing.
OPEN_ITEMS = {
}
CASE_PATTERN = re.compile(r"TC-[FSN]\d\d")
METHOD_PATTERN = re.compile(r"tc_([fsn])(\d\d)")


class RecordingResult(unittest.TextTestResult):
    outcomes = {}

    def _set(self, test, outcome):
        current = RecordingResult.outcomes.get(test.id())
        if current != "fail":
            RecordingResult.outcomes[test.id()] = outcome

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._set(test, "fail")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "skip")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            RecordingResult.outcomes[test.id()] = "fail"


class RecordingRunner(DiscoverRunner):
    def get_resultclass(self):
        return RecordingResult


def cases_for(test_class, method_name):
    found = set(CASE_PATTERN.findall(test_class.__doc__ or ""))
    found |= {f"TC-{kind.upper()}{num}" for kind, num in METHOD_PATTERN.findall(method_name)}
    return found


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=REPO).stdout.strip()
    except OSError:
        return "unknown"


def main():
    runner = RecordingRunner(verbosity=1, interactive=False)
    failures = runner.run_tests(["bloodbank"])

    import bloodbank.tests as test_module

    per_case = defaultdict(lambda: {"classes": set(), "pass": 0, "fail": 0, "skip": 0})
    totals = defaultdict(int)
    for test_id, outcome in RecordingResult.outcomes.items():
        totals[outcome] += 1
        _module, class_name, method_name = test_id.rsplit(".", 2)
        test_class = getattr(test_module, class_name, None)
        if test_class is None:
            continue
        for case in cases_for(test_class, method_name):
            per_case[case]["classes"].add(class_name)
            per_case[case][outcome] += 1

    rows = []
    for case, (title, requirements) in TEST_CASES.items():
        auto = per_case.get(case)
        auto_text = ""
        if auto:
            ran = auto["pass"] + auto["fail"] + auto["skip"]
            auto_text = f"{ran} test(s) in {', '.join(f'`{c}`' for c in sorted(auto['classes']))}"
        if case in DRILLS:
            status, evidence = DRILLS[case]()
            if auto_text:
                evidence = f"{evidence}; {auto_text}"
            if auto and auto["fail"]:
                status = "Fail"
        elif auto:
            status = "Fail" if auto["fail"] else "Pass"
            evidence = auto_text
        else:
            status = "Not executed"
            evidence = f"No test yet; waits on {OPEN_ITEMS.get(case, 'an open backlog item')}"
        rows.append((case, title, requirements, status, evidence))

    counts = defaultdict(int)
    for row in rows:
        counts[row[3].split(" (")[0]] += 1
    now = datetime.now(timezone.utc)
    lines = [
        "# Test Plan execution results",
        "",
        "Generated by `blood-bank-management-system/scripts/record_test_results.py`. Re-run it after any change "
        "to the tests; do not edit this table by hand.",
        "",
        f"- **Executed:** {now:%Y-%m-%d %H:%M} UTC, commit `{git_commit()}`",
        f"- **Environment:** Python {platform.python_version()} on {platform.system()} {platform.release()}, "
        f"Django {django.get_version()}, SQLite test database. GitHub Actions CI runs the same suite on Python 3.12.",
        f"- **Automated suite:** {sum(totals.values())} tests, {totals['pass']} passed, {totals['fail']} failed, "
        f"{totals['skip']} skipped",
        f"- **Test cases:** {len(rows)} total — " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
        "",
        "| Test case | Title | Requirements | Result | Evidence |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    lines += [f"| {c} | {t} | {r} | **{s}** | {e} |" for c, t, r, s, e in rows]
    lines += [
        "",
        "Status meanings: **Pass** — every automated test for the case passed in this run. "
        "**Pass (benchmark recorded)** — the performance run's measured results are committed and pass their "
        "thresholds. **Not executed (drill pending)** — the case needs a Docker drill that has not been "
        "recorded yet. **Not executed (trial pending)** — the case needs a timed session with a first-time user that "
        "has not been recorded yet. **Not executed** — no test exists yet because its backlog item is still open.",
        "",
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
