// 任务看板前端。只读 render_readme.py 生成的 docs/data/*.json，不做任何计算逻辑，
// 分档/排序的权威实现都在 Python 侧，这里只负责显示。
'use strict';

const KIND = { work: '作业', eval: '互评', exam: '考试' };
const STATE = { pending: '待完成', submitted: '已提交', done: '已完成', unknown: '未知' };
const BUCKETS = [
  ['overdue', '🔴 已逾期'],
  ['today', '🟠 今天截止'],
  ['week', '🟡 7 天内'],
  ['later', '⚪ 更远 / 无截止时间'],
];

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function load(name) {
  try {
    const r = await fetch(`data/${name}.json`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

function fmtRemaining(iso) {
  if (!iso) return '—';
  const ms = new Date(iso) - new Date();
  const abs = Math.abs(ms);
  const d = Math.floor(abs / 86400000);
  const h = Math.floor((abs % 86400000) / 3600000);
  const m = Math.floor((abs % 3600000) / 60000);
  const body = d ? `${d}天${h}小时` : h ? `${h}小时${m}分` : `${m}分`;
  return ms < 0 ? `已逾期${body}` : `剩余${body}`;
}

function fmtWhen(iso, exact) {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}${exact ? '' : ' ≈'}`;
}

function bucketOf(t, now) {
  if (!t.deadline) return 'later';
  const d = new Date(t.deadline);
  if (d < now) return 'overdue';
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  if (that.getTime() === today.getTime()) return 'today';
  if (d - now <= 7 * 86400000) return 'week';
  return 'later';
}

function taskTable(items) {
  if (!items.length) return '<p class="empty">无</p>';
  const rows = items.map((t) => `<tr>
    <td class="when">${esc(fmtWhen(t.deadline, t.deadline_exact))}</td>
    <td>${esc(t.course)}</td>
    <td>${t.url ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.title)}</a>` : esc(t.title)}</td>
    <td><span class="tag">${esc(KIND[t.kind] || t.kind)}</span></td>
    <td>${esc(STATE[t.state] || t.status)}</td>
    <td class="when">${esc(fmtRemaining(t.deadline))}</td>
  </tr>`).join('');
  return `<table><thead><tr><th>截止</th><th>课程</th><th>任务</th><th>类型</th><th>状态</th><th>倒计时</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderTasks(data) {
  const items = (data && data.items) || [];
  const now = new Date();
  const pending = items.filter((t) => t.state === 'pending');
  const groups = { overdue: [], today: [], week: [], later: [] };
  pending.forEach((t) => groups[bucketOf(t, now)].push(t));
  const byDeadline = (a, b) => (a.deadline ? new Date(a.deadline) : Infinity) - (b.deadline ? new Date(b.deadline) : Infinity);
  Object.values(groups).forEach((g) => g.sort(byDeadline));

  $('stats').innerHTML = [
    ['red', `逾期 ${groups.overdue.length}`],
    ['orange', `今天 ${groups.today.length}`],
    ['yellow', `7天内 ${groups.week.length}`],
    ['', `待完成 ${pending.length}`],
    ['', `总任务 ${items.length}`],
  ].map(([c, s]) => `<span class="chip ${c}">${esc(s)}</span>`).join('');

  $('tasks').innerHTML = BUCKETS
    .filter(([k]) => groups[k].length)
    .map(([k, label]) => `<div class="bucket ${k}"><h3>${label}（${groups[k].length}）</h3>${taskTable(groups[k])}</div>`)
    .join('') || '<p class="empty">当前没有待完成任务。</p>';
}

function renderNotices(data) {
  const items = ((data && data.items) || []).filter((n) => (n.score || 0) >= 3).slice(0, 15);
  if (!items.length) { $('notices').innerHTML = '<p class="empty">暂无高相关公告。</p>'; return; }
  $('notices').innerHTML = `<table><thead><tr><th>分</th><th>日期</th><th>公告</th><th>来源</th><th>截止</th></tr></thead><tbody>${
    items.map((n) => `<tr><td>${esc(n.score)}</td><td class="when">${esc(n.publish_date || '—')}</td>
      <td><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a></td>
      <td>${esc(n.source)}</td><td class="when">${esc(n.deadline || '—')}</td></tr>`).join('')
  }</tbody></table>`;
}

function renderPlans(data) {
  const items = ((data && data.items) || []).filter((p) => p.state === 'open').slice(0, 20);
  if (!items.length) { $('plans').innerHTML = '<p class="empty">暂无未完成计划。</p>'; return; }
  $('plans').innerHTML = `<table><thead><tr><th>计划</th><th>置信度</th><th>截止</th><th>来源</th></tr></thead><tbody>${
    items.map((p) => `<tr><td>${esc(p.text)}</td><td>${esc((p.confidence || 0).toFixed(1))}</td>
      <td class="when">${esc(p.due_date || '—')}</td><td>${esc(p.source_date || p.source_file)}</td></tr>`).join('')
  }</tbody></table>`;
}

function renderQuotes(data) {
  const items = ((data && data.items) || []).slice(0, 10);
  if (!items.length) { $('quotes').innerHTML = '<p class="empty">暂无摘录。</p>'; return; }
  $('quotes').innerHTML = items.map((q) =>
    `<blockquote>${esc(q.text)}<span class="ctx">${esc(q.context || '')}${q.source_date ? ' · ' + esc(q.source_date) : ''}</span></blockquote>`
  ).join('');
}

(async function main() {
  const [tasks, meta, notices, plans, quotes] = await Promise.all(
    ['tasks', 'meta', 'notices', 'plans', 'quotes'].map(load));
  $('meta').textContent = meta
    ? `最后更新 ${meta.updated_at || '未知'} · ${meta.week || ''}`
    : '数据尚未生成，先跑一次 Actions 的 finalize 模式。';
  renderTasks(tasks);
  renderNotices(notices);
  renderPlans(plans);
  renderQuotes(quotes);
})();