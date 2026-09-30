# 演示源库 ai_web_demo_pg 开通：探测 -> 建库 -> 把 init_demo_pg.sql 喂给 psql -> 只读账号复验。
# 口径来自 docs/verification.md §1.5 与工单 015：只建不删、口令不进命令行、探测不写任何东西。
#
# 需要你手工填一个已经存在的空模板（本脚本不碰你的建库口令，也不把它写进任何会被 grep 到的地方）：
#   backend/.setup/pg_login.env      ; 目录已在 .gitignore 里
#   PGHOST=127.0.0.1
#   PGPORT=5432
#   PGUSER=<有 CREATEDB / CREATE ROLE 权限的账号，本机是超级用户>
#   PGPASSWORD=<该账号口令>
# 跑完一次成功后，demo_pg_ro 的口令生成在 backend/.setup/demo_pg_ro.env，登记数据源时从那里抄。
#
# 口令为什么走 PGPASSFILE 而不是 PGPASSWORD 环境变量（与 002 的 --defaults-extra-file 同一路由）：
# 环境变量会进子进程的环境块、命令行参数会进 argv 与历史，而 passfile 只把"文件路径"交给环境，
# 口令本身留在 gitignored 的临时文件里，且用完即删。
#
# 用法：make demo-db-pg
#   或直接跑：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo_pg.ps1
#   （Bypass 只作用于这一个进程，不改机器/用户的执行策略；本机默认策略是 Restricted，不加就进不来）
#   psql 不在 PATH 上时设 AIWEB_PSQL=<psql.exe 全路径>

$ErrorActionPreference = "Stop"
# 控制台默认 cp936，SQL 里的中文和报告行会变乱码
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
$SetupDir = Join-Path $Root "backend\.setup"
$LoginEnv = Join-Path $SetupDir "pg_login.env"
$RoEnv = Join-Path $SetupDir "demo_pg_ro.env"
$SqlPath = Join-Path $Root "backend\scripts\init_demo_pg.sql"
$LogDir = Join-Path $Root "logs"
$LogPath = Join-Path $LogDir "demo_pg_apply.log"
$DemoDb = "ai_web_demo_pg"
# 建库脚本里 order_main 的行数（init_demo_pg.sql 的行数断言用的是同一个数）。
# 复验拿它当基准，是为了让"连错库/连到一份同名但不同内容的库"这件事在只读复验阶段就红，
# 而不是等到 024 抽取时才发现卡片建在空库上。
$DemoOrderMainRows = 2000
# 建库这条语句必须在别的库里发；postgres 是唯一的公共维护库。
$MaintDb = "postgres"
$Placeholder = "__AIWEB_PG_RO_PASSWORD__"

function Find-Psql {
    if ($env:AIWEB_PSQL -and (Test-Path $env:AIWEB_PSQL)) { return $env:AIWEB_PSQL }
    $cmd = Get-Command psql.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # EDB 安装器不把 psql 放进 PATH 是常事；能猜中就省一次配置，猜不中如实报错
    $guess = @(Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe",
                          "D:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1)
    if ($guess.Count) { return $guess[0].FullName }
    throw "找不到 psql.exe。把它加进 PATH，或设 AIWEB_PSQL=<psql.exe 全路径>（EDB 装法形如 D:\Program Files\PostgreSQL\18\bin\psql.exe）"
}
$Psql = Find-Psql

function Read-EnvValue {
    # 空值（`PGUSER=`）与"没这一行"都返回 $null：模板没填时要在连服务器之前就报清楚，
    # 而不是丢一个认证失败让人去猜是哪一行没填。
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) { return $null }
    $line = Select-String -Path $Path -Pattern ('^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$') |
        Select-Object -First 1
    if (-not $line) { return $null }
    $value = $line.Matches[0].Groups[1].Value.Trim()
    if ($value -eq "") { return $null }
    return $value
}

function New-Password {
    # 只用字母数字：口令要同时进 passfile（冒号是字段分隔符）和 SQL 字面量，
    # 特殊字符逼着两处都做转义，是自找麻烦。与 002 的 aiweb_ro 同一套字母表。
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    $chars = $bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] }
    return -join $chars
}

function Escape-PgPassField {
    # libpq 的 passfile 用冒号分列，所以反斜杠和冒号都得转义（先转反斜杠，否则把刚补的反斜杠又转一遍）
    param([string]$Value)
    $out = $Value -replace '\\', '\\'
    return $out.Replace(':', '\:')
}

function PgPassLine {
    param([string]$Database, [string]$User, [string]$Password)
    return "${Host_}:${Port}:$($Database):$($User):$(Escape-PgPassField $Password)"
}

function Write-Scrubs {
    # 日志与终端都可能沾到替换后的口令（CREATE ROLE ... PASSWORD '<明文>' 报错时 psql 会抄原文），
    # 所以任何一条 psql 的输出在落地/回显之前都先过一遍脱敏。
    # 除了整串替换，还按"字符之间允许插入任意空白"再替换一遍：psql 的表格输出与错误 CONTEXT 行
    # 会在折行处插进换行，整串替换对那种形态是无效的，而口令恰恰只在那种形态下最容易被抄出来。
    param([string[]]$Lines, [string[]]$Secrets)
    $body = ($Lines -join "`r`n") + "`r`n"
    foreach ($secret in @($Secrets | Where-Object { $_ })) {
        if ($body.Contains($secret)) { $body = $body.Replace($secret, "***") }
        $flex = -join ([char[]]$secret | ForEach-Object { [regex]::Escape("$_" ) + '\s*' })
        $body = [regex]::new($flex, 'IgnoreCase').Replace($body, '***')
    }
    return $body
}

function Invoke-Psql {
    <# 统一入口：凭证只来自环境（PGHOST/PGPORT/PGUSER + PGPASSFILE），命令行里永远没有口令。
       -AllowFail 时把非零退出码交回调用方判断，因为"以 demo_pg_ro 读隔壁 schema 被拒"
       这类验收本来就期待它失败。 #>
    param([string]$Database, [string]$Sql, [string]$File, [string[]]$Secrets = @(), [switch]$AllowFail, [switch]$Table)
    $cliArgs = @("--no-psqlrc", "--no-password", "-v", "ON_ERROR_STOP=1", "-d", $Database)
    # --no-password：passfile 没匹配上时直接失败，而不是挂在交互式提示上等输入。
    # 这四个长选项都在 `psql --help` 里点过名（-X/-w/-q/-t/-A）。注意 psql **没有** `--table`
    # （只有 `-T/--table-attr`）——对齐表格是它的默认输出，所以 -Table 那一支什么都不加。
    if (-not $Table) { $cliArgs += @("--quiet", "--tuples-only", "--no-align") }
    if ($Sql)  { $cliArgs += @("-c", $Sql) }
    if ($File) { $cliArgs += @("-f", $File) }
    $shown = if ($Sql) { $Sql } else { [IO.Path]::GetFileName($File) }
    Write-Host "+ psql -d $Database $($shown.Substring(0, [Math]::Min(72, $shown.Length)))"
    # EAP=Stop 时，原生命令往 stderr 写一个字都会被判成终止错误（NativeCommandError），
    # 于是"期待失败"的复验用例根本到不了下面的判断。这里临时降回 Continue，
    # 把 stderr 当普通文本收进 $out，由退出码来决定成败。
    $ErrorActionPreference = "Continue"
    $out = & $Psql @cliArgs 2>&1
    $code = $LASTEXITCODE
    $lines = @($out | ForEach-Object { "$_" })
    if ($code -ne 0 -and -not $AllowFail) {
        throw "psql 退出码 $code ：$(Write-Scrubs -Lines $lines -Secrets $Secrets)"
    }
    return [pscustomobject]@{ Code = $code; Out = $lines }
}

function Parse-Count {
    # psql 的 --tuples-only 输出可能夹一个空行；直接 [int]"" 会抛"输入字符串格式不正确"，
    # 把一条本来正常的探测变成看不懂的终止错误。这里只认第一个纯数字行。
    param([string]$Text)
    $match = [regex]::Match($Text, '\d+')
    if (-not $match.Success) { throw "期望一个数字，实际拿到：'$($Text -replace '\s+', ' ')" }
    return [int]$match.Value
}

function Confirm-PgPass {
    # passfile 的机密性靠 ACL，不靠"目录被 gitignore"。Windows 上没有 POSIX 的 0600，
    # 但 EDB 那套 libpq 在 Windows 上压根不检查 passfile 权限，所以这里补一次显式的
    # "只有当前用户能读"授权；不给就删掉重来，别让两份真口令停在别人可读的文件里。
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    try {
        $acl = Get-Acl $Path
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid, "FullControl", "Allow")
        $acl.SetAccessRuleProtection($true, $false)   # 断开继承：只留显式授权
        $acl.AddAccessRule($rule)
        Set-Acl -Path $Path -AclObject $acl
    } catch {
        Write-Host "   （警告）passfile 授权收紧失败，仍按原样使用：$($_.Exception.Message)"
    }
}

function Remove-PgPass {
    param([string]$Path)
    if (Test-Path $Path) { Remove-Item $Path -Force -ErrorAction SilentlyContinue }
}

function Invoke-RoCheck {
    <# 复验放在建库之外单列：库已经建好时（"已建过，跳过"分支）也要能重跑这四条，
       否则一次中途失败之后，只读授权就再也没被验证过第二次。
       注意必须把进程内的 PGUSER 换成 demo_pg_ro —— 不换的话这三条是拿超管在跑，
       "应该被拒"的用例会全绿成"竟然读得到"，把授权判反。 #>
    param([string]$PassFile, [string]$RoPassword)
    if (-not (Test-Path $RoEnv)) {
        throw "$DemoDb 已存在，但 $RoEnv 不在了：demo_pg_ro 的口令没人认领，无法复验只读。人工确认后重建，或手工 ALTER ROLE。"
    }
    if (-not $RoPassword) { throw "$RoEnv 的 PGPASSWORD= 是空的，无法复验只读" }
    # 库名写成 * 是故意的：下面要换到别的库（aiweb）去证书级读权限。
    # passfile 只匹配 $DemoDb 的话那次连接会因"没提供口令"而失败，
    # 而那种失败对一个本来有权限的账号同样成立 —— 拿它当"被拒"是假绿。
    [System.IO.File]::WriteAllText($PassFile,
        (PgPassLine -Database "*" -User "demo_pg_ro" -Password $RoPassword) + "`n",
        (New-Object System.Text.UTF8Encoding($false)))
    $env:PGUSER = "demo_pg_ro"

    Write-Host "== 复验 demo_pg_ro 只读 =="
    $roSelect = Invoke-Psql -Database $DemoDb -Sql "SELECT COUNT(*) FROM demo.order_main" `
        -Secrets @($RoPassword) -AllowFail
    if ($roSelect.Code -ne 0) { throw "demo_pg_ro 连不上或读不了 demo.order_main：$(Write-Scrubs -Lines $roSelect.Out -Secrets @($RoPassword))" }
    # 这一条也断言数值，不只是"能连能读"：只读账号读到的行数应当与建库自检报的数一致，
    # 否则说明它读的是另一个库/另一份数据（比如 marker 判断走岔了）。
    $roRows = Parse-Count ($roSelect.Out -join '')
    if ($roRows -ne $DemoOrderMainRows) {
        throw "demo_pg_ro 读到 demo.order_main 是 $roRows 行，与建库期望的 $DemoOrderMainRows 行不符：确认连的是 $DemoDb 而不是别的同名库"
    }
    Write-Host "   SELECT demo.order_main -> $roRows 行（与建库期望一致）"

    # 只认"权限被拒"这一种失败：哪天它改成因为别的错误而失败，退出码同样非 0，
    # 但只读授权其实已经漏了——那就是假绿。
    $roCross = Invoke-Psql -Database $DemoDb -Sql "SELECT COUNT(*) FROM other_app.secret_table" `
        -Secrets @($RoPassword) -AllowFail
    if ($roCross.Code -eq 0) { throw "demo_pg_ro 竟然读得到 other_app.secret_table：越权闸门没关（该 schema 一个权限都没授）" }
    if (($roCross.Out -join ' ') -notmatch 'permission denied') {
        throw "demo_pg_ro 读 other_app 是被别的错误挡下的（不是权限拒绝），不算通过：$(Write-Scrubs -Lines $roCross.Out -Secrets @($RoPassword))"
    }
    Write-Host "   跨 schema 读 other_app.secret_table 被权限拒绝（预期）"

    $roWrite = Invoke-Psql -Database $DemoDb -Sql "INSERT INTO demo.t_no_comment (id, kind) VALUES (999999, 'ro-write-probe')" `
        -Secrets @($RoPassword) -AllowFail
    if ($roWrite.Code -eq 0) { throw "demo_pg_ro 竟然写得进 demo.t_no_comment：只授 SELECT 没生效，检查 GRANT 语句" }
    if (($roWrite.Out -join ' ') -notmatch 'permission denied') {
        throw "demo_pg_ro 的 INSERT 是被别的错误挡下的（不是权限拒绝），不算通过：$(Write-Scrubs -Lines $roWrite.Out -Secrets @($RoPassword))"
    }
    Write-Host "   INSERT 被权限拒绝（预期）"

    # 跨库那一格在 PG 侧长得不一样：PG 没有"一条 SQL 里跨库引用表"的语法（MySQL 版可以
    # SELECT FROM mysql.user），所以要分两层看。这一格证的是**表级读权限**——
    # 换到 aiweb 库里去读它自己的 aiweb.users，读不到才算只授了一个库。
    # 这是一条 SELECT，不改任何东西、更不发 DDL；连不上也算拒绝。
    $roOther = Invoke-Psql -Database "aiweb" -Sql "SELECT count(*) FROM aiweb.users" `
        -Secrets @($RoPassword) -AllowFail
    if ($roOther.Code -eq 0) {
        throw "demo_pg_ro 读得到元数据库 aiweb 的表：权限给宽了（只该给 $DemoDb），后面的验收一律不碰 aiweb"
    }
    if (($roOther.Out -join ' ') -notmatch 'permission denied') {
        throw "demo_pg_ro 读 aiweb.users 的失败原因里看不到 permission denied，不能算通过：$(Write-Scrubs -Lines $roOther.Out -Secrets @($RoPassword))"
    }
    Write-Host "   读元数据库 aiweb.users 被权限拒绝（预期）"
}

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path $LoginEnv)) {
    throw "$LoginEnv 不存在。按脚本顶部说明手工建好（含你的建库账号口令），本脚本不代填、不回显。"
}
foreach ($key in @("PGUSER", "PGPASSWORD")) {
    if (-not (Read-EnvValue -Path $LoginEnv -Key $key)) {
        throw "$LoginEnv 的 $key= 还是空的，填好你本机建库账号的信息再跑（填了也别贴进对话）"
    }
}
if (-not (Test-Path $SqlPath)) { throw "找不到建库脚本：$SqlPath" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$Host_ = Read-EnvValue -Path $LoginEnv -Key "PGHOST"; if (-not $Host_) { $Host_ = "127.0.0.1" }
$Port = Read-EnvValue -Path $LoginEnv -Key "PGPORT"; if (-not $Port) { $Port = "5432" }
$AdminUser = Read-EnvValue -Path $LoginEnv -Key "PGUSER"
$AdminPassword = Read-EnvValue -Path $LoginEnv -Key "PGPASSWORD"

# 环境里只放非敏感的三件 + passfile 路径；PGPASSWORD 从头到尾不设
$env:PGHOST = $Host_
$env:PGPORT = $Port
$env:PGUSER = $AdminUser
$env:PGCLIENTENCODING = "UTF8"

$PassFile = Join-Path $SetupDir "pg_probe.pass"
$Rendered = Join-Path $SetupDir "init_demo_pg.rendered.sql"
try {
    $roExisting = Read-EnvValue -Path $RoEnv -Key "PGPASSWORD"
    $lines = @()
    if ($roExisting) {
        # ro 那行排在前面：libpq 取第一处匹配，别让它被下面的通配行抢先（同 user 不同库也能分开）
        $lines += PgPassLine -Database $DemoDb -User "demo_pg_ro" -Password $roExisting
    }
    $lines += PgPassLine -Database "*" -User $AdminUser -Password $AdminPassword
    # UTF-8 无 BOM：BOM 会粘在第一条记录的 host 上，libpq 直接匹配不上
    [System.IO.File]::WriteAllText($PassFile, ($lines -join "`n") + "`n",
        (New-Object System.Text.UTF8Encoding($false)))
    Confirm-PgPass -Path $PassFile
    $env:PGPASSFILE = $PassFile

    Write-Host "== 1/4 只读探测 =="
    $ver = Invoke-Psql -Database $MaintDb -Sql "SELECT version()" -Secrets @($AdminPassword)
    Write-Host "   server: $((($ver.Out | Select-Object -First 1) -replace '\s+', ' '))"

    # 维护库的编码只报不断言：真会写乱码的是新建那个库，它在 init_demo_pg.sql 里有自己的守卫。
    # 报出来是为了让下面 CREATE DATABASE 那句万一失败时一眼看懂——集群不是 UTF8 的话
    # template0 + ENCODING 'UTF8' 是要配 locale 的，不看这个数只能瞎猜。
    $maintEnc = Invoke-Psql -Database $MaintDb -Sql `
        "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname=current_database()" `
        -Secrets @($AdminPassword)
    Write-Host "   维护库 $MaintDb 编码：$((($maintEnc.Out -join ' ') -replace '\s+', ' ').Trim())"

    $exists = Invoke-Psql -Database $MaintDb -Sql `
        "SELECT count(*) FROM pg_database WHERE datname='$DemoDb'" -Secrets @($AdminPassword)
    if ((Parse-Count ($exists.Out -join '')) -gt 0) {
        # 库已存在：只有当我们认得它（marker 在）时才什么都不做，否则绝不接管
        $marker = Invoke-Psql -Database $DemoDb -Sql `
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='demo' AND c.relname='_aiweb_demo_marker' AND c.relkind='r'" `
            -Secrets @($AdminPassword)
        if ((Parse-Count ($marker.Out -join '')) -eq 0) {
            throw "$DemoDb 已存在但 schema demo 里没有 _aiweb_demo_marker：不是本脚本建的，拒绝接管。请改用别的库名/schema 或人工确认后再动。"
        }
        Write-Host "   已建过，跳过建库，只复验只读授权。如需重建请手工执行 DROP DATABASE $DemoDb（本脚本拒绝代劳）"
        Invoke-RoCheck -PassFile $PassFile -RoPassword $roExisting
        Write-Host "完成。demo_pg_ro 口令在 $RoEnv，登记数据源时从这里抄。"
    } else {
        # 角色是集群级对象，库没了它还在（人工 DROP DATABASE、或上次跑到一半失败）。
        # init_demo_pg.sql 对已存在的角色是"跳过创建、沿用旧口令"，于是这里新生成的口令根本不会生效，
        # 只读复验会以"口令不对"失败——看起来像脚本坏了。宁可在这里把话说清。
        $orphan = Invoke-Psql -Database $MaintDb -Sql `
            "SELECT count(*) FROM pg_roles WHERE rolname='demo_pg_ro'" -Secrets @($AdminPassword)
        if ((Parse-Count ($orphan.Out -join '') -gt 0) -and (-not $roExisting)) {
            throw "库 $DemoDb 不存在，但集群里还留着角色 demo_pg_ro，而 $RoEnv 也不在了：那份口令没人认领，而本脚本不会改角色口令（只建不删、不接管）。请人工 DROP ROLE demo_pg_ro 后重跑，或自己 ALTER ROLE 再把新口令填进 $RoEnv。"
        }

        # ------------------------------------------------------------ ro 账号口令
        $roPassword = $roExisting
        if (-not $roPassword) {
            $roPassword = New-Password
            Write-Host "   生成 demo_pg_ro 口令 -> $RoEnv（gitignored，稍后建数据源时用它）"
        }
        $secrets = @($AdminPassword, $roPassword)

        Write-Host "== 2/4 建库 + 执行建库脚本（全量日志 -> logs/demo_pg_apply.log） =="
        # 走到这里库一定不存在（存在那条分支已在上面收口），所以没有覆盖别人库的路径。
        # 编码显式写 UTF8 + TEMPLATE template0：本机集群若不是 UTF8，从 template1 继承过来的库
        # 会把中文注释写成乱码，而 024 才在真跑时发现——那已经不是夹具能信任的了。
        $create = Invoke-Psql -Database $MaintDb `
            -Sql "CREATE DATABASE $DemoDb WITH ENCODING 'UTF8' TEMPLATE template0" -Secrets $secrets -AllowFail
        if ($create.Code -ne 0) {
            throw "建库失败：$(Write-Scrubs -Lines $create.Out -Secrets $secrets)`n   若报 encoding 与 locale 不匹配，说明本机集群的 locale 撑不起 UTF8 库。请人工建库（CREATE DATABASE $DemoDb WITH ENCODING 'UTF8' TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'，或换成你集群可用的 locale）后重跑本脚本——本脚本不猜 locale，也不替你建库。"
        }

        $text = Get-Content -Path $SqlPath -Raw -Encoding UTF8
        if ($text -notmatch [regex]::Escape($Placeholder)) {
            throw "$SqlPath 里没有占位符 $Placeholder，脚本与外层已不同步"
        }
        $text = $text.Replace($Placeholder, $roPassword)
        # UTF-8 无 BOM：BOM 会跑到第一条语句前面，psql 当场语法错
        [System.IO.File]::WriteAllText($Rendered, $text, (New-Object System.Text.UTF8Encoding($false)))

        # -f 而不是管道喂 stdin：psql 对文件能报出"第几行"，出错时日志可读性高得多
        $apply = Invoke-Psql -Database $DemoDb -File $Rendered -Secrets $secrets -AllowFail -Table
        [System.IO.File]::WriteAllText($LogPath, (Write-Scrubs -Lines $apply.Out -Secrets $secrets),
                                       (New-Object System.Text.UTF8Encoding($false)))
        if ($apply.Code -ne 0) {
            # 回显走 Write-Scrubs 而不是手工 Replace：psql 的 CONTEXT 行会把 SQL 原文折行抄出来，
            # 整串 Replace 对"口令被换行切开"的形态无效。日志已经是脱敏过的，这里只是把尾巴搬到终端。
            (Write-Scrubs -Lines ($apply.Out | Select-Object -Last 20) -Secrets $secrets).Split("`r`n") |
                ForEach-Object { Write-Host "   ! $_" }
            throw "psql 退出码 $($apply.Code)，见 $LogPath"
        }

        Write-Host "== 3/4 自检裁决 =="
        $fails = @(Select-String -Path $LogPath -Pattern '^\s*\|?\s*FAIL\b' | ForEach-Object { $_.Line.Trim() })
        $passes = @(Select-String -Path $LogPath -Pattern '^\s*\|?\s*PASS\b').Count
        Write-Host "   自检通过项：$passes，失败项：$($fails.Count)"
        $fails | ForEach-Object { Write-Host "   FAIL> $_" }
        if ($fails.Count -gt 0) { throw "SQL 自检有 FAIL，见 $LogPath" }
        # PASS 条数下限，逐条数过（与 verification §1.5.4 同一份清单）：
        # 对象计数 4 + 注释覆盖 2 + 类型与列属性 9 + serial 1 + 索引 2 + 外键 1 = 19；
        # 行数断言 10（9 张表精确行数 + 视图非空）+ 枚举值覆盖 5 = 15。合计 34。
        # 少于这个数说明某段压根没跑，"没有 FAIL" 就变成了假绿。
        if ($passes -lt 34) { throw "自检裁决只有 $passes 行 PASS（应为 34 行），日志被截断或有段落没执行，见 $LogPath" }

        # 口令写盘放在脚本成功之后：失败了不该留下一份没人认领的凭证
        if (-not $roExisting) {
            $roText = "# demo_pg_ro 只读账号：登记 PG 数据源时用这份口令，勿提交`r`nPGHOST=${Host_}`r`nPGPORT=${Port}`r`nPGDATABASE=${DemoDb}`r`nPGUSER=demo_pg_ro`r`nPGPASSWORD=${roPassword}`r`n"
            [System.IO.File]::WriteAllText($RoEnv, $roText, (New-Object System.Text.UTF8Encoding($false)))
            Confirm-PgPass -Path $RoEnv
        }

        Write-Host "== 4/4 只读账号复验 =="
        Invoke-RoCheck -PassFile $PassFile -RoPassword $roPassword

        Write-Host "完成。demo_pg_ro 口令在 $RoEnv，登记数据源时从这里抄。"
    }
} finally {
    Remove-Item $Rendered -ErrorAction SilentlyContinue
    # passfile 含真实口令，比 rendered 更该即时删除
    Remove-PgPass -Path $PassFile
    Remove-Item Env:PGPASSFILE -ErrorAction SilentlyContinue
    Remove-Item Env:PGUSER -ErrorAction SilentlyContinue
}
