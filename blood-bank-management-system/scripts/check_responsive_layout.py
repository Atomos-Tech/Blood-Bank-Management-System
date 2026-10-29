"""BB-43 [NFR-06] / TC-N05: check the registration, appointment, inventory and request forms at 360, 768
and 1440 px in a real browser, complete one valid submission of each, and record the result.

Prerequisites:
    pip install playwright           # once
    python manage.py migrate && python manage.py seed_demo
    python manage.py runserver       # in another terminal

Run from blood-bank-management-system/:
    python scripts/check_responsive_layout.py                      # bundled Chromium
    python scripts/check_responsive_layout.py --channel chrome     # installed Google Chrome
    python scripts/check_responsive_layout.py --channel msedge     # installed Microsoft Edge
    python scripts/check_responsive_layout.py --browser firefox    # needs `playwright install firefox`

For each width the script signs in as the demo accounts, opens each form, and checks that
  1. the page has no horizontal scrolling (document width <= window width),
  2. every visible form control has a label (a <label> or aria-label),
  3. one valid submission succeeds (the success message appears).
It saves a screenshot of every form and writes one JSON record per browser to
../docs/test-results/TC-N05/, then rebuilds ../docs/test-results/TC-N05_NFR-06_responsive_layout_results.md
from every record present. Demo data it creates is clearly labelled "TC-N05".
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
RESULTS_DIR = APP.parent / "docs" / "test-results" / "TC-N05"
REPORT = APP.parent / "docs" / "test-results" / "TC-N05_NFR-06_responsive_layout_results.md"
WIDTHS = (360, 768, 1440)
PASSWORD = "demo123"
REQUIRED_BROWSERS = {"Chrome": "chrome", "Edge": "msedge", "Firefox": "firefox"}

NO_HSCROLL = "document.documentElement.scrollWidth <= window.innerWidth"
UNLABELLED = """[...document.querySelectorAll('main input, main select, main textarea')]
  .filter(e => e.type !== 'hidden' && e.offsetParent !== null)
  .filter(e => !(e.labels && e.labels.length) && !e.getAttribute('aria-label'))
  .map(e => e.name || e.id)"""


def prepare_slots(count):
    """Make sure the demo donor has enough future slots to book one per width."""
    sys.path.insert(0, str(APP))
    os.chdir(APP)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bbms.settings")
    import django

    django.setup()
    from django.utils import timezone as dj_tz
    from bloodbank.models import AppointmentSlot, Branch

    branch = Branch.objects.get(code="MAIN")
    base = dj_tz.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=5)
    for i in range(count):
        AppointmentSlot.objects.get_or_create(branch=branch, starts_at=base + timedelta(hours=i), defaults={"capacity": 20})


def sign_in(page, base, email):
    page.context.clear_cookies()
    page.goto(f"{base}/login/")
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", PASSWORD)
    page.click(".login-card button")
    page.wait_for_load_state("networkidle")


def submit_and_expect(page, button, text):
    page.click(button)
    page.wait_for_load_state("networkidle")
    return text in page.content()


def check(page, name, width, shots, run_submission):
    layout_ok = page.evaluate(NO_HSCROLL)
    unlabelled = page.evaluate(UNLABELLED)
    shot = shots / f"{width}-{name}.png"
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(150)
    page.screenshot(path=str(shot), full_page=True)
    submitted = run_submission()
    return {
        "form": name, "width": width, "no_horizontal_scroll": layout_ok, "unlabelled_controls": unlabelled,
        "submission_ok": submitted, "screenshot": shot.relative_to(APP.parent).as_posix(),
        "pass": bool(layout_ok and not unlabelled and submitted),
    }


def run(base, browser_name, channel, label):
    from playwright.sync_api import sync_playwright

    shots = RESULTS_DIR / label.lower()
    shots.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    rows = []
    with sync_playwright() as p:
        launcher = getattr(p, browser_name)
        browser = launcher.launch(channel=channel) if channel else launcher.launch()
        version = browser.version
        for width in WIDTHS:
            context = browser.new_context(viewport={"width": width, "height": 900})
            page = context.new_page()

            sign_in(page, base, "staff@bbms.local")
            page.goto(f"{base}/donors/")
            page.click("details.register summary")

            def register():
                tag = f"{label.lower()}-{width}-{stamp}"
                page.fill("#id_full_name", f"TC-N05 Donor {width}")
                page.fill("#id_email", f"tc-n05-{tag}@example.test")
                page.fill("#id_phone", "9845099999")
                page.fill("#id_date_of_birth", "1995-05-05")
                page.fill("#id_weight_kg", "62")
                page.select_option("#id_blood_group", "A+")
                page.fill("#id_medical_history", "None")
                page.check("#id_consent_given")
                for radio in page.query_selector_all("input[type=radio][value=no]"):
                    radio.check()
                return submit_and_expect(page, "details.register button.primary", "Donor registered.")

            rows.append(check(page, "registration", width, shots, register))

            page.goto(f"{base}/inventory/")

            def search():
                page.fill("input[name=blood_group]", "O+")
                return submit_and_expect(page, "form.filter button", "Inventory")

            rows.append(check(page, "inventory-search", width, shots, search))

            sign_in(page, base, "donor@bbms.local")
            page.goto(f"{base}/appointments/")

            def book():
                page.select_option("#id_slot", index=1)
                return submit_and_expect(page, "form[method=post] button.primary", "Appointment booked")

            rows.append(check(page, "appointment", width, shots, book))

            sign_in(page, base, "hospital@bbms.local")
            page.goto(f"{base}/requests/")

            def request():
                page.select_option("#id_branch", index=1)
                page.fill("#id_blood_group", "O+")
                page.fill("#id_component", "Packed Red Cells")
                page.fill("#id_quantity", "1")
                page.fill("#id_priority", "Routine")
                later = datetime.now() + timedelta(days=2)
                page.fill("#id_required_by", later.strftime("%Y-%m-%d 10:00"))
                page.fill("#id_clinical_reference", f"TC-N05-{width}")
                return submit_and_expect(page, "form.form-grid button.primary", "Blood request submitted.")

            rows.append(check(page, "blood-request", width, shots, request))
            context.close()
        browser.close()
    record = {
        "browser": label, "engine": browser_name, "channel": channel or "bundled", "version": version,
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "base_url": base,
        "pass": all(r["pass"] for r in rows), "checks": rows,
    }
    (RESULTS_DIR / f"{label.lower()}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def write_report():
    records = {}
    for path in sorted(RESULTS_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        records[record["browser"]] = record
    covered = [name for name in REQUIRED_BROWSERS if name in records or (name in ("Chrome", "Edge") and "Chromium" in records)]
    complete = all(name in records for name in REQUIRED_BROWSERS) and all(r["pass"] for r in records.values())
    lines = [
        "# TC-N05 — Responsive browser layout (NFR-06)",
        "",
        "Generated by `blood-bank-management-system/scripts/check_responsive_layout.py`; do not edit by hand.",
        "",
        "**Pass criterion (Test Plan 6.19):** at 360, 768 and 1440 px no standard form needs horizontal scrolling, "
        "labels stay associated with their controls, and one valid submission of each form completes.",
        "",
        f"**Result:** {'PASS' if complete else 'PARTIAL'} — browsers recorded: "
        + (", ".join(f"{r['browser']} {r['version']}" for r in records.values()) or "none"),
        "",
    ]
    missing = [name for name in REQUIRED_BROWSERS if name not in records]
    if missing:
        lines += [
            f"Still to record: {', '.join(missing)}. Chromium is the engine of Chrome and Edge, so its run is "
            "strong evidence for both, but NFR-06 names the browsers themselves; run the script with "
            "`--channel chrome`, `--channel msedge` and `--browser firefox` on a machine that has them.",
            "",
        ]
    for record in records.values():
        lines += [
            f"## {record['browser']} {record['version']} ({record['engine']}, {record['channel']}) — "
            f"{'PASS' if record['pass'] else 'FAIL'}",
            "",
            f"Run {record['run_at']} against {record['base_url']}.",
            "",
            "| Width | Form | No horizontal scroll | Unlabelled controls | Valid submission | Screenshot |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for c in record["checks"]:
            lines.append(
                f"| {c['width']} px | {c['form']} | {'Yes' if c['no_horizontal_scroll'] else '**No**'} | "
                f"{', '.join(c['unlabelled_controls']) or 'None'} | {'Completed' if c['submission_ok'] else '**Failed**'} | "
                f"[{Path(c['screenshot']).name}]({Path(c['screenshot']).relative_to('docs/test-results').as_posix()}) |"
            )
        lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return complete, covered


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    parser.add_argument("--channel", default=None, help="chrome or msedge to use an installed browser")
    args = parser.parse_args()
    label = {"chrome": "Chrome", "msedge": "Edge"}.get(args.channel) or {
        "chromium": "Chromium", "firefox": "Firefox", "webkit": "WebKit"}[args.browser]
    prepare_slots(len(WIDTHS) + 1)
    record = run(args.base_url.rstrip("/"), args.browser, args.channel, label)
    write_report()
    print(f"{label} {record['version']}: {'PASS' if record['pass'] else 'FAIL'}; report written to {REPORT}")
    sys.exit(0 if record["pass"] else 1)


if __name__ == "__main__":
    main()
