"""生产侧唯一的 embedding 客户端（工单 028 / ADR-0015 / 决策 2）。

只有一处实现、只有一个 provider：`provider` 恒 `openai_compatible`。闸内那个"假端点"
（`scripts/fake_embedding_server.py`）是**真进程真 socket**，不是这个类的第二个实现，所以本
文件里没有 `AIWEB_EMBEDDING__PROVIDER` 这类开关分支——那等于在生产代码里留一条只有开发机
会走到的路（ADR-0015 否掉的就是它）。

形状照 `app/services/llm_client.py`：同一套错误档位、同一个"只重试 429/5xx/超时"的口径。
**有意不同的一处**：构造不收 `httpx.AsyncClient`。收了就等于允许第二种替身，而第二种替身会
让"真 HTTP 那一路"（base_url 拼接、超时、非 200、维度不符）在闸里永远没有证据。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, Final

import httpx

from app.core.errors import AppError
from app.settings import EmbeddingGroup

# `kb_index_profile.provider` 那一格的唯一出处。027 之前它住在 `kb_service` 里，跟着"按
# settings 建一套档案"那条后门一起被删；重建它的位置归这里，因为这一格回答的是"这台机器用
# 哪种协议问端点"，而全仓只有一个答案。029 写档案行时读它，别自己写字面量。
EMBEDDING_PROVIDER: Final = "openai_compatible"


class EmbeddingNotConfigured(AppError):
    """`EmbeddingGroup.configured` 为假时抬（四格：base_url / api_key / model / dimension），
    **不让 HTTP 去撞空 base_url**。

    与"连不上"分开一档是必要的：前者是"这台机器还没启用向量路"（出路是配 .env 或起替身），
    后者是"启用了但端点不回话"（出路是查 base_url / 进程）。混成一档，运维会去查一个本来
    就没配的东西。
    """

    code = "embedding_not_configured"
    status_code = 503


class EmbeddingTimeout(AppError):
    code = "embedding_timeout"
    status_code = 504


class EmbeddingRateLimited(AppError):
    code = "embedding_rate_limited"
    status_code = 502


class EmbeddingUpstreamError(AppError):
    code = "embedding_upstream_error"
    status_code = 502


class EmbeddingUnreachable(AppError):
    code = "embedding_unreachable"
    status_code = 502


class EmbeddingBadRequest(AppError):
    """端点**答了**但拒绝这次请求（非可重试 4xx）——未知 model 名走的就是这一档。

    不许冒成 `EmbeddingBadResponse`：那是"答了但不成形"，两种处置不同（前者改配置，后者
    是端点自己坏了）。
    """

    code = "embedding_bad_request"
    status_code = 502


class EmbeddingBadResponse(AppError):
    """200 裹一个不像 `/v1/embeddings` 的 body（兼容端点常拿 200 回 error）。"""

    code = "embedding_bad_response"
    status_code = 502


class EmbeddingDimensionMismatch(AppError):
    """端点回的数组长度 ≠ 档案的 `dimensions`（spec 故事 15）。

    单独一档而不是折进 `EmbeddingBadResponse`，因为 029 要按这一档把**整颗作业**判 `failed`：
    维度是列类型的一部分，重试一万次也还是那个维度，只有建新档案才能救；而"响应不成形"是
    可以再试一次的。文案里期望与实收两个数都要在场（工单 028 验收 5）。
    """

    code = "embedding_dimension_mismatch"
    status_code = 502


# 只重试这几档：429 与 5xx 是端点暂时的状态，取向量又是输入的纯函数（重试幂等）。
# 词表与 `llm_client._RETRYABLE_STATUS` 同源——同一个上游故障在两条链路上不该得出
# 一种要重试、一种不重试的分派。公开它只有一个理由：那条"400 不在表里"的口径得有用例能钉。
RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})

# **没有对应的 `AIWEB_EMBEDDING__*` 键**，这是工单 028 的已定口径（"不新增键"）：那三个键
# 回答的是"用哪个端点、哪套宽度"，而这两格回答的是"这一发等多久、失败再试几次"——替身与
# 真端点用的是同一套，需要按机器调的话再加键，别为一件还没发生的事先开配置面。
_TIMEOUT_S: Final = 60.0
_MAX_RETRIES: Final = 2
_BACKOFF_BASE_S: Final = 0.5


class _RetryableStatus(Exception):
    """内部信号：状态码可重试。不跨出本模块，调用方看到的是 `_final_error` 翻译后的那一档。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


def _final_error(exc: _RetryableStatus | EmbeddingTimeout) -> AppError:
    """重试耗尽后把最后一次失败翻成用户看得见的那一档（只有这两类能走到这里）。"""
    if isinstance(exc, EmbeddingTimeout):
        return exc
    if exc.status_code == 429:
        return EmbeddingRateLimited("embedding 端点限流（HTTP 429），重试后仍没成功")
    return EmbeddingUpstreamError(f"embedding 端点不可用（HTTP {exc.status_code}），重试后仍没成功")


def embeddings_request(*, model: str, inputs: Sequence[str]) -> dict[str, Any]:
    """一批文本 → OpenAI 兼容的 `/v1/embeddings` 请求体。

    `input` 一律是数组：单条也发数组，客户端才只有一条请求形状（`scripts/check_env.py`
    那条探针也是这么发的，验收 4 要的就是"替身与真端点在同一个探针下无差别"）。
    """
    return {"model": model, "input": list(inputs)}


def embeddings_from_response(
    payload: Any, *, expected_dimension: int, count: int
) -> list[list[float]]:
    """响应体 → 与输入同序的向量批。

    归位读的是 `data[i].index`，不是数组下标：`index` 是 OpenAI 契约里唯一说明"这条向量属于
    哪条输入"的字段，而兼容端点不保证按请求顺序回。按下标取的话卡片 A 会挂上卡片 B 的向量——
    库里两行都合法、没有任何一层会报错，检索从此指错表。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise EmbeddingBadResponse(f"embedding 响应里没有 data 数组：{_brief(payload)}")
    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise EmbeddingBadResponse(
                f"embedding 响应的 data 里有一项不是 {{index,embedding}}：{item!r}"
            )
        index = _as_index(item.get("index"))
        vector = _as_vector(item["embedding"], what=index)
        if len(vector) != expected_dimension:
            raise EmbeddingDimensionMismatch(
                f"端点返回 {len(vector)} 维，这套档案期望 {expected_dimension} 维"
            )
        by_index[index] = vector
    try:
        return [by_index[i] for i in range(count)]
    except KeyError as exc:
        # 少回一条与 index 越界都走这里。不许退化成"返回拿到的那几条"：调用方 zip 短对齐时，
        # 后面的向量会挂到前面的卡片上，一条错误日志都不会有。
        raise EmbeddingBadResponse(
            f"embedding 响应缺第 {exc.args[0]} 条：请求 {count} 条、收到 {len(by_index)} 条"
        ) from exc


def _as_index(raw: Any) -> int:
    """`index` 是外部字段，不是实现里可以直接取的东西：缺它或非数一律落 `EmbeddingBadResponse`。

    裸 `KeyError`/`ValueError` 冒出去是一个没有 envelope 的 500，报出来的错会指向"服务器内部
    错误"，而真原因是兼容端点回了一份不合规的 body——那正是需要看得懂的一档。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise EmbeddingBadResponse(f"embedding 响应的 data 里有一项的 index 不是整数：{raw!r}")
    try:
        return int(raw)
    except ValueError as exc:
        raise EmbeddingBadResponse(
            f"embedding 响应的 data 里有一项的 index 不是整数：{raw!r}"
        ) from exc


def _as_vector(raw: list[Any], *, what: int) -> list[float]:
    """逐元素转 float；转不动就是"形状坏"，不是"宽度不对"。

    分成两档是有必要的：宽度不符要 029 把**整颗作业**判 `failed`（重试一万次还是那个维度），
    而"元素是字符串或 null"这种端点自己没成形，是 `EmbeddingBadResponse` 那一档。
    兼容端点真会这么回（把向量当字符串数组），所以这不是设想出来的分支。
    """
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError) as exc:
        raise EmbeddingBadResponse(
            f"embedding 响应第 {what} 条的向量里有非数值元素：{_brief(raw)}"
        ) from exc


def _brief(payload: Any) -> str:
    """把响应摘要成一行，同时不把整份 body 塞进异常文本（一份 1536 维向量有几十 KB）。"""
    return repr(payload)[:200]


def _reason_from_body(body: bytes) -> str:
    """端点自己写的拒绝原因（`error.message`），摘不出来就回原始字节的前 200 个。

    验收 3 要的"可辨识"全靠这一句：400 的响应体里端点已经说了"未知 model：'xxx'"，
    丢掉它就等于把"名字打错了"报成"请求被拒"，用户只能自己再去猜是哪一格。
    与 `llm_client._reason_from_body` 同一份逻辑，没有新发明。
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body[:200].decode("utf-8", "replace")
    if isinstance(payload, dict):
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else payload.get("message")
        if isinstance(message, str):
            return message[:200]
    return "上游未给出原因"


class EmbeddingClient:
    def __init__(self, *, embedding: EmbeddingGroup) -> None:
        self._embed = embedding

    @property
    def provider(self) -> str:
        """这一格要写进 `kb_index_profile.provider`，理由同 `LlmClient.model` 之于审计列：
        记的必须是**这次真按哪种协议问的**，而不是调用方各自再读一次配置。
        """
        return EMBEDDING_PROVIDER

    @property
    def model(self) -> str:
        return self._embed.model

    def _require_configured(self) -> None:
        if self._embed.configured:
            return
        raise EmbeddingNotConfigured(
            "embedding 未配置：请设置 AIWEB_EMBEDDING__{BASE_URL,API_KEY,MODEL,DIMENSION} 四格"
            "（本机没有真端点时，起 scripts/fake_embedding_server.py 把这四格指过去）"
        )

    @property
    def _url(self) -> str:
        # 版本段由 base_url 自带（与 `check_env.py:204` 同一拼法）：这里再拼一次 `/v1`
        # 就会出现"探针连得上、客户端连不上"那种最难分的错。
        return f"{self._embed.base_url.rstrip('/')}/embeddings"

    @property
    def _headers(self) -> dict[str, str]:
        # 有值才发（工单 028 已定口径的最后一句）：发一个空的 `Bearer ` 是在伪造一个不存在的凭证。
        # 今天这条路走不到——`_require_configured` 要求四格齐、`api_key` 已在里面——所以这不是
        # 一条救命的分支，而是"请求头按 OpenAI 兼容形状发"这条规则的写法本身。
        if not self._embed.api_key:
            return {}
        return {"authorization": f"Bearer {self._embed.api_key}"}

    async def embed(self, texts: Sequence[str], *, expected_dimension: int) -> list[list[float]]:
        """一批文本 → 与输入同序的向量批。`expected_dimension` 由调用方给。

        **宽度为什么是入参而不是 `settings.embedding.dimension`**（工单 029 的已定口径落在这里）：
        要验的是"这批向量配不配得上**目标档案**"，而档案那一行的 `dimensions` 是历史事实。
        reindex 的靶在入队那一刻就定下了，之后有人把 `.env` 改成别的宽度，改的是配置不是这颗
        作业该灌的宽度——客户端若自己读 settings，1024 的档案配 1536 的 .env 时整颗作业会死于
        "维度不符"，而该改的是配置。留一个缺省值等于留一条"忘了传就回到 settings"的退路，
        所以它是必填关键字参数（`test_sync_embed_sql.py` 钉着这条签名）。

        每次调用建一条自己的连接（`async with httpx.AsyncClient()`）：这是"不收 transport"
        这条口径的直接代价——客户端自己 own 生命周期，就没有"忘了 aclose、把连接池漏在
        worker 进程里"这一类 bug。reindex 是逐表一批一批发的，复用与否不是瓶颈。

        空批**一次 HTTP 都不发**：一张零卡片的表在 reindex 里是常态，让它去撞端点等于
        把"没有内容要嵌"报成"端点不通"。
        """
        self._require_configured()
        if not texts:
            return []
        count = len(texts)
        payload = embeddings_request(model=self._embed.model, inputs=texts)
        async with httpx.AsyncClient() as http:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    body = await self._post(http, payload)
                    break
                except (_RetryableStatus, EmbeddingTimeout) as exc:
                    if attempt == _MAX_RETRIES:
                        raise _final_error(exc) from exc
                    await asyncio.sleep(_BACKOFF_BASE_S * 2**attempt)
        return embeddings_from_response(body, expected_dimension=expected_dimension, count=count)

    async def _post(self, http: httpx.AsyncClient, payload: dict[str, Any]) -> Any:
        try:
            resp = await http.post(
                self._url, json=payload, headers=self._headers, timeout=_TIMEOUT_S
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingTimeout(f"embedding 端点在 {_TIMEOUT_S}s 内没回话：{self._url}") from exc
        except httpx.RequestError as exc:
            # 拒连、DNS 不通、对端直接断开都走这里。**不重试**：同一个地址再试三次还是不通
            # （与 `llm_client._post` 同一条理由，也别新造第三档——它就是 Unreachable 这一族）。
            raise EmbeddingUnreachable(
                f"无法连接 embedding 端点 {self._url}：{type(exc).__name__}"
            ) from exc
        if resp.status_code in RETRYABLE_STATUS:
            raise _RetryableStatus(resp.status_code)
        if resp.status_code >= 400:
            reason = _reason_from_body(resp.content)
            raise EmbeddingBadRequest(
                f"embedding 端点拒绝请求（HTTP {resp.status_code}）：{reason}"
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise EmbeddingBadResponse("embedding 端点回的不是 JSON") from exc
