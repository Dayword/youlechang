# ============================================================
# 知识库管理 API 路由 - /knowledge/*（需管理员登录）
# 修复：502超时问题、支持txt/word/pdf、异步处理、大文件上传
# ============================================================
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Depends
import os
import time
import logging
from config import DOCS_DIR

from core.auth import get_current_admin

logger = logging.getLogger(__name__)
# router 级依赖：本组所有端点均为知识库管理操作，统一要求管理员身份
router = APIRouter(prefix="/knowledge", tags=["知识库管理"], dependencies=[Depends(get_current_admin)])

# 确保 docs 目录存在
os.makedirs(DOCS_DIR, exist_ok=True)

# 允许的文件扩展名
ALLOWED_EXTENSIONS = {'.pdf', '.txt', '.docx', '.doc'}

# 最大文件大小 50MB
MAX_FILE_SIZE = 50 * 1024 * 1024


def _validate_file(filename: str, file_size: int = 0):
    """验证上传文件"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式 '{ext}'，仅支持: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{file_size / 1024 / 1024:.1f}MB），最大支持 50MB"
        )


def _apply_dedup_gate(save_path: str, filename: str):
    """写入口统一去重闸门：剔掉与现有知识库重复的问答块。

    为什么「知识库管理」的上传也要过这一道：
      * 本接口会给文件名加 `_{时间戳}` 后缀，**同一份文档再传一次必然变成新来源**，
        旧实现就是 append，天然堆出两份 —— 这是"库里出现重复"的另一条路径；
      * 所有写入都过同一道闸门，才能保证"同一问题只有一条答案"这条不变量。

    只干预"问答块结构"的文档（有 `## ` 标题块且块内含「标准答案：」标记）；
    自由格式资料原样放行，避免用只适用于问答块的规则误伤普通文档。

    Returns: (供入库的文件路径, 冲突清单)。发生去重时路径会指向一个临时 txt。
    """
    ext = os.path.splitext(save_path)[1].lower()
    if ext not in (".docx", ".txt"):
        return save_path, []                       # pdf/doc 走 OCR/旧格式，不做问题级去重
    try:
        from services.kb_agent import kb_agent
        from agents.document_agent import document_agent
        if ext == ".docx":
            raw = document_agent.extract_text_from_docx(save_path)
        else:
            raw = document_agent.extract_text_from_txt(save_path)
    except Exception as e:
        logger.warning(f"去重预检读取失败（原样入库）: {e}")
        return save_path, []

    try:
        cleaned, conflicts, blocks = kb_agent.dedup_filter_document(raw, filename)
    except Exception as e:
        logger.warning(f"去重预检失败（原样入库）: {e}")
        return save_path, []

    if not blocks or not conflicts:
        return save_path, []
    if not cleaned.strip():
        raise HTTPException(
            status_code=409,
            detail=f"文档 {filename} 的 {blocks} 个问答块全部与现有知识库重复"
                   f"（同一问题已有答案），已拒绝入库。若确需修改，请修改已有那份文档后回传。")

    tmp_path = save_path + ".dedup.txt"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    logger.info(f"[knowledge] 去重闸门：{filename} 识别 {blocks} 块，"
                f"保留 {blocks - len(conflicts)} 块，跳过 {len(conflicts)} 块")
    for c in conflicts[:10]:
        logger.info(f"[knowledge]   跳过「{c['question']}」← {c['reason']}"
                    f"（命中「{c.get('matched') or c.get('source') or '-'}」）")
    return tmp_path, conflicts


def _do_ingest(save_path: str, filename: str, tmp_path: str = "") -> dict:
    """在后台线程中执行入库操作"""
    try:
        from agents.document_agent import document_agent
        chunk_count = document_agent.ingest_document(save_path, filename)
        logger.info(f"入库完成: {filename}, chunk数: {chunk_count}")
        # 知识库变更后清空缓存 + 重载问法库
        from core.cache import redis_cache
        redis_cache.flush_cache()
        from agents.query_agent import query_agent
        query_agent.reload_patterns()
        try:
            from services.kb_dedup import invalidate_cache
            invalidate_cache()
        except Exception:
            pass
        return {"success": True, "chunk_count": chunk_count}
    except Exception as e:
        logger.error(f"入库失败: {filename}, 错误: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@router.post("/upload", summary="上传文档并异步入库（推荐）")
async def upload_document(file: UploadFile = File(...)):
    """
    上传文档（PDF/TXT/DOCX/DOC），异步后台入库
    立即返回 task_id，不阻塞请求
    """
    _validate_file(file.filename)

    # 生成唯一文件名避免覆盖
    base, ext = os.path.splitext(file.filename)
    timestamp = int(time.time())
    safe_name = f"{base}_{timestamp}{ext}"
    save_path = os.path.join(DOCS_DIR, safe_name)

    # 保存文件
    content = await file.read()
    _validate_file(file.filename, len(content))

    with open(save_path, "wb") as f:
        f.write(content)

    # 入库前去重闸门（问答块结构与现有知识库重复的会被剔除）
    ingest_path, conflicts = _apply_dedup_gate(save_path, safe_name)

    # 提交异步入库任务
    from core.async_tasks import async_processor
    task_id = async_processor.submit(
        f"ingest_{safe_name}",
        _do_ingest,
        ingest_path, safe_name, ingest_path if ingest_path != save_path else ""
    )

    return {
        "code": 200,
        "msg": (f"文档 {file.filename} 已提交入库，正在后台处理中"
                + (f"（已跳过 {len(conflicts)} 个与现有知识库重复的问题）"
                   if conflicts else "")),
        "data": {
            "task_id": task_id,
            "file_name": safe_name,
            "original_name": file.filename,
            "file_size": len(content),
            "dedup_skipped": len(conflicts),
        }
    }


@router.post("/upload/sync", summary="同步上传文档并入库")
async def upload_document_sync(file: UploadFile = File(...)):
    """
    同步上传文档（等待入库完成再返回）
    适用于小文件，大文件建议使用 /knowledge/upload 异步接口
    """
    _validate_file(file.filename)

    base, ext = os.path.splitext(file.filename)
    timestamp = int(time.time())
    safe_name = f"{base}_{timestamp}{ext}"
    save_path = os.path.join(DOCS_DIR, safe_name)

    content = await file.read()
    _validate_file(file.filename, len(content))

    with open(save_path, "wb") as f:
        f.write(content)

    # 入库前去重闸门
    ingest_path, conflicts = _apply_dedup_gate(save_path, safe_name)

    # 直接同步入库
    try:
        from agents.document_agent import document_agent
        from core.cache import redis_cache
        start = time.time()
        chunk_count = document_agent.ingest_document(ingest_path, safe_name)
        elapsed = round(time.time() - start, 2)
    
        # 知识库变更后清空缓存 + 重载问法库，确保下次问答使用最新数据
        redis_cache.flush_cache()
        from agents.query_agent import query_agent
        query_agent.reload_patterns()
        try:
            from services.kb_dedup import invalidate_cache
            invalidate_cache()
        except Exception:
            pass

        return {
            "code": 200,
            "msg": (f"文档 {file.filename} 入库完成"
                    + (f"（已跳过 {len(conflicts)} 个与现有知识库重复的问题）"
                       if conflicts else "")),
            "data": {
                "chunk_count": chunk_count,
                "file_name": safe_name,
                "original_name": file.filename,
                "file_size": len(content),
                "elapsed_seconds": elapsed,
                "dedup_skipped": len(conflicts),
            }
        }
    except Exception as e:
        logger.error(f"同步入库失败: {file.filename}, 错误: {e}", exc_info=True)
        # 清理已保存的文件
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(
            status_code=500,
            detail=f"文档处理失败: {str(e)}"
        )
    finally:
        if ingest_path != save_path and os.path.exists(ingest_path):
            try:
                os.remove(ingest_path)
            except Exception:
                pass


@router.get("/list", summary="列出所有已上传文档")
async def list_documents():
    """列出文档目录中的文件及元信息，同时附上对应切片文件的 chunk 数量"""
    from agents.document_agent import document_agent
    docs = document_agent.list_documents()
    
    # 读取所有切片文件映射: doc_name -> chunk_count
    chunk_files = document_agent.list_all_chunk_files()
    chunk_map = {}
    for cf in chunk_files:
        chunk_map[cf["doc_name"]] = cf["total_chunks"]
    
    print(f"📋 [API] /knowledge/list 返回 {len(docs)} 篇文档，chunks映射: {chunk_map}")
    
    result = []
    for doc in sorted(docs, reverse=True):
        path = os.path.join(DOCS_DIR, doc)
        stat = os.stat(path) if os.path.exists(path) else None
        result.append({
            "name": doc,
            "size": stat.st_size if stat else 0,
            "modified": stat.st_mtime if stat else 0,
            "chunk_count": chunk_map.get(doc, 0),
        })
    return {"code": 200, "data": result}


@router.delete("/{doc_name}", summary="删除指定文档")
async def delete_document(doc_name: str):
    """删除文档（从存储和Milvus+BM25索引中移除）"""
    from agents.document_agent import document_agent

    # 从向量库和BM25索引中移除
    try:
        document_agent.delete_document(doc_name)
    except Exception as e:
        logger.warning(f"从索引中删除文档失败: {doc_name}, 错误: {e}")

    # 删除磁盘文件
    file_path = os.path.join(DOCS_DIR, doc_name)
    if os.path.exists(file_path):
        os.remove(file_path)

    # 知识库变更 → 问法清单索引必须重建，否则去重闸门与问法提分还拿旧清单
    try:
        from services.kb_dedup import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    return {"code": 200, "msg": f"文档 {doc_name} 已删除"}


@router.get("/task/{task_id}", summary="查询入库任务状态")
async def get_task_status(task_id: str):
    """查询异步入库任务的进度"""
    from core.async_tasks import async_processor
    task = async_processor.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 200, "data": task}


@router.get("/chunks", summary="列出所有切片文件")
async def list_chunk_files():
    """列出所有已保存的切片文件及统计"""
    from agents.document_agent import document_agent
    files = document_agent.list_all_chunk_files()
    return {"code": 200, "data": files}


@router.get("/chunks/{doc_name}", summary="查看文档切片详情")
async def get_chunks(doc_name: str):
    """
    查看指定文档的所有文本块
    doc_name: 文档文件名（如 report_1234567890.pdf 或 report_1234567890_chunks.json）
    """
    from agents.document_agent import document_agent
    # 统一处理：去掉 _chunks.json 后缀还原为原始文档名
    clean_name = doc_name.replace("_chunks.json", "")
    chunks = document_agent.get_chunks_from_file(clean_name)
    if not chunks:
        raise HTTPException(status_code=404, detail=f"未找到文档 '{doc_name}' 的切片文件")
    return {
        "code": 200,
        "data": {
            "doc_name": clean_name,
            "total_chunks": len(chunks),
            "chunks": chunks
        }
    }


@router.get("/stats", summary="知识库统计信息")
async def knowledge_stats():
    """获取知识库统计"""
    from agents.retrieval_agent import retrieval_agent
    stats = retrieval_agent.get_index_stats()
    return {"code": 200, "data": stats}
