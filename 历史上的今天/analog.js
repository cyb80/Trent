/* 历史上的今天 —— 页面逻辑 */
(function () {
  'use strict';

  var D = window.ANALOG_DATA;
  var SNAP = window.ANALOG_SNAPSHOT;
  var IDXS = ['上证50', '沪深300', '中证500', '中证1000'];
  var HZS = [5, 10, 20, 60, 120, 240];
  var HZ_NAME = { 5: '1 周', 10: '2 周', 20: '1 月', 60: '3 月', 120: '半年', 240: '1 年' };
  var BLOCK_NAME = {
    trend: '趋势', momentum: '动量', vol: '波动', valuation: '估值',
    sentiment: '情绪', volume: '量能', macro: '宏观', basis: '基差'
  };
  var FEAT_NAME = {
    ma20: '距MA20', ma60: '距MA60', ma200: '距MA200', ma60_slope: 'MA60斜率',
    r20: '20日收益', r60: '60日收益', rv20: '20日波动率', vol_ratio: '短长波动比',
    iv30: 'IV30', pb: 'PB分位', pe: 'PE分位', spread: '股债利差', margin_chg: '融资余额变化',
    margin_flow: '融资净买入强度', qvix: '中国波指',
    m1m2: 'M1-M2', pmi: 'PMI', ppa_cpi: 'PPI-CPI', basis: '年化基差'
  };
  var BLOCK_LAST_LABEL = {
    trend: '行情', momentum: '行情', vol: '行情/IV', valuation: 'PE/PB/股债',
    sentiment: '两融/波指', volume: '成交量', macro: '宏观月度', basis: '期货基差'
  };

  var state = { idx: '沪深300', hz: 20 };
  var charts = {};

  if (!D || !D.indices) {
    document.querySelector('main').innerHTML =
      '<div class="error">数据未加载。请先运行 <code>python 历史上的今天/analog_core.py</code> 生成 analog_data.js。</div>';
    return;
  }

  var DATA_DATE = (D.indices['沪深300'] || {}).date || '';
  var LIVE = !!(SNAP && SNAP.indices && SNAP.trade_date > DATA_DATE);

  function byIdx(name) { return D.indices[name] || D.indices['沪深300']; }
  function liveFor(name) {
    if (!LIVE || !SNAP.indices[name]) return null;
    if (SNAP.indices[name].date < DATA_DATE) return null;
    return SNAP.indices[name];
  }
  function pick(name, h) {
    var lv = liveFor(name);
    var it = lv && lv.horizons[String(h)];
    if (it) { it = JSON.parse(JSON.stringify(it)); it._live = true; return it; }
    var c = byIdx(name).horizons[String(h)];
    return c ? JSON.parse(JSON.stringify(c)) : null;
  }
  function pct(x, d) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return (x * 100).toFixed(d === undefined ? 1 : d) + '%';
  }
  function spct(x, d) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return (x > 0 ? '+' : '') + (x * 100).toFixed(d === undefined ? 1 : d) + '%';
  }
  function cls(x) { return x > 0 ? 'pos' : (x < 0 ? 'neg' : 'muted'); }
  function colorOf(p) { return p >= 0.55 ? '#e53935' : (p <= 0.45 ? '#43a047' : '#607d8b'); }

  // ---------------------------------------------------------------- 状态栏
  function renderStatus() {
    var el = document.getElementById('status-bar');
    var h = '';
    if (LIVE) {
      h += '<span><i class="dot live"></i>盘中快照 ' + SNAP.updated + '（' +
        (SNAP.indices[state.idx] ? (SNAP.indices[state.idx].price_time || '') : '') + '）</span>';
    }
    h += '<span><i class="dot"></i>收盘数据截止 ' + DATA_DATE + '</span>';
    h += '<span>模型生成 ' + D.updated + '</span>';
    h += '<span>相似日样本 ' + (byIdx(state.idx).n_days || 0) + ' 个交易日（自 ' +
      (byIdx(state.idx).first_date || '') + '）</span>';
    if (LIVE) h += '<span class="tag-live">盘中数字随价格变动，收盘后以正式结果为准</span>';
    el.innerHTML = h;
  }

  // ---------------------------------------------------------------- 选择器
  function renderTabs() {
    var it = document.getElementById('idx-tabs');
    it.innerHTML = IDXS.map(function (n) {
      var live = liveFor(n);
      var d = byIdx(n);
      var chg = live ? live.chg : null;
      var sub = chg === null || chg === undefined ? '' :
        '<span class="sub">' + (chg > 0 ? '+' : '') + (chg * 100).toFixed(2) + '%</span>';
      return '<button class="pill' + (n === state.idx ? ' on' : '') + '" data-idx="' + n + '">' + n + sub + '</button>';
    }).join(' ');
    var hz = document.getElementById('hz-tabs');
    hz.innerHTML = HZS.map(function (h) {
      return '<button class="pill' + (h === state.hz ? ' on' : '') +
        '" data-hz="' + h + '">' + h + '日<span class="sub">' + HZ_NAME[h] + '</span></button>';
    }).join(' ');
    it.onclick = function (e) {
      var b = e.target.closest('[data-idx]');
      if (!b) return;
      state.idx = b.getAttribute('data-idx');
      renderAll();
    };
    hz.onclick = function (e) {
      var b = e.target.closest('[data-hz]');
      if (!b) return;
      state.hz = parseInt(b.getAttribute('data-hz'), 10);
      renderAll();
    };
  }

  // ---------------------------------------------------------------- KPI
  function renderKpi() {
    var box = document.getElementById('kpi-grid');
    var html = '';
    HZS.forEach(function (h) {
      var it = pick(state.idx, h);
      if (!it) return;
      var p = it.p_up;
      var dir = p >= 0.55 ? '偏多' : (p <= 0.45 ? '偏空' : '中性');
      var side = p >= 0.55 ? 'up' : (p <= 0.45 ? 'down' : '');
      var pc = p >= 0.55 ? 'up' : (p <= 0.45 ? 'down' : 'flat');
      html += '<div class="kpi ' + side + '">' +
        '<div class="h"><span>' + h + ' 个交易日 · ' + HZ_NAME[h] + '</span><span>' + dir + '</span></div>' +
        '<div class="p ' + pc + '">' + (p * 100).toFixed(1) + '<span style="font-size:.9rem;font-weight:500;">%</span></div>' +
        '<div class="ci">上涨概率 · 90%CI ' + (it.ci90[0] * 100).toFixed(0) + '~' + (it.ci90[1] * 100).toFixed(0) + '%</div>' +
        '<div class="bar"><i style="width:' + (p * 100).toFixed(1) + '%"></i></div>' +
        '<div class="row2"><span>中性情形 <b class="' + cls(it.q50) + '">' + spct(it.q50, 2) + '</b></span>' +
        '<span>赔率 <b>' + (it.odds === null || it.odds === undefined ? '—' : it.odds.toFixed(2)) + '</b></span></div>' +
        '<div class="row2"><span>涨 <b class="pos">' + spct(it.up_mean) + '</b></span>' +
        '<span>跌 <b class="neg">' + spct(it.down_mean) + '</b></span></div>' +
        '<div class="row2"><span>80% 区间</span><span>' + spct(it.q10, 1) + ' ~ ' + spct(it.q90, 1) + '</span></div>' +
        '</div>';
    });
    box.innerHTML = html;
  }

  // ---------------------------------------------------------------- 概率锥
  function renderFan() {
    var name = state.idx, h = state.hz;
    var it = byIdx(name).horizons[String(h)];
    var dom = document.getElementById('chart-fan');
    if (!it || !it.fan) { dom.innerHTML = '<div class="error">该期限暂无概率锥数据</div>'; return; }
    document.getElementById('fan-title').textContent =
      name + ' · 未来 ' + h + ' 个交易日路径分位（' + HZ_NAME[h] + '）';
    var fan = it.fan;
    var xs = ['今日'];
    for (var i = 0; i < fan.length; i++) xs.push('T+' + fan[i].d);
    var q10 = [0], q25 = [0], q50 = [0], q75 = [0], q90 = [0];
    var b1090 = [0], b2575 = [0];
    fan.forEach(function (r) {
      q10.push(r.q[0]); q25.push(r.q[1]); q50.push(r.q[2]); q75.push(r.q[3]); q90.push(r.q[4]);
      b1090.push(r.q[4] - r.q[0]);
      b2575.push(r.q[3] - r.q[1]);
    });
    var series = [
      { name: 'q10', type: 'line', data: q10, stack: 'b1', symbol: 'none', lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true },
      { name: '10~90%', type: 'line', data: b1090, stack: 'b1', symbol: 'none', lineStyle: { opacity: 0 }, areaStyle: { color: '#90a4ae', opacity: 0.16 }, silent: true },
      { name: 'q25', type: 'line', data: q25, stack: 'b2', symbol: 'none', lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true },
      { name: '25~75%', type: 'line', data: b2575, stack: 'b2', symbol: 'none', lineStyle: { opacity: 0 }, areaStyle: { color: '#5c6bc0', opacity: 0.18 }, silent: true },
      { name: '中位', type: 'line', data: q50, symbol: 'none', lineStyle: { color: '#1a237e', width: 2 } }
    ];
    var nbs = it.neighbors || [];
    nbs.forEach(function (nb, k) {
      series.push({
        name: '相似' + (k + 1), type: 'line', symbol: 'none',
        data: [0].concat(nb.path.slice(1).map(function (v) { return v; })),
        lineStyle: { color: '#90a4ae', width: 0.8, opacity: 0.35 },
        emphasis: { disabled: true }, silent: true, z: 1
      });
    });
    var chart = charts.fan || (charts.fan = echarts.init(dom));
    chart.setOption({
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: function (ps) {
          var s = ps[0].axisValue + '<br/>';
          ['q10', 'q25', 'q75', 'q90'].forEach(function (nm) {
            var p = ps.filter(function (x) { return x.seriesName === nm; })[0];
            if (p) s += p.marker + ' ' + nm + ': ' + spct(p.value, 2) + '<br/>';
          });
          var m = ps.filter(function (x) { return x.seriesName === '中位'; })[0];
          if (m) s += m.marker + ' 中位: ' + spct(m.value, 2);
          return s;
        }
      },
      legend: { data: ['中位', '25~75%', '10~90%'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 62, right: 24, top: 34, bottom: 56 },
      dataZoom: [
        { type: 'inside', xAxisIndex: 0, filterMode: 'none' },
        { type: 'slider', xAxisIndex: 0, height: 16, bottom: 8, filterMode: 'none' }
      ],
      xAxis: {
        type: 'category', data: xs, boundaryGap: false,
        axisLabel: { fontSize: 10, hideOverlap: true }
      },
      yAxis: {
        type: 'value', name: '累计涨跌幅', nameTextStyle: { fontSize: 10 },
        axisLabel: { formatter: function (v) { return (v * 100).toFixed(0) + '%'; }, fontSize: 10 },
        splitLine: { lineStyle: { color: '#f0f2f5' } }
      },
      series: series
    }, true);
    chart.resize();
    document.getElementById('fan-note').innerHTML =
      '阴影为中位数两侧的 25~75% 与 10~90% 分位区间；灰线是 12 个最相似历史日之后的真实路径。' +
      '波动率已按今日 20 日已实现波动率对各历史路径做缩放（限制在 0.6~1.6 倍）。';
  }

  // ---------------------------------------------------------------- 收益分布
  function renderHist() {
    var name = state.idx, h = state.hz;
    var itClose = byIdx(name).horizons[String(h)];
    var it = pick(name, h) || itClose;
    var dom = document.getElementById('chart-hist');
    document.getElementById('hist-title').textContent =
      name + ' · ' + h + ' 个交易日后涨跌幅的分布';
    if (!it || !itClose || !itClose.hist) { dom.innerHTML = '<div class="error">该期限暂无分布数据</div>'; return; }
    var ed = itClose.hist.edges, w = itClose.hist.w;
    var cats = [], data = [], zeroIdx = 0, bestZ = 1e9;
    for (var i = 0; i < w.length; i++) {
      var mid = (ed[i] + ed[i + 1]) / 2;
      if (Math.abs(mid) < bestZ) { bestZ = Math.abs(mid); zeroIdx = i; }
      cats.push((mid * 100).toFixed(1) + '%');
      data.push({
        value: +(w[i] * 100).toFixed(2),
        itemStyle: { color: mid >= 0 ? '#ef9a9a' : '#a5d6a7' }
      });
    }
    var chart = charts.hist || (charts.hist = echarts.init(dom));
    chart.setOption({
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var p = ps[0];
          var lo = (ed[p.dataIndex] * 100).toFixed(1), hi = (ed[p.dataIndex + 1] * 100).toFixed(1);
          return lo + '% ~ ' + hi + '%<br/>条件概率 ' + p.value.toFixed(2) + '%';
        }
      },
      grid: { left: 62, right: 24, top: 20, bottom: 56 },
      dataZoom: [
        { type: 'inside', xAxisIndex: 0 },
        { type: 'slider', xAxisIndex: 0, height: 16, bottom: 8 }
      ],
      xAxis: { type: 'category', data: cats, axisLabel: { fontSize: 9, interval: 1, rotate: 0, hideOverlap: true } },
      yAxis: {
        type: 'value', name: '条件概率', nameTextStyle: { fontSize: 10 },
        axisLabel: { formatter: '{value}%', fontSize: 10 },
        splitLine: { lineStyle: { color: '#f0f2f5' } }
      },
      series: [{
        type: 'bar', data: data, barCategoryGap: '12%',
        markLine: {
          symbol: 'none', silent: true, label: { show: false },
          lineStyle: { color: '#90a4ae', type: 'dashed', width: 1 },
          data: [{ xAxis: cats[zeroIdx] }]
        }
      }]
    }, true);
    chart.resize();
    var mfe = it.mfe, mae = it.mae;
    document.getElementById('hist-note').innerHTML =
      '上涨概率 <b>' + pct(it.p_up) + '</b>（基准 <b>' + pct(it.p_base) + '</b>）｜ 期望 <b class="' + cls(it.exp_ret) + '">' + spct(it.exp_ret, 2) + '</b>' +
      ' ｜ 上涨时平均 <b class="pos">' + spct(it.up_mean) + '</b>，下跌时平均 <b class="neg">' + spct(it.down_mean) + '</b><br/>' +
      '期间最大涨幅中位 <b class="pos">' + spct(mfe[1]) + '</b>（75分位 ' + spct(mfe[2]) + ' ／ 90分位 ' + spct(mfe[3]) + '）' +
      ' ｜ 期间最大跌幅中位 <b class="neg">' + spct(mae[1]) + '</b>（25分位 ' + spct(mae[0]) + ' ／ 10分位 ' + spct(mae[3]) + '）';
  }

  // ---------------------------------------------------------------- 相似日
  function sparkline(path) {
    if (!path || path.length < 2) return '';
    var w = 110, hh = 26, n = path.length;
    var mn = Math.min.apply(null, path), mx = Math.max.apply(null, path);
    if (mx - mn < 1e-6) { mx = mn + 1e-3; }
    var pts = path.map(function (v, i) {
      var x = (i / (n - 1)) * w;
      var y = hh - 2 - ((v - mn) / (mx - mn)) * (hh - 4);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    var zero = hh - 2 - ((0 - mn) / (mx - mn)) * (hh - 4);
    var col = path[n - 1] >= 0 ? '#e53935' : '#43a047';
    return '<svg class="spark" viewBox="0 0 ' + w + ' ' + hh + '" preserveAspectRatio="none">' +
      '<line x1="0" y1="' + zero.toFixed(1) + '" x2="' + w + '" y2="' + zero.toFixed(1) + '" stroke="#e0e4ea" stroke-width="1"/>' +
      '<polyline points="' + pts + '" fill="none" stroke="' + col + '" stroke-width="1.3"/></svg>';
  }

  function renderNeighbors() {
    var name = state.idx, h = state.hz;
    var it = byIdx(name).horizons[String(h)];
    var box = document.getElementById('neighbor-table');
    if (!it || !it.neighbors) { box.innerHTML = '<div class="error">暂无相似日</div>'; return; }
    var rows = it.neighbors.map(function (nb, i) {
      return '<tr><td class="muted">' + (i + 1) + '</td><td class="l">' + nb.date + '</td>' +
        '<td>' + nb.dist.toFixed(4) + '</td><td>' + (nb.w * 100).toFixed(1) + '%</td>' +
        '<td class="' + cls(nb.ret) + '">' + spct(nb.ret, 2) + '</td>' +
        '<td>' + sparkline(nb.path) + '</td></tr>';
    }).join('');
    box.innerHTML = '<table class="dt"><thead><tr><th>#</th><th class="l">历史日期</th><th>状态距离</th><th>权重</th>' +
      '<th>此后 ' + h + ' 日收益</th><th>走势</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  // ---------------------------------------------------------------- 方法对照
  function renderCross() {
    var name = state.idx;
    var base = byIdx(name);
    var rows = '';
    HZS.forEach(function (h) {
      var it = pick(name, h);
      if (!it) return;
      var c = it.cross || (base.horizons[String(h)] || {}).cross || {};
      var mc = c.mc, iv = c.iv;
      rows += '<tr><td class="l">' + h + ' 日</td>' +
        '<td class="' + cls(it.p_up - 0.5) + '">' + pct(it.p_up) + '</td>' +
        '<td class="muted">' + spct(it.q10, 1) + ' ~ ' + spct(it.q90, 1) + '</td>' +
        '<td>' + (mc ? pct(mc.p_up) : '—') + '</td>' +
        '<td class="muted">' + (mc ? (spct(mc.q10, 1) + ' ~ ' + spct(mc.q90, 1)) : '—') + '</td>' +
        '<td class="muted">' + (iv ? (spct(iv.q10, 1) + ' ~ ' + spct(iv.q90, 1)) : '—') + '</td></tr>';
    });
    document.getElementById('cross-table').innerHTML =
      '<table class="dt"><thead><tr><th class="l">期限</th><th>相似日概率</th><th>相似日 80%区间</th>' +
      '<th>蒙特卡洛</th><th>蒙特卡洛 80%区间</th><th>期权 IV 80%区间</th></tr></thead><tbody>' + rows + '</tbody></table>';
    var note = '蒙特卡洛：EWMA(λ=0.94) 标准化残差自举 3000 条路径，零漂移，只看波动给出的分布形状；';
    if (name === '中证1000') {
      note += '期权 IV：MO 期权反算的 30 天平值隐含波动率，按 √t 折算到各期限（仅中证1000 有数据）。';
    } else {
      note += '期权 IV 仅覆盖中证1000（MO 期权），其余指数留空。';
    }
    note += '<br/>若相似日的多空区间明显偏向一侧，而蒙特卡洛仍以 0 对称，说明偏离来自「历史相似状态」本身的经验，而非当前波动水平。';
    document.getElementById('cross-note').innerHTML = note;
  }

  // ---------------------------------------------------------------- 状态分解
  function renderBlocks() {
    var name = state.idx;
    var o = byIdx(name);
    var bl = o.blocks || {};
    var last = o.block_last || {};
    var rows = '';
    ['trend', 'momentum', 'vol', 'valuation', 'sentiment', 'volume', 'macro', 'basis'].forEach(function (k) {
      var f = bl[k];
      if (!f) return;
      var chips = '';
      var any = false;
      Object.keys(f).forEach(function (fk) {
        var v = f[fk];
        if (v === null || v === undefined) return;
        any = true;
        var bg = v > 0.15 ? '#fdecea' : (v < -0.15 ? '#e8f5e9' : '#f2f4f8');
        var col = v > 0.15 ? '#c62828' : (v < -0.15 ? '#2e7d32' : '#607d8b');
        chips += '<span style="display:inline-block;margin:2px 4px 2px 0;padding:1px 7px;border-radius:9px;background:' +
          bg + ';color:' + col + ';font-size:.74rem;">' + (FEAT_NAME[fk] || fk) + ' ' + v.toFixed(2) + '</span>';
      });
      if (!any) chips = '<span class="muted" style="font-size:.78rem;">当前时点无可用数据</span>';
      var stale = last[k] && last[k] < DATA_DATE ? ' <span style="color:#e65100;font-size:.72rem;">(截止 ' + last[k] + ')</span>' : '';
      rows += '<tr><td class="l">' + BLOCK_NAME[k] + '<span class="muted" style="font-size:.72rem;"> ' +
        (D.method.blocks[k] * 100).toFixed(0) + '%</span></td><td class="l">' + chips + '</td>' +
        '<td class="muted" style="font-size:.74rem;">' + (BLOCK_LAST_LABEL[k] || '') + stale + '</td></tr>';
    });
    document.getElementById('block-table').innerHTML =
      '<table class="dt"><thead><tr><th class="l">维度</th><th class="l">特征 z 值</th><th>数据来源</th></tr></thead><tbody>' +
      rows + '</tbody></table>';
    document.getElementById('block-note').innerHTML =
      'z 值为该特征在过去 750 个交易日分布中的位置（截断到 ±1）。红色偏向「更强/更高」，绿色偏向「更弱/更低」。' +
      '标记为「截止」的维度因数据源未更新，已自动从相似度计算中剔除，其余维度权重重新归一。';
  }

  // ---------------------------------------------------------------- 回测
  function btOf(name) { return (D.backtest || {})[name] || { dates: [], h: {} }; }

  function renderMetrics() {
    var bt = btOf(state.idx);
    var rows = '';
    HZS.forEach(function (h) {
      var m = (bt.h[String(h)] || {}).metrics || {};
      if (!m.n) return;
      var better = (m.brier !== undefined && m.brier_base !== undefined && m.brier <= m.brier_base);
      rows += '<tr><td class="l">' + h + ' 日</td><td>' + m.n + '</td>' +
        '<td class="' + (better ? 'pos' : '') + '">' + (m.brier === undefined ? '—' : m.brier.toFixed(4)) + '</td>' +
        '<td class="muted">' + (m.brier_base === undefined ? '—' : m.brier_base.toFixed(4)) + '</td>' +
        '<td>' + (m.hit_all === undefined ? '—' : (m.hit_all * 100).toFixed(1) + '%') + '</td>' +
        '<td class="' + (m.cov80 === undefined ? '' : (m.cov80 >= 0.75 ? 'pos' : 'neg')) + '">' +
        (m.cov80 === undefined ? '—' : (m.cov80 * 100).toFixed(0) + '%') + '</td>' +
        '<td class="muted">' + (m.width === undefined ? '—' : (m.width * 100).toFixed(1) + '%') + '</td>' +
        '<td>' + (m.hit_hi === undefined || m.hit_hi === null ? '—' : (m.hit_hi * 100).toFixed(0) + '% (' + m.n_hi + ')') + '</td>' +
        '<td>' + (m.hit_lo === undefined || m.hit_lo === null ? '—' : (m.hit_lo * 100).toFixed(0) + '% (' + m.n_lo + ')') + '</td></tr>';
    });
    document.getElementById('metrics-table').innerHTML =
      '<table class="dt"><thead><tr><th class="l">期限</th><th>样本</th><th>Brier(模型)</th><th>Brier(基准)</th>' +
      '<th>方向命中率</th><th>80%区间覆盖率</th><th>区间宽度</th>' +
      '<th>p≥60% 时实际上涨</th><th>p≤40% 时实际下跌</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function renderCalib() {
    var bt = btOf(state.idx);
    var item = bt.h[String(state.hz)] || {};
    var rel = (item.metrics || {}).rel || [];
    var pts = [], maxN = 1;
    rel.forEach(function (r) { if (r.n > maxN) maxN = r.n; });
    rel.forEach(function (r) {
      if (r.p === null || r.real === null || !r.n) return;
      pts.push([r.p, r.real, r.n, r.range]);
    });
    var dom = document.getElementById('chart-calib');
    var chart = charts.calib || (charts.calib = echarts.init(dom));
    chart.setOption({
      tooltip: {
        formatter: function (p) {
          var d = pts[p.dataIndex];
          return '预测 ' + (d[0] * 100).toFixed(1) + '% → 实际 ' + (d[1] * 100).toFixed(1) +
            '%<br/>区间 ' + (d[3][0] * 100).toFixed(0) + '~' + (d[3][1] * 100).toFixed(0) + '% ，样本 ' + d[2];
        }
      },
      grid: { left: 56, right: 20, top: 22, bottom: 42 },
      xAxis: {
        type: 'value', min: 0, max: 1, name: '预测上涨概率', nameTextStyle: { fontSize: 10 },
        axisLabel: { formatter: function (v) { return (v * 100).toFixed(0) + '%'; }, fontSize: 10 }
      },
      yAxis: {
        type: 'value', min: 0, max: 1, name: '实际上涨频率', nameTextStyle: { fontSize: 10 },
        axisLabel: { formatter: function (v) { return (v * 100).toFixed(0) + '%'; }, fontSize: 10 },
        splitLine: { lineStyle: { color: '#f0f2f5' } }
      },
      series: [
        {
          type: 'line', data: [[0, 0], [1, 1]], symbol: 'none', silent: true,
          lineStyle: { color: '#b0bec5', type: 'dashed', width: 1 }
        },
        {
          type: 'scatter', data: pts,
          symbolSize: function (d) { return 8 + 18 * Math.sqrt(d[2] / maxN); },
          itemStyle: { color: '#3949ab', opacity: 0.75 },
          label: { show: true, position: 'top', fontSize: 9, color: '#888', formatter: function (p) { return p.data[2]; } }
        }
      ]
    }, true);
    chart.resize();
  }

  function renderBtSeries() {
    var bt = btOf(state.idx);
    var item = bt.h[String(state.hz)] || {};
    var dom = document.getElementById('chart-bt');
    var p = item.p || [], real = item.real || [];
    var realPts = [];
    for (var i = 0; i < real.length; i++) if (real[i] !== null) realPts.push([i, real[i]]);
    var chart = charts.bt || (charts.bt = echarts.init(dom));
    chart.setOption({
      tooltip: {
        trigger: 'axis',
        formatter: function (ps) {
          var i = ps[0].dataIndex;
          return bt.dates[i] + '<br/>模型概率 ' + (p[i] === null ? '—' : (p[i] * 100).toFixed(1) + '%') +
            '<br/>实际 ' + (real[i] === null ? '—' : (real[i] ? '上涨' : '下跌'));
        }
      },
      legend: { data: ['模型概率', '实际结果'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 56, right: 20, top: 30, bottom: 52 },
      dataZoom: [
        { type: 'inside', xAxisIndex: 0 },
        { type: 'slider', xAxisIndex: 0, height: 16, bottom: 8 }
      ],
      xAxis: {
        type: 'category', data: bt.dates, boundaryGap: false,
        axisLabel: { fontSize: 10, hideOverlap: true, formatter: function (v) { return String(v).slice(0, 7); } }
      },
      yAxis: {
        type: 'value', min: 0, max: 1, name: '概率',
        axisLabel: { formatter: function (v) { return (v * 100).toFixed(0) + '%'; }, fontSize: 10 },
        splitLine: { lineStyle: { color: '#f0f2f5' } }
      },
      series: [
        {
          name: '模型概率', type: 'line', data: p, symbol: 'none',
          lineStyle: { color: '#1a237e', width: 1.6 },
          areaStyle: { color: '#3949ab', opacity: 0.06 }
        },
        {
          name: '实际结果', type: 'scatter', data: realPts, yAxisIndex: 0,
          symbolSize: 5, itemStyle: { color: 'rgba(120,144,156,0.55)' }
        }
      ]
    }, true);
    chart.resize();
  }

  // ---------------------------------------------------------------- 方法说明
  function renderMethod() {
    var w = D.method.blocks;
    var bl = Object.keys(w).map(function (k) {
      return BLOCK_NAME[k] + ' ' + (w[k] * 100).toFixed(0) + '%';
    }).join('、');
    document.getElementById('method-body').innerHTML =
      '<b>1. 状态刻画</b>：把当前市场拆成 8 个特征块（' + bl + '）。每个特征先在过去 <code>' + D.method.z_win +
      '</code> 个交易日内做滚动 z-score（截断 ±3 后除以 3），月频数据（宏观、股债利差）按 60 期滚动标准化并滞后 45 天发布；' +
      '数据过期或缺失的块自动退出，剩余块权重重新归一。<br/>' +
      '<b>1.1 关于权重</b>：8 个块的权重是先验设定（按经济含义排序），块内特征等权，<b>并未用回测结果拟合</b>。' +
      '做过敏感性检验：40 组随机权重在 2022 年以后样本外 Brier 的差异仅约 ±0.005，且在 2016-2021 上表现最好的权重放到样本外并不占优——' +
      '说明在样本量有限、重叠窗口高度相关的条件下，调权重只会拟合噪声，因此保留可解释的先验权重。<br/>' +
      '<b>1.2 关于近期相似日</b>：候选集要求 <code>s ≤ t − max(25, h)</code>，即结果必须已经完全兑现，杜绝未来函数；但<b>不额外剔除近期样本</b>。' +
      '实测 200 个邻居分布在二十余年历史中（约 192 个落在互不重叠的 20 日区间），只是权重最高的一批多来自最近两三个月——' +
      '这正是「行情同源、状态延续」的信息：强行剔除近 250 日或按 20 日强制去重后，样本外 Brier 反而变差（沪深300 10 日 0.247→0.268）。' +
      '重叠样本带来的「独立信息少于样本数」问题，改由收缩强度（按 h 折算独立样本）与 90% 置信区间体现。<br/>' +
      '<b>2. 相似度</b>：每个块先按自身尺度归一，再块内取特征差的均方平均，最后按块权重加权，得到「今天」与历史每一天的距离。<br/>' +
      '<b>3. 邻居</b>：只在 <code>s ≤ t - max(25, h)</code> 的历史日中挑选最近的 <code>' + D.method.topk +
      '</code> 个，权重 = 高斯核（尺度取最近邻距离中位数）× 时间衰减（半衰期 ' + D.method.decay_halflife +
      ' 个交易日），越近的历史权重越高。<br/>' +
      '<b>4. 概率</b>：邻居之后 h 个交易日的涨跌频率即为经验概率，再按独立样本量向「截至今日的无条件上涨频率」收缩' +
      '（重叠窗口按 20 日折算独立样本，长周期更依赖基准概率），90% 置信区间由 Beta 后验给出。<br/>' +
      '<b>5. 空间</b>：邻居的前瞻收益按 <code>σ今日 / σ当时</code> 缩放（限制 0.6~1.6 倍）后取加权分位（10/25/50/75/90），' +
      '并统计上涨/下跌的条件均值、赔率，以及期间最大涨幅 MFE 与最大跌幅 MAE 的分位。<br/>' +
      '<b>6. 交叉验证</b>：用 EWMA(λ=0.94) 标准化残差自举 3000 条蒙特卡洛路径（零漂移）给出纯波动驱动的分布；' +
      '中证1000 另用 MO 期权反算的 30 天 IV 折算区间。<br/>' +
      '<b>7. 回测</b>：2016 年起每 5 个交易日滚动一次，严格只使用当时可见信息，评估 Brier 分数、方向命中率与可靠性分箱。' +
      '注意长周期样本高度重叠（240 日仅有约 2 段独立行情），指标仅供参考。<br/>' +
      '<b>8. 更新</b>：交易日 14:30 生成盘中快照（只用实时价临时替换当日 K 线，不写入训练样本），收盘后 15:05 之后生成正式结果。';
  }

  function renderAll() {
    renderStatus();
    renderTabs();
    renderKpi();
    renderFan();
    renderHist();
    renderNeighbors();
    renderCross();
    renderBlocks();
    renderMetrics();
    renderCalib();
    renderBtSeries();
  }

  renderMethod();
  renderAll();
  document.getElementById('data-source').textContent =
    '相似日经验分布 | 8 维状态特征 · Top' + D.method.topk + ' 邻居 · 时间衰减半衰期 ' + D.method.decay_halflife +
    ' 日 | 正式数据生成于 ' + D.updated + (LIVE ? '，并含 ' + SNAP.updated + ' 盘中快照' : '');

  var rz;
  window.addEventListener('resize', function () {
    clearTimeout(rz);
    rz = setTimeout(function () {
      Object.keys(charts).forEach(function (k) { charts[k] && charts[k].resize(); });
    }, 120);
  });

})();
