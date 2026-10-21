"""`scripts/check_env.py` 的密钥判定、目录判定与第二层开关那一档。

判定本身早就写好了，这里补的是"它真的按环境分档"这件事的证据：prod 用默认密钥必须是
fail（非 0 退出码），local 只是 warn（否则谁都起不来）。目录那一条是 013 加的：体检报出的
落点必须与执行器真会写的落点是同一个（safety §7 的"同一个基准"）。
第二层开关那一档是 037 加的（safety §3.3），分档方向与 jwt 那条**相同但动机不同**：
jwt 那条管的是"这一台机器现在安不安全"，dry_run 那条管的是"prod 不许把这层关掉"——
它是**策略闸**不是能力探测，所以判据只看两个键的组合，不连库、不发语句。

期望值口径：`docs/roadmap.md` 分组 5（JWT secret ≥32B → fatal）、`docs/nl2sql-safety.md` §7、
§3.3（prod + `dry_run=False` → 报红并点名是哪个键；dev/test 同一组合只报黄不阻断）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.settings import Settings
from tests.conftest import load_script

BACKEND_ROOT = Path(__file__).resolve().parents[2]

CHECK_ENV = load_script("check_env", BACKEND_ROOT / "scripts" / "check_env.py")


def _jwt_verdict(environment: str) -> str:
    # 元素是 check_env.Check —— 跨模块的类不在这里重复声明
    checks: list[Any] = []
    # _env_file=None：不吃本机 .env，否则测的是"这台机器的密钥"而不是判定规则
    CHECK_ENV.check_secrets(Settings(_env_file=None, app={"environment": environment}), checks)
    return next(c.status for c in checks if c.name == "jwt.secret")


def test_默认jwt密钥在prod判fail() -> None:
    assert _jwt_verdict("prod") == "fail"


def test_默认jwt密钥在local只判warn() -> None:
    assert _jwt_verdict("local") == "warn"


def _vector_verdict(dimension: int, exts: dict[str, str]) -> list[Any]:
    checks: list[Any] = []
    settings = Settings(_env_file=None, embedding={"dimension": dimension})
    CHECK_ENV.check_embedding_gate(settings, checks, exts)
    return checks


def test_向量扩展在维度已定时缺失判fail_并给出建扩展的那句话() -> None:
    """roadmap §5 自检表第 2 行 as-built(P2-0005)：判据是 `DIMENSION>0`，不是"启用向量路"。

    0005 之后列类型 `vector(dim)` 就引用着这个扩展，端点配没配都一样。
    """
    got = {c.name: c for c in _vector_verdict(1536, {})}
    assert got["pg.ext.vector"].status == "fail"
    assert "CREATE EXTENSION vector" in got["pg.ext.vector"].hint
    assert "超级用户" in got["pg.ext.vector"].hint or "DBA" in got["pg.ext.vector"].hint


def test_扩展在位时只报版本号不报问题() -> None:
    got = {c.name: c for c in _vector_verdict(1536, {"vector": "0.8.6"})}
    assert got["pg.ext.vector"].status == "ok"
    assert got["pg.ext.vector"].message == "0.8.6"


def test_维度为0时报的是配置而不是扩展() -> None:
    """`DIMENSION=0` 时扩展在不在都没意义——0005 第一步就拒绝，hint 必须把人指向那个键。"""
    got = {c.name: c for c in _vector_verdict(0, {"vector": "0.8.6"})}
    assert got["embedding.dimension"].status == "fail"
    assert "AIWEB_EMBEDDING__DIMENSION" in got["embedding.dimension"].hint
    assert "pg.ext.vector" not in got


def test_目录体检报的就是执行器会写的那个目录(tmp_path: Path) -> None:
    """safety §7 的"同一个基准"收口后，体检侧不许再自己拼一次根。

    012 之前这里写的是 `BACKEND_ROOT.parent / path`，执行器用的是原样的相对 `Path`（跟 cwd 走），
    于是两处报出来的目录可以互相错开而两边都显示 ok——那一格的用途正好被掏空。
    现在两边都只读 Settings 里那个已解析的绝对值，所以断言的是**同一个值**而不是两个式子。
    """
    settings = Settings(
        _env_file=None,
        result={"dir": str(tmp_path / "res")},
        kb_docs={"dir": str(tmp_path / "kb")},
    )
    checks: list[Any] = []
    CHECK_ENV.check_dirs(settings, checks)
    got = {c.name: c for c in checks}
    assert [got["result.dir"].status, got["kb_docs.dir"].status] == ["ok", "ok"]
    assert got["result.dir"].message == str(settings.result.dir)
    assert got["kb_docs.dir"].message == str(settings.kb_docs.dir)
    # 探针文件写完即删，目录留着：ok 的证据是"这个目录真能建文件"
    assert (tmp_path / "res").is_dir() and (tmp_path / "kb").is_dir()
    assert not (tmp_path / "res" / ".aiweb_write_probe").exists()


def _dry_run_verdict(environment: str, dry_run: bool) -> Any:
    checks: list[Any] = []
    CHECK_ENV.check_dry_run_gate(
        Settings(_env_file=None, app={"environment": environment}, query={"dry_run": dry_run}),
        checks,
    )
    return next(c for c in checks if c.name == "query.dry_run")


def test_prod关掉第二层判fail并点名那个键() -> None:
    """safety §3.3 那一档的字面：prod + `dry_run=False` → **报红并点名是哪个键**。

    红必须落在 `query.dry_run` 这一档上（`main()` 的退出码只数 fail 项），且正文里要出现
    键名本身——"第二层被关了"这句话人读得到但改不了，`AIWEB_QUERY__DRY_RUN=false` 才是可执行的信息。
    """
    got = _dry_run_verdict("prod", False)
    assert got.status == "fail"
    assert "AIWEB_QUERY__DRY_RUN" in got.message or "AIWEB_QUERY__DRY_RUN" in got.hint


def test_local与staging关掉第二层只报黄不阻断() -> None:
    """同一组合在非 prod 只报黄——§3.3 的原话理由："本节只禁 prod，禁到 dev 上就是给自己加墙"。

    `staging` 归"不阻断"这一边，与上面 jwt 那两条用的是同一条判据（`environment == "prod"` 才红）。
    这里把三个环境逐个点名而不是只测 local，是因为"非 prod"这种写法最容易哪天被改成
    "`!= local`"——那一改 staging 就起不来了，而它正是排查大查询时要能关掉这一层的地方。
    """
    assert _dry_run_verdict("local", False).status == "warn"
    assert _dry_run_verdict("staging", False).status == "warn"


def test_开着时任何环境都报ok且不挂hint() -> None:
    """默认值 `true` 在三个环境下都是 ok——这一档不许变成"prod 才检查"之外的另一种噪音。

    断 `hint` 为空是顺带钉住 `Check.render()` 的行为：warn/fail 才渲染 hint 那一行，
    ok 档挂了 hint 也不显示，但会让人以为脚本里有第二条没落地的判据。
    """
    for environment in ("local", "staging", "prod"):
        got = _dry_run_verdict(environment, True)
        assert got.status == "ok", environment
        assert got.hint == ""
