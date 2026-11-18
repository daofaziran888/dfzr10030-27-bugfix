<template>
  <section class="page" data-module="monitor">
    <header class="page-head">
      <div>
        <h2>采集监控</h2>
        <p class="page-desc">
          消费周期采集报告，把修订版报告的失败增量同步进趋势；核对差异回写报告并进入待办工作台。面板与报告页共用同一份周期快照。
        </p>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">
          {{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}
        </span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="consume">
      <label><span>统计周期（留空消费全部）</span><input v-model="params['统计周期']" data-testid="consume-period" placeholder="2026-W45" /></label>
      <button class="btn primary" type="submit" data-testid="consume-reports">消费周期报告</button>
      <button class="btn" type="button" data-testid="run-analysis" @click="runAnalysis">运行分析</button>
      <span v-if="message" style="font-size:12px;color:var(--muted)">{{ message }}</span>
    </form>

    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }">
        <span>{{ bar.label }}</span>
      </div>
    </div>

    <table class="data-table" data-testid="trend-table">
      <thead>
        <tr>
          <th>报告编号</th><th>统计周期</th><th>版本</th><th>总采集量</th><th>成功量</th>
          <th>失败量</th><th>失败增量</th><th>平均耗秒</th><th>生成时间</th><th>消费时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="point in trend" :key="point.id">
          <td>{{ point['报告编号'] }}</td>
          <td>{{ point['统计周期'] }}</td>
          <td>v{{ point['版本'] }}</td>
          <td>{{ point['总采集量'] }}</td>
          <td>{{ point['成功量'] }}</td>
          <td>{{ point['失败量'] }}</td>
          <td :class="point['失败增量'] > 0 ? 'trend-down' : ''">{{ point['失败增量'] > 0 ? '+' : '' }}{{ point['失败增量'] }}</td>
          <td>{{ point['平均耗秒'] }}</td>
          <td>{{ point['生成时间'] }}</td>
          <td>{{ point['消费时间'] }}</td>
        </tr>
        <tr v-if="!trend.length"><td colspan="10" class="empty-state">尚未消费周期报告</td></tr>
      </tbody>
    </table>

    <h3 class="panel-title">已消费的周期报告（持久化结果）</h3>
    <table class="data-table" data-testid="consumed-table">
      <thead>
        <tr><th>报告编号</th><th>统计周期</th><th>已消费版本</th><th>总采集量</th><th>成功量</th><th>失败量</th><th>失败增量</th><th>趋势点数</th><th>核对结论</th><th>差异</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in consumed" :key="row['报告编号']">
          <td>{{ row['报告编号'] }}</td>
          <td>{{ row['统计周期'] }}</td>
          <td>v{{ row['已消费版本'] }}</td>
          <td>{{ row['总采集量'] }}</td>
          <td>{{ row['成功量'] }}</td>
          <td>{{ row['失败量'] }}</td>
          <td>{{ row['失败增量'] }}</td>
          <td>{{ row['趋势点数'] }}</td>
          <td><span :class="row['核对结论'] === '一致' ? 'tag ok' : 'tag warn'">{{ row['核对结论'] }}</span></td>
          <td>{{ (row['差异'] || []).join('；') || '—' }}</td>
        </tr>
        <tr v-if="!consumed.length"><td colspan="10" class="empty-state">尚未消费周期报告</td></tr>
      </tbody>
    </table>

    <h3 class="panel-title">待办工作台</h3>
    <table class="data-table" data-testid="todo-table">
      <thead><tr><th>待办编号</th><th>报告编号</th><th>统计周期</th><th>版本</th><th>待办原因</th><th>说明</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="todo in todos" :key="todo.id">
          <td>{{ todo['待办编号'] }}</td>
          <td>{{ todo['报告编号'] }}</td>
          <td>{{ todo['统计周期'] }}</td>
          <td>v{{ todo['版本'] }}</td>
          <td>{{ todo['待办原因'] }}</td>
          <td>{{ todo['待办说明'] }}</td>
          <td><span :class="todo['状态'] === '已处理' ? 'tag ok' : 'tag warn'">{{ todo['状态'] }}</span></td>
          <td class="row-actions">
            <button v-if="todo['状态'] === '待处理'" class="link" @click="todoAction(todo.id, '开始处理')">开始处理</button>
            <button v-if="todo['状态'] !== '已处理'" class="link" @click="todoAction(todo.id, '标记已处理')">标记已处理</button>
            <span v-if="todo['状态'] === '已处理'" style="color:var(--muted);font-size:12px">已闭环</span>
          </td>
        </tr>
        <tr v-if="!todos.length"><td colspan="8" class="empty-state">暂无待办</td></tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'

type Metric = { label: string; value: number; trend: number }

const ENDPOINT = '/api/monitor'
const summaryMetrics = ref<Metric[]>([])
const trend = ref<any[]>([])
const consumed = ref<any[]>([])
const todos = ref<any[]>([])
const chartData = ref<{ label: string; value: number }[]>([])
const message = ref('')
const params = reactive<Record<string, string>>({ 统计周期: '' })

function barHeight(v: number) {
  const max = Math.max(...chartData.value.map((d) => d.value), 1)
  return Math.max(10, (v / max) * 140)
}

async function fetchSummary() {
  try {
    const res = await request(`${ENDPOINT}/summary`)
    summaryMetrics.value = (await res.json()).metrics || []
  } catch (e) { /* 页面保持可操作 */ }
}

async function fetchTrend() {
  try {
    const res = await request(`${ENDPOINT}/trend`)
    trend.value = (await res.json()).items || []
    chartData.value = trend.value.slice(-8).map((point) => ({
      label: `${point['统计周期']} v${point['版本']}`,
      value: Number(point['失败量'] ?? 0),
    }))
  } catch (e) { trend.value = [] }
}

async function fetchConsumed() {
  try {
    const res = await request(`${ENDPOINT}/reports`)
    consumed.value = (await res.json()).items || []
  } catch (e) { consumed.value = [] }
}

async function fetchTodos() {
  try {
    const res = await request(`${ENDPOINT}/todos`)
    todos.value = (await res.json()).items || []
  } catch (e) { todos.value = [] }
}

async function refreshAll() {
  await Promise.all([fetchSummary(), fetchTrend(), fetchConsumed(), fetchTodos()])
}

async function consume() {
  message.value = ''
  try {
    const res = await request(`${ENDPOINT}/reports/consume`, {
      method: 'POST',
      body: JSON.stringify({ 统计周期: params['统计周期'] || undefined }),
    })
    const data = await res.json()
    if (!res.ok) {
      message.value = data.detail || '消费失败'
      return
    }
    const added = (data.consumed || []).reduce((sum: number, item: any) => sum + (item['本次新增趋势点'] || 0), 0)
    message.value = `已消费 ${(data.consumed || []).length} 份报告，新增趋势点 ${added} 个`
    await refreshAll()
  } catch (e) {
    message.value = '消费失败，请确认后端服务可用'
  }
}

async function runAnalysis() {
  message.value = ''
  try {
    const res = await request(`${ENDPOINT}/run`, {
      method: 'POST',
      body: JSON.stringify({ values: { ...params } }),
    })
    const data = await res.json()
    trend.value = data.items || []
    chartData.value = trend.value.slice(-8).map((point: any) => ({
      label: `${point['统计周期']} v${point['版本']}`,
      value: Number(point['失败增量'] ?? 0),
    }))
    message.value = '已按报告版本回放趋势'
  } catch (e) { message.value = '运行分析失败' }
}

async function todoAction(id: number, action: string) {
  try {
    const res = await request(`${ENDPOINT}/todos/${id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const data = await res.json()
    message.value = data.message
    await fetchTodos()
  } catch (e) { message.value = '待办更新失败' }
}

onMounted(refreshAll)
</script>
