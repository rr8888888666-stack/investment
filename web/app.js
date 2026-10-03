/* 锐思投研 · 前端
   所有数据来自本机服务 /api/*，页面内不硬编码任何行情数字。 */

var API = '';
var S = {};            // 全局状态
var charts = {};       // ECharts 实例

/* ---------------- 基础工具 ---------------- */

function $(id) { return document.getElementById(id); }

function api(path, opt) {
  return fetch(API + path, opt || {}).then(function (r) {
    return r.json().then(function (j) {
      if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
      return j;
    });
  });
}

function cls(v) {
  if (v === null || v === undefined || isNaN(v)) return 'flat';
  return v > 0 ? 'up' : (v < 0 ? 'down' : 'flat');
}

function nfmt(v, d) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  d = (d === undefined) ? 2 : d;
  // 二进制浮点会让 4.135 变成 4.1349999…，直接 toFixed 会显示成 4.13。
  // 金融场景下这种「差一分」的显示错误会被用户当成数据错误，所以做一次半进位修正。
  var f = Math.pow(10, d);
  var scaled = Number(v) * f;
  var r = Math.round(scaled + (scaled >= 0 ? 1e-9 : -1e-9)) / f;
  var s = Math.abs(r).toFixed(d);
  return (r < 0 ? '-' : '') + s;
}

function pct(v, d) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  var s = Number(v).toFixed(d === undefined ? 2 : d);
  return (v > 0 ? '+' : '') + s + '%';
}

function money(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  var a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(3) + ' 万亿';
  if (a >= 1e8) return (v / 1e8).toFixed(2) + ' 亿';
  if (a >= 1e4) return (v / 1e4).toFixed(2) + ' 万';
  return nfmt(v);
}

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function cell(v, d) {
  if (v === null || v === undefined) return '<span class="dim">—</span>';
  return nfmt(v, d);
}

function tag(text, k) { return '<span class="tag ' + (k || '') + '">' + esc(text) + '</span>'; }

function echarts_ready() { return typeof window.echarts !== 'undefined'; }

function chart(id, option) {
  if (!echarts_ready()) {
    var el = $(id);
    if (el && !el.dataset.warned) {
      el.dataset.warned = '1';
      el.className = 'empty';
      el.textContent = '图表库未加载（离线环境），数据见上方表格。';
    }
    return;
  }
  var el = $(id);
  if (!el) return;
  if (charts[id]) { charts[id].dispose(); }
  var c = echarts.init(el);
  c.setOption(option);
  charts[id] = c;
}

var AXIS_TEXT = '#8b949e';
function ax(extra) {
  var base = {
    axisLine: { lineStyle: { color: '#e3e7ed' } },
    axisLabel: { color: AXIS_TEXT, fontSize: 11 },
    splitLine: { lineStyle: { color: '#f0f2f5' } }
  };
  var out = {};
  for (var k in base) out[k] = base[k];
  for (var k2 in extra) out[k2] = extra[k2];
  return out;
}
function tip(extra) {
  var base = {
    trigger: 'axis',
    backgroundColor: 'rgba(255,255,255,.98)',
    borderColor: '#e3e7ed',
    textStyle: { color: '#151a21', fontSize: 12 },
    extraCssText: 'box-shadow:0 4px 16px rgba(16,24,40,.12);border-radius:8px;'
  };
  for (var k in extra) base[k] = extra[k];
  return base;
}

/* ---------------- 顶部 ---------------- */

function renderMeta(m) {
  $('p-asof').innerHTML = '<i></i>快照 · ' + (m.trade_date || '无数据');
  $('p-asof').title =
    '本页数据时点：' + (m.trade_date || '—') +
    '\n抓取时间：' + (m.fetched_between && m.fetched_between[1] ? m.fetched_between[1] : '—') +
    '\n\n这是同步快照，不是逐笔实时行情。页面在两次同步之间不会变化。';
  $('p-source').textContent = m.source || '数据源未知';
  var st = (m.sync && m.sync.status) || '—';
  var el = $('p-sync');
  el.textContent = '同步 ' + st + '（成功 ' + ((m.sync && m.sync.ok_count) || 0) +
    ' / 失败 ' + ((m.sync && m.sync.fail_count) || 0) + '）';
  el.className = 'pill ' + (st === 'ok' ? 'ok' : (st === 'failed' ? 'bad' : ''));
  $('dash-asof').textContent = '数据时点 ' + (m.trade_date || '—') +
    '　生成于 ' + (m.generated_at || '—');
  $('stock-asof').textContent = '数据时点 ' + (m.trade_date || '—');
}

/* ---------------- 模块一：市场总览 ---------------- */

function renderDash(d) {
  // 指数卡片
  var html = '';
  (d.indices || []).forEach(function (i) {
    html += '<div class="metric">' +
      '<div class="m-name"><span>' + esc(i.name) + '</span><span class="m-code">' + esc(i.code) + '</span></div>' +
      '<div class="m-val ' + cls(i.chg_pct) + '">' + nfmt(i.close, 2) + '</div>' +
      '<div class="m-row"><span class="' + cls(i.chg_pct) + '">' + pct(i.chg_pct) + '</span>' +
      '<span>额 ' + money(i.amount) + '</span>' +
      (i.turnover !== null && i.turnover !== undefined ? '<span>换手 ' + nfmt(i.turnover, 2) + '%</span>' : '') +
      '</div></div>';
  });
  $('idx-cards').innerHTML = html || '<div class="empty">无指数数据</div>';

  // 两市成交
  var bb = '';
  (d.breadth || []).forEach(function (b) {
    bb += '<div class="kv"><div class="item"><div class="k">' + esc(b.market) + '</div>' +
      '<div class="v">' + money(b.amount) + '</div>' +
      '<div class="k" style="margin-top:4px">较昨日 ' +
      (b.amount_chg === null || b.amount_chg === undefined
        ? '<span class="dim">未提供</span>'
        : '<span class="' + cls(b.amount_chg) + '">' + (b.amount_chg > 0 ? '+' : '') + money(b.amount_chg) + '</span>') +
      '</div></div></div>';
  });
  $('breadth-box').innerHTML = bb ? '<div class="kv">' + bb + '</div>' : '<div class="empty">无数据</div>';
  var hasUp = (d.breadth || []).some(function (b) { return b.up_count !== null && b.up_count !== undefined; });
  $('breadth-hint').textContent = hasUp ? '' : '涨跌家数：上游接口未提供';

  // 行业表
  var sec = d.sectors || [];
  var t = '<thead><tr><th class="no-sort">板块</th><th class="no-sort">涨跌幅</th>' +
    '<th class="no-sort">成交额</th><th class="no-sort">换手</th><th class="no-sort">涨股比</th>' +
    '<th class="no-sort">主力净流入</th><th class="no-sort">5日</th><th class="no-sort">20日</th>' +
    '<th class="no-sort">领涨股</th></tr></thead><tbody>';
  sec.forEach(function (s) {
    t += '<tr><td>' + esc(s.sector_name) + '</td>' +
      '<td class="' + cls(s.chg_pct) + '">' + pct(s.chg_pct) + '</td>' +
      '<td>' + (s.amount === null || s.amount === undefined ? '<span class="dim">—</span>' : nfmt(s.amount / 1e4, 0) + ' 亿') + '</td>' +
      '<td>' + cell(s.turnover) + '</td>' +
      '<td>' + (s.up_ratio === null || s.up_ratio === undefined ? '<span class="dim">—</span>' : nfmt(s.up_ratio, 0) + ' 家涨') + '</td>' +
      '<td class="' + cls(s.main_net_in) + '">' + (s.main_net_in === null || s.main_net_in === undefined ? '<span class="dim">—</span>' : (s.main_net_in > 0 ? '+' : '') + nfmt(s.main_net_in / 1e4, 2) + ' 亿') + '</td>' +
      '<td class="' + cls(s.main_net_in_5d) + '">' + (s.main_net_in_5d === null || s.main_net_in_5d === undefined ? '<span class="dim">—</span>' : nfmt(s.main_net_in_5d / 1e4, 0) + ' 亿') + '</td>' +
      '<td class="' + cls(s.main_net_in_20d) + '">' + (s.main_net_in_20d === null || s.main_net_in_20d === undefined ? '<span class="dim">—</span>' : nfmt(s.main_net_in_20d / 1e4, 0) + ' 亿') + '</td>' +
      '<td>' + (s.lead_name ? esc(s.lead_name) + ' <span class="' + cls(s.lead_chg_pct) + '">' + pct(s.lead_chg_pct, 1) + '</span>' : '<span class="dim">—</span>') + '</td>' +
      '</tr>';
  });
  $('sector-box').innerHTML = '<div class="tbl-wrap"><table>' + t + '</tbody></table></div>';

  // 行业图
  var names = sec.map(function (s) { return s.sector_name; });
  var chg = sec.map(function (s) { return s.chg_pct; });
  var flow = sec.map(function (s) {
    return s.main_net_in === null || s.main_net_in === undefined ? null : +(s.main_net_in / 1e4).toFixed(2);
  });
  chart('c-sector', {
    grid: { left: 8, right: 8, top: 34, bottom: 8, containLabel: true },
    tooltip: tip({}),
    legend: { data: ['涨跌幅(%)', '主力净流入(亿)'], top: 0, textStyle: { fontSize: 11, color: AXIS_TEXT } },
    xAxis: ax({ type: 'category', data: names, axisLabel: { color: AXIS_TEXT, fontSize: 10.5, rotate: 30 } }),
    yAxis: [ax({ type: 'value', name: '%', nameTextStyle: { fontSize: 10 } }),
            ax({ type: 'value', name: '亿', nameTextStyle: { fontSize: 10 }, splitLine: { show: false } })],
    series: [
      {
        name: '涨跌幅(%)', type: 'bar', data: chg, barMaxWidth: 26,
        itemStyle: { color: function (p) { return p.value >= 0 ? '#d92b2b' : '#0f9960'; } },
        label: { show: true, position: 'top', fontSize: 10, color: AXIS_TEXT }
      },
      { name: '主力净流入(亿)', type: 'line', yAxisIndex: 1, data: flow, symbolSize: 7,
        itemStyle: { color: '#185fa5' }, lineStyle: { width: 2 } }
    ]
  });

  // 全池个股
  S.pool = d.stocks || [];
  renderPool();
  $('stockpool-hint').textContent = '共 ' + S.pool.length + ' 只（重点研究池 + 排行榜标的）';
}

function renderPool() {
  var rows = S.pool || [];
  var t = '<thead><tr><th>名称</th><th>代码</th><th>数据</th><th>收盘</th><th>涨跌幅</th>' +
    '<th>成交额</th><th>换手%</th><th>量比</th><th>总市值(亿)</th>' +
    '<th>PE(扣非TTM)</th><th>PB</th><th>股息率</th><th>主力净流入</th></tr></thead><tbody>';
  rows.forEach(function (s) {
    var depthTag = s.depth === 'full' ? tag('重点池', 'g')
      : (s.depth === 'quote+valuation' ? tag('部分', 'a') : tag('仅行情', ''));
    t += '<tr><td>' + esc(s.name) + '</td><td class="dim">' + esc(s.code) + '</td>' +
      '<td>' + depthTag + '</td>' +
      '<td>' + cell(s.close) + '</td>' +
      '<td class="' + cls(s.chg_pct) + '">' + pct(s.chg_pct) + '</td>' +
      '<td>' + (s.amount === null || s.amount === undefined ? '<span class="dim">—</span>' : money(s.amount)) + '</td>' +
      '<td>' + cell(s.turnover) + '</td><td>' + cell(s.vol_ratio) + '</td>' +
      '<td>' + cell(s.mcap) + '</td>' +
      '<td>' + cell(s.pe_ttm) + '</td>' +
      '<td>' + (s.pb === null || s.pb === undefined ? '<span class="dim">—</span>' : nfmt(s.pb, 3)) + '</td>' +
      '<td>' + (s.dividend_yield === null || s.dividend_yield === undefined ? '<span class="dim">—</span>' : nfmt(s.dividend_yield, 2) + '%') + '</td>' +
      '<td class="' + cls(s.main_net_in) + '">' + (s.main_net_in === null || s.main_net_in === undefined ? '<span class="dim">—</span>' : (s.main_net_in > 0 ? '+' : '') + money(s.main_net_in)) + '</td>' +
      '</tr>';
  });
  $('pool-tbl').innerHTML = t + '</tbody>';
}

/* ---------------- 模块二：个股深度 ---------------- */

function renderStockChips() {
  // 后端已把重点研究池排在前面。再分两组渲染，让「有完整数据」和「只有行情」不会混淆。
  var full = (S.pool || []).filter(function (s) { return s.depth === 'full'; });
  var thin = (S.pool || []).filter(function (s) { return s.depth !== 'full'; });
  var html = '';
  full.forEach(function (s, i) {
    html += '<button class="chip' + (i === 0 ? ' on' : '') + '" data-code="' + esc(s.code) + '">' +
      esc(s.name) + '<span class="hint" style="margin-left:6px">' + esc(s.code) + '</span></button>';
  });
  if (thin.length) {
    html += '<span class="hint" style="align-self:center;margin:0 4px">仅有行情：</span>';
    thin.forEach(function (s) {
      html += '<button class="chip" data-code="' + esc(s.code) + '" style="opacity:.72">' +
        esc(s.name) + '<span class="hint" style="margin-left:6px">' + esc(s.code) + '</span></button>';
    });
  }
  $('stock-chips').innerHTML = html;
  Array.prototype.forEach.call($('stock-chips').querySelectorAll('.chip'), function (b) {
    b.onclick = function () {
      Array.prototype.forEach.call($('stock-chips').querySelectorAll('.chip'), function (x) { x.classList.remove('on'); });
      b.classList.add('on');
      loadStock(b.dataset.code);
    };
  });
}

function loadStock(code) {
  $('stock-body').innerHTML = '<div class="empty">加载中…</div>';
  api('/api/stock?code=' + encodeURIComponent(code)).then(function (d) {
    renderStock(d);
  }).catch(function (e) {
    $('stock-body').innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>';
  });
}

function renderStock(d) {
  var q = d.quote || {}, v = d.valuation || {}, mc = d.market_cap || {};
  var latest = v.latest || {};
  var w = v.window || {};
  var h = '';
  h += '<div class="card"><div class="dhead">' +
    '<span class="nm">' + esc(d.name) + '</span><span class="cd">' + esc(d.code) + '</span>' +
    (d.in_watchlist ? tag('已在自选', 'b') : '') +
    (d.focus ? tag('重点研究池', 'g') : tag('榜单标的', 'a')) +
    '<span class="px ' + cls(q.chg_pct) + '">' + nfmt(q.close) +
    ' <span style="font-size:15px">' + pct(q.chg_pct) + '</span></span></div>';

  if (d.depth === 'quote_only') {
    h += '<div class="note" style="margin-bottom:14px">' +
      '<b>该标的目前只有行情数据。</b>估值、资金流、财务接口都是单标的粒度，' +
      '无法在全市场批量取，所以榜单里顺手带进来的标的不会自动补齐这些字段。' +
      '点下方「加入自选」会<b>立即为它抓取完整数据</b>，之后这里就会填满。' +
      '界面不会用别处口径的数字来假装它完整。</div>';
  }
  if (!d.in_watchlist) {
    h += '<div style="margin-bottom:14px"><button class="btn primary" id="btn-detail-watch">加入自选并补齐数据</button>' +
      '<span class="hint" id="detail-hint" style="margin-left:10px"></span></div>';
  }

  var items = [
    ['开盘', nfmt(q.open)], ['最高', nfmt(q.high)], ['最低', nfmt(q.low)],
    ['前收', nfmt(q.prev_close)], ['振幅', q.amplitude !== null && q.amplitude !== undefined ? nfmt(q.amplitude) + '%' : null],
    ['成交额', money(q.amount)], ['成交量', q.volume === null || q.volume === undefined ? null : nfmt(q.volume / 1e8, 2) + ' 亿股'],
    ['换手率', q.turnover !== null && q.turnover !== undefined ? nfmt(q.turnover) + '%' : null],
    ['量比', nfmt(q.vol_ratio)],
    ['总市值', mc.mcap !== null && mc.mcap !== undefined ? nfmt(mc.mcap) + ' 亿' : null],
    ['流通市值', mc.float_mcap !== null && mc.float_mcap !== undefined ? nfmt(mc.float_mcap) + ' 亿' : null]
  ];
  h += '<div class="kv">';
  items.forEach(function (it) {
    h += '<div class="item"><div class="k">' + it[0] + '</div><div class="v">' +
      (it[1] === null || it[1] === undefined ? '<span class="dim">—</span>' : it[1]) + '</div></div>';
  });
  h += '</div></div>';

  // 估值
  h += '<div class="grid2">';
  h += '<div class="card"><div class="card-h">估值水平<span class="hint">' + esc(w.label || '') +
    '（' + esc(w.from || '—') + ' ~ ' + esc(w.to || '—') + '）</span></div>';
  h += '<div class="kv">' +
    '<div class="item"><div class="k">PE（扣非后滚动）</div><div class="v">' + nfmt(latest.pe_ttm) + '</div></div>' +
    '<div class="item"><div class="k">PE（动态）</div><div class="v">' + nfmt(latest.pe_dynamic) + '</div></div>' +
    '<div class="item"><div class="k">PB</div><div class="v">' + (latest.pb === null || latest.pb === undefined ? '<span class="dim">—</span>' : nfmt(latest.pb, 3)) + '</div></div>' +
    '<div class="item"><div class="k">股息率（TTM，推算）</div><div class="v">' +
    (latest.dividend_yield === null || latest.dividend_yield === undefined ? '<span class="dim">—</span>' : nfmt(latest.dividend_yield) + '%') + '</div></div>' +
    '</div>';
  if (v.pe_percentile !== null && v.pe_percentile !== undefined) {
    h += '<div style="margin-top:12px"><div class="k" style="font-size:11.5px;color:var(--text-2)">' +
      '当前 PE 在库内序列中的分位：<b>' + nfmt(v.pe_percentile, 1) + '%</b>' +
      '<span class="hint">　注意：样本仅 ' + (w.points || 0) + ' 个交易日，不等于长周期分位</span></div>' +
      '<div class="bar"><i class="' + (v.pe_percentile > 80 ? 'hot' : '') + '" style="width:' + Math.min(v.pe_percentile, 100) + '%"></i></div></div>';
  }
  h += '</div>';

  // 资金流
  var ff = (d.fund_flow && d.fund_flow.recent) || [];
  h += '<div class="card"><div class="card-h">资金流向<span class="hint">近期 ' + ff.length + ' 个交易日</span></div>';
  if (ff.length) {
    h += '<div class="kv">';
    ff.slice(0, 6).forEach(function (f) {
      h += '<div class="item"><div class="k">' + esc(f.trade_date) + '</div><div class="v ' + cls(f.main_net_in) +
        '" style="font-size:13.5px">' + (f.main_net_in > 0 ? '+' : '') + money(f.main_net_in) + '</div>' +
        '<div class="k">主力净流入</div></div>';
    });
    h += '</div>';
  } else { h += '<div class="empty">该标的尚无资金流数据</div>'; }
  h += '</div></div>';

  // 财务
  var fin = d.financial || [];
  h += '<div class="card"><div class="card-h">财务数据<span class="hint">来源：财报披露（H+1）</span></div>';
  if (fin.length) {
    var t = '<thead><tr><th class="no-sort">报告期</th><th class="no-sort">发布日期</th>' +
      '<th class="no-sort">营业总收入</th><th class="no-sort">净利润</th><th class="no-sort">归母净利润</th>' +
      '<th class="no-sort">每股收益</th></tr></thead><tbody>';
    fin.forEach(function (f) {
      t += '<tr><td>' + esc(f.report_period) + '</td><td class="dim">' + esc(f.publish_date || '—') + '</td>' +
        '<td>' + (f.revenue === null || f.revenue === undefined ? '<span class="dim">—</span>' : money(f.revenue)) + '</td>' +
        '<td>' + (f.net_profit === null || f.net_profit === undefined ? '<span class="dim">—</span>' : money(f.net_profit)) + '</td>' +
        '<td>' + (f.net_profit_attr === null || f.net_profit_attr === undefined ? '<span class="dim">—</span>' : money(f.net_profit_attr)) + '</td>' +
        '<td>' + cell(f.eps) + '</td></tr>';
    });
    h += '<div class="tbl-wrap"><table>' + t + '</tbody></table></div>';
  } else { h += '<div class="empty">该标的尚无财务数据</div>'; }
  h += '</div>';

  // 估值序列图
  var hist = v.history || [];
  h += '<div class="card"><div class="card-h">PE 估值序列<span class="hint">库内 ' + hist.length + ' 个交易日</span></div>' +
    '<div class="chart" id="c-pe" style="height:240px"></div></div>';

  $('stock-body').innerHTML = h;

  var wb = $('btn-detail-watch');
  if (wb) {
    wb.onclick = function () {
      wb.disabled = true;
      wb.textContent = '抓取中…';
      api('/api/watchlist', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'add', code: d.code, name: d.name })
      }).then(function () {
        $('detail-hint').textContent = '已补齐，正在刷新…';
        return Promise.all([api('/api/stocks'), api('/api/stocks')]);
      }).then(function (r) {
        S.pool = r[0].items;
        renderStockChips();
        renderPool();
        // 选中刚补齐的那只，重新拉详情，指标就填满了
        Array.prototype.forEach.call($('stock-chips').querySelectorAll('.chip'), function (x) {
          x.classList.toggle('on', x.dataset.code === d.code);
        });
        loadStock(d.code);
      }).catch(function (e) {
        wb.disabled = false;
        wb.textContent = '加入自选并补齐数据';
        $('detail-hint').textContent = '失败：' + e.message;
      });
    };
  }

  chart('c-pe', {
    grid: { left: 8, right: 14, top: 20, bottom: 8, containLabel: true },
    tooltip: tip({}),
    xAxis: ax({ type: 'category', data: hist.map(function (x) { return x.date; }),
      axisLabel: { color: AXIS_TEXT, fontSize: 10, rotate: 34 } }),
    yAxis: ax({ type: 'value', scale: true }),
    series: [{
      type: 'line', data: hist.map(function (x) { return x.pe_ttm; }),
      smooth: true, symbolSize: 5, itemStyle: { color: '#185fa5' },
      areaStyle: { color: 'rgba(24,95,165,.10)' }
    }]
  });
}

/* ---------------- 模块三：选股器 ---------------- */

var FILTER_DEFS = [
  { k: 'min_chg', label: '涨跌幅下限 (%)', ph: '如 0' },
  { k: 'max_chg', label: '涨跌幅上限 (%)', ph: '如 10' },
  { k: 'min_mcap', label: '总市值下限 (亿)', ph: '如 500' },
  { k: 'min_turnover', label: '换手率下限 (%)', ph: '如 1' },
  { k: 'min_pe', label: 'PE 下限', ph: '如 0' },
  { k: 'max_pe', label: 'PE 上限', ph: '如 20' },
  { k: 'max_pb', label: 'PB 上限', ph: '如 1' },
  { k: 'min_dy', label: '股息率下限 (%)', ph: '如 4' }
];

function renderFilters() {
  var h = '';
  FILTER_DEFS.forEach(function (f) {
    h += '<label>' + f.label + '<input id="f-' + f.k + '" placeholder="' + f.ph + '"></label>';
  });
  $('filters').innerHTML = h;
}

function runScreen() {
  var qs = [];
  FILTER_DEFS.forEach(function (f) {
    var v = ($('f-' + f.k).value || '').trim();
    if (v !== '') qs.push(f.k + '=' + encodeURIComponent(v));
  });
  var req = $('f-req-val');
  if (req && req.checked) qs.push('require_valuation=1');
  api('/api/screener?' + qs.join('&')).then(function (d) {
    var c = d.coverage || {};
    $('coverage-hint').innerHTML = '样本 ' + d.universe + ' 只，命中 ' + d.selected + ' 只。' +
      '<span class="tag a">估值覆盖 ' + (c.valuation_fields ? c.valuation_fields.available : 0) + '/' + d.universe + '</span>';
    $('screen-count').textContent = '共 ' + d.selected + ' 只';
    S.screenNote = (c.note || '');
    var t = '<thead><tr><th>名称</th><th>代码</th><th>数据</th><th>涨跌幅</th><th>总市值(亿)</th>' +
      '<th>换手%</th><th>量比</th><th>PE</th><th>PB</th><th>股息率</th><th>主力净流入</th>' +
      '<th class="no-sort">操作</th></tr></thead><tbody>';
    (d.items || []).forEach(function (s) {
      var dtag = s.depth === 'full' ? tag('完整', 'g')
        : (s.depth === 'quote+valuation' ? tag('部分', 'a') : tag('仅行情', ''));
      t += '<tr><td>' + esc(s.name) + '</td><td class="dim">' + esc(s.code) + '</td>' +
        '<td>' + dtag + '</td>' +
        '<td class="' + cls(s.chg_pct) + '">' + pct(s.chg_pct) + '</td>' +
        '<td>' + cell(s.mcap) + '</td><td>' + cell(s.turnover, 2) + '</td><td>' + cell(s.vol_ratio) + '</td>' +
        '<td>' + cell(s.pe_ttm) + '</td>' +
        '<td>' + (s.pb === null || s.pb === undefined ? '<span class="dim">—</span>' : nfmt(s.pb, 3)) + '</td>' +
        '<td>' + (s.dividend_yield === null || s.dividend_yield === undefined ? '<span class="dim">—</span>' : nfmt(s.dividend_yield) + '%') + '</td>' +
        '<td class="' + cls(s.main_net_in) + '">' + (s.main_net_in === null || s.main_net_in === undefined ? '<span class="dim">—</span>' : money(s.main_net_in)) + '</td>' +
        '<td><button class="btn mini" data-add="' + esc(s.code) + '" data-nm="' + esc(s.name) + '">加自选</button></td>' +
        '</tr>';
    });
    $('screen-tbl').innerHTML = t + '</tbody>';
    Array.prototype.forEach.call($('screen-tbl').querySelectorAll('[data-add]'), function (b) {
      b.onclick = function () { addWatch(b.dataset.add, b.dataset.nm, b); };
    });
  }).catch(function (e) {
    $('screen-tbl').innerHTML = '<tbody><tr><td class="empty">筛选失败：' + esc(e.message) + '</td></tr></tbody>';
  });
}

/* ---------------- 模块四：持仓与自选 ---------------- */

function addWatch(code, name, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '抓取中…'; }
  api('/api/watchlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'add', code: code, name: name })
  }).then(function () { return loadFolio(); })
    .then(function () { if (btn) { btn.textContent = '已加入'; } })
    .catch(function (e) { if (btn) { btn.textContent = '失败'; } $('add-hint').textContent = e.message; });
}

function renderFolio(d) {
  var pos = d.positions || [];
  $('folio-total').textContent = pos.length
    ? '共 ' + d.count + ' 只，总市值 ' + nfmt(d.total_market_value) + ' 元'
    : '（尚未录入持仓）';

  var t = '<thead><tr><th>名称</th><th>代码</th><th>持股数</th><th>成本价</th><th>现价</th>' +
    '<th>涨跌幅</th><th>市值</th><th>盈亏</th><th>盈亏%</th><th>权重</th>' +
    '<th class="no-sort">操作</th></tr></thead><tbody>';
  pos.forEach(function (p) {
    t += '<tr><td>' + esc(p.name) + '</td><td class="dim">' + esc(p.code) + '</td>' +
      '<td>' + nfmt(p.shares, 0) + '</td>' +
      '<td>' + (p.cost === null || p.cost === undefined ? '<span class="dim">—</span>' : nfmt(p.cost, 3)) + '</td>' +
      '<td>' + (p.current_price === null || p.current_price === undefined ? '<span class="dim">—</span>' : nfmt(p.current_price)) + '</td>' +
      '<td class="' + cls(p.chg_pct) + '">' + pct(p.chg_pct) + '</td>' +
      '<td>' + nfmt(p.market_value) + '</td>' +
      '<td class="' + cls(p.pnl) + '">' + (p.pnl === null || p.pnl === undefined ? '<span class="dim">—</span>' : (p.pnl > 0 ? '+' : '') + nfmt(p.pnl)) + '</td>' +
      '<td class="' + cls(p.pnl_pct) + '">' + (p.pnl_pct === null || p.pnl_pct === undefined ? '<span class="dim">—</span>' : pct(p.pnl_pct)) + '</td>' +
      '<td>' + (p.weight_pct === null || p.weight_pct === undefined ? '<span class="dim">—</span>' : nfmt(p.weight_pct) + '%') + '</td>' +
      '<td><button class="btn mini" data-del="' + esc(p.code) + '">删除</button></td></tr>';
  });
  $('folio-tbl').innerHTML = t + '</tbody>';
  Array.prototype.forEach.call($('folio-tbl').querySelectorAll('[data-del]'), function (b) {
    b.onclick = function () { removePos(b.dataset.del); };
  });

  // 权重饼图
  if (pos.length) {
    chart('c-expo', {
      tooltip: tip({ trigger: 'item', formatter: '{b}<br/>权重 {d}%' }),
      series: [{
        type: 'pie', radius: ['42%', '68%'], center: ['50%', '52%'],
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { fontSize: 11, color: '#4b5563', formatter: '{b} {d}%' },
        data: pos.map(function (p, i) {
          return { name: p.name, value: +(p.market_value).toFixed(2) };
        })
      }]
    });
  }

  // 风险检查
  renderRisk(pos, d.total_market_value);
}

function renderRisk(pos, total) {
  var out = [];
  if (!pos.length) {
    $('risk-box').innerHTML = '<div class="empty">录入持仓后自动计算集中度与风险提示</div>';
    return;
  }
  pos.forEach(function (p) {
    if (p.weight_pct !== null && p.weight_pct > 30) {
      out.push(['high', p.name + ' 权重 ' + nfmt(p.weight_pct) + '% 超过 30% 单票上限']);
    } else if (p.weight_pct !== null && p.weight_pct > 15) {
      out.push(['mid', p.name + ' 权重 ' + nfmt(p.weight_pct) + '% 偏集中（参考上限 15%）']);
    }
    if (p.pb !== null && p.pb !== undefined) {
      if (p.pb > 10) out.push(['high', p.name + ' PB ' + nfmt(p.pb, 2) + '，估值处于高位区间']);
      else if (p.pb < 1) out.push(['ok', p.name + ' PB ' + nfmt(p.pb, 3) + '，处于破净状态']);
    }
    if (p.pe_ttm !== null && p.pe_ttm !== undefined && p.pe_ttm > 45) {
      out.push(['mid', p.name + ' PE ' + nfmt(p.pe_ttm, 1) + '，隐含较高增长预期']);
    }
    if (p.chg_pct !== null && p.chg_pct !== undefined && p.chg_pct < -3) {
      out.push(['mid', p.name + ' 今日跌幅 ' + pct(p.chg_pct) + '，注意波动']);
    }
  });
  var maxW = Math.max.apply(null, pos.map(function (p) { return p.weight_pct || 0; }));
  if (maxW > 30) out.unshift(['high', '最大单一持仓权重 ' + nfmt(maxW) + '%，组合集中度偏高']);
  if (!out.some(function (o) { return o[0] !== 'ok'; })) {
    out.push(['ok', '未触发当前阈值下的风险提示（阈值：单票 15% / 30%，PE 45，PB 1 与 10）']);
  }
  var lvName = { high: '高', mid: '中', ok: '正常' };
  var h = '<ul class="risk" style="margin:0;padding-left:18px">';
  out.forEach(function (o) {
    h += '<li><span class="lv ' + o[0] + '">' + lvName[o[0]] + '</span>' + esc(o[1]) + '</li>';
  });
  h += '</ul>';
  h += '<div class="note" style="margin-top:12px">以上为按固定阈值机械计算的结果，' +
    '仅用于演示风控模块的运作方式，<b>不构成任何投资建议</b>。阈值可在 server.py 中调整。</div>';
  $('risk-box').innerHTML = h;
}

function renderWatch(d) {
  var t = '<thead><tr><th>名称</th><th>代码</th><th>收盘</th><th>涨跌幅</th><th>换手%</th>' +
    '<th>PE</th><th>PB</th><th>股息率</th><th>加入时间</th><th class="no-sort">操作</th></tr></thead><tbody>';
  (d.items || []).forEach(function (w) {
    t += '<tr><td>' + esc(w.name) + '</td><td class="dim">' + esc(w.code) + '</td>' +
      '<td>' + cell(w.close) + '</td>' +
      '<td class="' + cls(w.chg_pct) + '">' + pct(w.chg_pct) + '</td>' +
      '<td>' + cell(w.turnover, 2) + '</td>' +
      '<td>' + cell(w.pe_ttm) + '</td>' +
      '<td>' + (w.pb === null || w.pb === undefined ? '<span class="dim">—</span>' : nfmt(w.pb, 3)) + '</td>' +
      '<td>' + (w.dividend_yield === null || w.dividend_yield === undefined ? '<span class="dim">—</span>' : nfmt(w.dividend_yield) + '%') + '</td>' +
      '<td class="dim">' + esc((w.added_at || '').slice(0, 16)) + '</td>' +
      '<td><button class="btn mini" data-wdel="' + esc(w.code) + '">移除</button></td></tr>';
  });
  if (!(d.items || []).length) {
    t += '<tr><td colspan="10" class="empty">自选池为空。在上方输入代码加入，或在选股器结果里点「加自选」。</td></tr>';
  }
  $('watch-tbl').innerHTML = t + '</tbody>';
  Array.prototype.forEach.call($('watch-tbl').querySelectorAll('[data-wdel]'), function (b) {
    b.onclick = function () {
      api('/api/watchlist', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'remove', code: b.dataset.wdel })
      }).then(loadFolio);
    };
  });
}

function removePos(code) {
  api('/api/portfolio', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'remove', code: code })
  }).then(loadFolio);
}

function loadFolio() {
  return Promise.all([api('/api/portfolio'), api('/api/watchlist')]).then(function (r) {
    renderFolio(r[0]);
    renderWatch(r[1]);
  });
}

/* ---------------- 模块五：数据血缘 ---------------- */

function renderLineage(m, raw) {
  var c = m.row_counts || {};
  var h = '<div class="kv">';
  Object.keys(c).forEach(function (k) {
    h += '<div class="item"><div class="k">' + esc(k) + '</div><div class="v">' + c[k] + ' 行</div></div>';
  });
  h += '</div>';
  h += '<div class="note" style="margin-top:12px">数据源：<b>' + esc(m.source || '') + '</b>。' +
    '抓取时间：' + esc((m.fetched_between || [])[0] || '—') + ' ~ ' + esc((m.fetched_between || [])[1] || '—') +
    '。库文件：data/market.db（本机）。</div>';
  $('lineage-counts').innerHTML = h;

  var t = '<thead><tr><th class="no-sort">ID</th><th class="no-sort">交易日</th>' +
    '<th class="no-sort">来源</th><th class="no-sort">接口类型</th>' +
    '<th class="no-sort">查询语句</th><th class="no-sort">返回长度</th></tr></thead><tbody>';
  (raw.items || []).forEach(function (r) {
    t += '<tr><td>' + r.id + '</td><td class="dim">' + esc(r.trade_date) + '</td>' +
      '<td>' + esc(r.api_tag) + '</td><td>' + esc(r.api_type) + '</td>' +
      '<td style="text-align:left;white-space:normal;max-width:520px">' + esc(r.query) + '</td>' +
      '<td>' + r.len + '</td></tr>';
  });
  $('lineage-tbl').innerHTML = t + '</tbody>';
}

/* ---------------- 初始化 ---------------- */

function switchTab(name) {
  Array.prototype.forEach.call(document.querySelectorAll('#nav button'), function (b) {
    b.classList.toggle('on', b.dataset.tab === name);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.panel'), function (p) {
    p.classList.toggle('on', p.id === 'panel-' + name);
  });
  if (name === 'stock' && !S.stockLoaded) {
    S.stockLoaded = true;
    var first = (S.pool || [])[0];
    if (first) loadStock(first.code);
  }
  if (name === 'screen' && !S.screenLoaded) { S.screenLoaded = true; runScreen(); }
  if (name === 'folio' && !S.folioLoaded) { S.folioLoaded = true; loadFolio(); }
  if (name === 'lineage' && !S.lineageLoaded) {
    S.lineageLoaded = true;
    Promise.all([api('/api/meta'), api('/api/raw_recall')]).then(function (r) {
      renderLineage(r[0], r[1]);
    });
  }
  setTimeout(function () {
    for (var k in charts) { if (charts[k]) charts[k].resize(); }
  }, 60);
}

function boot() {
  Array.prototype.forEach.call(document.querySelectorAll('#nav button'), function (b) {
    b.onclick = function () { switchTab(b.dataset.tab); };
  });
  $('btn-reload').onclick = function () { location.reload(); };
  renderFilters();

  $('btn-screen').onclick = runScreen;
  $('btn-reset').onclick = function () {
    FILTER_DEFS.forEach(function (f) { $('f-' + f.k).value = ''; });
    runScreen();
  };

  $('btn-add-pos').onclick = function () {
    var code = ($('i-code').value || '').trim().toUpperCase();
    var shares = ($('i-shares').value || '').trim();
    var cost = ($('i-cost').value || '').trim();
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(code)) { $('add-hint').textContent = '代码需形如 600519.SH'; return; }
    if (!(parseFloat(shares) > 0)) { $('add-hint').textContent = '请输入持股数（大于 0）'; return; }
    $('add-hint').textContent = '保存中…（若为新标的会自动抓取数据）';
    api('/api/portfolio', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'upsert', code: code, name: $('i-name').value, shares: shares, cost: cost })
    }).then(function () {
      $('add-hint').textContent = '已保存';
      $('i-code').value = $('i-shares').value = $('i-cost').value = $('i-name').value = '';
      S.folioLoaded = false;
      return loadFolio();
    }).catch(function (e) { $('add-hint').textContent = '失败：' + e.message; });
  };

  $('btn-add-watch').onclick = function () {
    var code = ($('i-code').value || '').trim().toUpperCase();
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(code)) { $('add-hint').textContent = '代码需形如 600519.SH'; return; }
    $('add-hint').textContent = '抓取中…';
    addWatch(code, $('i-name').value, null).then(function () {
      $('add-hint').textContent = '已加入自选并补齐数据';
    });
  };

  // 首屏
  Promise.all([api('/api/meta'), api('/api/summary')]).then(function (r) {
    renderMeta(r[0]);
    S.meta = r[0];
    if (r[1].empty) {
      document.querySelector('main').innerHTML =
        '<div class="card"><div class="empty">本地库为空。请先执行：<br><br>' +
        '<code>python3 sync.py</code><br><br>同步完成后再刷新本页。</div></div>';
      return;
    }
    renderDash(r[1]);
    renderStockChips();
    // 缺口说明
    var gaps = [];
    if (!r[1].breadth.some(function (b) { return b.up_count !== null && b.up_count !== undefined; })) {
      gaps.push('涨跌家数 / 涨停家数：上游接口未提供该字段，本平台不展示，也不用其它口径估算。');
    }
    gaps.push('行业覆盖：板块接口单次仅返回涨幅前若干名与主力净流入前 3 名，因此行业一览不等于全部行业，排序结论仅在这些板块内成立。');
    gaps.push('估值覆盖面：PE/PB/股息率为单标的粒度接口，只能逐只取。当前已覆盖重点研究池与自选股，选股器里的「估值覆盖」标签会实时显示覆盖比例。');
    gaps.push('条件选股：上游没有筛选接口，选股器是在本地库上执行的，因此只能在已入库的标的中筛选。');
    $('gap-note').innerHTML = gaps.map(function (g) { return '· ' + g; }).join('<br>');
  }).catch(function (e) {
    document.querySelector('main').innerHTML =
      '<div class="card"><div class="empty">无法连接本地服务：' + esc(e.message) +
      '<br><br>请确认已运行 <code>python3 server.py</code></div></div>';
  });
}

document.addEventListener('DOMContentLoaded', boot);
