"""The Gemini adapter, driven by stand-ins for the model.

ADK runs the tool loop here exactly as it does against the API; only the
model's replies come from a script. These tests cover the adapter's central
rule: an answer is rejected unless the required tool calls succeeded and it
fits the role's output model. One test goes through ADK's real Gemini client
to a stand-in HTTP endpoint, to pin what is sent to the API.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from google.adk.models import BaseLlm, Gemini, LlmResponse
from google.genai import errors, types

from datatracemind import demo
from datatracemind.llm import gemini
from datatracemind.llm.base import (
    InvalidOutput,
    NotAuthenticated,
    ProviderError,
    RequiredToolMissing,
    RoleCall,
    TurnTimeout,
    UsageLimitReached,
)
from datatracemind.models import InvestigationReport
from datatracemind.orchestration.supervisor import run_investigation
from tests.support import DEFAULT_ARGUMENTS, DEFAULT_STEPS, incident, statuses

ANSWER = {"observations": [{"statement": "net_revenue contract read", "tool": "get_metric_contract"}],
          "open_questions": []}  # fmt: skip
USAGE = types.GenerateContentResponseUsageMetadata(
    prompt_token_count=1000, cached_content_token_count=400, candidates_token_count=100, thoughts_token_count=20
)


def model_reply(*parts: types.Part) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=list(parts)), usage_metadata=USAGE)


def tool_reply(name: str, arguments: dict) -> LlmResponse:
    return model_reply(types.Part(function_call=types.FunctionCall(name=name, args=arguments)))


def text_reply(text: str) -> LlmResponse:
    return model_reply(types.Part(text=text))


class ScriptedModel(BaseLlm):
    """Answers successive model requests in order and keeps what it was sent."""

    model: str = "gemini-3.8-flash"
    replies: list = []
    requests: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request.model_copy(deep=True))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield reply


def scripted(*replies) -> ScriptedModel:
    return ScriptedModel(replies=list(replies), requests=[])


def run(model: BaseLlm, **overrides):
    values = {
        "role": "source_investigator",
        "prompt": "fixed prompt",
        "output_model": InvestigationReport,
        "allowed_tools": ("get_metric_contract", "get_data_contract"),
        "required_tools": ("get_metric_contract",),
    }
    return gemini.GeminiProvider(llm=model).run_turn(RoleCall(**{**values, **overrides}))


def tool_results(request) -> list[dict]:
    """The tool results in the last message of a model request."""
    return [part.function_response.response for part in request.contents[-1].parts if part.function_response]


def test_tool_loop_records_real_tool_results_and_usage():
    model = scripted(tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                     text_reply("```json\n" + json.dumps(ANSWER) + "\n```"))  # fmt: skip
    result = run(model)
    assert result.output == InvestigationReport.model_validate(ANSWER)
    (call,) = result.tool_calls
    assert call.status == "completed" and call.arguments == {"metric_name": "net_revenue"}
    assert call.result["name"] == "net_revenue" and call.result["text"]
    usage = result.usage
    # Thinking is billed as output, on top of the answer's own tokens.
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.reasoning_output_tokens) == (
        2000, 800, 240, 40,
    )  # fmt: skip
    assert result.est_cost_usd == pytest.approx((1200 * 0.75 + 800 * 0.075 + 240 * 3.75) / 1e6)
    assert result.model == "gemini-3.8-flash"

    first, second = model.requests
    declared = [fd.name for tool in first.config.tools for fd in tool.function_declarations]
    assert declared == ["get_metric_contract", "get_data_contract"]
    assert first.config.tools[0].function_declarations[0].parameters_json_schema["required"] == ["metric_name"]
    # The schema reaches the model as written, and the role's prompt is the only message.
    assert '"observations"' in first.config.system_instruction
    assert [part.text for part in first.contents[0].parts] == ["fixed prompt"]
    # The tool's own result is what the model sees next.
    assert tool_results(second)[0]["name"] == "net_revenue"


def test_tool_refusal_reaches_the_model_and_is_recorded_as_failed():
    model = scripted(tool_reply("get_metric_contract", {"metric_name": "nope"}),
                     tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                     text_reply(json.dumps(ANSWER)))  # fmt: skip
    result = run(model)
    failed, succeeded = result.tool_calls
    assert failed.status == "failed" and failed.error
    assert tool_results(model.requests[1]) == [{"error": failed.error}]
    assert succeeded.status == "completed"


def test_tools_outside_the_role_are_refused_without_running():
    model = scripted(tool_reply("run_readonly_sql", {"query_template_id": "x"}), text_reply(json.dumps(ANSWER)))
    with pytest.raises(RequiredToolMissing) as caught:
        run(model)
    assert caught.value.tool_calls == []
    assert "no tool with that name is available" in tool_results(model.requests[1])[0]["error"]


def test_answer_without_required_tool_is_rejected():
    with pytest.raises(RequiredToolMissing) as caught:
        run(scripted(text_reply(json.dumps(ANSWER))))
    assert caught.value.usage.input_tokens == 1000


def test_bad_answer_gets_one_correction_then_fails():
    model = scripted(tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                     text_reply('{"unexpected": true}'), text_reply("still not json"))  # fmt: skip
    with pytest.raises(InvalidOutput) as caught:
        run(model)
    assert len(caught.value.tool_calls) == 1
    correction = model.requests[2].contents[-1]
    assert correction.role == "user" and "does not fit the schema" in correction.parts[0].text
    # The correction continues the same conversation: the rejected answer is above it.
    assert model.requests[2].contents[-2].parts[0].text == '{"unexpected": true}'


def test_a_corrected_answer_is_accepted():
    model = scripted(tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                     text_reply("not json"), text_reply(json.dumps(ANSWER)))  # fmt: skip
    assert run(model).output == InvestigationReport.model_validate(ANSWER)


def test_api_errors_are_classified():
    def api_error(code: int, status: str, message: str) -> errors.APIError:
        return errors.ClientError(code, {"error": {"code": code, "status": status, "message": message}})

    with pytest.raises(UsageLimitReached):
        run(scripted(api_error(429, "RESOURCE_EXHAUSTED", "quota exceeded")))
    with pytest.raises(NotAuthenticated):
        # What the Gemini API answers for a wrong key.
        run(scripted(api_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.")))
    with pytest.raises(NotAuthenticated):
        run(scripted(api_error(403, "PERMISSION_DENIED", "denied")))
    with pytest.raises(ProviderError):
        run(scripted(errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}})))
    with pytest.raises(ProviderError):
        run(scripted(httpx.ConnectError("no route to host")))


def test_a_failure_keeps_the_evidence_gathered_before_it():
    model = scripted(tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                     errors.ServerError(500, {"error": {"code": 500, "status": "INTERNAL", "message": "boom"}}))  # fmt: skip
    with pytest.raises(ProviderError) as caught:
        run(model)
    assert [c.status for c in caught.value.tool_calls] == ["completed"]
    assert caught.value.usage.input_tokens == 1000


def test_a_reply_with_no_content_fails_the_turn():
    blocked = LlmResponse(error_code="SAFETY", error_message="blocked", usage_metadata=USAGE)
    with pytest.raises(ProviderError, match="SAFETY"):
        run(scripted(blocked), required_tools=())


def test_model_requests_are_bounded(monkeypatch):
    monkeypatch.setattr(gemini, "MAX_MODEL_REQUESTS", 3)
    model = scripted(*[tool_reply("get_metric_contract", {"metric_name": "net_revenue"})] * 5)
    with pytest.raises(InvalidOutput, match="no final answer after 3 model requests") as caught:
        run(model)
    assert len(model.requests) == 3 and len(caught.value.tool_calls) == 3


def test_tool_calls_are_bounded(monkeypatch):
    monkeypatch.setattr(gemini, "MAX_TOOL_CALLS", 1)
    model = scripted(tool_reply("get_metric_contract", {"metric_name": "net_revenue"}),
                     tool_reply("get_data_contract", {"dataset_name": "orders"}),
                     text_reply(json.dumps(ANSWER)))  # fmt: skip
    result = run(model)
    assert [c.tool for c in result.tool_calls] == ["get_metric_contract"]
    assert "used all its tool calls" in tool_results(model.requests[2])[0]["error"]


def test_a_turn_that_runs_out_of_time_fails():
    class Stalled(ScriptedModel):
        async def generate_content_async(self, llm_request, stream=False):
            import asyncio

            await asyncio.sleep(30)
            yield text_reply("too late")

    with pytest.raises(TurnTimeout):
        run(Stalled(), timeout_s=0.2)


def test_availability_follows_the_api_key(monkeypatch):
    for name in gemini.API_KEY_ENVS:
        monkeypatch.setenv(name, "")
    status = gemini.GeminiProvider().availability()
    assert not status.available and "GEMINI_API_KEY" in status.detail
    with pytest.raises(NotAuthenticated):
        gemini.GeminiProvider().run_turn(RoleCall(role="r", prompt="p", output_model=InvestigationReport))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    provider = gemini.GeminiProvider()
    assert provider.availability().available and provider.model == "gemini-3.5-flash-lite"


def stand_in_api(replies: list[httpx.Response]) -> tuple[gemini.PatientGemini, list[httpx.Request]]:
    """The provider's own Gemini model, with only the HTTP endpoint replaced."""
    requests: list[httpx.Request] = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return replies.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    model = gemini.PatientGemini(
        model="gemini-3.8-flash",
        client_kwargs={"api_key": "test-key", "http_options": types.HttpOptions(httpx_async_client=client)},
    )
    return model, requests


def rate_limited(quota: str, retry_delay: str | None) -> httpx.Response:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota}]}]
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return httpx.Response(429, json={"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota", "details": details,
    }})  # fmt: skip


def answered(text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100},
    })  # fmt: skip


def test_a_per_minute_rate_limit_is_waited_out(monkeypatch):
    waits: list[float] = []
    sleep = gemini.asyncio.sleep

    async def record(seconds: float) -> None:
        waits.append(seconds)
        await sleep(0)

    monkeypatch.setattr(gemini.asyncio, "sleep", record)
    per_minute = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    model, requests = stand_in_api(
        [rate_limited(per_minute, "18s"), rate_limited(per_minute, "0.5s"), answered(json.dumps(ANSWER))]
    )
    result = run(model, required_tools=())
    assert result.output == InvestigationReport.model_validate(ANSWER)
    # Each wait is the refill time the API gave, and the same request is sent again.
    assert waits == [19.0, 1.5] and len(requests) == 3
    assert result.usage.input_tokens == 1000


def test_a_daily_quota_fails_the_turn_at_once():
    model, requests = stand_in_api([rate_limited("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "30s")])
    with pytest.raises(UsageLimitReached, match="429 RESOURCE_EXHAUSTED: You exceeded your current quota"):
        run(model, required_tools=())
    assert len(requests) == 1


def test_a_rate_limit_is_not_waited_for_without_end(monkeypatch):
    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(gemini.asyncio, "sleep", no_wait)
    monkeypatch.setattr(gemini, "RATE_LIMIT_WAITS", 2)
    per_minute = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    model, requests = stand_in_api([rate_limited(per_minute, "5s")] * 3)
    with pytest.raises(UsageLimitReached):
        run(model, required_tools=())
    assert len(requests) == 3
    # A wait the API puts beyond the minute is not taken at all.
    model, requests = stand_in_api([rate_limited(per_minute, "3600s")])
    with pytest.raises(UsageLimitReached):
        run(model, required_tools=())
    assert len(requests) == 1


def test_request_sent_to_the_gemini_api():
    """Through ADK's own Gemini client, with only the HTTP endpoint replaced."""

    def reply(*parts: dict) -> dict:
        return {
            "candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1000, "cachedContentTokenCount": 400, "candidatesTokenCount": 100},
        }

    replies = [reply({"functionCall": {"name": "get_metric_contract", "args": {"metric_name": "net_revenue"}}}),
               reply({"text": json.dumps(ANSWER)})]  # fmt: skip
    requests: list[httpx.Request] = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=replies.pop(0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    model = Gemini(
        model="gemini-3.8-flash",
        client_kwargs={"api_key": "test-key", "http_options": types.HttpOptions(httpx_async_client=client)},
    )
    result = run(model)
    assert result.output == InvestigationReport.model_validate(ANSWER)
    assert (result.usage.input_tokens, result.usage.cached_input_tokens, result.usage.output_tokens) == (
        2000, 800, 200,
    )  # fmt: skip

    first, second = requests
    assert first.url.path == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert first.headers["x-goog-api-key"] == "test-key"
    body = json.loads(first.content)
    assert [fd["name"] for fd in body["tools"][0]["functionDeclarations"]] == [
        "get_metric_contract", "get_data_contract",
    ]  # fmt: skip
    assert "DataTraceMind" in body["systemInstruction"]["parts"][0]["text"]
    assert body["contents"] == [{"role": "user", "parts": [{"text": "fixed prompt"}]}]
    answer = json.loads(second.content)["contents"][-1]["parts"][0]["functionResponse"]
    assert answer["name"] == "get_metric_contract" and answer["response"]["name"] == "net_revenue"


class RolePlayingModel(BaseLlm):
    """Plays every role: asks for its tools in one step, then gives the scripted answer."""

    model: str = "gemini-3.8-flash"

    async def generate_content_async(self, llm_request, stream=False):
        role = re.search(r'internal name is "(\w+)"', llm_request.config.system_instruction).group(1)
        offered = [fd.name for tool in llm_request.config.tools or [] for fd in tool.function_declarations]
        asked = any(part.function_call for content in llm_request.contents for part in content.parts or [])
        wanted = [name for name in offered if name in DEFAULT_ARGUMENTS]
        if wanted and not asked:
            yield model_reply(*(
                types.Part(function_call=types.FunctionCall(name=name, args=DEFAULT_ARGUMENTS[name]))
                for name in wanted
            ))  # fmt: skip
        else:
            yield text_reply(DEFAULT_STEPS[role](None).model_dump_json())


def test_whole_investigation_runs_on_adk_with_a_stand_in_model(baseline):
    """The graph, the roles, ADK's tool loop, and the real tools; only the model is scripted."""
    incident_id = demo.inject("DS-01")["incident_id"]
    provider = gemini.GeminiProvider(llm=RolePlayingModel())
    assert run_investigation(incident_id, provider=provider) == "awaiting_approval"
    assert statuses(incident_id) == [
        "detected", "investigating", "diagnosed", "repair_proposed", "validating", "awaiting_approval",
    ]  # fmt: skip
    found = incident(incident_id)
    # Every tool the model asked for, several at once, ran for real and was filed.
    by_role = {role: [e for e in found["evidence"] if e["collected_by"] == role] for role in DEFAULT_STEPS}
    assert len(by_role["source_investigator"]) == 5 and len(by_role["pipeline_investigator"]) == 4
    assert all(e["result_summary"] for e in found["evidence"])
    assert found["proposal"]["status"] == "validated"
    run = found["investigation_runs"][0]
    assert run["model"] == "gemini-3.8-flash" and run["status"] == "awaiting_approval"
    assert run["role_turns"] == 4 and run["tool_calls"] == 10 and run["failed_tool_calls"] == 0
    # Seven model requests: two for each role that used tools, one for the analyst.
    assert run["input_tokens"] == 7000 and run["cached_input_tokens"] == 2800
