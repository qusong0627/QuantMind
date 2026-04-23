"""
通达信市场数据推送到 Redis

功能：从通达信客户端获取 A 股实时行情，推送到 Redis 供 QuantMind 使用

使用前请配置 Redis 连接信息（以下三种方式任选其一）：

方式一：设置环境变量
    export REMOTE_QUOTE_REDIS_HOST=your_redis_host
    export REMOTE_QUOTE_REDIS_PORT=6379
    export REMOTE_QUOTE_REDIS_PASSWORD=your_password

方式二：创建 .env 文件（与脚本同目录）
    REMOTE_QUOTE_REDIS_HOST=your_redis_host
    REMOTE_QUOTE_REDIS_PORT=6379
    REMOTE_QUOTE_REDIS_PASSWORD=your_password

方式三：直接修改本文件底部的 REDIS_CONFIG 配置

依赖：
    - 通达信客户端（需启动并登录）
    - tqcenter.py（通达信 Python 接口）
    - redis 包：pip install redis
"""

import datetime
import time
import signal
import os
import sys
from dotenv import load_dotenv
import redis

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from tqcenter import tq
except ImportError:
    print("无法导入tqcenter模块，请确保tqcenter.py文件存在")
    exit()

load_dotenv()

# ============================================
# Redis 配置（请根据实际情况修改）
# ============================================
REDIS_CONFIG = {
    "host": os.getenv("REMOTE_QUOTE_REDIS_HOST", ""),
    "port": int(os.getenv("REMOTE_QUOTE_REDIS_PORT", "6379")),
    "password": os.getenv("REMOTE_QUOTE_REDIS_PASSWORD", ""),
}

class MarketDataToRedis:
    """市场数据推送到Redis类 - 符合QuantMind行情快照写入规范V1.0"""
    
    def __init__(self, tdx_path=None):
        """初始化
        
        Args:
            tdx_path (str): 通达信安装路径
        """
        if tdx_path is None:
            tdx_path = r'e:\new_tdx64'
        
        self.tdx_path = tdx_path
        self.running = True  # 控制循环运行的标志

        self._init_tq()
        self._init_redis()

    def _init_tq(self):
        """初始化TQ数据接口
        
        参考通达信开发规范：
        - 所有策略连接通达信客户端都必须调用initialize函数进行初始化
        - 如遇初始化失败，需先关闭现有连接
        - 策略退出前应调用close()断开连接
        """
        max_retries = 3
        retry_delay = 3
        
        # 增加环境变量检查，防止递归调用
        if os.environ.get('TQ_INITIALIZED') == '1':
            return

        for attempt in range(max_retries):
            try:
                # 先尝试关闭现有连接（参考规范：手动断开连接）
                try:
                    tq.close()
                    # print(f"  清理现有TQ连接...")
                except Exception:
                    pass

                # 等待一段时间让连接完全释放
                if attempt > 0:
                    print(f"  等待 {retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                
                # 尝试初始化（参考规范：所有策略连接通达信客户端都必须调用此函数）
                # 注意：必须传入文件路径作为标识，这里使用当前文件
                # 修复：使用绝对路径，避免路径解析问题
                init_path = os.path.abspath(__file__)
                print(f"  正在初始化TQ接口，路径: {init_path}")
                tq.initialize(init_path)
                print("✓ TQ数据接口初始化成功")
                os.environ['TQ_INITIALIZED'] = '1'
                return

            except Exception as e:
                error_msg = str(e)
                if "已有同名策略运行" in error_msg or "初始化失败" in error_msg:
                    print(f"✗ TQ数据接口初始化失败 (尝试 {attempt + 1}/{max_retries})")
                    if attempt == max_retries - 1:
                        print(f"\n" + "="*60)
                        print("TQ接口初始化失败，请检查以下事项：")
                        print("1. 确保通达信客户端已启动并登录")
                        print("2. 检查是否有其他程序占用TQ接口")
                        print("3. 在通达信客户端中关闭TQ策略管理器中的其他策略")
                        print("4. 如仍失败，请重启通达信客户端")
                        print("="*60 + "\n")
                        raise
                else:
                    print(f"✗ TQ数据接口初始化失败: {e}")
                    raise

    def _init_redis(self):
        """初始化Redis连接"""
        try:
            redis_host = REDIS_CONFIG["host"]
            redis_port = REDIS_CONFIG["port"]
            redis_password = REDIS_CONFIG["password"]

            if not redis_host:
                raise ValueError(
                    "Redis 配置缺失！请通过以下方式配置：\n"
                    "  1. 设置环境变量 REMOTE_QUOTE_REDIS_HOST/PORT/PASSWORD\n"
                    "  2. 创建 .env 文件配置上述变量\n"
                    "  3. 修改脚本顶部的 REDIS_CONFIG 字典"
                )

            if redis_password:
                self.redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    password=redis_password,
                    decode_responses=False
                )
            else:
                self.redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    decode_responses=False
                )

            self.redis_client.ping()
            print(f"✓ Redis连接成功: {redis_host}:{redis_port}")
        except Exception as e:
            print(f"✗ Redis连接失败: {e}")
            raise

    def get_stock_list(self):
        """获取全市场股票列表
        
        Returns:
            list: 股票代码列表
        """
        try:
            # 修改参数为字符串类型，避免类型错误
            # tq.get_stock_list('5') 对应所有A股
            stock_list = tq.get_stock_list('5')
            
            if stock_list:
                print(f"✓ 获取到 {len(stock_list)} 只股票")
                # 过滤掉非法格式的股票代码
                valid_stocks = [
                    s for s in stock_list if len(s) >= 9 and '.' in s
                ]
                print(f"✓ 有效股票代码 {len(valid_stocks)} 只")
                return valid_stocks
            else:
                print("✗ 获取股票列表失败")
                return []
        except Exception as e:
            print(f"✗ 获取股票列表时出错: {e}")
            return []

    def get_market_snapshot(self, stock_code):
        """获取单只股票的快照数据
        
        Args:
            stock_code (str): 股票代码
            
        Returns:
            dict: 股票快照数据
        """
        try:
            snapshot = tq.get_market_snapshot(stock_code=stock_code)
            
            if snapshot and snapshot.get('ErrorId') == '0':
                return snapshot
            else:
                return None
        except Exception as e:
            print(f"  ✗ 获取 {stock_code} 快照失败: {e}")
            return None

    def _validate_data(self, snapshot):
        """验证数据有效性
        
        Args:
            snapshot (dict): 股票快照数据
            
        Returns:
            bool: 数据是否有效
        """
        now = snapshot.get('Now', 0)
        return now is not None and float(now) > 0
    
    def is_trading_time(self):
        """判断当前是否为交易时间
        
        A股交易时间：
        - 早盘：09:15 - 11:30 (包含集合竞价)
        - 午盘：13:00 - 15:00
        
        Returns:
            bool: 是否在交易时间内
        """
        now = datetime.datetime.now()
        current_time = now.time()
        
        # 周末不交易
        if now.weekday() >= 5:
            return False
            
        # 交易时间段
        morning_start = datetime.time(9, 15)
        morning_end = datetime.time(11, 30)
        afternoon_start = datetime.time(13, 0)
        afternoon_end = datetime.time(15, 0)
        
        is_morning = morning_start <= current_time <= morning_end
        is_afternoon = afternoon_start <= current_time <= afternoon_end
        
        return is_morning or is_afternoon

    def get_wait_seconds(self):
        """计算距离下一次开盘的等待时间
        
        Returns:
            int: 等待秒数
        """
        now = datetime.datetime.now()
        current_time = now.time()
        
        # 周末
        if now.weekday() >= 5:
            # 计算距离下周一09:15的时间
            days_until_monday = 7 - now.weekday()
            next_start = (now + datetime.timedelta(days=days_until_monday)).replace(
                hour=9, minute=15, second=0, microsecond=0
            )
            return (next_start - now).total_seconds()
            
        morning_start = datetime.time(9, 15)
        morning_end = datetime.time(11, 30)
        afternoon_start = datetime.time(13, 0)
        afternoon_end = datetime.time(15, 0)
        
        # 盘前
        if current_time < morning_start:
            target = now.replace(hour=9, minute=15, second=0, microsecond=0)
            return (target - now).total_seconds()
            
        # 午休
        if morning_end < current_time < afternoon_start:
            target = now.replace(hour=13, minute=0, second=0, microsecond=0)
            return (target - now).total_seconds()
            
        # 盘后
        if current_time > afternoon_end:
            # 计算距离明天09:15的时间
            next_day = now + datetime.timedelta(days=1)
            # 如果明天是周末，顺延到周一
            if next_day.weekday() >= 5:
                next_day += datetime.timedelta(days=7 - next_day.weekday())
            
            target = next_day.replace(hour=9, minute=15, second=0, microsecond=0)
            return (target - now).total_seconds()
            
        return 0

    def get_market_snapshot_batch(self, stock_list=None, batch_size=200):
        """批量获取市场快照并推送到Redis - 使用Pipeline批量写入
        
        Args:
            stock_list (list): 股票代码列表，None表示获取全部
            batch_size (int): 批次大小（建议100-500）
            
        Returns:
            int: 成功获取的股票数量
        """
        start_time = time.time()
        
        if stock_list is None:
            stock_list = self.get_stock_list()
        
        if not stock_list:
            print("✗ 股票列表为空")
            return 0
        
        print(f"\n开始获取市场快照数据...")
        print(f"总股票数: {len(stock_list)}")
        print(f"批次大小: {batch_size}")
        
        success_count = 0
        fail_count = 0
        
        # 按批次处理
        for i in range(0, len(stock_list), batch_size):
            batch_stocks = stock_list[i:i+batch_size]
            pipe = self.redis_client.pipeline()
            batch_success = 0
            
            # 使用get_market_snapshot_batch接口批量获取（假设tqcenter支持）
            # 注意：tqcenter.py中没有get_market_snapshot_batch，只能循环调用
            # 优化：这里我们仍然循环调用，但是减少打印
            
            for stock_code in batch_stocks:
                # 增加重试机制
                retry_count = 2
                snapshot = None
                for _ in range(retry_count):
                    try:
                        snapshot = tq.get_market_snapshot(stock_code=stock_code)
                        if snapshot and snapshot.get('ErrorId') == '0':
                            break
                        time.sleep(0.01) # 短暂休眠避免请求过快
                    except:
                        pass
                
                if snapshot and self._validate_data(snapshot):
                    # 构造符合规范的Redis Key
                    # 确保格式为 stock:{code}.{market}
                    if '.' in stock_code:
                        code_parts = stock_code.split('.')
                        if len(code_parts) == 2:
                            redis_key = f"stock:{code_parts[0]}.{code_parts[1].upper()}"
                        else:
                            redis_key = f"stock:{stock_code}"
                    else:
                        redis_key = f"stock:{stock_code}"
                    
                    # 构造符合规范的数据结构
                    try:
                        now_price = float(snapshot.get('Now', 0))
                        data = {
                            'Now': now_price,
                            'Open': float(snapshot.get('Open', 0)),
                            'High': float(snapshot.get('High', 0)),
                            'Low': float(snapshot.get('Low', 0)),
                            'Close': float(snapshot.get('LastClose', 0)),
                            'Volume': int(float(snapshot.get('Volume', 0))),
                            'Amount': float(snapshot.get('Amount', 0)),
                            'timestamp': int(time.time())
                        }
                        
                        # 使用Pipeline批量写入
                        pipe.hset(redis_key, mapping=data)
                        pipe.expire(redis_key, 300)  # 设置5分钟过期时间
                        
                        success_count += 1
                        batch_success += 1
                    except (ValueError, TypeError) as e:
                        # 数据转换错误忽略
                        pass
                else:
                    fail_count += 1
            
            # 执行批量操作
            try:
                pipe.execute()
                print(f"  ✓ 批次 {i//batch_size + 1}: 成功 {batch_success}/{len(batch_stocks)}")
            except Exception as e:
                print(f"  ✗ 批次 {i//batch_size + 1} 写入失败: {e}")
                # 如果批量写入失败，尝试单个写入
                try:
                    for stock_code in batch_stocks:
                        # 这里简单处理，实际上应该重新获取数据
                        pass
                except:
                    pass
                fail_count += batch_success
                success_count -= batch_success
            
            # 小延迟避免请求过于频繁，保护客户端
            time.sleep(0.2)
        
        end_time = time.time()
        total_time = end_time - start_time
        
        print(f"\n" + "=" * 60)
        print("数据推送完成")
        print(f"  总耗时: {total_time:.2f} 秒 ({total_time/60:.2f} 分钟)")
        print(f"  成功: {success_count}")
        print(f"  失败: {fail_count}")
        if len(stock_list) > 0:
            print(f"  成功率: {success_count/len(stock_list)*100:.2f}%")
        print("=" * 60)
        
        return success_count
    
    def get_stock_data(self, stock_code):
        """从Redis获取指定股票数据
        
        Args:
            stock_code (str): 股票代码
            
        Returns:
            dict: 股票数据字典，包含所有字段
        """
        try:
            # 构造符合规范的Redis Key
            if '.' in stock_code:
                code_parts = stock_code.split('.')
                if len(code_parts) == 2:
                    redis_key = f"stock:{code_parts[0]}.{code_parts[1].upper()}"
                else:
                    redis_key = f"stock:{stock_code}"
            else:
                redis_key = f"stock:{stock_code}"
            
            data = self.redis_client.hgetall(redis_key)
            
            # 转换数据类型
            if data:
                result = {}
                for k, v in data.items():
                    key = k.decode('utf-8') if isinstance(k, bytes) else k
                    
                    if key in ['Now', 'Open', 'High', 'Low', 'Close', 'Amount']:
                        result[key] = float(v) if isinstance(v, (int, float)) else float(v.decode('utf-8'))
                    elif key in ['Volume', 'timestamp']:
                        result[key] = int(v) if isinstance(v, (int, float)) else int(v.decode('utf-8'))
                    else:
                        result[key] = v.decode('utf-8') if isinstance(v, bytes) else v
                return result
            else:
                return None
        except Exception as e:
            print(f"✗ 获取 {stock_code} 数据失败: {e}")
            return None
    
    def clear_all_data(self):
        """清除Redis中所有股票数据"""
        try:
            keys = self.redis_client.keys("stock:*")
            
            if keys:
                # 批量删除，每批次1000个
                for i in range(0, len(keys), 1000):
                    batch_keys = keys[i:i+1000]
                    self.redis_client.delete(*batch_keys)
                print(f"✓ 已清除 {len(keys)} 条股票数据")
            else:
                print("✓ 没有需要清除的数据")
        except Exception as e:
            print(f"✗ 清除数据失败: {e}")
    
    def close(self):
        """关闭连接"""
        try:
            tq.close()
            print("✓ TQ数据接口连接已关闭")
        except Exception as e:
            print(f"✗ 关闭TQ数据接口失败: {e}")
        
        try:
            if self.redis_client:
                self.redis_client.close()
                print("✓ Redis连接已关闭")
        except Exception as e:
            print(f"✗ 关闭Redis连接失败: {e}")

def signal_handler(signum, frame):
    """信号处理函数，实现优雅退出"""
    print("\n\n收到退出信号，正在停止程序...")
    global mdtr
    if mdtr:
        mdtr.running = False

def main():
    """主函数 - 支持循环推送"""
    global mdtr
    
    print("=" * 60)
    print("通达信市场数据推送到Redis（符合QuantMind规范）")
    print("循环模式：每隔30秒推送一次数据")
    print("按 Ctrl+C 可优雅退出")
    print("=" * 60)
    print()
    
    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal_handler)
    
    try:
        mdtr = MarketDataToRedis()
        stock_list = []

        print("\n开始循环推送市场数据...")
        print("推送间隔: 30 秒")
        print()

        cycle_count = 0
        total_success = 0

        while mdtr.running:
            # 检查交易时间
            if not mdtr.is_trading_time():
                wait_seconds = mdtr.get_wait_seconds()
                # 最多等待60秒，避免长时间休眠无法响应退出信号
                sleep_time = min(wait_seconds, 60)

                print(
                    f"\r当前非交易时间 "
                    f"({datetime.datetime.now().strftime('%H:%M:%S')})，"
                    f"等待开盘 (距离下次开盘约 {int(wait_seconds)} 秒)...",
                    end="", flush=True
                )
                time.sleep(sleep_time)
                continue

            # 交易时间，确保获取了股票列表
            if not stock_list:
                print("\n正在获取股票列表...")
                stock_list = mdtr.get_stock_list()
                if not stock_list:
                    print("✗ 无法获取股票列表，等待 30 秒后重试...")
                    time.sleep(30)
                    continue
                print(f"股票总数: {len(stock_list)}")

            cycle_count += 1
            cycle_start = time.time()

            print(f"\n{'='*60}")
            print(f"第 {cycle_count} 次推送 - {time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}")
            
            success_count = mdtr.get_market_snapshot_batch(stock_list=stock_list, batch_size=200)
            
            if success_count > 0:
                total_success += success_count
                print(f"\n✓ 本轮成功推送 {success_count} 只股票的数据到Redis")
                print(f"  累计推送: {total_success} 次")
            else:
                print(f"\n✗ 本轮推送失败")
            
            # 显示示例数据（仅在第一轮）
            if cycle_count == 1:
                print("\n示例：读取浦发银行(600000.SH)的数据")
                stock_data = mdtr.get_stock_data("600000.SH")
                if stock_data:
                    print(f"  代码: 600000.SH")
                    print(f"  当前价: {stock_data.get('Now', 'N/A')}")
                    print(f"  开盘价: {stock_data.get('Open', 'N/A')}")
                    print(f"  最高价: {stock_data.get('High', 'N/A')}")
                    print(f"  最低价: {stock_data.get('Low', 'N/A')}")
                    print(f"  收盘价: {stock_data.get('Close', 'N/A')}")
                    print(f"  成交量: {stock_data.get('Volume', 'N/A')}")
                    print(f"  成交额: {stock_data.get('Amount', 'N/A')}")
                    print(f"  时间戳: {stock_data.get('timestamp', 'N/A')}")
                else:
                    print("  未找到该股票数据")
            
            # 计算剩余等待时间
            cycle_time = time.time() - cycle_start
            wait_time = max(0, 30 - cycle_time)
            
            # 强制等待，确保每轮间隔至少30秒
            if mdtr.running:
                print(f"\n等待 {wait_time:.1f} 秒后进行下一次推送...")
                time.sleep(wait_time)
        
        print("\n\n" + "=" * 60)
        print("程序已停止")
        print(f"总共运行了 {cycle_count} 个周期")
        print(f"累计成功推送: {total_success} 次")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n✗ 执行失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            mdtr.close()
        except:
            pass

if __name__ == "__main__":
    main()
