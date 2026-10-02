"""替身 + 真客户端的一次真 HTTP 往返（工单 028 验收 1/2/3/5/6/7 / spec 故事 40、42）。

为什么整份都挂 `live`（ADR-0015 已定口径，不是偷懒的反面）：测试拓扑**只 subprocess 一层**，
需要 embedding 客户端的用例一律真起进程、真占端口、真发 HTTP。`EmbeddingClient` 的构造签名
里有意没有 httpx/transport 参数，所以这里连"打个桩"的口子都没有——那口子一开，
base_url 拼接、超时、非 200、维度不符这四件事就再也没有真跑证据。接缝那条断言住在
`tests/unit/test_embedding_client.py::test_构造签名里没有_transport_注入点`。

期望值口径（都是测试自己声明的字面量，不从响应里读回来再比）：
- 维度期望 `1536` / `1024` 来自工单 028 涉及层 ① 的"两套"，替身那边只是把它实现出来；
  从响应 `len()` 读回来比是**同义反复**，替身哪天回错宽度就没人知道。
- "确定"指的是**逐位相同**（同一个 list 逐项相等），不是"模长接近 1"或"前几位一样"：
  spec 故事 28 的 `ef_search` 单调性要的是"同一句问句两次取到同一个点"，那一点必须
  一个比特都不差，否则名次集合的超集关系是掷骰子掷出来的。
- 错误档位断的是**异常类型**（`EmbeddingBadRequest` / `EmbeddingDimensionMismatch` /
  `EmbeddingUnreachable`），不是状态码字符串：029 的分派读的是类型与 `code`。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from app.services.embedding_client import (
    EmbeddingBadRequest,
    EmbeddingClient,
    EmbeddingDimensionMismatch,
    EmbeddingNotConfigured,
    EmbeddingUnreachable,
)
from app.settings import EmbeddingGroup, Settings
from tests.conftest import BACKEND_ROOT, load_script
from tests.integration.conftest import FakeEmbedding, spawn_fake_embedding

# 验收 4 打的是**探针**，不是客户端：探针有自己的 httpx.post 与判据，替身要在它下面无差别，
# 所以这里调的是 `check_env.check_embedding` 而不是 `EmbeddingClient`。
CHECK_ENV = load_script("check_env", BACKEND_ROOT / "scripts" / "check_env.py")

# 替身认识的两个 model 名与它们各自的宽度（工单 028 涉及层 ① 的字面："1536 与 1024 两套"）。
_MODEL_1536 = "text-embedding-3-small"
_MODEL_1024 = "bge-large-zh-v1.5"


def _group(base_url: str, *, model: str = _MODEL_1536, dimension: int = 1536) -> EmbeddingGroup:
    """一个"四格都齐了"的配置组，指向替身。

    `api_key` 填的是假值而不是留空：`EmbeddingGroup.configured`（`app/settings.py`）要求四格齐
    （`base_url` / `api_key` / `model` / `dimension>0`），留空的话这里测的就变成"未配置"那一档了。
    替身不校验它。
    """
    return EmbeddingGroup(base_url=base_url, api_key="fake-key", model=model, dimension=dimension)


def _client(
    fake: FakeEmbedding, *, model: str = _MODEL_1536, dimension: int = 1536
) -> EmbeddingClient:
    return EmbeddingClient(embedding=_group(fake.base_url, model=model, dimension=dimension))


async def test_同一句问句两次调用逐位相同换一个问句就不同(fake_embedding: FakeEmbedding) -> None:
    """工单 028 验收 1 / spec 故事 42：确定性是硬要求。

    两个方向一起断："同输入同输出"钉的是哈希不带随机（内置 `hash()` 按进程加盐，换一次
    进程就漂）；"不同输入不同输出"钉的是它不是一常数——只断前者的话，一个永远回同一个
    向量的替身照样绿，而 031 的名次会全部并列。
    """
    client = _client(fake_embedding)
    first = await client.embed(["订单主表 order_main 销售额"], expected_dimension=1536)
    second = await client.embed(["订单主表 order_main 销售额"], expected_dimension=1536)
    assert first == second
    assert first[0] != (await client.embed(["客户表 customer 手机号"], expected_dimension=1536))[0]


async def test_一批里的一条与它单发时是同一个点(fake_embedding: FakeEmbedding) -> None:
    """批量不是另一种算法：reindex 逐表批量取，检索逐句问，两边必须落在同一个点。

    替身若在批量里换一种编号（比如把 index 参与哈希），029 写进库的向量与 032 查询时的
    向量就不在同一个位置上，而 `<=>` 仍然回一个像模像样的距离。
    """
    client = _client(fake_embedding)
    alone = await client.embed(["支付记录 payment_record"], expected_dimension=1536)
    in_batch = await client.embed(
        ["无关的一句", "支付记录 payment_record", "另一句"], expected_dimension=1536
    )
    assert in_batch[1] == alone[0]


async def test_换一个替身进程同一个问句还是同一个点(fake_embedding: FakeEmbedding) -> None:
    """确定性必须是**跨进程**成立的，不只是"同一份服务连着问两次"。

    夹具是 function 作用域，每条用例起一份新的替身；031 的名次断言可能拿这一轮起的替身写库、
    下一轮起的替身查询。内置 `hash()` 恰恰在这里骗人：它在单个进程内稳定（上一条用例查不出
    问题），但按 `PYTHONHASHSEED` 逐进程加盐，换一次进程同一个词就落到另一个点上。
    变异验过：把替身里的 `hashlib.blake2b` 换成 `hash()`，只有这一条红。
    """
    other = spawn_fake_embedding()
    try:
        first = await _client(fake_embedding).embed(
            ["订单主表 order_main 销售额"], expected_dimension=1536
        )
        again = await _client(other).embed(["订单主表 order_main 销售额"], expected_dimension=1536)
        assert first == again
    finally:
        other.terminate()


@pytest.mark.parametrize(
    ("model", "expected_dimension"),
    [(_MODEL_1536, 1536), (_MODEL_1024, 1024)],
    ids=["1536 那一套", "1024 那一套"],
)
async def test_按_model_名返回两套宽度且期望值是测试自己声明的(
    fake_embedding: FakeEmbedding, *, model: str, expected_dimension: int
) -> None:
    """工单 028 验收 2：宽度由 **model 名**决定，不是由配置里的 dimension 决定。

    期望值写死在参数表里。从 `len(响应)` 读回来比是同义反复，替身回错宽度时那条断言
    会跟着一起改错。这一条同时钉住"换维度=同表第二列"（ADR-0012）的前提：同一句话在
    1024 那一套里落在另一个位置上。
    """
    vectors = await _client(fake_embedding, model=model, dimension=expected_dimension).embed(
        ["订单表"], expected_dimension=expected_dimension
    )
    assert len(vectors[0]) == expected_dimension
    if expected_dimension == 1024:
        other = await _client(fake_embedding, model=_MODEL_1536, dimension=1536).embed(
            ["订单表"], expected_dimension=1536
        )
        # 不同宽度的两个点根本不在同一个空间里，这里只钉"替身确实按名字分了套"
        assert len(other[0]) == 1536


async def test_未知_model_名落成可辨识的一档而不是_JSON_解析错(
    fake_embedding: FakeEmbedding,
) -> None:
    """工单 028 验收 3：替身当场 400（不许静默给一个默认宽度），客户端把它翻成一档错误。

    "不是 JSONDecodeError" 是这条断言的存在理由：400 的响应体仍是 JSON，实现只要漏一个
    状态码检查就会去读 `data`，那时用户看到的是"响应解析失败"，而真原因是"这个 model 名
    端点不认识"。文案里要带出那个名字，否则运维要自己去猜是哪一格拼错了。
    """
    with pytest.raises(EmbeddingBadRequest) as caught:
        await _client(fake_embedding, model="text-embedding-9-huge").embed(
            ["订单表"], expected_dimension=1536
        )
    assert "text-embedding-9-huge" in str(caught.value), str(caught.value)


async def test_维度不符时那一档专属错误里两个数都在场(fake_embedding: FakeEmbedding) -> None:
    """工单 028 验收 5 / spec 故事 15 的原料：配置写 1024、端点按 model 名回 1536。

    这一档必须存在且**只此一档**：029 靠它把整颗作业判 `failed`（不截断、不补零、不降级
    只写关键词）。两个数在场是文案契约——只说"维度不符"的那条失败信息，读的人仍然不知道
    该改 `.env` 还是该建新档案。
    """
    with pytest.raises(EmbeddingDimensionMismatch) as caught:
        await _client(fake_embedding, model=_MODEL_1536, dimension=1024).embed(
            ["订单表"], expected_dimension=1024
        )
    message = str(caught.value)
    assert "1536" in message and "1024" in message, message


async def test_期望宽度由调用方给_客户端不读自己配置里那一格(fake_embedding: FakeEmbedding) -> None:
    """工单 029 已定口径「reindex 用 draft 档案自己的三元组」落在客户端签名上的这一半。

    配置组写 1024、调用方要 1536：按老写法（宽度取自 `self._embedding.dimension`）这里会抬
    维度不符，于是"这台机器的 `.env` 与那套档案不同宽"的一颗 reindex 作业整颗死于维度不符，
    而该改的是配置、不是这颗作业。**这一条专门盖住那条退路**：签名收必填关键字参数由
    `tests/unit/test_sync_embed_sql.py` 钉形状，这里钉的是"真的按它判"。
    """
    vectors = await _client(fake_embedding, model=_MODEL_1536, dimension=1024).embed(
        ["订单表"], expected_dimension=1536
    )
    assert len(vectors[0]) == 1536, len(vectors[0])


async def test_端点没在听时落连不上那一档且秒回(fake_embedding: FakeEmbedding) -> None:
    """工单 028 验收 6 + 7 一条包完：杀掉进程后端口不再响应，客户端也不会挂住。

    两件事共用同一个事实（同一个端口先起后杀）：
    - 验收 6"没有残留进程"= 这条之后 `GET /health` 必须连不上。若 terminate 没杀干净，
      下一条用例会连到上一条的僵尸，端口号还会撞上，那种红最难查。
    - 验收 7"不是超时挂住 30 秒"= 拒连当场落成 `EmbeddingUnreachable`。耗时上限给 10s，
      离客户端自己的 60s 超时还有一大截，所以它红的时候一定是"挂住"而不是机器慢。
    """
    fake_embedding.terminate()
    with pytest.raises(httpx.TransportError):
        httpx.get(fake_embedding.health_url, timeout=3)

    started = time.monotonic()
    with pytest.raises(EmbeddingUnreachable):
        await _client(fake_embedding).embed(["订单表"], expected_dimension=1536)
    assert time.monotonic() - started < 10.0, "连不上不该等满超时"


async def test_自己起的替身杀完之后那个端口上再没有第二个进程(
    fake_embedding: FakeEmbedding,
) -> None:
    """验收 6 的另一半：夹具那份是"用例结束时杀"，这里证"杀了就真的没了"。

    复用 `fake_embedding` 夹具只是为了拿到"起一份"的能力，随后**另起一份**再杀：那份
    由本用例 own，`finally` 之后端口必须拒绝连接。这条红的形态是"下一条用例连上了上一条
    的僵尸"——那是并行跑闸时最难复现的一种串扰。
    """
    extra = spawn_fake_embedding()
    try:
        assert await _client(extra).embed(["临时起的一份"], expected_dimension=1536) != []
    finally:
        extra.terminate()
    with pytest.raises(httpx.TransportError):
        httpx.get(extra.health_url, timeout=3)


async def test_三键没齐时抬未配置而不是去撞空_url() -> None:
    """工单 028 涉及层「逻辑层」：`configured` 为假时**不发 HTTP**。

    不占端口，所以它住在这份 live 文件里也仍然不需要替身（配置组走默认空值）。
    让它去撞空 base_url 的后果是"连不上"那一档：明明是这台机器没启用向量路，
    报出来的却是"端点不通"，运维会去查一个根本没配的地址。
    """
    client = EmbeddingClient(embedding=EmbeddingGroup())
    with pytest.raises(EmbeddingNotConfigured):
        await client.embed(["订单表"], expected_dimension=1536)


def test_探针在替身上报_ok_而不是只有真端点才过(fake_embedding: FakeEmbedding) -> None:
    """工单 028 验收 4 的前半边：`check_env.py` 那条 embedding 探针在替身下必须报 ok。

    期望值口径是**探针自己的判据**（状态 `ok` + 消息里的维度数），不是替身的返回宽度：
    替身与真端点要在这个探针下无差别，这条就是那个"无差别"的证据。探针走的是它自己的
    httpx.post（不是 `EmbeddingClient`），所以这里证的确实是"运维敲的那条命令"。
    """
    checks: list[Any] = []
    CHECK_ENV.check_embedding(
        Settings(
            _env_file=None,
            embedding={
                "base_url": fake_embedding.base_url,
                "api_key": "fake-key",
                "model": _MODEL_1536,
                "dimension": 1536,
            },
        ),
        checks,
    )
    got = {c.name: c for c in checks}
    assert got["embedding.dimension"].status == "ok", got["embedding.dimension"].message
    assert "1536" in got["embedding.dimension"].message


def test_探针那条维度比对分支能真的动一次(fake_embedding: FakeEmbedding) -> None:
    """工单 028 验收 4 的后半边：配置 1024、端点按 model 名回 1536 时必须 fail 且点得出两个数。

    这一条是"替身不许只陪成功路径"的核心证据：那条 fail 分支自 0005 写下以来从没在没有真端点的
    机器上跑过，实现只要把它写反（比 `>=` 而非 `!=`）或文案丢掉一个数，今天没有任何闸会红。
    """
    checks: list[Any] = []
    CHECK_ENV.check_embedding(
        Settings(
            _env_file=None,
            embedding={
                "base_url": fake_embedding.base_url,
                "api_key": "fake-key",
                "model": _MODEL_1536,
                "dimension": 1024,
            },
        ),
        checks,
    )
    got = {c.name: c for c in checks}
    assert got["embedding.dimension"].status == "fail", got["embedding.dimension"].message
    assert "1536" in got["embedding.dimension"].message, got["embedding.dimension"].message
    assert "1024" in got["embedding.dimension"].message, got["embedding.dimension"].message
