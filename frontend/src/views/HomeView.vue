<script setup lang="ts">
import { onMounted, ref } from "vue";

import PageHeader from "@/components/PageHeader.vue";
import StatePanel from "@/components/StatePanel.vue";
import { http } from "@/api/client";

interface Health {
  status: string;
  environment: string;
  retrieval_mode: string;
}

const health = ref<Health | null>(null);
const requestId = ref("");
const error = ref("");
const loading = ref(false);

async function probe(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const { data, headers } = await http.get<Health>("/healthz");
    health.value = data;
    requestId.value = headers["x-request-id"] ?? "";
  } catch (exc) {
    health.value = null;
    requestId.value = "";
    error.value = exc instanceof Error ? exc.message : String(exc);
  } finally {
    loading.value = false;
  }
}

onMounted(probe);
</script>

<template>
  <PageHeader
    title="系统状态"
    description="后端是否可达、当前生效的检索模式。数据源与知识库指标在 P3 接入后出现在这一页。"
  >
    <template #actions>
      <ElButton :loading="loading" @click="probe">重新探测</ElButton>
    </template>
  </PageHeader>

  <StatePanel
    v-if="error"
    kind="error"
    title="后端没有响应"
    :hint="`${error}。开发服务器需单独起：make dev-backend（默认 127.0.0.1:8000）。`"
  >
    <template #action>
      <ElButton type="primary" :loading="loading" @click="probe">再试一次</ElButton>
    </template>
  </StatePanel>

  <StatePanel v-else-if="loading && !health" kind="loading" title="正在探测后端……" />

  <div v-else-if="health" class="grid">
    <section class="stat">
      <span class="stat__label">后端</span>
      <strong class="stat__value stat__value--ok">{{ health.status }}</strong>
      <span class="stat__note">/api/healthz 200</span>
    </section>
    <section class="stat">
      <span class="stat__label">环境</span>
      <strong class="stat__value">{{ health.environment }}</strong>
      <span class="stat__note">AIWEB_APP__ENVIRONMENT</span>
    </section>
    <section class="stat">
      <span class="stat__label">检索模式</span>
      <strong class="stat__value">{{ health.retrieval_mode }}</strong>
      <span class="stat__note">未配 embedding 时为 keyword，属正常</span>
    </section>
    <section class="stat">
      <span class="stat__label">本次请求 ID</span>
      <strong class="stat__value stat__value--mono">{{ requestId || "—" }}</strong>
      <span class="stat__note">排查日志时按它 grep</span>
    </section>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  gap: var(--aw-space-4);
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.stat {
  display: flex;
  flex-direction: column;
  gap: var(--aw-space-1);
  padding: var(--aw-space-4) var(--aw-space-5);
  background: var(--aw-bg-card);
  border: 1px solid var(--aw-border-2);
  border-radius: var(--aw-radius-lg);
  box-shadow: var(--aw-shadow-card);
}

.stat__label {
  font-size: var(--aw-fs-xs);
  letter-spacing: 0.02em;
  color: var(--aw-text-3);
}

.stat__value {
  font-size: var(--aw-fs-xl);
  font-weight: 600;
  color: var(--aw-text-1);
  word-break: break-all;
}

.stat__value--ok {
  color: var(--aw-color-success);
}

.stat__value--mono {
  font-family: var(--aw-font-mono);
  font-size: var(--aw-fs-lg);
}

.stat__note {
  font-size: var(--aw-fs-xs);
  color: var(--aw-text-4);
}
</style>
