"""闸内的伪向量端点（工单 028 / ADR-0015 / spec 故事 40、42）。

它是一个**真 HTTP 服务**：真监听端口、真收 POST `/v1/embeddings`、真按 OpenAI 的形状回话。
它不是 `EmbeddingClient` 的第二个实现——生产代码里那一个实现从头到尾不知道对面是谁，
把三键指到本脚本，`settings.embedding.configured` 就变真，于是 base_url 拼接、超时、
非 200、维度不符这四件事在闸里全部可达（ADR-0015 选它的正是这一条）。

向量内容是**字符 n-gram 哈希**，不是语义。它证的是形状与链路（名次齐、列宽对、档案切换
可回滚），**不证**"销售额命中 `payment_record`"那一类语义命中——那一句只有配真端点才签得出，
到位前语义那一格在收口里写"只有用例、没有真跑"（spec 故事 43）。

确定性是硬要求而不是便利：spec 故事 28 的 `ef_search` 单调性断言建立在"同一句问句两次
取向量得到同一个点"之上，随机向量会让单调性永远测不出东西。因此哈希走 `hashlib` 而**不用**
内置 `hash()`——后者按进程随机加盐（`PYTHONHASHSEED`），换一次进程同一个词就落到另一个点，
而 031 的用例可能跨两次运行比同一句问句。

用法（人手工）：

```
uv run python scripts/fake_embedding_server.py --port 8111
# 然后把 backend/.env 的三键指过去（DIMENSION 要与 model 对上）：
#   AIWEB_EMBEDDING__BASE_URL=http://127.0.0.1:8111/v1
#   AIWEB_EMBEDDING__API_KEY=fake            # 探针只要求非空，服务端不校验
#   AIWEB_EMBEDDING__MODEL=text-embedding-3-small
#   AIWEB_EMBEDDING__DIMENSION=1536
```

`--port 0`（默认）让 OS 挑空闲端口，并把实际地址打成 `LISTENING <host> <port>` 一行——
用例要的就是这一行，并行跑两份 pytest 时固定端口一定被人占着。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

# model 名 → 维度。**只有这两套**，未知名字当场 400：静默给一个默认宽度，等于让
# "配置里的维度"与"端点回的维度"两件事各自漂——那正是 §2.6 三元组要拦的东西。
# 名字取自仓库里已经在用的两个（`text-embedding-3-small` 是 1536 那一套，
# `bge-large-zh-v1.5` 是 027 用例里"只改模型名"那一档用的 1024 那一套）。
MODEL_DIMENSIONS: Final[dict[str, int]] = {
    "text-embedding-3-small": 1536,
    "bge-large-zh-v1.5": 1024,
}

# n-gram 的窗口：中文卡片大多是一到两个字的字段名/表名注释，unigram+bigram 才分得开
# "订单表"与"订单明细表"；三 gram 起会把长文本的桶打散，命中数看着多但没有额外信息。
_NGRAM_SIZES: Final = (1, 2)

# 固定 person 字符串：写进 blake2b 的参数而不是靠变量名，保证换机器换版本都是同一张哈希。
# （blake2 的 person 上限 16 字节，超长会在起服务第一条请求上就抛 ValueError。）
_KEY: Final = b"aiweb-fake-emb"


def _digest(token: str) -> int:
    """一个 n-gram → 一个 128 位整数。前 64 位定桶，后 64 位定符号。

    分成两半是因为"落哪一维"与"正还是负"必须是独立随机源：用同一个数的低位同时决定两者，
    偶数桶就永远是负号，向量会呈现和文本无关的结构性偏斜。
    """
    return int.from_bytes(
        hashlib.blake2b(token.encode("utf-8"), digest_size=16, person=_KEY).digest(), "big"
    )


def pseudo_vector(text: str, *, dimension: int) -> list[float]:
    """文本 → 定长、单位 L2 模长、**同输入必同输出**的伪向量。

    两个边界各有明确处置：
    - 空文本（或整串都没有可取的 n-gram）回**全零**而不是编一个"默认方向"。零向量是
      "这里没有信息"的诚实说法，硬给一个单位向量会让一个空卡片看起来和别的卡片可比。
    - 归一化让不同长度的文本落在同一个单位球面上：不归一时"长卡片"的点距只反映它字多，
      而 031 要比的是 `<=>`（余弦）——余弦本身会约掉模长，归一只是让替身回的数字
      也能被人直接读懂。
    """
    counts = [0.0] * dimension
    for size in _NGRAM_SIZES:
        for start in range(0, max(len(text) - size + 1, 0)):
            token = text[start : start + size]
            digest = _digest(token)
            index = digest % dimension
            sign = 1.0 if (digest >> 64) & 1 else -1.0
            counts[index] += sign
    norm = math.sqrt(sum(value * value for value in counts))
    if norm == 0.0:
        return counts
    return [value / norm for value in counts]


def embeddings_response(model: str, inputs: Sequence[str]) -> dict[str, Any]:
    """一批输入 → OpenAI `/v1/embeddings` 的响应体。

    `index` 逐条回填：客户端按它归位（`embeddings_from_response`），而契约上没有任何一条
    保证 `data` 与请求同序——替身要是漏了它，那条"按下标取"的错法就照样能过闸。
    """
    dimension = MODEL_DIMENSIONS[model]
    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": pseudo_vector(text, dimension=dimension),
        }
        for i, text in enumerate(inputs)
    ]
    return {
        "object": "list",
        "model": model,
        "data": data,
        # `usage` 不在客户端契约上（客户端不读它），但真端点都有；留着是为了验收 4：
        # 探针与替身之间不许有任何"只有替身才有的形状"。
        "usage": {"prompt_tokens": sum(len(t) for t in inputs), "total_tokens": 0},
    }


class _Handler(BaseHTTPRequestHandler):
    """只有两条路：POST `/v1/embeddings` 出向量，GET `/health` 给用例做就绪探测。

    其余一律 404，**包括** `POST /embeddings`——base_url 少了 `/v1` 是本脚本设计里最容易犯的
    错（`check_env.py:204` 那条探针就是拼 `{base_url}/embeddings`，版本段必须由 base_url 自带）。
    把它 404 掉而不是"顺手也接"，是为了让这种配错在闸里表现为一次可定位的失败。
    """

    server_version = "AiWebFakeEmbedding/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        """静音每请求一行的访问日志。

        这不是省事：用例把子进程的 stderr 并进 stdout 那根管道后就只读启动那一行，没人再
        排水；几百次 embed 之后管道缓冲一满，`ThreadingHTTPServer` 就卡在写日志上，
        表现成"客户端超时"——一个和向量毫无关系的红。失败原因本来就在响应体里（400/404
        都带 `error.message`），客户端会把它原样抬出来，不需要日志替它说话。
        """

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, code: str) -> None:
        """形状照 OpenAI：`{"error":{message,type,code}}`，客户端的取词逻辑才不用分两种端点。"""
        self._send(
            status,
            {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        )

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "models": sorted(MODEL_DIMENSIONS)})
        else:
            self._error(404, f"未知路径 {self.path}", "unknown_path")

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/embeddings":
            self._error(404, f"未知路径 {self.path}", "unknown_path")
            return
        raw = self.rfile.read(_read_int(self.headers.get("content-length")))
        try:
            request = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._error(400, f"请求体不是 JSON：{exc}", "bad_json")
            return
        model = request.get("model") if isinstance(request, dict) else None
        if not isinstance(model, str) or model not in MODEL_DIMENSIONS:
            # 不许静默兜一个默认宽度（工单 028 涉及层 ① 的字面要求）。
            self._error(
                400,
                f"未知 model：{model!r}，本替身只认 {sorted(MODEL_DIMENSIONS)}",
                "model_not_supported",
            )
            return
        inputs = _as_input_list(request.get("input"))
        if inputs is None:
            self._error(400, "input 必须是字符串或字符串数组", "bad_input")
            return
        self._send(200, embeddings_response(model, inputs))


def _read_int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def _as_input_list(value: Any) -> list[str] | None:
    """OpenAI 的 `input` 允许字符串或数组，两种都收；其余回 None 由调用方 400。

    非字符串数组（`[1,2]`、`["a", null]`）也回 None：与其在服务端把 2 悄悄变成 "2"，
    不如让这次调用失败——那说明客户端那边已经错了，替身没资格替它圆。
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="闸内伪向量端点（只服务测试，不写元数据库）")
    parser.add_argument("--host", default="127.0.0.1")
    # 默认 0 = 让 OS 挑：并行跑两份 pytest 时固定端口一定被人占着，而那件事在 CI 之外
    # 天天发生，所以"自己挑一个没人用的"必须是默认值而不是选项。
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    host, port = server.server_address[:2]
    # 这一行是用例的**就绪协议**：绑定完成后才打印，所以看到它就等于看到"能连了"。
    # 不带 -u 时子进程的 stdout 是块缓冲，这一行会一直躺在缓冲区里，等着它的用例会红成超时。
    print(f"LISTENING {host} {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
