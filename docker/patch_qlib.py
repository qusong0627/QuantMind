#!/usr/bin/env python3
"""
修复 qlib 0.9.7 的 bug:
- position.py 中 calculate_stock_value 未处理 price=None 的情况
- 当股票停牌时，持仓价格可能为 None，导致 TypeError
"""
import sys

position_file = '/usr/local/lib/python3.10/site-packages/qlib/backtest/position.py'

try:
    with open(position_file, 'r') as f:
        content = f.read()
except FileNotFoundError:
    print(f"Warning: {position_file} not found, skipping patch")
    sys.exit(0)

# 修复 calculate_stock_value 方法
old_code = '''    def calculate_stock_value(self) -> float:
        stock_list = self.get_stock_list()
        value = 0
        for stock_id in stock_list:
            value += self.position[stock_id]["amount"] * self.position[stock_id]["price"]
        return value'''

new_code = '''    def calculate_stock_value(self) -> float:
        stock_list = self.get_stock_list()
        value = 0
        for stock_id in stock_list:
            price = self.position[stock_id].get("price")
            if price is not None:
                value += self.position[stock_id]["amount"] * price
        return value'''

if old_code in content:
    content = content.replace(old_code, new_code)
    with open(position_file, 'w') as f:
        f.write(content)
    print("Patched qlib position.py to handle None price")
else:
    print("qlib position.py already patched or code changed")
