"""生产侧唯一的 embedding 客户端的**纯**半边（工单 028 / ADR-0015 / spec 故事 42）。

接缝：这个文件只碰两个不发 HTTP 的函数——`embeddings_request()`（把一批文本编成请求体）与
`embeddings_from_response()`（把响应体读回一批向量）。需要客户端**真发一次 HTTP** 的验收
（1/2/3/5/6/7）全部在 `tests/integration/test_embedding_server_live.py` 挂 `live`：ADR-0015
拍板"测试拓扑只 subprocess 一层"，所以这里不许出现 respx/transport 桩，那条口径本身也由
`test_构造签名里没有_transport_注入点` 钉着。

期望值口径（独立真相源，不是从实现反推）：
- 请求体形状照 OpenAI `/v1/embeddings`：键是 `model` 与 `input`，`input` 收**数组**（一次一批
  而不是一条一发——029 的 reindex 要逐表批量取向量，逐条发会把一颗作业变成几百次往返）。
  这个形状的外部依据是 `scripts/check_env.py:199-226` 那条探针，它就是照
  `{"model": ..., "input": [...]}` 发、照 `data[0].embedding` 读的；替身与真端点要在这个探针
  下无差别（工单 028 验收 4），所以客户端必须发同一个形状。
- 响应用 `data[i].index` 归位，而不是"按响应数组的下标当作输入的下标"：`index` 是 OpenAI
  契约里唯一说明"这条向量属于哪条输入"的字段，兼容端点不保证按请求顺序回。把顺序当默认
  成立，结果是卡片 A 挂着卡片 B 的向量——库里两行都合法、检索从此指错表，没有任何一层会报错。
- 维度不符是**一整档专属错误**（spec 故事 15：029 靠它把整颗作业判 `failed`），文案里
  期望维度与实收维度两个数都要在场（工单 028 验收 5）。
- 形状坏（缺 `data`、条数与请求数对不上）落 `EmbeddingBadResponse` 而不是
  `KeyError`/`JSONDecodeError`：与 `llm_client._message_content` 同一条理由——裸异常冒出去
  是一个没有 envelope 的 500。
"""

from __future__ import annotations

import inspect

import pytest

from app.services.embedding_client import (
    RETRYABLE_STATUS,
    EmbeddingBadResponse,
    EmbeddingClient,
    EmbeddingDimensionMismatch,
    embeddings_from_response,
    embeddings_request,
)

# 三条各 2 维的手写响应：向量内容本身就是期望值，不从任何实现算出来。
_THREE = {
    "data": [
        {"index": 0, "embedding": [1.0, 0.0]},
        {"index": 1, "embedding": [0.0, 1.0]},
        {"index": 2, "embedding": [0.5, 0.5]},
    ]
}


def test_请求体是_openai_兼容形状_model_加_input_数组() -> None:
    """工单 028 涉及层「逻辑层」：一次一批，`input` 收数组。

    期望的字面量是手写的，不是从 `embeddings_request` 的输出抄的——这条断的是"发出去的
    键名与嵌套形状"，而那是外部契约（OpenAI 兼容），实现里改一个键名就该当场红。
    """
    assert embeddings_request(model="text-embedding-3-small", inputs=["订单表", "客户表"]) == {
        "model": "text-embedding-3-small",
        "input": ["订单表", "客户表"],
    }


def test_单条输入也发数组而不是裸字符串() -> None:
    """一张表只有一张卡片时走的必须是同一条路。

    OpenAI 允许 `input` 是字符串或数组，但兼容端点不一定两者都支持；而"单条走字符串、
    批量走数组"会让客户端有两条请求形状，测试只证得到其中一条。
    """
    assert embeddings_request(model="m", inputs=["只有一张表"]) == {
        "model": "m",
        "input": ["只有一张表"],
    }


def test_请求体一次带一批而不是循环单发() -> None:
    """批量是 reindex 的前提：逐表取向量时一条卡片一发 HTTP，一颗作业就变成几百次往返。"""
    payload = embeddings_request(model="m", inputs=[f"t{i}" for i in range(5)])
    assert payload["input"] == ["t0", "t1", "t2", "t3", "t4"]


def test_逐条按_index_归位而不是按响应的下标() -> None:
    """兼容端点不保证按请求顺序回，而 `index` 是契约里唯一说明"这条属于哪条输入"的字段。

    这里把 `data` 故意倒序摆。若实现按下标取，回的就是 `[0.5,0.5],[0,1],[1,0]` 而不是
    `[1,0],[0,1],[0.5,0.5]`——那种错在库里两行都合法，只是卡片 A 从此挂着卡片 B 的向量，
    检索永远指错表。
    """
    shuffled = {"data": list(reversed(_THREE["data"]))}
    assert embeddings_from_response(shuffled, expected_dimension=2, count=3) == [
        [1.0, 0.0],
        [0.0, 1.0],
        [0.5, 0.5],
    ]


def test_维度不符时抬专属那一档且两个数都在文案里() -> None:
    """工单 028 验收 5 / spec 故事 15 的原料：期望 3 维、实收 2 维。

    这一档必须**单独存在**而不是折进 BadResponse：029 靠它把整颗作业判 `failed`，而
    "端点回的东西不成形"（重试也许能救）与"端点回的维度不对"（要换列类型才能救，重试一万次
    也一样）是两种处置。文案里两个数都要在场——只说"维度不符"等于让人去猜是哪一格配错了。
    """
    wrong = {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
    with pytest.raises(EmbeddingDimensionMismatch) as caught:
        embeddings_from_response(wrong, expected_dimension=3, count=1)
    message = str(caught.value)
    assert "3" in message and "2" in message, message
    assert caught.value.code == "embedding_dimension_mismatch"


def test_响应里没有_data_时落_bad_response_而不是_KeyError() -> None:
    """兼容端点经常用 200 裹一个 `{error:...}`（`llm_client._message_content` 的同一条理由）。

    断的是异常类型：KeyError 冒出去是一个没有 envelope 的裸 500，而这一档的意义就在于
    "端点回了话但说的不是 embeddings"，运维要看得懂。
    """
    with pytest.raises(EmbeddingBadResponse):
        embeddings_from_response({"error": {"message": "no route"}}, expected_dimension=2, count=1)


def test_响应少回一条时落_bad_response_而不是_KeyError() -> None:
    """请求三条、端点只回两条：这一档必须是"响应对不上请求"，而不是"字典取不到键"。

    另一条可走的路是让它悄悄返回两条（调用方 zip 一下就错配），但那正是最坏的形状：卡片与
    向量数量不等时，取模或 zip 短对齐都会把**后面的**向量挂到**前面的**卡片上，一条错误日志
    都不会有。`KeyError` 冒出去同样不行——它是裸异常，envelope 都没有。
    """
    short = {"data": _THREE["data"][:2]}
    with pytest.raises(EmbeddingBadResponse):
        embeddings_from_response(short, expected_dimension=2, count=3)


def test_条目缺_index_或_index_不是数时落_bad_response() -> None:
    """`index` 也是要被读的外部字段，不是"实现里可以直接取"的内部约定。

    缺它 → `KeyError`、写 `"abc"` → `ValueError`：两者都是没有 envelope 的裸 500，而调用方
    看到的错还会指向"服务器内部错误"，明明是兼容端点回了一份不合规的 body。这一条与上面
    "缺 data"同族，期望的档也是同一个 `EmbeddingBadResponse`。
    """
    for payload in (
        {"data": [{"embedding": [1.0, 0.0]}]},  # 缺 index
        {"data": [{"index": "abc", "embedding": [1.0, 0.0]}]},  # index 不是数
        {"data": [{"index": None, "embedding": [1.0, 0.0]}]},  # index 是 null
    ):
        with pytest.raises(EmbeddingBadResponse):
            embeddings_from_response(payload, expected_dimension=2, count=1)


def test_向量元素不是数时落_bad_response而不是_TypeError() -> None:
    """`{"embedding": ["a", null]}` 这类端点是真存在的（把向量当 JSON 字符串数组回的兼容端点）。

    不加这层的话 `[float(v) for v in ...]` 抛 `TypeError`，那是裸异常；而它跟"宽度不对"
    也不是一档——宽度那条要判整颗作业 `failed`，这一档是可以再试一次的形状坏。
    """
    for payload in (
        {"data": [{"index": 0, "embedding": ["a", "b"]}]},
        {"data": [{"index": 0, "embedding": [1.0, None]}]},
    ):
        with pytest.raises(EmbeddingBadResponse):
            embeddings_from_response(payload, expected_dimension=2, count=1)


def test_重试词表里没有_不被重试的那几档() -> None:
    """工单 028 验收 3 的另一半：**400 不许进重试表**。

    词表本身抄的是 `llm_client` 那一份（同一个上游故障在两条链路上不该得出一种要重试、
    一种不重试的分派）。这一条钉的是"未知 model 名"那一档的代价：400 进了表，那次调用
    会先退避 0.5+1.0s 再抛同一个错，而错还是那个错——重试只对"暂时的故障"有意义。
    """
    assert frozenset({408, 429, 500, 502, 503, 504}) == RETRYABLE_STATUS
    assert 400 not in RETRYABLE_STATUS
    assert 401 not in RETRYABLE_STATUS
    assert 429 in RETRYABLE_STATUS


def test_构造签名里没有_transport_注入点() -> None:
    """工单 028 验收 8 —— 这是**口径**不是遗漏（ADR-0015 拍板，用户否掉的双层方案）。

    `LlmClient.__init__` 收 `http: httpx.AsyncClient`（`llm_client.py:89`），`EmbeddingClient`
    有意不收：收了就等于允许第二种替身，而第二种替身会让"真 HTTP 那一路"（base_url 拼接、
    超时、非 200、维度不符）永远没有证据。这条断的是参数表本身，所以将来有人"为了好测"
    加一个 `transport=` 时它会红，而不是一句注释慢慢过期。
    """
    params = inspect.signature(EmbeddingClient.__init__).parameters
    forbidden = {"http", "client", "transport", "async_client"}
    assert not forbidden & set(params), sorted(forbidden & set(params))
    # 类型面同样不许出现 httpx：只查参数名会放过 `def __init__(self, *, session: httpx.AsyncClient)`
    annotations = " ".join(str(p.annotation) for p in params.values())
    assert "httpx" not in annotations, annotations
