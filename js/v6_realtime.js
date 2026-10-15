/* V6 股指期货基差轮动 —— 页面渲染 + 盘中实时持仓计算 (js/v6_realtime.js)
 *
 * 数据: window.V6_BACKTEST (历史回测: 日频多空腿收益 + 每期选合约)
 *       window.V6_LIVE     (当前在挂合约/现货收盘/月末交易日/最近一期调仓)
 *
 * 实时逻辑 (严格按《V6策略说明文档.md》):
 *   候选池 = 全部在挂月份合约中「到期日 >= 下一个调仓日」者, 跨品种统一排名;
 *   年化基差率 b = (期货价 / 对应现货指数 − 1) × 365 / 剩余自然日;
 *   做多 b 最小(贴水最深)且 b <= 多头阈值者, 做空 b 最大(升水最高)且 b >= 空头阈值者; 两腿各 100% 名义。
 *
 * 更新时段:
 *   09:30–11:30 / 13:00–15:00  每 1 分钟用实时行情重算(只刷新数据与图, 不整页刷新)
 *   11:30–13:00  午间休市, 暂停
 *   15:00–16:00  停更(仅显示 15:00 收盘价)
 *   16:00        以收盘价更新一次, 之后不再更新
 *   盘前         使用上一交易日收盘(数据文件)
 */
(function () {
  "use strict";

  var bt = window.V6_BACKTEST;
  var live = window.V6_LIVE;
  if (!bt || !live) {
    document.querySelector("main").insertAdjacentHTML(
      "afterbegin",
      '<div class="error">数据未加载。请先运行 <code>python fetch_v6_futures.py</code> 与 <code>python gen_v6_backtest.py</code> 生成数据文件。</div>'
    );
    return;
  }

  var ANN = bt.ann || 252;
  var FACTOR = bt.factor || 365;
  var PROD2SPOT = { IH: "000016", IF: "000300", IC: "000905", IM: "000852" };
  var PROD_NAME = { IH: "上证50", IF: "沪深300", IC: "中证500", IM: "中证1000" };
  var PROD_ORDER = ["IH", "IF", "IC", "IM"];

  // ---------------- 工具 ----------------
  function p2(x) { return x < 10 ? "0" + x : "" + x; }
  function fmtTime(d) {
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()) + " "
      + p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds());
  }
  function fmtDate(d) {
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate());
  }
  function bjNow() {
    var d = new Date();
    return new Date(d.getTime() + (d.getTimezoneOffset() + 480) * 60000);
  }
  function daysBetween(d1, d2) {
    return Math.round((Date.parse(d2 + "T00:00:00Z") - Date.parse(d1 + "T00:00:00Z")) / 86400000);
  }
  // 前一个自然日（用于把 nav[0]=1.0 归入调仓日所在月/年，与生成端口径一致）
  function prevDayStr(s) {
    var d = new Date(Date.parse(s + "T00:00:00Z") - 86400000);
    return d.getUTCFullYear() + "-" + p2(d.getUTCMonth() + 1) + "-" + p2(d.getUTCDate());
  }
  function num(x, d) { return (isFinite(x) ? x : 0).toFixed(d === undefined ? 2 : d); }
  function pct(x, d) { return (x >= 0 ? "+" : "") + (x * 100).toFixed(d === undefined ? 2 : d) + "%"; }
  function withTimeout(promise, ms) {
    return new Promise(function (resolve) {
      var done = false;
      var t = setTimeout(function () { if (!done) { done = true; resolve(null); } }, ms);
      promise.then(function (v) { if (!done) { done = true; clearTimeout(t); resolve(v); } })
        .catch(function () { if (!done) { done = true; clearTimeout(t); resolve(null); } });
    });
  }

  // ---------------- 回测重建（阈值可调） ----------------
  var state = { lo: bt.lo, sh: bt.sh };

  function rebuild(lo, sh) {
    var dates = bt.dates, n = dates.length;
    var rl = bt.r_long, rs = bt.r_short, per = bt.periods;
    var comb = new Array(n), i, k;
    for (i = 0; i < n; i++) comb[i] = 0;

    var periods = [];
    for (k = 0; k < per.length; k++) {
      var p = per[k];
      var oL = p.long_b <= lo, oS = p.short_b >= sh;
      var prod = 1, prodL = 1, prodS = 1;
      for (i = p.i0; i < p.i1; i++) {
        var v = 0;
        if (oL) { v += rl[i]; prodL *= (1 + rl[i]); }
        if (oS) { v -= rs[i]; prodS *= (1 - rs[i]); }
        comb[i] = v;
        prod *= (1 + v);
      }
      periods.push({
        T: p.T, Tn: p.Tn, long_c: p.long_c, long_b: p.long_b,
        short_c: p.short_c, short_b: p.short_b, oL: oL, oS: oS,
        long_ret: oL ? prodL - 1 : null, short_ret: oS ? prodS - 1 : null, ret: prod - 1
      });
    }

    // 净值序列: nav[0]=1 (起点=首个调仓日), nav[i+1] 对应 dates[i]
    var nav = new Array(n + 1);
    nav[0] = 1;
    for (i = 0; i < n; i++) nav[i + 1] = nav[i] * (1 + comb[i]);

    // 指标
    var mean = 0;
    for (i = 0; i < n; i++) mean += comb[i];
    mean /= n;
    var vsum = 0;
    for (i = 0; i < n; i++) vsum += (comb[i] - mean) * (comb[i] - mean);
    var sd = Math.sqrt(vsum / (n - 1));
    var vol = sd * Math.sqrt(ANN);
    var sharpe = sd > 0 ? (mean / sd) * Math.sqrt(ANN) : 0;
    var nv = nav[n];

    // 最大回撤
    var peak = nav[0], cumPeakIdx = 0, mdd = 0, mddP = 0, mddT = 0;
    for (i = 1; i <= n; i++) {
      if (nav[i] > peak) { peak = nav[i]; cumPeakIdx = i; }
      var ddv = nav[i] / peak - 1;
      if (ddv < mdd) { mdd = ddv; mddP = cumPeakIdx; mddT = i; }
    }
    function dOf(idx) { return bt.dates[idx === 0 ? 0 : idx - 1]; }

    // 月度胜率 / 分年度（nav[i] 对应 dOf(i)，但 nav[0] 属调仓日 T 所在月）
    var monthLast = {}, mOrder = [], yearLast = {}, yOrder = [];
    for (i = 0; i <= n; i++) {
      var ds = i === 0 ? prevDayStr(dates[0]) : dOf(i), mk = ds.slice(0, 7), yk = ds.slice(0, 4);
      if (!(mk in monthLast)) mOrder.push(mk);
      monthLast[mk] = nav[i];
      if (!(yk in yearLast)) yOrder.push(yk);
      yearLast[yk] = nav[i];
    }
    var mWin = 0, mTot = 0;
    for (k = 1; k < mOrder.length; k++) {
      mTot++;
      // 1e-9 容差: 与生成端口径一致, 避免把"当月零收益"计为上涨
      if (monthLast[mOrder[k]] / monthLast[mOrder[k - 1]] - 1 > 1e-9) mWin++;
    }
    var yearly = [];
    for (k = 0; k < yOrder.length; k++) {
      var y0 = k === 0 ? 1 : yearLast[yOrder[k - 1]];
      yearly.push({ year: parseInt(yOrder[k], 10), nav: yearLast[yOrder[k]], ret: yearLast[yOrder[k]] / y0 - 1 });
    }

    // 品种归因 + 单腿净值
    var attr = [], legNav = {};
    [["多头", "long_c", "long_ret"], ["空头", "short_c", "short_ret"]].forEach(function (spec) {
      var leg = spec[0], vals = {}, navL = 1, cnt = 0;
      periods.forEach(function (p) {
        var pr = p[spec[1]] ? p[spec[1]].slice(0, 2) : null;
        var r = p[spec[2]];
        if (r === null || !pr) return;
        (vals[pr] = vals[pr] || []).push(r);
        navL *= (1 + r);
        cnt++;
      });
      legNav[leg] = { nav: navL, n: cnt };
      PROD_ORDER.forEach(function (po) {
        var v = vals[po];
        if (!v || !v.length) return;
        var w = 0, cum = 1, s = 0;
        v.forEach(function (x) { if (x > 0) w++; cum *= (1 + x); s += x; });
        attr.push({ leg: leg, prod: po, n: v.length, win: w / v.length, avg: s / v.length, cum: cum - 1 });
      });
    });

    var st = { only_long: 0, only_short: 0, both: 0, none: 0 };
    periods.forEach(function (p) {
      if (p.oL && p.oS) st.both++;
      else if (p.oL) st.only_long++;
      else if (p.oS) st.only_short++;
      else st.none++;
    });

    var yrs = (Date.parse(dates[n - 1]) - Date.parse(dates[0])) / 86400000 / 365;
    return {
      dates: dates, comb: comb, nav: nav, periods: periods, yearly: yearly, attr: attr,
      legNav: legNav, structure: st,
      kpi: {
        nv: nv, years: yrs, annual: Math.pow(nv, 1 / yrs) - 1, vol: vol, sharpe: sharpe, mdd: mdd,
        mdd_peak: dOf(mddP), mdd_trough: dOf(mddT),
        win_rate: mTot > 0 ? mWin / mTot : 0
      }
    };
  }

  // ---------------- DOM: KPI ----------------
  var chartNav = null, chartYear = null;

  function renderKPI(r) {
    var k = r.kpi;
    var cards = [
      ["累计净值", num(k.nv, 3), "回测 " + num(k.years, 2) + " 年", false],
      ["年化收益", pct(k.annual), "几何年化", k.annual < 0],
      ["年化波动", num(k.vol * 100, 2) + "%", "日频×√252", false],
      ["夏普(rf=0)", num(k.sharpe, 2), "年化", k.sharpe < 0],
      ["最大回撤", pct(k.mdd), k.mdd_peak + " ~ " + k.mdd_trough, true],
      ["月度胜率", num(k.win_rate * 100, 1) + "%", "共 " + r.periods.length + " 个持仓期", false]
    ];
    document.getElementById("kpi-grid").innerHTML = cards.map(function (c) {
      return '<div class="kpi-card' + (c[0] === "最大回撤" ? " neg" : "") + '">'
        + '<div class="k">' + c[0] + '</div>'
        + '<div class="v' + (c[3] ? " neg" : "") + '">' + c[1] + '</div>'
        + '<div class="s">' + c[2] + '</div></div>';
    }).join("");
    document.getElementById("kpi-range").textContent = bt.eval_start + " ~ " + bt.eval_end;
    document.getElementById("kpi-periods").textContent = r.periods.length;
  }

  // ---------------- DOM: 净值 + 回撤图 ----------------
  function renderNavChart(r) {
    var dom = document.getElementById("chart-nav");
    if (!chartNav) chartNav = echarts.init(dom);
    var dates = r.dates, nav = r.nav.slice(1);
    var peak = nav[0], ddArr = nav.map(function (v) {
      if (v > peak) peak = v;
      return (v / peak - 1) * 100;
    });
    var k = r.kpi;
    chartNav.setOption({
      animation: false,
      tooltip: {
        trigger: "axis", axisPointer: { type: "cross", link: [{ xAxisIndex: "all" }] },
        formatter: function (ps) {
          var s = ps[0].axisValue + "<br/>";
          ps.forEach(function (p) {
            if (p.seriesName === "策略净值") s += p.marker + " 净值: " + p.value.toFixed(4) + "<br/>";
            if (p.seriesName === "回撤") s += p.marker + " 回撤: " + p.value.toFixed(2) + "%<br/>";
          });
          return s;
        }
      },
      legend: { data: ["策略净值", "回撤"], top: 4, textStyle: { fontSize: 11 } },
      grid: [
        { left: 62, right: 30, top: 40, height: "52%" },
        { left: 62, right: 30, top: "72%", height: "20%" }
      ],
      xAxis: [
        { type: "category", gridIndex: 0, data: dates, axisLabel: { show: false }, axisTick: { show: false } },
        {
          type: "category", gridIndex: 1, data: dates,
          axisLabel: { formatter: function (v) { return v.slice(0, 7); }, fontSize: 10, interval: Math.floor(dates.length / 12) }
        }
      ],
      yAxis: [
        { type: "value", gridIndex: 0, name: "净值", nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 10 }, scale: true, splitLine: { show: true } },
        { type: "value", gridIndex: 1, name: "回撤%", nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 10 }, splitLine: { show: false } }
      ],
      series: [
        {
          name: "策略净值", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: nav,
          smooth: true, symbol: "none", lineStyle: { color: "#1a237e", width: 2 }, itemStyle: { color: "#1a237e" },
          markPoint: {
            symbol: "pin", symbolSize: 44, data: [
              {
                coord: [dates[dates.length - 1], nav[nav.length - 1]], value: num(nav[nav.length - 1], 2),
                itemStyle: { color: "#1a237e" }, label: { color: "#fff", fontSize: 10 }
              },
              {
                coord: [k.mdd_trough, nav[dates.indexOf(k.mdd_trough)]], value: "最大回撤",
                itemStyle: { color: "#c62828" }, label: { color: "#fff", fontSize: 9 }
              }
            ]
          }
        },
        {
          name: "回撤", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: ddArr,
          symbol: "none", lineStyle: { color: "#c62828", width: 1.2 }, itemStyle: { color: "#c62828" },
          areaStyle: { color: "rgba(198,40,40,0.12)" }
        }
      ]
    });
    document.getElementById("nav-metrics").innerHTML =
      "全区间：累计净值 <b>" + num(k.nv, 3) + "</b> · 年化 <b>" + pct(k.annual) + "</b> · 年化波动 " + num(k.vol * 100, 2)
      + "% · 夏普 <b>" + num(k.sharpe, 2) + "</b> · 最大回撤 <b>" + pct(k.mdd) + "</b>（" + k.mdd_peak + " ~ " + k.mdd_trough + "）";
  }

  function renderYearChart(r) {
    var dom = document.getElementById("chart-year");
    if (!chartYear) chartYear = echarts.init(dom);
    var ys = r.yearly;
    chartYear.setOption({
      animation: false,
      tooltip: {
        trigger: "axis", formatter: function (ps) {
          var p = ps[0];
          return p.name + " 年<br/>年度收益: " + (p.value >= 0 ? "+" : "") + p.value.toFixed(2) + "%";
        }
      },
      grid: { left: 62, right: 30, top: 30, bottom: 34 },
      xAxis: { type: "category", data: ys.map(function (y) { return y.year; }), axisLabel: { fontSize: 11 } },
      yAxis: { type: "value", name: "收益%", nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 10, formatter: "{value}%" }, splitLine: { show: true } },
      series: [{
        type: "bar", barMaxWidth: 46,
        data: ys.map(function (y) {
          return { value: y.ret * 100, itemStyle: { color: y.ret >= 0 ? "#e53935" : "#43a047" } };
        }),
        label: {
          show: true, position: "top", fontSize: 10,
          formatter: function (p) { return (p.value >= 0 ? "+" : "") + p.value.toFixed(1); }
        }
      }]
    });
  }

  // ---------------- DOM: 调仓明细表 ----------------
  var tblSort = { k: "idx", asc: true };
  function legsLabel(p) {
    if (p.oL && p.oS) return "多+空";
    if (p.oL) return "仅多头";
    if (p.oS) return "仅空头";
    return "全空仓";
  }
  function renderPeriodTable(r) {
    var per = r.periods.map(function (p, i) {
      return { idx: i + 1, T: p.T, Tn: p.Tn, long_c: p.long_c, long_b: p.long_b, short_c: p.short_c, short_b: p.short_b, legs: legsLabel(p), ret: p.ret };
    });
    var k = tblSort.k, asc = tblSort.asc;
    per.sort(function (a, b) {
      var x = a[k], y = b[k];
      if (typeof x === "string") return asc ? (x < y ? -1 : x > y ? 1 : 0) : (x > y ? -1 : x < y ? 1 : 0);
      return asc ? x - y : y - x;
    });
    document.getElementById("period-count").textContent = r.periods.length;
    document.querySelector("#period-table tbody").innerHTML = per.map(function (p) {
      var lg = p.legs;
      var lgCls = lg === "全空仓" ? "flat" : (lg === "仅空头" ? "short" : "open");
      return "<tr>"
        + "<td>" + p.idx + "</td><td>" + p.T + "</td><td>" + p.Tn + "</td>"
        + "<td><b>" + p.long_c + "</b></td><td>" + num(p.long_b, 2) + "</td>"
        + "<td><b>" + p.short_c + "</b></td><td>" + num(p.short_b, 2) + "</td>"
        + '<td><span class="badge ' + lgCls + '">' + lg + "</span></td>"
        + '<td class="' + (p.ret >= 0 ? "profit-pos" : "profit-neg") + '">' + num(p.ret * 100, 2) + "</td>"
        + "</tr>";
    }).join("");
    Array.prototype.forEach.call(document.querySelectorAll("#period-table th.sortable"), function (th) {
      th.onclick = function () {
        var key = th.getAttribute("data-k");
        tblSort = { k: key, asc: (tblSort.k === key ? !tblSort.asc : true) };
        renderPeriodTable(r);
      };
    });
  }

  function renderAttr(r) {
    var lgCls = { "多头": "open", "空头": "short" };
    document.querySelector("#attr-table tbody").innerHTML = r.attr.map(function (a) {
      return "<tr>"
        + '<td><span class="badge ' + lgCls[a.leg] + '">' + a.leg + "</span></td>"
        + "<td>" + a.prod + "（" + PROD_NAME[a.prod] + "）</td><td>" + a.n + "</td>"
        + "<td>" + num(a.win, 3) + "</td>"
        + '<td class="' + (a.avg >= 0 ? "profit-pos" : "profit-neg") + '">' + num(a.avg * 100, 3) + "</td>"
        + '<td class="' + (a.cum >= 0 ? "profit-pos" : "profit-neg") + '">' + num(a.cum * 100, 2) + "</td>"
        + "</tr>";
    }).join("");
    var ln = r.legNav["多头"], sn = r.legNav["空头"], st = r.structure;
    document.getElementById("leg-nav-note").innerHTML =
      "多头腿累计净值 <b>" + num(ln.nav, 3) + "</b>（开仓 " + ln.n + " 期） · 空头腿累计净值 <b>" + num(sn.nav, 3)
      + "</b>（开仓 " + sn.n + " 期） · 开仓结构：仅多头 " + st.only_long + " / 仅空头 " + st.only_short
      + " / 双腿 " + st.both + " / 全空仓 " + st.none + " 期。空头腿净值为做空盈亏（> 1 表示该腿净盈利）。";
  }

  function renderDesc(r) {
    var lo = state.lo, sh = state.sh;
    document.getElementById("strategy-desc").innerHTML =
      "<h4>何时开仓</h4>"
      + "<p>调仓日为<b>每个自然月的最后一个交易日</b>，按当日收盘价换仓。在每个调仓日 T，对 <b>IH / IF / IC / IM 四个品种的全部在挂月份合约</b>（跨品种统一排名，含远月）计算年化基差率：</p>"
      + "<p><code>升贴水率 = 期货价格 / 对应现货指数 − 1</code>；<code>年化基差率 b = 升贴水率 × 365 / 剩余自然日</code>（剩余自然日 = 合约到期日 − 调仓日；到期日按「到期月第三个星期五，遇非交易日顺延」推算）。</p>"
      + "<p>候选池限定为<b>到期日 ≥ 下一个调仓日</b>的合约（保证整段持仓期内不换月）。随后：</p>"
      + "<ul>"
      + "<li><b>做多腿</b>：取 b 最小者（贴水最深）。只有当 <b>b ≤ " + num(lo, 2) + "%</b> 时才真正开仓做多；否则该腿空仓、当期收益记 0。</li>"
      + "<li><b>做空腿</b>：取 b 最大者（升水最高）。只有当 <b>b ≥ " + num(sh, 2) + "%</b> 时才真正开仓做空；否则该腿空仓、当期收益记 0。</li>"
      + "<li>两腿各按 <b>100% 名义本金</b>，若两腿同时开仓则合计约 <b>2 倍名义杠杆</b>；两腿均空仓时组合当期收益为 0（等价现金）。</li>"
      + "<li>候选合约不足 2 个的调仓日直接跳过，不建仓。</li>"
      + "</ul>"
      + "<h4>何时平仓</h4>"
      + "<p>持仓期内<b>不做任何盘中判断</b>：只要该腿在调仓日开仓，就一直持有到下一个调仓日 T+1（候选池已保证合约不会在持仓期内到期，故无需展期）。在下一个调仓日按新一期规则重新选合约并重设两腿开关状态，隐含地完成平仓与换仓。</p>"
      + "<h4>历史业绩</h4>"
      + "<p>回测区间 <b>" + bt.eval_start + " ~ " + bt.eval_end + "</b>（" + num(r.kpi.years, 2) + " 年，" + r.periods.length + " 个持仓期）：累计净值 <b>"
      + num(r.kpi.nv, 3) + "</b>、年化 <b>" + pct(r.kpi.annual) + "</b>、年化波动 " + num(r.kpi.vol * 100, 2)
      + "%、夏普 <b>" + num(r.kpi.sharpe, 2) + "</b>、最大回撤 <b>" + pct(r.kpi.mdd) + "</b>、月度胜率 " + num(r.kpi.win_rate * 100, 1)
      + "%。开仓结构：仅多头 " + r.structure.only_long + " 期 / 仅空头 " + r.structure.only_short + " 期 / 双腿 " + r.structure.both + " 期 / 全空仓 " + r.structure.none + " 期。</p>"
      + "<h4>阈值的作用（V0 对照）</h4>"
      + "<p>若把两条腿的门槛去掉、<b>始终开仓</b>（记作 V0 基准），同期累计净值仅 <b>" + num(bt.v0.nv, 3) + "</b>、年化 "
      + pct(bt.v0.annual / 100) + "、夏普 " + num(bt.v0.sharpe, 2) + "、月胜率 " + num(bt.v0.win_rate, 1)
      + "%。关键差别在空头腿：长期做空股指期货（多数时候贴水）是负收益来源，加上「真升水才做空」的门槛后空头腿才转为正贡献。</p>"
      + "<h4>已知局限（如实标注）</h4>"
      + "<ul>"
      + "<li>阈值 " + num(bt.lo, 2) + "% / " + num(bt.sh, 2) + "% 为<b>样本内选定</b>，存在过拟合风险。</li>"
      + "<li>最大回撤发生在 2018–2019 年的单腿裸多阶段，方向暴露集中。</li>"
      + "<li>约 2 倍杠杆，未计保证金占用、资金成本与强平风险。</li>"
      + "<li>回测<b>未计任何交易成本</b>（手续费 / 冲击 / 展期）。</li>"
      + "<li>换仓按调仓日收盘价成交，实盘不可完全实现。</li>"
      + "</ul>";
  }

  function renderParams(r) {
    var rows = [
      ["调仓频率", "换仓时点", "每月最后一个交易日（日频盯市）"],
      ["多头阈值", "年化基差率 ≤ 该值才开多", num(state.lo, 2) + "%"],
      ["空头阈值", "年化基差率 ≥ 该值才开空", num(state.sh, 2) + "%"],
      ["候选池约束", "合约到期日不得早于", "下一个调仓日"],
      ["年化口径", "基差年化用自然日", FACTOR + " 日"],
      ["波动/夏普口径", "日频收益年化", "√" + ANN],
      ["单腿仓位", "开仓腿名义本金", "100%（两腿合计约 2 倍）"],
      ["空仓腿处理", "未达阈值", "当期收益记 0"],
      ["合约范围", "参与排名的合约", "IH/IF/IC/IM 全部在挂月份合约"],
      ["评价期", "回测净值区间", bt.eval_start + " ~ " + bt.eval_end],
      ["交易成本", "手续费/冲击/展期", "未计"]
    ];
    document.querySelector("#params-table tbody").innerHTML = rows.map(function (p) {
      return "<tr><td>" + p[0] + "</td><td>" + p[1] + "</td><td>" + p[2] + "</td></tr>";
    }).join("");

    var d = bt.kpis;
    var dev = Math.max(
      Math.abs(d.nv - r.kpi.nv) / d.nv,
      Math.abs(d.annual - r.kpi.annual * 100) / Math.abs(d.annual),
      Math.abs(d.sharpe - r.kpi.sharpe) / Math.abs(d.sharpe)
    );
    document.getElementById("param-note").innerHTML = (state.lo === bt.lo && state.sh === bt.sh)
      ? "当前为默认阈值，客户端重算与生成端一致（最大偏差 " + (dev * 100).toFixed(2) + "%）。"
      : "已按自定义阈值重算（生成端默认结果：累计 " + num(d.nv, 3) + " / 夏普 " + num(d.sharpe, 2) + "）。";
  }

  // ---------------- 回测整体渲染 ----------------
  var current = null;
  function renderBacktest() {
    var r = rebuild(state.lo, state.sh);
    current = r;
    renderKPI(r);
    renderNavChart(r);
    renderYearChart(r);
    renderPeriodTable(r);
    renderAttr(r);
    renderDesc(r);
    renderParams(r);
    document.getElementById("data-source").textContent =
      "回测数据源：中金所官方日行情（全部月份合约） + 对应现货指数收盘（via akshare） | 数据生成于 " + bt.generated_at
      + " | 实时行情：东方财富";
  }

  // ---------------- 实时行情 ----------------
  function nextRebal(todayStr) {
    var keys = Object.keys(live.month_ends).sort();
    for (var i = 0; i < keys.length; i++) {
      if (live.month_ends[keys[i]] > todayStr) return live.month_ends[keys[i]];
    }
    return null;
  }

  function fetchAll() {
    var codes = live.contracts.map(function (x) { return x.c; });
    var secids = codes.map(function (c) { return "220." + c; })
      .concat(PROD_ORDER.map(function (p) { return "1." + PROD2SPOT[p]; }));
    var url = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields=f12,f13,f14,f2,f3,f124&secids="
      + secids.join(",");
    return withTimeout(
      fetch(url).then(function (r) { return r.json(); }).then(function (j) {
        var diff = j && j.data && j.data.diff;
        if (!diff || !diff.length) return null;
        var px = {}, ts = 0;
        diff.forEach(function (x) {
          px[x.f12] = { p: x.f2, chg: x.f3, name: x.f14 };
          if (x.f124 > ts) ts = x.f124;
        });
        return { px: px, ts: ts };
      }), 12000);
  }

  // 用给定实时价(或数据文件收盘)计算全市场基差与两腿建议
  function analyse(todayStr, px) {
    var Tn = nextRebal(todayStr);
    var rows = [];
    live.contracts.forEach(function (ct) {
      if (ct.expiry < todayStr) return;                       // 已到期
      var d = daysBetween(todayStr, ct.expiry);
      if (d <= 0) return;
      var q = px && px[ct.c];
      var sp = live.spot[ct.prod];
      var qs = px && px[sp.code];
      var F = (q && q.p > 0) ? q.p : ct.close;
      var S = (qs && qs.p > 0) ? qs.p : sp.close;
      if (!(F > 0) || !(S > 0)) return;
      var prem = F / S - 1;
      rows.push({
        c: ct.c, prod: ct.prod, expiry: ct.expiry, d: d, F: F, S: S,
        prem: prem, b: prem * FACTOR / d, cand: !!(Tn && ct.expiry >= Tn),
        rtF: !!(q && q.p > 0), rtS: !!(qs && qs.p > 0)
      });
    });
    rows.sort(function (a, b) { return a.b - b.b; });
    var cands = rows.filter(function (r) { return r.cand; });
    var L = cands.length >= 2 ? cands[0] : null;
    var S = cands.length >= 2 ? cands[cands.length - 1] : null;
    return { Tn: Tn, rows: rows, cands: cands, L: L, S: S, px: px || null };
  }

  function legTag(open) {
    return open ? '<span class="pos-tag open">开仓</span>' : '<span class="pos-tag flat">空仓</span>';
  }

  function renderLive(an, stageText, srcNote) {
    var lo = state.lo, sh = state.sh;
    lastStage = stageText;
    lastSrc = srcNote;

    // ---- ② 若今日调仓 ----
    var todayStr = fmtDate(bjNow());
    document.getElementById("live-period").textContent = an.Tn
      ? ("拟持仓区间 " + todayStr + " → " + an.Tn + "（" + daysBetween(todayStr, an.Tn) + " 天后换仓）")
      : "";
    var rows2;
    if (!an.L || !an.S) {
      rows2 = '<tr><td colspan="5">候选合约不足 2 个，本期不建仓</td></tr>';
    } else {
      var loOK = an.L.b <= lo, shOK = an.S.b >= sh;
      rows2 =
        "<tr><td class='l'><b>多头腿</b></td><td><b>" + an.L.c + "</b>（" + an.L.prod + "）</td>"
        + '<td class="' + (an.L.b < 0 ? "profit-pos" : "profit-neg") + '">' + num(an.L.b * 100, 2) + "%</td>"
        + '<td><span class="badge ' + (loOK ? "open" : "flat") + '">' + (loOK ? "≤ " + num(lo, 2) + "%，达标" : "> " + num(lo, 2) + "%，不达标") + "</span></td>"
        + "<td>" + legTag(loOK) + "</td></tr>"
        + "<tr><td class='l'><b>空头腿</b></td><td><b>" + an.S.c + "</b>（" + an.S.prod + "）</td>"
        + '<td class="' + (an.S.b > 0 ? "profit-neg" : "profit-pos") + '">' + num(an.S.b * 100, 2) + "%</td>"
        + '<td><span class="badge ' + (shOK ? "open" : "flat") + '">' + (shOK ? "≥ " + num(sh, 2) + "%，达标" : "< " + num(sh, 2) + "%，不达标") + "</span></td>"
        + "<td>" + legTag(shOK) + "</td></tr>";
    }
    document.querySelector("#live-table tbody").innerHTML = rows2;
    document.getElementById("live-note").innerHTML =
      "全市场共 " + an.rows.length + " 个在挂合约，其中 <b>" + an.cands.length + "</b> 个满足「到期日 ≥ "
      + (an.Tn || "-") + "」进入候选池。候选池内跨品种统一排名，取年化基差率最小者为多头候选、最大者为空头候选。";

    // ---- ① 当前持仓期 ----
    var lr = live.last_rebalance;
    function pxOf(code, fallback) {
      for (var i = 0; i < an.rows.length; i++) if (an.rows[i].c === code) return an.rows[i].F;
      return fallback;
    }
    var fl = pxOf(lr.long_c, lr.long_close);
    var fs = pxOf(lr.short_c, lr.short_close);
    // long_close0/short_close0 = 调仓日 T 收盘; long_close/short_close = 数据文件最新收盘
    var c0L = lr.long_close0 > 0 ? lr.long_close0 : lr.long_close;
    var c0S = lr.short_close0 > 0 ? lr.short_close0 : lr.short_close;
    var holdL = c0L > 0 ? fl / c0L - 1 : 0;                       // 自调仓日起的多头腿收益
    var holdS = c0S > 0 ? fs / c0S - 1 : 0;                       // 自调仓日起的空头腿价格变动
    var cumL = lr.open_l ? holdL : null;                          // 开仓腿当期收益
    var cumS = lr.open_s ? -holdS : null;                         // 空头腿当期收益
    var todayL = lr.long_close > 0 ? fl / lr.long_close - 1 : 0;  // 数据文件最新收盘之后的变动
    var todayS = lr.short_close > 0 ? fs / lr.short_close - 1 : 0;
    var todayC = (lr.open_l ? todayL : 0) - (lr.open_s ? todayS : 0);
    var cumC = lr.nav_comb * (1 + todayC) - 1;

    document.getElementById("cur-period").textContent =
      "持仓期 " + lr.T + " → " + lr.Tn + "（预计下次调仓，已持有 " + lr.n_days + " 个交易日）";
    document.querySelector("#cur-table tbody").innerHTML =
      "<tr><td class='l'><b>多头腿</b></td><td><b>" + lr.long_c + "</b></td><td>" + num(lr.long_b, 2) + "%</td>"
      + "<td>" + num(c0L, 1) + "</td><td>" + num(fl, 1) + "</td>"
      + "<td class='" + (cumL === null ? "" : (cumL >= 0 ? "profit-pos" : "profit-neg")) + "'>"
      + (cumL === null ? "-" : num(cumL * 100, 2) + "%") + "</td>"
      + "<td>" + legTag(lr.open_l) + "</td></tr>"
      + "<tr><td class='l'><b>空头腿</b></td><td><b>" + lr.short_c + "</b></td><td>" + num(lr.short_b, 2) + "%</td>"
      + "<td>" + num(c0S, 1) + "</td><td>" + num(fs, 1) + "</td>"
      + "<td class='" + (cumS === null ? "" : (cumS >= 0 ? "profit-pos" : "profit-neg")) + "'>"
      + (cumS === null ? "-" : num(cumS * 100, 2) + "%") + "</td>"
      + "<td>" + legTag(lr.open_s) + "</td></tr>";
    document.getElementById("cur-pnl").innerHTML =
      "按当前价估算，本持仓期组合浮动盈亏 <b class='" + (cumC >= 0 ? "profit-pos" : "profit-neg") + "'>"
      + num(cumC * 100, 2) + "%</b>（" + lr.T + " → 数据末 " + live.trade_date + " 为 "
      + num((lr.nav_comb - 1) * 100, 2) + "%，之后按实时价再变动 " + num(todayC * 100, 2) + "%）。";

    // ---- 排名表 ----
    var Lc = an.L ? an.L.c : null, Sc = an.S ? an.S.c : null;
    document.querySelector("#rank-table tbody").innerHTML = an.rows.map(function (r, i) {
      var tag = r.c === Lc ? '<span class="badge open">贴水最深(多头)</span>'
        : r.c === Sc ? '<span class="badge short">升水最高(空头)</span>'
          : (r.cand ? '<span class="badge flat">候选</span>' : '<span class="badge flat">到期日不足</span>');
      return "<tr>"
        + "<td>" + (i + 1) + "</td><td><b>" + r.c + "</b></td><td>" + r.prod + "（" + PROD_NAME[r.prod] + "）</td>"
        + "<td>" + r.expiry + "</td><td>" + r.d + "</td>"
        + "<td>" + num(r.F, 1) + (r.rtF ? "" : "<span style='color:#bbb'>*</span>") + "</td>"
        + "<td>" + num(r.S, 2) + (r.rtS ? "" : "<span style='color:#bbb'>*</span>") + "</td>"
        + '<td class="' + (r.prem >= 0 ? "profit-neg" : "profit-pos") + '">' + num(r.prem * 100, 3) + "%</td>"
        + '<td class="' + (r.b >= 0 ? "profit-neg" : "profit-pos") + '"><b>' + num(r.b * 100, 2) + "%</b></td>"
        + "<td>" + tag + "</td></tr>";
    }).join("");

    document.getElementById("realtime-status").textContent = stageText + " · 重算于 " + fmtTime(bjNow());
    document.getElementById("rt-source").innerHTML = (srcNote || "")
      + " 带 <span style='color:#bbb'>*</span> 的价格为数据文件最新收盘（未取到实时行情）。"
      + " 上一个调仓日 " + lr.T + "，数据文件最新交易日 " + live.trade_date + "。";
  }

  // ---------------- 时段调度 ----------------
  function phaseOf(m) {
    if (m < 570) return "pre";
    if (m < 690) return "am";
    if (m < 780) return "lunch";
    if (m < 900) return "pm";
    if (m < 960) return "settle";
    return "closed";
  }

  var lastLive = null, lastStage = "", lastSrc = "";
  function refresh(stageText, done) {
    fetchAll().then(function (res) {
      var todayStr = fmtDate(bjNow());
      if (!res) {
        lastLive = analyse(todayStr, null);
        renderLive(lastLive, stageText + "（实时行情不可用）",
          "东方财富接口未返回数据，已回退到数据文件最新收盘价。");
      } else {
        lastLive = analyse(todayStr, res.px);
        var qtime = res.ts
          ? fmtTime(new Date(res.ts * 1000 + (new Date().getTimezoneOffset() + 480) * 60000))
          : "-";
        renderLive(lastLive, stageText, "行情时间 " + qtime + "。");
      }
      if (done) done();
    });
  }

  function loop() {
    var bj = bjNow(), m = bj.getHours() * 60 + bj.getMinutes(), ph = phaseOf(m);
    var secsLeft = Math.max(60000 - bj.getSeconds() * 1000 - bj.getMilliseconds(), 5000);
    if (ph === "am" || ph === "pm") {
      refresh(ph === "am" ? "盘中实时估算（上午）" : "盘中实时估算（下午）");
      setTimeout(loop, secsLeft);
    } else if (ph === "lunch") {
      if (lastLive) renderLive(lastLive, "午间休市", "午间休市，13:00 恢复每分钟更新。");
      setTimeout(loop, Math.max((780 - m) * 60000 - bj.getSeconds() * 1000, 5000));
    } else if (ph === "pre") {
      lastLive = analyse(fmtDate(bj), null);
      renderLive(lastLive, "盘前（按上一交易日收盘）", "尚未开盘，使用数据文件最新收盘价估算。");
      setTimeout(loop, Math.max((570 - m) * 60000 - bj.getSeconds() * 1000, 5000));
    } else if (ph === "settle") {
      refresh("已收盘（按 15:00 收盘价）");
      setTimeout(loop, Math.max((960 - m) * 60000 - bj.getSeconds() * 1000, 5000));
    } else {
      refresh("已收盘（按 16:00 收盘价定稿）", function () {
        document.getElementById("realtime-status").textContent =
          "已按收盘价定稿 · 16:00 后不再更新（刷新页面可重算） · 重算于 " + fmtTime(bjNow());
      });
    }
  }

  // ---------------- 交互：阈值 ----------------
  document.getElementById("btn-apply").onclick = function () {
    var lo = parseFloat(document.getElementById("in-lo").value);
    var sh = parseFloat(document.getElementById("in-sh").value);
    if (!isFinite(lo) || !isFinite(sh)) { alert("请输入有效的阈值数字"); return; }
    state.lo = lo;
    state.sh = sh;
    renderBacktest();
    if (lastLive) renderLive(analyse(fmtDate(bjNow()), lastLive.px), lastStage, lastSrc);
  };
  document.getElementById("btn-reset").onclick = function () {
    document.getElementById("in-lo").value = bt.lo;
    document.getElementById("in-sh").value = bt.sh;
    state.lo = bt.lo;
    state.sh = bt.sh;
    renderBacktest();
    if (lastLive) renderLive(analyse(fmtDate(bjNow()), lastLive.px), lastStage, lastSrc);
  };
  window.addEventListener("resize", function () {
    if (chartNav) chartNav.resize();
    if (chartYear) chartYear.resize();
  });

  // ---------------- 启动 ----------------
  renderBacktest();
  loop();
})();
