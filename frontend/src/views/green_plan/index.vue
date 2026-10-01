<template>
  <section class="page" data-module="green-plan">
    <header class="page-head">
      <div>
        <h2>修剪灌溉计划板</h2>
        <p class="page-desc">
          按物候周期滚动生成未来两周修剪 / 灌溉 / 病虫防治任务：串联区域品种、近期天气、巡查发现与作业车辆，
          高温暴雨以应急管制优先，事务发布后才占用车辆。
        </p>
      </div>
      <div class="page-actions">
        <label class="sim-check"><input type="checkbox" v-model="simulateFail" /> 模拟发布中断</label>
        <button class="btn ghost" type="button" @click="loadAll">重新枚举</button>
        <button class="btn primary" type="button" @click="onPublish">生成并发布两周计划</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card" v-for="card in summaryCards" :key="card.label">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value" :class="card.cls">{{ card.value }}</strong>
      </article>
    </div>

    <div class="banner">
      <span>口径版本：<b>{{ summary['口径版本'] }}</b></span>
      <span>滚动窗口：<b>{{ summary['窗口'] }}</b></span>
      <span>已发布批次：<b>{{ summary['已发布批次'] }}</b></span>
      <span>草稿批次：<b>{{ summary['草稿批次'] }}</b></span>
      <span class="banner-tip">提示：先「保留草稿」可在发布中断重连后续发，批次键重复调度只生效一次。</span>
    </div>

    <p v-if="message" class="result-msg" :class="messageOk ? 'ok' : 'err'">{{ message }}</p>

    <nav class="tabs">
      <button v-for="tab in tabs" :key="tab.key" type="button" class="tab"
              :class="{ active: activeTab === tab.key }" @click="activeTab = tab.key">
        {{ tab.label }}<em v-if="tab.count !== undefined">（{{ tab.count }}）</em>
      </button>
    </nav>

    <!-- 计划草稿（两周任务） -->
    <div v-show="activeTab === 'draft'">
      <div class="toolbar">
        <button class="btn" type="button" @click="onSaveDraft">保留草稿</button>
        <span class="muted">共 {{ draftTasks.length }} 条任务，按可行作业日与车辆容量滚动试排（暂不占用车辆）。</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>计划日期</th><th>区域</th><th>植物品种</th><th>作业类型</th><th>来源</th>
            <th>天气</th><th>应急管制</th><th>班组</th><th>建议车辆</th><th>紧急程度</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(t, i) in draftTasks" :key="i">
            <td>{{ t['计划日期'] }}<span v-if="t['到期日期'] !== t['计划日期']" class="muted"> /到期{{ t['到期日期'] }}</span></td>
            <td>{{ t['区域编号'] }} {{ t['区域名称'] }}</td>
            <td>{{ t['植物品种'] }}</td>
            <td><span class="tag" :class="opClass(t['作业类型'])">{{ t['作业类型'] }}</span></td>
            <td>{{ t['来源'] }}</td>
            <td>{{ t['天气'] }} {{ t['最高温'] }}℃ / {{ t['降水量'] }}mm</td>
            <td><span class="tag" :class="ctrlClass(t['应急管制'])">{{ t['应急管制'] }}</span></td>
            <td>{{ t['班组'] }}</td>
            <td>{{ t['建议车辆'] }}</td>
            <td>{{ t['紧急程度'] }}</td>
          </tr>
          <tr v-if="!draftTasks.length"><td colspan="10" class="empty-state">暂无草稿，请重新枚举或发布</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 绿化台账 -->
    <div v-show="activeTab === 'ledger'">
      <div class="toolbar">
        <select v-model="ledgerFilter">
          <option value="">全部状态</option>
          <option v-for="s in ['未开始', '进行中', '已完成', '已取消']" :key="s">{{ s }}</option>
        </select>
        <button class="btn" type="button" @click="loadLedger">刷新台账</button>
        <span class="muted">历史修剪 / 灌溉按「实际完成日期」留痕；已执行记录在口径调整后保留当时基准。</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>台账编号</th><th>区域</th><th>作业类型</th><th>计划日期</th><th>实际完成</th>
            <th>状态</th><th>口径版本</th><th>来源</th><th>车辆</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in ledgerRows" :key="r.id">
            <td>{{ r['台账编号'] }}</td>
            <td>{{ r['区域编号'] }} {{ r['区域名称'] }}</td>
            <td><span class="tag" :class="opClass(r['作业类型'])">{{ r['作业类型'] }}</span></td>
            <td>{{ r['计划日期'] }}</td>
            <td>{{ r['实际完成日期'] || '—' }}</td>
            <td><span class="tag" :class="statusClass(r['状态'])">{{ r['状态'] }}</span>
              <div v-if="r['取消说明']" class="muted small">{{ r['取消说明'] }}</div></td>
            <td>{{ r['口径版本'] }}</td>
            <td>{{ r['来源'] }}</td>
            <td>{{ r['车辆编号'] }}</td>
            <td>
              <button v-if="r['状态'] === '未开始'" class="link" type="button"
                      @click="onComplete(r)">现场完成</button>
              <span v-else class="muted">—</span>
            </td>
          </tr>
          <tr v-if="!ledgerRows.length"><td colspan="10" class="empty-state">暂无台账记录</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 车辆排班 -->
    <div v-show="activeTab === 'schedule'">
      <div class="toolbar"><span class="muted">每条已发布工单对应一条排班；事务提交后才占用车辆，回滚则释放，不重复占用。</span></div>
      <table class="data-table">
        <thead><tr><th>排班编号</th><th>日期</th><th>车辆</th><th>班次</th><th>用途</th><th>状态</th><th>批次键</th></tr></thead>
        <tbody>
          <tr v-for="s in scheduleRows" :key="s.id">
            <td>{{ s['排班编号'] }}</td>
            <td>{{ s['日期'] }}</td>
            <td>{{ s['车辆编号'] }}</td>
            <td>{{ s['班次'] }}</td>
            <td>{{ s['用途'] }}</td>
            <td><span class="tag" :class="schedClass(s['状态'])">{{ s['状态'] }}</span></td>
            <td class="muted small">{{ s['批次键'] || '—' }}</td>
          </tr>
          <tr v-if="!scheduleRows.length"><td colspan="7" class="empty-state">暂无排班，发布计划后生成</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 生成链路 -->
    <div v-show="activeTab === 'inputs'">
      <div class="chain-grid">
        <section class="chain-col">
          <h3>区域品种（{{ areas.length }}）</h3>
          <table class="data-table compact">
            <thead><tr><th>区域</th><th>品种/物候</th><th>上次修剪</th><th>上次灌溉</th></tr></thead>
            <tbody>
              <tr v-for="a in areas" :key="a.id">
                <td>{{ a['区域编号'] }}<div class="muted small">{{ a['区域名称'] }}</div></td>
                <td>{{ a['植物品种'] }}<div class="muted small">{{ a['物候类型'] }}</div></td>
                <td>{{ a['上次修剪日期'] }}</td><td>{{ a['上次灌溉日期'] }}</td>
              </tr>
            </tbody>
          </table>
        </section>
        <section class="chain-col">
          <h3>近期天气（{{ weather.length }}）</h3>
          <table class="data-table compact">
            <thead><tr><th>日期</th><th>天气</th><th>最高温</th><th>降水</th><th>管制</th></tr></thead>
            <tbody>
              <tr v-for="w in weather" :key="w.id">
                <td>{{ w['日期'] }}</td><td>{{ w['天气'] }}</td>
                <td :class="Number(w['最高温']) >= Number(caliber['高温阈值']) ? 'warn' : ''">{{ w['最高温'] }}℃</td>
                <td :class="Number(w['降水量']) >= Number(caliber['暴雨阈值']) ? 'warn' : ''">{{ w['降水量'] }}mm</td>
                <td><span class="tag" :class="ctrlClass(weatherControl(w))">{{ weatherControl(w) }}</span></td>
              </tr>
            </tbody>
          </table>
        </section>
        <section class="chain-col">
          <h3>巡查发现（{{ findings.length }}）</h3>
          <table class="data-table compact">
            <thead><tr><th>编号</th><th>区域</th><th>建议作业</th><th>紧急</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="f in findings" :key="f.id">
                <td>{{ f['发现编号'] }}<div class="muted small">{{ f['描述'] }}</div></td>
                <td>{{ f['区域编号'] }}</td><td>{{ f['建议作业'] }}</td>
                <td>{{ f['紧急程度'] }}</td>
                <td>{{ Number(f['是否已处理']) === 1 ? '已处理' : '待处理' }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </div>

    <!-- 物候口径与批次 -->
    <div v-show="activeTab === 'caliber'">
      <section class="panel">
        <h3>物候口径版本化</h3>
        <p class="muted">调整阈值生成新生效版本；随后「计划重排」会迁移未开始任务，已执行记录保留当时基准。</p>
        <form class="caliber-form" @submit.prevent="onAdjustCaliber">
          <label><span>高温阈值(℃)</span><input type="number" v-model.number="caliberForm['高温阈值']" /></label>
          <label><span>暴雨阈值(mm)</span><input type="number" v-model.number="caliberForm['暴雨阈值']" /></label>
          <label class="grow"><span>调整说明</span><input v-model="caliberForm['创建说明']" placeholder="如：入秋阈值调整" /></label>
          <button class="btn primary" type="submit">调整口径并生效</button>
          <button class="btn" type="button" @click="onRecalculate">计划重排（迁移未开始）</button>
        </form>
        <table class="data-table compact">
          <thead><tr><th>版本号</th><th>高温阈值</th><th>暴雨阈值</th><th>生效起始</th><th>是否生效</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="c in calibers" :key="c.id">
              <td>{{ c['版本号'] }}</td><td>{{ c['高温阈值'] }}</td><td>{{ c['暴雨阈值'] }}</td>
              <td>{{ c['生效起始'] }}</td>
              <td><span class="tag" :class="Number(c['是否生效']) === 1 ? 'ok' : ''">{{ Number(c['是否生效']) === 1 ? '生效中' : '历史版本' }}</span></td>
              <td class="muted small">{{ c['创建说明'] }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <h3>发布批次（幂等调度键）</h3>
        <table class="data-table compact">
          <thead><tr><th>批次键</th><th>窗口</th><th>口径版本</th><th>任务数</th><th>状态</th><th>创建时间</th><th>发布时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="b in batches" :key="b.id">
              <td class="small">{{ b['批次键'] }}</td>
              <td>{{ b['窗口起'] }} ~ {{ b['窗口止'] }}</td>
              <td>{{ b['口径版本'] }}</td><td>{{ b['任务数'] }}</td>
              <td><span class="tag" :class="b['状态'] === '已发布' ? 'ok' : 'draft'">{{ b['状态'] }}</span></td>
              <td class="muted small">{{ b['创建时间'] }}</td>
              <td class="muted small">{{ b['发布时间'] || '—' }}</td>
              <td>
                <button v-if="b['状态'] === '草稿'" class="link" type="button" @click="onResume(b['批次键'])">重连续发</button>
                <span v-else class="muted">已生效</span>
              </td>
            </tr>
            <tr v-if="!batches.length"><td colspan="8" class="empty-state">暂无批次</td></tr>
          </tbody>
        </table>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const API = '/api/green/plan'

const activeTab = ref<'draft' | 'ledger' | 'schedule' | 'inputs' | 'caliber'>('draft')
const simulateFail = ref(false)
const message = ref('')
const messageOk = ref(true)

const summary = ref<Row>({})
const draftTasks = ref<Row[]>([])
const ledgerRows = ref<Row[]>([])
const scheduleRows = ref<Row[]>([])
const areas = ref<Row[]>([])
const weather = ref<Row[]>([])
const findings = ref<Row[]>([])
const caliber = ref<Row>({})
const calibers = ref<Row[]>([])
const batches = ref<Row[]>([])
const ledgerFilter = ref('')

const caliberForm = reactive({ 高温阈值: 35, 暴雨阈值: 50, 创建说明: '' })

const tabs = computed(() => [
  { key: 'draft', label: '两周计划草稿', count: draftTasks.value.length },
  { key: 'ledger', label: '绿化台账', count: ledgerRows.value.length },
  { key: 'schedule', label: '车辆排班', count: scheduleRows.value.length },
  { key: 'inputs', label: '生成链路', count: areas.value.length + weather.value.length + findings.value.length },
  { key: 'caliber', label: '口径与批次', count: batches.value.length },
] as const)

const summaryCards = computed(() => [
  { label: '未开始', value: summary.value['未开始'] ?? 0, cls: '' },
  { label: '进行中', value: summary.value['进行中'] ?? 0, cls: '' },
  { label: '已完成', value: summary.value['已完成'] ?? 0, cls: 'ok-text' },
  { label: '已取消(重排迁移)', value: summary.value['已取消'] ?? 0, cls: 'muted-text' },
  { label: '高温/暴雨管制', value: summary.value['高温/暴雨管制'] ?? 0, cls: 'warn-text' },
  { label: '排班占用', value: summary.value['排班占用'] ?? 0, cls: '' },
  { label: '待派车', value: summary.value['待派车'] ?? 0, cls: 'warn-text' },
])

function flash(msg: string, ok = true) {
  message.value = msg
  messageOk.value = ok
}

async function getJson(path: string): Promise<any> {
  const resp = await request(path)
  if (!resp.ok) throw new Error(`接口返回 ${resp.status}`)
  return resp.json()
}

async function postJson(path: string, body: any): Promise<any> {
  const resp = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const data = await resp.json().catch(() => ({}))
  return { ok: resp.ok, data }
}

async function loadBoard() {
  const d = await getJson(`${API}`)
  summary.value = d.summary ?? {}
}

async function loadDraft() {
  const d = await getJson(`${API}/draft`)
  draftTasks.value = d['任务'] ?? []
}

async function loadInputs() {
  const d = await getJson(`${API}/inputs`)
  areas.value = d.areas ?? []
  weather.value = d.weather ?? []
  findings.value = d.findings ?? []
  caliber.value = d.caliber ?? {}
  caliberForm['高温阈值'] = Number(caliber.value['高温阈值'] ?? 35)
  caliberForm['暴雨阈值'] = Number(caliber.value['暴雨阈值'] ?? 50)
}

async function loadLedger() {
  const qs = ledgerFilter.value ? `?status=${encodeURIComponent(ledgerFilter.value)}` : ''
  const d = await getJson(`${API}/ledger${qs}`)
  ledgerRows.value = d.items ?? []
}

async function loadSchedule() {
  const d = await getJson(`${API}/schedule`)
  scheduleRows.value = d.items ?? []
}

async function loadCalibers() {
  const d = await getJson(`${API}/calibers`)
  calibers.value = d.items ?? []
  caliber.value = d.current ?? caliber.value
}

async function loadBatches() {
  const d = await getJson(`${API}/batches`)
  batches.value = d.items ?? []
}

async function loadAll() {
  message.value = ''
  try {
    await Promise.all([loadBoard(), loadDraft(), loadInputs(), loadLedger(), loadSchedule(), loadCalibers(), loadBatches()])
  } catch (e) {
    flash(e instanceof Error ? e.message : '计划板数据加载失败', false)
  }
}

async function onSaveDraft() {
  const { ok, data } = await postJson(`${API}/draft`, {})
  flash(data.message ?? (ok ? '草稿已保留' : '草稿保留失败'), ok && data.ok !== false)
  await Promise.all([loadDraft(), loadBatches(), loadBoard()])
}

async function onPublish() {
  const { data } = await postJson(`${API}/publish`, { force_fail: simulateFail.value })
  if (!data.ok) {
    flash(data.message ?? '发布中断已回滚，草稿保留，可重连续发', false)
  } else if (data['幂等命中']) {
    flash(data.message ?? '该批次已发布，重复调度只生效一次', true)
  } else {
    flash(`${data.message ?? '发布成功'}｜台账/待办/排班各 ${data['生成']?.['台账工单'] ?? 0} 条`, true)
  }
  await loadAll()
}

async function onResume(batchKey: string) {
  const { data } = await postJson(`${API}/publish`, { '批次键': batchKey })
  flash(data.message ?? (data.ok ? '续发成功' : '续发失败'), !!data.ok)
  await loadAll()
}

async function onComplete(row: Row) {
  const actual = window.prompt('请输入实际完成日期（YYYY-MM-DD）', '2026-10-01')
  if (actual === null) return
  const note = window.prompt('完成说明（可留空）', '现场按计划完成') ?? ''
  const { ok, data } = await postJson(`${API}/ledger/${row.id}/complete`, {
    '实际完成日期': actual, '完成说明': note,
  })
  if (!ok || data.ok === false) {
    flash(data.message ?? '现场完成填报失败', false)
  } else {
    flash(data.message ?? '已按实际完成时间留痕', true)
  }
  await loadAll()
}

async function onAdjustCaliber() {
  const { ok, data } = await postJson(`${API}/calibers`, {
    '高温阈值': caliberForm['高温阈值'],
    '暴雨阈值': caliberForm['暴雨阈值'],
    '创建说明': caliberForm['创建说明'],
  })
  if (!ok || data.ok === false) {
    flash(data.message ?? '口径调整失败', false)
  } else {
    flash(data.message ?? '新口径已生效，请执行计划重排', true)
    caliberForm['创建说明'] = ''
  }
  await Promise.all([loadCalibers(), loadInputs(), loadBoard(), loadDraft()])
}

async function onRecalculate() {
  const { data } = await postJson(`${API}/recalculate`, {})
  flash(data.message ?? '计划重排完成', !!data.ok)
  await loadAll()
}

function weatherControl(w: Row): string {
  if (Number(w['降水量']) >= Number(caliber.value['暴雨阈值'])) return '暴雨管制'
  if (Number(w['最高温']) >= Number(caliber.value['高温阈值'])) return '高温管制'
  return '正常'
}

function opClass(op: string): string {
  return op === '修剪' ? 'trim' : op === '灌溉' ? 'irr' : 'pest'
}
function ctrlClass(c: string): string {
  return c === '暴雨管制' ? 'rain' : c === '高温管制' ? 'heat' : 'ok'
}
function statusClass(s: string): string {
  return { '未开始': 'pending', '进行中': 'doing', '已完成': 'ok', '已取消': 'cancel' }[s] ?? ''
}
function schedClass(s: string): string {
  return { '已排班': 'ok', '已完成': 'ok', '待派车': 'heat', '已释放': 'cancel' }[s] ?? ''
}

onMounted(loadAll)
</script>

<style scoped>
.page-actions { display: flex; align-items: center; gap: 10px; }
.sim-check { font-size: 13px; color: #b42318; display: flex; align-items: center; gap: 4px; white-space: nowrap; }
.banner { display: flex; flex-wrap: wrap; gap: 18px; background: #fff; border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-bottom: 12px; align-items: center; }
.banner b { color: var(--brand); }
.banner-tip { color: var(--muted); font-size: 12px; }
.result-msg { padding: 8px 12px; border-radius: 6px; font-size: 13px; }
.result-msg.ok { background: #ecfdf3; color: #027a48; border: 1px solid #abefc6; }
.result-msg.err { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 12px; }
.tab { border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 13px; color: var(--muted);
  border-bottom: 2px solid transparent; }
.tab em { font-style: normal; color: var(--muted); }
.tab.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.warn { color: #b42318; font-weight: 600; }
.ok-text { color: #027a48; }
.muted-text { color: var(--muted); }
.warn-text { color: #b42318; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; border: 1px solid transparent; }
.tag.trim { background: #eff4ff; color: #1d4ed8; border-color: #c7d7fe; }
.tag.irr { background: #ecfdff; color: #0e70a0; border-color: #a5e8f3; }
.tag.pest { background: #fffaeb; color: #b54708; border-color: #fedf89; }
.tag.heat { background: #fef3f2; color: #b42318; border-color: #fda29b; }
.tag.rain { background: #eef4ff; color: #1849a9; border-color: #b2ccff; }
.tag.ok { background: #ecfdf3; color: #027a48; border-color: #abefc6; }
.tag.pending { background: #f2f4f7; color: #344054; border-color: #d0d5dd; }
.tag.doing { background: #fffaeb; color: #b54708; border-color: #fedf89; }
.tag.cancel { background: #f2f4f7; color: #98a2b3; border-color: #e4e7ec; }
.tag.draft { background: #f2f4f7; color: #475467; border-color: #d0d5dd; }
.chain-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.chain-col h3, .panel h3 { margin: 4px 0 8px; font-size: 14px; }
.data-table.compact th, .data-table.compact td { padding: 5px 8px; font-size: 12px; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 14px; }
.caliber-form { display: flex; gap: 12px; align-items: flex-end; margin-bottom: 12px; flex-wrap: wrap; }
.caliber-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.caliber-form label.grow { flex: 1; min-width: 200px; }
.caliber-form input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; min-width: 120px; }
select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
</style>
