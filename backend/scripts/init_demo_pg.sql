-- =====================================================================
-- Ai_Web 本机演示源库 ai_web_demo_pg（PostgreSQL 18，schema = demo）
--
-- 用途：P3 验收 6「PG 数据源跑通同步」的原料。它是**被抽取的源库**，
--   不是元数据库——那两个元数据库的名字在本文件里不以独立词出现（含注释），
--   只有本夹具自己的标记表名 `_aiweb_demo_marker` 里含着那一段。
--   这条写成"能 grep 证伪"的形式，是为了让"它会不会动元数据库"不用读完整份文件就能排除。
-- 口径来源：docs/verification.md §1.5（对象枚举、每表考点、自检清单）。
-- 对象构成：9 张业务表 + 视图 v_daily_sales；另有脚本自己的标记表
--   _aiweb_demo_marker（下划线前缀，同步时按数据源的排除规则过滤掉）。
--   → 与 MySQL 演示库同构的四个数：业务对象 10 / 其中 BASE TABLE 9 / VIEW 1 / 枚举全量 11。
--
-- 三条安全约束，改这个文件前先读：
--   1. 只建不删：不含任何删库/删 schema 的语句，那些关键字连注释里都不写（验收口径是
--      "grep 不到"，写在说明里一样算命中）；要重建由人手工做。
--   2. 可重复执行：CREATE ... IF NOT EXISTS + 带主键的 INSERT ... ON CONFLICT DO NOTHING；
--      第二遍跑完流程但一行新数据都不产生。库里已有 demo schema 却没有标记表时**中止**
--      （不是我们建的，拒绝接管别人的 schema）。
--   3. 零密钥：本文件进 git，只含占位符 __AIWEB_PG_RO_PASSWORD__，真实口令由
--      外层 scripts/demo_pg.ps1 替换后写进 gitignored 的临时文件，用完即删。
--
-- 数据可复现：不调随机函数。所有派生值都由 generate_series 的下标做同余，
--   两次执行的行数/金额/枚举分布完全一致，手测才核得掉数字。
--   唯一例外是时间轴：created_at 的上界锚在"今天"，让"近30天…"类问数永远有数据。
--
-- 执行方式：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo_pg.ps1
--   （由外层探测库是否存在、生成口令、跑自检、复验只读；不要手工 psql -f 除非你在超管会话里）
-- =====================================================================

-- 连错库当场中止：本脚本只能在 ai_web_demo_pg 里跑
DO $blk$
BEGIN
    IF current_database() <> 'ai_web_demo_pg' THEN
        RAISE EXCEPTION '本脚本只能在数据库 ai_web_demo_pg 内执行，当前是 "%"。外层应当先建库再连进来。',
            current_database();
    END IF;
END
$blk$;

-- 中文注释与中文数据要求库编码是 UTF8。外层建库时已经显式写了 ENCODING 'UTF8'，
-- 这一道是给"手工 psql -f"留的：编码不对就中止，而不是把乱码写进夹具、等到 024 才发现。
DO $blk$
DECLARE
    enc text;
BEGIN
    SELECT pg_encoding_to_char(encoding) INTO enc
      FROM pg_database WHERE datname = current_database();
    IF enc <> 'UTF8' THEN
        RAISE EXCEPTION '库 "%" 的编码是 "%"，不是 UTF8：中文注释会写成乱码，夹具不可信。'
                        '请人工删掉这个库，再由外层脚本以 ENCODING ''UTF8'' TEMPLATE template0 重建。',
            current_database(), enc;
    END IF;
END
$blk$;

SET client_encoding = 'UTF8';

-- ---------------------------------------------------------------- 闸：接管判断
-- schema 已存在但没有标记表 → 不是本脚本建的，中止（与 MySQL 版的 marker 同一手）
DO $blk$
DECLARE
    has_schema  boolean;
    has_marker  boolean;
BEGIN
    SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'demo') INTO has_schema;
    SELECT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'demo' AND c.relname = '_aiweb_demo_marker'
    ) INTO has_marker;
    IF has_schema AND NOT has_marker THEN
        RAISE EXCEPTION 'schema demo 已存在但没有 _aiweb_demo_marker：不是本脚本建的，拒绝接管。请改用别的 schema 或人工确认后再动。';
    END IF;
END
$blk$;

CREATE SCHEMA IF NOT EXISTS demo;

-- 标记表：证明"这个 schema 是本脚本建的"。外层据此决定跳过还是中止。
-- 带下划线前缀 —— 它同时是"排除规则在 PG 侧也生效"的活体考点。
CREATE TABLE IF NOT EXISTS demo._aiweb_demo_marker (
    version    text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE demo._aiweb_demo_marker IS 'Ai_Web 演示源库标记表：仅供建库脚本判断"是否本脚本所建"，不是业务数据';

INSERT INTO demo._aiweb_demo_marker (version, created_at)
VALUES ('1.0.0', now())
ON CONFLICT (version) DO NOTHING;

-- 权限用例用的"隔壁 schema"：本文件对它**不授予任何权限**，
-- 于是 demo_pg_ro 读它必然 permission denied（42501）—— 对应 MySQL 侧"跨库读 mysql.user 被拒"那一格。
-- 它**不在 demo 里**，所以不影响业务对象计数。
CREATE SCHEMA IF NOT EXISTS other_app;
CREATE TABLE IF NOT EXISTS other_app.secret_table (
    id      int PRIMARY KEY,
    account text,
    token   text
);
INSERT INTO other_app.secret_table (id, account, token)
VALUES (1, 'someone-else', 'not-for-demo-pg-ro')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------- 1. customer
-- serial：§9 映射清单点名的两样之一（另一样是 generated always）
CREATE TABLE IF NOT EXISTS demo.customer (
    id          serial PRIMARY KEY,
    name        varchar(64)  NOT NULL,
    phone       varchar(20),
    gender      text         NOT NULL DEFAULT 'U',
    level       text         NOT NULL DEFAULT 'normal',
    remark      text,
    register_at timestamptz  NOT NULL
);
COMMENT ON TABLE  demo.customer             IS '客户档案表';
COMMENT ON COLUMN demo.customer.name        IS '客户姓名';
COMMENT ON COLUMN demo.customer.phone       IS '手机号（唯一）';
COMMENT ON COLUMN demo.customer.gender      IS '性别：M/F/U';
COMMENT ON COLUMN demo.customer.level       IS '会员等级：normal/silver/gold/platinum';
COMMENT ON COLUMN demo.customer.remark      IS '人工备注，可为空';
COMMENT ON COLUMN demo.customer.register_at IS '注册时间（带时区）';
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_phone ON demo.customer (phone);

INSERT INTO demo.customer (id, name, phone, gender, level, remark, register_at)
SELECT n,
       '客户' || lpad(n::text, 4, '0'),
       -- 括号是必需的：`::` 的优先级高于 `%`，少了括号就变成「整数 % 文本」，直接报运算符不存在
       '13' || lpad((((100000000 + n * 7) % 1000000000))::text, 9, '0'),
       (ARRAY['M', 'F', 'U'])[(n % 3) + 1],
       (ARRAY['normal', 'silver', 'gold', 'platinum'])[(n % 4) + 1],
       CASE WHEN n % 5 = 0 THEN '重点客户，第 ' || n || ' 号建档' ELSE NULL END,
       timestamptz '2023-01-01 00:00:00+08'
         + make_interval(days => (n * 3) % 1200, mins => n % 1440)
FROM generate_series(1, 200) AS n
ON CONFLICT (id) DO NOTHING;
-- serial 的序列要推到当前 max：否则下次人工插行会撞主键。
-- 走 DO/PERFORM 而不是裸 SELECT —— 后者会往 stdout 吐一行数字，混进外层的 PASS/FAIL 裁决计数里。
DO $blk$
BEGIN
    PERFORM setval(pg_get_serial_sequence('demo.customer', 'id'),
                   (SELECT max(id) FROM demo.customer));
END
$blk$;

-- ---------------------------------------------------------------- 2. category（自环）
CREATE TABLE IF NOT EXISTS demo.category (
    id        int PRIMARY KEY,
    name      varchar(64) NOT NULL,
    parent_id int
);
COMMENT ON TABLE  demo.category           IS '商品分类表，两级树结构';
COMMENT ON COLUMN demo.category.name      IS '分类名称';
COMMENT ON COLUMN demo.category.parent_id IS '父分类 id，顶层为空（自引用，测 JOIN 图自环）';

INSERT INTO demo.category (id, name, parent_id)
SELECT n,
       CASE WHEN n <= 8 THEN '一级分类-' || n ELSE '二级分类-' || n END,
       CASE WHEN n <= 8 THEN NULL ELSE ((n - 9) % 8) + 1 END
FROM generate_series(1, 40) AS n
ON CONFLICT (id) DO NOTHING;
-- 自引用外键：MySQL 版只有列名关系，PG 侧真建一条，让 extracted 边在方言之间可比
DO $blk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_category_parent' AND connamespace = 'demo'::regnamespace
    ) THEN
        ALTER TABLE demo.category
            ADD CONSTRAINT fk_category_parent FOREIGN KEY (parent_id) REFERENCES demo.category (id);
    END IF;
END
$blk$;

-- ---------------------------------------------------------------- 3. product
-- 数组 / jsonb / 表达式索引 / 部分索引 四样都压在这张表上
CREATE TABLE IF NOT EXISTS demo.product (
    id         int PRIMARY KEY,
    category_id int NOT NULL,
    name       varchar(120) NOT NULL,
    status     text        NOT NULL DEFAULT 'on_sale',
    price      numeric(10, 2) NOT NULL,
    tags       text[],
    attrs      jsonb,
    created_at timestamptz NOT NULL
);
COMMENT ON TABLE  demo.product            IS '商品主表（SKU 粒度）';
COMMENT ON COLUMN demo.product.name       IS '商品名称（检索主字段）';
COMMENT ON COLUMN demo.product.status     IS '上下架状态：on_sale/off_sale/draft';
COMMENT ON COLUMN demo.product.price      IS '售价，元（两位小数）';
COMMENT ON COLUMN demo.product.tags       IS '标签数组（text[]，归一化考点）';
COMMENT ON COLUMN demo.product.attrs      IS '扩展属性（jsonb，归一化考点）';
COMMENT ON COLUMN demo.product.created_at IS '上架时间（带时区）';

INSERT INTO demo.product (id, category_id, name, status, price, tags, attrs, created_at)
SELECT n,
       ((n - 1) % 32) + 9,
       '商品-' || lpad(n::text, 4, '0') || CASE WHEN n % 7 = 0 THEN '（特惠）' ELSE '' END,
       (ARRAY['on_sale', 'off_sale', 'draft'])[(n % 3) + 1],
       (((n * 137) % 89000 + 100)::numeric / 100)::numeric(10, 2),
       ARRAY['tag' || (n % 9), '系列' || (n % 4)],
       jsonb_build_object('weight', n % 5000, 'origin', (ARRAY['华东', '华南', '华北', '西南'])[(n % 4) + 1]),
       timestamptz '2023-01-01 00:00:00+08' + make_interval(days => (n * 2) % 900)
FROM generate_series(1, 500) AS n
ON CONFLICT (id) DO NOTHING;

-- 表达式索引：§8.2 D 那句"attnum=0 → column_name 为 NULL，从 pg_get_indexdef 回捞表达式"的原料
CREATE INDEX IF NOT EXISTS ix_product_name_lower ON demo.product ((lower(name)));
-- 部分索引：pg_index.indpred 非空，抽取器要能读出来而不炸
CREATE INDEX IF NOT EXISTS ix_product_on_sale ON demo.product (price) WHERE status = 'on_sale';

DO $blk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_product_category' AND connamespace = 'demo'::regnamespace
    ) THEN
        ALTER TABLE demo.product
            ADD CONSTRAINT fk_product_category FOREIGN KEY (category_id) REFERENCES demo.category (id);
    END IF;
END
$blk$;

-- ---------------------------------------------------------------- 4. order_main
-- generated always as identity：§9 映射清单点名的另一样
CREATE TABLE IF NOT EXISTS demo.order_main (
    id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_no    varchar(64) NOT NULL UNIQUE,
    customer_id int         NOT NULL,
    status      text        NOT NULL,
    amount      numeric(10, 2) NOT NULL,
    discount    numeric(10, 2) NOT NULL DEFAULT 0,
    pay_type    text        NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz
);
COMMENT ON TABLE  demo.order_main            IS '订单主表（一笔订单一行）';
COMMENT ON COLUMN demo.order_main.order_no   IS '订单号（唯一）';
COMMENT ON COLUMN demo.order_main.customer_id IS '下单客户 id';
COMMENT ON COLUMN demo.order_main.status     IS '订单状态：pending/paid/shipped/completed/cancelled/refunding';
COMMENT ON COLUMN demo.order_main.amount     IS '订单金额，元';
COMMENT ON COLUMN demo.order_main.discount   IS '优惠金额，元';
COMMENT ON COLUMN demo.order_main.pay_type   IS '支付方式：alipay/wechat/card/offline';
COMMENT ON COLUMN demo.order_main.created_at IS '下单时间（带时区）';
COMMENT ON COLUMN demo.order_main.updated_at IS '最后变更时间（带时区，可空）';

INSERT INTO demo.order_main (order_no, customer_id, status, amount, discount, pay_type, created_at, updated_at)
SELECT 'SO' || to_char(n, 'FM00000000'),
       (n % 200) + 1,
       (ARRAY['pending', 'paid', 'shipped', 'completed', 'cancelled', 'refunding'])[(n % 6) + 1],
       (((n * 211) % 198000 + 200)::numeric / 100)::numeric(10, 2),
       (((n * 31) % 5000)::numeric / 100)::numeric(10, 2),
       (ARRAY['alipay', 'wechat', 'card', 'offline'])[(n % 4) + 1],
       -- 时间轴上界锚在"今天中午"，与 MySQL 版同一口径：近 30 天必有单
       now() - make_interval(days => (n * 7) % 1000, hours => n % 12),
       CASE WHEN n % 4 = 0 THEN now() - make_interval(days => (n * 7) % 1000, hours => n % 12) + interval '2 hours'
            ELSE NULL END
FROM generate_series(1, 2000) AS n
ON CONFLICT (order_no) DO NOTHING;

CREATE INDEX IF NOT EXISTS ix_order_main_customer_created
    ON demo.order_main (customer_id, created_at);

DO $blk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_order_main_customer' AND connamespace = 'demo'::regnamespace
    ) THEN
        ALTER TABLE demo.order_main
            ADD CONSTRAINT fk_order_main_customer FOREIGN KEY (customer_id) REFERENCES demo.customer (id);
    END IF;
END
$blk$;

-- ---------------------------------------------------------------- 5. order_item
CREATE TABLE IF NOT EXISTS demo.order_item (
    id         bigint PRIMARY KEY,
    order_id   int   NOT NULL,
    product_id int   NOT NULL,
    qty        int   NOT NULL,
    unit_price numeric(10, 2) NOT NULL,
    is_gift    boolean NOT NULL DEFAULT false
);
COMMENT ON TABLE  demo.order_item            IS '订单明细行表';
COMMENT ON COLUMN demo.order_item.order_id   IS '所属订单 id';
COMMENT ON COLUMN demo.order_item.product_id IS '商品 id';
COMMENT ON COLUMN demo.order_item.qty        IS '件数';
COMMENT ON COLUMN demo.order_item.unit_price IS '成交单价，元';
COMMENT ON COLUMN demo.order_item.is_gift    IS '是否赠品';

INSERT INTO demo.order_item (id, order_id, product_id, qty, unit_price, is_gift)
SELECT n,
       ((n - 1) % 2000) + 1,
       ((n * 3) % 500) + 1,
       (n % 5) + 1,
       (((n * 97) % 45000 + 50)::numeric / 100)::numeric(10, 2),
       (n % 17 = 0)
FROM generate_series(1, 5000) AS n
ON CONFLICT (id) DO NOTHING;

DO $blk$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_order_item_order'
                   AND connamespace = 'demo'::regnamespace) THEN
        ALTER TABLE demo.order_item
            ADD CONSTRAINT fk_order_item_order FOREIGN KEY (order_id) REFERENCES demo.order_main (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_order_item_product'
                   AND connamespace = 'demo'::regnamespace) THEN
        ALTER TABLE demo.order_item
            ADD CONSTRAINT fk_order_item_product FOREIGN KEY (product_id) REFERENCES demo.product (id);
    END IF;
END
$blk$;

-- ---------------------------------------------------------------- 6. payment_record
CREATE TABLE IF NOT EXISTS demo.payment_record (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_no varchar(64) NOT NULL UNIQUE,
    order_id int         NOT NULL,
    channel  text        NOT NULL,
    amount   numeric(10, 2) NOT NULL,
    paid_at  timestamptz NOT NULL
);
COMMENT ON TABLE  demo.payment_record          IS '支付流水表（一单可多次）';
COMMENT ON COLUMN demo.payment_record.trade_no IS '渠道流水号（唯一）';
COMMENT ON COLUMN demo.payment_record.order_id IS '对应订单 id';
COMMENT ON COLUMN demo.payment_record.channel  IS '渠道：alipay/wechat/card/offline';
COMMENT ON COLUMN demo.payment_record.amount   IS '本次回款金额，元';
COMMENT ON COLUMN demo.payment_record.paid_at  IS '到账时间（带时区，跨表问数主角）';

INSERT INTO demo.payment_record (trade_no, order_id, channel, amount, paid_at)
SELECT 'PM' || to_char(n, 'FM00000000'),
       ((n - 1) % 1500) + 1,
       (ARRAY['alipay', 'wechat', 'card', 'offline'])[(n % 4) + 1],
       (((n * 173) % 158000 + 300)::numeric / 100)::numeric(10, 2),
       now() - make_interval(days => (n * 5) % 900)
FROM generate_series(1, 1500) AS n
ON CONFLICT (trade_no) DO NOTHING;

DO $blk$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_payment_order'
                   AND connamespace = 'demo'::regnamespace) THEN
        ALTER TABLE demo.payment_record
            ADD CONSTRAINT fk_payment_order FOREIGN KEY (order_id) REFERENCES demo.order_main (id);
    END IF;
END
$blk$;

-- ---------------------------------------------------------------- 7. user_activity_log（不建外键）
-- 三种数组类型都挂在这张表上：int4[] / numeric[] / timestamptz[]
CREATE TABLE IF NOT EXISTS demo.user_activity_log (
    id             bigint PRIMARY KEY,
    user_id        int  NOT NULL,
    product_id     int  NOT NULL,
    session_id     varchar(32),
    event_type     varchar(32) NOT NULL,
    extra          jsonb,
    hit_ids        int4[],
    scores         numeric(10, 2)[],
    occurred_at    timestamptz[],
    created_at     timestamptz NOT NULL
);
COMMENT ON TABLE  demo.user_activity_log              IS '用户行为埋点日志（无外键约束）';
COMMENT ON COLUMN demo.user_activity_log.user_id     IS '用户 id（只有列名，不建 FK，靠命名推断 JOIN）';
COMMENT ON COLUMN demo.user_activity_log.product_id  IS '商品 id（同上，不建 FK）';
COMMENT ON COLUMN demo.user_activity_log.session_id  IS '会话 id';
COMMENT ON COLUMN demo.user_activity_log.event_type  IS '事件类型：view/cart/pay/share';
COMMENT ON COLUMN demo.user_activity_log.extra       IS '埋点扩展（jsonb）';
COMMENT ON COLUMN demo.user_activity_log.hit_ids     IS '本次会话命中的商品 id 数组（int4[]）';
COMMENT ON COLUMN demo.user_activity_log.scores      IS '行为分值数组（numeric[]）';
COMMENT ON COLUMN demo.user_activity_log.occurred_at IS '事件时刻数组（timestamptz[]）';

INSERT INTO demo.user_activity_log
    (id, user_id, product_id, session_id, event_type, extra, hit_ids, scores, occurred_at, created_at)
SELECT n,
       (n % 200) + 1,
       (n % 500) + 1,
       'sess-' || to_char(n % 900, 'FM0000'),
       (ARRAY['view', 'cart', 'pay', 'share'])[(n % 4) + 1],
       jsonb_build_object('device', (ARRAY['ios', 'android', 'web'])[(n % 3) + 1], 'n', n),
       ARRAY[n % 500 + 1, n % 37 + 1, n % 11 + 1],
       ARRAY[((n % 100)::numeric / 10)::numeric(10, 2), ((n % 7)::numeric)::numeric(10, 2)],
       ARRAY[now() - make_interval(days => n % 30, hours => n % 24),
                  now() - make_interval(days => n % 30 + 1, hours => n % 24)],
       now() - make_interval(days => n % 60)
FROM generate_series(1, 3000) AS n
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------- 8. product_stats_wide（宽表 + varchar[]）
CREATE TABLE IF NOT EXISTS demo.product_stats_wide (
    product_id        int PRIMARY KEY,
    stat_date         date NOT NULL,
    gmv_7d            numeric(10, 2),
    gmv_30d           numeric(10, 2),
    uv_7d             int,
    uv_30d            int,
    pv_7d             int,
    pv_30d            int,
    cvr_7d            numeric(10, 4),
    cvr_30d           numeric(10, 4),
    return_rate       numeric(10, 4),
    repurchase_cnt    int,
    cart_cnt          int,
    fav_cnt           int,
    refund_amt_7d     numeric(10, 2),
    refund_amt_30d    numeric(10, 2),
    avg_price_7d      numeric(10, 2),
    exposure_cnt      int,
    click_cnt         int,
    add_cart_rate     numeric(10, 4),
    top_keywords      varchar(64)[],
    calc_window_hours int,
    note_long_text    text,
    updated_flag      text,
    channel_split     jsonb
);
COMMENT ON TABLE  demo.product_stats_wide                IS '商品运营统计宽表（按天累计口径）';
COMMENT ON COLUMN demo.product_stats_wide.gmv_7d         IS '近 7 日成交额，元';
COMMENT ON COLUMN demo.product_stats_wide.gmv_30d        IS '近 30 日成交额，元';
COMMENT ON COLUMN demo.product_stats_wide.uv_7d          IS '近 7 日独立访客数';
COMMENT ON COLUMN demo.product_stats_wide.uv_30d         IS '近 30 日独立访客数';
COMMENT ON COLUMN demo.product_stats_wide.pv_7d          IS '近 7 日浏览量';
COMMENT ON COLUMN demo.product_stats_wide.pv_30d         IS '近 30 日浏览量';
COMMENT ON COLUMN demo.product_stats_wide.cvr_7d         IS '近 7 日转化率';
COMMENT ON COLUMN demo.product_stats_wide.cvr_30d        IS '近 30 日转化率';
COMMENT ON COLUMN demo.product_stats_wide.return_rate    IS '退货率';
COMMENT ON COLUMN demo.product_stats_wide.repurchase_cnt IS '复购人次';
COMMENT ON COLUMN demo.product_stats_wide.cart_cnt       IS '加购次数';
COMMENT ON COLUMN demo.product_stats_wide.fav_cnt        IS '收藏次数';
COMMENT ON COLUMN demo.product_stats_wide.refund_amt_7d  IS '近 7 日退款额，元';
COMMENT ON COLUMN demo.product_stats_wide.refund_amt_30d IS '近 30 日退款额，元';
COMMENT ON COLUMN demo.product_stats_wide.avg_price_7d   IS '近 7 日成交均价，元';
COMMENT ON COLUMN demo.product_stats_wide.exposure_cnt   IS '曝光次数';
COMMENT ON COLUMN demo.product_stats_wide.click_cnt      IS '点击次数';
COMMENT ON COLUMN demo.product_stats_wide.add_cart_rate  IS '点击加购率';
COMMENT ON COLUMN demo.product_stats_wide.top_keywords   IS '高频搜索词数组（varchar(64)[]，归一化考点）';
COMMENT ON COLUMN demo.product_stats_wide.calc_window_hours IS '统计窗口小时数';
COMMENT ON COLUMN demo.product_stats_wide.note_long_text IS '长文本备注（测单元格截断）';
COMMENT ON COLUMN demo.product_stats_wide.updated_flag   IS '更新时间戳的文本形态';
COMMENT ON COLUMN demo.product_stats_wide.channel_split  IS '渠道拆分（jsonb）';

INSERT INTO demo.product_stats_wide
    (product_id, stat_date, gmv_7d, gmv_30d, uv_7d, uv_30d, pv_7d, pv_30d, cvr_7d, cvr_30d,
     return_rate, repurchase_cnt, cart_cnt, fav_cnt, refund_amt_7d, refund_amt_30d, avg_price_7d,
     exposure_cnt, click_cnt, add_cart_rate, top_keywords, calc_window_hours, note_long_text,
     updated_flag, channel_split)
SELECT n,
       -- 用整数加天数而不是 make_interval：`date + interval` 的结果是 timestamp，
       -- 往 date 列上写还多一道转换，白担一份风险；`date + integer` 的结果确定是 date。
       (date '2026-01-01' + (n % 240)),
       (((n * 311) % 880000)::numeric / 100)::numeric(10, 2),
       (((n * 977) % 3800000)::numeric / 100)::numeric(10, 2),
       (n * 13) % 9000, (n * 17) % 30000, (n * 19) % 40000, (n * 23) % 120000,
       ((n % 900)::numeric / 10000)::numeric(10, 4), ((n % 1200)::numeric / 10000)::numeric(10, 4),
       ((n % 300)::numeric / 10000)::numeric(10, 4),
       (n % 400), (n % 900), (n % 700),
       (((n * 71) % 60000)::numeric / 100)::numeric(10, 2),
       (((n * 149) % 240000)::numeric / 100)::numeric(10, 2),
       (((n * 97) % 45000 + 50)::numeric / 100)::numeric(10, 2),
       (n * 29) % 200000, (n * 37) % 60000, ((n % 700)::numeric / 10000)::numeric(10, 4),
       ARRAY['词' || (n % 20), '词' || (n % 7)],
       24 * ((n % 3) + 1),
       '第 ' || n || ' 号商品的累计口径说明：本列为长文本，用来测单元格截断与卡片换行行为。',
       to_char(now() - make_interval(days => n % 30), 'YYYY-MM-DD'),
       jsonb_build_object('alipay', n % 500, 'wechat', n % 300)
FROM generate_series(1, 500) AS n
ON CONFLICT (product_id) DO NOTHING;

-- ---------------------------------------------------------------- 9. t_no_comment（两个降级分支合一）
-- 无表注释、无列注释、且**无主键**：卡片的"注释缺失降级"与"无 PK"两条分支在 MySQL 侧各钉过一次，
-- PG 侧的考点是类型与注释抽取，这里合成一个对象，避免演示库对象数漂到 11 打乱 total 口径。
CREATE TABLE IF NOT EXISTS demo.t_no_comment (
    id   int,
    kind text,
    note text,
    meta jsonb
);

INSERT INTO demo.t_no_comment (id, kind, note, meta)
SELECT n, 'k' || (n % 5), '无注释表第 ' || n || ' 行', jsonb_build_object('n', n)
FROM generate_series(1, 50) AS n
WHERE NOT EXISTS (SELECT 1 FROM demo.t_no_comment);

-- ---------------------------------------------------------------- 10. v_daily_sales（视图）
CREATE OR REPLACE VIEW demo.v_daily_sales AS
SELECT date_trunc('day', om.created_at)          AS sale_day,
       count(*)                                  AS order_cnt,
       sum(om.amount)::numeric(12, 2)            AS total_amount,
       avg(om.amount)::numeric(12, 2)            AS avg_amount
FROM demo.order_main om
WHERE om.status IN ('paid', 'shipped', 'completed')
GROUP BY 1;
-- 视图**不写注释**：与 MySQL 版同一考点（视图列注释缺失，卡片走降级模板）

-- ---------------------------------------------------------------- 只读账号
-- 口令占位符由外层替换；角色是集群级对象，已存在就跳过（与 MySQL 的 IF NOT EXISTS 同手）
DO $blk$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo_pg_ro') THEN
        EXECUTE format('CREATE ROLE demo_pg_ro LOGIN PASSWORD %L', '__AIWEB_PG_RO_PASSWORD__');
    ELSE
        RAISE NOTICE '角色 demo_pg_ro 已存在，跳过创建（口令沿用先前那份）';
    END IF;
END
$blk$;

REVOKE ALL ON SCHEMA demo       FROM demo_pg_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA demo FROM demo_pg_ro;

GRANT CONNECT ON DATABASE ai_web_demo_pg TO demo_pg_ro;
GRANT USAGE   ON SCHEMA demo             TO demo_pg_ro;
GRANT SELECT  ON ALL TABLES IN SCHEMA demo TO demo_pg_ro;
-- 序列不授：只读账号不该看见 nextval 权限
ALTER DEFAULT PRIVILEGES IN SCHEMA demo GRANT SELECT ON TABLES TO demo_pg_ro;
-- other_app 一个权限都不给：这是"越权读别人的表必须被拒"那一格用例

-- ---------------------------------------------------------------- 自检裁决
-- 每行输出一条 PASS/FAIL，外层按 FAIL 计数裁决、按 PASS 行数下限防"某段压根没跑"（假绿防线）。
-- 期望值全部手算，来源 docs/verification.md §1.5 的枚举表。
WITH obj AS (
    SELECT count(*) FILTER (WHERE c.relname NOT LIKE '\_%')       AS business_objects,
           count(*) FILTER (WHERE c.relkind IN ('r', 'p', 'm', 'f')
                              AND c.relname NOT LIKE '\_%')       AS base_tables,
           count(*) FILTER (WHERE c.relkind = 'v'
                              AND c.relname NOT LIKE '\_%')       AS views,
           count(*)                                               AS all_objects
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'demo' AND c.relkind IN ('r', 'p', 'm', 'f', 'v')
),
cmt AS (
    SELECT count(*) FILTER (WHERE obj_description(c.oid) IS NOT NULL) AS commented_objects,
           count(*) FILTER (WHERE obj_description(c.oid) IS NULL)      AS uncommented_objects
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'demo' AND c.relkind IN ('r', 'v') AND c.relname NOT LIKE '\_%'
),
typ AS (
    SELECT count(*) FILTER (WHERE a.atttypid = 'text'::regtype)                        AS t_text,
           count(*) FILTER (WHERE a.atttypid = 'jsonb'::regtype)                       AS t_jsonb,
           count(*) FILTER (WHERE a.atttypid = 'text[]'::regtype)                      AS t_text_arr,
           count(*) FILTER (WHERE a.atttypid = 'integer[]'::regtype)                   AS t_int4_arr,
           count(*) FILTER (WHERE a.atttypid = 'numeric[]'::regtype)                   AS t_numeric_arr,
           count(*) FILTER (WHERE a.atttypid = 'timestamp with time zone[]'::regtype)  AS t_ts_arr,
           count(*) FILTER (WHERE a.atttypid = 'character varying[]'::regtype)          AS t_varchar_arr,
           count(*) FILTER (WHERE a.atttypid = 'character varying[]'::regtype
                              AND a.atttypmod <> -1)                                    AS t_varchar_arr_sized,
           count(*) FILTER (WHERE a.attidentity = 'a')                                 AS generated_always
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'demo' AND a.attnum > 0 AND NOT a.attisdropped
),
serial_probe AS (
    -- serial 的真身是「列默认值 = nextval(...)」；identity 列不走这条路，所以两者能分开数
    SELECT count(*) AS serial_cols
    FROM pg_attrdef d
    JOIN pg_class c ON c.oid = d.adrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'demo' AND pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval%'
),
idx AS (
    -- 表达式索引的判据用 indexprs 非空（"if expression-based index, non-null"），
    -- 不用 indkey::int[] —— indkey 是 int2vector，那个显式数组转换我没在 live 上证过，
    -- 拿不准的写法不该进自检。
    SELECT count(*) FILTER (WHERE i.indexprs IS NOT NULL) AS expression_indexes,
           count(*) FILTER (WHERE i.indpred IS NOT NULL)  AS partial_indexes
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'demo'
),
fk AS (
    SELECT count(*) AS fk_constraints
    FROM pg_constraint con
    JOIN pg_namespace n ON n.oid = con.connamespace
    WHERE n.nspname = 'demo' AND con.contype = 'f'
)
SELECT 'PASS  business_objects=' || obj.business_objects FROM obj WHERE obj.business_objects = 10
UNION ALL SELECT 'FAIL  business_objects=' || obj.business_objects || '（应为 10）' FROM obj WHERE obj.business_objects <> 10
UNION ALL SELECT 'PASS  base_tables=' || obj.base_tables FROM obj WHERE obj.base_tables = 9
UNION ALL SELECT 'FAIL  base_tables=' || obj.base_tables || '（应为 9）' FROM obj WHERE obj.base_tables <> 9
UNION ALL SELECT 'PASS  views=' || obj.views FROM obj WHERE obj.views = 1
UNION ALL SELECT 'FAIL  views=' || obj.views || '（应为 1）' FROM obj WHERE obj.views <> 1
UNION ALL SELECT 'PASS  all_objects_with_marker=' || obj.all_objects FROM obj WHERE obj.all_objects = 11
UNION ALL SELECT 'FAIL  all_objects_with_marker=' || obj.all_objects || '（应为 11）' FROM obj WHERE obj.all_objects <> 11
UNION ALL SELECT 'PASS  commented_objects=' || cmt.commented_objects FROM cmt WHERE cmt.commented_objects = 8
UNION ALL SELECT 'FAIL  commented_objects=' || cmt.commented_objects || '（应为 8：除 t_no_comment 与 v_daily_sales 外的 8 张业务表）' FROM cmt WHERE cmt.commented_objects <> 8
UNION ALL SELECT 'PASS  uncommented_objects=' || cmt.uncommented_objects FROM cmt WHERE cmt.uncommented_objects = 2
UNION ALL SELECT 'FAIL  uncommented_objects=' || cmt.uncommented_objects || '（应为 2：t_no_comment 与 v_daily_sales）' FROM cmt WHERE cmt.uncommented_objects <> 2
UNION ALL SELECT 'PASS  type:text=' || typ.t_text FROM typ WHERE typ.t_text > 0
UNION ALL SELECT 'FAIL  type:text=0' FROM typ WHERE typ.t_text = 0
UNION ALL SELECT 'PASS  type:jsonb=' || typ.t_jsonb FROM typ WHERE typ.t_jsonb > 0
UNION ALL SELECT 'FAIL  type:jsonb=0' FROM typ WHERE typ.t_jsonb = 0
UNION ALL SELECT 'PASS  type:text[]=' || typ.t_text_arr FROM typ WHERE typ.t_text_arr = 1
UNION ALL SELECT 'FAIL  type:text[]=' || typ.t_text_arr || '（应为 1：product.tags）' FROM typ WHERE typ.t_text_arr <> 1
UNION ALL SELECT 'PASS  type:int4[]=' || typ.t_int4_arr FROM typ WHERE typ.t_int4_arr = 1
UNION ALL SELECT 'FAIL  type:int4[]=' || typ.t_int4_arr || '（应为 1：hit_ids）' FROM typ WHERE typ.t_int4_arr <> 1
UNION ALL SELECT 'PASS  type:numeric[]=' || typ.t_numeric_arr FROM typ WHERE typ.t_numeric_arr = 1
UNION ALL SELECT 'FAIL  type:numeric[]=' || typ.t_numeric_arr || '（应为 1：scores）' FROM typ WHERE typ.t_numeric_arr <> 1
UNION ALL SELECT 'PASS  type:timestamptz[]=' || typ.t_ts_arr FROM typ WHERE typ.t_ts_arr = 1
UNION ALL SELECT 'FAIL  type:timestamptz[]=' || typ.t_ts_arr || '（应为 1：occurred_at）' FROM typ WHERE typ.t_ts_arr <> 1
UNION ALL SELECT 'PASS  type:varchar64[]=' || typ.t_varchar_arr FROM typ WHERE typ.t_varchar_arr = 1
UNION ALL SELECT 'FAIL  type:varchar64[]=' || typ.t_varchar_arr || '（应为 1：top_keywords）' FROM typ WHERE typ.t_varchar_arr <> 1
UNION ALL SELECT 'PASS  varchar_array_keeps_modifier=' || typ.t_varchar_arr_sized FROM typ WHERE typ.t_varchar_arr_sized = 1
UNION ALL SELECT 'FAIL  varchar_array_keeps_modifier=' || typ.t_varchar_arr_sized || '（应为 1，atttypmod 丢了就没法测归一）' FROM typ WHERE typ.t_varchar_arr_sized <> 1
UNION ALL SELECT 'PASS  generated_always_identity=' || typ.generated_always FROM typ WHERE typ.generated_always = 2
UNION ALL SELECT 'FAIL  generated_always_identity=' || typ.generated_always || '（应为 2：order_main.id、payment_record.id）' FROM typ WHERE typ.generated_always <> 2
UNION ALL SELECT 'PASS  serial_column=' || serial_probe.serial_cols FROM serial_probe WHERE serial_probe.serial_cols = 1
UNION ALL SELECT 'FAIL  serial_column=' || serial_probe.serial_cols || '（应为 1：customer.id）' FROM serial_probe WHERE serial_probe.serial_cols <> 1
UNION ALL SELECT 'PASS  expression_index=' || idx.expression_indexes FROM idx WHERE idx.expression_indexes = 1
UNION ALL SELECT 'FAIL  expression_index=' || idx.expression_indexes || '（应为 1：ix_product_name_lower）' FROM idx WHERE idx.expression_indexes <> 1
UNION ALL SELECT 'PASS  partial_index=' || idx.partial_indexes FROM idx WHERE idx.partial_indexes = 1
UNION ALL SELECT 'FAIL  partial_index=' || idx.partial_indexes || '（应为 1：ix_product_on_sale）' FROM idx WHERE idx.partial_indexes <> 1
UNION ALL SELECT 'PASS  fk_constraints=' || fk.fk_constraints FROM fk WHERE fk.fk_constraints = 6
UNION ALL SELECT 'FAIL  fk_constraints=' || fk.fk_constraints || '（应为 6；user_activity_log 那两个列名不该有 FK）' FROM fk WHERE fk.fk_constraints <> 6
ORDER BY 1;

-- 每张表的列数与注释覆盖（打印，不作断言）：verification.md §1.5 的表与这段输出逐字对齐
SELECT c.relname                                   AS table_name,
       c.relkind                                   AS kind,
       count(a.attnum)                             AS column_count,
       COALESCE(obj_description(c.oid), '（无注释）') AS table_comment
FROM pg_class c
LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = 'demo' AND c.relkind IN ('r', 'p', 'm', 'f', 'v')
GROUP BY c.relname, c.relkind, c.oid
ORDER BY c.relname;

-- 行数断言（期望值手算：见 §1.5 的枚举表）。
-- 这一段必须是 PASS/FAIL 而不是打印：只核对结构不核对数据，"表建齐了但一行没插"
-- 也能凑够前面那些 PASS 判绿，而那正好是 002 在 MySQL 侧踩过的同一类假绿。
WITH rows_(t, got, want) AS (
    VALUES ('customer', (SELECT count(*) FROM demo.customer), 200),
           ('category', (SELECT count(*) FROM demo.category), 40),
           ('product', (SELECT count(*) FROM demo.product), 500),
           ('order_main', (SELECT count(*) FROM demo.order_main), 2000),
           ('order_item', (SELECT count(*) FROM demo.order_item), 5000),
           ('payment_record', (SELECT count(*) FROM demo.payment_record), 1500),
           ('user_activity_log', (SELECT count(*) FROM demo.user_activity_log), 3000),
           ('product_stats_wide', (SELECT count(*) FROM demo.product_stats_wide), 500),
           ('t_no_comment', (SELECT count(*) FROM demo.t_no_comment), 50)
)
SELECT 'PASS  rows:' || t || '=' || got FROM rows_ WHERE got = want
UNION ALL SELECT 'FAIL  rows:' || t || '=' || got || '（应为 ' || want || '）' FROM rows_ WHERE got <> want
UNION ALL SELECT 'PASS  rows:v_daily_sales>0' FROM (SELECT count(*) AS c FROM demo.v_daily_sales) v WHERE v.c > 0
UNION ALL SELECT 'FAIL  rows:v_daily_sales=0（视图按天聚合，行数随时间轴漂移，只断非空）'
  FROM (SELECT count(*) AS c FROM demo.v_daily_sales) v WHERE v.c = 0
ORDER BY 1;

-- 枚举覆盖断言：造数用的是下标同余，`(ARRAY[...])[(n % k) + 1]` 里若把 k 写错，
-- 数据看起来仍然"很满"但少一个值——卡片与 SAMPLE_DISTINCT 的考点就静默消失。
-- 与 MySQL 版 625-642 那段同一考点（那边曾因 draft 恒 0 而漏过一个真 bug）。
WITH e AS (
    SELECT 'order_main.status' AS what, count(DISTINCT status) AS got, 6 AS want FROM demo.order_main
    UNION ALL SELECT 'product.status', count(DISTINCT status), 3 FROM demo.product
    UNION ALL SELECT 'payment_record.channel', count(DISTINCT channel), 4 FROM demo.payment_record
    UNION ALL SELECT 'customer.gender', count(DISTINCT gender), 3 FROM demo.customer
    UNION ALL SELECT 'customer.level', count(DISTINCT level), 4 FROM demo.customer
)
SELECT 'PASS  enum:' || e.what || '=' || e.got FROM e WHERE e.got = e.want
UNION ALL SELECT 'FAIL  enum:' || e.what || '=' || e.got || '（应为 ' || e.want || ' 个值都有行）' FROM e WHERE e.got <> e.want
ORDER BY 1;

\echo 'init_demo_pg.sql 执行完毕'
