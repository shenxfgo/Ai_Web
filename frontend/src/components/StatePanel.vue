<script setup lang="ts">
import { Loading } from "@element-plus/icons-vue";

withDefaults(
  defineProps<{
    /** 状态由后端错误码驱动，前端只按这三类渲染，不再自造第四种 */
    kind?: "empty" | "loading" | "error";
    title: string;
    hint?: string;
  }>(),
  { kind: "empty", hint: "" },
);
</script>

<template>
  <div class="state" :class="`state--${kind}`" role="status" aria-live="polite">
    <ElIcon v-if="kind === 'loading'" class="is-loading" :size="22" color="var(--aw-text-4)">
      <Loading />
    </ElIcon>
    <span v-else class="state__glyph" :aria-hidden="true">{{ kind === "error" ? "!" : "–" }}</span>

    <div class="state__body">
      <p class="state__title">{{ title }}</p>
      <p v-if="hint" class="state__hint">{{ hint }}</p>
      <!-- 每种空/失败态都必须给一个可点的下一步，没有动作的状态就是死路 -->
      <div v-if="$slots.action" class="state__action">
        <slot name="action" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.state {
  display: flex;
  gap: var(--aw-space-3);
  align-items: flex-start;
  padding: var(--aw-space-5);
  text-align: left;
  background: var(--aw-bg-subtle);
  border: 1px dashed var(--aw-border-1);
  border-radius: var(--aw-radius);
}

.state--error {
  background: var(--aw-color-danger-soft);
  border-color: var(--aw-border-danger);
}

.state__glyph {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  font-size: var(--aw-fs-md);
  font-weight: 600;
  color: var(--aw-text-4);
  border: 1px solid currentcolor;
  border-radius: 50%;
  flex: none;
}

.state--error .state__glyph {
  color: var(--aw-color-danger);
}

.state__body {
  min-width: 0;
}

.state__title {
  margin: 0;
  font-size: var(--aw-fs-md);
  font-weight: 600;
  color: var(--aw-text-1);
}

.state__hint {
  margin: var(--aw-space-1) 0 0;
  max-width: 68ch;
  font-size: var(--aw-fs-sm);
  color: var(--aw-text-3);
}

.state__action {
  margin-top: var(--aw-space-3);
}
</style>
