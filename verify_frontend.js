/* 锐思投研 · 前端运行时验证
   把 index.html 装进 jsdom，用真实 HTTP 打到本机服务，检查五个模块是否真的渲染出来。
   不 mock 数据 —— 若前端没能正确消费 API，这里会直接失败。 */

const fs = require('fs');
const http = require('http');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = '/Users/zhangzhijian/WorkBuddy/2026-09-18-15-14-39/ruisi-platform';
const PORT = 8770;

let html = fs.readFileSync(path.join(ROOT, 'web/index.html'), 'utf8');
let appjs = fs.readFileSync(path.join(ROOT, 'web/app.js'), 'utf8');

// 1) 去掉 CDN echarts，换成会记录 setOption 的 stub
html = html.replace(/<script src="https:\/\/cdn[^"]+"><\/script>/, `<script>
window.__opts = [];
window.echarts = {
  init: function(el){
    return {
      setOption: function(o){ window.__opts.push({id: el.id, opt: o}); },
      resize: function(){}, dispose: function(){}
    };
  }
};
</script>`);

// 2) 内联 app.js（jsdom 无 fetch，需要它走我们的 polyfill；内联也避免加载顺序问题）
html = html.replace('<script src="app.js"></script>', '<script>' + appjs + '</script>');

const vc = new VirtualConsole();
const logs = [];
vc.on('jsdomError', e => logs.push('JSDOM_ERR: ' + (e && e.message)));
vc.on('error', (...a) => logs.push('CONSOLE_ERR: ' + a.join(' ')));

function fetchImpl(url, opt) {
  return new Promise((resolve, reject) => {
    const u = new URL(url, 'http://127.0.0.1:' + PORT);
    // 必须显式带 Content-Length，否则 Node 会改用 chunked 编码
    const hdrs = Object.assign({}, (opt && opt.headers) || {});
    if (opt && opt.body) hdrs['Content-Length'] = Buffer.byteLength(opt.body);
    const req = http.request(
      { host: '127.0.0.1', port: PORT, path: u.pathname + u.search,
        method: (opt && opt.method) || 'GET', headers: hdrs },
      res => {
        let d = '';
        res.setEncoding('utf8');
        res.on('data', c => (d += c));
        res.on('end', () => resolve({
          ok: res.statusCode < 400, status: res.statusCode,
          json: () => Promise.resolve(JSON.parse(d))
        }));
      }
    );
    req.on('error', reject);
    if (opt && opt.body) req.write(opt.body);
    req.end();
  });
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc,
  url: 'http://127.0.0.1:' + PORT + '/',
  beforeParse(w) {
    w.fetch = fetchImpl;
    Object.defineProperty(w.HTMLElement.prototype, 'clientWidth', { get() { return 900; } });
    Object.defineProperty(w.HTMLElement.prototype, 'clientHeight', { get() { return 320; } });
  }
});

const { window } = dom;
const doc = window.document;

const results = [];
function check(name, cond, got) {
  results.push({ name, cond: !!cond, got });
  console.log(`  ${cond ? 'PASS ' : 'FAIL '} ${name}${got === undefined ? '' : '   → ' + got}`);
}
const txt = id => ((doc.getElementById(id) || {}).textContent || '');

function click(sel) {
  const el = doc.querySelector(sel);
  if (el) el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async function () {
  // 先把自选与持仓清空，保证空态断言不受上一轮残留影响
  try {
    const wl = await (await fetchImpl('/api/watchlist')).json();
    for (const w of (wl.items || [])) {
      await fetchImpl('/api/watchlist', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'remove', code: w.code }) });
    }
    const pf = await (await fetchImpl('/api/portfolio')).json();
    for (const q of (pf.positions || [])) {
      await fetchImpl('/api/portfolio', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'remove', code: q.code }) });
    }
  } catch (e) { console.log('  清理失败（忽略）:', e.message); }

  // 期望值一律从接口推导，不硬编码某一天的数字 —— 硬编码快照值的测试第二天就会整片失败，
  // 那种「验证」等于没有。
  const metaJ = await (await fetchImpl('/api/meta')).json();
  const idxJ = await (await fetchImpl('/api/indices')).json();
  const stkJ = await (await fetchImpl('/api/stocks')).json();
  const secJ = await (await fetchImpl('/api/sectors')).json();
  const ASOF = metaJ.trade_date;
  const SH = (idxJ.items || []).find(i => i.code === '000001.SH') || {};
  const MT = (stkJ.items || []).find(s => s.code === '600519.SH') || {};
  const CMB = (stkJ.items || []).find(s => s.code === '600036.SH') || {};
  const SEC_NAMES = (secJ.items || []).map(s => s.sector_name);
  // 与 app.js 的 nfmt 同口径（原生 toFixed，无千分位）；再放宽到千分位写法，
  // 免得前端换了数字格式就把这条断言误判成失败。
  const hasNum = (t, v, d) => {
    if (v === null || v === undefined || isNaN(v)) return false;
    const dd = (d === undefined) ? 2 : d;
    return t.indexOf(Number(v).toFixed(dd)) >= 0 ||
           t.indexOf(Number(v).toLocaleString('en-US',
             { minimumFractionDigits: dd, maximumFractionDigits: dd })) >= 0;
  };
  const nf = (v, d) => (v === null || v === undefined ? '—'
    : Number(v).toFixed(d === undefined ? 2 : d));
  check('取到库内最新交易日用于比对', !!ASOF, ASOF);
  check('取到茅台/招行的接口值用于比对', MT.close != null && CMB.close != null,
        `茅台 ${nf(MT.close)} / 招行 ${nf(CMB.close)}`);

  // 等待首屏两次 fetch 完成
  let waited = 0;
  while (waited < 8000 && doc.querySelectorAll('#idx-cards .metric').length === 0) {
    await sleep(120); waited += 120;
  }
  await sleep(400);

  console.log('='.repeat(70));
  console.log('一、首屏（市场总览）');
  console.log('='.repeat(70));

  const realErrs = logs.filter(l => l.startsWith('JSDOM_ERR:') || l.startsWith('CONSOLE_ERR:'));
  check('无 JS 运行时错误', realErrs.length === 0, realErrs.slice(0, 3).join(' | ') || '0');

  check('指数卡片 8 个', doc.querySelectorAll('#idx-cards .metric').length === 8,
        doc.querySelectorAll('#idx-cards .metric').length);
  const idxTxt = txt('idx-cards');
  check('上证指数已渲染为接口最新值', hasNum(idxTxt, SH.close), nf(SH.close));
  check('指数卡片标注涨跌方向', doc.querySelectorAll('#idx-cards .up').length > 0);

  check('行业表有行', doc.querySelectorAll('#sector-box tbody tr').length >= 3,
        doc.querySelectorAll('#sector-box tbody tr').length);
  check('行业表内容与接口一致', SEC_NAMES.some(n => txt('sector-box').indexOf(n) >= 0),
        SEC_NAMES.slice(0, 3).join(','));
  check('全池个股表有行', doc.querySelectorAll('#pool-tbl tbody tr').length >= 12,
        doc.querySelectorAll('#pool-tbl tbody tr').length);
  check('个股表含茅台与招行', txt('pool-tbl').indexOf('贵州茅台') >= 0 && txt('pool-tbl').indexOf('招商银行') >= 0);
  check('个股表股息率已展示且与接口一致',
        hasNum(txt('pool-tbl'), CMB.dividend_yield) || hasNum(txt('pool-tbl'), MT.dividend_yield),
        `招行 ${nf(CMB.dividend_yield)}% / 茅台 ${nf(MT.dividend_yield)}%`);
  check('两市成交已渲染', txt('breadth-box').indexOf('万亿') >= 0);
  check('涨跌家数标注为未提供', txt('breadth-hint').indexOf('未提供') >= 0, txt('breadth-hint'));

  check('顶部显示快照时点 = 接口数据时点', txt('p-asof').indexOf(ASOF) >= 0, txt('p-asof'));
  check('顶部未使用「实时」字样', txt('p-asof').indexOf('实时') < 0 && txt('p-source').indexOf('实时') < 0);
  check('顶部同步状态为 ok', txt('p-sync').indexOf('ok') >= 0, txt('p-sync'));

  const gaps = txt('gap-note');
  check('缺口说明提及涨跌家数', gaps.indexOf('涨跌家数') >= 0);
  check('缺口说明提及行业覆盖不全', gaps.indexOf('不等于全部行业') >= 0);
  check('缺口说明提及估值覆盖限制', gaps.indexOf('单标的粒度') >= 0);

  check('行业图与图表已初始化', window.__opts.length >= 1, window.__opts.map(o => o.id).join(','));

  console.log();
  console.log('='.repeat(70));
  console.log('二、个股深度研究');
  console.log('='.repeat(70));

  const chips = doc.querySelectorAll('#stock-chips .chip');
  check('个股快捷切换按钮存在', chips.length >= 8, chips.length);
  check('首个默认是重点研究池标的', chips.length && /重点|茅台|宁德|中际|招商|平安|新易盛|长江电力|紫金/.test(chips[0].textContent),
        chips.length ? chips[0].textContent.trim() : '');
  check('重点池与榜单标的已分组标注',
        doc.getElementById('stock-chips').textContent.indexOf('仅有行情') >= 0);
  check('个股表标注了数据完整度', txt('pool-tbl').indexOf('重点池') >= 0 && txt('pool-tbl').indexOf('仅行情') >= 0);

  // 显式点茅台，避免依赖排序
  const mtChip = Array.prototype.filter.call(chips, c => c.dataset.code === '600519.SH')[0];
  check('重点池含贵州茅台按钮', !!mtChip);
  mtChip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  waited = 0;
  while (waited < 8000 && txt('stock-body').indexOf('PE（扣非后滚动）') < 0) {
    await sleep(120); waited += 120;
  }
  await sleep(300);

  const sb = txt('stock-body');
  check('个股详情已渲染', sb.indexOf('贵州茅台') >= 0);
  check('显示收盘价（= 接口最新价）', hasNum(sb, MT.close), nf(MT.close));
  check('显示 PE（接口值）', hasNum(sb, MT.pe_ttm), nf(MT.pe_ttm));
  check('显示 PB（接口推算值）', hasNum(sb, MT.pb) && sb.indexOf('0.000') < 0, nf(MT.pb));
  var dyM = sb.match(/股息率（TTM，推算）([-\d.]+)%/);
  check('显示股息率（TTM 推算）且与接口一致',
        dyM && Math.abs(parseFloat(dyM[1]) - MT.dividend_yield) < 0.02,
        (dyM ? dyM[1] : '未匹配') + '% / 接口 ' + nf(MT.dividend_yield) + '%');
  check('显示估值分位', sb.indexOf('分位') >= 0);
  check('分位样本窗口已标注', sb.indexOf('不等于长周期分位') >= 0);
  check('显示资金流', sb.indexOf('主力净流入') >= 0);
  check('财务表有报告期', /2025-FY|2025-S|2024-FY/.test(sb.replace(/\s/g, '')));
  check('PE 序列图已配置', window.__opts.some(o => o.id === 'c-pe'),
        window.__opts.map(o => o.id).join(','));
  check('页面无 undefined 泄漏', sb.indexOf('undefined') < 0);
  check('页面无 NaN 泄漏', sb.indexOf('NaN') < 0);

  console.log();
  console.log('='.repeat(70));
  console.log('三、选股器');
  console.log('='.repeat(70));

  click('#nav button[data-tab="screen"]');
  waited = 0;
  while (waited < 8000 && doc.querySelectorAll('#screen-tbl tbody tr').length === 0) {
    await sleep(120); waited += 120;
  }
  await sleep(300);
  check('切 tab 后选股器激活', doc.getElementById('panel-screen').classList.contains('on'));
  check('筛选输入项 8 个', doc.querySelectorAll('#filters input').length === 8,
        doc.querySelectorAll('#filters input').length);
  check('结果表有行', doc.querySelectorAll('#screen-tbl tbody tr').length >= 15,
        doc.querySelectorAll('#screen-tbl tbody tr').length);
  check('覆盖度提示含估值覆盖', txt('coverage-hint').indexOf('估值覆盖') >= 0, txt('coverage-hint'));
  check('结果含「加自选」按钮', doc.querySelectorAll('#screen-tbl [data-add]').length >= 1);

  // 条件筛选：股息率 ≥3%
  doc.getElementById('f-min_dy').value = '3';
  const reqVal = doc.createElement('input');
  click('#btn-screen');
  waited = 0;
  while (waited < 6000 && txt('screen-count').indexOf('3') < 0) { await sleep(120); waited += 120; }
  await sleep(400);
  const scTxt = txt('screen-count');
  check('股息率筛选后结果变少', doc.querySelectorAll('#screen-tbl tbody tr').length < 18,
        scTxt + ' / 行数 ' + doc.querySelectorAll('#screen-tbl tbody tr').length);

  console.log();
  console.log('='.repeat(70));
  console.log('四、持仓与自选');
  console.log('='.repeat(70));

  click('#nav button[data-tab="folio"]');
  await sleep(500);
  check('切 tab 后持仓面板激活', doc.getElementById('panel-folio').classList.contains('on'));
  check('空持仓有引导文案', txt('folio-tbl').indexOf('尚未录入持仓') >= 0 || txt('risk-box').indexOf('录入持仓后') >= 0);
  check('自选池空态提示存在', txt('watch-tbl').indexOf('自选池为空') >= 0);

  // 录入一笔持仓（走真实 POST 回环）
  doc.getElementById('i-code').value = '600519.SH';
  doc.getElementById('i-name').value = '贵州茅台';
  doc.getElementById('i-shares').value = '100';
  doc.getElementById('i-cost').value = '1200';
  click('#btn-add-pos');
  waited = 0;
  while (waited < 20000 && doc.querySelectorAll('#folio-tbl tbody tr').length === 0) {
    await sleep(200); waited += 200;
  }
  await sleep(400);
  check('持仓行已渲染', doc.querySelectorAll('#folio-tbl tbody tr').length === 1,
        doc.querySelectorAll('#folio-tbl tbody tr').length);
  const ft = txt('folio-tbl');
  const expMv = MT.close * 100, expPnl = (MT.close - 1200) * 100;
  check('持仓显示市值（= 现价 × 持股数）', hasNum(ft, expMv), nf(expMv));
  check('持仓显示盈亏（= 市值 − 成本额）',
        hasNum(ft, expPnl) || hasNum(ft, Math.abs(expPnl)), nf(expPnl));
  check('权重 100% 已计算', ft.indexOf('100.00%') >= 0);
  check('持仓饼图已配置', window.__opts.some(o => o.id === 'c-expo'),
        window.__opts.map(o => o.id).join(','));
  check('风险检查已输出', txt('risk-box').length > 20);
  check('风险提示含免责声明', txt('risk-box').indexOf('不构成任何投资建议') >= 0);

  console.log();
  console.log('='.repeat(70));
  console.log('五、数据血缘');
  console.log('='.repeat(70));

  click('#nav button[data-tab="lineage"]');
  waited = 0;
  while (waited < 8000 && doc.querySelectorAll('#lineage-tbl tbody tr').length === 0) {
    await sleep(120); waited += 120;
  }
  await sleep(300);
  check('血缘面板激活', doc.getElementById('panel-lineage').classList.contains('on'));
  check('库表行数已展示', txt('lineage-counts').indexOf('行') >= 0);
  check('血缘表有原始返回记录', doc.querySelectorAll('#lineage-tbl tbody tr').length >= 30,
        doc.querySelectorAll('#lineage-tbl tbody tr').length);
  check('血缘记录含接口类型', /统一(行情|估值|资金流向)查询|板块|利润表|指数估值/.test(txt('lineage-tbl')));

  // 全局泄漏检查（剔除 script 源码）
  const clone = doc.body.cloneNode(true);
  clone.querySelectorAll('script,style').forEach(n => n.remove());
  const bodyText = clone.textContent;
  check('全页无 undefined 泄漏', bodyText.indexOf('undefined') < 0);
  check('全页无 NaN 泄漏', bodyText.indexOf('NaN') < 0);
  check('全页无 [object Object]', bodyText.indexOf('[object Object]') < 0);

  // 清理：删掉测试持仓
  await fetchImpl('/api/portfolio', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'remove', code: '600519.SH' })
  });

  const pass = results.filter(r => r.cond).length;
  console.log();
  console.log('='.repeat(70));
  console.log(`  通过 ${pass} / ${results.length}`);
  const failed = results.filter(r => !r.cond);
  if (failed.length) {
    console.log('  失败项：');
    failed.forEach(f => console.log('    - ' + f.name));
  }
  console.log('='.repeat(70));
  process.exit(failed.length ? 1 : 0);
})();
