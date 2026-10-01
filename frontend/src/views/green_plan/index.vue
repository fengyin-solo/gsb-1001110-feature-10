<template>
  <section class="page" data-module="green-plan">
    <header class="page-head">
      <div>
        <h2>修剪灌溉计划板</h2>
        <p class="page-desc">按物候周期滚动枚举未来两周：区域品种 × 物候口径 × 近期天气 × 巡查发现 × 作业车辆，生成结果同步绿化台账、巡查待办与车辆排班清单。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="generate()">生成未来两周计划</button>
        <button class="btn" type="button" @click="generate(true)">演练发布中断</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">计划任务</span>
        <strong class="stat-value">{{ board.total ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">未开始</span>
        <strong class="stat-value">{{ board.counts?.['未开始'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">应急管制待办</span>
        <strong class="stat-value" :class="{ 'emergency-num': (board.emergency ?? 0) > 0 }">{{ board.emergency ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">计划批次</span>
        <strong class="stat-value">{{ board.batches?.length ?? 0 }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="tab-item"
        :class="{ active: activeTab === tab.key }"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="message" class="tip-line" :class="messageOk ? 'tip-ok' : 'error-text'">{{ message }}</p>

    <!-- ============================== 计划板 ============================== -->
    <div v-if="activeTab === 'board'">
      <form class="filter-bar" @submit.prevent="loadBoard">
        <label class="filter-item">
          <span>计划日期</span>
          <input v-model="filters.day" type="date" />
        </label>
        <label class="filter-item">
          <span>任务类型</span>
          <select v-model="filters.task_type">
            <option value="">全部</option>
            <option value="修剪">修剪</option>
            <option value="灌溉">灌溉</option>
          </select>
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="filters.status">
            <option value="">全部</option>
            <option v-for="s in statusOptions" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>优先级</span>
          <select v-model="filters.priority">
            <option value="">全部</option>
            <option value="常规">常规</option>
            <option value="应急管制">应急管制</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置</button>
      </form>

      <div v-for="group in board.days" :key="group.日期" class="day-block">
        <h3 class="day-title">{{ group.日期 }}（{{ weekday(group.日期) }}）</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>任务编号</th><th>区域</th><th>品种</th><th>类型</th><th>计划时段</th>
              <th>车辆</th><th>班组</th><th>依据</th><th>口径</th><th>优先级</th><th>天气</th><th>状态</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in group.tasks" :key="t.任务编号">
              <td>{{ t.任务编号 }}</td>
              <td>{{ t.区域编号 }}<br /><span class="sub-text">{{ t.区域名称 }}</span></td>
              <td>{{ t.植物品种 }}</td>
              <td>{{ t.任务类型 }}</td>
              <td>
                <span class="baseline">{{ t.计划时段 }}</span>
                <span v-if="t.实际时段" class="actual-text">→ 实 {{ t.实际时段 }}</span>
              </td>
              <td>
                <span class="baseline">{{ t.计划车辆编号 ?? '—' }}</span>
                <span v-if="t.实际车辆编号" class="actual-text">→ 实 {{ t.实际车辆编号 }}</span>
              </td>
              <td>{{ t.实际班组 || t.计划班组 }}</td>
              <td>
                {{ t.计划依据 }}
                <div v-if="t.巡查问题" class="sub-text">巡查 {{ t.来源巡查 }}：{{ t.巡查问题 }}</div>
              </td>
              <td>v{{ t.口径版本 }}</td>
              <td><span class="badge" :class="t.优先级 === '应急管制' ? 'badge-danger' : 'badge-normal'">{{ t.优先级 }}</span></td>
              <td>
                {{ t.天气摘要 }}
                <div v-if="t.应急说明" class="sub-text">{{ t.应急说明 }}</div>
              </td>
              <td><span class="badge" :class="statusClass(t.status)">{{ t.status }}</span></td>
              <td class="row-actions">
                <button v-if="canComplete(t)" class="link" type="button" @click="openComplete(t)">完成登记</button>
                <button v-if="canComplete(t)" class="link" type="button" @click="openAdjust(t)">现场调整</button>
                <span v-if="!canComplete(t)" class="sub-text">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!board.days?.length" class="empty-state">当前筛选下暂无计划任务，可点击右上角生成未来两周计划。</p>
    </div>

    <!-- ============================== 批次 ============================== -->
    <div v-if="activeTab === 'batch'">
      <table class="data-table">
        <thead>
          <tr><th>批次键（幂等调度键）</th><th>窗口</th><th>口径</th><th>状态</th><th>已生成截至</th><th>任务数</th><th>备注</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in board.batches" :key="b.id">
            <td>{{ b.批次键 }}</td>
            <td>{{ b.起始日期 }} ~ {{ b.结束日期 }}</td>
            <td>v{{ b.口径版本 }}</td>
            <td><span class="badge" :class="batchClass(b.状态)">{{ b.状态 }}</span></td>
            <td>{{ b.已生成截至 ?? '—' }}</td>
            <td>{{ b.任务数 }}</td>
            <td class="sub-text">{{ b.备注 }}</td>
            <td>
              <button v-if="b.状态 === '草稿'" class="link" type="button" @click="resume(b.id)">重连续传</button>
              <span v-else class="sub-text">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="tip-line">同一批次键的重复调度只生效一次；发布中断保留草稿，重连后从「已生成截至」的次日继续演算，最终一次事务提交。</p>
    </div>

    <!-- ============================== 物候口径 ============================== -->
    <div v-if="activeTab === 'version'">
      <div class="panel">
        <h3>现行口径 v{{ currentVersion.version }}（{{ currentVersion.生效日期 }} 生效）</h3>
        <p class="sub-text">{{ currentVersion.调整说明 }}</p>
        <table class="data-table">
          <thead>
            <tr><th>品种类别</th><th v-for="f in thresholdFields" :key="f">{{ f }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="cat in categories" :key="cat">
              <td>{{ cat }}</td>
              <td v-for="f in thresholdFields" :key="f">
                <input v-model.number="draftProfiles[cat][f]" type="number" min="1" class="num-input" />
              </td>
            </tr>
          </tbody>
        </table>
        <div class="filter-bar">
          <label class="filter-item">
            <span>调整说明</span>
            <input v-model="versionRemark" placeholder="例如：入秋修剪周期整体放宽" style="width: 260px" />
          </label>
          <button class="btn primary" type="button" @click="adjustVersion">发布新口径并重算未开始任务</button>
        </div>
        <p class="tip-line">阈值调整后：未开始/已调整未执行任务按新口径重算（旧任务标「已迁移」并释放车辆）；已执行记录保留当时基准快照。</p>
      </div>
      <h3>历史版本</h3>
      <table class="data-table">
        <thead><tr><th>版本</th><th>状态</th><th>生效日期</th><th>调整说明</th></tr></thead>
        <tbody>
          <tr v-for="v in versions" :key="v.id">
            <td>v{{ v.version }}</td>
            <td>{{ v.status }}</td>
            <td>{{ v.生效日期 }}</td>
            <td>{{ v.调整说明 }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============================== 绿化台账 ============================== -->
    <div v-if="activeTab === 'ledger'">
      <form class="filter-bar" @submit.prevent="loadLedger">
        <label class="filter-item">
          <span>记录类型</span>
          <select v-model="ledgerFilter">
            <option value="">全部</option>
            <option value="修剪">修剪</option>
            <option value="灌溉">灌溉</option>
            <option value="巡查整改">巡查整改</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
      </form>
      <table class="data-table">
        <thead>
          <tr><th>台账编号</th><th>类型</th><th>区域</th><th>计划基线</th><th>实际完成</th><th>口径快照</th><th>车辆</th><th>天气/优先级</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="l in ledger" :key="l.id">
            <td>{{ l.台账编号 }}</td>
            <td>{{ l.记录类型 }}</td>
            <td>{{ l.区域编号 }} {{ l.区域名称 }}</td>
            <td>
              <div>{{ l.计划日期 }} {{ l.计划时段 }}</div>
              <div class="sub-text">{{ l.计划班组 }}</div>
            </td>
            <td>
              <div v-if="l.实际日期" class="actual-text">{{ l.实际日期 }} {{ l.实际时段 || '' }}</div>
              <div v-if="l.实际完成时间" class="sub-text">完成时间：{{ l.实际完成时间 }}</div>
              <div v-if="l.调整原因" class="sub-text">调整：{{ l.调整原因 }}</div>
              <div v-if="l.迁移说明" class="sub-text">{{ l.迁移说明 }}</div>
              <span v-if="!l.实际日期">—</span>
            </td>
            <td>{{ l.计划口径版本 }}<div class="sub-text">{{ snapshotText(l.执行口径快照) }}</div></td>
            <td>{{ l.实际车辆编号 || l.计划车辆编号 }}</td>
            <td>{{ l.天气摘要 }}<div><span class="badge" :class="l.优先级 === '应急管制' ? 'badge-danger' : 'badge-normal'">{{ l.优先级 }}</span></div></td>
            <td><span class="badge" :class="statusClass(l.status)">{{ l.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============================== 巡查待办 ============================== -->
    <div v-if="activeTab === 'todo'">
      <table class="data-table">
        <thead>
          <tr><th>待办编号</th><th>巡查编号</th><th>发现问题</th><th>处置措施</th><th>计划日期</th><th>优先级</th><th>关联任务</th><th>口径</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="t in todos" :key="t.id">
            <td>{{ t.待办编号 }}</td>
            <td>{{ t.巡查编号 }}</td>
            <td>{{ t.发现问题 }}</td>
            <td>{{ t.处置措施 }}</td>
            <td>{{ t.计划日期 }}</td>
            <td><span class="badge" :class="t.优先级 === '应急管制' ? 'badge-danger' : 'badge-normal'">{{ t.优先级 }}</span></td>
            <td>{{ t.关联任务 }}</td>
            <td>{{ t.口径版本 }}</td>
            <td><span class="badge" :class="statusClass(t.status)">{{ t.status }}</span></td>
          </tr>
        </tbody>
      </table>
      <p v-if="!todos.length" class="empty-state">暂无巡查待办，生成计划后巡查发现会自动推入。</p>
    </div>

    <!-- ============================== 车辆排班 ============================== -->
    <div v-if="activeTab === 'schedule'">
      <form class="filter-bar" @submit.prevent="loadSchedules">
        <label class="filter-item">
          <span>排班日期</span>
          <input v-model="scheduleDay" type="date" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="scheduleDay = ''; loadSchedules()">全量</button>
      </form>
      <table class="data-table">
        <thead>
          <tr><th>排班单号</th><th>日期</th><th>时段</th><th>车辆编号</th><th>车牌号</th><th>车辆类型</th><th>任务</th><th>区域</th><th>关联任务</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in schedules" :key="s.id">
            <td>{{ s.排班单号 }}</td>
            <td>{{ s.计划日期 }}</td>
            <td>{{ s.计划时段 }}</td>
            <td>{{ s.车辆编号 }}</td>
            <td>{{ s.车牌号 }}</td>
            <td>{{ s.车辆类型 }}</td>
            <td>{{ s.任务类型 }}</td>
            <td>{{ s.区域名称 }}</td>
            <td>{{ s.关联任务 }}</td>
            <td><span class="badge" :class="statusClass(s.status)">{{ s.status }}</span></td>
          </tr>
        </tbody>
      </table>
      <p class="tip-line">只有事务提交后的任务才占用车辆；演算失败或发布中断不会写入排班清单。</p>
    </div>

    <!-- ====================== 现场调整 / 完成登记弹层 ====================== -->
    <div v-if="dialogTask" class="modal-mask" @click.self="dialogTask = null">
      <div class="modal-box">
        <h3>{{ dialogMode === 'complete' ? '完成登记' : '现场调整' }} · {{ dialogTask.任务编号 }}</h3>
        <p class="sub-text">
          计划基线：{{ dialogTask.计划日期 }} {{ dialogTask.计划时段 }} {{ dialogTask.计划车辆编号 }}
          （口径 v{{ dialogTask.口径版本 }}，只读保留）
        </p>
        <table class="data-table">
          <tbody>
            <tr>
              <th>实际日期</th>
              <td><input v-model="dialogForm.实际日期" type="date" /></td>
            </tr>
            <tr>
              <th>实际时段</th>
              <td>
                <select v-model="dialogForm.实际时段">
                  <option v-for="slot in slotOptions(dialogTask.任务类型)" :key="slot" :value="slot">{{ slot }}</option>
                </select>
              </td>
            </tr>
            <tr>
              <th>实际车辆编号</th>
              <td>
                <input v-model="dialogForm.实际车辆编号" :placeholder="`留空则用计划车 ${dialogTask.计划车辆编号 ?? ''}`" />
              </td>
            </tr>
            <tr>
              <th>实际班组</th>
              <td><input v-model="dialogForm.实际班组" :placeholder="dialogTask.计划班组 ?? ''" /></td>
            </tr>
            <tr v-if="dialogMode === 'complete'">
              <th>完成结果</th>
              <td><input v-model="dialogForm.完成结果" placeholder="例如：修剪到位 / 浇透返青" /></td>
            </tr>
            <tr v-else>
              <th>调整原因 *</th>
              <td><input v-model="dialogForm.调整原因" placeholder="现场调整必须填写原因" /></td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn" type="button" @click="dialogTask = null">取消</button>
          <button class="btn primary" type="button" @click="submitDialog">提交</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const BASE = '/api/green-plan'

type Task = Record<string, any>
type Row = Record<string, any>

const tabs = [
  { key: 'board', label: '计划板' },
  { key: 'batch', label: '计划批次' },
  { key: 'version', label: '物候口径' },
  { key: 'ledger', label: '绿化台账' },
  { key: 'todo', label: '巡查待办' },
  { key: 'schedule', label: '车辆排班' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('board')
const statusOptions = ['未开始', '进行中', '已调整', '已完成', '已取消', '已迁移']
const thresholdFields = ['修剪间隔天', '灌溉间隔天', '高温阈值', '暴雨阈值']
const categories = ['乔木', '灌木', '绿篱', '地被', '草坪']

const board = ref<{ total: number; days: { 日期: string; tasks: Task[] }[]; counts: Row; emergency: number; batches: Row[] }>({
  total: 0, days: [], counts: {}, emergency: 0, batches: [],
})
const ledger = ref<Row[]>([])
const todos = ref<Row[]>([])
const schedules = ref<Row[]>([])
const versions = ref<Row[]>([])
const currentVersion = ref<Row>({ version: 1, 生效日期: '', 调整说明: '', profiles: {} })
const draftProfiles = reactive<Record<string, Row>>({})
const versionRemark = ref('')
const ledgerFilter = ref('')
const scheduleDay = ref('')
const message = ref('')
const messageOk = ref(true)

const filters = reactive({ day: '', task_type: '', status: '', priority: '' })

const dialogTask = ref<Task | null>(null)
const dialogMode = ref<'adjust' | 'complete'>('adjust')
const dialogForm = reactive<Row>({})

function tip(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

async function post(path: string, body: Row = {}) {
  const response = await request(`${BASE}${path}`, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || `请求失败（${response.status}）`)
  }
  return payload
}

async function loadBoard() {
  const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v) as [string, string][]).toString()
  const response = await request(`${BASE}/board?${query}`)
  board.value = await response.json()
}

async function loadLedger() {
  const query = new URLSearchParams(ledgerFilter.value ? { record_type: ledgerFilter.value } : {}).toString()
  ledger.value = (await (await request(`${BASE}/ledger?${query}`)).json()).items ?? []
}

async function loadTodos() {
  todos.value = (await (await request(`${BASE}/todos`)).json()).items ?? []
}

async function loadSchedules() {
  const query = new URLSearchParams(scheduleDay.value ? { day: scheduleDay.value } : {}).toString()
  schedules.value = (await (await request(`${BASE}/vehicle-schedules?${query}`)).json()).items ?? []
}

async function loadVersions() {
  const payload = await (await request(`${BASE}/versions`)).json()
  versions.value = payload.items ?? []
  currentVersion.value = payload.current
  for (const cat of categories) {
    draftProfiles[cat] = { ...(currentVersion.value.profiles?.[cat] ?? {}) }
  }
}

async function switchTab(key: string) {
  activeTab.value = key as typeof activeTab.value
  message.value = ''
  if (key === 'board') await loadBoard()
  if (key === 'ledger') await loadLedger()
  if (key === 'todo') await loadTodos()
  if (key === 'schedule') await loadSchedules()
  if (key === 'version') await loadVersions()
}

function resetFilters() {
  filters.day = ''
  filters.task_type = ''
  filters.status = ''
  filters.priority = ''
  void loadBoard()
}

async function generate(simulateOutage = false) {
  try {
    const payload = await post('/generate', simulateOutage ? { fail_after_days: 3 } : {})
    tip(payload.message ?? '生成完成', payload.ok !== false)
  } catch (error) {
    tip(error instanceof Error ? error.message : '生成失败', false)
  }
  await loadBoard()
  if (activeTab.value === 'schedule') await loadSchedules()
}

async function resume(batchId: number) {
  try {
    const payload = await post(`/batches/${batchId}/resume`, {})
    tip(payload.message, payload.ok)
  } catch (error) {
    tip(error instanceof Error ? error.message : '续传失败', false)
  }
  await loadBoard()
}

async function adjustVersion() {
  try {
    const payload = await post('/versions/adjust', {
      profiles: JSON.parse(JSON.stringify(draftProfiles)),
      remark: versionRemark.value,
    })
    tip(payload.message, payload.ok)
    versionRemark.value = ''
    await loadVersions()
    await loadBoard()
  } catch (error) {
    tip(error instanceof Error ? error.message : '口径调整失败', false)
  }
}

function canComplete(t: Task) {
  return t.status === '未开始' || t.status === '已调整' || t.status === '进行中'
}

function openAdjust(t: Task) {
  dialogMode.value = 'adjust'
  dialogTask.value = t
  Object.assign(dialogForm, {
    实际日期: t.实际日期 ?? '',
    实际时段: t.实际时段 ?? t.计划时段,
    实际车辆编号: t.实际车辆编号 ?? '',
    实际班组: t.实际班组 ?? '',
    调整原因: t.调整原因 ?? '',
  })
}

function openComplete(t: Task) {
  dialogMode.value = 'complete'
  dialogTask.value = t
  Object.assign(dialogForm, {
    实际日期: t.实际日期 ?? todayStr(),
    实际时段: t.实际时段 ?? t.计划时段,
    实际车辆编号: t.实际车辆编号 ?? '',
    实际班组: t.实际班组 ?? '',
    完成结果: '',
  })
}

async function submitDialog() {
  if (!dialogTask.value) return
  const path = dialogMode.value === 'complete' ? 'complete' : 'adjust'
  const values: Row = {}
  for (const [key, value] of Object.entries(dialogForm)) {
    if (value !== '' && value !== null && value !== undefined) values[key] = value
  }
  try {
    const payload = await post(`/tasks/${dialogTask.value.id}/${path}`, values)
    if (!payload.ok) {
      tip(payload.message, false)
      return
    }
    tip(payload.message, true)
    dialogTask.value = null
    await Promise.all([loadBoard(), activeTab.value === 'ledger' ? loadLedger() : null, loadTodos(), loadSchedules()])
  } catch (error) {
    tip(error instanceof Error ? error.message : '提交失败', false)
  }
}

function slotOptions(type: string) {
  return type === '灌溉'
    ? ['06:00-08:00', '08:00-10:00', '16:00-18:00']
    : ['09:00-11:00', '14:00-16:00']
}

function statusClass(status: string) {
  if (status === '已完成') return 'badge-ok'
  if (status === '已取消' || status === '失败') return 'badge-muted'
  if (status === '已迁移' || status === '已重算') return 'badge-warn'
  if (status === '已调整') return 'badge-warn'
  if (status === '进行中') return 'badge-info'
  return 'badge-normal'
}

function batchClass(status: string) {
  if (status === '已发布') return 'badge-ok'
  if (status === '草稿') return 'badge-warn'
  if (status === '失败') return 'badge-muted'
  return 'badge-info'
}

function weekday(day: string) {
  return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][new Date(`${day}T00:00:00`).getDay()]
}

function snapshotText(snapshot: Row | undefined) {
  if (!snapshot) return ''
  return thresholdFields.map((f) => `${f.replace('天', '')}:${snapshot[f]}`).join(' / ')
}

function todayStr() {
  return new Date().toISOString().slice(0, 10)
}

onMounted(() => {
  void loadBoard()
})
</script>

<style scoped>
.tab-bar { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 12px; flex-wrap: wrap; }
.tab-item { border: none; background: none; padding: 8px 14px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; }
.tab-item.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.day-block { margin-bottom: 16px; }
.day-title { font-size: 14px; margin: 10px 0 6px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.badge-normal { background: #eef2ff; color: #3730a3; }
.badge-danger { background: #fee4e2; color: #b42318; }
.badge-ok { background: #dcfae6; color: #067647; }
.badge-warn { background: #fef0c7; color: #b54708; }
.badge-info { background: #e0f2fe; color: #075985; }
.badge-muted { background: #f2f4f7; color: #667085; }
.emergency-num { color: #b42318; }
.sub-text { color: var(--muted); font-size: 12px; }
.actual-text { color: #b54708; font-size: 12px; }
.baseline { color: #101828; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 16px; }
.num-input { width: 72px; }
.tip-line { font-size: 12px; color: var(--muted); margin: 8px 0; }
.tip-ok { color: #067647; }
.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal-box { background: #fff; border-radius: 10px; padding: 18px 20px; width: 620px; max-width: 92vw; }
.modal-box table { margin: 10px 0; }
.modal-box th { width: 110px; text-align: left; color: var(--muted); font-weight: normal; }
.modal-box input, .modal-box select { width: 100%; padding: 4px 8px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
</style>
