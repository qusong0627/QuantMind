#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shared.cos_service import TencentCOSService


DIST_ROOT = ROOT / "dist" / "qmt_agent"
DEFAULT_MANIFEST_PATH = DIST_ROOT / "latest.json"
DEFAULT_INSTALLER_DIR = DIST_ROOT / "installer"


def _load_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()


def _apply_cos_env_overrides(args: argparse.Namespace) -> None:
    overrides = {
        "TENCENT_SECRET_ID": args.secret_id,
        "COS_SECRET_ID": args.secret_id,
        "SecretId": args.secret_id,
        "TENCENT_SECRET_KEY": args.secret_key,
        "COS_SECRET_KEY": args.secret_key,
        "SecretKey": args.secret_key,
        "TENCENT_BUCKET": args.bucket,
        "COS_BUCKET": args.bucket,
        "Bucket": args.bucket,
        "TENCENT_REGION": args.region,
        "COS_REGION": args.region,
    }
    for key, value in overrides.items():
        if value:
            os.environ[key] = value

    if args.base_url:
        os.environ["TENCENT_COS_URL"] = args.base_url
        os.environ["COS_URL"] = args.base_url
        os.environ["COS_BASE_URL"] = args.base_url


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"发布清单不存在: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("发布清单格式错误，应为 JSON 对象")
    return data


def _require_cos_service() -> TencentCOSService:
    cos = TencentCOSService()
    if not cos.client or not cos.bucket_name:
        raise RuntimeError(
            "COS 配置不完整，请先设置 TENCENT_SECRET_ID / TENCENT_SECRET_KEY / TENCENT_BUCKET / TENCENT_REGION"
        )
    return cos


def _resolve_asset_path(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    asset_name: str,
    override: str | None,
) -> Path:
    if override:
        return Path(override).expanduser().resolve()

    asset_info = manifest.get(asset_name)
    if not isinstance(asset_info, dict):
        raise KeyError(f"发布清单缺少 {asset_name} 节点")

    file_name = str(asset_info.get("file_name") or "").strip()
    if not file_name:
        raise KeyError(f"发布清单缺少 {asset_name}.file_name")

    if asset_name == "installer":
        return (DEFAULT_INSTALLER_DIR / file_name).resolve()
    return (manifest_path.parent / file_name).resolve()


def _build_update_latest_json(manifest: dict[str, Any], installer_path: Path) -> str:
    """Build latest.json for update checking."""
    version = str(manifest.get("version") or "").strip()
    build_time = str(manifest.get("build_time") or "").strip()
    installer = manifest.get("installer") or {}
    portable = manifest.get("portable") or {}

    installer_key = str(installer.get("key") or "").strip()
    portable_key = str(portable.get("key") or "").strip()

    # Build download URLs
    base_url = "https://cos.quantmind.cloud"
    installer_url = f"{base_url}/{installer_key}" if installer_key else None
    portable_url = f"{base_url}/{portable_key}" if portable_key else None

    latest = {
        "version": version,
        "release_date": build_time,
        "release_notes": manifest.get("release_notes") or manifest.get("notes"),
        "download_url": installer_url,
        "downloads": {
            "installer": installer_url,
            "portable": portable_url,
        },
        "file_size": installer_path.stat().st_size if installer_path.exists() else None,
        "file_hash": str(installer.get("sha256") or "").strip() or None,
    }

    # Remove None values
    latest = {k: v for k, v in latest.items() if v is not None}

    return json.dumps(latest, ensure_ascii=False, indent=2)


def _build_sha256_text(manifest: dict[str, Any]) -> str:
    version = str(manifest.get("version") or "").strip()
    build_time = str(manifest.get("build_time") or "").strip()
    manifest_key = str(manifest.get("manifest_key") or "").strip()
    installer = manifest.get("installer") or {}
    portable = manifest.get("portable") or {}

    installer_key = str(installer.get("key") or "").strip()
    installer_file = str(installer.get("file_name") or "").strip()
    installer_sha = str(installer.get("sha256") or "").strip()
    portable_key = str(portable.get("key") or "").strip()
    portable_file = str(portable.get("file_name") or "").strip()
    portable_sha = str(portable.get("sha256") or "").strip()

    lines = [
        "# QuantMindQMTAgent release sha256",
        f"version: {version}",
        f"build_time: {build_time}",
        f"manifest_key: {manifest_key}",
        "",
        f"{installer_sha}  {installer_file}  {installer_key}",
        f"{portable_sha}  {portable_file}  {portable_key}",
        "",
    ]
    return "\n".join(lines)


def _upload_exact(
    cos: TencentCOSService,
    *,
    key: str,
    source: Path | bytes,
    file_name: str,
    content_type: str,
    retries: int = 3,
) -> dict[str, Any]:
    last_error: str | None = None
    for attempt in range(1, max(1, retries) + 1):
        if isinstance(source, Path):
            result = cos.upload_file(
                str(source),
                key,
                content_type=content_type,
                use_exact_key=True,
            )
        else:
            result = cos.upload_file(
                source,
                key,
                content_type=content_type,
                use_exact_key=True,
            )
        if result.get("success"):
            print(f"[upload-qmt-agent] uploaded {file_name} -> {key}")
            return result

        last_error = str(result.get("error") or "unknown error")
        if attempt < max(1, retries):
            wait_seconds = min(10, attempt * 2)
            print(
                f"[upload-qmt-agent] upload failed for {file_name} -> {key}, "
                f"retry {attempt}/{retries} after {wait_seconds}s: {last_error}"
            )
            time.sleep(wait_seconds)

    raise RuntimeError(f"上传失败: {key}, {last_error or 'unknown error'}")


def _head_object_or_none(cos: TencentCOSService, key: str) -> dict[str, Any] | None:
    if not cos.client or not cos.bucket_name:
        return None
    try:
        resp = cos.client.head_object(Bucket=cos.bucket_name, Key=key)
        return {
            "content_length": int(resp.get("Content-Length", 0)),
            "etag": str(resp.get("ETag", "")).strip('"'),
            "content_type": resp.get("Content-Type"),
            "last_modified": resp.get("Last-Modified"),
        }
    except Exception:
        return None


def _verify_uploaded_object(
    cos: TencentCOSService,
    *,
    key: str,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    head = _head_object_or_none(cos, key)
    if head is None:
        raise RuntimeError(f"无法验证对象是否存在: {key}")
    if expected_size is not None and head.get("content_length") != expected_size:
        raise RuntimeError(
            f"对象大小不匹配: {key}, expected={expected_size}, actual={head.get('content_length')}"
        )
    if expected_sha256:
        # COS 的 ETag 不一定等于 SHA256，因此这里仅做存在性验证，
        # sha256 由本地发布清单与 sha256.txt 保证一致性。
        print(f"[upload-qmt-agent] verified {key} exists, sha256={expected_sha256}")
    else:
        print(f"[upload-qmt-agent] verified {key} exists")


def main() -> int:
    parser = argparse.ArgumentParser(description="将 QMT Agent 发布产物上传到 COS")
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST_PATH),
        help="本地发布清单路径，默认 dist/qmt_agent/latest.json",
    )
    parser.add_argument(
        "--installer",
        default="",
        help="安装器 exe 路径，默认读取清单中的 file_name 并位于 dist/qmt_agent/installer/",
    )
    parser.add_argument(
        "--portable",
        default="",
        help="便携包 zip 路径，默认读取清单中的 file_name 并位于发布清单同目录",
    )
    parser.add_argument(
        "--sha256-key",
        default="",
        help="sha256.txt 的 COS key，默认按 release/version 自动生成",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印计划，不执行上传",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="单个对象上传重试次数，默认 3",
    )
    parser.add_argument(
        "--skip-installer",
        action="store_true",
        help="跳过安装器上传",
    )
    parser.add_argument(
        "--skip-portable",
        action="store_true",
        help="跳过便携包上传",
    )
    parser.add_argument(
        "--skip-manifest",
        action="store_true",
        help="跳过发布清单上传",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅做本地与远端存在性验证，不执行上传",
    )
    parser.add_argument("--secret-id", default="", help="COS SecretId，覆盖环境变量")
    parser.add_argument("--secret-key", default="", help="COS SecretKey，覆盖环境变量")
    parser.add_argument("--bucket", default="", help="COS Bucket，覆盖环境变量")
    parser.add_argument("--region", default="", help="COS Region，默认 ap-guangzhou")
    parser.add_argument("--base-url", default="", help="COS 自定义域名或基础 URL，覆盖环境变量")
    parser.add_argument(
        "--env-file",
        default="",
        help="额外加载的 .env 路径，优先级高于根目录 .env",
    )
    args = parser.parse_args()

    if args.env_file:
        env_file = Path(args.env_file).expanduser().resolve()
        if not env_file.exists():
            raise FileNotFoundError(f"指定的 env 文件不存在: {env_file}")
        load_dotenv(dotenv_path=env_file, override=True)

    _load_env()
    _apply_cos_env_overrides(args)

    if args.region and not os.getenv("TENCENT_REGION") and not os.getenv("COS_REGION"):
        os.environ["TENCENT_REGION"] = args.region
        os.environ["COS_REGION"] = args.region

    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = _read_manifest(manifest_path)

    installer_info = manifest.get("installer") or {}
    portable_info = manifest.get("portable") or {}

    installer_path = _resolve_asset_path(
        manifest_path=manifest_path,
        manifest=manifest,
        asset_name="installer",
        override=args.installer or None,
    )
    portable_path = _resolve_asset_path(
        manifest_path=manifest_path,
        manifest=manifest,
        asset_name="portable",
        override=args.portable or None,
    )

    installer_key = str(installer_info.get("key") or "").strip()
    portable_key = str(portable_info.get("key") or "").strip()
    manifest_key = str(manifest.get("manifest_key") or "").strip()
    version = str(manifest.get("version") or "").strip()
    if not installer_key or not portable_key or not manifest_key:
        raise KeyError("发布清单缺少 installer.key / portable.key / manifest_key")

    if not installer_path.exists():
        raise FileNotFoundError(f"安装器不存在: {installer_path}")
    if not portable_path.exists():
        raise FileNotFoundError(f"便携包不存在: {portable_path}")

    sha256_key = args.sha256_key.strip()
    if not sha256_key:
        sha256_key = f"qmt-agent/windows/release/v{version}/sha256.txt"

    sha256_text = _build_sha256_text(manifest)

    print("[upload-qmt-agent] manifest:", manifest_path)
    print("[upload-qmt-agent] installer:", installer_path, "->", installer_key)
    print("[upload-qmt-agent] portable:", portable_path, "->", portable_key)
    print("[upload-qmt-agent] sha256:", sha256_key)
    print("[upload-qmt-agent] release manifest:", manifest_key)

    if args.dry_run:
        return 0

    cos = _require_cos_service()

    if args.verify_only:
        _verify_uploaded_object(
            cos,
            key=installer_key,
            expected_sha256=str(installer_info.get("sha256") or "").strip() or None,
            expected_size=installer_path.stat().st_size,
        )
        _verify_uploaded_object(
            cos,
            key=portable_key,
            expected_sha256=str(portable_info.get("sha256") or "").strip() or None,
            expected_size=portable_path.stat().st_size,
        )
        _verify_uploaded_object(cos, key=sha256_key)
        _verify_uploaded_object(cos, key=manifest_key)
        print("[upload-qmt-agent] verification completed")
        return 0

    if not args.skip_installer:
        _upload_exact(
            cos,
            key=installer_key,
            source=installer_path,
            file_name=installer_path.name,
            content_type="application/vnd.microsoft.portable-executable",
            retries=args.retries,
        )
    else:
        print("[upload-qmt-agent] skip installer upload")

    if not args.skip_portable:
        _upload_exact(
            cos,
            key=portable_key,
            source=portable_path,
            file_name=portable_path.name,
            content_type="application/zip",
            retries=args.retries,
        )
    else:
        print("[upload-qmt-agent] skip portable upload")

    _upload_exact(
        cos,
        key=sha256_key,
        source=sha256_text.encode("utf-8"),
        file_name="sha256.txt",
        content_type="text/plain; charset=utf-8",
        retries=args.retries,
    )

    # Upload latest.json for update checking
    update_latest_key = f"qmt-agent/windows/latest.json"
    update_latest_json = _build_update_latest_json(manifest, installer_path)
    _upload_exact(
        cos,
        key=update_latest_key,
        source=update_latest_json.encode("utf-8"),
        file_name="latest.json",
        content_type="application/json; charset=utf-8",
        retries=args.retries,
    )
    print(f"[upload-qmt-agent] update latest.json -> {update_latest_key}")

    if not args.skip_manifest:
        _upload_exact(
            cos,
            key=manifest_key,
            source=manifest_path,
            file_name=manifest_path.name,
            content_type="application/json; charset=utf-8",
            retries=args.retries,
        )
    else:
        print("[upload-qmt-agent] skip manifest upload")

    print("[upload-qmt-agent] upload completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
