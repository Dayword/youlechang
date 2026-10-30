# ============================================================
# 商家端「经营小助手」执行器（AdminAssistantExecutor）
# ------------------------------------------------------------
# 形态：管理员在商家 PC 右下角弹窗说一句话 → 模型直接调工具把事办完
#       → 结束后给一份确定性的动作流水（不是聊天回答）。
#
# 与 agents/business_agent.py 的关键差异（也是本模块存在的理由）：
#   1. **不产出回答**：循环里只取 msg.tool_calls，msg.content 一律丢弃；
#      一轮工具都没调（模型想聊天）→ 回固定话术，绝不把模型的话转给管理员。
#      这条写在代码里，不靠 prompt 求模型。
#   2. **汇总不让模型写**：模型只见过被截断的工具返回，容易编数字；
#      汇总由工具名 + 参数 + 结果首行确定性拼装（ADMIN_ASSISTANT_POLISH=1 才润色）。
#      查询类工具更彻底：完整报告 / 数字卡 / ECharts 图表由**工具自己**产出
#      （tools/admin_tools.py 的 _report），执行器原样透传上屏，模型碰不到。
#   3. **高危操作中断**：工具返回 `⛔ {...}`（见 tools/admin_tools.py）时立即停轮，
#      把确认卡交给前端；管理员点「执行」后走 run_approved() 单步续跑。
#
# 对用户侧 RAG 主链路零影响：本模块不 import orchestrator，工具 schema 也不进 /chat。
# ============================================================
import json
import logging
import time
from typing import Generator, Optional

from config import (ADMIN_ASSISTANT_MODEL, ADMIN_ASSISTANT_ROUNDS,
                    ADMIN_ASSISTANT_TOOL_RESULT_CHARS, ADMIN_ASSISTANT_POLISH)
from tools.admin_tools import get_admin_tools, tools_to_openai, pop_confirm_token

logger = logging.getLogger(__name__)

PARK_NAME = "大黄鸭室外游乐园"

# 固定话术：一轮工具都没调用（模型想聊天）时回它。刻意不透露模型说了什么。
NO_ACTION_HINT = ("我没有识别到可执行的操作。请说得具体些，例如："
                  "「把 9.9 元那张小额充币下架」「核销昨天未核销的消费单」"
                  "「看看今天的经营数据」。")

_EXECUTOR_RULES = """【第一条铁律】你不是客服，是执行器：
- 禁止寒暄、禁止解释、禁止给建议、禁止复述管理员的话、禁止说"我将会/建议您"。
- 你写出来的文字**不会被任何人看到**，只有工具调用会真正生效。

【第二条铁律】直接干，别预判：
- 管理员的每个诉求都必须落到工具调用上；涉及数据一律先调工具取真实值，禁止凭印象下结论。
- **不要替管理员判断"这件事能不能做"**：能不能做由工具结果决定，你只管调。
- 需要处理多个对象时（多张卡 / 多个单号），**在同一轮里一次性给出全部工具调用**（支持并行），
  不要一轮只调一个。
- 对象不明确（例如"下架最贵那张卡"）时，先调查询类工具（list_cards_admin 等）拿到候选清单，
  再基于真实清单决定，禁止瞎猜 id。

【第三条铁律】高危操作会自动弹确认：
- 退款 / 调币 / 删除 / 改 Agent 配置类工具会返回 `⛔ 需要管理员确认`。这是**正常流程**，
  不要重试、不要换别的方式绕、不要自己动手。
- 一旦收到 `⛔`，立刻停止（不要再调任何工具），本次任务到此为止。
"""


class AdminAssistantExecutor:
    """纯执行器：一句话 → 一串真实操作 → 一份动作流水。"""

    def __init__(self, model: str = None, max_rounds: int = None,
                 tool_result_chars: int = None):
        self.model = model or ADMIN_ASSISTANT_MODEL
        self.max_rounds = max(1, int(max_rounds or ADMIN_ASSISTANT_ROUNDS))
        self.tool_result_chars = max(80, int(tool_result_chars or ADMIN_ASSISTANT_TOOL_RESULT_CHARS))

    # ---------------- 组装 ----------------
    def _setup(self, ctx: dict):
        tools = get_admin_tools(ctx)
        name2tool = {t["name"]: t for t in tools}
        system = (f'你是{PARK_NAME}运营后台的执行器。管理员会给你一句话，'
                  f'你要把它变成真实操作。\n\n{_EXECUTOR_RULES}\n'
                  f'【可用工具】共 {len(tools)} 个，覆盖：经营看板 / 问答记录 / 卡产品 / '
                  f'办卡账单与持卡 / 游乐消费单 / 失物招领 / 公告热点反馈 / 知识库 / 用户与系统配置。')
        return tools, name2tool, tools_to_openai(tools), system

    # ---------------- 流水 ----------------
    @staticmethod
    def _split(result) -> tuple:
        """工具返回归一化 → (文字, 数字卡[], 图表[])。

        查询类工具返回 _report(...) 字典，写工具仍返回纯字符串；
        统一成三件套后，调用方不必判断类型。
        """
        if isinstance(result, dict):
            return (str(result.get("text") or ""),
                    result.get("stats") or [], result.get("charts") or [])
        return str(result), [], []

    @staticmethod
    def _brief(result: str) -> str:
        """工具返回的首个有意义行，作为流水里的一行摘要"""
        for line in str(result).split("\n"):
            line = line.strip()
            if line:
                return line[:120]
        return "(无返回)"

    @staticmethod
    def _summarize(actions: list) -> str:
        """确定性汇总（零幻觉）：工具名 + 关键参数 + 结果首行"""
        done = [a for a in actions if a["status"] == "done"]
        failed = [a for a in actions if a["status"] == "failed"]
        pending = [a for a in actions if a["status"] == "need_confirm"]
        head = f"共执行 {len(done) + len(failed)} 步：成功 {len(done)}、失败 {len(failed)}"
        head += f"，另有 {len(pending)} 步等待你确认。" if pending else "。"
        lines = [head]
        for a in actions:
            mark = {"done": "✓", "failed": "✗", "need_confirm": "⚠"}.get(a["status"], "·")
            lines.append(f"{mark} [{a['seq']}] {a['brief']}")
        notes = []
        if pending:
            notes.append("带 ⚠ 的步骤涉及退款/调币/删除等不可逆操作，需要你点「执行」才会真正生效。")
        if failed:
            notes.append("失败项多为对象不存在或状态已变化，可先查询复核再重试。")
        names = {a["tool"] for a in actions}
        if names & {"verify_consumption"}:
            notes.append("核销不可撤销；若为误操作，需到「游乐消费」页对该单退款。")
        if names & {"refund_order", "refund_consumption", "adjust_user_card_coins"}:
            notes.append("涉及资金的改动已写入操作日志，用户端余币会立即变化。")
        if notes:
            lines.append("需要注意：" + "".join(notes))
        return "\n".join(lines)

    def _fmt_args(self, args: dict) -> str:
        if not args:
            return ""
        try:
            return json.dumps(args, ensure_ascii=False)[:120]
        except Exception:
            return str(args)[:120]

    # ---------------- 主流程 ----------------
    def run(self, message: str, ctx: Optional[dict] = None,
            user: Optional[dict] = None) -> Generator[dict, None, None]:
        """执行一句话指令，流式产出事件（供 SSE 端点直接转发）。

        事件：step / confirm / summary / notice / error / done
        """
        ctx = ctx if ctx is not None else {}
        uid = (user or {}).get("user_id") or (user or {}).get("username") or "-"
        message = (message or "").strip()
        start = time.time()

        if not message:
            yield {"type": "notice", "content": "请输入要做的事，例如「把 9.9 元那张小额充币下架」。"}
            yield {"type": "done", "elapsed": 0}
            return

        from core.llm_client import llm_client
        client = llm_client.get_llm()

        try:
            tools, name2tool, openai_tools, system = self._setup(ctx)
        except Exception as e:                       # pragma: no cover
            logger.error(f"[admin_assistant] 工具装载失败: {e}", exc_info=True)
            yield {"type": "error", "content": f"执行器初始化失败：{str(e)[:120]}"}
            return

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]
        actions: list = []
        seq = 0
        rounds = 0
        hit_confirm = False

        for rounds in range(1, self.max_rounds + 1):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto",
                    temperature=0.1,
                    # glm-5.3-flash 对项目全局注入的 thinking:{type:disabled} 近乎无感，
                    # 显式再传一次（setdefault 合并后两个参数并存，glm 只认后者）。
                    extra_body={"enable_thinking": False},
                )
            except Exception as e:
                logger.error(f"[admin_assistant] 模型调用失败: {e}")
                yield {"type": "error", "content": f"模型调用失败：{str(e)[:140]}"}
                break

            msg = resp.choices[0].message
            tool_calls = list(msg.tool_calls or [])
            if not tool_calls:
                # 🔒 「不用回答」的代码层落点：模型的 content 到此为止，不采纳、不上屏。
                logger.info(f"[admin_assistant] uid={uid} 第 {rounds} 轮无工具调用（content 丢弃）"
                            f" len={len(msg.content or '')}")
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in tool_calls],
            })

            for tc in tool_calls:
                seq += 1
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                tool = name2tool.get(name)
                risk = (tool or {}).get("risk", "read")
                view = (tool or {}).get("view", "log")
                title = (tool or {}).get("description", "")[:60]

                yield {"type": "step", "seq": seq, "tool": name, "args": args,
                       "risk": risk, "status": "running",
                       "title": f"正在执行 {name}"}

                if tool is None:
                    brief = f"未知工具 {name}（已跳过）"
                    actions.append({"seq": seq, "tool": name, "args": args,
                                    "risk": risk, "status": "failed", "brief": brief})
                    yield {"type": "step", "seq": seq, "tool": name, "args": args,
                           "risk": risk, "status": "failed", "brief": brief}
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": brief})
                    continue

                t0 = time.time()
                try:
                    text, stats, charts = self._split(tool["run"](args))
                    ok_flag = not text.startswith("❌")
                except Exception as e:
                    text, stats, charts = f"❌ 工具 {name} 执行异常：{str(e)[:120]}", [], []
                    ok_flag = False
                    logger.warning(f"[admin_assistant] uid={uid} tool={name} args={args} 异常: {e}")

                elapsed = round(time.time() - t0, 2)
                # ---------- 高危确认卡：中断整轮，交给前端 ----------
                if text.startswith("⛔ "):
                    try:
                        card = json.loads(text[2:])
                    except Exception:
                        card = {"tool": name, "args": args, "preview": text[2:120],
                                "token": ""}
                    brief = f"待确认：{card.get('preview') or name}"
                    actions.append({"seq": seq, "tool": name, "args": args, "risk": risk,
                                    "status": "need_confirm", "brief": brief,
                                    "token": card.get("token"), "preview": card.get("preview")})
                    logger.info(f"[admin_assistant] uid={uid} tool={name} args={args} "
                                f"-> 待确认（未执行）")
                    yield {"type": "confirm", "seq": seq, "tool": name, "args": args,
                           "risk": risk, "preview": card.get("preview") or "",
                           "token": card.get("token") or ""}
                    hit_confirm = True
                    break

                # 🔒 审计日志（不建表）：谁、调了什么、带什么参数、结果如何
                logger.info(f"[admin_assistant] uid={uid} tool={name} args={args} "
                            f"ok={ok_flag} {elapsed}s")

                # 查询类（view=report）：完整报告 + 数字卡 + 图表一并上屏，不再只给一行摘要。
                # 写操作保持"动作流水"形态（只有 brief），避免把执行日志撑成报表。
                rich = {"report": text, "stats": stats, "charts": charts} \
                    if (ok_flag and view == "report") else {}
                brief = self._brief(text)
                actions.append({"seq": seq, "tool": name, "args": args, "risk": risk,
                                "status": "done" if ok_flag else "failed",
                                "brief": brief, "elapsed": elapsed, **rich})
                yield {"type": "step", "seq": seq, "tool": name, "args": args,
                       "risk": risk, "status": "done" if ok_flag else "failed",
                       "brief": brief, "elapsed": elapsed, **rich}

                # 回填给模型：截断，避免 8 轮循环把上下文撑到几万 token
                clipped = text if len(text) <= self.tool_result_chars \
                    else text[:self.tool_result_chars] + "…（已截断）"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": clipped})

            if hit_confirm:
                break
        else:
            # for 正常跑完（未 break）→ 达到轮数上限
            actions.append({"seq": seq + 1, "tool": "-", "args": {}, "risk": "read",
                            "status": "failed",
                            "brief": f"达到 {self.max_rounds} 轮上限，任务可能未做完，建议拆成更小的指令"})

        # ---------------- 收尾 ----------------
        if not actions:
            yield {"type": "notice", "content": NO_ACTION_HINT}
            yield {"type": "done", "elapsed": round(time.time() - start, 2), "rounds": rounds}
            return

        summary = self._summarize(actions)
        if ADMIN_ASSISTANT_POLISH:
            polished = self._polish(message, summary)
            if polished:
                summary = polished
        yield {"type": "summary", "text": summary, "actions": actions,
               "rounds": rounds, "elapsed": round(time.time() - start, 2)}
        yield {"type": "done", "elapsed": round(time.time() - start, 2), "rounds": rounds}

    # ---------------- 确认后续跑（单步执行，不重新问模型） ----------------
    def run_approved(self, token: str, ctx: Optional[dict] = None,
                     user: Optional[dict] = None) -> Generator[dict, None, None]:
        """管理员点了确认卡上的「执行」→ 只执行这一步，不再走 LLM（避免模型改参数）。"""
        ctx = ctx if ctx is not None else {}
        uid = (user or {}).get("user_id") or (user or {}).get("username") or "-"
        start = time.time()

        item = pop_confirm_token(token)
        if not item:
            yield {"type": "error", "content": "确认已失效（超过 5 分钟或已被处理），请重新发起该指令。"}
            return

        name, args = item["tool"], item["args"]
        _, name2tool, _, _ = self._setup(ctx)
        tool = name2tool.get(name)
        if tool is None:
            yield {"type": "error", "content": f"工具 {name} 不存在，无法执行。"}
            return

        yield {"type": "step", "seq": 1, "tool": name, "args": args,
               "risk": tool.get("risk", "read"), "status": "running",
               "title": f"正在执行 {name}（已确认）"}

        # 已确认 → 本次执行强制走"会话已授权"分支（令牌本身就是管理员的授权凭据）
        approved_ctx = dict(ctx)
        approved_ctx["allow_dangerous"] = True
        tools2 = get_admin_tools(approved_ctx)
        name2 = {t["name"]: t for t in tools2}
        run = name2.get(name, {}).get("run")
        t0 = time.time()
        try:
            text, stats, charts = self._split(run(args))
            ok_flag = not text.startswith("❌")
        except Exception as e:
            text, stats, charts = f"❌ 执行异常：{str(e)[:120]}", [], []
            ok_flag = False
        elapsed = round(time.time() - t0, 2)
        logger.info(f"[admin_assistant] uid={uid} tool={name} args={args} "
                    f"approved=True ok={ok_flag} {elapsed}s")

        view = name2.get(name, {}).get("view", "log")
        rich = {"report": text, "stats": stats, "charts": charts} \
            if (ok_flag and view == "report") else {}
        action = {"seq": 1, "tool": name, "args": args, "risk": tool.get("risk", "read"),
                  "status": "done" if ok_flag else "failed",
                  "brief": self._brief(text), "elapsed": elapsed, **rich}
        yield {"type": "step", **{k: v for k, v in action.items() if k != "seq"},
               "seq": 1}
        yield {"type": "summary", "text": self._summarize([action]), "actions": [action],
               "rounds": 0, "elapsed": round(time.time() - start, 2)}
        yield {"type": "done", "elapsed": round(time.time() - start, 2), "rounds": 0}

    # ---------------- 可选润色（默认关） ----------------
    def _polish(self, message: str, summary: str) -> str:
        try:
            from core.llm_client import llm_client
            res = llm_client.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system",
                     "content": "把下面这份后台操作流水改写成中文汇报，保留全部数字与单号，"
                                "不得新增任何数字、单号或结论，不得删除失败项。只输出汇报正文。"},
                    {"role": "user", "content": f"管理员指令：{message}\n\n流水：\n{summary}"},
                ],
                temperature=0.2,
                extra_body={"enable_thinking": False},
            )
            out = (res.choices[0].message.content or "").strip()
            return out or summary
        except Exception as e:
            logger.warning(f"[admin_assistant] 润色失败（回退确定性流水）: {e}")
            return summary


# 全局单例
admin_assistant = AdminAssistantExecutor()
