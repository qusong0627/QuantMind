if (!window._bt_scale_range) {
    window._bt_scale_range = function (range, min, max, pad) {
        "use strict";
        if (min !== Infinity && max !== -Infinity) {
            pad = pad ? (max - min) * .03 : 0;
            range.start = min - pad;
            range.end = max + pad;
        } else console.error('backtesting: scale range error:', min, max, range);
    };
}

if (!window._bt_scale_range) {
    window._x_scale_range = function (range, min, max, pad) {
        "use strict";
        if (min !== Infinity && max !== -Infinity) {
            range.start = min ;
            range.end = max + pad;
        } else console.error('backtesting: scale range error:', min, max, range);
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

    //let i = Math.max(Math.floor(xr.start), 0);
    cb_obj.x=source.x[0]
        // j = Math.min(Math.ceil(cb_obj.end), source.data['High'].length);
    
    // let max = Math.max.apply(null, source.data['High'].slice(i, j)),
    //     min = Math.min.apply(null, source.data['Low'].slice(i, j));
    
    // // let x = Math.max.apply(null, source.data['index'].slice(i, j)),
    // //     max_x = source.data['index'].length;
    // _bt_scale_range(ohlc_range, min, max, true);
    // if (candles_range) {
    //     for (ix = 0; ix < candles_range.length; ix++) {
    //         _bt_scale_range(candles_range[ix], min, max, true);
    //     }
    // }
    // if (volume_range) {
    //     max = Math.max.apply(null, source.data['volume'].slice(i, j));
    //     _bt_scale_range(volume_range, 0, max * 1.03, false);
    // }
    
    /*if (x >= max_x) {*/
    /*cb_obj.end = j + 200;*/
    
    

}, 50);
