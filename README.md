# WAJ AI Engine: run agent tests

A GitHub Action that runs a [WAJ AI Engine](https://github.com/waj-1/ai-engine) test suite against an agent and fails the job when the pass rate drops.

WAJ AI Engine is a self-hosted AI agent platform. A test suite is a list of cases. Each case has a question and what the answer must satisfy: words it must contain, tools the agent must call, or a rubric for a judge model. You can try the engine at [engine-demo.waj.ai](https://engine-demo.waj.ai).

## What it does

1. Starts a run of the suite on your engine.
2. Waits until the run finishes, or until the timeout.
3. Writes a job summary with one row per case: pass or fail, and a short reason.
4. Sets outputs (run id, pass rate, counts, a link to the run page).
5. Fails the step when the pass rate is below `min-pass-rate`.

The action needs no install step. It is one Python file that uses only the standard library. The runner needs Python 3.9 or newer, which GitHub-hosted runners have.

## Usage

```yaml
name: Agent tests
on: [pull_request]

jobs:
  agent-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: waj-1/ai-engine-test-action@v1
        with:
          engine-url: https://engine.example.com
          api-key: ${{ secrets.ENGINE_API_KEY }}
          suite: Support basics
          min-pass-rate: 90
```

Create the API key in the engine portal under **Settings > API keys**. It must have Full access. Save it as a repository secret. The action sends the key only to your engine and never prints it.

## Inputs

| Input | Required | Default | What it is |
|---|---|---|---|
| `engine-url` | yes | | Base URL of your engine, for example `https://engine.example.com`. |
| `api-key` | yes | | An engine API key with Full access. Pass it from a secret. |
| `suite` | yes | | The test suite: its id or its exact name. |
| `agent` | no | the suite's agent | The agent to test: its slug, name or profile id. |
| `model` | no | the agent's model | Run with another model: its name, model string or id in the engine. |
| `instructions-file` | no | | Path to a draft of the agent's instructions. See below. |
| `min-pass-rate` | no | `100` | Lowest pass rate, in percent, that keeps the job green. |
| `timeout-minutes` | no | `30` | How long to wait for the run to finish. |
| `fail-on-regression` | no | `false` | Also fail when a case that passed in the previous run fails now. |
| `environment` | no | `staging` | The engine environment that holds the agent and the suite: `staging` or `prod`. |
| `poll-interval-seconds` | no | `5` | Seconds between status checks. |

## Outputs

| Output | What it is |
|---|---|
| `run-id` | The id of the test run. |
| `run-url` | The run's page in the engine portal (`<engine-url>/admin/evals/runs/<run-id>`). |
| `status` | The engine's final status: `passed`, `failed`, `errored` or `cancelled`. `timeout` when the action stopped waiting. |
| `pass-rate` | Share of graded cases that passed, in percent. |
| `passed` | Number of cases that passed. |
| `failed` | Number of cases that failed. |
| `not-judged` | Number of cases the engine could not grade. |
| `total` | Number of cases in the run. |
| `regressed` | Number of cases that passed in the previous run and fail now. Empty when there is no previous run. |

## How the result is decided

- The pass rate is the number of passed cases divided by the number of graded cases.
- A case the engine could not grade is shown as "not judged" and left out of the rate. This happens when a case has no expected words and the suite has no judge model. If no case could be graded, the step fails.
- The step fails when the pass rate is below `min-pass-rate`.
- The step fails when the run ends as `errored` or `cancelled`, whatever the threshold.
- The engine also gives each run its own status, from the suite's target score. The action reports it as the `status` output but decides on `min-pass-rate` only.

Exit codes: `0` passed, `1` failed, `2` could not run (bad input, refused key, engine error), `3` timed out. After a timeout the run may still be going on the engine. The action does not cancel it.

## Test a prompt change in a pull request

Keep the agent's instructions in your repository. This workflow tests the changed text before anyone merges it.

```yaml
name: Test instructions
on:
  pull_request:
    paths: ["agents/helpdesk/instructions.md"]

jobs:
  test-draft:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - id: tests
        uses: waj-1/ai-engine-test-action@v1
        with:
          engine-url: ${{ vars.ENGINE_URL }}
          api-key: ${{ secrets.ENGINE_API_KEY }}
          suite: Support basics
          agent: helpdesk
          instructions-file: agents/helpdesk/instructions.md
          min-pass-rate: 90
          fail-on-regression: true
      - if: always()
        run: echo "Pass rate ${{ steps.tests.outputs.pass-rate }}%, run ${{ steps.tests.outputs.run-url }}"
```

What happens to the file:

- The engine tests a draft by version number. So the action saves the file as a new version of the agent's instructions, then runs the suite against that version.
- The new version is not published. The agent keeps serving the version it served before. Publishing stays a separate step that you do in the portal or over MCP.
- If the file equals the newest saved version, that version is reused. If it equals the served text, the run is a normal run.
- Each different draft adds one version to the agent's history, with the note "CI draft" and the commit.
- The agent must have its own instructions. An agent that still uses its template's text is refused with a message. Save its instructions once in the portal to fix that.

Workflows from forks do not receive secrets, so this job cannot run on pull requests from forks.

## About `fail-on-regression`

The action compares the new run with the most recent finished run of the same suite and agent. It uses the engine's own comparison. A case counts as broken when it passed in that run and fails in this one. The summary lists broken cases even when the input is `false`.

The previous run is whatever ran last. It can be a run of another draft or another model. Keep that in mind when several pull requests test the same agent.

## Where the engine runs

### A staging engine

This is the simplest setup. Point `engine-url` at an engine that your runners can reach, and keep your agents and suites there. The runner must be able to open `<engine-url>/mcp`.

### In the job, with Docker Compose

The engine starts with one command and needs no model key to start. These steps start it in a job and get an admin token. The engine accepts that token in place of an API key.

```yaml
    steps:
      - uses: actions/checkout@v4
        with:
          repository: waj-1/ai-engine
          path: engine
      - name: Start the engine
        working-directory: engine
        env:
          ADMIN_EMAIL: ci@example.com
          ADMIN_PASSWORD: ${{ secrets.ENGINE_CI_ADMIN_PASSWORD }}
        run: |
          docker compose up -d
          until curl -fsS http://127.0.0.1:8000/health; do sleep 3; done
          token=$(docker compose exec -T engine sh deploy/compose/with_env.sh python deploy/compose/admin_token.py)
          echo "::add-mask::$token"
          echo "ENGINE_TOKEN=$token" >> "$GITHUB_ENV"
      # Load your agent and your suite here. A new engine has none.
      - uses: waj-1/ai-engine-test-action@v1
        with:
          engine-url: http://127.0.0.1:8000
          api-key: ${{ env.ENGINE_TOKEN }}
          suite: Support basics
```

Things to know:

- A new engine has one demo agent and no test suites. Your job has to create the agent and the suite before the action runs, for example through the engine's MCP server.
- The agent needs a model to answer. The engine's README explains how to give it a provider key or a local model.
- The first `docker compose up` builds the engine image, so the first step is slow.
- The token lasts one hour.
- These steps were tested on a local machine against the engine's compose stack. They have not been run on a GitHub-hosted runner yet.

## Run it outside GitHub Actions

The script reads its inputs from environment variables.

```bash
INPUT_ENGINE_URL=https://engine.example.com \
INPUT_API_KEY=wae_... \
INPUT_SUITE="Support basics" \
INPUT_MIN_PASS_RATE=90 \
python3 run_tests.py
```

## How it talks to the engine

The action calls the engine's admin MCP server at `<engine-url>/mcp` with plain JSON-RPC over HTTP. It uses the tools `eval_suites`, `list_profiles`, `models`, `prompts`, `trigger_eval_run` and `eval_runs`. The engine's portal pages under `/admin` need a browser sign-in and do not accept an API key, so MCP is the interface for automation.

The API key has full control of the engine. Use a key made for CI, and prefer a staging engine.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests start a small fake engine with `http.server`. They cover a passing run, a run below the threshold, a timeout, refused keys, and replies framed as `text/event-stream`.

## License

Apache-2.0. See [LICENSE](LICENSE).
