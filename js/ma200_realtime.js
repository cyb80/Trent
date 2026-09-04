/* MA200 盘中实时持仓计算 (js/ma200_realtime.js)
 * 读取 window.MA200_LIVE(含双指数近~430日收盘) + window.MA200_BACKTEST(回测净值)。
 * 每次页面加载/刷新即用实时行情更新一次:
 *   将"实时价作为当日收盘"拼接到收盘序列, 重算 200日均线趋势(连续5日确认)+低波动,
 *   得到当时应持有的 IC/IM 目标权重与总敞口, 用于指导调仓。
 * 实时源: 东方财富 push2 (CORS开放) → 腾讯行情 JSONP 兜底。
 */
(function () {
  "use strict";
  var back = window.MA200_BACKTEST;
  var live = window.MA200_LIVE;
  if (!back || !live) return;

  var MA = live.ma || 200;
  var CONF = live.confirm_days || 5;
  var VWIN = live.vol60 || 60;
  var MWIN = live.median_win || 252;
  var ANNUAL = 242;

  // 实时行情代码映射
  var SECID = { IC: "1.000905", IM: "1.000852" };
  var TXCOD = { IC: "sh000905", IM: "sh000852" };
  var LEG_ORDER = ["IC", "IM"];

  // ---------- 工具 ----------
  function fmtTime(d) {
    function p2(x) {
      return x < 10 ? "0" + x : "" + x;
    }
    return (
      d.getFullYear() +
      "-" +
      p2(d.getMonth() + 1) +
      "-" +
      p2(d.getDate()) +
      " " +
      p2(d.getHours()) +
      ":" +
      p2(d.getMinutes()) +
      ":" +
      p2(d.getSeconds())
    );
  }
  function fmtDate(d) {
    function p2(x) {
      return x < 10 ? "0" + x : "" + x;
    }
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate());
  }
  function avg(a) {
    var s = 0;
    for (var i = 0; i < a.length; i++) s += a[i];
    return s / a.length;
  }
  function sampleStd(a) {
    if (a.length < 2) return NaN;
    var m = avg(a),
      s = 0,
      i;
    for (i = 0; i < a.length; i++) s += (a[i] - m) * (a[i] - m);
    return Math.sqrt(s / (a.length - 1));
  }
  function rollingStd(arr, win) {
    var out = new Array(arr.length).fill(NaN),
      i;
    for (i = win - 1; i < arr.length; i++)
      out[i] = sampleStd(arr.slice(i - win + 1, i + 1));
    return out;
  }
  function rollingMed(arr, win, minP) {
    var out = new Array(arr.length).fill(NaN),
      i;
    for (i = 0; i < arr.length; i++) {
      var w = arr.slice(Math.max(0, i - win + 1), i + 1),
        vals = [];
      for (var k = 0; k < w.length; k++) if (isFinite(w[k])) vals.push(w[k]);
      if (vals.length >= minP) {
        vals.sort(function (a, b) {
          return a - b;
        });
        out[i] = vals[Math.floor(vals.length / 2)];
      }
    }
    return out;
  }
  function rollingMean(arr, win) {
    var out = new Array(arr.length).fill(NaN),
      i,
      s = 0;
    for (i = 0; i < arr.length; i++) {
      s += arr[i];
      if (i >= win) s -= arr[i - win];
      if (i >= win - 1) out[i] = s / win;
    }
    return out;
  }
  function confirmedTrend(raw) {
    // 与 fetch_ma200.py confirmed_trend 一致
    var state = new Array(raw.length).fill(0),
      cur = null,
      streak = 0,
      i;
    for (i = 0; i < raw.length; i++) {
      var v = raw[i] ? 1 : 0;
      if (cur === null) {
        streak = 1;
        cur = v;
      } else if (v === cur) {
        streak += 1;
      } else {
        streak = 1;
        cur = v;
      }
      if (streak >= CONF) cur = v;
      state[i] = cur;
    }
    return state;
  }
  function targetWeights(icS, imS, icLv, imLv) {
    if (icS && imS) {
      if (icLv && imLv)
        return {
          ic: 0.6,
          im: 0.6,
          ex: 1.2,
          tag: "双强·低波动",
          pos: "高杠杆1.2倍",
          cls: "strong",
        };
      return {
        ic: 0.5,
        im: 0.5,
        ex: 1.0,
        tag: "双强",
        pos: "标准1倍敞口",
        cls: "strong",
      };
    }
    if (icS && !imS)
      return {
        ic: 0.75,
        im: 0.25,
        ex: 1.0,
        tag: "IC强·IM弱",
        pos: "偏IC持仓",
        cls: "flat",
      };
    if (!icS && imS)
      return {
        ic: 0.25,
        im: 0.75,
        ex: 1.0,
        tag: "IC弱·IM强",
        pos: "偏IM持仓",
        cls: "flat",
      };
    return {
      ic: 0.125,
      im: 0.125,
      ex: 0.25,
      tag: "双弱",
      pos: "降仓至0.25倍",
      cls: "weak",
    };
  }

  // ---------- 重算某一腿的最新状态 ----------
  function calcLeg(closes) {
    var n = closes.length,
      i;
    var ma = rollingMean(closes, MA);
    var raw = new Array(n).fill(0);
    for (i = 0; i < n; i++)
      raw[i] = isFinite(ma[i]) && closes[i] > ma[i] ? 1 : 0;
    var conf = confirmedTrend(raw);
    var ret = new Array(n).fill(NaN);
    for (i = 1; i < n; i++)
      ret[i] = (closes[i] - closes[i - 1]) / closes[i - 1];
    var vol60 = rollingStd(ret, VWIN).map(function (x) {
      return isFinite(x) ? x * Math.sqrt(ANNUAL) : NaN;
    });
    var med = rollingMed(vol60, MWIN, Math.floor(MWIN / 2));
    var last = n - 1;
    return {
      close: closes[last],
      ma200: ma[last],
      strong: conf[last] === 1,
      rawStrong: raw[last] === 1,
      lowvol: !!(
        isFinite(vol60[last]) &&
        isFinite(med[last]) &&
        vol60[last] < med[last]
      ),
      vol60: vol60[last],
      med: med[last],
    };
  }

  // ---------- DOM 渲染 ----------
  var stateEls = {
    IC: ["ic-close", "ic-ma200", "ic-trend", "ic-lowvol", "ic-weight"],
    IM: ["im-close", "im-ma200", "im-trend", "im-lowvol", "im-weight"],
  };

  function renderPosition(snaps, header) {
    var w = targetWeights(
      snaps.IC.strong,
      snaps.IM.strong,
      snaps.IC.lowvol,
      snaps.IM.lowvol,
    );
    var banner = document.getElementById("pos-banner");
    var tagCls = w.cls,
      tagTxt = w.tag;
    document.getElementById("pos-label").innerHTML =
      '建议持仓：<span class="pos-tag ' +
      tagCls +
      '">' +
      tagTxt +
      "</span> &nbsp;" +
      w.pos;
    document.getElementById("pos-desc").textContent =
      "按最新行情估算 · " +
      header +
      " | IC权重 " +
      w.ic * 100 +
      "% / IM权重 " +
      w.im * 100 +
      "% / 总敞口 " +
      w.ex +
      "倍";

    document.getElementById("weight-row").innerHTML =
      '<div class="weight-item"><div class="k">IC 目标权重</div><div class="v">' +
      w.ic * 100 +
      "%</div></div>" +
      '<div class="weight-item"><div class="k">IM 目标权重</div><div class="v">' +
      w.im * 100 +
      "%</div></div>" +
      '<div class="weight-item"><div class="k">总名义敞口</div><div class="v expo">' +
      w.ex +
      " 倍</div></div>";

    var tbody = document.querySelector("#mini-table tbody");
    var html = "";
    LEG_ORDER.forEach(function (leg) {
      var s = snaps[leg];
      var c = isFinite(s.close) ? s.close.toFixed(2) : "-";
      var ma = isFinite(s.ma200) ? s.ma200.toFixed(2) : "-";
      html +=
        "<tr>" +
        "<td>" +
        live.spot[leg].name +
        "</td>" +
        "<td>" +
        c +
        "</td><td>" +
        ma +
        "</td>" +
        '<td><span class="badge ' +
        (s.strong ? "strong" : "weak") +
        '">' +
        (s.strong ? "强势" : "弱势") +
        "</span></td>" +
        '<td><span class="badge ' +
        (s.lowvol ? "yes" : "no") +
        '">' +
        (s.lowvol ? "低波动" : "非低波动") +
        "</span></td>" +
        "<td>" +
        (leg === "IC" ? w.ic : w.im) * 100 +
        "%</td>" +
        "</tr>";
    });
    tbody.innerHTML = html;
    return w;
  }

  function renderSnapshot(snapshot, header, srcNote) {
    if (!snapshot || !snapshot.ic || !snapshot.im) return;
    var snaps = { IC: snapshot.ic, IM: snapshot.im };
    var w = renderPosition(snaps, header);
    document.getElementById("rt-source").textContent = srcNote || "";
    return w;
  }

  // ---------- 实时行情 ----------
  function withTimeout(promise, ms, tag) {
    return new Promise(function (resolve) {
      var done = false;
      var t = setTimeout(function () {
        if (!done) {
          done = true;
          resolve({ error: "请求超时(" + (tag || "") + ")" });
        }
      }, ms);
      promise.then(function (v) {
        if (!done) {
          done = true;
          clearTimeout(t);
          resolve(v);
        }
      });
    });
  }
  function fetchEM(secid, leg) {
    var url =
      "https://push2.eastmoney.com/api/qt/stock/get?secid=" +
      secid +
      "&invt=2&fltt=2&fields=f43,f44,f45,f47,f57,f58,f60,f86";
    return withTimeout(
      fetch(url)
        .then(function (r) {
          return r.json();
        })
        .then(function (j) {
          var d = j && j.data;
          if (!d || !d.f43 || d.f43 <= 0) return { error: "无有效数据" };
          return {
            price: d.f43,
            high: d.f44 || d.f43,
            low: d.f45 || d.f43,
            time: Date.now(),
          };
        })
        .catch(function (e) {
          return { error: e && e.message ? e.message : String(e) };
        }),
      10000,
      leg,
    );
  }
  function fetchTX(code, leg) {
    return new Promise(function (resolve) {
      try {
        delete window["v_" + code];
      } catch (e) {
        window["v_" + code] = undefined;
      }
      var sc = document.createElement("script");
      sc.charset = "GBK";
      sc.src = "https://qt.gtimg.cn/q=" + code + "&_=" + Date.now();
      sc.onload = function () {
        var s = window["v_" + code] || "";
        var f = s.split("~"),
          price = parseFloat(f[3]);
        if (!(price > 0)) {
          resolve({ error: "接口返回为空" });
          sc.remove();
          return;
        }
        resolve({ price: price, time: Date.now() });
        sc.remove();
      };
      sc.onerror = function () {
        resolve({ error: "网络错误" });
      };
      document.head.appendChild(sc);
    });
  }
  function fetchRT(leg) {
    return fetchEM(SECID[leg], leg).then(function (em) {
      if (em && em.price > 0) return em;
      return withTimeout(fetchTX(TXCOD[leg], leg), 10000, leg).then(
        function (tx) {
          return tx && tx.price > 0 ? tx : em || tx || null;
        },
      );
    });
  }

  // ---------- 盘中重算 ----------
  function bjDate() {
    var now = new Date();
    return new Date(now.getTime() + (480 + now.getTimezoneOffset()) * 60000);
  }
  var lastSource = "";

  function recompute() {
    var now = new Date();
    var curMin = now.getHours() * 60 + now.getMinutes();
    var doneTime = fmtTime(now);
    document.getElementById("realtime-status").textContent =
      "重算于 " + doneTime + " （页面刷新时更新）";

    // 按数据文件最新收盘快照先渲染（兜底）
    var w0 = renderSnapshot(
      live.snap,
      "最新收盘(" + live.snap.date + ")",
      "于 " + doneTime + " 使用数据文件最新收盘",
    );

    // 实时刷新
    Promise.all(
      LEG_ORDER.map(function (leg) {
        return fetchRT(leg).then(function (rt) {
          return { leg: leg, rt: rt };
        });
      }),
    ).then(function (list) {
      var snaps = {};
      var allOk = true,
        source = "";
      list.forEach(function (item) {
        var leg = item.leg,
          rt = item.rt;
        var closes = live.spot[leg].close.slice();
        if (rt && rt.price > 0) {
          closes.push(rt.price);
          source = source || "实时行情";
        } else {
          allOk = false;
        }
        snaps[leg] = calcLeg(closes);
      });
      if (allOk && list.length === 2) {
        var todayStr = fmtDate(bjDate());
        var stage =
          curMin < 540
            ? "盘前(用上个交易日)"
            : curMin >= 900
              ? "已收盘(用今日实时收盘)"
              : "盘中实时估算";
        renderPosition(snaps, stage + " · 实时数据");
        document.getElementById("rt-source").textContent =
          "作为当日收盘的实时价: IC=" +
          snaps.IC.close.toFixed(2) +
          " IM=" +
          snaps.IM.close.toFixed(2) +
          " | " +
          todayStr +
          " | 低波动为现货收盘收益近似";
      } else {
        document.getElementById("rt-source").textContent =
          "实时行情获取失败，已回退到最新收盘快照(上一步)。" +
          list
            .map(function (x) {
              return x.leg + ":" + x.rt.error;
            })
            .join(" ");
      }
    });
  }

  // ---------- 图表与静态信息 ----------
  function renderNavChart() {
    var dom = document.getElementById("chart-nav");
    var chart = echarts.init(dom);
    var dates = back.dates,
      base = dates.length - 1;
    // 历史比值: 策略净值 / 买入持有中证1000净值
    var ratio = back.strat_nav.map(function (v, i) {
      return back.bench_nav[i] > 0 ? v / back.bench_nav[i] : null;
    });
    document.getElementById("nav-title").textContent =
      "回测净值曲线（策略 vs 中证1000基准 · " +
      back.eval_start +
      " ~ " +
      back.eval_end +
      "）";
    chart.setOption({
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross", link: [{ xAxisIndex: "all" }] },
      },
      legend: {
        data: [
          "MA200策略",
          "中证1000(买入持有)",
          "策略/基准相对净值",
          "历史总敞口",
        ],
        top: 5,
        textStyle: { fontSize: 10 },
      },
      grid: [
        { left: 60, right: 55, top: 55, height: "55%" },
        { left: 60, right: 55, top: "75%", height: "19%" },
      ],
      xAxis: [
        {
          type: "category",
          gridIndex: 0,
          data: dates,
          boundaryGap: false,
          axisLabel: { show: false },
          axisTick: { show: false },
        },
        {
          type: "category",
          gridIndex: 1,
          data: dates,
          boundaryGap: false,
          axisLabel: {
            formatter: function (v) {
              return v.slice(0, 7);
            },
            fontSize: 10,
            interval: Math.floor(dates.length / 12),
          },
        },
      ],
      yAxis: [
        {
          type: "value",
          gridIndex: 0,
          name: "净值",
          nameTextStyle: { fontSize: 11 },
          axisLabel: { fontSize: 10 },
          splitLine: { show: true },
          scale: true,
        },
        {
          type: "value",
          gridIndex: 0,
          name: "比值×" + ratio[base].toFixed(2),
          nameTextStyle: { fontSize: 10, color: "#2e7d32" },
          axisLabel: {
            fontSize: 10,
            formatter: function (v) {
              return v.toFixed(2);
            },
          },
          splitLine: { show: false },
          scale: true,
        },
        {
          type: "value",
          gridIndex: 1,
          name: "总敞口",
          nameTextStyle: { fontSize: 10, color: "#c62828" },
          axisLabel: {
            fontSize: 9,
            formatter: function (v) {
              return v.toFixed(1) + "x";
            },
          },
          splitLine: { show: false },
          min: 0,
          max: 1.3,
        },
      ],
      series: [
        {
          name: "MA200策略",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: back.strat_nav,
          smooth: true,
          lineStyle: { color: "#1a237e", width: 2 },
          itemStyle: { color: "#1a237e" },
          symbol: "none",
          markPoint: {
            symbol: "pin",
            symbolSize: 46,
            data: [
              {
                coord: [dates[base], back.strat_nav[base]],
                value: (back.strat_nav[base] / back.bench_nav[base]).toFixed(2),
                itemStyle: { color: "#1a237e" },
                label: { color: "#fff", fontSize: 10 },
              },
            ],
          },
        },
        {
          name: "中证1000(买入持有)",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: back.bench_nav,
          smooth: true,
          lineStyle: { color: "#90a4ae", width: 1.5, type: "dashed" },
          itemStyle: { color: "#90a4ae" },
          symbol: "none",
        },
        {
          name: "策略/基准相对净值",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 1,
          data: ratio,
          smooth: true,
          lineStyle: { color: "#2e7d32", width: 1.5 },
          itemStyle: { color: "#2e7d32" },
          symbol: "none",
        },
        {
          name: "历史总敞口",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: back.exposure,
          step: "end",
          lineStyle: { color: "#c62828", width: 1.2 },
          itemStyle: { color: "#c62828" },
          symbol: "none",
          areaStyle: { color: "rgba(198,40,40,0.08)" },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { type: "dashed", color: "#c62828", opacity: 0.4 },
            label: { show: false },
            data: [{ yAxis: 1 }],
          },
        },
      ],
    });
    window.addEventListener("resize", function () {
      chart.resize();
    });
    var p = back.performance;
    document.getElementById("nav-metrics").innerHTML =
      "回测绩效：年化收益 <b>" +
      p.annual_return +
      "</b> · 累计 <b>" +
      p.cumulative_return +
      "</b> · 最大回撤 <b>" +
      p.max_drawdown +
      "</b>（" +
      p.drawdown_window +
      "）· 年化波动 " +
      p.annual_vol +
      " · Sharpe <b>" +
      p.sharpe +
      "</b> · 平均敞口 " +
      p.avg_exposure;
  }

  function renderStatic() {
    // 最新现货数据表
    var html =
      '<table class="mini-table"><thead><tr><th>指数</th><th>最新日期</th><th>收盘</th><th>200日均线</th><th>趋势</th><th>低波动</th></tr></thead><tbody>';
    LEG_ORDER.forEach(function (leg) {
      var s = live.snap[leg === "IC" ? "ic" : "im"];
      html +=
        "<tr><td>" +
        s.name +
        "</td><td>" +
        s.date +
        "</td><td>" +
        s.close.toFixed(2) +
        "</td><td>" +
        s.ma200.toFixed(2) +
        "</td>" +
        '<td><span class="badge ' +
        (s.strong ? "strong" : "weak") +
        '">' +
        (s.strong ? "强势" : "弱势") +
        '</span></td><td><span class="badge ' +
        (s.lowvol ? "yes" : "no") +
        '">' +
        (s.lowvol ? "低波动" : "非低波动") +
        "</span></td></tr>";
    });
    html += "</tbody></table>";
    document.getElementById("latest-data").innerHTML = html;

    // 策略说明
    var W = {
      "双强·低波动": "60% / 60%（1.2x）",
      "双强·非低波动": "50% / 50%（1.0x）",
      "单强（IC/IM）": "75% / 25%",
      "弱强（IC/IM）": "25% / 75%",
      双弱: "12.5% / 12.5%（0.25x）",
    };
    document.getElementById("strategy-desc").innerHTML =
      "<h4>策略逻辑</h4>" +
      "<p>交易 <b>IC</b>（中证500）与 <b>IM</b>（中证1000）近月股指期货，持有以获取贴水收益，并用长期均线控制权益方向风险。趋势判断仅用现货指数的 <b>200日均线</b>，状态须连续 " +
      (live.confirm_days || 5) +
      " 个交易日成立才确认（过滤均线附近的噪声，降低频繁切换）。</p>" +
      "<p>依据双腿趋势强弱与低波动状态查权重表调整 IC/IM 权重与总敞口：</p>" +
      '<table class="mini-table" style="max-width:520px"><thead><tr><th>IC 趋势</th><th>IM 趋势</th><th>低波动</th><th>IC / IM 权重（总敞口）</th></tr></thead><tbody>' +
      "<tr><td>强</td><td>强</td><td>是</td><td>60% / 60%（1.2倍）</td></tr>" +
      "<tr><td>强</td><td>强</td><td>否</td><td>50% / 50%（1.0倍）</td></tr>" +
      "<tr><td>强</td><td>弱</td><td>—</td><td>75% / 25%</td></tr>" +
      "<tr><td>弱</td><td>强</td><td>—</td><td>25% / 75%</td></tr>" +
      "<tr><td>弱</td><td>弱</td><td>—</td><td>12.5% / 12.5%（0.25倍）</td></tr>" +
      "</tbody></table>" +
      "<p>低波动：近月净收益 60 日波动率低于其 " +
      (live.median_win || 6) +
      " 个月中位数即视为低波动。不使用贴水权重微调。</p>" +
      "<h4>回测口径</h4>" +
      "<p>近月合约提前5日换月，换月成本 2×单边 2bp；信号相对收益区间滞后 2 个交易日，避免收盘价前视。评价期 " +
      back.eval_start +
      " ~ " +
      back.eval_end +
      "，基准为 <code>中证1000</code> 买入持有现金收益率。</p>" +
      "<h4>盘中实时指导口径</h4>" +
      '<p>交易时间每 30 分钟取中证500/中证1000现货实时价作为"当日收盘价"，重算 200 日均线趋势与低波动，得到当前应持有的 IC/IM 目标权重与总敞口，用于指导调仓。低波动盘中用现货指数收盘收益的 60 日波动率近似（实盘以期货反映更准确）。</p>';

    // 参数配置表
    var params = [
      ["均线", "趋势判断均线窗口", live.ma + " 日"],
      ["趋势确认", "状态连续成立天数", live.confirm_days + " 天"],
      ["波动率窗口", "近月净收益波动率计算窗口", live.vol60 + " 日"],
      ["中位数窗口", "判断低波动的滚动中位数窗口", live.median_win + " 月"],
      ["单边成本", "每次权重/换月成本（单边）", "2bp（0.02%）"],
      ["信号滞后", "信号相对收益区间滞后交易日", "2 天"],
      ["换月规则", "近月合约切换", "提前 5 交易日"],
      ["双强·低波动", "IC/IM 权重（总敞口）", W["双强·低波动"]],
      ["双强·非低波动", "IC/IM 权重（总敞口）", W["双强·非低波动"]],
      ["单强/弱强", "IC/IM 权重（总敞口）", "75/25 · 25/75"],
      ["双弱", "IC/IM 权重（总敞口）", W["双弱"]],
      ["评价期", "回测净值区间", back.eval_start + " ~ " + back.eval_end],
      ["基准", "回测对比基准", "中证1000（买入持有）"],
    ];
    var pt = document
      .getElementById("params-table")
      .getElementsByTagName("tbody")[0];
    pt.innerHTML = params
      .map(function (p) {
        return (
          "<tr><td>" +
          p[0] +
          "</td><td>" +
          p[1] +
          "</td><td>" +
          p[2] +
          "</td></tr>"
        );
      })
      .join("");
    document.getElementById("data-source").textContent =
      "回测数据源：股指期货单合约 + 中证500/中证1000现货(aakshare,xlsx) | 实时：东方财富→腾讯 | 最新数据 " +
      (live.latest_date || "");
  }

  // ---------- 启动 ----------
  renderNavChart();
  renderStatic();
  recompute(); // 每次页面加载/刷新即按最新实时价重算，不再定时轮询
})();
