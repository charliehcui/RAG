# Multi-Agent Web Testing System

A Python Agent / CLI application using a Main Manager, identical Tester Workers (three concurrent slots by default), isolated Chromium contexts, SQLite Shared State, deterministic reproduction and verification, and evidence-based reporting.

## Run locally

Use Python 3.13 and install the project with its development dependencies. Start the local target with python -m demo_app, then run:

    .\.venv\Scripts\python.exe -m web_testing_system path\to\run_config.json

The workflow entry point is orchestration/runner.py. Structured facts are saved under artifacts/runs/<run_id>/report.json. The same Main Agent reviews those facts in one tool-free final model call, writes summary.md, and supplies the CLI response. Ordinary tests do not start formal Baseline or Evaluation.

## Fixed paid OpenRouter models

Only OPENROUTER_API_KEY is required for production model access. Copy .env.example to .env and supply the key locally. No direct Google/Gemini or Groq client remains.

| Role | Primary model | Fixed provider endpoint | Manual backup model / endpoint |
|---|---|---|---|
| Main | deepseek/deepseek-v4-flash | streamlake/fp8 | deepseek/deepseek-v3.2 / deepinfra/fp4 |
| Tester | z-ai/glm-5.3-flash | relace | openai/gpt-oss-20b / darkbloom/fp8 |
| Jev | typesafe/jev-1.13 | Existing OpenRouter Decisions API | Unchanged |

Every Agent request pins one endpoint using provider.only, provider.order, allow_fallbacks=false and require_parameters=true. There is no price router, floor variant, automatic model fallback or SDK retry. Backup settings are informational: manual switching requires replacing the primary model AND provider, then starting a separate experiment series. Keep both pairs fixed throughout Baseline, Evaluation, Regression and Final Optimization.

Catalog verified on 2026-10-06. Prices in USD per million input/output tokens: Main StreamLake $0.042/$0.084; Tester Relace $0.0352/$0.50. No provider is cheapest for every input/output ratio: Main Relace costs $0.03/$1.28; Tester DeepInfra costs $0.075/$0.25. Main StreamLake is cheaper than Relace below about 99.67 input tokens per output token. Tester Relace is cheaper than DeepInfra above about 6.28 input tokens per output token. These are selection assumptions, not measured Baseline results. Prices can change even when endpoints remain pinned.

Sources: [Main endpoints](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-flash/endpoints), [Tester endpoints](https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints), [provider pinning](https://openrouter.ai/docs/guides/routing/provider-selection).

Main and Tester use tool calls rather than JSON response-format output. Selected endpoints advertise tools and tool-choice support. Free/routing model variants are rejected. Fake providers remain available for deterministic tests.

## Workflow and task outcomes

    RunConfig -> Main planning -> SQLite Tasks -> continuous AsyncIO scheduling
    -> task-specific Tester context -> isolated BrowserContext
    -> Runtime -> Playwright / Page State -> Candidates -> Jev -> validation -> Playwright
    -> Finding + observation evidence
    -> deterministic reproduction -> existing VerificationRunner
    -> CONFIRMED_BUG / CLOSED / NEEDS_CONFIRMATION -> structured report.json
    -> same Main Manager final review -> summary.md -> CLI user response

The scheduler fills free slots with priority-ordered dependency-ready Tasks. max_testers and max_parallel_browser_contexts default to 3 and accept 1-4. Each ready Task gets a Tester instance with a private session, BrowserContext/Page and assignment; up to three run together by default when independent work exists. Instance count across an entire run can exceed concurrent capacity as new Tasks become ready. Replanning occurs only for relevant requests/high-risk findings while pending work remains; duplicate reasons are suppressed. Reproduction runs after exploration so application-wide resets cannot corrupt active Tasks.

Execution status COMPLETED means execution ended. Success status PASS/FAIL/UNKNOWN is calculated by Python from actual goal assertions. Mark goal assertions with goal_check=true or the supplied behavior_id. Every declared expected behavior needs a recorded check before PASS is possible; missing checks yield UNKNOWN. Discovering a bug can produce completed execution with FAIL expected behavior, which differs from an execution failure. The finish_task tool records the outcome and ends the loop without a final model Done response.

One Runtime page lock serializes browser-changing operations and decisions for the same Page. Independent Shared State reads may run concurrently.

## Task handoff and inputs

Main Tasks carry data_requirements.identity_reference, expected_behavior_ids and test_data_keys. Tester prompts receive the current goal, role, identity/secret references, relevant expected behaviors, data reference names, scope, denied operations, namespace and step budget. Input values are obtained with read_test_data or passed to Runtime by value_reference. Dynamic coverage, findings and progress are read through tools rather than copied into the handoff.

When accounts share a role, specify identity_reference; ambiguous selection is rejected. Account secret_reference supports env:VARIABLE_NAME. Runtime resolves the allowed reference in memory and fills it through value_reference. Secrets are not returned by read_test_data, placed in prompts or saved in action history. Supply non-secret usernames through test_data and select their keys in the Task handoff.

Known INPUT/SELECT actions should use valid value_reference values, resolved to values by Runtime and preserved as references for replay. Callers must supply application-specific inputs and expected behavior; absent credentials and test oracles cannot be inferred.

Exploration and replay have separate step budgets: max_browser_steps_per_task (default 50) and max_replay_steps_per_finding (default 200). Default automatic reproduction needs two matching attempts, at most three, followed by verification. Automatic step minimization is disabled in this workflow; the existing component remains for explicit use. Only assertion failure during verification confirms a bug; broken setup paths remain unconfirmed.

Reports retain every Finding status and include task_outcomes, model_configuration, peak concurrency, provider-returned tokens/cost, and incomplete-cost status. SQLite stores evidence metadata; files contain screenshots, DOM, network summaries, console errors and replay traces.

Python produces all testing facts. The final Main review has an isolated fact-only session on the same Agent, with planning tools removed for that phase, and cannot mutate Tasks, Findings or evidence. Natural language is advisory; report.json remains authoritative. The summary prompt states that COMPLETED differs from PASS and verification FAIL confirms a failed expected behavior only after reproduction. No additional judge or correction model is added.

cost_and_performance includes final-summary usage and end-to-end wall time; llm_by_phase separates planning, replan, Tester, visual and final-summary requests, tokens, billed cost and latency. testing_phase_cost_and_performance preserves the metrics supplied to Main before the final call. main_final_summary records the summary status and artifact reference. Final summary requires available model/time/token budget; on failure, the structured report is retained and the run fails explicitly without automatic fallback.

## LangSmith observability

Set LANGSMITH_API_KEY locally. Tracing is enabled when a key is present; LANGSMITH_TRACING=false disables it. LANGSMITH_PROJECT defaults to multi-agent-web-testing. The pinned official langsmith SDK handles span context, parallel task propagation and background upload; a bounded flush occurs after execution.

    Run
    -> MainPlanning -> LLM / planning tools
    -> concurrent Testers -> LLM / tools -> BrowserAction / Jev / Evidence / optional VisualLLM
    -> conditional MainReplan -> LLM / planning tools
    -> Reproduction / Verification -> BrowserAction
    -> MainFinalSummary -> one LLM request

Only safe metadata is uploaded: run/scenario/task/finding IDs, Agent role, Tester instance, fixed models/providers, action types, outcomes and model usage. Prompts, Tool arguments/results, browser content, screenshots, credentials, test values and natural summaries are omitted. Existing field redaction and registered-value replacement protect metadata; errors contain exception types rather than messages or tracebacks. Connection, span upload and flush failures do not alter Shared State, scheduling or results. There are no LangSmith datasets, judges or evaluation runners.

SDK documentation: [custom instrumentation](https://docs.langchain.com/langsmith/annotate-code), [sensitive trace data](https://docs.langchain.com/langsmith/mask-inputs-outputs), [LLM usage metadata](https://docs.langchain.com/langsmith/log-llm-trace).

## Jev and optional visual actions

Jev is unchanged and remains the default unknown-path selector. It is not merged into the Tester prompt or replaced by an Agent model. Existing explicit evaluation comparison routes remain separate from the production default.

Optional visual actions also use paid OpenRouter. Leave COMPUTER_USE_MODEL empty and max_computer_use_calls=0 when disabled. To enable the migrated client, configure z-ai/glm-5.3-flash with the fixed relace endpoint and a positive budget. One screenshot produces one allowed click/drag; Python checks coordinates and returns control to Playwright. Gateway success alone does not establish visual task quality.

## Deterministic verification

    .\.venv\Scripts\python.exe -m pytest
    .\.venv\Scripts\python.exe -m ruff check src tests
    .\.venv\Scripts\python.exe -m mypy src\web_testing_system

Tests use Fake/Mock providers and local browsers, including a formal-entry E2E smoke test. They do not execute a formal evaluation dataset or call paid models. Real OpenRouter endpoint verification is a separately invoked minimal smoke test.

The demo has B1-B6 switches, disabled by default. POST /test/reset is available only in testing mode. Ground Truth remains isolated until after a run. Formal Evaluation still requires existing gates and explicit enablement; FULL_EVALUATION=false remains the default.
