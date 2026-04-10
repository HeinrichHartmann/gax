# Project Evaluation Report

*Template for structured assessments*
**Team:** Acme Engineering
**Author:** \_\_\_\_\_\_\_\_\_\_\_\_\_\_\_
**Status:** Draft

## Cost Analysis

Platform cost has two components that always stack:

1. **Compute cost** - standard pricing for the instance type, billed for the entire time the cluster is provisioned.
1. **Service surcharge** - charged on top of compute in platform units. For batch pipelines, the relevant tier is **Jobs Compute** (lowest rate).

The critical point: **you pay from start to termination**, regardless of whether a task is actively running.

*Fill in Current and Target columns. If the value is identical, write `=` in the Target column.*

| Metric | Current | Target | Comparison |
| :---- | :---- | :---- | :---- |
| **Platform** (e.g. Service A, Service B) |  | Platform X |  |
| **Instance type** (e.g. m5.4xlarge) |  |  |  |
| **Cluster size** (# nodes / vCPUs) |  |  |  |
| **Cold start time** (trigger to first task) |  |  |  |
| **Total run time** (wall-clock) |  |  |  |
| **Cost per run** (actual or estimate) |  |  | cheaper / ~equal / more expensive |

## Integration Scores

*Use the 0-5 scale below.*

- ✅ **5 - Seamless:** works as expected, zero friction
- 🟢 **4 - Easy:** minor rough edges, nothing blocking
- 🟡 **3 - Convenient:** some effort required, but straightforward
- 🟠 **2 - Tedious:** significant manual steps or workarounds
- 🔴 **1 - Major friction:** barely works, serious workarounds required
- ⛔ **0 - Impossible:** not supported or completely blocked
- ⬜ **N/A:** not tested or not applicable

| Integration Point | Score | Comment |
| :---- | :---- | :---- |
| **Data Integrations** |  |  |
| Data Lake | 🟢 4 | Reads and writes work well |
| Feature Store | 🟡 3 | Some friction with schema setup |
| Message Queue | N/A | *Not tested in this evaluation* |
| **Platform Integrations** |  |  |
| CI/CD Pipeline | ✅ 5 | YAML config works cleanly |
| Secret Manager | 🟠 2 | Extra hops needed for credential rotation |
| Monitoring Stack | 🔴 1 | No out-of-box integration |

## Performance and Scaling

### Batch Performance

| Dimension | What to measure | Observed | Limit hit? | Comment |
| :---- | :---- | :---- | :---- | :---- |
| **Concurrency** | Max parallel tasks | 4 tasks | ✅ | No issues |
| **Data volume** | Largest dataset processed | ~10M rows | ✅ | No failures at this scale |
| **Throughput** | Records per minute at peak | 500K rec/min |  |  |
| **Queue wait** | Time from submit to running | 4-10 min |  | Cold start dominates |

### Serving Performance

*Skip if batch-only.*

|  | Value |
| :---- | :---- |
| Expected normal load (req/sec) |  |
| Expected peak load (req/sec) |  |
| Typical payload size |  |

## Functional Requirements

### Onboarding Metrics

* **Time to Hello World:** \_\_\_\_\_\_\_\_ (mins/hours)
* **Time to Production:** \_\_\_\_\_\_\_\_ (days/weeks)

| # | Requirement | Score | Comment |
| :---- | :---- | :---- | :---- |
| **Setup** |  |  |  |
| R01 | **Onboard a new user** (access, permissions) | 🟠 2 | Not self-explanatory for new users |
| R02 | Set up local dev environment |  |  |
| **Workflow** |  |  |  |
| R03 | Install custom dependencies | 🟡 3 | Public packages work fine |
| R04 | **Run code interactively** (notebook or IDE) | 🟡 3 | Cold start kills iteration speed |
| R05 | **Deploy as scheduled job** | 🟢 4 | Config-driven deployment works well |
| **Operations** |  |  |  |
| R06 | Debug a failed job (logs, errors) | 🟠 2 | Error messages not always actionable |
| R07 | Monitor a running job (progress, resources) | 🟡 3 | Basic metrics visible; gaps in GPU display |
