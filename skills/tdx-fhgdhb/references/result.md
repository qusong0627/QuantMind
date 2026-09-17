## Call Templates

### dividend_financing_dividend_and_fundraising

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "000001",
    "pxmz"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_dividend_and_fundraising",
    "resultSets": [
      {
        "name": "dividend_financing_dividend_and_fundraising_0",
        "index": 0,
        "fieldMap": {
          "total": "分红次数",
          "sum": "派息金额"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_and_fundraising_1",
        "index": 1,
        "fieldMap": {
          "total": "首发次数",
          "sum": "募资金额"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_and_fundraising_2",
        "index": 2,
        "fieldMap": {
          "total": "增发次数",
          "sum": "募资金额",
          "zfcnt": "增发预案次数"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_and_fundraising_3",
        "index": 3,
        "fieldMap": {
          "total": "配股次数",
          "sum": "募资金额",
          "pgcnt": "配股预案次数"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_and_fundraising_4",
        "index": 4,
        "fieldMap": {
          "ssy": "上市年份"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_and_fundraising_5",
        "index": 5,
        "fieldMap": {
          "total": "转债次数",
          "sum": "募资金额"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_and_fundraising_6",
        "index": 6,
        "fieldMap": {
          "gxl": "最新股息率",
          "glzfl": "最新年度股利支付率",
          "ljxjfh": "累计现金分红（派现+回购注销）",
          "njgmjlrfrom": "年均归母净利润",
          "xjfhnl": "累计现金分红（派现+回购注销）/年均归母净利润"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_and_fundraising_7",
        "index": 7,
        "fieldMap": {
          "total": "已实施股权激励和授予",
          "sum": "募资额"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "000001",
    "description": "股票代码"
  },
  {
    "param": "pxmz",
    "description": "固定标识"
  }
]
```

### dividend_financing_dividend_chart

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "000001",
    "fh"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_dividend_chart",
    "resultSets": [
      {
        "name": "dividend_financing_dividend_chart_0",
        "index": 0,
        "fieldMap": {
          "rq": "分红年度",
          "N002": "报告期",
          "N012": "分红金额"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "000001",
    "description": "股票代码"
  },
  {
    "param": "fh",
    "description": "固定标识"
  }
]
```

### dividend_financing_dividend_table

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz_fh",
  "params": [
    "000001",
    "fh",
    "1"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_dividend_table",
    "resultSets": [
      {
        "name": "dividend_financing_dividend_table_0",
        "index": 0,
        "fieldMap": {
          "rq": "分红年度",
          "T003": "董事会预案公告日期",
          "T004": "实施方案分红说明",
          "T006": "基本美股收益",
          "T026": "净资产收益率",
          "T021": "股权登记日",
          "T023": "除权日",
          "T036": "方案进度",
          "aT036": "方案进度编码",
          "glzfl": "股利支付率",
          "jdcode": "派发对象"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_table_1",
        "index": 1,
        "fieldMap": {
          "zs": "总数"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "000001",
    "description": "股票代码"
  },
  {
    "param": "fh",
    "description": "固定标识"
  },
  {
    "param": "1",
    "description": "页码"
  }
]
```

### dividend_financing_dividend_insight_stock_screening

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_sj",
  "params": [
    "qhgp",
    "000001",
    "0",
    ""
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_dividend_insight_stock_screening",
    "resultSets": [
      {
        "name": "dividend_financing_dividend_insight_stock_screening_0",
        "index": 0,
        "fieldMap": {
          "zs": "总数"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_dividend_insight_stock_screening_1",
        "index": 1,
        "fieldMap": {
          "id": "判断入参代码",
          "N001": "证券代码",
          "N002": "证券简称"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "qhgp",
    "description": "板块"
  },
  {
    "param": "000001",
    "description": "证券代码"
  },
  {
    "param": "0",
    "description": "统计区间"
  },
  {
    "param": "",
    "description": ""
  }
]
```

### dividend_financing_dividend_insight_comparison_data

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_sj",
  "params": [
    "fh_sj",
    "000001",
    "0",
    "000001,600000,600016,600036,600015,601988,601398,601166"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_dividend_insight_comparison_data",
    "resultSets": [
      {
        "name": "dividend_financing_dividend_insight_comparison_data_0",
        "index": 0,
        "fieldMap": {
          "id": "判断入参代码",
          "N001": "年份",
          "N002": "证券代码",
          "N003": "证券简称",
          "N004": "累计分红总额(元)",
          "N005": "首发融资金额(元)",
          "N006": "增发融资累计金额(元)",
          "N007": "配股融资累计金额(元)",
          "N008": "累计融资(元)",
          "N009": "累计净分红(元)",
          "N010": "派现融资比%",
          "N011": "上市日期"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "fh_sj",
    "description": "固定标识"
  },
  {
    "param": "000001",
    "description": "证券代码"
  },
  {
    "param": "0",
    "description": "统计区间"
  },
  {
    "param": "000001,600000,600016,600036,600015,601988,601398,601166",
    "description": "切换股票"
  }
]
```

### dividend_financing_rights_issue_implemented_plan_rights_issue_plan

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "000001",
    "pf"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_rights_issue_implemented_plan_rights_issue_plan",
    "resultSets": [
      {
        "name": "dividend_financing_rights_issue_implemented_plan_rights_issue_plan_0",
        "index": 0,
        "fieldMap": {
          "rq": "公告日期",
          "T005": "配股比例(每10股配N股)",
          "T006": "配股价格(元)",
          "T011": "股权登记日",
          "T012": "除权基准日",
          "T015": "实际配股数量(万股)",
          "T017": "实际募资总额(万元)"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_rights_issue_implemented_plan_rights_issue_plan_1",
        "index": 1,
        "fieldMap": {
          "rq": "公告日期",
          "T023": "方案进度",
          "T006": "配股比例(董)(每10股配N股)",
          "T011": "预计配股价格上限(元)",
          "T012": "预计配股价格下限(元)",
          "T014": "预计配股数量(万股)",
          "T015": "预计募集资金(万元)"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "000001",
    "description": "股票代码"
  },
  {
    "param": "pf",
    "description": "固定标识"
  }
]
```

### dividend_financing_secondary_offering_allocation_details

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "000001",
    "zfpg"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_secondary_offering_allocation_details",
    "resultSets": [
      {
        "name": "dividend_financing_secondary_offering_allocation_details_0",
        "index": 0,
        "fieldMap": {
          "T004": "公布获配机构/代码",
          "T005": "获配机构",
          "T009": "锁定期(月)",
          "T007": "获配数量(股)",
          "T008": "有效申购数量(股)",
          "T006": "机构类型",
          "T012": "获配金额",
          "jjrq": "截止日期",
          "hpjg": "发行价",
          "T002": "股东id",
          "T003": "通达信股东id",
          "id": "股东类别"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_secondary_offering_allocation_details_1",
        "index": 1,
        "fieldMap": {
          "mx": "公告日期"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "000001",
    "description": "股票代码"
  },
  {
    "param": "zfpg",
    "description": "固定标识"
  }
]
```

### dividend_financing_secondary_offering_allocation_details_shareholder_in_out_details

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_gdyjcgmx",
  "params": [
    "gdjc",
    "000001",
    "9900002221",
    "8000056",
    "1"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_secondary_offering_allocation_details_shareholder_in_out_details",
    "resultSets": [
      {
        "name": "dividend_financing_secondary_offering_allocation_details_shareholder_in_out_details_0",
        "index": 0,
        "fieldMap": {
          "T001": "机构id",
          "zqdm": "证券代码",
          "sc": "证券市场",
          "rq": "报告日期",
          "T006": "持股数量",
          "T007": "占流通股股本比例",
          "T012": "股份性质",
          "T009": "种类",
          "cnt": "个数",
          "T008": "增减股数",
          "zqjc": "证券简称",
          "stype": "类别"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "gdjc",
    "description": "固定标识"
  },
  {
    "param": "000001",
    "description": "股票代码"
  },
  {
    "param": "9900002221",
    "description": "机构代码"
  },
  {
    "param": "8000056",
    "description": "股东id"
  },
  {
    "param": "1",
    "description": "页码"
  }
]
```

### dividend_financing_secondary_offering_allocation_details_shareholder_in_out_details_category

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_gdyjcgmx",
  "params": [
    "gdjcmxrq",
    "",
    "9900002221",
    "8000056",
    "1"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_secondary_offering_allocation_details_shareholder_in_out_details_category",
    "resultSets": [
      {
        "name": "dividend_financing_secondary_offering_allocation_details_shareholder_in_out_details_category_0",
        "index": 0,
        "fieldMap": {
          "T001": "机构id",
          "stype": "类别"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "gdjcmxrq",
    "description": "固定标识"
  },
  {
    "param": "",
    "description": "股票代码"
  },
  {
    "param": "9900002221",
    "description": "机构代码"
  },
  {
    "param": "8000056",
    "description": "股东id"
  },
  {
    "param": "1",
    "description": "页码"
  }
]
```

### dividend_financing_secondary_offering

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "000001",
    "zf"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_financing_secondary_offering",
    "resultSets": [
      {
        "name": "dividend_financing_secondary_offering_0",
        "index": 0,
        "fieldMap": {
          "T003": "公告日期",
          "T005": "总发行数量(万股)",
          "T006": "公开发行数量(万股)",
          "T011": "每股面值(元)",
          "T012": "发行价格(人民币)(元)",
          "T017": "发行定价方式",
          "T025": "预计募资金额(万元)",
          "T026": "实际募资总额(万元)",
          "T111": "承销方式",
          "T110": "发行方式",
          "T037": "股权登记日",
          "T038": "除权日",
          "T039": "发行前总股本(万股)",
          "T040": "发行后总股本(万股)",
          "T080": "增发股上市日"
        },
        "layout": "record"
      },
      {
        "name": "dividend_financing_secondary_offering_1",
        "index": 1,
        "fieldMap": {
          "T005": "发行规模(万股)",
          "T008": "预计募资金额(万元)",
          "T002": "公告日期",
          "T007": "发行定价方式",
          "T016": "方案进度",
          "T006": "发行对象",
          "T009": "预计募资投向"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "000001",
    "description": "股票代码"
  },
  {
    "param": "zf",
    "description": "固定标识"
  }
]
```

### dividend_history_trend_payout_ratio

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "601086",
    "fhlszs_glzfl"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_history_trend_payout_ratio",
    "resultSets": [
      {
        "name": "dividend_history_trend_payout_ratio_0",
        "index": 0,
        "fieldMap": {
          "N001": "分红年度",
          "N002": "分红金额",
          "N003": "归母净利润",
          "N004": "股利支付率"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "601086",
    "description": "股票代码"
  },
  {
    "param": "fhlszs_glzfl",
    "description": "固定标识"
  }
]
```

### dividend_history_trend_dividend_yield

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "601086",
    "fhlszs_gxl"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_history_trend_dividend_yield",
    "resultSets": [
      {
        "name": "dividend_history_trend_dividend_yield_0",
        "index": 0,
        "fieldMap": {
          "N001": "日期",
          "N002": "股息率",
          "N003": "余额宝七日年化收益",
          "N004": "除权除息日"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "601086",
    "description": "股票代码"
  },
  {
    "param": "fhlszs_gxl",
    "description": "固定标识"
  }
]
```

### dividend_ranking_payout_ratio_industry_payout_ratio_top10

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "601086",
    "fhpm_glzfl"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_ranking_payout_ratio_industry_payout_ratio_top10",
    "resultSets": [
      {
        "name": "dividend_ranking_payout_ratio_industry_payout_ratio_top10_0",
        "index": 0,
        "fieldMap": {
          "N001": "排名",
          "N002": "股票简称",
          "N003": "股利支付率%"
        },
        "layout": "record"
      },
      {
        "name": "dividend_ranking_payout_ratio_industry_payout_ratio_top10_1",
        "index": 1,
        "fieldMap": {
          "N001": "排名",
          "N002": "股票简称",
          "N003": "股利支付率%"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "601086",
    "description": "股票代码"
  },
  {
    "param": "fhpm_glzfl",
    "description": "固定标识"
  }
]
```

### dividend_ranking_dividend_yield_industry_dividend_yield_top10

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "601086",
    "fhpm_gxl"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_ranking_dividend_yield_industry_dividend_yield_top10",
    "resultSets": [
      {
        "name": "dividend_ranking_dividend_yield_industry_dividend_yield_top10_0",
        "index": 0,
        "fieldMap": {
          "N001": "排名",
          "N002": "股票简称",
          "N003": "股息率%"
        },
        "layout": "record"
      },
      {
        "name": "dividend_ranking_dividend_yield_industry_dividend_yield_top10_1",
        "index": 1,
        "fieldMap": {
          "N001": "排名",
          "N002": "股票简称",
          "N003": "股息率%"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "601086",
    "description": "股票代码"
  },
  {
    "param": "fhpm_gxl",
    "description": "固定标识"
  }
]
```

### dividend_ranking_cash_dividend_financing_ratio_industry_cash_dividend_financing_ratio_top10

```json
{
  "mode": "raw",
  "entry": "TdxSharePCCW.tdxf10_gg_fhrz",
  "params": [
    "601086",
    "fhpm_pxrzb"
  ],
  "responseTransform": {
    "kind": "result-sets",
    "parserName": "dividend_ranking_cash_dividend_financing_ratio_industry_cash_dividend_financing_ratio_top10",
    "resultSets": [
      {
        "name": "dividend_ranking_cash_dividend_financing_ratio_industry_cash_dividend_financing_ratio_top10_0",
        "index": 0,
        "fieldMap": {
          "N001": "排名",
          "N002": "股票简称",
          "N003": "派现融资比%"
        },
        "layout": "record"
      },
      {
        "name": "dividend_ranking_cash_dividend_financing_ratio_industry_cash_dividend_financing_ratio_top10_1",
        "index": 1,
        "fieldMap": {
          "N001": "排名",
          "N002": "股票简称",
          "N003": "派现融资比%"
        },
        "layout": "record"
      }
    ]
  }
}
```

Param descriptions:

```json
[
  {
    "param": "601086",
    "description": "股票代码"
  },
  {
    "param": "fhpm_pxrzb",
    "description": "固定标识"
  }
]
```
