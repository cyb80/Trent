/* RSRS盘中预测值计算 (js/rsrs_realtime.js)
 * 读取 window.RSRS_DATA(含ohlc/params), 获取实时行情, 重算各策略当日预测值并填充表格
 * 时段逻辑: 盘前→上一交易日; 盘中→当日实时高低点估算; 盘后→当日收盘; 节假日→最近交易日
 * 实时数据源: 东方财富 push2 (CORS开放), 失败时腾讯行情 JSONP 兜底
 */
(function () {
  'use strict';
  var json = window.RSRS_DATA;
  if (!json) return;

  var SECID_MAP = { '上证50': '1.000016', '沪深300': '1.000300', '中证500': '1.000905', '中证1000': '1.000852' };
  var TXCODE_MAP = { '上证50': 'sh000016', '沪深300': 'sh000300', '中证500': 'sh000905', '中证1000': 'sh000852' };
  // 与rsrs.html中容器id后缀对应, 用于定位状态行
  var SUFFIX_MAP = { '上证50': 'sz50', '沪深300': 'hs300', '中证500': 'zz500', '中证1000': 'zz1000' };

  function fmtDate(d) {
    var m = d.getMonth() + 1, dd = d.getDate();
    return d.getFullYear() + '-' + (m < 10 ? '0' + m : m) + '-' + (dd < 10 ? '0' + dd : dd);
  }
  function fmtTime(d) {
    function p2(x) { return x < 10 ? '0' + x : '' + x; }
    return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate()) +
      ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
  }
  function avg(a) { var s = 0, i; for (i = 0; i < a.length; i++) s += a[i]; return s / a.length; }
  function sampleStd(a) {
    var n = a.length, i, m = 0, s = 0;
    if (n < 2) return NaN;
    for (i = 0; i < n; i++) m += a[i];
    m /= n;
    for (i = 0; i < n; i++) s += (a[i] - m) * (a[i] - m);
    return Math.sqrt(s / (n - 1));
  }
  function sampleCov(a, b) {
    var n = a.length, i, ma = 0, mb = 0, c = 0;
    if (n < 2) return NaN;
    for (i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
    ma /= n; mb /= n;
    for (i = 0; i < n; i++) c += (a[i] - ma) * (b[i] - mb);
    return c / (n - 1);
  }

  // 超时包装: ms内未完成则返回{error}并打印日志, 避免状态行一直停留在"预测值计算中"
  function withTimeout(promise, ms, tag) {
    return new Promise(function (resolve) {
      var done = false;
      var t = setTimeout(function () {
        if (!done) {
          done = true;
          console.warn('[RSRS预测值] ' + tag + ' 请求超时(' + (ms / 1000) + 's)');
          resolve({ error: '请求超时(' + (ms / 1000) + 's)', source: tag });
        }
      }, ms);
      promise.then(function (v) { if (!done) { done = true; clearTimeout(t); resolve(v); } });
    });
  }

  // 滚动窗口计算: 有效值个数 >= minP 时调用 fn(有效值窗口), 否则 NaN
  function rolling(arr, M, minP, fn) {
    var out = new Array(arr.length).fill(NaN), i;
    for (i = 0; i < arr.length; i++) {
      var start = Math.max(0, i - M + 1), win = arr.slice(start, i + 1), vals = [];
      for (var k = 0; k < win.length; k++) if (isFinite(win[k])) vals.push(win[k]);
      if (vals.length >= minP) out[i] = fn(vals);
    }
    return out;
  }

  // 与 fetch_rsrs.py compute_rsrs_indicators 相同公式的前端实现
  function computeSignals(close, high, low, vol, N, M) {
    var n = close.length, i, k;
    var beta = new Array(n).fill(NaN), r2 = new Array(n).fill(NaN);
    var bW = new Array(n).fill(NaN), r2W = new Array(n).fill(NaN);

    for (i = N - 1; i < n; i++) {
      var H = high.slice(i - N + 1, i + 1), L = low.slice(i - N + 1, i + 1);
      var W = vol.slice(i - N + 1, i + 1).map(function (x) { return x || 0; });
      var varL = sampleStd(L) * sampleStd(L);
      if (varL > 0) {
        var cov = sampleCov(H, L);
        beta[i] = cov / varL;
        var varH = sampleStd(H) * sampleStd(H);
        r2[i] = varH > 0 ? cov * cov / (varL * varH) : 0;
      }
      var wsum = 0;
      for (k = 0; k < W.length; k++) wsum += W[k];
      if (wsum > 0) {
        var w = W.map(function (x) { return x / wsum; });
        var mH = 0, mL = 0;
        for (k = 0; k < N; k++) { mH += w[k] * H[k]; mL += w[k] * L[k]; }
        var cw = 0, vlw = 0, vhw = 0;
        for (k = 0; k < N; k++) {
          cw += w[k] * (H[k] - mH) * (L[k] - mL);
          vlw += w[k] * (L[k] - mL) * (L[k] - mL);
          vhw += w[k] * (H[k] - mH) * (H[k] - mH);
        }
        if (vlw > 0) {
          bW[i] = cw / vlw;
          r2W[i] = vhw > 0 ? cw * cw / (vlw * vhw) : 0;
        }
      } else if (varL > 0) {
        var cov2 = sampleCov(H, L), varH2 = sampleStd(H) * sampleStd(H);
        bW[i] = cov2 / varL;
        r2W[i] = varH2 > 0 ? cov2 * cov2 / (varL * varH2) : 0;
      }
    }

    var minPM = Math.floor(M / 2), minPN = Math.floor(N / 2);
    function zfn(v) { var sd = sampleStd(v); return sd > 0 ? (v[v.length - 1] - avg(v)) / sd : NaN; }
    var z = rolling(beta, M, minPM, zfn);
    var zW = rolling(bW, M, minPM, zfn);

    var ret = new Array(n).fill(NaN);
    for (i = 1; i < n; i++) ret[i] = (close[i] - close[i - 1]) / close[i - 1];
    var retStd = rolling(ret, N, minPN, sampleStd);
    var retQ = rolling(retStd, M, minPM, function (v) {
      var last = v[v.length - 1], mn = Math.min.apply(null, v), mx = Math.max.apply(null, v);
      var q = mx === mn ? 0.5 : (last - mn) / (mx - mn);
      return Math.max(0, Math.min(1, q));
    });

    var last = n - 1;
    return {
      'RSRS_原始': z[last] * r2[last],
      'RSRS_右偏修正': z[last] * r2[last] * beta[last],
      'RSRS_钝化': z[last] * Math.pow(r2[last], 4 * retQ[last]),
      'RSRS_成交额加权钝化': zW[last] * Math.pow(r2W[last], 4 * retQ[last])
    };
  }

  // 东方财富 push2 实时行情 (10秒超时)
  function fetchEM(secid) {
    var url = 'https://push2.eastmoney.com/api/qt/stock/get?secid=' + secid +
      '&invt=2&fltt=2&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f86';
    return withTimeout(fetch(url).then(function (r) { return r.json(); }).then(function (j) {
      var d = j && j.data;
      if (!d || !d.f43 || d.f43 <= 0) return { error: '数据源无有效数据(最新价<=0)', source: '东方财富' };
      var ts = d.f86 ? (d.f86 < 1e12 ? d.f86 * 1000 : d.f86) : Date.now();
      return { price: d.f43, high: d.f44 || d.f43, low: d.f45 || d.f43, volume: d.f47 || 0, time: ts, source: '东方财富' };
    }).catch(function (e) {
      // 网络层错误: TypeError(Failed to fetch)=断网/CORS/DNS; JSON解析错误=数据源返回非JSON
      console.warn('[RSRS预测值] 东方财富请求出错: ' + secid, e && e.message ? e.message : String(e));
      return { error: e && e.message ? e.message : String(e), source: '东方财富' };
    }), 10000, '东方财富');
  }

  // 腾讯行情兜底 (JSONP), 返回格式 v_sh000016="1~名称~代码~..."
  function fetchTX(code) {
    return new Promise(function (resolve) {
      try { delete window['v_' + code]; } catch (e) { window['v_' + code] = undefined; }
      var sc = document.createElement('script');
      sc.charset = 'GBK';
      sc.src = 'https://qt.gtimg.cn/q=' + code + '&_=' + Date.now();
      sc.onload = function () {
        var s = window['v_' + code] || '';
        if (!s) { console.warn('[RSRS预测值] 腾讯接口返回为空: ' + code); resolve({ error: '接口返回为空', source: '腾讯' }); sc.remove(); return; }
        var f = s.split('~');
        var price = parseFloat(f[3]), high = parseFloat(f[33]), low = parseFloat(f[34]);
        if (!(price > 0)) { resolve({ error: '数据源无有效数据(最新价<=0)', source: '腾讯' }); sc.remove(); return; }
        var ts = f[30] || '';
        var t = new Date(+ts.slice(0, 4), +ts.slice(4, 6) - 1, +ts.slice(6, 8), +ts.slice(8, 10), +ts.slice(10, 12), +ts.slice(12, 14));
        resolve({ price: price, high: high || price, low: low || price, volume: parseFloat(f[6]) || 0, time: isNaN(t.getTime()) ? Date.now() : t.getTime(), source: '腾讯' });
        sc.remove();
      };
      sc.onerror = function (e) { console.warn('[RSRS预测值] 腾讯接口网络错误: ' + code, e); resolve({ error: '网络错误', source: '腾讯' }); };
      document.head.appendChild(sc);
    });
  }

  // 东财优先, 失败/超时打印日志并尝试腾讯兜底
  function fetchRT(name) {
    return fetchEM(SECID_MAP[name]).then(function (em) {
      if (em && em.price > 0) return em;
      if (em && em.error) console.warn('[RSRS预测值] ' + name + ' 东方财富失败: ' + em.error + ' → 尝试腾讯');
      return withTimeout(fetchTX(TXCODE_MAP[name]), 10000, '腾讯').then(function (tx) {
        if (tx && tx.price > 0) return tx;
        if (tx && tx.error) console.warn('[RSRS预测值] ' + name + ' 腾讯失败: ' + tx.error);
        return em || tx || null;
      });
    });
  }

  // 文件最新信号值 (与后端一致)
  function fileLastValues(o) {
    var out = {};
    Object.keys(o.strategies).forEach(function (key) {
      var s = o.strategies[key].signal;
      var v = s && s.length ? s[s.length - 1] : null;
      out[key] = (v === null || v === undefined) ? NaN : v;
    });
    return out;
  }

  var now = new Date();
  var bj = new Date(Date.now() + (480 + now.getTimezoneOffset()) * 60000);
  var todayStr = fmtDate(bj);
  var curMin = bj.getHours() * 60 + bj.getMinutes();

  function fillPred(name, vals, label) {
    var statusEl = document.getElementById('pred-status-' + (SUFFIX_MAP[name] || name));
    if (statusEl) statusEl.textContent = label;
    Object.keys(vals).forEach(function (key) {
      var cell = document.querySelector('.pred-cell[data-idx="' + name + '"][data-key="' + key + '"]');
      if (!cell) return;
      var v = vals[key];
      if (!isFinite(v)) { cell.textContent = '-'; return; }
      cell.textContent = v.toFixed(4);
      var th = json[name].strategies[key] && json[name].strategies[key].threshold;
      cell.className = 'pred-cell ' + (th && v > th ? 'pos-full' : (th && v < -th ? 'pos-empty' : 'pos-flat'));
    });
  }

  var names = ['上证50', '沪深300', '中证500', '中证1000'];
  Promise.all(names.map(function (name) {
    return fetchRT(name).then(function (rt) { return { name: name, rt: rt }; });
  })).then(function (list) {
    var doneTime = fmtTime(new Date(Date.now() + (480 + new Date().getTimezoneOffset()) * 60000));
    list.forEach(function (item) {
      var name = item.name, o = json[name];
      if (!o || !o.ohlc) return;
      var ohlc = o.ohlc, lastDate = ohlc.dates[ohlc.dates.length - 1];
      var label, vals;

      if (lastDate >= todayStr) {
        vals = fileLastValues(o);
        label = '预测值计算于 ' + doneTime + ' | 数据文件已含今日(' + lastDate + ')';
      } else if (item.rt && item.rt.price > 0) {
        var rt = item.rt, rtDate = fmtDate(new Date(rt.time));
        if (rtDate < todayStr) {
          vals = fileLastValues(o);
          label = '预测值计算于 ' + doneTime + ' | 今日休市, 显示最近交易日 ' + lastDate;
        } else {
          var c2 = ohlc.close.slice(), h2 = ohlc.high.slice(), l2 = ohlc.low.slice();
          var v2 = ohlc.volume.slice(), d2 = ohlc.dates.slice();
          d2.push(todayStr); c2.push(rt.price); h2.push(rt.high); l2.push(rt.low); v2.push(rt.volume);
          vals = computeSignals(c2, h2, l2, v2, o.params.N, o.params.M);
          var stage = curMin < 570 ? '盘前' : (curMin >= 900 ? '今日已收盘' : '盘中估算');
          label = '预测值计算于 ' + doneTime + ' | ' + stage + ': 当日 高' + rt.high.toFixed(2) + '/低' + rt.low.toFixed(2) + ' 实时数据源: ' + rt.source;
        }
      } else {
        var errSrc = item.rt && item.rt.source ? item.rt.source : '未知';
        var errMsg = item.rt && item.rt.error ? item.rt.error : '未知错误';
        vals = fileLastValues(o);
        label = '预测值计算于 ' + doneTime + ' | 实时行情获取失败(' + errSrc + ': ' + errMsg + '), 显示最近交易日 ' + lastDate;
      }
      fillPred(name, vals, label);
    });
  });
})();
