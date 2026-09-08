// if (!window._bt_scale_range) {
//     window._bt_scale_range = function (range, min, max, pad) {
//         "use strict";
//         if (min !== Infinity && max !== -Infinity) {
//             pad = pad ? (max - min) * .03 : 0;
//             range.start = min - pad;
//             range.end = max + pad;
//         } else console.error('backtesting: scale range error:', min, max, range);
//     };
// }

// // if (!window._bt_scale_range) {
// //     window._x_scale_range = function (range, min, max, pad) {
// //         "use strict";
// //         if (min !== Infinity && max !== -Infinity) {
// //             range.start = min ;
// //             range.end = max + pad;
// //         } else console.error('backtesting: scale range error:', min, max, range);
// //     };
// // }

// clearTimeout(window._bt_autoscale_timeout);

// window._bt_autoscale_timeout = setTimeout(function () {
//     /**
//      * @variable cb_obj `fig_ohlc.x_range`.
//      * @variable source `ColumnDataSource`
//      * @variable ohlc_range `fig_ohlc.y_range`.
//      * @variable volume_range `fig_volume.y_range`.
//      */
//     "use strict";
//     let i = Math.max(Math.floor(cb_obj.start), 0),
//         j = Math.min(Math.ceil(cb_obj.end), source.data['High'].length);

//     let max = Math.max.apply(null, source.data['High'].slice(i, j)),
//         min = Math.min.apply(null, source.data['Low'].slice(i, j));

//     // let x = Math.max.apply(null, source.data['index'].slice(i, j)),
//     //     max_x = source.data['index'].length;
//     _bt_scale_range(ohlc_range, min, max, true);
//     if (indicator_range) {
//         for (i = 0; i < indicator_range.length; i++) {
//             let ii = Math.max(Math.floor(cb_obj.start), 0),
//                 jj = Math.min(Math.ceil(cb_obj.end), source.data[indicator_h[i]].length);

//             let max_ = Math.max.apply(null, source.data[indicator_h[i]].slice(ii, jj)),
//                 min_ = Math.min.apply(null, source.data[indicator_l[i]].slice(ii, jj));
//             _bt_scale_range(indicator_range[i], min_, max_, true);
//         }
//     }
//     // if (candles_range) {
//     //     for (ix = 0; ix < candles_range.length; ix++) {
//     //         _bt_scale_range(candles_range[ix], min, max, true);
//     //     }
//     // }
//     if (volume_range) {
//         max = Math.max.apply(null, source.data['volume'].slice(i, j));
//         _bt_scale_range(volume_range, 0, max * 1.03, false);
//     }
//     label_x = j + 100

// }, 50);

// ========== 修改 AUTOSCALE_JS_CALLBACK 代码 ==========
if (!window._bt_scale_range) {
    window._bt_scale_range = function (range, min, max, pad) {
        "use strict";
        if (min !== Infinity && max !== -Infinity && !isNaN(min) && !isNaN(max)) {
            pad = pad ? (max - min) * .03 : 0;
            range.start = min - pad;
            range.end = max + pad;
        } else console.error('backtesting: scale range error:', min, max, range);
    };
}

// 新增：更严格的参数校验
if (!cb_obj || !source || !source.data ||
    !source.data['High'] || !source.data['Low'] ||
    !ohlc_range) {
    console.warn('autoscale js: missing core data', {
        cb_obj: cb_obj,
        source: source,
        ohlc_range: ohlc_range
    });
    return;
}

clearTimeout(window._bt_autoscale_timeout);

window._bt_autoscale_timeout = setTimeout(function () {
    "use strict";
    let start = Math.max(Math.floor(cb_obj.start), 0),
        end = Math.min(Math.ceil(cb_obj.end), source.data['High'].length);

    // 新增：数据长度校验
    if (end - start < 1 || source.data['High'].length < 1) {
        console.warn('autoscale js: no data in range', { start, end, length: source.data['High'].length });
        return;
    }

    // 修复：使用slice后检查空数组
    let highSlice = source.data['High'].slice(start, end);
    let lowSlice = source.data['Low'].slice(start, end);
    if (highSlice.length === 0 || lowSlice.length === 0) {
        console.warn('autoscale js: empty slice', { start, end });
        return;
    }

    let max = Math.max.apply(null, highSlice),
        min = Math.min.apply(null, lowSlice);

    // 修复：NaN判断
    if (isNaN(max) || isNaN(min)) {
        console.warn('autoscale js: NaN values', { max, min });
        return;
    }

    _bt_scale_range(ohlc_range, min, max, true);

    if (indicator_range && indicator_h && indicator_l) {
        for (let idx = 0; idx < indicator_range.length; idx++) {
            let hKey = indicator_h[idx];
            let lKey = indicator_l[idx];
            // 新增：指标字段存在性校验
            if (!source.data[hKey] || !source.data[lKey]) {
                console.warn('autoscale js: missing indicator data', { hKey, lKey });
                continue;
            }

            // 修复：指标数据可能比价格数据短（被截断到 min_start_length）
            // 指标数据只对应价格数据的最后 N 条，需要正确映射索引
            let indicator_length = source.data[hKey].length;
            let price_length = source.data['High'].length;
            let hSlice, lSlice;
            
            if (indicator_length < price_length) {
                // 指标数据被截断，只对应价格数据的最后 indicator_length 条
                let indicator_start_in_price = price_length - indicator_length;
                
                // 计算 X 轴可视范围与指标数据对应范围的交集
                let visible_start = Math.max(Math.floor(cb_obj.start), 0);
                let visible_end = Math.min(Math.ceil(cb_obj.end), price_length);
                
                // 指标数据在价格数据中的范围
                let ind_start_in_price = indicator_start_in_price;
                let ind_end_in_price = price_length;
                
                // 计算交集
                let overlap_start = Math.max(visible_start, ind_start_in_price);
                let overlap_end = Math.min(visible_end, ind_end_in_price);
                
                // 如果没有交集，跳过该指标的自动缩放
                if (overlap_start >= overlap_end) {
                    continue;
                }
                
                // 将交集映射到指标数据的索引
                ii = overlap_start - indicator_start_in_price;
                jj = overlap_end - indicator_start_in_price;
                
                hSlice = source.data[hKey].slice(ii, jj);
                lSlice = source.data[lKey].slice(ii, jj);
            } else {
                // 数据长度一致，使用原始逻辑
                ii = Math.max(Math.floor(cb_obj.start), 0);
                jj = Math.min(Math.ceil(cb_obj.end), indicator_length);
                hSlice = source.data[hKey].slice(ii, jj);
                lSlice = source.data[lKey].slice(ii, jj);
            }
            
            if (hSlice.length === 0 || lSlice.length === 0) {
                continue;
            }

            let max_ = Math.max.apply(null, hSlice),
                min_ = Math.min.apply(null, lSlice);

            if (!isNaN(max_) && !isNaN(min_)) {
                _bt_scale_range(indicator_range[idx], min_, max_, true);
            }
        }
    }

    if (volume_range && source.data['volume']) {
        let volSlice = source.data['volume'].slice(start, end);
        if (volSlice.length > 0) {
            let maxVol = Math.max.apply(null, volSlice);
            if (!isNaN(maxVol)) {
                _bt_scale_range(volume_range, 0, maxVol * 1.03, false);
            }
        }
    }

    label_x = end + 100;

}, 50);
