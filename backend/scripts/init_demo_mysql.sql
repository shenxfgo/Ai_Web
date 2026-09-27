-- =====================================================================
-- Ai_Web 本机演示库 ai_web_demo（MySQL 5.7）
--
-- 口径来源：docs/verification.md §1 的对象枚举与每表考点。
-- 对象构成：9 张业务表 + 视图 v_daily_sales；另有脚本内部对象
--   _seq（数字表，造完数据即回收）和 _aiweb_demo_marker（"本脚本建的"凭证）。
--
-- 三条安全约束，改这个文件前先读：
--   1. 只建不删：不删库、也不删任何业务表；要重建由人手工做（见 verification.md §1.2 第 3 步）。
--   2. 可重复执行：CREATE ... IF NOT EXISTS + INSERT IGNORE，且 id 全部显式给出，
--      第二遍跑完流程但一行新数据都不产生（这是"重复执行不报已存在"的实现方式）。
--      外层 scripts/demo_db.ps1 还会先探测库是否存在，正常情况下走不到第二遍。
--   3. 零密钥：本文件进 git，只含占位符 __AIWEB_RO_PASSWORD__，真实口令由外层注入。
--
-- 数据可复现：所有"随机"值都来自 @seed 派生的 MD5 → 32bit 整数 → 同余，
--   不用无参随机函数（那样每次跑结果都不同），两次执行的行数/金额/枚举分布完全一致，
--   手测才核得掉数字。
--   唯一例外是时间轴：created_at 的上界锚在"今天"，让"近30天…"类问数永远有数据。
--
-- 执行方式：make demo-db（或 powershell -File scripts\demo_db.ps1 demo-db）
-- =====================================================================

SET NAMES utf8mb4;

SET @seed = 20260922;
-- 时间轴：2023-01-01 起到"今天中午"，覆盖 2023/2024/2025 三个整年且近 30 天必有单
SET @span_start = UNIX_TIMESTAMP('2023-01-01 00:00:00');
SET @span_end = UNIX_TIMESTAMP(DATE_ADD(CURDATE(), INTERVAL 12 HOUR));
SET @span_len = @span_end - @span_start;

CREATE DATABASE IF NOT EXISTS ai_web_demo
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE ai_web_demo;

-- 标记表：证明"这个库是本脚本建的"。外层据此决定是跳过还是中止；
-- 库存在但 marker 不存在 → 不是我们建的，外层拒绝接管。
CREATE TABLE IF NOT EXISTS _aiweb_demo_marker (
  version VARCHAR(16) NOT NULL COMMENT '建库脚本版本号',
  created_at DATETIME NOT NULL COMMENT '首次建库时间',
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='Ai_Web 演示库标记表：仅供建库脚本判断"是否本脚本所建"，不是业务数据';

INSERT IGNORE INTO _aiweb_demo_marker (version, created_at) VALUES ('1.0.0', NOW());

-- 数字表：5.7 没有 CTE/递归，造行只能靠它。0..99999 一次建够，
-- 后面所有表的行数都从 _seq 里按区间取。
CREATE TABLE IF NOT EXISTS _seq (
  n INT NOT NULL COMMENT '连续整数，从 0 开始',
  PRIMARY KEY (n)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='脚本内部数字表，造数用，建库结束时删除';

INSERT IGNORE INTO _seq (n)
SELECT a.n + b.n * 10 + c.n * 100 + d.n * 1000 + e.n * 10000
FROM (SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) a,
     (SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) b,
     (SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) c,
     (SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) d,
     (SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) e;

-- ---------------------------------------------------------------------
-- 1. category：自引用外键，考 JOIN 图的自环处理
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS category (
  id INT NOT NULL COMMENT '分类ID，主键，由建库脚本显式指定',
  name VARCHAR(64) NOT NULL COMMENT '分类名称（中文）',
  level TINYINT NOT NULL COMMENT '层级：1=一级分类，2=二级分类',
  parent_id INT NULL COMMENT '父分类ID，自引用外键；一级分类为 NULL',
  sort_order INT NOT NULL COMMENT '同级展示顺序，越小越靠前',
  PRIMARY KEY (id),
  KEY idx_category_parent (parent_id),
  CONSTRAINT fk_category_parent FOREIGN KEY (parent_id) REFERENCES category (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='商品分类表，两级树结构';

INSERT IGNORE INTO category (id, name, level, parent_id, sort_order)
SELECT s.n,
       ELT(s.n, '家用电器', '手机数码', '服饰鞋包', '美妆个护', '食品生鲜',
            '母婴玩具', '图书文娱', '运动户外', '家居家装', '宠物生活'),
       1, NULL, s.n
FROM _seq s WHERE s.n BETWEEN 1 AND 10;

INSERT IGNORE INTO category (id, name, level, parent_id, sort_order)
SELECT s.n,
       CONCAT(ELT(1 + FLOOR((s.n - 11) / 3), '家用电器', '手机数码', '服饰鞋包', '美妆个护',
                  '食品生鲜', '母婴玩具', '图书文娱', '运动户外', '家居家装', '宠物生活'),
              '-', ELT(1 + ((s.n - 11) % 3), '热销款', '新品首发', '性价比')),
       2, 1 + FLOOR((s.n - 11) / 3), 1 + ((s.n - 11) % 3)
FROM _seq s WHERE s.n BETWEEN 11 AND 40;

-- ---------------------------------------------------------------------
-- 2. product：ENUM + DECIMAL + 逗号分隔 tags
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product (
  id INT NOT NULL COMMENT '商品ID，主键',
  sku VARCHAR(32) NOT NULL COMMENT 'SKU 编码，唯一',
  name VARCHAR(128) NOT NULL COMMENT '商品名称（中文）',
  category_id INT NOT NULL COMMENT '所属分类ID，外键指向 category.id',
  brand VARCHAR(64) NOT NULL COMMENT '品牌名（中文）',
  status ENUM('on_sale','off_sale','draft') NOT NULL COMMENT '上架状态：on_sale=在售，off_sale=下架，draft=草稿',
  price DECIMAL(10,2) NOT NULL COMMENT '售价（元）',
  tags VARCHAR(255) NOT NULL COMMENT '标签，逗号分隔的中文短语（非规范化字段，考抽取与检索）',
  created_at DATETIME NOT NULL COMMENT '商品创建时间',
  updated_at DATETIME NOT NULL COMMENT '最近一次修改时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_product_sku (sku),
  KEY idx_product_category (category_id),
  CONSTRAINT fk_product_category FOREIGN KEY (category_id) REFERENCES category (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='商品主表（SKU 粒度）';

INSERT IGNORE INTO product (id, sku, name, category_id, brand, status, price, tags, created_at, updated_at)
SELECT p.n,
       CONCAT('SKU', LPAD(p.n, 6, '0')),
       CONCAT(ELT(1 + p.x1 % 10, '智能家居', '轻薄笔记本', '无线降噪耳机', '经典休闲鞋', '保湿修护套装',
                  '有机坚果', '益智积木', '越野跑鞋', '北欧落地灯', '冻干猫粮'),
              ' ', ELT(1 + p.x2 % 5, '标准版', '旗舰版', '青春版', 'Pro 版', '家庭套装')),
       1 + p.x3 % 40,
       ELT(1 + p.x2 % 12, '云帆', '木白', '青麦', '拾光', '物研社', '南屿',
           '常喜', '禾风', '初语', '山海', '良物', '潮汐'),
       ELT(1 + p.x4 % 6, 'on_sale', 'on_sale', 'on_sale', 'on_sale', 'off_sale', 'draft'),
       ROUND(19 + (p.x1 % 480000) / 100, 2),
       CONCAT_WS(',', ELT(1 + p.x3 % 5, '热销', '新品', '促销', '自营', '包邮'),
                     ELT(1 + p.x4 % 4, '正品保障', '七天无理由', '破损包退', '会员专享')),
       FROM_UNIXTIME(@span_start + (p.x2 % @span_len)),
       FROM_UNIXTIME(@span_start + (p.x2 % @span_len) + (p.x3 % 86400))
FROM (
  SELECT g.n,
         CAST(CONV(LEFT(g.h, 8), 16, 10) AS UNSIGNED) AS x1,
         CAST(CONV(SUBSTRING(g.h, 9, 8), 16, 10) AS UNSIGNED) AS x2,
         CAST(CONV(SUBSTRING(g.h, 17, 8), 16, 10) AS UNSIGNED) AS x3,
         CAST(CONV(SUBSTRING(g.h, 25, 8), 16, 10) AS UNSIGNED) AS x4
  FROM (SELECT s.n AS n, MD5(CONCAT(@seed, ':product:', s.n)) AS h FROM _seq s WHERE s.n BETWEEN 1 AND 1200) g
) p;

-- ---------------------------------------------------------------------
-- 3. customer：手机号唯一索引 + 两个 ENUM + 城市（区域×月份×渠道问数的区域维度）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer (
  id INT NOT NULL COMMENT '客户ID，主键',
  nickname VARCHAR(64) NOT NULL COMMENT '客户昵称（中文）',
  phone CHAR(11) NOT NULL COMMENT '手机号，唯一索引（抽取时要认出它是敏感列）',
  gender ENUM('M','F','U') NOT NULL COMMENT '性别：M=男，F=女，U=未知',
  level ENUM('normal','silver','gold','platinum') NOT NULL COMMENT '会员等级，四级递进',
  city VARCHAR(64) NOT NULL COMMENT '所在城市（中文），问"区域"时的分组维度',
  register_at DATETIME NOT NULL COMMENT '注册时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_customer_phone (phone),
  KEY idx_customer_city (city)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='客户档案表';

INSERT IGNORE INTO customer (id, nickname, phone, gender, level, city, register_at)
SELECT c.n,
       CONCAT('用户', LPAD(c.n, 4, '0')),
       CONCAT('1', ELT(1 + c.n % 8, '3','5','6','7','8','9','2','1'), LPAD(c.n, 9, '0')),
       ELT(1 + c.x1 % 3, 'M', 'F', 'U'),
       ELT(1 + c.x2 % 4, 'normal', 'silver', 'gold', 'platinum'),
       ELT(1 + c.x3 % 12, '上海', '北京', '广州', '深圳', '杭州', '成都',
                          '武汉', '西安', '南京', '苏州', '长沙', '重庆'),
       FROM_UNIXTIME(@span_start + (c.x4 % @span_len))
FROM (
  SELECT s.n,
         CAST(CONV(LEFT(MD5(CONCAT(@seed, ':customer:', s.n)), 8), 16, 10) AS UNSIGNED) AS x1,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':customer:', s.n)), 9, 8), 16, 10) AS UNSIGNED) AS x2,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':customer:', s.n)), 17, 8), 16, 10) AS UNSIGNED) AS x3,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':customer:', s.n)), 25, 8), 16, 10) AS UNSIGNED) AS x4
  FROM _seq s WHERE s.n BETWEEN 1 AND 3000
) c;

-- ---------------------------------------------------------------------
-- 4. order_main：六个枚举值全覆盖（"已完成订单"→'completed' 的映射考点）
--    + 复合索引 (customer_id, created_at)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_main (
  id INT NOT NULL COMMENT '订单ID，主键',
  order_no VARCHAR(32) NOT NULL COMMENT '订单号，业务唯一编号',
  customer_id INT NOT NULL COMMENT '下单客户ID，外键指向 customer.id',
  status ENUM('pending','paid','shipped','completed','cancelled','refunding') NOT NULL
    COMMENT '订单状态：pending=待付款，paid=已付款，shipped=已发货，completed=已完成，cancelled=已取消，refunding=退款中',
  amount DECIMAL(12,2) NOT NULL COMMENT '订单金额（元，优惠前）',
  discount DECIMAL(10,2) NOT NULL COMMENT '优惠金额（元）',
  pay_type ENUM('alipay','wechat','card','offline') NOT NULL COMMENT '支付方式：alipay=支付宝，wechat=微信，card=银行卡，offline=线下',
  created_at DATETIME NOT NULL COMMENT '下单时间',
  updated_at DATETIME NOT NULL COMMENT '状态最近变更时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_order_no (order_no),
  KEY idx_order_customer_created (customer_id, created_at),
  CONSTRAINT fk_order_customer FOREIGN KEY (customer_id) REFERENCES customer (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='订单主表（一笔订单一行）';

INSERT IGNORE INTO order_main (id, order_no, customer_id, status, amount, discount, pay_type, created_at, updated_at)
SELECT o.n,
       CONCAT('ORD', LPAD(o.n, 10, '0')),
       1 + o.x2 % 3000,
       ELT(1 + o.x1 % 6, 'pending', 'paid', 'shipped', 'completed', 'cancelled', 'refunding'),
       o.amt,
       ROUND(o.amt * (o.x4 % 20) / 100, 2),
       ELT(1 + o.x3 % 4, 'alipay', 'wechat', 'card', 'offline'),
       FROM_UNIXTIME(o.ts),
       FROM_UNIXTIME(o.ts + (o.x2 % 14400))
FROM (
  SELECT g.n, g.x1, g.x2, g.x3, g.x4,
         @span_start + (g.x3 % @span_len) AS ts,
         ROUND(50 + (g.x2 % 200000) / 100, 2) AS amt
  FROM (
    SELECT s.n,
           CAST(CONV(LEFT(MD5(CONCAT(@seed, ':order_main:', s.n)), 8), 16, 10) AS UNSIGNED) AS x1,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':order_main:', s.n)), 9, 8), 16, 10) AS UNSIGNED) AS x2,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':order_main:', s.n)), 17, 8), 16, 10) AS UNSIGNED) AS x3,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':order_main:', s.n)), 25, 8), 16, 10) AS UNSIGNED) AS x4
    FROM _seq s WHERE s.n BETWEEN 1 AND 30000
  ) g
) o;

-- ---------------------------------------------------------------------
-- 5. order_item：8.8 万行，专门越过 5 万阈值，考聚合与 LIMIT 截断
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_item (
  id INT NOT NULL COMMENT '明细行ID，主键',
  order_id INT NOT NULL COMMENT '所属订单ID，外键指向 order_main.id',
  product_id INT NOT NULL COMMENT '商品ID，外键指向 product.id',
  qty INT NOT NULL COMMENT '购买数量',
  unit_price DECIMAL(10,2) NOT NULL COMMENT '成交单价（元）',
  is_gift TINYINT(1) NOT NULL COMMENT '是否赠品：1=赠品（单价记 0），0=正常行',
  created_at DATETIME NOT NULL COMMENT '下单时间（冗余自订单，便于按明细直接按天聚合）',
  PRIMARY KEY (id),
  KEY idx_item_order (order_id),
  KEY idx_item_product (product_id),
  CONSTRAINT fk_item_order FOREIGN KEY (order_id) REFERENCES order_main (id),
  CONSTRAINT fk_item_product FOREIGN KEY (product_id) REFERENCES product (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='订单明细行表';

INSERT IGNORE INTO order_item (id, order_id, product_id, qty, unit_price, is_gift, created_at)
SELECT i.n,
       1 + i.x1 % 30000,
       1 + i.x2 % 1200,
       1 + i.x3 % 5,
       ROUND(9 + (i.x4 % 199000) / 100, 2),
       IF(i.x3 % 50 = 0, 1, 0),
       o.created_at
FROM (
  SELECT s.n,
         CAST(CONV(LEFT(MD5(CONCAT(@seed, ':order_item:', s.n)), 8), 16, 10) AS UNSIGNED) AS x1,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':order_item:', s.n)), 9, 8), 16, 10) AS UNSIGNED) AS x2,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':order_item:', s.n)), 17, 8), 16, 10) AS UNSIGNED) AS x3,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':order_item:', s.n)), 25, 8), 16, 10) AS UNSIGNED) AS x4
  FROM _seq s WHERE s.n BETWEEN 1 AND 88000
) i
-- 明细行的时间跟着订单走：冗余列与主表不一致会让"按天聚合明细"得出和订单表不同的答案
JOIN order_main o ON o.id = 1 + i.x1 % 30000;

-- ---------------------------------------------------------------------
-- 6. payment_record：跨表问数主角（区域 × 月份 × 渠道回款）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_record (
  id INT NOT NULL COMMENT '支付流水ID，主键',
  trade_no VARCHAR(40) NOT NULL COMMENT '第三方支付流水号，唯一',
  order_id INT NOT NULL COMMENT '对应订单ID，外键指向 order_main.id（一单可多条）',
  channel ENUM('alipay','wechat','card','offline') NOT NULL COMMENT '支付渠道：alipay=支付宝，wechat=微信，card=银行卡，offline=线下汇款',
  amount DECIMAL(12,2) NOT NULL COMMENT '本次实付金额（元）',
  status ENUM('success','failed','refunded') NOT NULL COMMENT '流水状态：success=成功，failed=失败，refunded=已退回',
  paid_at DATETIME NOT NULL COMMENT '支付完成时间（晚于下单时间，取订单 created_at + 偏移）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_payment_trade_no (trade_no),
  KEY idx_payment_order (order_id),
  KEY idx_payment_paid_at (paid_at),
  CONSTRAINT fk_payment_order FOREIGN KEY (order_id) REFERENCES order_main (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='支付流水表（一单可多次）';

INSERT IGNORE INTO payment_record (id, trade_no, order_id, channel, amount, status, paid_at)
SELECT y.n,
       CONCAT('PAY', LPAD(y.n, 12, '0')),
       y.order_id,
       ELT(1 + y.x2 % 4, 'alipay', 'wechat', 'card', 'offline'),
       y.amount,
       ELT(1 + y.x3 % 8, 'success', 'success', 'success', 'success', 'success', 'success', 'failed', 'refunded'),
       FROM_UNIXTIME(y.paid_ts)
FROM (
  SELECT g.n, g.x2, g.x3,
         o.amount AS amount,
         -- 取模落在 1..20000：后 1 万笔订单没有支付流水，且 1..20000 里必然有订单出现两条
         1 + (g.n % 20000) AS order_id,
         UNIX_TIMESTAMP(o.created_at) + (g.x1 % 7200) AS paid_ts
  FROM (
    SELECT s.n,
           CAST(CONV(LEFT(MD5(CONCAT(@seed, ':payment_record:', s.n)), 8), 16, 10) AS UNSIGNED) AS x1,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':payment_record:', s.n)), 9, 8), 16, 10) AS UNSIGNED) AS x2,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':payment_record:', s.n)), 17, 8), 16, 10) AS UNSIGNED) AS x3
    FROM _seq s WHERE s.n BETWEEN 1 AND 26000
  ) g
  JOIN order_main o ON o.id = 1 + (g.n % 20000)
) y;

-- ---------------------------------------------------------------------
-- 7. refund_record：一单至多一条，让"未退款订单数"= 30000 - 2400 可手算核对
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refund_record (
  id INT NOT NULL COMMENT '退款单ID，主键',
  refund_no VARCHAR(32) NOT NULL COMMENT '退款单号，唯一',
  order_id INT NOT NULL COMMENT '退款订单ID，唯一约束：一个订单至多一条退款申请',
  amount DECIMAL(12,2) NOT NULL COMMENT '申请退款金额（元）',
  reason VARCHAR(255) NOT NULL COMMENT '退款原因（中文自由文本）',
  status ENUM('pending','approved','rejected','success') NOT NULL COMMENT '处理状态：pending=待审核，approved=已同意，rejected=已驳回，success=退款到账',
  applied_at DATETIME NOT NULL COMMENT '申请时间',
  finished_at DATETIME NULL COMMENT '处理完成时间；未处理为 NULL（不是零值日期）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_refund_no (refund_no),
  UNIQUE KEY uk_refund_order (order_id),
  CONSTRAINT fk_refund_order FOREIGN KEY (order_id) REFERENCES order_main (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='退款申请与处理表';

INSERT IGNORE INTO refund_record (id, refund_no, order_id, amount, reason, status, applied_at, finished_at)
SELECT r.n,
       CONCAT('RFD', LPAD(r.n, 10, '0')),
       r.order_id,
       LEAST(o.amount, ROUND(10 + (r.x2 % 200000) / 100, 2)),
       CONCAT(ELT(1 + r.x3 % 5, '拍错/多拍', '商品与描述不符', '质量问题', '物流太慢', '七天无理由'),
              '（', ELT(1 + r.x4 % 3, '已上传凭证', '客服已电话沟通', '等待买家退货'), '）'),
       ELT(1 + r.x1 % 4, 'pending', 'approved', 'rejected', 'success'),
       FROM_UNIXTIME(UNIX_TIMESTAMP(o.created_at) + 86400 + (r.x1 % 172800)),
       IF(r.x2 % 5 = 0, NULL,
          FROM_UNIXTIME(UNIX_TIMESTAMP(o.created_at) + 86400 * 3 + (r.x3 % 172800)))
FROM (
  SELECT g.n, g.x1, g.x2, g.x3, g.x4,
         -- 均匀撒在 1..29988 且互不相同，配合 uk_refund_order 保证行数正好 2400
         FLOOR((g.n - 1) * 30000 / 2400) + 1 AS order_id
  FROM (
    SELECT s.n,
           CAST(CONV(LEFT(MD5(CONCAT(@seed, ':refund_record:', s.n)), 8), 16, 10) AS UNSIGNED) AS x1,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':refund_record:', s.n)), 9, 8), 16, 10) AS UNSIGNED) AS x2,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':refund_record:', s.n)), 17, 8), 16, 10) AS UNSIGNED) AS x3,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':refund_record:', s.n)), 25, 8), 16, 10) AS UNSIGNED) AS x4
    FROM _seq s WHERE s.n BETWEEN 1 AND 2400
  ) g
) r
JOIN order_main o ON o.id = r.order_id;

-- ---------------------------------------------------------------------
-- 8. product_stats_wide：68 列全部带中文注释，考 prompt 的 token 预算裁切
--    （列数与注释完整性由文件末尾的自检查询断言，不靠人眼数）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_stats_wide (
  id INT NOT NULL COMMENT '统计行ID，主键',
  product_id INT NOT NULL COMMENT '商品ID，外键指向 product.id',
  stat_date DATE NOT NULL COMMENT '统计基准日（该日为其滚动窗口的最后一天）',
  gmv_1d DECIMAL(14,2) NOT NULL COMMENT '最近1天成交总额（元）',
  gmv_7d DECIMAL(14,2) NOT NULL COMMENT '最近7天成交总额（元）',
  gmv_14d DECIMAL(14,2) NOT NULL COMMENT '最近14天成交总额（元）',
  gmv_30d DECIMAL(14,2) NOT NULL COMMENT '最近30天成交总额（元）',
  gmv_90d DECIMAL(14,2) NOT NULL COMMENT '最近90天成交总额（元）',
  uv_1d INT NOT NULL COMMENT '最近1天访问独立访客数',
  uv_7d INT NOT NULL COMMENT '最近7天访问独立访客数',
  uv_14d INT NOT NULL COMMENT '最近14天访问独立访客数',
  uv_30d INT NOT NULL COMMENT '最近30天访问独立访客数',
  uv_90d INT NOT NULL COMMENT '最近90天访问独立访客数',
  pv_1d INT NOT NULL COMMENT '最近1天页面浏览次数',
  pv_7d INT NOT NULL COMMENT '最近7天页面浏览次数',
  pv_14d INT NOT NULL COMMENT '最近14天页面浏览次数',
  pv_30d INT NOT NULL COMMENT '最近30天页面浏览次数',
  pv_90d INT NOT NULL COMMENT '最近90天页面浏览次数',
  buyer_cnt_1d INT NOT NULL COMMENT '最近1天支付买家数',
  buyer_cnt_7d INT NOT NULL COMMENT '最近7天支付买家数',
  buyer_cnt_14d INT NOT NULL COMMENT '最近14天支付买家数',
  buyer_cnt_30d INT NOT NULL COMMENT '最近30天支付买家数',
  buyer_cnt_90d INT NOT NULL COMMENT '最近90天支付买家数',
  sales_qty_1d INT NOT NULL COMMENT '最近1天销量（件）',
  sales_qty_7d INT NOT NULL COMMENT '最近7天销量（件）',
  sales_qty_14d INT NOT NULL COMMENT '最近14天销量（件）',
  sales_qty_30d INT NOT NULL COMMENT '最近30天销量（件）',
  sales_qty_90d INT NOT NULL COMMENT '最近90天销量（件）',
  exposure_cnt_1d INT NOT NULL COMMENT '最近1天曝光次数',
  exposure_cnt_7d INT NOT NULL COMMENT '最近7天曝光次数',
  exposure_cnt_14d INT NOT NULL COMMENT '最近14天曝光次数',
  exposure_cnt_30d INT NOT NULL COMMENT '最近30天曝光次数',
  exposure_cnt_90d INT NOT NULL COMMENT '最近90天曝光次数',
  click_cnt_1d INT NOT NULL COMMENT '最近1天点击次数',
  click_cnt_7d INT NOT NULL COMMENT '最近7天点击次数',
  click_cnt_14d INT NOT NULL COMMENT '最近14天点击次数',
  click_cnt_30d INT NOT NULL COMMENT '最近30天点击次数',
  click_cnt_90d INT NOT NULL COMMENT '最近90天点击次数',
  refund_amt_1d DECIMAL(14,2) NOT NULL COMMENT '最近1天退款金额（元）',
  refund_amt_7d DECIMAL(14,2) NOT NULL COMMENT '最近7天退款金额（元）',
  refund_amt_14d DECIMAL(14,2) NOT NULL COMMENT '最近14天退款金额（元）',
  refund_amt_30d DECIMAL(14,2) NOT NULL COMMENT '最近30天退款金额（元）',
  refund_amt_90d DECIMAL(14,2) NOT NULL COMMENT '最近90天退款金额（元）',
  cvr_1d DECIMAL(6,4) NOT NULL COMMENT '最近1天支付转化率（支付买家数/独立访客数）',
  cvr_7d DECIMAL(6,4) NOT NULL COMMENT '最近7天支付转化率（支付买家数/独立访客数）',
  cvr_14d DECIMAL(6,4) NOT NULL COMMENT '最近14天支付转化率（支付买家数/独立访客数）',
  cvr_30d DECIMAL(6,4) NOT NULL COMMENT '最近30天支付转化率（支付买家数/独立访客数）',
  cvr_90d DECIMAL(6,4) NOT NULL COMMENT '最近90天支付转化率（支付买家数/独立访客数）',
  ctr_1d DECIMAL(6,4) NOT NULL COMMENT '最近1天点击率（点击次数/曝光次数）',
  ctr_7d DECIMAL(6,4) NOT NULL COMMENT '最近7天点击率（点击次数/曝光次数）',
  ctr_14d DECIMAL(6,4) NOT NULL COMMENT '最近14天点击率（点击次数/曝光次数）',
  ctr_30d DECIMAL(6,4) NOT NULL COMMENT '最近30天点击率（点击次数/曝光次数）',
  ctr_90d DECIMAL(6,4) NOT NULL COMMENT '最近90天点击率（点击次数/曝光次数）',
  return_rate_1d DECIMAL(6,4) NOT NULL COMMENT '最近1天退款率（退款金额/成交总额）',
  return_rate_7d DECIMAL(6,4) NOT NULL COMMENT '最近7天退款率（退款金额/成交总额）',
  return_rate_14d DECIMAL(6,4) NOT NULL COMMENT '最近14天退款率（退款金额/成交总额）',
  return_rate_30d DECIMAL(6,4) NOT NULL COMMENT '最近30天退款率（退款金额/成交总额）',
  return_rate_90d DECIMAL(6,4) NOT NULL COMMENT '最近90天退款率（退款金额/成交总额）',
  repurchase_cnt INT NOT NULL COMMENT '累计复购人数（同一买家购买 2 次及以上）',
  repurchase_rate DECIMAL(6,4) NOT NULL COMMENT '复购率（累计复购人数/累计买家数）',
  avg_order_amt DECIMAL(10,2) NOT NULL COMMENT '客单价（元，成交总额/支付买家数）',
  cart_add_cnt INT NOT NULL COMMENT '累计加购次数',
  fav_cnt INT NOT NULL COMMENT '累计收藏次数',
  share_cnt INT NOT NULL COMMENT '累计分享次数',
  comment_cnt INT NOT NULL COMMENT '累计评价条数',
  avg_comment_score DECIMAL(3,2) NOT NULL COMMENT '平均评分（1.00~5.00）',
  ad_spend DECIMAL(12,2) NOT NULL COMMENT '累计广告投放花费（元）',
  stock_qty INT NOT NULL COMMENT '当前可售库存（件）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_stats_product_date (product_id, stat_date),
  CONSTRAINT fk_stats_product FOREIGN KEY (product_id) REFERENCES product (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='商品运营统计宽表（按天累计口径）';

INSERT IGNORE INTO product_stats_wide
SELECT w.n, w.product_id, DATE_SUB(CURDATE(), INTERVAL 1 DAY),
       ROUND((w.b + w.n2 / 100) * 1, 2),
       ROUND((w.b + w.n2 / 100) * 7, 2),
       ROUND((w.b + w.n2 / 100) * 14, 2),
       ROUND((w.b + w.n2 / 100) * 30, 2),
       ROUND((w.b + w.n2 / 100) * 90, 2),
       (w.b * 3 + w.n2) * 1, (w.b * 3 + w.n2) * 7, (w.b * 3 + w.n2) * 14, (w.b * 3 + w.n2) * 30, (w.b * 3 + w.n2) * 90,
       (w.b * 8 + w.n3) * 1 * 3, (w.b * 8 + w.n3) * 7 * 3, (w.b * 8 + w.n3) * 14 * 3,
       (w.b * 8 + w.n3) * 30 * 3, (w.b * 8 + w.n3) * 90 * 3,
       (w.b + w.n3) * 1, (w.b + w.n3) * 7, (w.b + w.n3) * 14, (w.b + w.n3) * 30, (w.b + w.n3) * 90,
       (w.b * 2 + w.n4) * 1, (w.b * 2 + w.n4) * 7, (w.b * 2 + w.n4) * 14, (w.b * 2 + w.n4) * 30, (w.b * 2 + w.n4) * 90,
       (w.b * 20 + w.n4) * 1, (w.b * 20 + w.n4) * 7, (w.b * 20 + w.n4) * 14, (w.b * 20 + w.n4) * 30, (w.b * 20 + w.n4) * 90,
       (w.b * 2 + w.n2) * 1, (w.b * 2 + w.n2) * 7, (w.b * 2 + w.n2) * 14, (w.b * 2 + w.n2) * 30, (w.b * 2 + w.n2) * 90,
       ROUND((w.b + w.n4) * 1 / 20, 2), ROUND((w.b + w.n4) * 7 / 20, 2), ROUND((w.b + w.n4) * 14 / 20, 2),
       ROUND((w.b + w.n4) * 30 / 20, 2), ROUND((w.b + w.n4) * 90 / 20, 2),
       ROUND(0.005 + ((w.n2 + 1) % 8000) / 1000000, 4), ROUND(0.005 + ((w.n2 + 7) % 8000) / 1000000, 4),
       ROUND(0.005 + ((w.n2 + 14) % 8000) / 1000000, 4), ROUND(0.005 + ((w.n2 + 30) % 8000) / 1000000, 4),
       ROUND(0.005 + ((w.n2 + 90) % 8000) / 1000000, 4),
       ROUND(0.010 + ((w.n3 + 1) % 6000) / 100000, 4), ROUND(0.010 + ((w.n3 + 7) % 6000) / 100000, 4),
       ROUND(0.010 + ((w.n3 + 14) % 6000) / 100000, 4), ROUND(0.010 + ((w.n3 + 30) % 6000) / 100000, 4),
       ROUND(0.010 + ((w.n3 + 90) % 6000) / 100000, 4),
       ROUND(((w.n4 + 1) % 1200) / 10000, 4), ROUND(((w.n4 + 7) % 1200) / 10000, 4),
       ROUND(((w.n4 + 14) % 1200) / 10000, 4), ROUND(((w.n4 + 30) % 1200) / 10000, 4),
       ROUND(((w.n4 + 90) % 1200) / 10000, 4),
       w.n2 % 5000,
       ROUND((w.n3 % 3000) / 10000, 4),
       ROUND(80 + (w.n2 % 150000) / 100, 2),
       w.n3 % 100000,
       w.n4 % 80000,
       w.n2 % 20000,
       w.n3 % 15000,
       ROUND(3.5 + (w.n4 % 10) / 10, 2),
       ROUND((w.n2 % 500000) / 100, 2),
       1 + w.n4 % 9999
FROM (
  SELECT g.n, g.n AS product_id,
         100 + (g.x1 % 40000) AS b,
         g.x2 % 10000 AS n2,
         g.x3 % 10000 AS n3,
         g.x4 % 10000 AS n4
  FROM (
    SELECT s.n,
           CAST(CONV(LEFT(MD5(CONCAT(@seed, ':product_stats_wide:', s.n)), 8), 16, 10) AS UNSIGNED) AS x1,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':product_stats_wide:', s.n)), 9, 8), 16, 10) AS UNSIGNED) AS x2,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':product_stats_wide:', s.n)), 17, 8), 16, 10) AS UNSIGNED) AS x3,
           CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':product_stats_wide:', s.n)), 25, 8), 16, 10) AS UNSIGNED) AS x4
    FROM _seq s WHERE s.n BETWEEN 1 AND 1200
  ) g
) w;

-- ---------------------------------------------------------------------
-- 9. user_activity_log：故意不建外键，考 FORCE_FK_INFER 靠命名约定推 JOIN
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_activity_log (
  id INT NOT NULL COMMENT '埋点日志ID，主键',
  user_id INT NOT NULL COMMENT '用户ID，列名与 customer.id 对齐但**没有外键约束**（埋点表的历史包袱）',
  product_id INT NOT NULL COMMENT '商品ID，列名与 product.id 对齐但**没有外键约束**',
  session_id CHAR(32) NOT NULL COMMENT '会话ID，同一用户同一天共享',
  event_type VARCHAR(32) NOT NULL COMMENT '事件类型：view=浏览，search=搜索，cart_add=加购，fav=收藏，order_submit=提交订单，pay_success=支付成功',
  extra JSON NULL COMMENT '事件附加信息（JSON，含来源页与停留时长），考 JSON 列的抽取',
  created_at DATETIME NOT NULL COMMENT '事件发生时间',
  PRIMARY KEY (id),
  KEY idx_activity_user_created (user_id, created_at),
  KEY idx_activity_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='用户行为埋点日志（无外键约束）';

INSERT IGNORE INTO user_activity_log (id, user_id, product_id, session_id, event_type, extra, created_at)
SELECT l.n,
       1 + l.x1 % 3000,
       1 + l.x2 % 1200,
       MD5(CONCAT(@seed, ':session:', 1 + l.x1 % 3000, ':', FLOOR(l.x3 % @span_len / 86400))),
       ELT(1 + l.x3 % 6, 'view', 'search', 'cart_add', 'fav', 'order_submit', 'pay_success'),
       JSON_OBJECT('page', ELT(1 + l.x4 % 4, '/home', '/product/detail', '/cart', '/checkout'),
                   'stay_ms', 200 + (l.x4 % 48000)),
       FROM_UNIXTIME(@span_start + (l.x3 % @span_len))
FROM (
  SELECT s.n,
         CAST(CONV(LEFT(MD5(CONCAT(@seed, ':user_activity_log:', s.n)), 8), 16, 10) AS UNSIGNED) AS x1,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':user_activity_log:', s.n)), 9, 8), 16, 10) AS UNSIGNED) AS x2,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':user_activity_log:', s.n)), 17, 8), 16, 10) AS UNSIGNED) AS x3,
         CAST(CONV(SUBSTRING(MD5(CONCAT(@seed, ':user_activity_log:', s.n)), 25, 8), 16, 10) AS UNSIGNED) AS x4
  FROM _seq s WHERE s.n BETWEEN 1 AND 50000
) l;

-- ---------------------------------------------------------------------
-- 视图：列注释拿不到（MySQL 不给视图列挂注释）→ 考卡片降级模板
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_daily_sales AS
SELECT DATE(o.created_at) AS stat_date,
       COUNT(*) AS order_cnt,
       SUM(o.amount) AS total_amount,
       ROUND(AVG(o.amount), 2) AS avg_amount,
       COUNT(DISTINCT o.customer_id) AS buyer_cnt
FROM order_main o
WHERE o.status IN ('paid', 'shipped', 'completed')
GROUP BY DATE(o.created_at);

-- ---------------------------------------------------------------------
-- 应用侧只读账号：权限**只给 ai_web_demo 一个库**。
-- 不给 ON *.* —— 否则"跨库读 mysql.user"这类攻击语料在本地永远测不出真拦截。
-- host 开 localhost 与 127.0.0.1 两个：本机 TCP 连接在 MySQL 侧可能被解析成任一身份。
-- 不开 '%'：演示库只服务本机开发，没必要把账号暴露到网段。
-- ---------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'aiweb_ro'@'localhost' IDENTIFIED BY '__AIWEB_RO_PASSWORD__';
CREATE USER IF NOT EXISTS 'aiweb_ro'@'127.0.0.1' IDENTIFIED BY '__AIWEB_RO_PASSWORD__';
GRANT SELECT ON `ai_web_demo`.* TO 'aiweb_ro'@'localhost';
GRANT SELECT ON `ai_web_demo`.* TO 'aiweb_ro'@'127.0.0.1';
FLUSH PRIVILEGES;

-- 数字表只为造数而生，造完即收走：留在库里会污染元数据抽取的对象清单。
DROP TABLE IF EXISTS _seq;

-- =====================================================================
-- 自检：期望值直接抄自 docs/verification.md §1 的表格，FAIL 就不是"看起来好了"
-- =====================================================================
SELECT 'rows:ai_web_demo' AS check_name, x.obj, x.expected, IFNULL(y.actual, -1) AS actual,
       IF(x.expected = IFNULL(y.actual, -1), 'PASS', 'FAIL') AS verdict
FROM (
  SELECT 'category' AS obj, 40 AS expected
  UNION ALL SELECT 'product', 1200
  UNION ALL SELECT 'customer', 3000
  UNION ALL SELECT 'order_main', 30000
  UNION ALL SELECT 'order_item', 88000
  UNION ALL SELECT 'payment_record', 26000
  UNION ALL SELECT 'refund_record', 2400
  UNION ALL SELECT 'product_stats_wide', 1200
  UNION ALL SELECT 'user_activity_log', 50000
) x
LEFT JOIN (
  SELECT 'category' AS obj, COUNT(*) AS actual FROM category
  UNION ALL SELECT 'product', COUNT(*) FROM product
  UNION ALL SELECT 'customer', COUNT(*) FROM customer
  UNION ALL SELECT 'order_main', COUNT(*) FROM order_main
  UNION ALL SELECT 'order_item', COUNT(*) FROM order_item
  UNION ALL SELECT 'payment_record', COUNT(*) FROM payment_record
  UNION ALL SELECT 'refund_record', COUNT(*) FROM refund_record
  UNION ALL SELECT 'product_stats_wide', COUNT(*) FROM product_stats_wide
  UNION ALL SELECT 'user_activity_log', COUNT(*) FROM user_activity_log
) y ON y.obj = x.obj;

-- 对象计数口径：P3 的同步 total 必须和这里同源。
-- 业务对象 = 9 张 BASE TABLE + 1 张 VIEW = 10；下划线前缀的是脚本内部对象。
SELECT 'objects:ai_web_demo' AS check_name, x.obj, x.expected, IFNULL(y.actual, -1) AS actual,
       IF(x.expected = IFNULL(y.actual, -1), 'PASS', 'FAIL') AS verdict
FROM (
  SELECT 'business_all' AS obj, 10 AS expected
  UNION ALL SELECT 'business_base_table', 9
  UNION ALL SELECT 'business_view', 1
  UNION ALL SELECT 'internal_underscore', 1
  UNION ALL SELECT 'show_full_tables', 11
) x
LEFT JOIN (
  SELECT 'business_all' AS obj, COUNT(*) AS actual FROM information_schema.tables
    WHERE table_schema = 'ai_web_demo' AND table_name NOT LIKE '\_%'
  UNION ALL SELECT 'business_base_table', COUNT(*) FROM information_schema.tables
    WHERE table_schema = 'ai_web_demo' AND table_name NOT LIKE '\_%' AND table_type = 'BASE TABLE'
  UNION ALL SELECT 'business_view', COUNT(*) FROM information_schema.tables
    WHERE table_schema = 'ai_web_demo' AND table_name NOT LIKE '\_%' AND table_type = 'VIEW'
  UNION ALL SELECT 'internal_underscore', COUNT(*) FROM information_schema.tables
    WHERE table_schema = 'ai_web_demo' AND table_name LIKE '\_%'
  UNION ALL SELECT 'show_full_tables', COUNT(*) FROM information_schema.tables
    WHERE table_schema = 'ai_web_demo'
) y ON y.obj = x.obj;

-- 宽表 68 列必须"全部"带注释；漏注释会让卡片模板退化成裸列名，token 预算也测不准。
SELECT 'columns:product_stats_wide' AS check_name,
       COUNT(*) AS total_columns,
       SUM(IF(COLUMN_COMMENT <> '', 1, 0)) AS commented_columns,
       IF(COUNT(*) = 68 AND COUNT(*) = SUM(IF(COLUMN_COMMENT <> '', 1, 0)), 'PASS', 'FAIL') AS verdict
FROM information_schema.columns
WHERE table_schema = 'ai_web_demo' AND table_name = 'product_stats_wide';

-- 六个订单状态都要有数据，否则枚举映射的问数会在某个值上悄悄为空
SELECT 'enum:order_main.status' AS check_name, e.status, IFNULL(c.n, 0) AS rows_actual,
       IF(IFNULL(c.n, 0) > 0, 'PASS', 'FAIL') AS verdict
FROM (
  SELECT 'pending' AS status
  UNION ALL SELECT 'paid' UNION ALL SELECT 'shipped' UNION ALL SELECT 'completed'
  UNION ALL SELECT 'cancelled' UNION ALL SELECT 'refunding'
) e
LEFT JOIN (SELECT status, COUNT(*) AS n FROM order_main GROUP BY status) c ON c.status = e.status;

-- 商品状态三值同理。上一版写的是 `% 3` 配四个候选，索引永远够不到 draft，
-- 而这条断言当时不存在，所以 24 行 PASS 全绿、库里的 draft 却是 0 行。
SELECT 'enum:product.status' AS check_name, e.status, IFNULL(c.n, 0) AS rows_actual,
       IF(IFNULL(c.n, 0) > 0, 'PASS', 'FAIL') AS verdict
FROM (
  SELECT 'on_sale' AS status
  UNION ALL SELECT 'off_sale' UNION ALL SELECT 'draft'
) e
LEFT JOIN (SELECT status, COUNT(*) AS n FROM product GROUP BY status) c ON c.status = e.status;

-- 时间轴必须覆盖 2024 全年（roadmap P2 验收 4 的月度问数）且近 30 天有单
SELECT 'time:coverage' AS check_name,
       MIN(created_at) AS earliest_order, MAX(created_at) AS latest_order,
       (SELECT COUNT(*) FROM order_main WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)) AS orders_last_30d,
       (SELECT COUNT(DISTINCT DATE_FORMAT(created_at, '%Y-%m')) FROM order_main
         WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01') AS months_in_2024,
       IF((SELECT COUNT(DISTINCT DATE_FORMAT(created_at, '%Y-%m')) FROM order_main
            WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01') = 12
          AND (SELECT COUNT(*) FROM order_main WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)) > 0,
          'PASS', 'FAIL') AS verdict
FROM order_main;

-- 埋点表必须一条外键都没有，否则 FORCE_FK_INFER 的考点被建库脚本自己抹平了
SELECT 'fk:user_activity_log(must be 0)' AS check_name,
       COUNT(*) AS fk_count,
       IF(COUNT(*) = 0, 'PASS', 'FAIL') AS verdict
FROM information_schema.referential_constraints
WHERE constraint_schema = 'ai_web_demo' AND table_name = 'user_activity_log';

-- category 的自引用外键必须存在（考 JOIN 图自环）
SELECT 'fk:category.self_ref(must be 1)' AS check_name,
       COUNT(*) AS fk_count,
       IF(COUNT(*) = 1, 'PASS', 'FAIL') AS verdict
FROM information_schema.key_column_usage
WHERE constraint_schema = 'ai_web_demo' AND table_name = 'category'
  AND referenced_table_name = 'category' AND column_name = 'parent_id';

-- verification.md §1.2 第 6 步"收尾核对"。这不是 PASS/FAIL 裁决，而是一份人工核对清单，
-- 同时它正是 extractor/mysql.py 第一批要跑的 SQL —— 拿建库脚本当场验证抽取 SQL 的形状。
-- table_rows 是 InnoDB 的估算值，所以这里只列不判：判行数的活儿由上面那些 COUNT(*) 干。
SELECT 'listing:§1.2-6' AS check_name, t.table_name, t.table_type, t.table_comment, t.table_rows
FROM information_schema.tables t
WHERE t.table_schema = 'ai_web_demo'
ORDER BY t.table_type, t.table_name;
