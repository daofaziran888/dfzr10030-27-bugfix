<template>
  <section class="page" data-module="report">
    <header class="page-head">
      <div>
        <h2>采集报告</h2>
        <p class="page-desc">
          按统计周期汇总采集记录与失败事件，生成可重复核对的周期报告快照。同一周期的总量、成功量与失败量都来自采集记录。
        </p>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">
          {{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}{{ m.label === '成功率' ? 'pt' : '' }}
        </span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="createReport">
      <label><span>统计周期</span><input v-model="createForm.period" data-testid="report-period" placeholder="2026-W45 / 2026-11 / 2026-11-09" /></label>
      <label><span>采集源（留空为全部）</span><input v-model="createForm.scope" data-testid="report-scope" placeholder="全部采集源" /></label>
      <button class="btn primary" type="submit" data-testid="create-period-report">生成周期报告</button>
      <span v-if="createMessage" :class="createOk ? 'hint-ok' : 'error-text'" style="font-size:12px">{{ createMessage }}</span>
    </form>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label><span>统计周期（分析）</span><input v-model="params['统计周期']" placeholder="留空查看全部周期" /></label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
      <span style="font-size:12px;color:var(--muted)">分析结果只回放周期报告快照，同一周期多次运行结果一致。</span>
    </form>

    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }">
        <span>{{ bar.label }}</span>
      </div>
    </div>

    <table class="data-table" data-testid="report-table">
      <thead>
        <tr>
          <th>报告编号</th><th>采集源</th><th>统计周期</th><th>版本</th>
          <th>总采集量</th><th>成功量</th><th>失败量</th><th>平均耗秒</th><th>未结算</th>
          <th>生成时间</th><th>核对</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in results" :key="row.id" :data-testid="`report-row-${row.id}`">
          <td>{{ row['报告编号'] }}</td>
          <td>{{ row['采集源'] }}</td>
          <td>{{ row['统计周期'] }}</td>
          <td>{{ row['版本'] }}</td>
          <td>{{ row['总采集量'] }}</td>
          <td>{{ row['成功量'] }}</td>
          <td>{{ row['失败量'] }}</td>
          <td>{{ row['平均耗秒'] }}</td>
          <td>{{ row['未结算量'] ?? 0 }}</td>
          <td>{{ row['生成时间'] }}</td>
          <td>
            <span :class="row['核对']?.['核对结论'] === '一致' ? 'tag ok' : 'tag warn'">
              {{ row['核对']?.['核对结论'] ?? '—' }}
            </span>
          </td>
          <td><button class="link" @click="openDetail(row.id)">查看周期报告</button></td>
        </tr>
        <tr v-if="!results.length"><td colspan="12" class="empty-state">暂无周期采集报告</td></tr>
      </tbody>
    </table>

    <section class="panel">
      <header class="panel-head">
        <h3>失败事件</h3>
        <span class="page-desc">
          事件时间口径修订原周期报告；报告生成时间口径把迟到事件计入下一周期。两种口径都会把失败与采集记录配对，不会算成成功。
        </span>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>事件编号</th><th>采集源</th><th>任务</th><th>事件时间</th><th>上报时间</th><th>归属策略</th><th>归属周期</th><th>失败原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="event in events" :key="event.id">
            <td>{{ event['事件编号'] }}</td>
            <td>{{ event['采集源'] }}</td>
            <td>{{ event['任务'] }}</td>
            <td>{{ event['事件时间'] }}</td>
            <td>{{ event['上报时间'] }}</td>
            <td>{{ policyLabel(event['归属策略']) }}</td>
            <td>{{ event['归属周期'] }}</td>
            <td>{{ event['失败原因'] }}</td>
          </tr>
          <tr v-if="!events.length"><td colspan="8" class="empty-state">暂无失败事件</td></tr>
        </tbody>
      </table>
      <form class="analysis-form" @submit.prevent="submitEvent">
        <label><span>事件编号</span><input v-model="eventForm['事件编号']" data-testid="event-no" placeholder="FAIL-0005" /></label>
        <label><span>采集源</span><input v-model="eventForm['采集源']" placeholder="官网抓取" /></label>
        <label><span>任务</span><input v-model="eventForm['任务']" placeholder="官网商品列表" /></label>
        <label><span>事件时间</span><input v-model="eventForm['事件时间']" data-testid="event-time" placeholder="2026-11-18T10:00:00" /></label>
        <label><span>上报时间</span><input v-model="eventForm['上报时间']" placeholder="留空取当前时间" /></label>
        <label><span>归属记录 id</span><input v-model="eventForm['归属记录']" placeholder="可留空按来源+任务+时间匹配" /></label>
        <label>
          <span>归属策略</span>
          <select v-model="eventForm['归属策略']" data-testid="event-policy">
            <option value="event_time">按事件发生时间（修订原周期）</option>
            <option value="report_time">按报告生成时间（计入下一周期）</option>
          </select>
        </label>
        <label><span>失败原因</span><input v-model="eventForm['失败原因']" placeholder="接口超时" /></label>
        <button class="btn" type="submit" data-testid="submit-event">登记失败事件</button>
      </form>
      <p v-if="eventMessage" :class="eventOk ? 'hint-ok' : 'error-text'" style="font-size:12px">{{ eventMessage }}</p>
    </section>

    <div v-if="detail" class="modal-backdrop" @click.self="detail = null">
      <div class="modal report-detail" data-testid="report-detail">
        <header class="panel-head">
          <h3>{{ detail['报告编号'] }} · {{ detail['统计周期'] }} · 版本 {{ detail['版本'] }}</h3>
          <button class="btn ghost" @click="detail = null">关闭</button>
        </header>
        <p class="page-desc">{{ detail['修订原因'] }}</p>
        <div class="stat-row">
          <div class="stat-card"><span class="stat-label">总采集量</span><strong class="stat-value">{{ detail['总采集量'] }}</strong></div>
          <div class="stat-card"><span class="stat-label">成功量</span><strong class="stat-value">{{ detail['成功量'] }}</strong></div>
          <div class="stat-card"><span class="stat-label">失败量</span><strong class="stat-value">{{ detail['失败量'] }}</strong></div>
          <div class="stat-card"><span class="stat-label">平均耗秒</span><strong class="stat-value">{{ detail['平均耗秒'] }}</strong></div>
          <div class="stat-card"><span class="stat-label">未结算</span><strong class="stat-value">{{ detail['未结算量'] }}</strong></div>
        </div>

        <h4>周期核对</h4>
        <table class="data-table">
          <tbody>
            <tr v-for="(value, key) in detail['核对']" :key="key"><th style="width:180px">{{ key }}</th><td>{{ value }}</td></tr>
          </tbody>
        </table>

        <h4>失败明细（{{ detail['失败明细'].length }}）</h4>
        <table class="data-table">
          <thead><tr><th>记录编号</th><th>任务</th><th>发生时间</th><th>失败原因</th><th>事件编号</th><th>来源</th><th>归因</th></tr></thead>
          <tbody>
            <tr v-for="(item, i) in detail['失败明细']" :key="i">
              <td>{{ item['记录编号'] || '—' }}</td>
              <td>{{ item['任务'] }}</td>
              <td>{{ item['发生时间'] }}</td>
              <td>{{ item['失败原因'] }}</td>
              <td>{{ item['事件编号'] || '—' }}</td>
              <td>{{ item['来源'] }}</td>
              <td>{{ item['归因'] }}</td>
            </tr>
            <tr v-if="!detail['失败明细'].length"><td colspan="7" class="empty-state">本周期没有失败</td></tr>
          </tbody>
        </table>

        <h4>异常与修订依据</h4>
        <table class="data-table">
          <thead><tr><th>类型</th><th>对象</th><th>说明</th><th>依据</th></tr></thead>
          <tbody>
            <tr v-for="(item, i) in detail['异常项']" :key="i">
              <td>{{ item['类型'] }}</td>
              <td>{{ item['对象'] }}</td>
              <td>{{ item['说明'] }}</td>
              <td>{{ item['依据'] || '—' }}</td>
            </tr>
            <tr v-if="!detail['异常项'].length"><td colspan="4" class="empty-state">无异常</td></tr>
          </tbody>
        </table>

        <h4>修订记录</h4>
        <table class="data-table" data-testid="version-table">
          <thead><tr><th>版本</th><th>总采集量</th><th>成功量</th><th>失败量</th><th>平均耗秒</th><th>生成时间</th><th>修订原因</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="version in detail['修订记录']" :key="version['版本']">
              <td>{{ version['版本'] }}</td>
              <td>{{ version['总采集量'] }}</td>
              <td>{{ version['成功量'] }}</td>
              <td>{{ version['失败量'] }}</td>
              <td>{{ version['平均耗秒'] }}</td>
              <td>{{ version['生成时间'] }}</td>
              <td>{{ version['修订原因'] }}</td>
              <td><button class="link" @click="openVersion(version['版本'])">查看旧摘要</button></td>
            </tr>
          </tbody>
        </table>
        <p v-if="versionSnapshot" class="version-note" data-testid="version-snapshot">
          历史版本 {{ versionSnapshot['版本'] }}：{{ versionSnapshot['指标摘要'] }}（生成于 {{ versionSnapshot['生成时间'] }}）
        </p>

        <div class="row-actions" style="margin-top:12px">
          <button class="btn primary" @click="regenerate(detail['id'])">重新生成</button>
          <span v-if="detailMessage" style="font-size:12px;color:var(--muted)">{{ detailMessage }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'

type Metric = { label: string; value: number; trend: number }
type ReportRow = Record<string, any>
type EventRow = Record<string, any>

const ENDPOINT = '/api/report'
const summaryMetrics = ref<Metric[]>([])
const results = ref<ReportRow[]>([])
const events = ref<EventRow[]>([])
const chartData = ref<{ label: string; value: number }[]>([])
const loading = ref(false)
const params = reactive<Record<string, string>>({ 统计周期: '' })
const createForm = reactive({ period: '', scope: '' })
const createMessage = ref('')
const createOk = ref(true)
const eventMessage = ref('')
const eventOk = ref(true)
const detail = ref<ReportRow | null>(null)
const detailMessage = ref('')
const versionSnapshot = ref<Record<string, any> | null>(null)
const policies = ref<{ value: string; label: string }[]>([
  { value: 'event_time', label: '按事件发生时间' },
  { value: 'report_time', label: '按报告生成时间' },
])
const eventForm = reactive<Record<string, string>>({
  事件编号: '', 采集源: '', 任务: '', 事件时间: '', 上报时间: '', 归属记录: '', 归属策略: 'event_time', 失败原因: '',
})

function policyLabel(value: string) {
  return policies.value.find((item) => item.value === value)?.label ?? value
}

function barHeight(v: number) {
  const max = Math.max(...chartData.value.map((d) => d.value), 1)
  return Math.max(10, (v / max) * 140)
}

async function fetchSummary() {
  try {
    const res = await request(`${ENDPOINT}/summary`)
    summaryMetrics.value = (await res.json()).metrics || []
  } catch (e) { /* 保留空卡片，页面仍可操作 */ }
}

async function fetchReports() {
  try {
    const res = await request(`${ENDPOINT}`)
    const data = await res.json()
    results.value = data.items || []
  } catch (e) { results.value = [] }
}

async function fetchEvents() {
  try {
    const res = await request(`${ENDPOINT}/events`)
    const data = await res.json()
    events.value = data.items || []
    if (data.policies?.length) policies.value = data.policies
  } catch (e) { events.value = [] }
}

async function runAnalysis() {
  loading.value = true
  try {
    const res = await request(`${ENDPOINT}/run`, {
      method: 'POST',
      body: JSON.stringify({ values: { ...params } }),
    })
    const data = await res.json()
    results.value = data.items || []
    chartData.value = (data.items || []).map((row: ReportRow) => ({
      label: String(row['统计周期'] ?? row['报告编号']),
      value: Number(row['总采集量'] ?? 0),
    })).slice(0, 8)
  } catch (e) { results.value = [] }
  loading.value = false
}

async function createReport() {
  createMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/period`, {
      method: 'POST',
      body: JSON.stringify({ 统计周期: createForm.period, 采集源: createForm.scope }),
    })
    const data = await res.json()
    createOk.value = Boolean(data.ok)
    createMessage.value = data.message
    if (data.ok) {
      await Promise.all([fetchReports(), fetchEvents(), fetchSummary()])
    }
  } catch (e) {
    createOk.value = false
    createMessage.value = '生成失败，请确认后端服务可用'
  }
}

async function openDetail(id: number) {
  versionSnapshot.value = null
  detailMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${id}`)
    detail.value = await res.json()
  } catch (e) { detail.value = null }
}

async function openVersion(version: number) {
  if (!detail.value) return
  try {
    const res = await request(`${ENDPOINT}/${detail.value['id']}/versions/${version}`)
    versionSnapshot.value = await res.json()
  } catch (e) { versionSnapshot.value = null }
}

async function regenerate(id: number) {
  try {
    const res = await request(`${ENDPOINT}/${id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action: '重新生成' }),
    })
    const data = await res.json()
    detailMessage.value = data.message
    if (data.entry) detail.value = { ...detail.value, ...data.entry }
    await Promise.all([fetchReports(), fetchEvents(), fetchSummary()])
  } catch (e) { detailMessage.value = '重新生成失败' }
}

async function submitEvent() {
  eventMessage.value = ''
  const payload: Record<string, unknown> = { ...eventForm }
  if (!eventForm['归属记录']) delete payload['归属记录']
  if (!eventForm['上报时间']) delete payload['上报时间']
  try {
    const res = await request(`${ENDPOINT}/events`, { method: 'POST', body: JSON.stringify(payload) })
    const data = await res.json()
    eventOk.value = Boolean(data.ok)
    eventMessage.value = data.message
    if (data.ok) {
      await Promise.all([fetchEvents(), fetchReports(), fetchSummary()])
      for (const key of ['事件编号', '事件时间', '上报时间', '归属记录', '失败原因']) eventForm[key] = ''
    }
  } catch (e) {
    eventOk.value = false
    eventMessage.value = '登记失败，请确认后端服务可用'
  }
}

onMounted(async () => {
  await Promise.all([fetchSummary(), fetchReports(), fetchEvents()])
})
</script>
