# BBMS GitHub kit: Deliverables Part-2

Drop these folders into the root of the repo:

| Path | Purpose | Backlog item |
|---|---|---|
| `.github/workflows/ci.yml` | CI on every push/PR: flake8 (fatal errors), `manage.py check`, missing-migration check, `seed_demo`, full test suite | BB-02 |
| `.github/workflows/pr-automation.yml` + `.github/labeler.yml` | Auto-labels PRs by files changed, assigns the author, fails the PR if it doesn't say `Closes #N` | BB-03 |
| `.github/pull_request_template.md` | Testing checklist and a **Deviation from SRS/SAD/Test Plan** section | BB-03 |
| `.github/ISSUE_TEMPLATE/*.yml` | User-story (SRS ID, acceptance criteria, points) and bug templates | BB-03 |
| `docs/deviations.md` | Running log of deviations for the final demo | BB-17 / BB-35 |
| `scripts/create_backlog.py` | Pushes `BBMS_Product_Backlog.xlsx` to GitHub (issues, milestones, Project) | BB-01 |

## 1. Push the backlog (deliverable 1, due 12 Oct)

After the team has agreed on the spreadsheet (SRS IDs checked, names and GitHub usernames filled in on *Sprint Plan*):

```sh
# one-time, on the repo owner's machine
gh auth login
gh auth refresh -s project          # Projects need this scope
pip install openpyxl

python3 scripts/create_backlog.py --repo OWNER/REPO --xlsx BBMS_Product_Backlog.xlsx          # preview
python3 scripts/create_backlog.py --repo OWNER/REPO --xlsx BBMS_Product_Backlog.xlsx --apply  # create
```

It creates labels, one milestone per sprint (with due dates), one issue per story (story, acceptance
criteria, points, assignee), and a Project **"BBMS - SE Mini Project"** linked to the repo with
*Story Points* and *Sprint* fields filled in. It is safe to re-run after editing the sheet (e.g. on 19 Oct).

Then in the Project UI: add a **Board** view (group by Status) and a **Table** view (group by Sprint,
show sum of Story Points). Under Project > Workflows, turn on *Item closed → Done* and
*Pull request merged → Done*.

## 2. Make "assignee raises PR, owner reviews and merges" enforceable

Settings → Branches → Add rule for `main`:
- Require a pull request before merging, **1 approval**
- Require status checks to pass: **Lint, check and test** and **Label, assign and validate PR**
- Do not allow bypassing

Settings → Actions → General → Workflow permissions: **Read and write**.

## 3. Day-to-day flow

```sh
git checkout -b BB-07-donor-validation
# ...code + tests...
git push -u origin BB-07-donor-validation
gh pr create --fill        # put "Closes #<issue>" in the description
```
CI runs, the PR is labelled and assigned automatically, a repo owner reviews and merges, and the issue
closes and moves to Done on the board.
