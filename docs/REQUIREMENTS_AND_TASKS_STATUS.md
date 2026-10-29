# Blood Bank Management System — Requirements & Tasks Status

**Course:** `UE24CS341A` – Software Engineering  
**Baseline documents:** `phase1/1_Software_Requirements_Specification`, `phase1/2_Software_Test_Plan`, `phase1/3_Software_Architecture_and_Design_Specification`  
**Updated:** 28 Oct 2026 for `BB-06` from the live GitHub backlog. This replaces the 26 Oct audit, whose "Partial" entries (FR-14, FR-16, NFR-08, NFR-10, SEC-01, SEC-07 and others) have since been completed.

**Backlog:** 53 of 59 items complete. Test case results: [`test-results/TEST_EXECUTION_RESULTS.md`](test-results/TEST_EXECUTION_RESULTS.md). Every difference from the baseline documents: [`deviations.md`](deviations.md).

## Still open

- `BB-49` (#51) Configurable record retention and export — Nikita
- `BB-44` (#46) First-time staff finish donor lookup and check-in in 5 minutes — Nikita
- `BB-43` (#45) Works on current browsers from 360 to 1440 px — Nikita
- `BB-36` (#38) Send appointment reminders and request-status emails — Nikita
- `BB-06` (#8) Complete documentation and the deviations log — Niyatee
- `BB-05` (#7) Record and upload the 2-minute Sprint 2 demo video — Niyatee, Nishitha, Nikita

## Functional requirements

| Requirement | Backlog item | Issue | Owner | Sprint | Status | Test case |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| FR-01 | Register donor with validated mandatory fields | `BB-08` (#10) | Nikita | Sprint 1 | Complete | TC-F01 |
| FR-02 | Reject duplicate active donor profile by email | `BB-09` (#11) | Nikita | Sprint 0 (dry run) | Complete | TC-F01 |
| FR-03 | Evaluate eligibility from administrator-configured rules | `BB-10` (#12) | Nikita | Sprint 1 | Complete | TC-F02 |
| FR-04 | Show every failed eligibility rule and block confirmation | `BB-11` (#13) | Nikita | Sprint 1 | Complete | TC-F02 |
| FR-05 | Create, reschedule and cancel an appointment | `BB-12` (#14) | Nikita | Sprint 1 | Complete | TC-F03 |
| FR-06 | Prevent slot capacity from being exceeded | `BB-13` (#15) | Nikita | Sprint 1 | Complete | TC-F03 |
| FR-07 | Show donor donation history, appointments and eligibility date | `BB-14` (#16) | Nikita | Sprint 2 | Complete | TC-F04 |
| FR-08 | Record temporary or permanent donor deferrals | `BB-15` (#17) | Nikita | Sprint 1 | Complete | TC-F05 |
| FR-09 | Assign unique donation and blood unit identifiers | `BB-16` (#18) | Niyatee | Sprint 1 | Complete | TC-F06 |
| FR-10 | Record collection details for each blood unit | `BB-17` (#19) | Niyatee | Sprint 0 (dry run) | Complete | TC-F06 |
| FR-11 | Store results for the mandatory infection test panel | `BB-18` (#20) | Niyatee | Sprint 1 | Complete | TC-F07 |
| FR-12 | Release unit to Available only after all tests are acceptable | `BB-19` (#21) | Niyatee | Sprint 1 | Complete | TC-F07 |
| FR-13 | Quarantine units with reactive, indeterminate or incomplete results | `BB-20` (#22) | Niyatee | Sprint 1 | Complete | TC-F07 |
| FR-14 | Enforce valid blood unit state transitions | `BB-21` (#23) | Niyatee | Sprint 1 | Complete | TC-F08 |
| FR-15 | Calculate expiry from collection time and shelf-life rule | `BB-22` (#24) | Niyatee | Sprint 1 | Complete | TC-F08 |
| FR-16 | Automatically mark unissued units Expired | `BB-23` (#25) | Niyatee | Sprint 2 | Complete | TC-F08 |
| FR-17 | Search available stock by group, component, branch and expiry | `BB-24` (#26) | Nimay | Sprint 1 | Complete | TC-F09 |
| FR-18 | Alert staff on low stock and near-expiry units | `BB-25` (#27) | Nimay | Sprint 2 | Complete | TC-F10 |
| FR-19 | Hospital staff create a blood request | `BB-26` (#28) | Nimay | Sprint 0 (dry run) | Complete | TC-F11 |
| FR-20 | Track request states and record every transition | `BB-27` (#29) | Nimay | Sprint 1 | Complete | TC-F11 |
| FR-21 | Restrict request cancellation by role and state | `BB-28` (#30) | Nimay | Sprint 1 | Complete | TC-F12 |
| FR-22 | Reserve compatible units without double assignment | `BB-29` (#31) | Nimay | Sprint 1 | Complete | TC-F13 |
| FR-23 | Require a cross-match before issuing a reserved unit | `BB-30` (#32) | Nimay | Sprint 1 | Complete | TC-F13 |
| FR-24 | Issue unit and update request in one transaction | `BB-31` (#33) | Nimay | Sprint 1 | Complete | TC-F13 |
| FR-25 | Authenticate all four roles before protected functions | `BB-32` (#34) | Nishitha | Sprint 1 | Complete | TC-S01 |
| FR-26 | Administrator manages user accounts and roles | `BB-33` (#35) | Nishitha | Sprint 1 | Complete | TC-S02 |
| FR-27 | Administrator configures branches, slots, rules and thresholds | `BB-34` (#36) | Nishitha | Sprint 2 | Complete | TC-F14 |
| FR-28 | Generate reports by date range and branch | `BB-35` (#37) | Nishitha | Sprint 2 | Complete | TC-F15 |
| FR-29 | Send appointment reminders and request-status emails | `BB-36` (#38) | Nikita | Sprint 2 | **Open** | TC-F16 |
| FR-30 | Write audit events for all security-relevant actions | `BB-37` (#39) | Nishitha | Sprint 1 | Complete | TC-S03 |

## Non-functional requirements

| Requirement | Backlog item | Issue | Owner | Sprint | Status | Test case |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| NFR-01 | Search and dashboard respond within 3 s for 100 users | `BB-38` (#40) | Nimay | Sprint 2 | Complete | TC-N01 |
| NFR-02 | Reservation completes within 5 s with no duplicates under load | `BB-39` (#41) | Nimay | Sprint 2 | Complete | TC-N02 |
| NFR-03 | Support 99.0 percent monthly availability | `BB-40` (#42) | Nishitha | Sprint 2 | Complete | TC-N03 |
| NFR-04 | Daily encrypted backups with tested restore | `BB-41` (#43) | Nishitha | Sprint 2 | Complete | TC-N03 |
| NFR-05 | Committed data survives an application restart | `BB-42` (#44) | Nishitha | Sprint 1 | Complete | TC-N04 |
| NFR-06 | Works on current browsers from 360 to 1440 px | `BB-43` (#45) | Nikita | Sprint 2 | **Open** | TC-N05 |
| NFR-07 | First-time staff finish donor lookup and check-in in 5 minutes | `BB-44` (#46) | Nikita | Sprint 2 | **Open** | TC-N06 |
| NFR-08 | Layered modules with unit tests for state rules | `BB-45` (#47) | Niyatee | Sprint 2 | Complete | Code review + unit tests |
| NFR-09 | Branch-level access restrictions for operational users | `BB-46` (#48) | Nimay | Sprint 2 | Complete | TC-S02 |
| NFR-10 | Store UTC and display branch local time | `BB-47` (#49) | Niyatee | Sprint 2 | Complete | TC-N07 |
| NFR-11 | Hide medical and screening data from search and hospital reports | `BB-48` (#50) | Nimay | Sprint 1 | Complete | TC-S04 |
| NFR-12 | Configurable record retention and export | `BB-49` (#51) | Nikita | Sprint 2 | **Open** | Configuration inspection |

## Security requirements

| Requirement | Backlog item | Issue | Owner | Sprint | Status | Test case |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| SEC-01 | Server-side authorization on every protected action | `BB-50` (#52) | Niyatee | Sprint 1 | Complete | TC-S02 |
| SEC-02 | Adaptive password hashing and no plain-text passwords | `BB-51` (#53) | Nishitha | Sprint 1 | Complete | TC-S01 |
| SEC-03 | Lock account for 15 minutes after 5 failed logins | `BB-52` (#54) | Nishitha | Sprint 1 | Complete | TC-S01 |
| SEC-04 | Expire idle sessions and invalidate on logout or deactivation | `BB-53` (#55) | Nikita | Sprint 2 | Complete | TC-S05 |
| SEC-05 | HTTPS only in production | `BB-54` (#56) | Nimay | Sprint 2 | Complete | Configuration inspection |
| SEC-06 | CSRF protection and input validation and encoding | `BB-55` (#57) | Nikita | Sprint 1 | Complete | TC-S06 |
| SEC-07 | Complete, tamper-resistant audit records | `BB-56` (#58) | Niyatee | Sprint 1 | Complete | TC-S03 |
| SEC-08 | Encrypted backups restricted to backup and admin accounts | `BB-57` (#59) | Nishitha | Sprint 2 | Complete | TC-N03 |
| SEC-09 | Show only the minimum donor and patient data per role | `BB-58` (#60) | Nimay | Sprint 1 | Complete | TC-S04 |
| SEC-10 | Generic authentication failure message | `BB-59` (#61) | Nishitha | Sprint 0 (dry run) | Complete | TC-S01 |

## Process items

| Requirement | Backlog item | Issue | Owner | Sprint | Status | Test case |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| — | Set up GitHub Project, backlog, story points and sprints | `BB-01` (#3) | Nishitha | Sprint 0 (dry run) | Complete | Process |
| — | Enable GitHub Actions CI for every push and PR | `BB-02` (#4) | Nishitha | Sprint 1 | Complete | Process |
| — | Automate PRs: labels, assignee, linked issue, templates, branch protection | `BB-03` (#5) | Nishitha | Sprint 1 | Complete | Process |
| — | Record and upload the 1-minute Sprint 1 demo video | `BB-04` (#6) | Nikita | Sprint 1 | Complete | Process |
| — | Record and upload the 2-minute Sprint 2 demo video | `BB-05` (#7) | Niyatee, Nishitha, Nikita | Sprint 2 | **Open** | Process |
| — | Complete documentation and the deviations log | `BB-06` (#8) | Niyatee | Sprint 2 | **Open** | Process |
| — | Freeze development and tag the v1.0 release | `BB-07` (#9) | Nishitha | Sprint 2 | Complete | Process |
