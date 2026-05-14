import sys
import os
import json

# 添加项目根目录到系统路径
sys.path.append(os.getcwd())

from backend.shared.redis_sentinel_client import get_redis_sentinel_client

def clear_cache():
    try:
        redis = get_redis_sentinel_client()
        key = "qm:admin:data_status"
        if redis.exists(key):
            redis.delete(key)
            print(f"Successfully deleted Redis key: {key}")
        else:
            print(f"Key {key} does not exist in Redis.")
    except Exception as e:
        print(f"Failed to clear Redis cache: {e}")

if __name__ == "__main__":
    clear_cache()
