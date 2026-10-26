if (!window._bt_scale_range) {
    window._bt_scale_range = function (range, min, max, pad) {
        "use strict";
        // 必须同时排除 NaN / Infinity, 否则 range.start/end 会被写成 NaN,
        // 副图会整块空白(缩放或左右拖动时必现)。
        if (typeof min === 'number' && typeof max === 'number' &&
            isFinite(min) && isFinite(max)) {
            // ★ [修复 BUG] 让 bokehjs 的 DataRange1d **停止自动重拟合**。
            //   DataRange1d.update() 的第一句就是:
            //       if (this.have_updated_interactively) return;
            //   否则它会拿**全部数据**重算 start/end, 把我们刚刚写入的
            //   “可见 K 线范围”覆盖掉 -> 现象: 缩放/拖动时 Y 轴先按可见 K 线
            //   调整, 下一秒又弹回全段 K 线的范围(明显的范围跳变)。
            //   这个标志本是 bokeh 用来表示“用户已接管该 range”的开关,
            //   我们在 JS 里主动置位即可; 双击 reset 时 bokeh 会自动清掉它。
            try { range.have_updated_interactively = true; } catch (e) {}
            if (max === min) {
                // 退化区间(min==max)时 pad 会算成 0, 同样会导致空白,
                // 兜底给一个最小可视跨度。
                var fallback = Math.abs(max) * 0.01 || 1;
                range.start = min - fallback;
                range.end = max + fallback;
                return;
            }
            pad = pad ? (max - min) * .03 : 0;
            range.start = min - pad;
            range.end = max + pad;
        } else console.error('backtesting: scale range error:', min, max, range);
    };
}

// 过滤掉 NaN / null / undefined, 避免 Math.max/min 被 NaN 污染
if (!window._bt_finite) {
    window._bt_finite = function (arr) {
        "use strict";
        var out = [];
        if (!arr) return out;
        for (var k = 0; k < arr.length; k++) {
            var v = arr[k];
            if (typeof v === 'number' && isFinite(v)) out.push(v);
        }
        return out;
    };
}

clearTimeout(window._bt_autoscale_timeout);

window._bt_autoscale_timeout = setTimeout(function () {
    /**
     * @variable cb_obj `fig_ohlc.x_range`.
     * @variable source `ColumnDataSource`
     * @variable ohlc_range `fig_ohlc.y_range`.
     * @variable volume_range `fig_volume.y_range`.
     */
    "use strict";

    let i = Math.max(Math.floor(cb_obj.start), 0),
        j = Math.min(Math.ceil(cb_obj.end), source.data['High'].length);

    let max = Math.max.apply(null, window._bt_finite(source.data['High'].slice(i, j))),
        min = Math.min.apply(null, window._bt_finite(source.data['Low'].slice(i, j)));
    _bt_scale_range(ohlc_range, min, max, true);
    if (typeof indicator_range !== 'undefined' && indicator_range) {
        for (i = 0; i < indicator_range.length; i++) {
            let ii = Math.max(Math.floor(cb_obj.start), 0),
                jj = Math.min(Math.ceil(cb_obj.end), source.data[indicator_h[i]].length);

            let max_ = Math.max.apply(null, window._bt_finite(source.data[indicator_h[i]].slice(ii, jj))),
                min_ = Math.min.apply(null, window._bt_finite(source.data[indicator_l[i]].slice(ii, jj)));
            _bt_scale_range(indicator_range[i], min_, max_, true);
        }
    }
    if (typeof volume_range !== 'undefined' && volume_range) {
        max = Math.max.apply(null, window._bt_finite(source.data['volume'].slice(i, j)));
        _bt_scale_range(volume_range, 0, max * 1.03, false);
    }

}, 50);
