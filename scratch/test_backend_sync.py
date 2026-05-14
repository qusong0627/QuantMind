import sys
import os
from pathlib import Path

# 添加项目根目录到系统路径
sys.path.append(os.getcwd())

from backend.services.api.routers.admin.model_management_utils import _scan_feature_snapshots_status

def test():
    print("Testing _scan_feature_snapshots_status...")
    result = _scan_feature_snapshots_status(target_date="2026-05-13")
    
    print(f"Exists: {result['exists']}")
    print(f"File Count: {result['file_count']}")
    print(f"Scanned Files: {result['scanned_files']}")
    print(f"Total Rows: {result['total_rows']}")
    print(f"Metadata Files Count: {len(result.get('metadata_files', []))}")
    
    if result.get('metadata_files'):
        print("\nFirst Metadata File Sample:")
        print(result['metadata_files'][0])

if __name__ == "__main__":
    test()
