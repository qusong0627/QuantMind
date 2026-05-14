import sys
import os
import redis

# 添加项目根目录到系统路径
sys.path.append(os.getcwd())

def clear_cache():
    try:
        # 直接使用 redis 库，避开项目复杂的 sentinel 配置
        r = redis.Redis(
            host='localhost',
            port=6379,
            password='quantmind2026',
            db=0,
            decode_responses=True
        )
        key = "qm:admin:data_status"
        if r.exists(key):
            r.delete(key)
            print(f"Successfully deleted Redis key: {key}")
        else:
            print(f"Key {key} does not exist in Redis.")
            
        # 检查一下 db 4 (Engine) 是否也有缓存
        r4 = redis.Redis(
            host='localhost',
            port=6379,
            password='quantmind2026',
            db=4,
            decode_responses=True
        )
        if r4.exists(key):
            r4.delete(key)
            print(f"Successfully deleted Redis key in DB 4: {key}")
            
    except Exception as e:
        print(f"Failed to clear Redis cache: {e}")

if __name__ == "__main__":
    clear_cache()
