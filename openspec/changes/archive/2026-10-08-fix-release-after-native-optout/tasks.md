## 1. Repair the condition

- [x] 1.1 Reproduce the final-job eligibility failure with the existing evaluator and a native-skipped/publish-success context.
- [x] 1.2 Add the minimal final-job condition; preserve upstream gates and add behavior coverage for denial outcomes.

## 2. Verify and publish

- [x] 2.1 Run focused policy checks and the applicable root gate; record actual expression/runtime limits and architecture review.
- [x] 2.2 Create the missing v0.3.1 GitHub Release from hash-matched tag-run distributions, with native qualification explicitly absent.
- [x] 2.3 Update publication/install/handoff records, pass protected dev checks and merge through the normal PR path; retain immutable tags and held production cutover. PR18 passed all six required hosted checks and merged normally as `49ae22012d0213388f44b13ee0efd6ba855b20af`; no native or main merge is claimed.
