#!/usr/bin/env python3
"""Run a WAJ AI Engine test suite and fail when the pass rate is too low.

Talks to the engine's admin MCP server (`<engine>/mcp`, streamable HTTP) with
plain JSON-RPC. Python standard library only: no install step, no build step.

Inputs are read from `INPUT_*` environment variables (see action.yml).
Exit codes: 0 = passed, 1 = below the pass rate / run errored / regression,
2 = configuration, authentication or engine error, 3 = timed out.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

PROTOCOL_VERSION = "2025-03-26"
CLIENT_NAME = "waj-ai-engine-test-action"
CLIENT_VERSION = "1.0.0"
# agent_eval_runs.status values the engine ends a run with.
TERMINAL_STATUSES = ("passed", "failed", "errored", "cancelled")
FINISHED_STATUSES = ("passed", "failed")
DRAFT_NOTE = "CI draft (ai-engine-test-action)"

EXIT_OK, EXIT_FAILED, EXIT_ERROR, EXIT_TIMEOUT = 0, 1, 2, 3


class ActionError(Exception):
    """A problem the user can act on. Its message is shown as-is."""


class AuthError(ActionError):
    pass


class TransportError(ActionError):
    """The engine could not be reached or answered with a server error."""


# --------------------------------------------------------------------------
# MCP over streamable HTTP
# --------------------------------------------------------------------------

def parse_sse(body: str) -> List[Dict[str, Any]]:
    """Return the JSON messages carried in a `text/event-stream` body."""
    messages: List[Dict[str, Any]] = []
    data_lines: List[str] = []

    def flush() -> None:
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        data_lines.clear()
        try:
            parsed = json.loads(payload)
        except ValueError:
            return
        if isinstance(parsed, list):
            messages.extend(m for m in parsed if isinstance(m, dict))
        elif isinstance(parsed, dict):
            messages.append(parsed)

    for raw in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw == "":
            flush()
        elif raw.startswith(":"):
            continue
        elif raw.startswith("data:"):
            value = raw[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    flush()
    return messages


class McpClient:
    """The few parts of MCP streamable HTTP this action needs."""

    def __init__(self, engine_url: str, api_key: str, timeout: float = 60.0):
        base = engine_url.strip().rstrip("/")
        if base.endswith("/mcp"):
            base = base[: -len("/mcp")]
        if not base.startswith(("http://", "https://")):
            raise ActionError("engine-url must start with http:// or https://")
        self.base_url = base
        self.endpoint = base + "/mcp"
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._session_id: Optional[str] = None
        self._protocol: Optional[str] = None
        self._next_id = 0

    def _post(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(message).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = session
                content_type = (resp.headers.get("Content-Type") or "").lower()
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            exc.close()
            if exc.code in (401, 403):
                raise AuthError(
                    f"The engine refused the API key (HTTP {exc.code}): {detail}. "
                    "Use a key with Full access (the admin scope) from "
                    "Settings > API keys.") from None
            if exc.code == 404:
                raise ActionError(
                    f"{self.endpoint} was not found (HTTP 404). Check engine-url, "
                    "and that the engine's MCP server is turned on.") from None
            if exc.code >= 500:
                raise TransportError(f"The engine answered HTTP {exc.code}: {detail}") from None
            raise ActionError(f"The engine answered HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"Could not reach {self.endpoint}: {reason}") from None

        if "id" not in message:  # a notification: no reply is expected
            return None
        if "text/event-stream" in content_type:
            candidates = parse_sse(body)
        else:
            try:
                parsed = json.loads(body) if body.strip() else None
            except ValueError:
                raise TransportError(
                    "The engine's reply was not JSON. Check engine-url.") from None
            candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("id") == message["id"]:
                return candidate
        raise TransportError("The engine's reply did not answer the request.")

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        reply = self._post({"jsonrpc": "2.0", "id": self._next_id,
                            "method": method, "params": params}) or {}
        if reply.get("error"):
            error = reply["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise ActionError(f"The engine refused {method}: {message}")
        return reply.get("result") or {}

    def initialize(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        })
        self._protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool and return its JSON payload. An `{"error": ...}`
        payload (how the engine reports a refused request) raises."""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(
            block.get("text", "") for block in result.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text")
        if result.get("isError"):
            raise ActionError(f"{name} failed: {_short(text, 500) or 'no detail'}")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(text) if text.strip() else {}
            except ValueError:
                raise ActionError(f"{name} returned text that is not JSON.") from None
        if isinstance(payload, dict) and set(payload) == {"result"} \
                and isinstance(payload["result"], dict):
            payload = payload["result"]
        if not isinstance(payload, dict):
            raise ActionError(f"{name} returned an unexpected reply.")
        if payload.get("error"):
            hint = payload.get("hint")
            raise ActionError(f"{name}: {payload['error']}" + (f" ({hint})" if hint else ""))
        return payload


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        body = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            body = str(parsed.get("error") or parsed.get("detail") or body)
    except ValueError:
        pass
    return _short(body, 300) or (exc.reason or "no detail")


# --------------------------------------------------------------------------
# Resolving the inputs to engine ids
# --------------------------------------------------------------------------

def _pick(rows: List[Dict[str, Any]], wanted: str, id_key: str,
          name_keys: Tuple[str, ...], what: str,
          prefer: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Dict[str, Any]:
    wanted_clean = wanted.strip()
    for row in rows:
        if str(row.get(id_key) or "") == wanted_clean:
            return row
    lowered = wanted_clean.lower()
    matches = [r for r in rows
               if any(str(r.get(k) or "").strip().lower() == lowered for k in name_keys)]
    if len(matches) > 1 and prefer:
        preferred = [r for r in matches if prefer(r)]
        if preferred:
            matches = preferred
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = sorted({str(r.get(name_keys[0]) or "") for r in rows} - {""})
        listed = ", ".join(known[:15]) or "none"
        raise ActionError(f"No {what} matches {wanted_clean!r}. Found: {listed}.")
    ids = ", ".join(str(r.get(id_key)) for r in matches)
    raise ActionError(
        f"{len(matches)} {what}s match {wanted_clean!r}. Use the id instead: {ids}.")


def resolve_suite(client: McpClient, suite: str, env: str) -> Dict[str, Any]:
    rows = client.call_tool("eval_suites", {"action": "list", "env": env}).get("suites") or []
    return _pick(rows, suite, "suite_id", ("name",), "test suite")


def resolve_profile(client: McpClient, agent: str, suite: Dict[str, Any],
                    env: str) -> Dict[str, Any]:
    payload = client.call_tool("list_profiles", {"env": env, "limit": 500})
    rows = payload.get("profiles") or []
    if agent.strip():
        kind = suite.get("agent_kind")
        return _pick(rows, agent, "profile_id", ("slug", "display_name"), "agent",
                     prefer=lambda r: r.get("agent_kind") == kind)
    default_id = suite.get("profile_id")
    if not default_id:
        raise ActionError(
            f"The suite {suite.get('name')!r} has no default agent. Set the `agent` input.")
    for row in rows:
        if row.get("profile_id") == default_id:
            return row
    return {"profile_id": default_id, "slug": None, "agent_kind": suite.get("agent_kind")}


def resolve_model(client: McpClient, model: str, env: str) -> Dict[str, Any]:
    rows = client.call_tool("models", {"action": "list", "env": env}).get("models") or []
    return _pick(rows, model, "model_id", ("name", "model_string"), "model")


def prepare_instructions(client: McpClient, profile: Dict[str, Any], path: str,
                         env: str, note: str) -> Tuple[Dict[str, int], str]:
    """Store the file as a new, unpublished version of the agent's
    instructions and return the `prompt_overrides` that test it.

    The engine tests a draft by version number, so the text has to be saved
    as a version first. The version is NOT pinned: the agent keeps serving
    what it served before.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise ActionError(f"Could not read instructions-file {path!r}: {exc.strerror}") from None
    if not content.strip():
        raise ActionError(f"instructions-file {path!r} is empty.")

    profile_id = profile["profile_id"]
    served = client.call_tool("prompts", {
        "action": "get", "profile_id": profile_id, "prompt_key": "system", "env": env})
    prompt_id = served.get("prompt_id")
    if not prompt_id:
        raise ActionError("The agent has no instructions to test a draft against.")
    kind, slug = profile.get("agent_kind"), profile.get("slug")
    # A generic agent owns a prompt named "<agent slug>:system"; other kinds
    # share "system". A draft saved anywhere else would be ignored by the run.
    expected_slug = f"{slug}:system" if kind == "custom" and slug else "system"
    if kind and (served.get("agent_kind") != kind or served.get("slug") != expected_slug):
        raise ActionError(
            "This agent still uses its template's instructions, so a draft cannot be "
            "tested against it. Save the agent's instructions once in the portal, "
            "then run again.")
    if (served.get("content") or "") == content:
        return {}, "The file matches the instructions the agent already serves."

    newest = client.call_tool("prompts", {
        "action": "get", "prompt_id": prompt_id, "env": env})
    if (newest.get("content") or "") == content and newest.get("version_number"):
        version = int(newest["version_number"])
        return {"system": version}, f"Draft instructions: version {version} (already saved)."
    created = client.call_tool("prompts", {
        "action": "create_version", "prompt_id": prompt_id,
        "content": content, "notes": note, "env": env})
    version = created.get("version_number")
    if not version:
        raise ActionError("The engine did not save the draft instructions.")
    return {"system": int(version)}, f"Draft instructions: saved as version {int(version)}, not published."


# --------------------------------------------------------------------------
# Running and reading a run
# --------------------------------------------------------------------------

def find_baseline(client: McpClient, suite_id: str, profile_id: str,
                  env: str) -> Optional[str]:
    runs = client.call_tool("eval_runs", {
        "action": "list", "suite_id": suite_id, "profile_id": profile_id,
        "kind": "text", "limit": 20, "env": env}).get("runs") or []
    for run in runs:
        if run.get("status") in FINISHED_STATUSES:
            return run.get("run_id")
    return None


def wait_for_run(client: McpClient, run_id: str, env: str, timeout_s: float,
                 poll_s: float, log: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    """Poll until the run reaches a final status. None means timed out."""
    deadline = time.monotonic() + timeout_s
    last_status, failures = None, 0
    while True:
        try:
            run = client.call_tool("eval_runs", {
                "action": "get", "run_id": run_id, "env": env}).get("run") or {}
            failures = 0
        except TransportError as exc:
            failures += 1
            if failures >= 5:
                raise
            log(f"Could not read the run ({exc}); trying again.")
            run = {}
        status = run.get("status")
        if status and status != last_status:
            log(f"Run {run_id}: {status}")
            last_status = status
        if status in TERMINAL_STATUSES:
            return run
        if time.monotonic() + poll_s > deadline:
            return None
        time.sleep(poll_s)


def case_reason(result: Dict[str, Any]) -> str:
    """One short line saying why a case failed (or was not graded)."""
    if result.get("error"):
        return f"error: {result['error']}"
    parts: List[str] = []
    if result.get("missing_substrings"):
        parts.append("missing: " + ", ".join(map(str, result["missing_substrings"])))
    if result.get("missing_expected_tools"):
        parts.append("did not call: " + ", ".join(map(str, result["missing_expected_tools"])))
    if result.get("forbidden_tools_used"):
        parts.append("called a forbidden tool: "
                     + ", ".join(map(str, result["forbidden_tools_used"])))
    if result.get("errored_expected_tools"):
        parts.append("tool returned an error: "
                     + ", ".join(map(str, result["errored_expected_tools"])))
    if result.get("reply_check_failures"):
        parts.append("; ".join(map(str, result["reply_check_failures"])))
    for check in result.get("checks") or []:
        if isinstance(check, dict) and check.get("passed") is False:
            parts.append(str(check.get("reason") or check.get("detail")
                             or check.get("type") or "a check failed"))
    judge = result.get("judge")
    if not parts and isinstance(judge, dict) and judge.get("rationale"):
        parts.append(str(judge["rationale"]))
    status = result.get("http_status")
    if not parts and isinstance(status, int) and not 200 <= status < 300:
        parts.append(f"the agent answered HTTP {status}")
    if not parts and result.get("grading_note"):
        parts.append(str(result["grading_note"]))
    if not parts and result.get("ungraded"):
        parts.append("no judge model and no expected words to check")
    return "; ".join(parts)


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts and the pass rate. A case the engine could not grade is
    reported but left out of the rate, as in the portal."""
    cases = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        if result.get("passed"):
            outcome = "pass"
        elif result.get("ungraded"):
            outcome = "not judged"
        else:
            outcome = "fail"
        name = (result.get("name") or result.get("scenario_id") or result.get("id")
                or f"case {index + 1}")
        if result.get("attempt") is not None and len(results) > 1:
            name = f"{name} (attempt {result['attempt']})"
        cases.append({"name": str(name), "outcome": outcome,
                      "reason": "" if outcome == "pass" else case_reason(result),
                      "latency_ms": result.get("latency_ms")})
    passed = sum(1 for c in cases if c["outcome"] == "pass")
    failed = sum(1 for c in cases if c["outcome"] == "fail")
    ungraded = sum(1 for c in cases if c["outcome"] == "not judged")
    graded = passed + failed
    rate = round(100.0 * passed / graded, 1) if graded else None
    return {"cases": cases, "passed": passed, "failed": failed,
            "ungraded": ungraded, "total": len(cases), "pass_rate": rate}


def find_regressions(client: McpClient, baseline_id: str, run_id: str,
                     env: str) -> List[str]:
    compared = client.call_tool("eval_runs", {
        "action": "compare", "run_ids": [baseline_id, run_id], "env": env})
    return [str(s.get("scenario_id")) for s in compared.get("scenarios") or []
            if isinstance(s, dict) and s.get("label") == "regressed"]


# --------------------------------------------------------------------------
# GitHub output
# --------------------------------------------------------------------------

def _short(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _cell(text: Any, limit: int = 160) -> str:
    return _short(text, limit).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "n/a"
    return f"{rate:g}%"


def build_summary(*, suite_name: str, agent_label: str, run_id: str, run_url: str,
                  status: str, summary: Dict[str, Any], min_rate: float, ok: bool,
                  notes: List[str], regressions: Optional[List[str]],
                  baseline_id: Optional[str], error: Optional[str]) -> str:
    verdict = "Passed" if ok else "Failed"
    lines = [f"## Agent tests: {_cell(suite_name)}", ""]
    headline = (f"**{verdict}.** {summary['passed']} of "
                f"{summary['passed'] + summary['failed']} cases passed "
                f"({_fmt_rate(summary['pass_rate'])}). Required: {min_rate:g}%.")
    if summary["ungraded"]:
        headline += f" {summary['ungraded']} not judged (left out of the rate)."
    lines += [headline, ""]
    lines.append(f"- Agent: {_cell(agent_label)}")
    lines.append(f"- Run: [{run_id}]({run_url}) (engine status: `{status}`)")
    for note in notes:
        lines.append(f"- {_cell(note, 300)}")
    if error:
        lines.append(f"- Error: {_cell(error, 400)}")
    if regressions is not None:
        if regressions:
            lines.append(f"- Broke since the previous run (`{baseline_id}`): "
                         + ", ".join(f"`{_cell(r, 80)}`" for r in regressions))
        else:
            lines.append(f"- Nothing broke since the previous run (`{baseline_id}`).")
    if summary["cases"]:
        icon = {"pass": "✅ pass", "fail": "❌ fail", "not judged": "⚪ not judged"}
        lines += ["", "| Case | Result | Reason |", "|---|---|---|"]
        ordered = sorted(summary["cases"],
                         key=lambda c: {"fail": 0, "not judged": 1, "pass": 2}[c["outcome"]])
        for case in ordered:
            lines.append(f"| {_cell(case['name'], 100)} | {icon[case['outcome']]} "
                         f"| {_cell(case['reason'])} |")
    return "\n".join(lines) + "\n"


_ENV: Dict[str, str] = {}


def _append(path_var: str, text: str) -> None:
    path = _ENV.get(path_var)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def set_outputs(values: Dict[str, Any]) -> None:
    lines = []
    for key, value in values.items():
        if value is None:
            value = ""
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{key}={str(value).splitlines()[0] if str(value) else ''}\n")
    _append("GITHUB_OUTPUT", "".join(lines))


def _annotation(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _input(env: Dict[str, str], name: str, default: str = "") -> str:
    return (env.get("INPUT_" + name.upper().replace("-", "_")) or default).strip()


def _number(env: Dict[str, str], name: str, default: str, low: float, high: float) -> float:
    raw = _input(env, name, default)
    try:
        value = float(raw)
    except ValueError:
        raise ActionError(f"{name} must be a number, got {raw!r}.") from None
    if not low <= value <= high:
        raise ActionError(f"{name} must be between {low:g} and {high:g}, got {raw!r}.")
    return value


def _flag(env: Dict[str, str], name: str) -> bool:
    return _input(env, name).lower() in ("1", "true", "yes", "on")


def run(env: Dict[str, str], log: Callable[[str], None]) -> int:
    engine_url = _input(env, "engine-url")
    api_key = _input(env, "api-key")
    suite_input = _input(env, "suite")
    missing = [n for n, v in (("engine-url", engine_url), ("api-key", api_key),
                              ("suite", suite_input)) if not v]
    if missing:
        raise ActionError("Missing required input: " + ", ".join(missing) + ".")
    min_rate = _number(env, "min-pass-rate", "100", 0, 100)
    timeout_s = _number(env, "timeout-minutes", "30", 0.01, 1440) * 60
    poll_s = _number(env, "poll-interval-seconds", "5", 0.01, 600)
    engine_env = _input(env, "environment", "staging")
    if engine_env not in ("staging", "prod"):
        raise ActionError("environment must be staging or prod.")
    fail_on_regression = _flag(env, "fail-on-regression")

    client = McpClient(engine_url, api_key)
    client.initialize()

    suite = resolve_suite(client, suite_input, engine_env)
    suite_id, suite_name = suite["suite_id"], suite.get("name") or suite["suite_id"]
    profile = resolve_profile(client, _input(env, "agent"), suite, engine_env)
    profile_id = profile["profile_id"]
    agent_label = profile.get("display_name") or profile.get("slug") or profile_id
    log(f"Suite: {suite_name} ({suite_id})")
    log(f"Agent: {agent_label} ({profile_id})")

    notes: List[str] = []
    arguments: Dict[str, Any] = {"profile_id": profile_id, "source": "suite",
                                 "suite_id": suite_id, "env": engine_env}
    labels: List[str] = []
    model_input = _input(env, "model")
    if model_input:
        model = resolve_model(client, model_input, engine_env)
        arguments["model_id"] = model["model_id"]
        labels.append(str(model.get("name") or model_input))
        notes.append(f"Model: {model.get('name') or model_input}")
    instructions_file = _input(env, "instructions-file")
    if instructions_file:
        sha = (env.get("GITHUB_SHA") or "")[:12]
        note = DRAFT_NOTE + (f" {sha}" if sha else "")
        overrides, message = prepare_instructions(
            client, profile, instructions_file, engine_env, note)
        notes.append(message)
        log(message)
        if overrides:
            arguments["prompt_overrides"] = overrides
            labels.append(f"draft v{overrides['system']}")
    if labels:
        arguments["variant_label"] = _short("CI: " + ", ".join(labels), 80)

    baseline_id = find_baseline(client, suite_id, profile_id, engine_env)

    run_id = client.call_tool("trigger_eval_run", arguments).get("run_id")
    if not run_id:
        raise ActionError("The engine did not return a run id.")
    run_url = f"{client.base_url}/admin/evals/runs/{run_id}"
    log(f"Started run {run_id}")
    log(f"Run page: {run_url}")
    set_outputs({"run-id": run_id, "run-url": run_url})

    finished = wait_for_run(client, run_id, engine_env, timeout_s, poll_s, log)
    if finished is None:
        message = (f"The run did not finish within {timeout_s / 60:g} minutes. "
                   f"It may still be running: {run_url}")
        log(f"::error title=Agent tests timed out::{_annotation(message)}")
        _append("GITHUB_STEP_SUMMARY",
                f"## Agent tests: {_cell(suite_name)}\n\n**Timed out.** {message}\n")
        set_outputs({"status": "timeout"})
        return EXIT_TIMEOUT

    full = client.call_tool("eval_runs", {
        "action": "get", "run_id": run_id, "include_results": True,
        "env": engine_env}).get("run") or finished
    status = str(full.get("status") or finished.get("status"))
    summary = summarize(full.get("results") or [])
    error = full.get("error") if status in ("errored", "cancelled") else None

    regressions: Optional[List[str]] = None
    if baseline_id and status in FINISHED_STATUSES:
        try:
            regressions = find_regressions(client, baseline_id, run_id, engine_env)
        except ActionError as exc:
            notes.append(f"Could not compare with the previous run: {exc}")
    elif not baseline_id:
        notes.append("No previous finished run of this suite and agent to compare with.")

    problems: List[str] = []
    if status == "errored":
        problems.append(f"the run errored: {_short(error, 300) or 'no detail'}")
    elif status == "cancelled":
        problems.append("the run was cancelled")
    elif summary["pass_rate"] is None:
        problems.append("no case could be graded")
    elif summary["pass_rate"] < min_rate:
        problems.append(f"pass rate {_fmt_rate(summary['pass_rate'])} is below "
                        f"the required {min_rate:g}%")
    if fail_on_regression and regressions:
        problems.append(f"{len(regressions)} case(s) broke since the previous run: "
                        + ", ".join(regressions[:10]))
    ok = not problems

    _append("GITHUB_STEP_SUMMARY", build_summary(
        suite_name=suite_name, agent_label=str(agent_label), run_id=run_id,
        run_url=run_url, status=status, summary=summary, min_rate=min_rate, ok=ok,
        notes=notes, regressions=regressions, baseline_id=baseline_id, error=error))
    set_outputs({
        "status": status,
        "pass-rate": "" if summary["pass_rate"] is None else f"{summary['pass_rate']:g}",
        "passed": summary["passed"], "failed": summary["failed"],
        "not-judged": summary["ungraded"], "total": summary["total"],
        "regressed": "" if regressions is None else len(regressions),
    })

    log(f"{summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['ungraded']} not judged. Pass rate: {_fmt_rate(summary['pass_rate'])} "
        f"(required {min_rate:g}%).")
    for case in summary["cases"]:
        if case["outcome"] != "pass":
            log(f"  {case['outcome']}: {case['name']}: {_short(case['reason'], 200)}")
    if not ok:
        message = "; ".join(problems)
        log(f"::error title=Agent tests failed::{_annotation(message[0].upper() + message[1:])}")
        return EXIT_FAILED
    return EXIT_OK


def main(env: Optional[Dict[str, str]] = None) -> int:
    env = dict(os.environ if env is None else env)
    _ENV.clear()
    _ENV.update(env)

    def log(message: str) -> None:
        print(message, flush=True)

    try:
        return run(env, log)
    except ActionError as exc:
        log(f"::error title=Agent tests could not run::{_annotation(str(exc))}")
        _append("GITHUB_STEP_SUMMARY", f"## Agent tests\n\n**Could not run.** {_cell(exc, 600)}\n")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
