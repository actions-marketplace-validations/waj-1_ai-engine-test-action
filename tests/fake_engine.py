"""A tiny stand-in for the engine's `/mcp` endpoint, for the unit tests.

Standard library only. It speaks enough MCP streamable HTTP for the action:
`initialize`, `notifications/initialized` and `tools/call` for the tools the
action uses, as plain JSON or framed as `text/event-stream`.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

GOOD_KEY = "wae_test_good_key"
CHAT_ONLY_KEY = "wae_test_chat_only"
SESSION_ID = "session-123"


def case(name: str, passed: bool, **extra: Any) -> Dict[str, Any]:
    return {"scenario_id": name, "name": name, "passed": passed,
            "score": 1.0 if passed else 0.0, "http_status": 200,
            "latency_ms": 120, **extra}


class FakeEngine:
    def __init__(self, *, results: Optional[List[Dict[str, Any]]] = None,
                 final_status: str = "passed", polls_before_done: int = 1,
                 sse: bool = False, require_session: bool = False,
                 run_error: Optional[str] = None,
                 previous_run: Optional[str] = None,
                 regressed: Optional[List[str]] = None,
                 agent_kind: str = "custom"):
        self.results = results if results is not None else [case("greets", True)]
        self.final_status = final_status
        self.polls_before_done = polls_before_done
        self.sse = sse
        self.require_session = require_session
        self.run_error = run_error
        self.previous_run = previous_run
        self.regressed = regressed or []
        self.agent_kind = agent_kind
        self.calls: List[Dict[str, Any]] = []       # every tools/call
        self.auth_headers: List[str] = []
        self.session_headers: List[Optional[str]] = []
        self.methods: List[str] = []
        self.polls = 0
        self.prompt_versions = [{"version_number": 1, "content": "You are helpful."}]
        self.served_version = 1
        self.own_prompt = True
        self._server: Optional[ThreadingHTTPServer] = None
        self.url = ""

    # -- tools -------------------------------------------------------------
    def suites(self) -> List[Dict[str, Any]]:
        return [
            {"suite_id": "suite-1", "name": "Support basics", "profile_id": "prof-1",
             "agent_kind": self.agent_kind},
            {"suite_id": "suite-2", "name": "No default", "profile_id": None,
             "agent_kind": self.agent_kind},
        ]

    def profiles(self) -> List[Dict[str, Any]]:
        return [
            {"profile_id": "prof-1", "slug": "helpdesk", "display_name": "Helpdesk",
             "agent_kind": self.agent_kind},
            {"profile_id": "prof-2", "slug": "sales", "display_name": "Sales",
             "agent_kind": self.agent_kind},
        ]

    def tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        if name == "eval_suites" and action == "list":
            return {"suites": self.suites()}
        if name == "list_profiles":
            return {"profiles": self.profiles()}
        if name == "models" and action == "list":
            return {"models": [{"model_id": "model-9", "name": "Fast model",
                                "model_string": "fast-1"}]}
        if name == "prompts" and action == "get":
            if args.get("profile_id"):
                number = self.served_version
            else:
                number = self.prompt_versions[-1]["version_number"]
            row = next(v for v in self.prompt_versions if v["version_number"] == number)
            slug = "helpdesk:system" if (self.agent_kind == "custom" and self.own_prompt) \
                else "system"
            kind = self.agent_kind if self.own_prompt else "template_kind"
            return {"prompt_id": "prompt-1", "slug": slug, "agent_kind": kind,
                    "version_number": number, "content": row["content"]}
        if name == "prompts" and action == "create_version":
            if args.get("profile_id"):
                return {"error": "the test engine must not be asked to pin a version"}
            number = self.prompt_versions[-1]["version_number"] + 1
            self.prompt_versions.append({"version_number": number,
                                         "content": args.get("content")})
            return {"created": True, "version_number": number,
                    "prompt_version_id": f"pv-{number}"}
        if name == "trigger_eval_run":
            if not args.get("suite_id"):
                return {"error": "suite_id is required for a suite run", "hint": None}
            if args.get("model_id") == "model-needs-judge":
                return {"error": "comparison runs require a model judge on the suite"}
            return {"run_id": "run-42", "status": "queued",
                    "hint": "poll eval_runs action=get run_id=..."}
        if name == "eval_runs" and action == "list":
            runs = []
            if self.previous_run:
                runs = [{"run_id": "run-still-going", "status": "running"},
                        {"run_id": self.previous_run, "status": "passed"}]
            return {"runs": runs}
        if name == "eval_runs" and action == "get":
            self.polls += 1
            done = self.polls > self.polls_before_done
            status = self.final_status if done else ("queued" if self.polls == 1 else "running")
            results = self.results if done else []
            passed = sum(1 for r in results if r.get("passed"))
            run = {"run_id": args.get("run_id"), "status": status, "kind": "text",
                   "error": self.run_error if done else None,
                   "results_summary": {
                       "scenarios": len(results), "passed": passed,
                       "failed": len(results) - passed,
                       "pass_rate": round(passed / len(results), 3) if results else None}}
            if args.get("include_results"):
                run["results"] = results
            return {"run": run}
        if name == "eval_runs" and action == "compare":
            return {"run_ids": args.get("run_ids"),
                    "scenarios": [{"scenario_id": s, "label": "regressed"}
                                  for s in self.regressed]
                                 + [{"scenario_id": "greets", "label": "held"}]}
        return {"error": f"unexpected tool call {name} {action}"}

    # -- http --------------------------------------------------------------
    def start(self, port: int = 0) -> "FakeEngine":
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def _send(self, status: int, body: bytes, content_type: str,
                      headers: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._send(200, b'{"status":"ok"}', "application/json")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if self.path != "/mcp":
                    return self._send(404, b'{"detail":"Not Found"}', "application/json")
                auth = self.headers.get("Authorization") or ""
                engine.auth_headers.append(auth)
                if not auth.startswith("Bearer "):
                    return self._send(401, json.dumps({
                        "error": "MCP requires Authorization: Bearer <API key or token>"
                    }).encode(), "application/json")
                token = auth[len("Bearer "):]
                if token == CHAT_ONLY_KEY:
                    return self._send(403, json.dumps({
                        "error": "this API key lacks the admin scope"}).encode(),
                        "application/json")
                if token != GOOD_KEY:
                    return self._send(401, json.dumps({
                        "error": "invalid API key"}).encode(), "application/json")
                message = json.loads(raw)
                method = message.get("method")
                engine.methods.append(method)
                engine.session_headers.append(self.headers.get("Mcp-Session-Id"))
                if method == "notifications/initialized":
                    return self._send(202, b"", "application/json")
                if method == "initialize":
                    result: Dict[str, Any] = {
                        "protocolVersion": message["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-engine", "version": "0"}}
                    extra = {"Mcp-Session-Id": SESSION_ID} if engine.require_session else {}
                    return self._reply(message, result, extra)
                if engine.require_session and \
                        self.headers.get("Mcp-Session-Id") != SESSION_ID:
                    return self._send(400, b'{"error":"missing session"}',
                                      "application/json")
                if method == "tools/call":
                    params = message.get("params") or {}
                    name, args = params.get("name"), params.get("arguments") or {}
                    engine.calls.append({"name": name, "arguments": args})
                    payload = engine.tool(name, args)
                    result = {"content": [{"type": "text", "text": json.dumps(payload)}],
                              "structuredContent": payload, "isError": False}
                    return self._reply(message, result)
                return self._reply(message, None, error={
                    "code": -32601, "message": f"unknown method {method}"})

            def _reply(self, message: Dict[str, Any], result: Any,
                       headers: Optional[Dict[str, str]] = None,
                       error: Optional[Dict[str, Any]] = None) -> None:
                envelope: Dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
                if error:
                    envelope["error"] = error
                else:
                    envelope["result"] = result
                if engine.sse:
                    # A server notification first, a comment line, CRLF line
                    # ends and a multi-line data field: what a real stream
                    # may carry around the one message that matters.
                    notice = json.dumps({"jsonrpc": "2.0",
                                         "method": "notifications/message",
                                         "params": {"level": "info", "data": "working"}})
                    text = json.dumps(envelope, indent=1)
                    data = "\r\n".join("data: " + line for line in text.split("\n"))
                    body = (f": keep-alive\r\n\r\nevent: message\r\ndata: {notice}\r\n\r\n"
                            f"event: message\r\nid: 7\r\n{data}\r\n\r\n")
                    return self._send(200, body.encode(), "text/event-stream", headers)
                return self._send(200, json.dumps(envelope).encode(),
                                  "application/json", headers)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def tool_calls(self, name: str, action: Optional[str] = None) -> List[Dict[str, Any]]:
        return [c["arguments"] for c in self.calls if c["name"] == name
                and (action is None or c["arguments"].get("action") == action)]
