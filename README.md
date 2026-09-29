# Multi-Agent Web Testing System

This project is a local demonstration of Main Agent planning, identical Tester Agent collaboration, Playwright browser control, shared SQLite test state, deterministic reproduction, verification, and evidence-based reporting.

## Local Demo App

The Demo App is intentionally small and exists only as a safe Agent-testing target.

```powershell
.\.venv\Scripts\python.exe -m demo_app
```

Open `http://127.0.0.1:8000`. Fixed local test identities are `admin`, `member`, and `member2`; their matching test-only passwords are visible in `demo_app/app.py` and are not real credentials.

Seeded bugs B1 through B6 are disabled by default. Enable only the required switch in the local process environment, for example:

```powershell
$env:DEMO_BUG_B1 = "true"
.\.venv\Scripts\python.exe -m demo_app
```

The reset endpoint is `POST /test/reset` and is available only when the server is started in testing mode. The version endpoint is `GET /version`.

## Low-Cost Verification

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Development tests use Fake providers. They do not call real Gemini, Groq, Laya, or Computer Use services, and `FULL_EVALUATION` remains disabled by default.

## Metrics and Evaluation

`MetricsCalculator` derives run, finding, browser, latency, cost, recovery, stability, and report-usability metrics from SQLite Shared State. Ground Truth is supplied only after a run for recall and false-positive calculation. Rates with no denominator are reported as `N/A`.

The six controlled comparison modes are:

1. Single Tester vs Multi-Tester
2. Laya/Jev vs LLM Every Decision
3. Shared State vs Independent Testers
4. Auto Reproduction On vs Off
5. Playwright-first vs Model-every-step
6. Computer Use Fallback On vs Off

Each comparison keeps the Demo version, seeded bugs, model configuration, budgets, accounts, initial data, and scope fixed. Every run resets the Demo state and writes to an independent evidence directory. Raw results and Run 1/2/3, median, minimum, and maximum summaries are stored under the selected evaluation output directory.

Full Evaluation has three independent gates: `FULL_EVALUATION=true`, an explicit caller decision, and successful Task 23 plus final functional acceptance. Model-every-step is also rejected outside Full Evaluation. Normal test commands cannot start the full experiment matrix.

## Real Provider Configuration

Real provider clients are created from `Settings`; API keys and model names are never embedded in Agent code. The current low-cost configuration uses Gemini through `agent-framework-gemini`, Groq's OpenAI-compatible endpoint through `agent-framework-openai`, and the local Laya `english` checkpoint.

```ini
MAIN_AGENT_PROVIDER=gemini
MAIN_AGENT_MODEL=gemini-3.5-flash
TESTER_AGENT_PROVIDER=groq
TESTER_AGENT_MODEL=openai/gpt-oss-20b
GROQ_BASE_URL=https://api.groq.com/openai/v1
LAYA_MODEL=english
COMPUTER_USE_PROVIDER=gemini
COMPUTER_USE_MODEL=gemini-3.5-flash
FULL_EVALUATION=false
```

Gemini Computer Use sends one screenshot to the configured Gemini model, accepts only the requested click or drag action, rejects safety-confirmation and unsupported actions, converts normalized coordinates to viewport pixels, and returns control to Playwright after state resynchronization.
