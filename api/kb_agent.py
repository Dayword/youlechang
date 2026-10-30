# ============================================================
# 知识库智能体 API 路由 - /admin/kb-agent/*（需管理员登录）
# ------------------------------------------------------------
# 闭环链路（商家 PC「知识库智能体」）：
#   1) POST /admin/kb-agent/analyze  (SSE) 统计未命中 → 生成标准答案 → 导出 Word
#   2) GET  /admin/kb-agent/download/{name} 下载生成的 Word（管理员改完回传）
#   3) POST /admin/kb-agent/ingest   (SSE) 回传 Word → 解析 → 切片 → 入库（立即供检索）
#
# 与 /chat/stream 一致：SSE 帧格式为 `data: {json}\n\n`，
# 前端用 fetch + ReadableStream 消费（POST 无法用原生 EventSource）。
# ============================================================
import asyncio
import json
import logging
import os
import re
import time
from typing import Generator, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import DOCS_DIR, KB_AGENT_OUT_DIR, KB_AGENT_DAYS, KB_AGENT_LIMIT, KB_AGENT_ANSWER_TOKENS
from core.auth import get_current_admin
from services.kb_agent import kb_agent, detect_intent, KB_CURATOR_PREFIX

logger = logging.getLogger(__name__)

# router 级依赖：本组全部为知识库运营操作，统一要求管理员身份
router = APIRouter(prefix="/admin/kb-agent", tags=["知识库智能体"],
                   dependencies=[Depends(get_current_admin)])

# 允许回传的文档格式与大小上限（知识库补充文档都是文本类，20MB 足够）
ALLOWED_EXTENSIONS = {".docx", ".txt", ".pdf"}
MAX_FILE_SIZE = 20 * 1024 * 1024

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",   # 关闭 nginx 缓冲，保证逐条事件实时到达
}


# ============================================================
# 工具：同步生成器 → SSE StreamingResponse
# ============================================================
def _sse(gen: Generator[dict, None, None]) -> StreamingResponse:
    """把 kb_agent 的同步事件生成器包成 SSE 响应。

    每个 next() 丢到线程池执行，避免 LLM/入库这类阻塞操作卡住事件循环、
    导致 SSE 心跳与后续事件无法送出（与 api/chat.py 的流式处理同一思路）。
    """
    loop = asyncio.get_event_loop()
    _DONE = object()

    def _safe_next():
        try:
            return next(gen)
        except StopIteration:
            return _DONE
        except Exception as e:                          # pragma: no cover
            logger.error(f"[kb_agent] 流式处理异常: {e}", exc_info=True)
            return {"type": "error", "content": f"处理出错：{str(e)[:160]}"}

    async def event_generator():
        try:
            while True:
                event = await loop.run_in_executor(None, _safe_next)
                if event is _DONE:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0)
        except Exception as e:                          # pragma: no cover
            logger.error(f"[kb_agent] SSE 管道异常: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': f'连接异常，请重试：{str(e)[:120]}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# ============================================================
# 工具：文件名安全化（防目录穿越 / 非法字符）
# ============================================================
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_name(name: str) -> str:
    """只保留纯文件名，替换 Windows 非法字符，去掉首尾点与空格"""
    name = os.path.basename((name or "").strip().replace("\\", "/"))
    name = _UNSAFE_NAME_RE.sub("_", name).strip(". ")
    return name[:120]


# ============================================================
# 1) 统计 → 标准答案 → Word（SSE）
# ============================================================
class AnalyzeRequest(BaseModel):
    """管理员发送的指令（message 自由文本，其余参数可显式覆盖）

    generate / export 用 Optional：None = 由 detect_intent 从 message 推断
    （例如"只统计"→ 不生成答案也不出文档），显式 true/false 则以此为准。
    """
    message: str = ""
    days: Optional[int] = None
    limit: Optional[int] = None
    max_tokens: Optional[int] = None
    generate: Optional[bool] = None
    export: Optional[bool] = None


@router.post("/analyze", summary="统计未命中问题并生成标准答案 Word（SSE）")
async def analyze(req: AnalyzeRequest):
    """把管理员的一句话解析成 意图+参数，然后流式返回全流程进度。

    未显式传参时用 detect_intent 从 message 里提取：
      「帮我统计所有未检索到知识库的用户问题，基于知识库精细化回答，token 300 以内，生成文档」
      → intent=analyze, days=0, limit=20, max_tokens=300
    """
    msg = (req.message or "").strip()
    parsed = detect_intent(msg, has_file=False)

    days = parsed["days"] if req.days is None else max(0, int(req.days))
    limit = parsed["limit"] if req.limit is None else max(1, int(req.limit))
    max_tokens = parsed["max_tokens"] if req.max_tokens is None else max(64, int(req.max_tokens))
    generate = parsed["generate"] if req.generate is None else bool(req.generate)
    export = parsed["export"] if req.export is None else bool(req.export)
    if not generate:
        export = False            # 不出答案就没必要出文档

    logger.info(f"[kb_agent] 分析请求: {msg[:60]!r} → days={days} limit={limit} "
                f"max_tokens={max_tokens} generate={generate} export={export}")

    gen = kb_agent.analyze_stream(message=msg, days=days, limit=limit,
                                  max_tokens=max_tokens,
                                  generate=generate, export=export)

    # 先补一条 intent 事件，方便前端把"我理解的指令"回显给管理员（便于纠偏）
    def _with_intent() -> Generator[dict, None, None]:
        yield {"type": "intent", "intent": parsed["intent"], "reason": parsed["reason"],
               "days": days, "limit": limit, "max_tokens": max_tokens,
               "generate": generate, "export": export}
        for ev in gen:
            yield ev

    return _sse(_with_intent())


# ============================================================
# 2) 下载 / 查看已生成的 Word
# ============================================================
@router.get("/docs", summary="列出智能体生成的 Word 文档")
async def list_docs(limit: int = 20):
    """列出 static/kb_agent 下生成的补充标准答案文档（最新的在前）"""
    limit = min(max(1, limit), 100)
    if not os.path.isdir(KB_AGENT_OUT_DIR):
        return {"code": 200, "data": []}
    items = []
    for fn in os.listdir(KB_AGENT_OUT_DIR):
        if not fn.lower().endswith((".docx", ".txt")):
            continue
        path = os.path.join(KB_AGENT_OUT_DIR, fn)
        if not os.path.isfile(path):
            continue
        st = os.stat(path)
        items.append({
            "name": fn,
            "size": st.st_size,
            "modified": st.st_mtime,
            "url": f"/admin/kb-agent/download/{fn}",
        })
    items.sort(key=lambda x: x["modified"], reverse=True)
    return {"code": 200, "data": items[:limit]}


@router.get("/download/{name}", summary="下载智能体生成的 Word 文档")
async def download(name: str):
    """下载生成的 Word（管理员拿到后直接修改，再回传入库）"""
    fn = _safe_name(name)
    path = os.path.join(KB_AGENT_OUT_DIR, fn)
    if not fn or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="文件不存在或已过期，请重新生成")
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
             if fn.lower().endswith(".docx") else "text/plain; charset=utf-8")
    return FileResponse(path, filename=fn, media_type=media)


# ============================================================
# 3) 回传 Word → 解析 / 切片 / 入库（SSE）
# ============================================================
def _doc_exists(doc_name: str) -> bool:
    return os.path.isfile(os.path.join(DOCS_DIR, doc_name))


@router.post("/ingest", summary="回传 Word 文档，解析切片并入库（SSE）")
async def ingest(
    file: UploadFile = File(...),
    replace_source: str = Form(""),
):
    """管理员改完 Word 回传 → 解析 → 按标题块切片 → 写入 Milvus + BM25 → 清缓存重载问法库。

    - 同名文件默认按"更新"处理：先清掉旧切片/向量再重新写入，避免重复堆叠；
    - replace_source 可显式指定要替换的旧来源（默认取本次 doc_name）。
    """
    raw_name = _safe_name(file.filename or "")
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400,
                            detail=f"不支持的文件格式 '{ext or '未知'}'，仅支持: "
                                   f"{', '.join(sorted(ALLOWED_EXTENSIONS))}")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413,
                            detail=f"文件过大（{len(content) / 1024 / 1024:.1f}MB），最大支持 20MB")

    # 入库文档统一落 docs/，这样「知识库管理」页也能看到这份文件
    os.makedirs(DOCS_DIR, exist_ok=True)
    doc_name = raw_name
    existed_before = _doc_exists(doc_name)
    save_path = os.path.join(DOCS_DIR, doc_name)
    with open(save_path, "wb") as f:
        f.write(content)

    # 同名覆盖 或 显式指定旧来源 → 先删旧切片与向量
    replace = _safe_name(replace_source) if replace_source else ""
    if not replace and existed_before:
        replace = doc_name

    logger.info(f"[kb_agent] 回传入库: {doc_name}（{len(content)} bytes，"
                f"替换来源={replace or '无'}）")

    gen = kb_agent.ingest_stream(save_path, doc_name, replace_source=replace)

    def _with_head() -> Generator[dict, None, None]:
        yield {"type": "intent", "intent": "ingest",
               "reason": "识别到文档回传，执行解析 → 切片 → 入库",
               "doc_name": doc_name, "size": len(content),
               "replace_source": replace}
        failed = False
        try:
            for ev in gen:
                if ev.get("type") == "error" or (
                        ev.get("type") == "step" and ev.get("status") == "failed"):
                    failed = True
                yield ev
        finally:
            # 入库失败且本次是新建文件 → 清理落盘文件，避免 docs/ 留垃圾
            if failed and not existed_before:
                try:
                    os.remove(save_path)
                    logger.info(f"[kb_agent] 入库失败，已清理: {save_path}")
                except Exception:
                    pass

    return _sse(_with_head())


# ============================================================
# 4) 运行参数（前端展示"当前默认口径"，也便于排查）
# ============================================================
@router.get("/config", summary="知识库智能体默认参数")
async def get_config():
    from agents.retrieval_agent import retrieval_agent
    try:
        stats = retrieval_agent.get_index_stats()
    except Exception:
        stats = {}
    return {
        "code": 200,
        "data": {
            "default_days": KB_AGENT_DAYS,
            "default_limit": KB_AGENT_LIMIT,
            "default_max_tokens": KB_AGENT_ANSWER_TOKENS,
            "doc_prefix": KB_CURATOR_PREFIX,
            "index": stats,
        },
    }
