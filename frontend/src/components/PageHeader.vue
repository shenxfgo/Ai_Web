<script setup lang="ts">
withDefaults(defineProps<{ title: string; description?: string }>(), {
  description: "",
});
</script>

<template>
  <header class="ph">
    <div class="ph__text">
      <h1 class="ph__title">{{ title }}</h1>
      <p v-if="description" class="ph__desc">{{ description }}</p>
    </div>
    <div class="ph__actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped>
.ph {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--aw-space-4);
  margin-bottom: var(--aw-space-5);
}

.ph__text {
  min-width: 0;
}

.ph__title {
  font-size: var(--aw-fs-2xl);
}

.ph__desc {
  margin: var(--aw-space-1) 0 0;
  max-width: 72ch;
  font-size: var(--aw-fs-sm);
  color: var(--aw-text-3);
}

.ph__actions {
  display: flex;
  flex: none;
  align-items: center;
  gap: var(--aw-space-2);
}
</style>
