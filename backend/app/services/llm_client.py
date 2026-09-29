"""LLM 客户端：OpenAI 兼容 `/chat/completions`，可注入 httpx 客户端便于 respx 拦截。"""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx

from app.core.errors import AppError
from app.settings import LlmGroup

# 只有这几类值得重试：限流与上游抖动是暂时的，4xx 的其余部分是"我们请求写错了"，
# 重试只会把同一个错误再付三次钱。408 按超时对待；425(Too Early) 不在此列——
# 它是 HTTP/2 乱序窗口问题，重试只是撞上同一个代理。
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_BACKOFF_BASE_S = 0.5


class LlmNotConfigured(AppError):
    code = "llm_not_configured"
    status_code = 503


class LlmTimeout(AppError):
    code = "llm_timeout"
    status_code = 504


class LlmRateLimited(AppError):
    code = "llm_rate_limited"
    status_code = 502


class LlmUpstreamError(AppError):
    code = "llm_upstream_error"
    status_code = 502


class LlmUnreachable(AppError):
    code = "llm_unreachable"
    status_code = 502


class LlmBadRequest(AppError):
    code = "llm_bad_request"
    status_code = 502


class LlmBadResponse(AppError):
    code = "llm_bad_response"
    status_code = 502


Message = Mapping[str, Any]
SleepFn = Callable[[float], Awaitable[None]]


class _RetryableStatus(Exception):
    """内部信号：状态码可重试。不跨出本模块，调用方看到的是最终的那条 AppError。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


async def _asyncio_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def _final_error(exc: _RetryableStatus | LlmTimeout) -> AppError:
    """把重试耗尽后的最后一次失败翻译成的错误——只有这两类能走到这里。"""
    if isinstance(exc, LlmTimeout):
        return exc
    status_code = exc.status_code
    if status_code == 429:
        return LlmRateLimited("LLM 触发限流（HTTP 429），已重试后仍未成功，请稍后再问")
    return LlmUpstreamError(f"LLM 上游不可用（HTTP {status_code}），已重试后仍未成功")


class LlmClient:
    def __init__(
        self,
        *,
        llm: LlmGroup,
        http: httpx.AsyncClient,
        sleep: SleepFn | None = None,
    ) -> None:
        self._llm = llm
        self._http = http
        # 注入点：测试不等真秒，生产用 asyncio.sleep
        self._sleep: SleepFn = sleep if sleep is not None else _asyncio_sleep

    def _require_configured(self) -> None:
        if self._llm.configured:
            return
        raise LlmNotConfigured(
            "LLM 未配置：请在 .env 设置 AIWEB_LLM__BASE_URL / AIWEB_LLM__API_KEY / AIWEB_LLM__MODEL"
        )

    def _request(self, messages: Sequence[Message], *, stream: bool) -> dict[str, Any]:
        return {
            "model": self._llm.model,
            "messages": [dict(m) for m in messages],
            # temperature=0 是 NL2SQL 的确定性要求，不能被 `or` 当成 falsy 丢掉
            "temperature": self._llm.temperature,
            "max_tokens": self._llm.max_tokens,
            "stream": stream,
        }

    @property
    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._llm.api_key}"}

    @property
    def model(self) -> str:
        """这次请求要问的模型名。

        公开它只有一个理由：`chat_messages.model` 是审计列，记的必须是**这次真问了哪个**，
        而不是调用方各自再读一次 Settings（读两处就会漂移，尤其测试里客户端与 Settings 不同源）。
        """
        return self._llm.model

    @property
    def _url(self) -> str:
        # base_url 由 settings 单点决定，不绑在注入的 http client 上（绑两处会漂移）
        return f"{self._llm.base_url.rstrip('/')}/chat/completions"

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self._http.post(
                self._url,
                json=payload,
                headers=self._headers,
                timeout=self._llm.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise LlmTimeout(f"LLM 调用超过 {self._llm.timeout_s}s") from exc
        except httpx.RequestError as exc:
            # DNS 写错、端点下线都会落在这里。不重试：同一个地址再试三次还是不通。
            raise LlmUnreachable(f"无法连接 LLM 端点 {self._url}：{type(exc).__name__}") from exc
        if resp.status_code in _RETRYABLE_STATUS:
            raise _RetryableStatus(resp.status_code)
        if resp.status_code >= 400:
            raise LlmBadRequest(
                f"LLM 端点拒绝请求（HTTP {resp.status_code}）：{_reason_from_body(resp.content)}"
            )
        return resp

    async def _request_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        for attempt in range(self._llm.max_retries + 1):
            try:
                return await self._post(payload)
            except (_RetryableStatus, LlmTimeout) as exc:
                if attempt == self._llm.max_retries:
                    raise _final_error(exc) from exc
                await self._sleep(_BACKOFF_BASE_S * 2**attempt)
        raise AssertionError("unreachable")  # 循环要么 return 要么在末轮抛

    async def complete(self, messages: Sequence[Message]) -> str:
        self._require_configured()
        resp = await self._request_with_retry(self._request(messages, stream=False))
        return _message_content(resp)

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        """逐 token 产出正文。

        不重试：一旦开始吐 token 就无法回滚，重发只会让前端看到重复内容。
        失败（建流被拒、半途断流）仍然翻译成 AppError，否则 SSE 前端拿到的是裸 500。

        `AIWEB_LLM__TIMEOUT_S` 在这里是"相邻两段数据的间隔上限"，不是整段回答的总时长：
        httpx 的标量 timeout 会落成 read 超时，而 read 是按次读取计时的。这与
        docs/roadmap.md 的键说明一致，改任何一边的语义都要同步。
        """
        self._require_configured()
        async with self._http.stream(
            "POST",
            self._url,
            json=self._request(messages, stream=True),
            headers=self._headers,
            timeout=self._llm.timeout_s,
        ) as resp:
            if resp.status_code in _RETRYABLE_STATUS:
                raise _final_error(_RetryableStatus(resp.status_code))
            if resp.status_code >= 400:
                body = await resp.aread()
                raise LlmBadRequest(
                    f"LLM 端点拒绝请求（HTTP {resp.status_code}）：{_reason_from_body(body)}"
                )
            try:
                async for token in _sse_tokens(resp.aiter_bytes()):
                    yield token
            except httpx.TimeoutException as exc:
                raise LlmTimeout(f"LLM 流式响应在 {self._llm.timeout_s}s 内没有下一段数据") from exc
            except httpx.RequestError as exc:
                raise LlmUnreachable(f"LLM 流式响应中断：{type(exc).__name__}") from exc


def _reason_from_body(body: bytes) -> str:
    """流式分支里响应体是 bytes，非流式里已经被读成 dict——两条路共用一个提取器。"""
    try:
        return _upstream_message(json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body[:200].decode("utf-8", "replace")


def _upstream_message(payload: Any) -> str:
    """把上游的 error.message 摘出来给用户看，只截断不解释——上游文案已经够具体。"""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:200]
        if isinstance(payload.get("message"), str):
            return payload["message"][:200]
    return "上游未给出原因"


def _message_content(resp: httpx.Response) -> str:
    """OpenAI 兼容响应里正文的唯一取法。

    不直接 `["choices"][0]...`：兼容端点经常用 200 裹一个 {error:...}，
    KeyError 会冒成一个没有 envelope 的裸 500。
    """
    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise LlmBadResponse("LLM 返回的不是 JSON，无法取出回答正文") from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LlmBadResponse(f"LLM 响应缺少 choices：{_upstream_message(payload)}")
    content = (
        (choices[0].get("message") or {}).get("content") if isinstance(choices[0], dict) else None
    )
    if not isinstance(content, str):
        raise LlmBadResponse("LLM 响应里的 choices[0].message.content 不是文本")
    return content


class _SseFramer:
    """把任意切分的字节流还原成一条条 SSE 事件（多行 `data:` 拼回一段文本）。

    不能"收满 \n\n 再 split"：网络分块不保证落在帧边界上，也不能按 bytes 累加后整体
    decode——一个汉字被切成两半时会解出替换字符，中文结论就这么一个字一个字地烂掉。
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        self._pending += self._decoder.decode(chunk)
        return self._drain()

    def close(self) -> list[str]:
        """补两个换行：让末尾那条没有空行收尾的事件也能被派发出来。"""
        self._pending += self._decoder.decode(b"", final=True) + "\n\n"
        return self._drain()

    def _drain(self) -> list[str]:
        out: list[str] = []
        text = self._pending
        start = 0
        while True:
            newline = text.find("\n", start)
            if newline < 0:
                break
            line = text[start:newline]
            start = newline + 1
            if line.endswith("\r"):  # \r\n 的前半可能落在上一块末尾
                line = line[:-1]
            if not line:
                event = self._take_event()
                if event is not None:
                    out.append(event)
            elif line.startswith("data:"):
                self._data.append(line[len("data:") :].lstrip())
            # event:/id:/retry: 与 `: keep-alive` 注释对本协议无用，直接丢
        self._pending = text[start:]
        return out

    def _take_event(self) -> str | None:
        data, self._data = self._data, []
        return "\n".join(data) if data else None


async def _iter_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    framer = _SseFramer()
    async for raw in chunks:
        for event in framer.feed(raw):
            yield event
    for event in framer.close():
        yield event


async def _sse_tokens(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    async for event in _iter_events(chunks):
        if event.strip() == "[DONE]":
            return
        try:
            obj = json.loads(event)
        except json.JSONDecodeError:
            continue  # 中间代理塞进来的非 JSON 帧：跳过它，但不能编出一个 token
        choices = obj.get("choices") or []
        if not choices:
            continue
        content = (choices[0].get("delta") or {}).get("content")
        if content:
            yield content
