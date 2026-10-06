# Evaluation Targets

Status: Targets defined. Baseline = TBD.

This document is the single source of truth for:

Baseline
→ Structural Latency Optimization
→ Quality Optimization
→ Regression
→ Final Optimization

Architecture, Model and Provider remain fixed during the same evaluation series.

---

## 1. Evaluation Principles

- Main Agent: `deepseek/deepseek-v4-flash` + `streamlake/fp8`
- Tester Agent: `z-ai/glm-5.3-flash` + `relace`
- Jev: `typesafe/jev-1.13`
- Default Tester concurrency: 3
- Same Scenario, Ground Truth, Test Data, Budget and Reset rules must be used when comparing runs.
- Ground Truth must not enter Agent prompts.
- Latency / Cost optimization must not cause Quality Regression.
- Failed or interrupted Runs must remain in evaluation results.
- LangSmith is only for limited Observability, not the main Evaluation system.
- Do not use LLM-as-a-Judge.
- Do not report P95 unless there are enough samples.

---

# 2. Main Agent Targets

Main Agent responsibilities:

Planning
→ Delegation
→ Replanning
→ Final Summary

### Quality

**Planning Success Rate**

Formula:

`Successful Plans / Runs requiring Planning`

Successful means the plan covers the required Scenario goals and creates executable Tasks.

Baseline: TBD

Target: No Quality Regression

Priority: Critical


**Final Summary Faithfulness**

Formula:

`Faithful Summaries / Runs requiring Final Summary`

A faithful Summary must:

- not invent Bugs
- not change Finding status
- not change metrics
- not invent Evidence
- correctly explain completed, failed and uncertain results

Baseline: TBD

Target: No Quality Regression

Priority: Critical


**Unnecessary Replan Rate**

Formula:

`Unnecessary Replans / Total Replans`

Baseline: TBD

Target: As close to 0 as possible without removing necessary Replans

Priority: Important

---

### Latency

**Main Planning Latency**

Unit: seconds / Run

Baseline: TBD

Target: Improve from Baseline


**Main Replan Latency**

Unit: seconds / Replan

Baseline: TBD

Target: Improve from Baseline


**Main Final Summary Latency**

Unit: seconds / Run

Baseline: TBD

Target: Improve from Baseline

---

### Efficiency

Track:

- Main LLM Requests / Run
- Main Input Tokens / Run
- Main Output Tokens / Run
- Main Cost / Run

Baseline: TBD

Target:

Reduce unnecessary requests, tokens and cost without Quality Regression.

The Final Summary normally remains one LLM request.

---

# 3. Tester Agent Targets

Tester metrics are recorded both:

- Per Task
- Aggregate across all Tester Workers

Bug Precision / Recall are calculated only at Overall System level.

---

### Quality

**Tester Task Success Rate**

Formula:

`Successful Tester Tasks / Attempted Tester Tasks`

A Tester Task is successful when it correctly completes the required checks and records the correct result.

Finding a real Bug can still count as Task Success.

Baseline: TBD

Target: No Quality Regression

Priority: Critical

---

### Latency

Track:

**Tester Latency / Task**

Unit: seconds


**Average Tester Latency / Task**

Formula:

`Total Tester Task Latency / Completed Timed Tasks`


**Longest Tester Latency / Run**

Formula:

`max(Tester Task Latency)`

Baseline: TBD

Target:

Improve without Quality Regression.

Tester latencies must not be added together and treated as Overall latency because Workers run concurrently.

---

### LLM Efficiency

**Tester LLM Requests / Task**

Formula:

`Total Tester LLM Requests / Attempted Tester Tasks`

Baseline: TBD

Target: Reduce from Baseline without Quality Regression

Priority: Critical


Also record:

- Tester LLM Requests / Run
- Tester Input Tokens / Task
- Tester Output Tokens / Task
- Tester Cost / Task

Baseline: TBD

Target:

Improve from Baseline without Quality Regression.

---

### Jev

**Jev Requests / Task**

Formula:

`Total Jev Requests / Attempted Tester Tasks`


**Jev Requests / Run**

Formula:

`Total JEV_CALL events`

Baseline: TBD

Target:

No fixed requirement to reduce Jev calls.

Later optimization may intentionally increase Jev delegation if it reduces expensive Tester LLM reasoning and improves overall latency / cost.

---

# 4. Overall Multi-Agent System Targets

These are the most important final project metrics.

---

### Quality

**End-to-End Task Success Rate**

Formula:

`Successful E2E Runs / Attempted E2E Runs`

A successful Run must satisfy the Scenario requirements and produce the correct final result.

Baseline: TBD

Target: No Quality Regression

Priority: Critical


**Bug Precision**

Formula:

`Correct Confirmed Bugs / All Confirmed Bugs`

Baseline: TBD

Target: No Quality Regression

Priority: Critical


**Bug Recall**

Formula:

`Detected Ground Truth Bugs / Total Enabled Ground Truth Bugs`

Baseline: TBD

Target: No Quality Regression

Priority: Critical


**Reproduction Success Rate**

Formula:

`Successfully Reproduced Findings / Findings Attempted for Reproduction`

Use the fixed stable reproduction requirement defined by the Scenario.

Baseline: TBD

Target: No Quality Regression

Priority: Critical

---

### Latency

**Total Wall-clock Time**

Formula:

`Run Finished Time - Run Started Time`

Includes:

- Main Planning
- Tester execution
- Replanning
- Reproduction
- Verification
- Main Final Summary

Baseline: TBD

Target: Improve from Baseline without Quality Regression

Priority: Critical


**Median Runtime**

Only report when at least 3 comparable Runs exist.

Baseline: TBD

Target: Improve from Baseline

Do not report P95 with a small sample.

---

### Multi-Agent Parallelism

**Peak Concurrent Testers**

Target:

Reach 3 when the Scenario provides at least 3 independent ready Tasks.


**Concurrency Utilization**

Formula:

`Total active Tester time / (Tester capacity × exploration window)`

Baseline: TBD

Target: Improve when comparable parallel workload exists.


**Parallel Speedup**

Formula:

`1-Tester Wall-clock / 3-Tester Wall-clock`

Baseline: TBD

Target:

Demonstrate real speedup without Quality Regression.

This requires a controlled 1-vs-3 Tester experiment.

---

### Overall Efficiency

Track:

- Total LLM Requests
- Total Jev Requests
- Total Input Tokens
- Total Output Tokens
- Total Cost / Run

Where:

`Total LLM Requests = Main + Tester + Optional Visual`

Jev is recorded separately and must not be double-counted.

Baseline: TBD

Targets:

- Reduce Total LLM Requests
- Reduce Tokens
- Reduce Cost
- Reduce Wall-clock Time
- Maintain Quality Guardrails

---

# 5. Hard Guardrails

Optimization is not successful if it improves latency or cost by reducing test quality.

The following must be protected:

- Main Planning Success
- Tester Task Success
- End-to-End Task Success
- Bug Precision
- Bug Recall
- Reproduction Success
- Final Summary Faithfulness

Do not improve metrics by:

- skipping difficult Tasks
- reducing required checks
- removing failed Runs
- changing Ground Truth
- changing Model / Provider
- lowering reproduction requirements
- disabling Verification

---

# 6. Structural Latency Optimization Targets

This phase starts only after Baseline.

Primary optimization targets:

1. Tester LLM Requests / Task
2. Total LLM Requests
3. Total Wall-clock Time
4. Tester Latency / Task
5. Input / Output Tokens
6. Total Cost
7. Concurrency Utilization

Main planned optimization:

`LLM per Action`
→
`LLM per Subgoal`

Move more deterministic page interaction to:

`Jev + Playwright`

while Tester LLM focuses on:

- testing strategy
- semantic decisions
- unexpected situations
- important Findings

Jev Requests may increase if Overall LLM Requests, latency and cost improve.

Do not implement this before Baseline.

---

# 7. LangSmith Usage Policy

LangSmith account has limited free usage.

Use LangSmith only for:

- a small number of selected Baseline Runs
- important before/after Optimization Runs
- specific Debug Runs

Tracing OFF for:

- Unit Tests
- Fake Provider tests
- bulk repeated experiments
- normal Regression runs

Do not create:

- LangSmith Dataset
- LangSmith Evaluator
- LLM Judge
- separate LangSmith Evaluation system

Local Evaluation results remain the official source.

When comparing latency, both runs must use the same Tracing setting.

---

# 8. Baseline Output Requirements

For every formal Baseline Run, record at minimum:

### Main Agent

- Planning Success
- Summary Faithfulness
- Replan count
- Planning latency
- Final Summary latency
- LLM requests
- Tokens
- Cost

### Tester Agents

- Task Success
- Latency / Task
- LLM Requests / Task
- Jev Requests / Task
- Tokens / Task
- Cost / Task

### Overall

- E2E Success
- Bug Precision
- Bug Recall
- Reproduction Success
- Wall-clock Time
- Peak Concurrent Testers
- Concurrency Utilization
- Total LLM Requests
- Total Jev Requests
- Total Tokens
- Total Cost

---

# 9. Resume Metric Candidates

Final resume should select only the strongest 3–5 measured numbers.

Primary candidates:

1. End-to-End Task Success Rate
2. Bug Recall with measured Bug Precision
3. Reproduction Success Rate
4. 1-vs-3 Tester Parallel Speedup
5. Tester LLM Requests / Task Reduction

Useful supporting metrics:

- Wall-clock Latency Reduction
- Token Reduction
- Cost Reduction

Do not use Fake / Smoke results as resume metrics.

Do not fill optimization percentages until real Baseline and Final results exist.

---

# 10. Current Status

Architecture: Fixed

Model / Provider: Fixed

Evaluation Targets: Defined

Baseline: TBD

Next:

Scenario + Ground Truth
→ Baseline
→ Structural Latency Optimization
→ Quality Optimization
→ Regression
→ Final Optimization