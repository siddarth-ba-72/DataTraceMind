"""The Codex adapter, driven by a stand-in `codex` executable.

The stand-in prints event streams in the format recorded in
docs/CODEX_LLM_PROVIDER.md, so these tests cover the adapter's parsing and its
central rule: an answer is rejected unless the required tool calls succeeded.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from datatracemind.llm import codex
from datatracemind.llm.base import (
    InvalidOutput,
    RequiredToolMissing,
    RoleCall,
    TurnTimeout,
    UsageLimitReached,
)
from datatracemind.models import InvestigationReport

FAKE_CODEX = r'''#!__PYTHON__
import json, os, sys, time

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    sys.exit(int(os.environ.get("FAKE_CODEX_LOGIN_EXIT", "0")))

with open(os.environ["FAKE_CODEX_ARGV"], "w") as handle:
    json.dump({"argv": args, "stdin_closed": sys.stdin.read() == "", "env": sorted(os.environ)}, handle)

def emit(event):
    print(json.dumps(event), flush=True)

mode = os.environ["FAKE_CODEX_MODE"]
answer = json.dumps({"observations": [{"statement": "SUCCEEDED refunds total 37150", "tool": "run_readonly_sql"}],
                     "open_questions": []})
call = {"id": "item_1", "type": "mcp_tool_call", "server": "dtm", "tool": "run_readonly_sql",
        "arguments": {"query_template_id": "source_refunds_by_status"}}

emit({"type": "thread.started", "thread_id": "thread-123"})
emit({"type": "turn.started"})
if mode == "hang":
    time.sleep(30)
if mode == "limit":
    emit({"type": "error", "message": "You've hit your usage limit. Try again in 3 hours."})
    sys.exit(1)
emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "I'll query the refunds."}})
if mode in ("ok", "bad_json", "tool_failed"):
    emit({"type": "item.started", "item": {**call, "result": None, "error": None, "status": "in_progress"}})
    if mode == "tool_failed":
        emit({"type": "item.completed", "item": {**call, "result": None, "status": "failed",
              "error": {"message": "MCP tool call requires approval, but approval policy is never"}}})
    else:
        emit({"type": "item.completed", "item": {**call, "error": None, "status": "completed", "result": {
              "content": [{"type": "text", "text": "{}"}],
              "structured_content": {"row_count": 1, "rows": [{"refund_status": "SUCCEEDED", "total": 37150}]}}}})
emit({"type": "item.completed", "item": {"id": "item_9", "type": "reasoning", "text": "(skipped by the adapter)"}})
emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
      "text": '{"unexpected": true}' if mode == "bad_json" else answer}})
emit({"type": "turn.completed", "usage": {"input_tokens": 34239, "cached_input_tokens": 22272,
      "cache_write_input_tokens": 0, "output_tokens": 130, "reasoning_output_tokens": 8}})
'''


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch) -> Path:
    """Put a stand-in `codex` first on PATH. Returns the file its argv is written to."""
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    executable.write_text(FAKE_CODEX.replace("__PYTHON__", sys.executable))
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_ARGV", str(argv_file))
    monkeypatch.setenv("NEATLOGS_API_KEY", "must-not-reach-codex")
    return argv_file


def role_call(**overrides) -> RoleCall:
    values = {
        "role": "source_investigator",
        "prompt": "fixed prompt",
        "output_model": InvestigationReport,
        "allowed_tools": ("run_readonly_sql", "profile_column"),
        "required_tools": ("run_readonly_sql",),
    }
    return RoleCall(**{**values, **overrides})


def run(mode: str, monkeypatch, **overrides):
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    return codex.CodexProvider().run_turn(role_call(**overrides))


def test_successful_turn_returns_recorded_tool_results_and_usage(fake_codex, monkeypatch):
    result = run("ok", monkeypatch)
    assert result.output.observations[0].tool == "run_readonly_sql"
    assert result.thread_id == "thread-123"
    assert result.model == "gpt-6-sol" and result.reasoning_effort == "medium"
    (call,) = result.tool_calls
    assert call.status == "completed" and call.arguments == {"query_template_id": "source_refunds_by_status"}
    # The evidence value comes from the tool's structured result, not the model's sentence.
    assert call.result == {"row_count": 1, "rows": [{"refund_status": "SUCCEEDED", "total": 37150}]}
    assert (result.usage.input_tokens, result.usage.cached_input_tokens, result.usage.output_tokens) == (
        34239, 22272, 130,
    )  # fmt: skip
    assert result.est_cost_usd == pytest.approx((11967 * 2.00 + 22272 * 0.20 + 130 * 10.00) / 1e6)


def test_command_carries_every_required_safety_setting(fake_codex, monkeypatch):
    run("ok", monkeypatch)
    recorded = json.loads(fake_codex.read_text())
    argv = recorded["argv"]
    joined = " ".join(argv)
    assert argv[0] == "exec" and argv[-1] == "fixed prompt"
    for flag in ("--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--output-schema"):
        assert flag in argv
    assert argv[argv.index("-m") + 1] == "gpt-6-sol"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("--disable") + 1] == "shell_tool"
    assert 'model_reasoning_effort="medium"' in argv and 'web_search="disabled"' in argv
    assert "mcp_servers.dtm.required=true" in argv
    assert 'mcp_servers.dtm.default_tools_approval_mode="approve"' in argv
    assert 'mcp_servers.dtm.enabled_tools=["run_readonly_sql", "profile_column"]' in argv
    assert "mcp_server.py" in joined
    assert "danger" not in joined and "bypass" not in joined and "workspace-write" not in joined
    assert recorded["stdin_closed"] is True
    # Secrets the model process has no use for are withheld from it.
    assert "NEATLOGS_API_KEY" not in recorded["env"] and "DTM_DATABASE_URL" not in recorded["env"]
    workdir = Path(argv[argv.index("-C") + 1])
    assert not workdir.exists()  # an empty directory, removed after the turn


def test_role_without_tools_gets_no_tool_server(fake_codex, monkeypatch):
    run("no_tool", monkeypatch, allowed_tools=(), required_tools=())
    assert "mcp_servers" not in " ".join(json.loads(fake_codex.read_text())["argv"])


def test_schema_valid_answer_is_rejected_when_the_required_tool_failed(fake_codex, monkeypatch):
    with pytest.raises(RequiredToolMissing, match="run_readonly_sql") as failure:
        run("tool_failed", monkeypatch)
    (call,) = failure.value.tool_calls
    assert call.status == "failed" and "requires approval" in call.error
    assert failure.value.usage.input_tokens == 34239  # the failed turn is still accounted for


def test_schema_valid_answer_is_rejected_when_the_required_tool_was_never_called(fake_codex, monkeypatch):
    with pytest.raises(RequiredToolMissing):
        run("no_tool", monkeypatch)


def test_final_message_that_does_not_fit_the_model_is_rejected(fake_codex, monkeypatch):
    with pytest.raises(InvalidOutput):
        run("bad_json", monkeypatch)


def test_usage_limit_is_reported_as_such_and_is_not_retryable(fake_codex, monkeypatch):
    with pytest.raises(UsageLimitReached) as failure:
        run("limit", monkeypatch)
    assert failure.value.retryable is False


def test_hung_turn_is_killed_at_the_timeout(fake_codex, monkeypatch):
    with pytest.raises(TurnTimeout):
        run("hang", monkeypatch, timeout_s=1.0)


def test_unknown_tool_names_are_refused_before_any_process_starts(fake_codex, monkeypatch):
    with pytest.raises(ValueError, match="unknown tools"):
        run("ok", monkeypatch, allowed_tools=("run_shell",))
    assert not fake_codex.exists()


def test_availability_follows_login_status_and_api_key(fake_codex, monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    assert codex.CodexProvider().availability().method == "chatgpt_login"
    monkeypatch.setenv("FAKE_CODEX_LOGIN_EXIT", "1")
    unavailable = codex.CodexProvider().availability()
    assert unavailable.available is False and "codex login" in unavailable.detail
    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    assert codex.CodexProvider().availability().method == "api_key"


def test_output_schemas_are_strict():
    def closed(node) -> bool:
        if isinstance(node, dict):
            if node.get("type") == "object" and (
                node.get("additionalProperties") is not False
                or sorted(node.get("required", [])) != sorted(node["properties"])
            ):
                return False
            return all(closed(value) for value in node.values())
        if isinstance(node, list):
            return all(closed(value) for value in node)
        return True

    from datatracemind.models import RepairPlan, RootCauseReport

    for model in (InvestigationReport, RootCauseReport, RepairPlan):
        schema = codex.strict_schema(model)
        assert closed(schema)
        assert "minimum" not in json.dumps(schema) and '"title"' not in json.dumps(schema)
