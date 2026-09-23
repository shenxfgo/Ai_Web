/**
 * 侧边栏导航的唯一来源。
 * 未交付的入口保留为 disabled 并标注阶段，比放一个点开 404 的链接诚实。
 */
export interface NavItem {
  title: string;
  /** 对应路由 name；为空表示该入口尚未交付 */
  routeName?: string;
  icon: string;
  /** 未交付时给用户的解释 */
  phase?: string;
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

export const navGroups: NavGroup[] = [
  {
    title: "工作区",
    items: [
      { title: "系统状态", routeName: "home", icon: "Odometer" },
      { title: "问数", routeName: undefined, icon: "ChatLineSquare", phase: "P8 交付" },
      { title: "历史会话", routeName: undefined, icon: "Clock", phase: "P7 交付" },
    ],
  },
  {
    title: "知识库",
    items: [
      { title: "数据源", routeName: undefined, icon: "Coin", phase: "P3 交付" },
      { title: "表结构", routeName: undefined, icon: "Grid", phase: "P3 交付" },
      { title: "业务术语", routeName: undefined, icon: "Collection", phase: "P9 交付" },
    ],
  },
  {
    title: "管理",
    items: [
      { title: "用户与授权", routeName: undefined, icon: "User", phase: "P6 交付" },
      { title: "检索调优", routeName: undefined, icon: "Setting", phase: "P9 交付" },
    ],
  },
];

export interface RouteLocation {
  group?: NavGroup;
  item?: NavItem;
}

/** 按路由 name 反查导航项：顶栏面包屑和侧栏选中态共用这一份真相。 */
export function routeTitle(name: string | undefined): RouteLocation {
  if (!name) return {};
  for (const group of navGroups) {
    for (const item of group.items) if (item.routeName === name) return { group, item };
  }
  return {};
}
