"""The LiteLLM gateway adapter, driven by a stand-in Chat Completions endpoint.

The stand-in answers each request from a script, so these tests cover the
tool loop and the adapter's central rule: an answer is rejected unless the
required tool calls succeeded and it fits the role's output model.
"""

from __future__ import annotations

import json

import httpx
import pytest

from datatracemind.llm import litellm
from datatracemind.llm.base import (
    InvalidOutput,
    NotAuthenticated,
    RequiredToolMissing,
    RoleCall,
    UsageLimitReached,
)
from datatracemind.models import InvestigationReport

ANSWER = {"observations": [{"statement": "net_revenue contract read", "tool": "get_metric_contract"}],
          "open_questions": []}  # fmt: skip


def tool_reply(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}  # fmt: skip


def text_reply(text: str) -> dict:
    return {"role": "assistant", "content": text}


def completion(message: dict) -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "claude-sonnet-4.6",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                  "prompt_tokens_details": {"cached_tokens": 400}},
    }  # fmt: skip


class Gateway:
    """Replies to successive requests in order and keeps what was sent."""

    def __init__(self, replies: list[dict | httpx.Response]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        reply = self.replies.pop(0)
        return (
            reply
            if isinstance(reply, httpx.Response)
            else httpx.Response(200, json=completion(reply))
        )


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.delenv("LITELLM_MODEL", raising=False)
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)


def run(gateway: Gateway, **overrides):
    values = {
        "role": "source_investigator",
        "prompt": "fixed prompt",
        "output_model": InvestigationReport,
        "allowed_tools": ("get_metric_contract", "get_data_contract"),
        "required_tools": ("get_metric_contract",),
    }
    provider = litellm.LiteLLMProvider(
        http_client=httpx.Client(transport=httpx.MockTransport(gateway))
    )
    return provider.run_turn(RoleCall(**{**values, **overrides}))


def test_tool_loop_records_real_tool_results_and_usage():
    gateway = Gateway([tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                       text_reply("```json\n" + json.dumps(ANSWER) + "\n```")])  # fmt: skip
    result = run(gateway)
    assert result.output == InvestigationReport.model_validate(ANSWER)
    (call,) = result.tool_calls
    assert call.status == "completed" and call.arguments == {
        "metric_name": "net_revenue"
    }
    assert call.result["name"] == "net_revenue" and call.result["text"]
    assert (
        result.usage.input_tokens,
        result.usage.cached_input_tokens,
        result.usage.output_tokens,
    ) == (2000, 800, 200)
    assert result.est_cost_usd == pytest.approx(
        (1200 * 3.00 + 800 * 0.30 + 200 * 15.00) / 1e6
    )

    first, second = gateway.requests
    assert gateway.headers[0]["authorization"] == "Bearer test-key"
    assert first["model"] == "claude-sonnet-4.6"
    assert [t["function"]["name"] for t in first["tools"]] == [
        "get_metric_contract",
        "get_data_contract",
    ]
    assert first["tools"][0]["function"]["parameters"]["required"] == ["metric_name"]
    # The tool's own result is what the model sees next.
    assert (
        second["messages"][-1]["role"] == "tool"
        and "net_revenue" in second["messages"][-1]["content"]
    )


def test_tool_refusal_reaches_the_model_and_is_recorded_as_failed():
    gateway = Gateway([tool_reply("get_metric_contract", {"metric_name": "nope"}),
                       tool_reply("get_metric_contract", {"metric_name": "net_revenue"}, "call_2"),
                       text_reply(json.dumps(ANSWER))])  # fmt: skip
    result = run(gateway)
    failed, succeeded = result.tool_calls
    assert failed.status == "failed" and failed.error
    assert gateway.requests[1]["messages"][-1]["content"].startswith("Error:")
    assert succeeded.status == "completed"


def test_tools_outside_the_role_are_refused_without_running():
    gateway = Gateway(
        [
            tool_reply("run_readonly_sql", {"query_template_id": "x"}),
            text_reply(json.dumps(ANSWER)),
        ]
    )
    with pytest.raises(RequiredToolMissing):
        run(gateway)
    assert (
        "not available to this role" in gateway.requests[1]["messages"][-1]["content"]
    )


def test_answer_without_required_tool_is_rejected():
    with pytest.raises(RequiredToolMissing) as caught:
        run(Gateway([text_reply(json.dumps(ANSWER))]))
    assert caught.value.usage.input_tokens == 1000


def test_bad_answer_gets_one_correction_then_fails():
    gateway = Gateway([tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                       text_reply('{"unexpected": true}'), text_reply("still not json")])  # fmt: skip
    with pytest.raises(InvalidOutput) as caught:
        run(gateway)
    assert len(caught.value.tool_calls) == 1
    assert "does not fit the schema" in gateway.requests[2]["messages"][-1]["content"]


def test_gateway_errors_are_classified():
    limited = Gateway(
        [httpx.Response(429, json={"error": {"message": "rate limit"}})] * 3
    )
    with pytest.raises(UsageLimitReached):
        run(limited)
    with pytest.raises(NotAuthenticated):
        run(Gateway([httpx.Response(401, json={"error": {"message": "bad key"}})]))


def test_availability_follows_the_api_key(monkeypatch):
    assert litellm.LiteLLMProvider().availability().available
    monkeypatch.setenv("LITELLM_API_KEY", "")
    status = litellm.LiteLLMProvider().availability()
    assert not status.available and "LITELLM_API_KEY" in status.detail
