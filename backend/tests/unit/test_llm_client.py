from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.errors import AppError
from app.services.llm_client import LlmClient, SleepFn
from app.settings import LlmGroup

READY_LLM = LlmGroup(
    base_url="https://llm.test/v1",
    api_key="sk-test",
    model="gpt-4o-mini",
    temperature=0,
    timeout_s=5,
)


def _chat_payload(content: str) -> dict[str, object]:
    return {
        "id": "cmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


def _client(llm: LlmGroup, *, sleep: SleepFn | None = None) -> LlmClient:
    # 不给 httpx 绑 base_url：与 deps.get_llm_http() 保持一致，URL 由 settings 单点拼出
    return LlmClient(llm=llm, http=httpx.AsyncClient(), sleep=sleep)


async def test_未配置凭据时给出中文提示而不是空指针崩溃() -> None:
    client = _client(LlmGroup())

    with pytest.raises(AppError) as exc_info:
        await client.complete([{"role": "user", "content": "你好"}])

    assert exc_info.value.code == "llm_not_configured"
    # hint 必须可执行：告诉用户去配哪三个键，而不是只说"失败了"
    assert "AIWEB_LLM__BASE_URL" in exc_info.value.message
    assert "NoneType" not in exc_info.value.message


async def test_流式入口在未配置时同样给出提示() -> None:
    """两个入口都得守住：/api/chat/ask 走的是 stream()，漏一个就是裸 500。"""
    client = _client(LlmGroup())

    with pytest.raises(AppError) as exc_info:
        async for _ in client.stream([{"role": "user", "content": "你好"}]):
            pass

    assert exc_info.value.code == "llm_not_configured"


@respx.mock
async def test_一次正常问答返回助手文本并带上模型与密钥() -> None:
    route = respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_payload("SELECT 1"))
    )
    client = _client(READY_LLM)

    out = await client.complete([{"role": "user", "content": "查一下 1"}])

    assert out == "SELECT 1"
    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"] == [{"role": "user", "content": "查一下 1"}]


@respx.mock
async def test_temperature为零时仍然显式透传() -> None:
    """0 是 falsy：一个 `payload["temperature"] or default` 就会把确定性悄悄丢掉。"""
    route = respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_payload("ok"))
    )

    await _client(READY_LLM).complete([{"role": "user", "content": "hi"}])

    body = json.loads(route.calls.last.request.content)
    assert body["temperature"] == 0


@respx.mock
async def test_限流响应重试到上限后带上游状态码上抛() -> None:
    """429 不能静默降级成"模型没回答"：最终必须抛带 code 的 AppError，且重试次数有上界。"""
    route = respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = _client(READY_LLM, sleep=fake_sleep)

    with pytest.raises(AppError) as exc_info:
        await client.complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "llm_rate_limited"
    assert exc_info.value.status_code == 502
    assert route.call_count == READY_LLM.max_retries + 1
    # 指数退避的期望值手算得出：0.5 × 2^n，不是"只要递增就算对"
    assert sleeps == [0.5, 1.0]


class _Chunks:
    """把字节流按任意长度切碎——真实网络不会在 SSE 帧边界上停。"""

    def __init__(self, payload: bytes, size: int) -> None:
        self._parts = [payload[i : i + size] for i in range(0, len(payload), size)]

    async def __aiter__(self):
        for part in self._parts:
            yield part


def _sse(text: str, *, done: bool = True) -> bytes:
    frames = []
    for ch in text:
        body = {"choices": [{"index": 0, "delta": {"content": ch}}]}
        frames.append(f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode())
    if done:
        frames.append(b"data: [DONE]\n\n")
    return b"".join(frames)


@respx.mock
@pytest.mark.parametrize("chunk_size", [1, 7, 64, 4096])
async def test_流式分帧不完整时也不丢字(chunk_size: int) -> None:
    expected = "SELECT id, 名称 FROM customer LIMIT 10"
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Chunks(_sse(expected), chunk_size),  # type: ignore[arg-type]
        )
    )
    client = _client(READY_LLM)

    pieces = [tok async for tok in client.stream([{"role": "user", "content": "hi"}])]

    assert "".join(pieces) == expected


@respx.mock
async def test_心跳帧与换行归一不干扰取字() -> None:
    """代理会把 \n\n 改写成 \r\n\r\n，也会插 `: keep-alive`；两者都不该变成一个 token。"""
    frames = [
        b": keep-alive\n\n",
        b'data: {"choices":[{"index":0,"delta":{"content":"AB"}}]}\r\n\r\n',
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',  # 只有角色，无正文
        b"not json at all\n\n",
        b'data: {"choices":[{"index":0,"delta":{"content":"CD"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=b"".join(frames))
    )

    pieces = [tok async for tok in _client(READY_LLM).stream([{"role": "user", "content": "hi"}])]

    assert "".join(pieces) == "ABCD"


class _Flaky:
    """吐完内容后断流——真实网络最常见的失败形态，就发生在"已经给用户看了半个答案"之后。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self):
        yield self._payload
        raise httpx.ReadTimeout("connection dropped")


@respx.mock
async def test_流式半途断流也翻译成AppError() -> None:
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Flaky(_sse("SELECT 1", done=False)),  # type: ignore[arg-type]
        )
    )
    client = _client(READY_LLM)

    with pytest.raises(AppError) as exc_info:
        [tok async for tok in client.stream([{"role": "user", "content": "hi"}])]

    assert exc_info.value.code == "llm_timeout"


@respx.mock
async def test_上游抖动一次后重试成功而不是把错误丢给用户() -> None:
    route = respx.post("https://llm.test/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(503, json={"error": {"message": "overloaded"}}),
            httpx.Response(200, json=_chat_payload("SELECT 2")),
        ]
    )
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    out = await _client(READY_LLM, sleep=fake_sleep).complete([{"role": "user", "content": "查 2"}])

    assert out == "SELECT 2"
    assert route.call_count == 2
    assert slept == [0.5]


@respx.mock
async def test_超时用配置的秒数并且错误码可辨识() -> None:
    """超时不能和"模型拒答"混成同一个 code：SSE 前端要按 code 决定能不能重试。"""
    seen: list[object] = []

    def drop(request: httpx.Request) -> None:
        # 断言真的把 AIWEB_LLM__TIMEOUT_S 交给了 httpx，而不是只在错误文案里提了一句
        seen.append(request.extensions["timeout"])
        raise httpx.ConnectTimeout("timed out", request=request)

    respx.post("https://llm.test/v1/chat/completions").mock(side_effect=drop)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    llm = READY_LLM.model_copy(update={"timeout_s": 7})

    with pytest.raises(AppError) as exc_info:
        await _client(llm, sleep=fake_sleep).complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "llm_timeout"
    assert "7" in exc_info.value.message
    assert exc_info.value.status_code == 504
    assert len(slept) == llm.max_retries
    assert len(seen) == llm.max_retries + 1
    assert all(t["read"] == 7 and t["connect"] == 7 for t in seen)


@respx.mock
async def test_客户端参数写错时不重试也翻译成AppError() -> None:
    """4xx 不重试，但也不能把 httpx 原文丢给上层：异常处理器只认 AppError，否则是裸 500。"""
    route = respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "model not found"}})
    )

    with pytest.raises(AppError) as exc_info:
        await _client(READY_LLM).complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "llm_bad_request"
    assert exc_info.value.status_code == 502
    assert route.call_count == 1


@respx.mock
async def test_端点连不上时给出可读原因而不是底层异常() -> None:
    respx.post("https://llm.test/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    )

    with pytest.raises(AppError) as exc_info:
        await _client(READY_LLM).complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "llm_unreachable"
    assert exc_info.value.status_code == 502


@respx.mock
async def test_上游返回非承诺结构时报错而不是键缺失() -> None:
    """兼容端点常拿 200 裹一个 {error:...}；直接取 choices[0] 会 KeyError 成裸 500。"""
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"error": {"message": "upstream said no"}})
    )

    with pytest.raises(AppError) as exc_info:
        await _client(READY_LLM).complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "llm_bad_response"
    # 上游给的原因必须带出来，否则用户只能看到"失败了"
    assert "upstream said no" in exc_info.value.message


@respx.mock
async def test_流式建流失败时也是同一个错误码() -> None:
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    client = _client(READY_LLM)

    with pytest.raises(AppError) as exc_info:
        async for _ in client.stream([{"role": "user", "content": "hi"}]):
            pass

    assert exc_info.value.code == "llm_rate_limited"
