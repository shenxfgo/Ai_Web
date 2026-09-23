# UI 设计契约（专业工具风）

写代码前读这份。它不是愿景板，是**约束**：新页面如果没按这里的令牌和组件写，就算没做完。

## 1. 定位

数据密集型后台工具。用户 80% 的时间在看表结构、读 SQL、比对结果集，
所以优先级是：**信息密度 > 可读性 > 观感**。不做大面积装饰、不做营销页式 hero、不做动效表演。

## 2. 设计令牌

唯一来源：`frontend/src/styles/tokens.css`。
**禁止**在组件里写裸色值（`#409eff`、`rgb(...)`）或裸尺寸（除 `1px` 边框）——一律走 `--aw-*` 变量。

| 组 | 令牌 | 值 | 用途 |
|---|---|---|---|
| 主色 | `--aw-color-primary` | `#2f6feb` | 主按钮、链接、选中态（对白底 4.57:1，达 AA） |
| 语义 | `success/warning/danger` | `#17803d` `#ab6200` `#c62828` | 同步成功 / 部分失败 / 守卫拒绝与执行错误 |
| 文字 | `--aw-text-1..4` | `#1f2733 → #677689` | 标题 / 正文 / 次要 / 占位（均 ≥4.5:1） |
| 面 | `--aw-bg-page/card/subtle/hover` | `#f5f6f8 / #fff / #f7f9fb / #f0f3f7` | 页面底 / 卡片 / 分区底 / 行悬停 |
| 导航 | `--aw-nav-bg` `--aw-nav-bg-active` | `#1e2836` `#2f6feb` | 深色侧栏 + 浅色工作区 |
| 半径 | `--aw-radius-sm/-/-lg` | 4 / 6 / 10 px | 表单控件 6，浮层 10 |
| 间距 | `--aw-space-1..6` | 4/8/12/16/24/32 | 只用这 6 档，不插值 |
| 字号 | `--aw-fs-xs..2xl` | 12/13/14/16/18/22 | 正文 14，表格 13，标签 12，页面标题 22 |
| 阴影 | `--aw-shadow-card/-pop` | 两级 | 卡片用 card，下拉与抽屉用 pop，不用第三级 |
| 图表 | `--aw-chart-1..8` | 见令牌 | ECharts 分类色，**与状态色不混用** |

等宽数字：表格单元格与所有指标数字用 `font-variant-numeric: tabular-nums`（`base.css` 已全局给
`.num`、`.el-table .cell`）——金额位数一变就跳动的观感问题由此根治。

半透明叠加色同样是令牌，不是例外：侧栏分隔线 `--aw-nav-divider`、抽屉遮罩 `--aw-nav-scrim`、
滚动条 `--aw-scrollbar(-hover)`。写组件时冒出新颜色，就来这里补一条，别在 `*.vue` 里落 `rgb()`。

## 3. Element Plus 接入方式

- 只 `import "element-plus/theme-chalk/base.css"`（变量 + reset），组件样式交给
  `unplugin-vue-components` 的 `ElementPlusResolver` 按需注入。**不要引 `dist/index.css`**：
  实测全量 CSS 368 kB、按需 37 kB。
- 主题覆盖一律通过 `--el-*` 变量（`tokens.css` 末尾已建立映射），不改 Element 的 SCSS 变量，
  因此不需要 `sass`，也避免升级 Element 时覆盖失效。
- 需要 Element 没有的外观时，**先加令牌再写样式**，不要在页面里就地调色。

## 4. 页面骨架与外壳

```
AppShell
├── 侧栏（208px，可收起到 56px；≤900px 收起为抽屉，由汉堡键唤出 + 遮罩）
├── 顶栏（52px：面包屑「分组 › 页面」+ 环境标记，不写 h1）
└── 内容区（24px 内边距，独立滚动）
```

导航项唯一来源是 `src/layouts/nav.ts`。**未交付的入口保留为 disabled 并标注阶段**（如"P8 交付"），
不要放一个点开 404 的链接，也不要提前造空路由。

全页只有一个 `<h1>`，即 `PageHeader` 的标题；顶栏是面包屑不是标题，
否则外壳与页面会各渲染一次同名标题。

每个业务页的第一层固定为：

```vue
<PageHeader title="…" description="…">
  <template #actions> 主操作 </template>
</PageHeader>
<ElAlert v-if="pageLevelError" …>   <!-- 页面级错误，不是 toast -->
<内容>
```

规则：
- 一个页面**最多一个 primary 按钮**，放在 `PageHeader` 的 `#actions`；其余降级为 default/text。
- 页面标题在 `nav.ts` 与 `PageHeader` 只写一次；顶栏标题由外壳从导航表反查，不在页面里传。
- 列表页 = 筛选条 + 表格 + 分页；详情页 = 分段（ElTabs 或分节卡片），不做无限下拉的一坨。

## 5. 状态设计（这条最重要）

加载中 / 空 / 失败**必须区分**，且失败态要给出下一步动作。统一用 `StatePanel`：

```vue
<StatePanel kind="error" title="后端没有响应" :hint="具体原因 + 怎么修">
  <template #action><ElButton @click="retry">再试一次</ElButton></template>
</StatePanel>
```

`kind`：`empty`（结构正常但无数据）/ `loading` / `error`。
文案要求：
- `title` 说**发生了什么**，不说"出错了"。
- `hint` 说**用户下一步做什么**（对应后端错误码：`NO_SCHEMA_FOUND` → "该源未同步，点此同步"；
  `GUARD_REJECTED` → 展示违规项 + "点此编辑 SQL"；`EXEC_ERROR` → 展示驱动报错原文）。
- 只有"用户来不及看到的反馈"才用 `ElMessage`；凡是可能需要回头读的，落到页面里。

## 6. 反馈、可访问性、动效

- 焦点：全局 `:focus-visible` 双环已定义（`base.css`），**不许**用 `outline: none` 去掉。
- 对比度：正文 ≥4.5:1；状态点不能只靠颜色区分，必须同时有文字或图标。
- 键盘：Tab 顺序 = 视觉顺序；表格行内操作按钮要能 Tab 到。
- 动效 ≤200ms，只做状态过渡（侧栏收起、hover、loading）；`prefers-reduced-motion` 已全局降级。
- 中文优先排版：字体栈含 PingFang SC / Microsoft YaHei，正文 `line-height: 1.55`。

## 7. 验收清单（P7/P8 每页自查）

1. 组件里没有裸色值/裸字号（`grep -n "#[0-9a-f]\{3,6\}" src/views` 应为空）。
2. 空/加载/失败三态都有截图，且每态给出可执行下一步。
3. ≤900px 不破版，侧栏收起后主内容不横向滚动。
4. 键盘走完主流程（登录 → 建源 → 同步 → 提问）不点鼠标也能完成。
5. `npm run typecheck` 与 `npm run build` 通过。
