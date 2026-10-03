"""pg 用例共用的"真库 + ASGI 客户端"夹具。

放在 integration/ 这一层而不是顶层 conftest：只有打真库的用例才需要它们，
顶层那份管的是"随机 schema + 真迁移"（见 `tests/conftest.py::pg_migrated`）。
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import deps
from app.core.security import hash_password
from app.main import create_app
from app.models.kb import KbIndexProfile
from app.models.user import User
from app.services.kb_service import ActiveProfile, configured_triplet, profile_name
from app.settings import get_settings

BACKEND = Path(__file__).resolve().parents[2]
CNF = BACKEND / ".setup" / "aiweb_ro.cnf"


class DbAccount(NamedTuple):
    """演示库的连接四元组，**口令不参与 repr**。

    为什么要自己写 `__repr__`：pytest 一失败就会把 fixture 的实参和整条调用栈的帧局部变量
    倒进终端——用 tuple 的默认 repr，那条输出里第二位就是真口令，一次红测等于把口令贴进
    控制台、CI 日志和会话记录。它仍是 tuple 的子类，解包和 `tuple[str, str, str, int]`
    的注解都照旧。
    """

    user: str
    password: str
    host: str
    port: int

    def __repr__(self) -> str:
        host, port = self.host, self.port
        return f"DbAccount(user={self.user!r}, password=<redacted>, host={host!r}, port={port})"


@pytest.fixture(scope="module")
def account() -> DbAccount:
    """演示库的 `(user, password, host, port)`——口令只活在这条调用链里。

    读的是 gitignored 的 `.setup/aiweb_ro.cnf`（工单 002 生成的只读账号）：把它抄进
    `.env` 或测试常量里，就等于把一台真库的入口写进会被 push 的文件。
    缺文件就 skip，换台机器不该把整个闸口卡红。
    """
    if not CNF.exists():
        pytest.skip(f"缺少 {CNF}：要先跑工单 002 的建库脚本")
    cp = configparser.ConfigParser()
    cp.read(CNF, encoding="utf-8")
    c = cp["client"]
    return DbAccount(
        c["user"], c["password"], c.get("host", "127.0.0.1"), int(c.get("port", "3306"))
    )


# ================================================== 闸内伪向量替身（工单 028 / ADR-0015）
#
# 需要 embedding 客户端的用例**全部**走这里：ADR-0015 拍板"只 subprocess 一层"，所以没有
# 第二条路（transport 注入）可走。这份 helper 住在这里而不是每个用例文件各抄一遍，是因为
# 029 的 reindex 与 031 的向量路都要起同一个替身——那时"端口怎么读、僵尸怎么杀"就是三处口径。


@dataclass
class FakeEmbedding:
    """替身的一次运行：`base_url` 交给客户端，`terminate()` 交给 `finally`。

    `host`/`port` 单独留着只有一个用途：验收 6 要在杀完之后再探一次端口，证明下一条用例
    不可能连到上一条的僵尸。
    """

    proc: subprocess.Popen[str]
    host: str
    port: int

    @property
    def base_url(self) -> str:
        """OpenAI 兼容的 base_url，**带版本段**：`check_env.py:204` 那条探针拼的是
        `{base_url}/embeddings`，版本段必须由 base_url 自带。验收 4 要的就是客户端与探针
        用同一个 base_url 拼法。
        """
        return f"http://{self.host}:{self.port}/v1"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    def terminate(self) -> None:
        """活着就 terminate，10s 内不退再 kill；之后关掉管道，别让解释器等一个没人写的 fd。"""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self.proc.stdout is not None:
            self.proc.stdout.close()


def spawn_fake_embedding(*, fail_on: Sequence[str] = ()) -> FakeEmbedding:
    """起一份替身并**等它把端口打出来**。

    为什么读 stdout 而不是自己挑端口：`--port 0` 让 OS 分配，两份 pytest 并行时固定端口
    一定被人占着（同 `test_sync_events_live.py` 的 `_free_port` 那条理由）；而"看到了
    LISTENING 这一行"就是就绪信号——它在 `server_bind()` 之后才打印，比"睡一秒再试"诚实。

    `-u` 不是可选项：子进程的 stdout 连着管道时 Python 默认**块缓冲**，那一行会一直躺在
    缓冲区里，调用方就红成"读不到端口"。

    `fail_on` 是工单 029 验收 6 的注入通道（"含这些文本的那一发回 500"）。它走**启动参数**
    而不是给 `run_sync` 加第二个 test-only 形参，是工单里的已定口径：019 的 `card_build_hook`
    已经是签名上的代价，别再叠一个——而且向量路的失败本来就该长在"端点自己坏了"那一侧。
    """
    stub = BACKEND / "scripts" / "fake_embedding_server.py"
    cmd = [sys.executable, "-u", str(stub), "--port", "0"]
    for token in fail_on:
        cmd += ["--fail-on", token]
    proc = subprocess.Popen(
        cmd,
        cwd=str(BACKEND),
        # cp936 控制台上子进程一打印中文就炸在解码里，真正的启动错误反而看不见
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        # 并进同一根管道：不接 PIPE 的 stderr 没人读，缓冲区满时替身会卡在写日志上。
        # 替身自己不打印请求日志（`log_message` 已静音），所以这里进来的只有 traceback。
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout is not None
    seen: list[str] = []
    for line in proc.stdout:
        seen.append(line.rstrip())
        if line.startswith("LISTENING "):
            _, host, port = line.split()
            return FakeEmbedding(proc=proc, host=host, port=int(port))
    # 管道 EOF = 进程已经死了。把看到的原文倒出来，否则红测只留下一句"没读到端口"。
    raise RuntimeError(f"替身没起来（退出码 {proc.poll()}）：\n" + "\n".join(seen))


@pytest.fixture
def fake_embedding() -> Iterator[FakeEmbedding]:
    """一条用例一份替身，结束杀干净（工单 028 验收 6）。

    为什么是 function 作用域而不是 session：验收 7 要在同一个文件里**杀掉一份再去连它**，
    共用一份就没法这么干；而起的成本只是一次解释器启动。029 的 reindex 若嫌慢，那时再来
    谈作用域，别现在为不存在的耗时预付。
    """
    handle = spawn_fake_embedding()
    try:
        yield handle
    finally:
        handle.terminate()


@pytest.fixture
def fake_embedding_with_switches() -> Iterator[Callable[..., FakeEmbedding]]:
    """要**启动开关**的替身（工单 029 验收 6 的 `--fail-on`；031 若要错宽度 model 也走这里）。

    与 `fake_embedding` 分开而不是给它加参数：那条夹具的 13 处调用要的正是"默认那份成功的替身"，
    加一个可选参数位就等于允许用例忘记杀——这里把杀的活收在夹具的 `finally` 里，用例只负责起。
    """
    spawned: list[FakeEmbedding] = []

    def build(**kw: Any) -> FakeEmbedding:
        handle = spawn_fake_embedding(**kw)
        spawned.append(handle)
        return handle

    try:
        yield build
    finally:
        for handle in spawned:
            handle.terminate()


@pytest.fixture(autouse=True)
def embedding_endpoint_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """整条 integration 目录默认"没有向量端点"，要真端的用例自己盖回来。

    工单 029 之后 embed 档是**真发 HTTP**的：四格齐（`settings.embedding.configured`）时
    `run_sync` 会逐表去问端点。本机 `.env` 此刻是空的，所以闸看起来没事——而"看起来没事"
    正是这条夹具的存在理由：换一台把这四格指向真端点的机器，几十条 pg 用例会在别人的账单上
    跑几百次 embedding，而且用例之间还会互相抢那个端点的限流。测试不许花真钱，是 012 起
    对 LLM 端点同一条纪律（那侧的形态是"live 用例只在自己的文件里打"）。

    只钉 `BASE_URL` 与 `API_KEY` 两格而不是四格全钉：`configured` 判的是四格齐（`BASE_URL` /
    `API_KEY` / `MODEL` / `DIMENSION`），而
    `configured_triplet()`（027 那道守卫的 settings 那一侧）读的是 `MODEL` 与 `DIMENSION`——
    把 MODEL 也清空会让"档案行与配置同意"那一族用例的档案名变成 `@1536@tplv1`，
    夹具就顺手改掉了它不该改的一件事。

    要真端点的用例（029 的 `configured`、031 的向量路）在同名键上再 `setenv` 一次：pytest
    把 autouse 排在同作用域的其余夹具之前，所以后写的那一份赢，盖的方向是确定的。
    """
    monkeypatch.setenv("AIWEB_EMBEDDING__BASE_URL", "")
    monkeypatch.setenv("AIWEB_EMBEDDING__API_KEY", "")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
async def session_factory(
    pg_migrated: None, fernet_key: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """真迁移之后的会话工厂；每条用例从空表开始。

    `fernet_key` 挂在这里是必须的：不显式给一把，加解密就会去用开发者机器上的真密钥，
    而 `backend/.setup` 里那把是要能解真库口令的。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    # 删除顺序按 FK 来：kb_card 挂在 data_sources 上级联，而它对 kb_index_profile 是 RESTRICT，
    # 所以必须先清 data_sources，profile 才删得掉。
    #
    # profile 为什么也在这一张清单上：随机 schema 是**整轮会话共用**的，用例造的第二个
    # profile（例如把老 profile 摘掉 active 再建一套新的那种）不会随 data_sources 一起走，
    # 于是全会话后头每一条"取当前生效 profile"的语句都会撞上它——表现是检索莫名其妙返回空。
    #
    # 往这张清单里加东西的时机：新表一有 DDL 就要跟上，否则"每条用例从空表开始"这句话
    # 会从这行注释开始骗人。012 的 chat_sessions/chat_messages 已跟上（删序照 FK：
    # messages → sessions → data_sources，sessions 对 users 是 CASCADE，但显式删才不依赖
    # 外键方向对不对）。还没建表的 datasource_grants 在这儿排队。
    #
    # 017 的 `sync_job_event` **不在**这张清单上，是有意的而不是漏了：它对 `sync_jobs` 是
    # `ON DELETE CASCADE`，而 `sync_jobs.datasource_id` 对 `data_sources` 同样是 CASCADE，
    # 所以上面那句 `DELETE FROM data_sources` 把它们一起带走了。判断"要不要往清单里加一行"
    # 的依据是 FK 的 ondelete，不是表名新不新。
    async with engine.begin() as conn:
        await conn.execute(text(f'DELETE FROM "{schema}".chat_messages'))
        await conn.execute(text(f'DELETE FROM "{schema}".chat_sessions'))
        await conn.execute(text(f'DELETE FROM "{schema}".data_sources'))
        await conn.execute(text(f'DELETE FROM "{schema}".kb_index_profile'))
        await conn.execute(text(f'DELETE FROM "{schema}".users'))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def insert_active_profile(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    model: str,
    dimensions: int,
    card_template_version: int,
) -> int:
    """摆一行 `status='active'` 的档案并 commit，回它的 id。

    027 关掉"同步链路按 settings 自己 ensure 一行档案"那条后门之后的代价：链路上再也不会
    自己长出基准行，想要卡片的用例就得自己把这一行摆出来。以前"先同步、卡片自己冒出来"
    那个前提已经不成立了。

    这是**同步链路那一族**用例共用的"档案行长什么样"（要 worker 看得见、要 commit 的那一类）：
    `provider` 的字面（§2.6 注④）、`name` 的三段拼法（`profile_name()`）只住这里。029 的
    reindex 与 031 的收缩都改过这一行的形状，两处都只动这里。
    读侧那几族（要 draft/retired、要在用例自己的会话里不提交的）各造各的，它们要的不是这一行。

    **必须 commit**：worker 用的是另一条连接（工单 016 的进程分离），未提交的行在它眼里不存在。
    0005 那条镜像列不写、也写不了：031 的收缩迁移（0009）已经把它从库里删了，
    读写两侧的尺子从此只有 `status` 一把。
    """
    async with session_factory() as session:
        profile = KbIndexProfile(
            name=profile_name(
                model, dimension=dimensions, card_template_version=card_template_version
            ),
            provider="openai_compatible",
            model=model,
            dimensions=dimensions,
            card_template_version=card_template_version,
            status="active",
        )
        session.add(profile)
        await session.flush()
        profile_id = int(profile.id)
        await session.commit()
        return profile_id


@pytest.fixture
async def active_profile(
    session_factory: async_sessionmaker[AsyncSession],
) -> ActiveProfile:
    """摆一行 `status='active'` 的档案当基准（工单 027 之后"卡片从哪来"的唯一答案）。

    为什么每条想看到卡片的用例都要它：027 关掉了"同步链路自己按 settings 长基准行"那条后门，
    链路上再也不会自己长出档案行——"先同步、卡片自己冒出来"的老前提已经不成立，
    这是关掉后门的代价，不是回归（工单 027 已定口径）。

    三元组取自 `configured_triplet()`：这条 fixture 的任务是"与这台机器的配置同意"，
    让守卫放行。守卫那一族用例（"不一致时要告警不写卡"）走的是同一个
    `insert_active_profile()`，只是把三元组显式摆成 `triplet` 夹具钉住的那一套，
    再改配置侧 —— 形状共用、字面各自独立，否则用例的期望值就成了从夹具算出来的同义反复。
    """
    model, dimension, tplv = configured_triplet()
    profile_id = await insert_active_profile(
        session_factory, model=model, dimensions=dimension, card_template_version=tplv
    )
    return ActiveProfile(
        index_profile_id=profile_id,
        name=profile_name(model, dimension=dimension, card_template_version=tplv),
        model=model,
        dimensions=dimension,
        card_template_version=tplv,
    )


@pytest.fixture
def application(session_factory: async_sessionmaker[AsyncSession], jwt_secret: str) -> FastAPI:
    """装配好依赖 override 的应用实例，`client` 与各片自己的夹具**共用同一个对象**。

    单独成一个夹具是因为 `dependency_overrides` 是应用上的可变字典：SSE 那一片要在 `get_db`
    之外再 override 一个会话来源（流用的不是请求级会话，见 `deps.get_stream_sessionmaker`），
    加在同一个实例上才等于"两个夹具打的是同一个应用"，而不是各搓一个客户端去连不同的库。
    """

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[deps.get_db] = override_get_db
    return app


@pytest.fixture
async def client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    """打真 ASGI 应用，但 `get_db` 指向测试库的引擎。

    走 `dependency_overrides` 而不是改环境变量：应用自己的连接池会连到本机 .env
    那套 host/库上，测试就会写到真元数据库去。

    注意 `ASGITransport` 是**整响应缓冲**的（它把 body 片段攒成一个 list 才返回），所以它
    测不出"流式"——帧的顺序与内容照样能测，但"首帧早于作业结束"那种时序只能打真 HTTP
    服务（见 `test_sync_events_live.py`）。
    """

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://t") as c:
        yield c


@dataclass
class Account:
    """一个已登录身份：headers 直接拿去发请求，user_id 用来断言 owner 判定。"""

    user_id: int
    headers: dict[str, str]


@pytest.fixture
def login(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[..., Awaitable[Account]]:
    """建号 + 真走一次 `/auth/login`，返回可直接用的身份。

    走真登录而不是手搓 JWT：令牌里的 `tv`、citext 的大小写匹配、get_current_user 的
    三道门都顺带被验一遍。桩出来的身份会让"改密后旧令牌失效"那类用例失去对象。
    """

    async def _login(*, username: str, role: str = "member", password: str = "p-12345") -> Account:
        async with session_factory() as session:
            user = User(username=username, role=role, password_hash=hash_password(password))
            session.add(user)
            await session.commit()
            uid = user.id
        resp = await client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        return Account(
            user_id=uid, headers={"Authorization": f"Bearer {resp.json()['access_token']}"}
        )

    return _login


@pytest.fixture
def results_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把结果目录钉进 tmp_path：早退用例要断言"一根 csv 都没落"，就得知道该看哪个目录。

    012 起放在这里而不是某个测试模块里：编排用例与 live 用例都要它，而 fixture 靠
    参数名解析，跨模块 import 会被 ruff 判成 F811（同名重定义）。
    """
    target = tmp_path / "results"
    monkeypatch.setenv("AIWEB_RESULT__DIR", str(target))
    get_settings.cache_clear()
    try:
        yield target
    finally:
        get_settings.cache_clear()
