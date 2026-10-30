# ============================================================
# 问答 API 路由 - /chat /chat/stream
# ============================================================
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import json
import asyncio
import os
import time

from core.auth import get_current_user, get_optional_user
from core.cache import redis_cache

router = APIRouter()


class AskRequest(BaseModel):
    question: str
    history: str = ""
    session_id: str = ""
    source: str = "web"  # web / miniprogram（运营区分端统计）


class ChatResponse(BaseModel):
    code: int = 200
    data: dict


@router.post("/chat", summary="知识库问答（非流式）")
async def chat(req: AskRequest,
               current_user: dict = Depends(get_optional_user)):
    """RAG 智能问答接口（可携带 Bearer Token，用于业务 Agent 查询个人账户）"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    # 问候语快速响应
    from agents.query_agent import query_agent
    if query_agent.is_greeting(req.question):
        return {"code": 200, "data": {
            "query": req.question,
            "answer": query_agent.get_greeting_response(),
            "references": [],
            "trace": {"elapsed": 0.0}
        }}

    # 充值档位咨询规则应答（保证话术确定性：只有三档 + 收付款引导）
    if query_agent.is_price_inquiry(req.question):
        return {"code": 200, "data": {
            "query": req.question,
            "answer": query_agent.get_price_response(),
            "references": [],
            "trace": {"elapsed": 0.0, "rule": "price_inquiry"}
        }}

    # 通过调度Agent执行完整流程
    start = time.time()
    from agents.orchestrator import orchestrator
    result = orchestrator.process(req.question, req.history, user=current_user,
                                  session_id=req.session_id)

    # 统计 + Redis 日志
    elapsed = result.get("trace", {}).get("elapsed", time.time() - start)
    redis_cache.incr_counter("total_questions")
    redis_cache.incr_counter(f"intent:{query_agent.detect_intent(req.question)}")
    # 回答类型（知识库盲区统计的唯一判据）：靠"回答类型"而不是"references 是否为空"
    from agents.verification_agent import verification_agent
    answer_type = verification_agent.classify_answer(
        result.get("answer", ""), cached=bool(result.get("trace", {}).get("cached")))
    redis_cache.save_question_log(
        question=req.question,
        answer=result.get("answer", ""),
        references=result.get("references", []),
        elapsed=elapsed,
        search_query=result.get("search_query", ""),
        session_id=req.session_id,
        source=req.source,
        answer_type=answer_type,
    )

    return {"code": 200, "data": result}


@router.post("/chat/stream", summary="知识库问答（SSE流式）")
async def chat_stream(req: AskRequest,
                      current_user: dict = Depends(get_optional_user)):
    """RAG 流式问答接口，SSE 格式（可携带 Bearer Token，用于业务 Agent 查询个人账户）"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    # 问候语
    from agents.query_agent import query_agent
    if query_agent.is_greeting(req.question):
        async def greet_gen():
            resp = query_agent.get_greeting_response()
            yield f"data: {json.dumps({'type': 'meta', 'reference_doc': []}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'chunk', 'content': resp}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(greet_gen(), media_type="text/event-stream")

    # 充值档位咨询规则应答（与 /chat 保持一致）
    if query_agent.is_price_inquiry(req.question):
        async def price_gen():
            resp = query_agent.get_price_response()
            yield f"data: {json.dumps({'type': 'meta', 'reference_doc': []}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'chunk', 'content': resp}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(price_gen(), media_type="text/event-stream")

    # 流式处理
    from agents.orchestrator import orchestrator

    loop = asyncio.get_event_loop()
    gen = orchestrator.process_stream(req.question, req.history, user=current_user,
                                      session_id=req.session_id)

    _DONE = object()

    def _safe_next(g):
        """推进生成器；任何异常都包装为 error 事件返回，避免静默断流导致前端回答截断"""
        try:
            return next(g)
        except StopIteration:
            return _DONE
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"流式处理异常: {e}", exc_info=True)
            return {"type": "error", "content": f"生成回答时出错，请稍后重试：{str(e)[:120]}"}

    async def event_generator():
        start = time.time()
        chunk_count = 0
        full_answer_parts = []
        meta_info = {}
        # 回答耗时口径：取 orchestrator 在 done 事件里给出的真实回答耗时
        # （done 之后还有"追问建议"等不阻塞用户的收尾工作，不能计入回答耗时，
        #  否则提速看板会被这 0~6s 污染，也会误判"生成变慢"）
        answer_elapsed = None
        try:
            while True:
                event = await loop.run_in_executor(None, _safe_next, gen)
                if event is _DONE:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                chunk_count += 1
                if event.get("type") == "meta":
                    meta_info = event
                if event.get("type") == "chunk":
                    full_answer_parts.append(event.get("content", ""))
                if event.get("type") == "done" and isinstance(event.get("elapsed"), (int, float)):
                    answer_elapsed = float(event["elapsed"])
                await asyncio.sleep(0)
        except Exception as e:
            # 双保险：管道本身异常也推送 error 事件，保证前端始终能收到收尾信号
            import logging
            logging.getLogger(__name__).error(f"SSE管道异常: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': f'连接异常，请重试：{str(e)[:120]}'}, ensure_ascii=False)}\n\n"

        # 统计 + Redis 日志
        wall_elapsed = round(time.time() - start, 3)
        elapsed = round(answer_elapsed, 3) if answer_elapsed is not None else wall_elapsed
        full_answer = "".join(full_answer_parts)
        redis_cache.incr_counter("total_questions")
        # 回答类型（知识库盲区统计的唯一判据）：
        #   cached 由缓存命中分支的 meta.cached 标记确定；其余按答案开头的兜底话术判定。
        from agents.verification_agent import verification_agent
        answer_type = verification_agent.classify_answer(
            full_answer, cached=bool(meta_info.get("cached")))
        redis_cache.save_question_log(
            question=req.question,
            answer=full_answer,
            references=meta_info.get("reference_doc", []),
            elapsed=elapsed,
            search_query=meta_info.get("search_query", ""),
            session_id=req.session_id,
            source=req.source,
            answer_type=answer_type,
        )
        yield f"data: {json.dumps({'type': 'trace', 'total_chunks': chunk_count, 'elapsed': elapsed, 'wall_elapsed': wall_elapsed}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/chat/history", summary="获取会话历史（结构化多轮 + 用户画像）", dependencies=[Depends(get_current_user)])
async def get_history(session_id: str = "default",
                      current_user: dict = Depends(get_current_user)):
    """获取会话记忆（需登录）：结构化消息列表 + 画像 + 纯文本历史"""
    from core.chat_memory import load_context
    # 传 user_id：登录用户优先拿到 user 级画像，与问答链路保持同一份口径
    ctx = load_context(session_id, (current_user or {}).get("user_id"))
    return {
        "code": 200,
        "data": {
            "session_id": session_id,
            "history": ctx.get("history_text", ""),
            "messages": ctx.get("messages", []),
            "profile": ctx.get("profile", {}),
            "profile_note": ctx.get("profile_note", ""),
        }
    }


@router.delete("/chat/history", summary="清除会话历史（含结构化消息）", dependencies=[Depends(get_current_user)])
async def clear_history(session_id: str = "default"):
    """清除会话历史（Redis 缓存 + 结构化消息表，需登录）"""
    from core.cache import redis_cache
    redis_cache.save_history(session_id, "")
    try:
        from database.db import delete_session
        delete_session(session_id)
    except Exception:
        pass
    return {"code": 200, "msg": "会话历史已清除"}


# ============================================================
# 快捷提问：近 7 天热点问题 Top6（小程序 AI 问答页 6 个按钮）
# ============================================================
@router.get("/chat/hot-questions", summary="近7天热点问题 Top6（小程序快捷提问按钮）")
async def hot_questions(limit: int = 6, days: int = 7):
    """返回当前批次热点问题：{question 完整问句, label ≤4字标签, icon 图标, hit_count}。

    每 7 天自动刷新（商家 PC「分析图谱」页也可手动刷新）；数据不足时用兜底问题补齐，
    因此小程序端拿到的永远是 6 条可直接点击发送的问句。登录非必需。
    """
    from services.hot_questions import get_hot_questions, REFRESH_DAYS
    limit = min(max(1, limit), 12)
    days = min(max(1, days), 60) or REFRESH_DAYS
    items = get_hot_questions(limit=limit, days=days)
    return {
        "code": 200,
        "data": [
            {
                "question": it.get("question", ""),
                "label": it.get("label") or (it.get("question") or "")[:4],
                "icon": it.get("icon") or "💬",
                "hit_count": int(it.get("hit_count") or 0),
            }
            for it in items
        ],
    }


# ============================================================
# AI 回答操作条：收藏（复制/转发/删除在小程序端本地完成）
# ============================================================
class FavoriteRequest(BaseModel):
    question: str = ""
    answer: str = ""
    session_id: str = ""
    # 回答关联的图片（用户上传的原图 / 地图静态图 / 图表），存服务器路径
    images: list[str] = Field(default_factory=list)


# 收藏图片落盘目录（与失物招领照片同一套 /static 静态路由）
_FAVORITE_IMG_DIR = os.path.join("static", "favorites")
_ALLOWED_IMG_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
_MAX_FAV_IMG_SIZE = 8 * 1024 * 1024


@router.post("/chat/favorites/upload", summary="上传收藏附图（小程序本地图片 → 服务器路径）")
async def upload_favorite_image(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """把收藏要保存的本地图片存到服务器，返回可直接访问的 /static 路径。

    小程序侧的图片是临时路径（重启后失效），必须落盘后收藏里才能长期显示。
    """
    os.makedirs(_FAVORITE_IMG_DIR, exist_ok=True)
    if file.content_type and file.content_type not in _ALLOWED_IMG_TYPES:
        raise HTTPException(status_code=400, detail="仅支持图片格式（jpg/png/webp/gif/bmp）")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(content) > _MAX_FAV_IMG_SIZE:
        raise HTTPException(status_code=413, detail="图片过大，最大支持 8MB")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        ext = ".jpg"
    filename = f"fav_{current_user['user_id']}_{int(time.time() * 1000)}{ext}"
    with open(os.path.join(_FAVORITE_IMG_DIR, filename), "wb") as f:
        f.write(content)
    return {"code": 200, "msg": "图片已上传", "data": {"url": f"/static/favorites/{filename}"}}


def _is_persistent_image_url(u: str) -> bool:
    """是否是可长期访问的图片地址（过滤小程序本地临时路径）。

    小程序 chooseImage 的临时路径形如 http://tmp/xxx、wxfile://xxx、http://usr/xxx，
    它们在客户端重启后即失效，存进收藏只会显示成裂图，必须由前端先上传换成正
    式的 /static/... 地址。
    """
    if not isinstance(u, str):
        return False
    u = u.strip()
    if not u:
        return False
    if any(bad in u for bad in ("://tmp/", "://usr/", "wxfile://", "blob:")):
        return False
    return u.startswith("/static/") or u.startswith("http://") or u.startswith("https://")


@router.post("/chat/favorites", summary="收藏一条问答（我的页「我的收藏」）")
async def add_favorite(req: FavoriteRequest, current_user: dict = Depends(get_current_user)):
    if not (req.question.strip() or req.answer.strip() or req.images):
        raise HTTPException(status_code=400, detail="收藏内容不能为空")
    from database.db import add_favorite
    # 只保留服务器可长期访问的图片（临时路径一律丢弃）
    images = [u.strip() for u in (req.images or []) if _is_persistent_image_url(u)]
    fav_id = add_favorite(
        user_id=current_user["user_id"],
        question=req.question.strip()[:2000],
        answer=req.answer.strip()[:20000],
        session_id=req.session_id or "",
        images=images[:6],
    )
    return {"code": 200, "msg": "已收藏，可在「我的-我的收藏」查看", "data": {"favorite_id": fav_id}}


@router.get("/chat/favorites", summary="我的收藏列表")
async def list_favorites(limit: int = 50, page: int = 1,
                         current_user: dict = Depends(get_current_user)):
    from database.db import list_favorites as _list, count_favorites
    limit = min(max(1, limit), 100)
    offset = max(0, (max(1, page) - 1) * limit)
    rows = _list(current_user["user_id"], limit=limit, offset=offset) or []
    return {
        "code": 200,
        "data": [
            {
                "id": r.get("id"),
                "question": r.get("question") or "",
                "answer": r.get("answer") or "",
                "images": r.get("images") or [],
                "created_at": str(r.get("created_at") or ""),
            }
            for r in rows
        ],
        "total": count_favorites(current_user["user_id"]),
    }


@router.delete("/chat/favorites/{favorite_id}", summary="取消收藏")
async def remove_favorite(favorite_id: int, current_user: dict = Depends(get_current_user)):
    from database.db import delete_favorite
    ok = delete_favorite(favorite_id, current_user["user_id"])
    if not ok:
        raise HTTPException(status_code=404, detail="收藏不存在或已删除")
    return {"code": 200, "msg": "已取消收藏"}


# ============================================================
# 图片提问（原生多模态）：用户上传图片 → 直接作为图像输入交给视觉模型
# （不再走 OCR）；模型返回文字回答。
# ============================================================
_VISION_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_VISION_SYSTEM = (
    "你是大黄鸭室外游乐园的智能客服鸭鸭。用户上传了一张图片并希望获得解读。\n"
    "请先简要说明图中内容；如果图片与园区相关（价目表/票务/游乐项目/告示牌/"
    "失物照片/单据等），请进一步给出用户可能想了解的信息或建议；"
    "如果图片内容无法看清或与园区无关，如实说明，不要编造图中没有的价格/规则。"
    "回答口语化、简洁，控制在 5 句左右。"
)


def _image_to_data_url(content: bytes) -> str:
    """校验并压缩图片 → data URL（统一转 JPEG，缩到长边≤1536，便于多模态入参）

    实现已收敛到 services/vision_service.image_to_data_url，
    与失物照片识别共用同一套预处理，避免两处实现漂移。
    """
    from services.vision_service import image_to_data_url
    return image_to_data_url(content)


@router.post("/chat/vision", summary="图片+文字提问（多模态：图片直接入参视觉模型，可携带文字问题）")
async def chat_vision(
    file: UploadFile = File(...),
    question: str = Form(""),
    session_id: str = Form(""),
    source: str = Form("web"),
    current_user: dict = Depends(get_optional_user),
):
    """上传一张图片（可同时输入文字问题），AI 视觉模型看图+读文字+参考知识库回答。
    纯文字问题仍走 /chat 或 /chat/stream（FAQ→Redis缓存；其余→RAG+文本模型）；
    带图提问必须走视觉模型（deepseek-v4-flash-vision-exp）。
    模型路由见 core/llm_client.py。
    """
    import os
    import logging

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _VISION_EXTS:
        raise HTTPException(status_code=400, detail=f"仅支持图片格式: {', '.join(sorted(_VISION_EXTS))}")

    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片过大，最大支持 15MB")

    try:
        data_url = _image_to_data_url(content)
    except Exception as e:
        logging.getLogger(__name__).warning(f"图片解析失败: {e}")
        return {"code": 200, "data": {
            "answer": "抱歉，这张图片我暂时无法解析（可能已损坏或格式不支持），请换一张清晰的图片试试。",
            "references": [], "note": "vision_parse_error", "trace": {"agent": "vision"}}}

    # 用户问题：可单独发图（默认看图解读），也可图片+文字一起发（以文字为主）
    user_question = (question or "").strip()

    # 有文字时检索园区知识库节选作为背景规则（图片识别 + 知识库 + 模型推理三合一，不编造）
    references: list = []
    context_snippets: list = []
    if user_question:
        try:
            from agents.retrieval_agent import retrieval_agent
            ctx_docs = retrieval_agent.search(user_question, top_k=3, use_cache=True) or []
            for t, s, _sc in ctx_docs:
                context_snippets.append(f"- {t[:220]}")
                if s and s not in references:
                    references.append(s)
        except Exception as e:
            logging.getLogger(__name__).warning(f"图片问答知识库检索失败(忽略继续): {e}")

    system = (
        "你是大黄鸭室外游乐园的智能客服鸭鸭。用户上传了一张图片，并可能同时输入了文字问题。\n"
        "请按以下要求回答：\n"
        "1. 先看清图片内容，再结合用户文字问题作答（两者都重要）；\n"
        "2. 若图片或问题与园区相关（价目表/票务/办卡/告示牌/失物/游乐项目等），"
        "可参考下方提供的园区知识库节选，结合图片信息与你的常识进行推理，给出贴心、准确的建议；\n"
        "3. 图片看不清、与园区无关或超出资料范围时，如实说明并引导用户补充，绝不编造图片/资料中没有的价格或规则；\n"
        "4. 语气亲切耐心、高情商（体察用户此刻的疑问或情绪），回答口语化、简洁，一般 3~6 句。"
    )
    if context_snippets:
        system += "\n\n【园区知识库节选（可参考，若有冲突以现场为准）】\n" + "\n".join(context_snippets)

    # 用户画像：图片问答同样要"基于画像回答"（此前这条路径完全没接画像）。
    # 走 user 级（跟账号走），没有会话 id 也照样生效；读不到就按无画像继续。
    _uid = (current_user or {}).get("user_id")
    if _uid:
        try:
            from core.chat_memory import load_context as _load_ctx
            from core.llm_client import set_llm_user as _set_uid
            _set_uid(_uid)      # 缓存隔离键（只放自增 id，不含隐私）
            _pn = (_load_ctx(session_id or "default", _uid, query=user_question)
                   .get("profile_note") or "").strip()
        except Exception as e:
            logging.getLogger(__name__).warning(f"图片问答画像读取失败（忽略继续）: {e}")
            _pn = ""
        if _pn:
            system += ("\n\n【用户档案】（来自之前对话，回答可自然结合，不要生硬罗列）\n" + _pn)

    prompt = (
        "【用户文字问题】" + (user_question or "请帮我看看这张图片，并告诉我上面有什么、是否需要关注。")
        + "\n【图片】请结合图片内容作答。"
    )
    from core.llm_client import llm_client
    try:
        answer = llm_client.invoke_vision(prompt, data_url, system=system)
    except Exception as e:
        logging.getLogger(__name__).error(f"视觉模型调用失败: {e}")
        return {"code": 200, "data": {
            "answer": "抱歉，视觉模型暂时不可用，请稍后再试，或直接打字问我。",
            "references": [], "note": f"vision_error: {str(e)[:100]}",
            "trace": {"agent": "vision"}}}

    if not answer:
        return {"code": 200, "data": {
            "answer": "这张图片我没能理解出有效内容，请拍得更清楚一些，或直接描述你想问什么。",
            "references": [], "trace": {"agent": "vision"}}}

    return {"code": 200, "data": {
        "answer": answer,
        "references": references,
        "note": "vision_native_multimodal" if not user_question else "vision_text_image",
        "trace": {"agent": "vision"},
    }}


# ============================================================
# 满意度评价（小程序 1-5 星，feedback 表持久化，商家端统计用）
# ============================================================
class FeedbackRequest(BaseModel):
    rating: int                 # 1-5 星
    session_id: str = ""
    comment: str = ""


@router.post("/feedback", summary="用户满意度评价提交（1-5星，落库供商家端统计）")
async def submit_feedback(req: FeedbackRequest, current_user: dict = Depends(get_current_user)):
    if req.rating not in (1, 2, 3, 4, 5):
        raise HTTPException(status_code=400, detail="rating 需为 1-5 星")
    from database.db import add_feedback
    fid = add_feedback(
        rating=str(req.rating),
        session_id=req.session_id or "",
        user_id=current_user["user_id"],
        comment=(req.comment or "")[:200],
    )
    import logging
    logging.getLogger(__name__).info(
        f"[满意度] 用户 {current_user['user_id']} 评价 {req.rating} 星")
    return {"code": 200, "msg": "感谢您的反馈", "data": {"feedback_id": fid}}
