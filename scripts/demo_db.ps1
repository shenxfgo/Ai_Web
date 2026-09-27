# 演示库 ai_web_demo 建库：探测 -> 只在库不存在时把 SQL 喂给 mysql -> 只读账号复验。
# 口径来自 docs/verification.md §1.2：只建不删、口令不进 git、探测不写任何东西。
#
# 需要你手工填一个已经存在的空模板（本脚本不碰你的建库口令，也不把它写进任何会被 grep 到的地方）：
#   backend/.setup/my_login.cnf      ; 目录已在 .gitignore 里
#   [client]
#   user=<有 CREATE DATABASE / CREATE USER 权限的账号>
#   password=<该账号口令>
# 跑完一次成功后，aiweb_ro 的口令会生成在 backend/.setup/aiweb_ro.cnf，登记数据源时从那里抄。
#
# 用法：make demo-db
#   或直接跑：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo_db.ps1
#   （Bypass 只作用于这一个进程，不改机器/用户的执行策略；本机默认策略是 Restricted，不加就进不来）

$ErrorActionPreference = "Stop"
# 控制台默认 cp936，SQL 里的中文和报告行会变乱码
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
$SetupDir = Join-Path $Root "backend\.setup"
$LoginCnf = Join-Path $SetupDir "my_login.cnf"
$RoCnf = Join-Path $SetupDir "aiweb_ro.cnf"
$SqlPath = Join-Path $Root "backend\scripts\init_demo_mysql.sql"
$LogDir = Join-Path $Root "logs"
$LogPath = Join-Path $LogDir "demo_mysql_apply.log"
$ErrPath = Join-Path $LogDir "demo_mysql_apply.err.log"
$DemoDb = "ai_web_demo"
$Placeholder = "__AIWEB_RO_PASSWORD__"

$Host_ = if ($env:AIWEB_DEMO_MYSQL_HOST) { $env:AIWEB_DEMO_MYSQL_HOST } else { "127.0.0.1" }
$Port = if ($env:AIWEB_DEMO_MYSQL_PORT) { $env:AIWEB_DEMO_MYSQL_PORT } else { "3306" }

function Invoke-Mysql {
    <# 用某个 defaults 文件跑一条 SQL。-AllowFail 时把非零退出码交回调用方判断，
       因为"以 aiweb_ro 写库被拒"这类验收本来就期待它失败。 #>
    param([string]$Cnf, [string]$Sql, [switch]$AllowFail)
    # 每个选项写成 --opt=value 单令牌：路径含空格时 PowerShell 的参数切分会把它拆开
    $cliArgs = @("--defaults-extra-file=$Cnf", "--host=$Host_", "--port=$Port",
                 "--default-character-set=utf8mb4", "--skip-column-names", "--batch", "--raw",
                 "-e", $Sql)
    Write-Host "+ mysql $($Sql.Substring(0, [Math]::Min(72, $Sql.Length)))"
    # EAP=Stop 时，原生命令往 stderr 写一个字都会被判成终止错误（NativeCommandError），
    # 于是"期待失败"的复验用例根本到不了下面的判断。这里临时降回 Continue，
    # 把 stderr 当普通文本收进 $out，由退出码来决定成败。
    $ErrorActionPreference = "Continue"
    $out = & mysql @cliArgs 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $AllowFail) {
        throw "mysql 退出码 $code ：$($out -join ' | ')"
    }
    return [pscustomobject]@{ Code = $code; Out = @($out | ForEach-Object { "$_" }) }
}

function Read-CnfValue {
    # 空值（`user=`）与"没这一行"都返回 $null：模板文件没填时要在连服务器之前就报清楚，
    # 而不是丢一个 1045 让人去猜是哪一行没填。
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) { return $null }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=\s*(\S.*?)\s*$'
    $line = Select-String -Path $Path -Pattern $pattern | Select-Object -First 1
    if (-not $line) { return $null }
    return $line.Matches[0].Groups[1].Value
}

function New-Password {
    # 只用字母数字：口令会出现在 SQL 字面量和 defaults 文件里，引号/反斜杠是自找麻烦
    $bytes = New-Object byte[] 32
    # GetRandomBytes 是 Windows PowerShell 那侧的静态便捷方法，pwsh(.NET 6+) 没有；
    # Create().GetBytes() 两边都在。
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    $chars = $bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] }
    return -join $chars
}

function Invoke-RoCheck {
    # 复验放在建库之外单列：库已经建好时（"已建过，跳过"分支）也要能重跑这三条，
    # 否则一次中途失败之后，只读授权就再也没被验证过第二次。
    if (-not (Test-Path $RoCnf)) {
        throw "$DemoDb 已存在，但 $RoCnf 不在了：aiweb_ro 的口令没人认领，无法复验只读。人工确认后重建库，或手工 SET PASSWORD。"
    }
    Write-Host "== 复验 aiweb_ro 只读 =="
    $roSelect = Invoke-Mysql -Cnf $RoCnf -Sql "SELECT COUNT(*) FROM $DemoDb.order_main" -AllowFail
    if ($roSelect.Code -ne 0) { throw "aiweb_ro 连不上或读不了 order_main：$($roSelect.Out -join ' | ')" }
    Write-Host "   SELECT order_main -> $($roSelect.Out -join '') 行（预期 30000）"
    # 只认"权限被拒"这一种失败：如果哪天它改成因为 id 冲突/字段不合法而失败，
    # 退出码同样非 0，但只读授权其实已经漏了——那就是假绿。
    $roInsert = Invoke-Mysql -Cnf $RoCnf -Sql "INSERT INTO $DemoDb.order_main (id, order_no, customer_id, status, amount, discount, pay_type, created_at, updated_at) VALUES (999999999,'X',1,'pending',0,0,'alipay',NOW(),NOW())" -AllowFail
    if ($roInsert.Code -eq 0) { throw "aiweb_ro 竟然写得进 order_main：只读授权没生效，检查 GRANT 语句" }
    if (($roInsert.Out -join ' ') -notmatch 'denied') {
        throw "aiweb_ro 的 INSERT 是被别的错误挡下的（不是权限拒绝），不算通过：$($roInsert.Out -join ' | ')"
    }
    Write-Host "   INSERT 被权限拒绝（预期）"
    $roCross = Invoke-Mysql -Cnf $RoCnf -Sql "SELECT COUNT(*) FROM mysql.user" -AllowFail
    if ($roCross.Code -eq 0 -or (($roCross.Out -join ' ') -notmatch 'denied')) {
        throw "aiweb_ro 没能被跨库读 mysql.user 挡住：权限给宽了（只该给 $DemoDb 一个库），实际：$($roCross.Out -join ' | ')"
    }
    Write-Host "   跨库读 mysql.user 被权限拒绝（预期）"
}

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path $LoginCnf)) {
    throw "$LoginCnf 不存在。按脚本顶部说明手工建好（含你的建库账号口令），本脚本不代填、不回显。"
}
foreach ($key in @("user", "password")) {
    if (-not (Read-CnfValue -Path $LoginCnf -Key $key)) {
        throw "$LoginCnf 的 $key= 还是空的，填好你本机建库账号的信息再跑（填了也别贴进对话）"
    }
}
if (-not (Test-Path $SqlPath)) { throw "找不到建库脚本：$SqlPath" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

Write-Host "== 1/4 只读探测 =="
$ver = Invoke-Mysql -Cnf $LoginCnf -Sql "SELECT VERSION()"
Write-Host "   server: $($ver.Out -join '')"

$exists = Invoke-Mysql -Cnf $LoginCnf -Sql "SHOW DATABASES LIKE '$DemoDb'"
if ($exists.Out -join '') {
    # 库已存在：只有当我们认得它（marker 在）时才什么都不做，否则绝不接管
    $marker = Invoke-Mysql -Cnf $LoginCnf -Sql `
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DemoDb' AND table_name='_aiweb_demo_marker'"
    if ([int]($marker.Out -join '') -eq 0) {
        throw "$DemoDb 已存在但没有 _aiweb_demo_marker：不是本脚本建的，拒绝接管。请改用别的库名或人工确认后再动。"
    }
    Write-Host "   已建过，跳过建库，只复验只读授权。如需重建请手工执行 DROP DATABASE $DemoDb（本脚本拒绝代劳）"
    Invoke-RoCheck
    Write-Host "完成。aiweb_ro 口令在 $RoCnf，登记数据源时从这里抄。"
    exit 0
}

# ---------------------------------------------------------------- ro 账号口令
$roPassword = Read-CnfValue -Path $RoCnf -Key password
if (-not $roPassword) {
    $roPassword = New-Password
    Write-Host "   生成 aiweb_ro 口令 -> $RoCnf（gitignored，稍后建数据源时用它）"
}

# 占位符替换后的 SQL 含真实口令，只能落在 gitignored 目录，且用完即删
$rendered = Join-Path $SetupDir "init_demo_mysql.rendered.sql"
try {
    $text = Get-Content -Path $SqlPath -Raw -Encoding UTF8
    if ($text -notmatch [regex]::Escape($Placeholder)) {
        throw "$SqlPath 里没有占位符 $Placeholder，脚本与外层已不同步"
    }
    $text = $text.Replace($Placeholder, $roPassword)
    # UTF-8 无 BOM：BOM 会跑到第一条语句前面，MySQL 当场语法错
    [System.IO.File]::WriteAllText($rendered, $text, (New-Object System.Text.UTF8Encoding($false)))

    Write-Host "== 2/4 执行建库脚本（全量日志 -> logs/demo_mysql_apply.log） =="
    # PowerShell 没有 `<` 重定向；Start-Process 的 -RedirectStandardInput 收文件路径，
    # 比把整串交给 cmd.exe 少一层引号地狱。口令只活在 rendered 文件里，不进命令行。
    # Start-Process 是把数组用空格拼成一条命令行的，不会替含空格的路径补引号
    # （这跟 `&` 原生调用不一样），所以这里自己包一层双引号。
    $apply = Start-Process -FilePath "mysql" -NoNewWindow -Wait -PassThru `
        -ArgumentList @("--defaults-extra-file=`"$LoginCnf`"", "--host=$Host_", "--port=$Port",
                        "--default-character-set=utf8mb4", "--table") `
        -RedirectStandardInput $rendered `
        -RedirectStandardOutput $LogPath -RedirectStandardError $ErrPath
    # 建库语句一旦报错，mysql 会把出错语句的原文片段抄进 stderr——那里面是替换后的
    # IDENTIFIED BY '<真实口令>'。日志目录虽然 gitignored，也不该让口令在磁盘上过夜。
    # 先抹掉口令，再决定要不要按失败退出。
    foreach ($log in @($LogPath, $ErrPath)) {
        if (Test-Path $log) {
            $body = Get-Content $log -Raw -Encoding UTF8
            if ($body -and $body.Contains($roPassword)) {
                [System.IO.File]::WriteAllText($log, $body.Replace($roPassword, "***"),
                                               (New-Object System.Text.UTF8Encoding($false)))
            }
        }
    }
    if ($apply.ExitCode -ne 0) {
        Get-Content $ErrPath -Tail 20 | ForEach-Object { Write-Host "   ! $_" }
        throw "mysql 退出码 $($apply.ExitCode)，见 $LogPath / $ErrPath"
    }

    Write-Host "== 3/4 自检裁决 =="
    $fails = @(Select-String -Path $LogPath, $ErrPath -Pattern '\bFAIL\b' | ForEach-Object { $_.Line.Trim() })
    $passes = @(Select-String -Path $LogPath, $ErrPath -Pattern '\bPASS\b').Count
    Write-Host "   自检通过项：$passes，失败项：$($fails.Count)"
    $fails | ForEach-Object { Write-Host "   FAIL> $_" }
    if ($fails.Count -gt 0) { throw "SQL 自检有 FAIL，见 $LogPath" }
    # PASS 条数下限：SQL 末尾有 9+5+1+6+3+1+1+1=27 行裁决。少于这个数说明某段查询压根没跑，
    # "没有 FAIL" 就变成了假绿。
    if ($passes -lt 27) { throw "自检裁决只有 $passes 行 PASS（应为 27 行），日志被截断或有段落没执行，见 $LogPath" }

    # 口令写盘放在脚本成功之后：失败了不该留下一份没人认领的凭证
    if (-not (Read-CnfValue -Path $RoCnf -Key password)) {
        $roCnfText = "# aiweb_ro 只读账号：登记数据源时用这份口令，勿提交`n[client]`nhost=${Host_}`nport=${Port}`nuser=aiweb_ro`npassword=${roPassword}`n"
        [System.IO.File]::WriteAllText($RoCnf, $roCnfText, (New-Object System.Text.UTF8Encoding($false)))
    }

    Invoke-RoCheck

    Write-Host "完成。aiweb_ro 口令在 $RoCnf，登记数据源时从这里抄。"
} finally {
    Remove-Item $rendered -ErrorAction SilentlyContinue
}
