"""AlphaAgent 文档中心端点（T-FM-08）—— 上传/列表/详情/整理/预览/删除/配额。

与 `routers/alpha_agent.py` 同前缀（`/api/v1/alpha-agent`），拆成独立文件是
为了让 3400 行的主路由不再长大；两个 router 由 `main.py` 分别 include。

闸门（T-FM-13）
---------------
整组端点挂在 **router 级** ``dependencies=[Depends(require_doc_mining)]`` 上：
`ENABLE_DOC_MINING=false`（默认）时全部 403 + 机器可读 detail
``doc_mining_disabled``；将来往这个 router 加路由**默认落在拒绝侧**。
`/evolve` 带 doc_id 的分支在另一个 router，够不到这里，由它自己调闸门。

上传的安全边界（与 trading_agents 报告上传同款手法，更严）
--------------------------------------------------------
- 文件名：拒绝一切路径分隔符（含 Windows ``\\``——posix 的 ``Path().name``
  不会剥它）、隐藏名、超长名、**控制/格式字符**（``Cc/Cf``：换行伪造日志、
  bidi/零宽伪装，安全审查 L3）；
- **内容定形三分法**（改名/异格式真图片不该被拒，可执行文件仍必须被拒）：
  ``sniff_content_type`` 读首 ≤16 字节判内容类型 → ①直通族（pdf/png/jpeg，
  含改名）按**内容**定扩展名落盘（改名 png 存成 ``original.png``）；②转码族
  （webp/gif/bmp/tiff/avif）Pillow 解首帧压制为 PNG 后入库（MinerU 只吃
  png/jpg/jpeg；复用键按转码后字节重算）；③容器族（zip/OLE）声称扩展名
  必须与容器同族（docx/pptx、doc/ppt），否则 400；HEIC 一律可读 400 引导
  转 JPG/PNG（容器无 heif 解码器）；其余未知内容 400（防「改名成 .pdf 的
  可执行文件」）。落盘仍按定型后的 ``STORAGE_MAGIC`` 复验一遍 magic；
- 大小**两道闸**（安全审查 C1）：handler 里先从 ``request.stream()`` 拿不到
  就拒——先用 ``Content-Length`` 粗拦（缺头 411：不支持 chunked），再分块
  计数精验（不信任声明值）。**不能**用 ``file: UploadFile = File(...)`` 参数：
  FastAPI 会在进入 handler 前把整个 multipart 无上限落临时文件，上限就变成
  「收完之后才生效」。多文件后粗闸按**合计上限**（``RD_AGENT_DOC_MAX_TOTAL_MB``，
  默认 200MB——单文件上限×N 可能合法超过它）；单文件 200MB 由流式落盘时的
  分块计数精验拦截；
- 拒绝后**连根清**：目录 rmtree、行不落库或 hard_delete（见下）。

多文件合并（T-FM-19a）
----------------------
同一字段 ``file`` 可重复出现（≤ ``MAX_DOC_FILES`` 件，**顺序=合并顺序**，
正文在前附录在后由用户决定）。单文件走历史路径（``original{ext}``，行不写
manifest，逐字节同口径）；多文件落 ``originals/p{i}{ext}``（i 从 1 起，与
MinerU data_id 的 ``_p{i}`` 部件序号对齐），行上 ``original_paths`` 记部件
清单、``files_count`` 记件数、显示名缀「（共N个文件）」。复用键 = 各
(name, sha256) 按顺序拼接的复合 sha256（顺序/文件名敏感：同集合换序或改名
不命中复用——宁可重解析一次，也不把产物章节标题贴错名）。

配额顺序（先预留后提交；安全审查 H1）
-------------------------------------
``DocQuota.reserve`` 必须在 ``submit_parse`` **之前**，且是**原子预留**
（先 INCR 后判）：旧实现「只读预检」在并发下 N 个请求都能通过，真实页数
要等解析完成才入账，一次就能打穿共享的 1000 页/日平台预算。页数预估一律
**保守**（非 PDF 数不出页数 = 单文件上限 200，图片 = 1 页），结算时多退少补。
sha256 命中已解析文档（maybe_reuse）则不占配额也不提交——这是复用的全部
意义。被配额拦下的上传 **hard_delete**（那是从未进入解析链的行，留着只会
滞留一份「永远不解析」的 uploaded）。

频控与在途去重（安全审查 M1）
------------------------------
上传/整理按用户小时配额限流（env ``DOC_UPLOAD_RATE_PER_HOUR`` /
``DOC_ORGANIZE_RATE_PER_HOUR``）；整理对同一文档加在途锁——一次整理最多烧
9 次 LLM 调用，同一文档的并发重复整理必须被 409 挡下。

输出剥内部字段
--------------
``original_path/md_path/content_list_path/sha256/mineru_batch_id/user_id`` 一律
不出 API（展示面不需要，还泄漏磁盘布局）；``organized_text`` 只在详情/整理
响应里带（列表带正文纯属浪费带宽）。

预览白名单
----------
只允许解析产物目录（md_path 的父目录）下的 ``.md`` 与图片：路径先按 posix
规范化、拒绝对路径/``..``/反斜杠，再 resolve 后比对绝对前缀（双保险，符号链接
也逃不出去）。FileResponse 不传 filename → 浏览器内联预览。

删除防复活（安全审查 H2）
--------------------------
三层防线，缺一都会「删完又复活」：
1. ``svc.cancel(doc_id)`` 等在途轮询**完全停下**（同进程路径）；
2. ``doc_store.update_doc`` 带 ``status <> 'deleted'`` 守卫——跨进程
   （ENGINE_WORKERS>1）或轮询正卡在下载/解包/LLM 期间的每一笔落库都变
   no-op，调用方按返回值清理产物（parse service 已收口）；
3. 软删成功后再 rmtree 一次（清掉与在途写竞态残留的半成品），并退回该
   文档未结算的配额预留。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import unicodedata
import uuid
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from backend.services.engine.alpha_agent.doc_credentials import (
    resolve_effective_mineru_config,
)
from backend.services.engine.alpha_agent.doc_gate import require_doc_mining
from backend.services.engine.alpha_agent.doc_mining_settings import (
    DocMiningSettingsError,
    get_doc_mining_settings_store,
)
from backend.services.engine.alpha_agent.doc_organize import (
    ORGANIZE_KINDS,
    OrganizeError,
    organize_and_store,
)
from backend.services.engine.alpha_agent.doc_parse_service import (
    doc_accounting,
    get_doc_parse_service,
)
from backend.services.engine.alpha_agent.doc_quota import (
    QuotaExceeded,
    RateLimited,
    get_doc_quota,
)
from backend.services.engine.alpha_agent.doc_store import (
    DOC_STATUSES,
    get_doc_store,
    resolve_list_filters,
)
from backend.services.engine.alpha_agent.mineru_client import (
    MODE_LOCAL,
    MAX_PAGES_PER_FILE,
    MineruError,
    count_pdf_pages,
    resolve_mineru_config,
    run_pdf_job,
)
from backend.services.engine.alpha_agent.task_store import get_mining_task_store
from backend.services.engine.auth_context import get_authenticated_identity
from backend.services.engine.routers.alpha_agent import _resolve_effective_llm_config

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/alpha-agent",
    tags=["AlphaAgentDocs"],
    dependencies=[Depends(require_doc_mining)],
)

#: 上传大小上限的 env 名（MB；默认 200 = MinerU 单文件上限）
MAX_UPLOAD_ENV = "RD_AGENT_DOC_MAX_MB"
DEFAULT_MAX_UPLOAD_MB = 200

#: 单次上传**合计**上限的 env 名（MB）。默认 200 = 与部署链同口径：nginx
#: client_max_body_size 210m（镜像内）与 api→engine 代理体 ENGINE_PROXY_MAX_BODY_MB
#: 默认 210MB 都卡在同一个请求上——合计上限抬过 210 之前必须先把那两处一起
#: 抬高（见 docs/文档挖掘_启用与通道指南.md 第四节），否则多文件会在更外层
#: 吃 413（且 nginx 报的是裸 HTML 错误页）。
MAX_TOTAL_UPLOAD_ENV = "RD_AGENT_DOC_MAX_TOTAL_MB"
DEFAULT_MAX_TOTAL_UPLOAD_MB = 200

#: 单次上传文件数上限（一批提交给 MinerU；批上限 50，这里留足余量）
MAX_DOC_FILES = 20

#: multipart 编码在文件字节之外的开销上限（boundary/头/文件名等）：Content-Length
#: 粗拦时按「文件上限 + 本余量」封顶（安全审查 C1）
MULTIPART_OVERHEAD_BYTES = 8 * 1024 * 1024

#: 整理在途锁 TTL：LLM map-reduce 最坏时长上界 = 8 段 × 120s + reduce 120s
#: = 1080s（doc_organize 的 ORGANIZE_MAX_CHUNKS/ORGANIZE_LLM_TIMEOUT_S），
#: TTL 必须盖过它——否则慢文档还在整理、锁先过期，第二个请求会拿到新锁并发
#: 跑双份 LLM（用户自己付费）并双写 organized_text。
ORGANIZE_LOCK_TTL_S = 1800

MAX_FILENAME_CHARS = 200

#: 文件名层接受的扩展名：8 个基准族 + 服务端可转 PNG 的图片族（webp/gif/bmp/
#: tiff/avif）。内容层再由嗅探收口（纠正 / 转码 / 拒绝），见
#: ``resolve_upload_content``；HEIC 在文件名层单独给「请转换」文案。
ACCEPTED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".docx",
        ".pptx",
        ".doc",
        ".ppt",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".avif",
    }
)

#: 直存族：内容自证身份，落盘扩展名以**实际内容**为准——声明 .jpg 实为 PNG
#: 是改名不是攻击，纠正尾缀后照常解析。
DIRECT_SNIFF_EXTS = frozenset({".pdf", ".png", ".jpeg"})

#: 转码族：MinerU 云端/本地都只吃 png/jpg/jpeg——服务端解首帧转 PNG 再进链。
IMAGE_TRANSCODE_EXTS = frozenset({".webp", ".gif", ".bmp", ".tiff", ".avif"})

#: 图片转码读入内存的保护上限：超限让用户先压缩，不硬扛 RAM。
IMAGE_TRANSCODE_MAX_BYTES = 64 * 1024 * 1024

#: 落盘前首字节复查（小写比较；嗅探刚验过，这是防呆双保险）。avif 的 ftyp
#: 不在首字节、无法用前缀表达——留空 = 不再复查（以嗅探为准）。
STORAGE_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%pdf-",),
    ".png": (b"\x89png\r\n\x1a\n",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"gif87a", b"gif89a"),
    ".webp": (b"riff",),
    ".bmp": (b"bm",),
    ".tiff": (b"ii*\x00", b"mm\x00*"),
    ".avif": (),
    ".docx": (b"pk\x03\x04",),
    ".pptx": (b"pk\x03\x04",),
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

#: HEIC/HEIF：可识别、不可直接解析（Pillow 无 HEIF 解码）——文件名层直接拦
_HEIC_EXTS = frozenset({".heic", ".heif"})

#: 嗅探读取的头部字节数：ftyp 品牌在 4..12，16 足够覆盖全部签名
_SNIFF_HEAD_BYTES = 16

#: 预览白名单：扩展名 → media type（解析产物目录里只会出现这些）
PREVIEW_MEDIA_TYPES: dict[str, str] = {
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: 页数预估的读取上限：更大的 PDF 不读全文数页。数不出 → 按单文件上限保守
#: 预留（H1），结算时按 MinerU 报告的 total_pages 多退少补。
PDF_PAGE_COUNT_MAX_BYTES = 64 * 1024 * 1024

#: 图片扩展名：MinerU 按单页处理，预留守恒为 1
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})

#: 内部字段：一律不出 API（展示面不需要 + 不泄漏磁盘布局/凭据来源）
_INTERNAL_FIELDS = (
    "user_id",
    "sha256",
    "original_path",
    "original_paths",
    "md_path",
    "content_list_path",
    "mineru_batch_id",
    "mineru_token_src",
    "tenant_id",
)


class OrganizeRequest(BaseModel):
    """整理请求体。kind: free=自由挖掘 / paper=论文复现。"""

    kind: str = Field(..., description="整理口径：free | paper")
    extra: str | None = Field(None, description="额外要求（≤2000 字）")


# ── 纯函数（测试直接打这些点） ──────────────────────────────────────


def sanitize_upload_filename(raw: str | None) -> str:
    """原名 → 可用文件名；不可用抛 400。返回原样的安全基名（保留中文/空格）。"""
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少文件名")
    if len(name) > MAX_FILENAME_CHARS:
        raise HTTPException(
            status_code=400, detail=f"文件名过长（≤{MAX_FILENAME_CHARS} 字符）"
        )
    if "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="非法文件名（不允许路径分隔符）")
    # L3：控制字符（\n\r 等 Cc）可伪造日志行；格式字符（Cf：bidi/零宽）可伪装
    # 文件名——展示面与日志面都不许带它们。
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in name):
        raise HTTPException(status_code=400, detail="非法文件名（包含控制字符）")
    if name.startswith("."):
        raise HTTPException(status_code=400, detail="非法文件名")
    ext = PurePosixPath(name).suffix.lower()
    if ext in _HEIC_EXTS:
        raise HTTPException(
            status_code=400,
            detail=(
                "HEIC 照片（iPhone 默认格式）暂不支持直接解析："
                "请在导出/另存时选择 JPG 或 PNG 后再上传"
            ),
        )
    if ext not in ACCEPTED_EXTENSIONS:
        supported = "、".join(sorted(ACCEPTED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext or '（无扩展名）'}，支持：{supported}",
        )
    return name


#: ftyp 品牌 → HEIC/HEIF 族（mif1/msf1 是通用 HEIF 品牌，同样只能走转换）
_HEIC_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
)
_AVIF_BRANDS = frozenset({b"avif", b"avis"})


def sniff_content_type(head: bytes) -> str | None:
    """文件头（≤16 字节）→ 实际内容类型的规范扩展名；无法识别回 None。

    顺序即优先级。zip/OLE 是**同族容器**（docx vs pptx、doc vs ppt 内容上
    无从分辨），返回容器标记，由 ``resolve_upload_content`` 按声明扩展名
    定形。判定只依赖前 16 字节，绝不读满文件。
    """
    if head[:5].lower() == b"%pdf-":
        return ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"BM"):
        return ".bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return ".heic"
        if brand in _AVIF_BRANDS:
            return ".avif"
    if head.startswith(b"PK\x03\x04"):
        return ".zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".ole"
    return None


def resolve_upload_content(sniffed: str | None, claimed_ext: str) -> str:
    """（实际内容, 声明扩展名）→ 落盘扩展名；不可直接解析抛 400 可读报错。

    内容即真相：声明尾缀只在「内容无从分辨」的容器族（zip/OLE）里参与
    定形，其余以嗅探结果为准。
    """
    if sniffed is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"无法识别的文件内容（声明为 {claimed_ext or '无后缀'}）："
                "请上传 PDF、PNG/JPG 图片或 Word/PPT 文档"
            ),
        )
    if sniffed in DIRECT_SNIFF_EXTS or sniffed in IMAGE_TRANSCODE_EXTS:
        return sniffed
    if sniffed == ".zip":
        if claimed_ext in (".docx", ".pptx"):
            return claimed_ext
        raise HTTPException(
            status_code=400,
            detail=f"文件内容实为 Office 压缩包（docx/pptx），与扩展名 {claimed_ext} 不符",
        )
    if sniffed == ".ole":
        if claimed_ext in (".doc", ".ppt"):
            return claimed_ext
        raise HTTPException(
            status_code=400,
            detail=f"文件内容实为旧版 Office 文档（doc/ppt），与扩展名 {claimed_ext} 不符",
        )
    if sniffed == ".heic":
        raise HTTPException(
            status_code=400,
            detail=(
                "文件实为 HEIC 照片（iPhone 默认格式），暂不支持直接解析："
                "请在导出/另存时选择 JPG 或 PNG 后再上传"
            ),
        )
    raise HTTPException(  # 防御：嗅探表新增类型忘了归族时显形
        status_code=400,
        detail=f"暂不支持的文件内容类型（{sniffed}）",
    )


def _env_mb(name: str, default_mb: int) -> int:
    """env 的 MB 值 → 正整数；脏值回落默认——import/构造期绝不炸。"""
    raw = (os.getenv(name) or "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
        logger.warning("%s=%r 不是正整数，回落默认 %d", name, raw, default_mb)
    return default_mb


def max_upload_bytes() -> int:
    """单文件上传上限（字节）。"""
    return _env_mb(MAX_UPLOAD_ENV, DEFAULT_MAX_UPLOAD_MB) * 1024 * 1024


def max_total_upload_bytes() -> int:
    """单次上传**合计**上限（字节；多文件按逐件累计精验）。

    永不低于单文件上限：配置 ``RD_AGENT_DOC_MAX_MB=800`` 而合计没跟着抬时，
    单文件 600MB 合法却过不了粗闸——取 max 消除这个自相矛盾。
    """
    return (
        max(
            _env_mb(MAX_TOTAL_UPLOAD_ENV, DEFAULT_MAX_TOTAL_UPLOAD_MB),
            _env_mb(MAX_UPLOAD_ENV, DEFAULT_MAX_UPLOAD_MB),
        )
        * 1024
        * 1024
    )


def _enforce_upload_content_length(request: Request) -> None:
    """C1：在读 body **之前**按 Content-Length 粗拦（缺头=411）。

    Starlette 解析 multipart 会先把整个请求体写进临时文件（无内置上限），
    所以上限必须在「解析之前」就生效。多文件后粗闸用**合计上限**（单文件
    上限 × N 可能合法超过它，用单文件上限粗筛会误杀多文件上传）；单文件
    上限由 ``_stream_to_disk`` 流式落盘时分块计数精验（声明值只是提前拒绝
    的粗筛，不可信）。缺 Content-Length（chunked）无法粗筛 → 411 拒绝，
    浏览器/axios/网关转发都带 Content-Length，正常客户端不受影响。
    """
    raw = request.headers.get("content-length")
    if raw is None:
        raise HTTPException(
            status_code=411,
            detail="缺少 Content-Length，无法校验上传大小（不支持 chunked 上传）",
        )
    try:
        declared = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Content-Length 非法") from exc
    limit = max_total_upload_bytes() + MULTIPART_OVERHEAD_BYTES
    if declared < 0 or declared > limit:
        max_mb = max_total_upload_bytes() // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"文件过大（上限 {max_mb}MB）")


async def _stream_to_disk(
    file: UploadFile, dest: Path, *, max_bytes: int, magics: tuple[bytes, ...]
) -> tuple[int, str]:
    """分块落盘 + sha256 边写边算 + 首字节复查；超限抛 413、不符抛 400。

    计数是唯一权威（不信 Content-Length）：分段读到超过 max_bytes 立即抛。
    调用方负责清理半成品（异常路径 rmtree 整个文档目录）。``magics`` 为空
    元组 = 跳过复查（嗅探阶段已定性、签名不在首字节的格式，如 avif）。
    """
    max_mb = max_bytes // (1024 * 1024)
    hasher = hashlib.sha256()
    with dest.open("wb") as fh:
        head = await file.read(_SNIFF_HEAD_BYTES)
        if magics and (not head or not any(head.lower().startswith(m) for m in magics)):
            raise HTTPException(
                status_code=400, detail="文件内容与声明类型不符（落盘复查失败）"
            )
        fh.write(head)
        hasher.update(head)
        written = len(head)
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(
                    status_code=413, detail=f"文件过大（上限 {max_mb}MB）"
                )
            fh.write(chunk)
            hasher.update(chunk)
    return written, hasher.hexdigest()


def _composite_sha256(pairs: list[tuple[str, str]]) -> str:
    """多文件复用键 = sha256(按顺序拼接的 ``name\\0file_sha256\\0``)。

    顺序敏感（文件顺序就是合并顺序，正文/附录互换不是同一份文档）；文件名
    也参与（复用产物里的章节分隔标记贴的原名，同内容改名命中复用会贴错名
    ——宁可重解析一次）。
    """
    hasher = hashlib.sha256()
    for name, digest in pairs:
        hasher.update(f"{name}\0{digest}\0".encode())
    return hasher.hexdigest()


async def _sniff_upload_ext(file: UploadFile) -> str | None:
    """读流首 ≤16 字节嗅探内容类型；读完归位——落盘仍从头开始。"""
    head = await file.read(_SNIFF_HEAD_BYTES)
    await file.seek(0)
    return sniff_content_type(head)


def _sha256_file(path: Path) -> str:
    """文件字节 sha256（分块读；转码件重算复用键用）。"""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _convert_image_to_png_blocking(src: Path) -> None:
    """就地转码：解首帧 → 同目录同名 .png（透明底压白，避免 α 丢成黑底）。

    动图（GIF/WebP）只取首帧——挖掘链吃的是版面内容，帧序列无意义。
    """
    from PIL import Image

    with Image.open(src) as im:
        im.seek(0)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            out = flat
        elif im.mode == "RGB":
            out = im.copy()
        else:
            out = im.convert("RGB")
        out.save(src.with_suffix(".png"), "PNG")


async def _transcode_image_to_png(src: Path) -> Path:
    """转码族落盘后 → PNG（返回新路径，原文件删除）。失败一律 400 可读报错。"""
    if src.stat().st_size > IMAGE_TRANSCODE_MAX_BYTES:
        max_mb = IMAGE_TRANSCODE_MAX_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"图片过大（>{max_mb}MB），请压缩或先转为 PNG/JPG 再上传",
        )
    try:
        await asyncio.to_thread(_convert_image_to_png_blocking, src)
    except ImportError as exc:
        raise HTTPException(
            status_code=400,
            detail="服务器缺少图片转换组件，请先将图片转为 PNG/JPG 再上传",
        ) from exc
    except Exception as exc:  # noqa: BLE001 —— 解码坏件是用户可修 400，不是 500
        raise HTTPException(
            status_code=400, detail="图片解码失败（文件可能损坏），请转换后重试"
        ) from exc
    png_path = src.with_suffix(".png")
    src.unlink(missing_ok=True)
    return png_path


async def _store_uploaded_files(
    files: list[UploadFile],
    filenames: list[str],
    exts: list[str],
    doc_dir: Path,
) -> tuple[list[dict[str, str]], list[int], str]:
    """单/多文件落盘 → (部件清单, 各部件字节数, 复用键 sha256)。

    逐件先嗅探内容定形（改名纠尾缀 / webp 等转 PNG / HEIC 拒绝），再流式落盘：
    - 单文件：``original{ext}``（ext = 定型后的扩展名）；
    - 多文件：``originals/p{i}{ext}``（i 从 1 起，与 MinerU 部件序号对齐），
      逐件精验单文件上限、累计精验合计上限，复用键 = 复合 sha256（转码件按
      转码后字节哈希——复用键跟着真正进解析链的那份内容走）。

    任何异常（含 413/400）由调用方 rmtree 整个文档目录。
    """
    multi = len(files) > 1
    originals_dir = doc_dir / "originals"
    if multi:
        originals_dir.mkdir(parents=True, exist_ok=True)
    total_cap = max_total_upload_bytes()
    manifest: list[dict[str, str]] = []
    sizes: list[int] = []
    pairs: list[tuple[str, str]] = []
    total = 0
    # filenames/exts 由 files 逐件推导，三列表天然同长
    for idx, (file, name, claimed_ext) in enumerate(
        zip(files, filenames, exts, strict=True), 1
    ):
        sniffed = await _sniff_upload_ext(file)
        ext = resolve_upload_content(sniffed, claimed_ext)
        if multi:
            dest = originals_dir / f"p{idx}{ext}"
        else:
            dest = doc_dir / f"original{ext}"
        size, sha256 = await _stream_to_disk(
            file, dest, max_bytes=max_upload_bytes(), magics=STORAGE_MAGIC[ext]
        )
        if ext in IMAGE_TRANSCODE_EXTS:
            dest = await _transcode_image_to_png(dest)
            ext = ".png"
            size = dest.stat().st_size
            sha256 = await asyncio.to_thread(_sha256_file, dest)
        total += size
        if total > total_cap:
            max_mb = total_cap // (1024 * 1024)
            raise HTTPException(
                status_code=413, detail=f"文件合计过大（上限 {max_mb}MB）"
            )
        manifest.append({"path": str(dest), "name": name, "ext": ext})
        sizes.append(size)
        pairs.append((name, sha256))
    if multi:
        return manifest, sizes, _composite_sha256(pairs)
    # 单文件沿用历史口径：复用键 = 该文件字节 sha256（转码件即转码后字节）
    return manifest, sizes, pairs[0][1]


async def _estimate_pages(path: Path, ext: str, size: int) -> int:
    """提交给配额预留的页数预估——**保守值**（H1：宁多留不许多放，结算多退少补）。

    - 图片 = 1 页（MinerU 对图片按单页处理）；
    - PDF 数得动如实报（超过单文件上限由调用方提前 400 拒），读不动/过大 =
      单文件上限；
    - office 族（docx/pptx/doc/ppt）不在上传路径数页——一律按上限预留。
    """
    if ext in IMAGE_EXTENSIONS:
        return 1
    if ext == ".pdf" and size <= PDF_PAGE_COUNT_MAX_BYTES:
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return MAX_PAGES_PER_FILE
        pages = await run_pdf_job(count_pdf_pages, content)
        if pages:
            return int(pages)
    return MAX_PAGES_PER_FILE


def resolve_preview_path(parsed_root: Path, rel_path: str) -> Path:
    """预览相对路径 → 绝对路径；白名单外一律 400，不存在 404。"""
    raw = (rel_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="缺少预览路径")
    if "\x00" in raw or "\\" in raw:
        raise HTTPException(status_code=400, detail="非法预览路径")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or any(part in ("..", ".") for part in rel.parts):
        raise HTTPException(status_code=400, detail="非法预览路径")
    ext = rel.suffix.lower()
    if ext not in PREVIEW_MEDIA_TYPES:
        raise HTTPException(
            status_code=400, detail=f"不支持预览该类型：{ext or '（无扩展名）'}"
        )
    root = parsed_root.resolve()
    target = (root.joinpath(*rel.parts)).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="非法预览路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{raw}")
    return target


def _public_doc(
    row: dict | None, *, include_organized: bool = True, task_count: int | None = None
) -> dict | None:
    """DB 行 → API 视图：剥内部字段；列表不带 organized_text（体积）。

    ``task_count``（该文档关联的挖掘任务数，T-FM-19b）：**None = 未知，键整个
    不出现**——绝不落成 0（0 的语义是「确认没挖过」，查询失败时冒充 0 会让
    已挖过的文档看起来从未挖掘）。
    """
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in _INTERNAL_FIELDS}
    if not include_organized:
        out.pop("organized_text", None)
    if task_count is not None:
        out["task_count"] = task_count
    return out


async def _task_counts_or_none(
    user_id: str, doc_ids: list[str]
) -> dict[str, int] | None:
    """批量取 per-doc 任务数；失败只告警返回 None（列表面降级，不拦主数据）。

    None 与 {} 语义不同：None=查不到（徽标不显示），{} 或缺键=确认 0 条。
    """
    if not doc_ids:
        return {}
    try:
        return await get_mining_task_store().count_tasks_by_docs(
            user_id=user_id, doc_ids=doc_ids
        )
    except Exception as exc:  # noqa: BLE001 —— 徽标是辅助信息，坏掉不拦文档列表
        logger.warning("[docs] task_count 批量查询失败（列表不带任务数）：%s", exc)
        return None


async def _tasks_or_none(user_id: str, doc_id: str) -> list[dict] | None:
    """取该文档的任务明细；失败只告警返回 None（同上：None=未知，[]=确认没有）。"""
    try:
        return await get_mining_task_store().list_by_doc(user_id=user_id, doc_id=doc_id)
    except Exception as exc:  # noqa: BLE001 —— 明细是辅助信息，坏掉不拦文档详情
        logger.warning("[docs] 任务明细查询失败 doc=%s：%s", doc_id, exc)
        return None


def _require_owned_doc(doc: dict | None, doc_id: str) -> dict:
    """归属收口：没有 / 别人的 / 已删的一律 404（不多说一个字）。"""
    if not doc or doc.get("status") == "deleted":
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    return doc


# ── 端点 ────────────────────────────────────────────────────────────


@router.post("/docs/upload")
async def upload_doc(request: Request) -> dict:
    """multipart 上传（单/多文件）→ 落盘 + 建行 → sha256 复用或提交 MinerU 解析。

    多文件（T-FM-19a）：同一字段 ``file`` 重复出现（≤ ``MAX_DOC_FILES`` 件，
    顺序=合并顺序）。单文件与历史逐字节同口径（``original{ext}``、行不写
    manifest）。

    刻意**不**声明 ``file: UploadFile = File(...)`` 参数（安全审查 C1）：
    FastAPI 会在进入 handler 前把整个 multipart 无上限落临时文件。这里先
    认证、再按 Content-Length 粗拦，然后才 ``request.form()`` 解析。
    """
    user_id, tenant_id = get_authenticated_identity(request)
    quota = get_doc_quota()
    try:
        quota.check_rate(user_id, "upload")
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    _enforce_upload_content_length(request)
    form = await request.form()
    raw_files = form.getlist("file")
    # 比对的必须是 starlette.datastructures.UploadFile：request.form() 产出的
    # 文件对象是 starlette 类，不是它的子类 fastapi.UploadFile——比子类会让
    # 真请求全数 400（测试替身用 starlette 类，钉住这一条）。
    if not raw_files or any(not isinstance(f, UploadFile) for f in raw_files):
        raise HTTPException(status_code=400, detail="缺少文件（multipart 字段名 file）")
    if len(raw_files) > MAX_DOC_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多上传 {MAX_DOC_FILES} 个文件（收到 {len(raw_files)} 个）",
        )
    filenames = [sanitize_upload_filename(f.filename) for f in raw_files]
    exts = [PurePosixPath(name).suffix.lower() for name in filenames]
    multi = len(raw_files) > 1
    display_name = (
        f"{filenames[0]}（共{len(filenames)}个文件）" if multi else filenames[0]
    )

    store = get_doc_store()
    svc = get_doc_parse_service()
    # 通道预检走「有效配置」口径（因子挖掘设置 > env）：没配就别收文件——
    # 先收再发现不可用会留下永远解析不了的孤儿。
    effective_cfg, _cfg_src = await resolve_effective_mineru_config(user_id, tenant_id)
    if effective_cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "文档解析服务未就绪：未配置 MinerU 解析通道。"
                "请在「因子挖掘 → 文档解析设置」配置 MinerU API Token"
                "（或本地/局域网 MinerU 服务地址），"
                "或在服务器 .env 配置 MINERU_API_TOKEN / MINERU_LOCAL_URL。"
            ),
        )

    doc_id = uuid.uuid4().hex[:16]
    doc_dir = svc.doc_dir(doc_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    try:
        parts, sizes, sha256 = await _store_uploaded_files(
            raw_files, filenames, exts, doc_dir
        )
    except HTTPException:
        shutil.rmtree(doc_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 —— 磁盘/流异常统一可读 500
        shutil.rmtree(doc_dir, ignore_errors=True)
        logger.exception("[docs] 上传落盘失败 doc=%s", doc_id)
        raise HTTPException(status_code=500, detail="上传写入失败，请重试") from exc
    size = sum(sizes)
    # 以部件清单里的定型扩展名建行（改名纠尾缀 / webp 转 PNG 后 ext 可能已变）
    ext = parts[0]["ext"]

    # L5：建行/复用抛错时已落盘的原件不许变成孤儿（≤ 合计上限）。
    try:
        await store.create_doc(
            doc_id=doc_id,
            user_id=user_id,
            filename=display_name,
            ext=ext,
            size_bytes=size,
            sha256=sha256,
            original_path=parts[0]["path"],
            status="uploaded",
            # 行上带 tenant：重启续轮询时凭 (user_id, tenant_id) 重读用户 Token
            tenant_id=tenant_id,
            files_count=len(parts),
            # 单文件行不写 manifest（历史逐字节同口径）；多文件记部件清单
            original_paths=parts if multi else None,
        )
        doc = await store.get_doc(doc_id, user_id=user_id)
        reused = False
        if doc is not None:
            reused = await svc.maybe_reuse(doc)
    except Exception:
        shutil.rmtree(doc_dir, ignore_errors=True)
        try:
            await store.hard_delete(doc_id, user_id=user_id)
        except Exception as cleanup_exc:  # noqa: BLE001 —— 清理失败只告警（行有 stale 对账兜底）
            logger.warning("[docs] 上传失败清理行 %s 异常: %s", doc_id, cleanup_exc)
        raise
    if not reused and doc is not None:
        # H2：MinerU 上传窗口可长，先确认行还在（被删除就别烧共享配额）
        latest = await store.get_doc(doc_id, user_id=user_id)
        if latest is None or latest.get("status") == "deleted":
            shutil.rmtree(doc_dir, ignore_errors=True)
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        # 页数预估：conservative，单文件上限逐件判（多文件 = 各件之和）
        pages = 0
        for part, part_size in zip(parts, sizes, strict=True):  # 同一函数返回，逐件对齐
            estimate = await _estimate_pages(Path(part["path"]), part["ext"], part_size)
            if estimate > MAX_PAGES_PER_FILE:
                shutil.rmtree(doc_dir, ignore_errors=True)
                await store.hard_delete(doc_id, user_id=user_id)
                if multi:
                    raise HTTPException(
                        status_code=400,
                        detail=f"「{part['name']}」超过单文件 {MAX_PAGES_PER_FILE} 页上限，"
                        "请拆分后重试",
                    )
                raise HTTPException(
                    status_code=400,
                    detail=f"文件超过单文件 {MAX_PAGES_PER_FILE} 页上限，请拆分后重试",
                )
            pages += estimate
        try:
            # 本地/局域网通道不烧平台云配额：按有效配置传按次开关
            quota.reserve(
                user_id,
                pages,
                doc_id=doc_id,
                accounting=effective_cfg.mode != MODE_LOCAL,
            )
        except QuotaExceeded as exc:
            # 从未进入解析链的行：连根清（目录 + 行），不留「永远解析不了」的滞留
            shutil.rmtree(doc_dir, ignore_errors=True)
            await store.hard_delete(doc_id, user_id=user_id)
            logger.info("[docs] 配额拦下上传 doc=%s user=%s", doc_id, user_id)
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        try:
            await svc.submit_parse(latest)
        except MineruError as exc:
            # 行已被 submit_parse 定格 parse_failed（可见的失败），留着
            raise HTTPException(status_code=502, detail=f"提交解析失败：{exc}") from exc

    fresh = await store.get_doc(doc_id, user_id=user_id)
    logger.info(
        "[docs] 上传完成 doc=%s user=%s file=%s size=%d reused=%s",
        doc_id,
        user_id,
        display_name,
        size,
        reused,
    )
    return {"code": 200, "data": {"doc": _public_doc(fresh), "reused": reused}}


@router.get("/docs")
async def list_docs(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """本人文档列表（不含已删；organized_text 只在详情带）。

    每行带 ``task_count``（已挖掘方向数，T-FM-19b）；任务数查询失败时该键
    整体缺省（未知 ≠ 0——见 :func:`_public_doc`）。
    """
    user_id, _tenant_id = get_authenticated_identity(request)
    try:
        filters = resolve_list_filters(status=status, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = get_doc_store()
    items = await store.list_docs(
        user_id=user_id,
        status=filters["status"],
        limit=filters["limit"],
        offset=filters["offset"],
    )
    total = await store.count_docs(user_id=user_id, status=filters["status"])
    counts = await _task_counts_or_none(user_id, [str(r["doc_id"]) for r in items])
    return {
        "code": 200,
        "data": {
            "items": [
                _public_doc(
                    r,
                    include_organized=False,
                    task_count=None
                    if counts is None
                    else counts.get(str(r["doc_id"]), 0),
                )
                for r in items
            ],
            "total": total,
            "limit": filters["limit"],
            "offset": filters["offset"],
        },
    }


@router.get("/docs/quota")
async def doc_quota_status(request: Request) -> dict:
    """当日解析配额（北京时间日界）：已用/上限/余量 + 通道是否配置。

    ``token_configured`` = 解析通道已配（云端 Token 或本地服务地址，
    用户设置 > env）；``mineru_mode`` = 生效通道（cloud/local/None）。
    本地通道的文档不计平台云配额，前端据此把页数配额显示为「不限」。

    ⚠️ 必须注册在 ``/docs/{doc_id}`` **之前**，否则被参数路由吞掉
    （P0 的 /tasks/history 同款教训，有路由顺序回归测试钉住）。
    """
    user_id, tenant_id = get_authenticated_identity(request)
    st = get_doc_quota().status(user_id)
    data = asdict(st)
    # 有效配置口径（因子挖掘设置 > env）：前端据此提示「去哪配」
    cfg, _src = await resolve_effective_mineru_config(user_id, tenant_id)
    data["token_configured"] = cfg is not None
    data["mineru_mode"] = cfg.mode if cfg is not None else None
    return {"code": 200, "data": data}


#: 统计面的状态桶（不含 deleted：已删行既不可见也不该进失败率分母）
STATS_STATUSES = tuple(s for s in DOC_STATUSES if s != "deleted")


@router.get("/docs/stats")
async def docs_stats(request: Request) -> dict:
    """本人文档的解析计数与失败率 + 平台配额快照（可观测面）。

    - ``counts``：各状态行数（不含 deleted）；
    - ``attempted`` = 解析已定局的行（parsed/organized/parse_failed/expired，
      含 GC 转 expired 的——它们都真实占过页）；在途的 uploaded/parsing 不计，
      否则失败率被在途稀释；
    - ``failure_rate`` = parse_failed / attempted（attempted=0 → 0.0，不虚报）；
    - ``quota``：与 ``/docs/quota`` 同一份（平台余量 + token 是否配置）。

    ⚠️ 必须注册在 ``/docs/{doc_id}`` **之前**，否则被参数路由吞掉
    （与 /docs/quota 同一路由顺序教训，有回归测试钉住）。
    """
    user_id, tenant_id = get_authenticated_identity(request)
    store = get_doc_store()
    counts: dict[str, int] = {}
    for status in STATS_STATUSES:
        counts[status] = await store.count_docs(user_id=user_id, status=status)
    total = await store.count_docs(user_id=user_id, status=None)
    attempted = (
        counts["parsed"]
        + counts["organized"]
        + counts["parse_failed"]
        + counts["expired"]
    )
    failed = counts["parse_failed"]
    quota_data = asdict(get_doc_quota().status(user_id))
    cfg, _src = await resolve_effective_mineru_config(user_id, tenant_id)
    quota_data["token_configured"] = cfg is not None
    quota_data["mineru_mode"] = cfg.mode if cfg is not None else None
    return {
        "code": 200,
        "data": {
            "total": total,
            "counts": counts,
            "attempted": attempted,
            "parse_failed": failed,
            "failure_rate": round(failed / attempted, 4) if attempted else 0.0,
            "quota": quota_data,
        },
    }


class MineruSettingsPayload(BaseModel):
    """PUT /docs/mineru-settings 请求体。

    密钥字段（api_token / local_api_key）**留空 = 保留原值**：前端回显的
    是掩码，回存掩码会把真密钥写坏；清除走整条 DELETE。
    ``local_tier=""`` = 清为服务端默认档。
    """

    mode: str
    api_token: str | None = None
    local_url: str | None = None
    local_api_key: str | None = None
    local_tier: str | None = None


async def _mineru_settings_view(user_id: str, tenant_id: str) -> dict:
    """设置页视图：掩码后的用户设置 + 生效来源 + 部署 env 概览。

    ``readable=False``（Redis 读不到）必须与「没配」区分开——把读故障
    显示成空设置会诱导用户覆盖掉自己已存的密钥（旧 Profile 网关同款纪律）。
    """
    store = get_doc_mining_settings_store()
    try:
        settings = store.get(user_id, tenant_id, strict=True)
        readable = True
    except DocMiningSettingsError:
        settings = None
        readable = False
    effective_cfg, src = await resolve_effective_mineru_config(user_id, tenant_id)
    env_cfg = resolve_mineru_config()
    return {
        "settings": settings.to_public() if settings is not None else None,
        "readable": readable,
        "source": src,
        "effective_mode": effective_cfg.mode if effective_cfg is not None else None,
        "env_configured": env_cfg is not None,
        "env_mode": env_cfg.mode if env_cfg is not None else None,
    }


@router.get("/docs/mineru-settings")
async def get_mineru_settings(request: Request) -> dict:
    """读取本人 MinerU 解析设置（因子挖掘内配置；密钥只回掩码）。

    ⚠️ 与 /docs/quota 同一路由顺序纪律：必须注册在 ``/docs/{doc_id}`` 之前。
    """
    user_id, tenant_id = get_authenticated_identity(request)
    return {"code": 200, "data": await _mineru_settings_view(user_id, tenant_id)}


@router.put("/docs/mineru-settings")
async def save_mineru_settings(
    request: Request, payload: MineruSettingsPayload
) -> dict:
    """保存本人 MinerU 解析设置（保存即校验，失败 400 带可读原因）。"""
    user_id, tenant_id = get_authenticated_identity(request)
    store = get_doc_mining_settings_store()
    try:
        store.save(user_id, tenant_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— Redis 写失败：固定文案，不回显内部错误
        logger.exception("[docs] 解析设置保存失败 user=%s", user_id)
        raise HTTPException(status_code=500, detail="保存失败，请稍后重试") from exc
    return {"code": 200, "data": await _mineru_settings_view(user_id, tenant_id)}


@router.delete("/docs/mineru-settings")
async def clear_mineru_settings(request: Request) -> dict:
    """清除本人 MinerU 解析设置（回到部署默认通道）。"""
    user_id, tenant_id = get_authenticated_identity(request)
    store = get_doc_mining_settings_store()
    try:
        store.clear(user_id, tenant_id)
    except Exception as exc:  # noqa: BLE001 —— 同上：固定文案
        logger.exception("[docs] 解析设置清除失败 user=%s", user_id)
        raise HTTPException(status_code=500, detail="清除失败，请稍后重试") from exc
    return {"code": 200, "data": await _mineru_settings_view(user_id, tenant_id)}


@router.get("/docs/{doc_id}")
async def get_doc_detail(request: Request, doc_id: str) -> dict:
    """文档详情（含 organized_text、解析进度字段与关联挖掘任务）。

    ``data.tasks`` = 该文档的挖掘任务明细（最近 ≤20 条，一文档多方向）；
    ``doc.task_count`` = 真实总数（明细截断时界面报「共 N 个」）。
    两者各自独立降级：查询失败则该键缺省，绝不冒充空列表/0。
    """
    user_id, _tenant_id = get_authenticated_identity(request)
    doc = _require_owned_doc(
        await get_doc_store().get_doc(doc_id, user_id=user_id), doc_id
    )
    counts = await _task_counts_or_none(user_id, [doc_id])
    tasks = await _tasks_or_none(user_id, doc_id)
    data: dict = {
        "doc": _public_doc(
            doc,
            task_count=None if counts is None else counts.get(doc_id, 0),
        )
    }
    if tasks is not None:
        data["tasks"] = tasks
    return {"code": 200, "data": data}


@router.get("/docs/{doc_id}/file")
async def preview_doc_file(request: Request, doc_id: str, path: str = "full.md"):
    """解析产物内联预览（白名单：.md + 图片；不传 filename → 浏览器内联）。"""
    user_id, _tenant_id = get_authenticated_identity(request)
    doc = _require_owned_doc(
        await get_doc_store().get_doc(doc_id, user_id=user_id), doc_id
    )
    md_path_raw = doc.get("md_path")
    if not md_path_raw:
        raise HTTPException(
            status_code=409,
            detail=(
                f"文档尚未解析完成（当前状态 {doc.get('status') or '未知'}），"
                "暂无可预览内容"
            ),
        )
    target = resolve_preview_path(Path(str(md_path_raw)).parent, path)
    return FileResponse(target, media_type=PREVIEW_MEDIA_TYPES[target.suffix.lower()])


@router.post("/docs/{doc_id}/organize")
async def organize_doc(request: Request, doc_id: str, payload: OrganizeRequest) -> dict:
    """整理解析文本 → 结构化草稿（落库 organized_text，记录模板版本）。

    同步等待 LLM（长文 map-reduce 可达分钟级）；前端 axios 需放宽超时。
    """
    user_id, tenant_id = get_authenticated_identity(request)
    kind = (payload.kind or "").strip()
    if kind not in ORGANIZE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"未知整理口径 {kind!r}（支持 {'、'.join(ORGANIZE_KINDS)}）",
        )

    store = get_doc_store()
    doc = _require_owned_doc(await store.get_doc(doc_id, user_id=user_id), doc_id)

    quota = get_doc_quota()
    try:
        quota.check_rate(user_id, "organize")
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    lock_name = f"organize:{doc_id}"
    lock_token = quota.try_lock(lock_name, ttl_s=ORGANIZE_LOCK_TTL_S)
    if not lock_token:
        raise HTTPException(status_code=409, detail="该文档正在整理中，请稍候再试")

    try:
        llm_config, llm_source, _embedding_env = await _resolve_effective_llm_config(
            user_id, tenant_id
        )
        if llm_config is None:
            raise HTTPException(
                status_code=412,
                detail="未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写（与 AI-IDE 共用），"
                "或在服务器 .env 配置 DEEPSEEK_API_KEY / AI_IDE_LLM_API_KEY / OPENAI_API_KEY。",
            )
        logger.info(
            "[docs] organize doc=%s user=%s kind=%s llm_source=%s",
            doc_id,
            user_id,
            kind,
            llm_source,
        )
        try:
            result = await organize_and_store(
                store, doc, kind=kind, extra=payload.extra, config=llm_config
            )
        except OrganizeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        # compare-and-delete：只解自己那把（锁若已过期并被后来者重取，不误删）
        quota.unlock(lock_name, lock_token)

    # H2：LLM 期间用户可能已删除——落库被 deleted 守卫拦下（没复活），
    # 这里如实 404，不把「已删除」的文档当成功返回。
    fresh = _require_owned_doc(await store.get_doc(doc_id, user_id=user_id), doc_id)
    return {
        "code": 200,
        "data": {
            "kind": result["kind"],
            "prompt_version": result["prompt_version"],
            "payload": result["payload"],
            "markdown": result["markdown"],
            "truncated": result["truncated"],
            "chunks_used": result["chunks_used"],
            "doc": _public_doc(fresh),
        },
    }


@router.delete("/docs/{doc_id}")
async def delete_doc(request: Request, doc_id: str) -> dict:
    """连根删：先取消在途轮询 → rmtree 目录 → 软删行（行保留审计）。"""
    user_id, _tenant_id = get_authenticated_identity(request)
    store = get_doc_store()
    doc = _require_owned_doc(await store.get_doc(doc_id, user_id=user_id), doc_id)

    svc = get_doc_parse_service()
    try:
        doc_dir = svc.doc_dir(doc_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Document {doc_id} not found"
        ) from exc
    await svc.cancel(doc_id)
    shutil.rmtree(doc_dir, ignore_errors=True)
    hit = await store.soft_delete(doc_id, user_id=user_id)
    if not hit:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    # H1（2026-10-09 修订）：批次已建（MinerU 拿到文件、无取消 API、照常
    # 计费）→ 不退预留（退 = 账面与真实账单脱钩；每用户日限、余量告警全部
    # 失真——上传→秒删循环能在账面为零的情况下烧穿平台额度），改挂**只记
    # 账收尾**盯批次到终态：云端实际只烧了几页就结到几页。绝不再即刻把
    # 预留原样记成已用——docx 预留 200 页、云端只烧 2 页，删后无人收账就是
    # 幽灵占账、当日配额假性用尽（实案见 doc_parse_service 的 _reconcile_gone）。
    # 从未提交的行（无批次号）费用没发生 → 全退。已 settle 的行
    # （parsed/organized）收尾查批次已是终态，结算 = 对已取走的预留如实补记
    # （幂等无害）；预留键 24h TTL 是进程停机的最后兜底。
    if doc.get("mineru_batch_id"):
        svc.schedule_quota_reconcile(doc)
    else:
        try:
            get_doc_quota().release(doc_id, user_id, accounting=doc_accounting(doc))
        except Exception as exc:  # noqa: BLE001 —— 释放失败只告警（TTL 兜底回收）
            logger.warning("[docs] 删除释放配额预留失败 doc=%s: %s", doc_id, exc)
    # H2：软删落地后二次清扫——抓在途写（下载/解包）在 status 检查与 rmtree
    # 之间竞态写回的半成品；此后一切写入都被 deleted 守卫拦下。
    shutil.rmtree(doc_dir, ignore_errors=True)
    logger.info("[docs] 删除 doc=%s user=%s", doc_id, user_id)
    return {"code": 200, "data": {"doc_id": doc_id, "deleted": True}}
