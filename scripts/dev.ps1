param(
    [Parameter(Position = 0)]
    [string]$Target = "help"
)

$ErrorActionPreference = "Stop"
# 控制台默认 cp936，脚本里的中文提示会变乱码
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"

function Invoke-Step {
    param([string]$Dir, [string[]]$Command)
    Write-Host "+ $($Command -join ' ')  ($(Split-Path -Leaf $Dir))"
    # 参数必须存进变量再 @ 展开：直接写 @(...) 会被当成一整个参数传给 npm
    $exe = $Command[0]
    $rest = @()
    if ($Command.Length -gt 1) { $rest = $Command[1..($Command.Length - 1)] }
    Push-Location $Dir
    try {
        & $exe @rest
        if ($LASTEXITCODE -ne 0) { throw "退出码 $LASTEXITCODE：$($Command -join ' ')" }
    }
    finally { Pop-Location }
}

function Start-Dev {
    $back = Start-Process -PassThru -NoNewWindow -FilePath "uv" `
        -ArgumentList "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload" `
        -WorkingDirectory $Backend
    $front = Start-Process -PassThru -NoNewWindow -FilePath "npm" `
        -ArgumentList "run", "dev" -WorkingDirectory $Frontend
    Write-Host "后端 pid=$($back.Id) http://127.0.0.1:8000/api/docs"
    Write-Host "前端 pid=$($front.Id) http://127.0.0.1:5173"
    # 工单 016 之后同步是两条命令的事：API 只把作业写进队列，跑它的是 worker 进程。
    Write-Host "同步作业不在这里——另开一条：dev.ps1 worker（否则点同步只会停在 pending）"
    Write-Host "Ctrl+C 只停掉本脚本前台，两个子进程需自行结束（或任务管理器按 pid）"
    Wait-Process -Id $back.Id, $front.Id -ErrorAction SilentlyContinue
}

switch ($Target) {
    "help" {
        Write-Host "可用目标（与 Makefile 同名）："
        Write-Host "  bootstrap  check-env  migrate  seed-admin  demo-db"
        Write-Host "  dev  dev-backend  dev-frontend  worker"
        Write-Host "  lint  fmt  typecheck  test  check  clean"
    }
    "bootstrap" { Invoke-Step $Root @("python", "scripts/bootstrap.py") }
    "check-env" { Invoke-Step $Backend @("uv", "run", "python", "scripts/check_env.py") }
    "migrate" { Invoke-Step $Backend @("uv", "run", "alembic", "upgrade", "head") }
    "seed-admin" { Invoke-Step $Backend @("uv", "run", "python", "scripts/seed_admin.py") }
    "demo-db" { & (Join-Path $PSScriptRoot "demo_db.ps1") }
    "dev-backend" { Invoke-Step $Backend @("uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload") }
    "dev-frontend" { Invoke-Step $Frontend @("npm", "run", "dev") }
    "worker" { Invoke-Step $Backend @("uv", "run", "python", "scripts/run_worker.py") }
    "dev" { Start-Dev }
    "lint" { Invoke-Step $Backend @("uv", "run", "ruff", "check", "app", "scripts", "tests", "alembic") }
    "fmt" { Invoke-Step $Backend @("uv", "run", "ruff", "format", "app", "scripts", "tests", "alembic") }
    "typecheck" { Invoke-Step $Frontend @("npm", "run", "typecheck") }
    "test" { Invoke-Step $Backend @("uv", "run", "python", "-m", "pytest") }
    "check" {
        Invoke-Step $Backend @("uv", "run", "ruff", "check", "app", "scripts", "tests", "alembic")
        Invoke-Step $Frontend @("npm", "run", "typecheck")
        Invoke-Step $Backend @("uv", "run", "python", "-m", "pytest")
    }
    "clean" {
        Remove-Item -Recurse -Force (Join-Path $Backend ".pytest_cache"), (Join-Path $Backend ".ruff_cache"),
            (Join-Path $Backend ".mypy_cache"), (Join-Path $Backend ".uvtmp"), (Join-Path $Frontend "dist") `
            -ErrorAction SilentlyContinue
    }
    default { throw "未知目标：$Target（跑 dev.ps1 help 看清单）" }
}
