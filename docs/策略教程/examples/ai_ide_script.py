# -*- coding: utf-8 -*-
"""示例 6：AI-IDE 普通脚本入口。

AI-IDE 支持三种入口：
1. if __name__ == "__main__": 或顶层代码；
2. main() / run()；
3. STRATEGY_CONFIG / get_strategy_config / get_strategy_instance（模块型回测）。
"""


def main():
    print("AI-IDE 脚本示例启动")
    values = [1, 2, 3, 4, 5]
    print("均值:", sum(values) / len(values))
    print("把这里替换成你的研究代码；需要回测请用 STRATEGY_CONFIG 或 minibt。")


if __name__ == "__main__":
    main()
