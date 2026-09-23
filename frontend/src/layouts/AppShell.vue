<script setup lang="ts">
import type { Component } from "vue";
import * as elementIcons from "@element-plus/icons-vue";
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { navGroups, routeTitle } from "@/layouts/nav";

const route = useRoute();
const collapsed = ref(false);
const mobileNav = ref(false);
const environment = import.meta.env.MODE;

const iconOf = (name: string): Component | undefined =>
  (elementIcons as unknown as Record<string, Component>)[name];

const current = computed(() =>
  routeTitle(typeof route.name === "string" ? route.name : undefined),
);
const groupTitle = computed(() => current.value.group?.title ?? "");
const pageTitle = computed(() => current.value.item?.title ?? "");

// 抽屉里点了菜单项就该收起，否则用户以为页面卡住了
watch(
  () => route.fullPath,
  () => {
    mobileNav.value = false;
  },
);
</script>

<template>
  <div class="shell" :class="{ 'shell--collapsed': collapsed }">
    <div class="nav-scrim" :class="{ 'nav-scrim--on': mobileNav }" @click="mobileNav = false" />

    <aside class="nav" :class="{ 'nav--open': mobileNav }">
      <div class="nav__brand">
        <span class="nav__mark">问</span>
        <span v-show="!collapsed || mobileNav" class="nav__name">AI 问数</span>
      </div>

      <nav class="nav__scroll" aria-label="主导航">
        <section v-for="group in navGroups" :key="group.title" class="nav__group">
          <p v-show="!collapsed || mobileNav" class="nav__group-title">{{ group.title }}</p>
          <ul>
            <li v-for="item in group.items" :key="item.title">
              <ElTooltip
                :content="item.phase ?? ''"
                placement="right"
                :disabled="!item.phase || mobileNav"
              >
                <RouterLink
                  v-if="item.routeName"
                  class="nav__item"
                  :class="{ 'nav__item--active': route.name === item.routeName }"
                  :to="{ name: item.routeName }"
                >
                  <ElIcon :size="17"><component :is="iconOf(item.icon)" /></ElIcon>
                  <span v-show="!collapsed || mobileNav">{{ item.title }}</span>
                </RouterLink>
                <span v-else class="nav__item nav__item--todo">
                  <ElIcon :size="17"><component :is="iconOf(item.icon)" /></ElIcon>
                  <span v-show="!collapsed || mobileNav">{{ item.title }}</span>
                </span>
              </ElTooltip>
            </li>
          </ul>
        </section>
      </nav>

      <button
        v-show="!mobileNav"
        class="nav__toggle"
        type="button"
        @click="collapsed = !collapsed"
      >
        <ElIcon :size="16"><component :is="iconOf(collapsed ? 'Expand' : 'Fold')" /></ElIcon>
        <span v-show="!collapsed">收起菜单</span>
      </button>
    </aside>

    <div class="work">
      <header class="top">
        <ElButton
          text
          :icon="iconOf('Operation')"
          class="top__burger"
          aria-label="切换导航"
          @click="mobileNav = !mobileNav"
        />
        <p class="top__crumb">
          <span>{{ groupTitle }}</span>
          <ElIcon :size="12" class="top__sep"><component :is="iconOf('ArrowRight')" /></ElIcon>
          <strong>{{ pageTitle }}</strong>
        </p>
        <div class="top__spacer" />
        <span class="top__env">{{ environment }}</span>
      </header>

      <main class="work__body">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: var(--aw-nav-width) minmax(0, 1fr);
  height: 100%;
  transition: grid-template-columns 0.18s ease;
}

.shell--collapsed {
  grid-template-columns: var(--aw-nav-width-collapsed) minmax(0, 1fr);
}

.nav {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--aw-nav-bg);
}

.nav-scrim {
  display: none;
}

.nav__brand {
  display: flex;
  gap: var(--aw-space-2);
  align-items: center;
  height: var(--aw-header-height);
  padding: 0 var(--aw-space-4);
  flex: none;
}

.nav__mark {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  font-size: var(--aw-fs-sm);
  font-weight: 600;
  color: var(--aw-text-invert);
  background: var(--aw-color-primary);
  border-radius: var(--aw-radius-sm);
  flex: none;
}

.nav__name {
  font-size: var(--aw-fs-lg);
  font-weight: 600;
  color: var(--aw-text-invert);
  white-space: nowrap;
}

.nav__scroll {
  flex: 1;
  min-height: 0;
  padding: var(--aw-space-3) var(--aw-space-2) var(--aw-space-4);
  overflow-y: auto;
}

.nav__group + .nav__group {
  margin-top: var(--aw-space-5);
}

.nav__group-title {
  padding: 0 var(--aw-space-3) var(--aw-space-2);
  margin: 0;
  font-size: var(--aw-fs-xs);
  font-weight: 500;
  letter-spacing: 0.04em;
  color: var(--aw-nav-section);
}

.nav__group ul {
  padding: 0;
  margin: 0;
  list-style: none;
  display: grid;
  gap: 2px;
}

.nav__item {
  display: flex;
  align-items: center;
  gap: var(--aw-space-3);
  height: 36px;
  padding: 0 var(--aw-space-3);
  font-size: var(--aw-fs-md);
  color: var(--aw-nav-text);
  border-radius: var(--aw-radius);
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
}

.nav__item:hover {
  color: var(--aw-text-invert);
  background: var(--aw-nav-bg-hover);
  text-decoration: none;
}

.nav__item--active {
  color: var(--aw-nav-text-active);
  background: var(--aw-nav-bg-active);
  font-weight: 500;
}

.nav__item--active:hover {
  background: var(--aw-nav-bg-active);
}

.nav__item--todo {
  color: var(--aw-nav-section);
  cursor: not-allowed;
}

.nav__item :deep(.el-icon) {
  flex: none;
}

.nav__toggle {
  display: flex;
  flex: none;
  align-items: center;
  gap: var(--aw-space-3);
  height: 40px;
  padding: 0 var(--aw-space-4);
  font-size: var(--aw-fs-sm);
  font-family: inherit;
  color: var(--aw-nav-text);
  background: none;
  border: 0;
  border-top: 1px solid var(--aw-nav-divider);
  cursor: pointer;
  white-space: nowrap;
}

.nav__toggle:hover {
  color: var(--aw-text-invert);
}

.work {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.top {
  display: flex;
  align-items: center;
  gap: var(--aw-space-3);
  flex: none;
  height: var(--aw-header-height);
  padding: 0 var(--aw-space-5);
  background: var(--aw-bg-card);
  border-bottom: 1px solid var(--aw-border-2);
}

.top__burger {
  display: none;
  padding: 0 var(--aw-space-1);
}

.top__crumb {
  display: flex;
  align-items: center;
  gap: var(--aw-space-2);
  margin: 0;
  font-size: var(--aw-fs-sm);
  color: var(--aw-text-3);
}

.top__crumb strong {
  font-size: var(--aw-fs-md);
  font-weight: 600;
  color: var(--aw-text-1);
}

.top__sep {
  color: var(--aw-text-4);
}

.top__spacer {
  flex: 1;
}

.top__env {
  padding: 2px var(--aw-space-2);
  font-size: var(--aw-fs-xs);
  color: var(--aw-text-3);
  background: var(--aw-bg-subtle);
  border: 1px solid var(--aw-border-2);
  border-radius: var(--aw-radius-sm);
}

.work__body {
  flex: 1;
  min-height: 0;
  padding: var(--aw-space-5);
  overflow: auto;
}

@media (width <= 900px) {
  .shell,
  .shell--collapsed {
    grid-template-columns: minmax(0, 1fr);
  }

  .top__burger {
    display: inline-flex;
  }

  .nav {
    position: fixed;
    z-index: 200;
    inset: 0 auto 0 0;
    width: var(--aw-nav-width);
    transform: translateX(-100%);
    transition: transform 0.18s ease;
  }

  .nav--open {
    transform: none;
  }

  .nav-scrim {
    display: block;
    position: fixed;
    z-index: 199;
    inset: 0;
    background: var(--aw-nav-scrim);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.18s ease;
  }

  .nav-scrim--on {
    opacity: 1;
    pointer-events: auto;
  }
}
</style>
