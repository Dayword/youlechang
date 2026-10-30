# ============================================================
# 业务 Agent (BusinessAgent)
# 基于 OpenAI 原生 function calling 工具循环 + 园区业务工具集
# （tools/business_tools.py，不依赖 langchain-openai）
# ------------------------------------------------------------
# 职责：
# 1. 关键词路由：判断问题是否适合由业务 Agent 处理（个人账户 / 办卡 / 失物 /
#    项目价目组合推荐等"办事型"问题）
# 2. Agent 执行：LLM 自主选择调用业务工具 → 结构化数据 → 口语化回答
# 3. 安全降级：未调用任何工具 / 执行异常时 fallback，由 Orchestrator 走 RAG
# 4. 用户身份由 Orchestrator 注入，工具按用户绑定（服务端权威，不经过模型）
# ============================================================
import json
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PARK_NAME = "大黄鸭室外游乐园"

# ------------------------------------------------------------------
# 账户管家 / 服务工单：触发词表定义在各自工具模块里（与"工具是否挂载"同一真源），
# 避免出现"路由认了这类问题、但工具没挂给模型"的错配。
# ------------------------------------------------------------------
from tools.account_tools import is_account_query                      # noqa: E402
from tools.service_tools import is_service_query                      # noqa: E402
from tools.weather_tools import mentions_weather                      # noqa: E402
from tools.consume_tools import is_consume_query                      # noqa: E402

# 追加给业务 Agent 的能力说明（**按需拼接**：只有本轮真挂了该组工具才写进 prompt，
# 保证普通业务问答的 prompt 与改动前完全一致，不引入额外延迟）
_EXTRA_PROMPTS = {
    "account": """
【账户管家能力（本轮已开启）】
- get_my_orders：我的办卡/充值订单与到账状态
- get_my_card_detail：我每张卡的余币 / 是否生效 / 是否可退
- check_refund_eligibility：退卡资格核查（逐张给出可退金额与不可退原因）
- request_refund_card：执行退卡（写操作，不可逆）
- get_redeem_qrcode：生成会员码 / 提币码
- get_my_consumptions：我的游乐消费明细（每笔玩了什么、扣多少币、是否已核销）
- list_my_favorites：我的收藏
- get_notices：园区公告与今日热点 / 活动
- get_my_profile：我的账号资料（只读，手机号已脱敏）

【账户管家铁律】
1. 退卡**不可逆**，必须走三步：先调 check_refund_eligibility → 把可退金额与影响
   （卡失效、余币扣减）明确告诉用户 → 等用户明确回复确认，才可调
   request_refund_card(user_confirmed=true)。用户没确认前一律不得执行，
   也不要替用户"假设"他确认了。
2. 订单到账、余币、可退金额、消费笔数等一律以工具返回为准，禁止估算或编造；
   工具说没有就说没有，不要用"应该""大概"糊过去。
3. 手机号/密码等敏感信息只回脱敏结果；改昵称、改密码、换绑手机号属设置页操作，
   AI 不能代办，只能引导。
4. 用户要"出码/会员码/提币码"时**直接调 get_redeem_qrcode**，不要先替他判断
   "你没生效卡所以出不了码"——**能不能出码由工具决定**。工具没出码才说不可以，
   而且要照工具给的理由说（如未登录），不许自己编一套拒绝理由。
""",
    "service": """
【服务工单能力（本轮已开启）】
- match_lost_items：按用户描述在园区失物库（待认领）中模糊匹配
- report_lost_item：报失登记（写操作）
- submit_feedback：提交意见 / 建议 / 投诉（写操作）

【服务工单铁律】
1. 用户说"我丢了 XX / 有没有人捡到 XX"时**必须先调 match_lost_items 查库**，
   严禁不查就说"没有"或直接让用户去报失。
2. 查到疑似物品 → 让用户先确认是不是他的（引导到「失物认领」页），
   **不要直接登记报失**（避免一件东西既在待认领列表又新增一条重复报失）。
3. 没查到 → 把要登记的内容（名称）复述给用户并询问是否登记，
   用户确认后才可调 report_lost_item(user_confirmed=true)。
4. 反馈/投诉同理：先把整理后的内容复述确认，用户确认后才 submit_feedback。
""",
    "weather": """
【天气能力（本轮已开启）】
- get_weather：查指定城市（默认北京·园区所在城市）实时天气与未来两天预报，
  并给出园区出游建议。用户的问题同时涉及天气与其他诉求（如"下雨的话玩什么"）时调用。
""",
    "trip": """
【出行规划（本轮已开启）】
- get_weather：用户说的那天的天气（明天/周末/后天…）
- park_location：园区门店地址与导航入口
- plan_trip：本人到访统计——每次平均消费多少币、每个项目平均玩几次（均为整数）+ 画像偏好

【必须按这个顺序把工具调完再答，缺一项都不算完成】
① **先调 get_weather**：用户说了"明天/周末要来玩"，就替他查那天的天气，
   **不必等他开口问天气**。园区全部是室外项目，天气直接决定"能不能来"。
② **天气结论放在最前面，优先于地址和推荐**：
   · 恶劣（大雨/暴雨/雷雨/大风）→ 必须明确提醒**可能临时闭园**，并给**室内备选**
     （万达广场三号楼 happy go 室内游乐园）或改期建议。此时**先讲这个**，
     并且**绝对不要**再说"很适合来玩"这种话。
   · 一般降水（小雨/阵雨）→ 提醒带雨具 + 室外项目可能暂停，让用户有心理预期。
   · 晴好 → 一句话带过，不占篇幅。
③ 调 park_location 告知园区地址（用户问的是"你们那"，要让他知道怎么来）。
④ 调 plan_trip 拿到"每次平均多少币、每个项目平均几次"，按这个习惯量排组合。
⑤ 用户提到孩子：若档案里**没有**这个孩子的年龄/身高，如实说"档案里没记到"，
   **不要猜**。若②已判成恶劣天气，方案要以"改期/改去室内"为主，
   **不要再推荐一整套游玩组合**（能不能来比玩什么更重要）。

【★ 数字铁律（本轮最容易犯的错，务必照做）】
1. plan_trip 返回的结果里有一个 `[[TRIP_FACTS]] … [[/TRIP_FACTS]]` 标记块，
   那是**本轮唯一的数字来源**。回答里出现的**每一个金额（币数）与次数**，
   都必须**逐字**来自这个块（或块里几项相加）。
2. **严禁自己造数**：不许把几项金额相加后报一个新总数，不许"估算一下差不多"，
   不许出现块里没有的任何金额（例如块里只有 90，却说出"平均每次花 742 币"）。
3. **次数**同样照块走：默认 **1 次**；只有当块里某项目的"平均次数"确实到 2 或以上，
   才说 2 次；**任何项目最多 3 次**。不许把项目全排成 3 次。
4. 若 plan_trip 说"次数太少/没有记录/排不出组合"，就**如实照说**，
   不要为了给结论而编一个数字补上（"我还不知道"远好过编造）。
5. **每个孩子的喜好单独看**：某个孩子在档案里没填过喜欢的项目，
   就明确说「我还不知道XX喜欢玩什么项目」，并邀请他在「AI 记得我」里勾选；
   **绝不能**把另一个孩子的喜好说成他的（张冠李戴是最严重的错误）。
6. **账号级「偏好项目」不是某个孩子的喜好**：它只代表"这个用户提到过、且没排斥"的项目。
   某个孩子的专属喜好**只认 plan_trip 里「孩子N（…，喜欢X）」括号内的内容**；
   孩子没填过就照实说不知道，**不许**拿账号级偏好替他说话
   （实测漏过：「孩子 likes 为空 → 却说"你女儿档案里记到的偏好项目是蹦床"」）。

【最后按这个顺序整理成一段话】天气（含能否成行）→ 地址 → 孩子情况 → 推荐组合 → 注意事项。
""",
    "consume": """
【购物 / 办卡能力（本轮已开启）】
- purchase_projects：【写操作·真实扣币】直接帮用户买游乐项目——自动按库里的权威价算钱、
  核对余币、扣币、生成核销码（purchase_projects / purchase_card 见下）
- purchase_card：【写操作】直接帮用户办储值卡 / 充值

【购物铁律（重点：一步到位，别把球踢回给用户）】
1. 办事顺序固定为"先看余币 → 再算账 → 直接办"：
   - 先调 get_my_balance 拿用户当前可用余币；
   - 再用价目（get_attractions 或 purchase_projects 自己算）得出本次总价；
   - **余币 ≥ 总价 → 直接调 purchase_projects 把事办了**，然后汇报结果：
     买了什么、扣了多少币、还剩多少币、核销码是多少、到哪儿出示；
   - **余币 < 总价 → 不要下单**，直接给结论「您的余币不足，请充值后再购买」，
     并说清还差多少币、有哪些充值档位；用户要充就调 purchase_card 直接帮他充。
2. **只给结论，不给"建议清单"**。禁止出现"建议您先确认是哪种…""可以去现场问问工作人员"
   "具体以现场公示为准"这类把问题推回给用户的话。要么把事办成，要么明确说办不成以及为什么。
3. 价格、余币、扣款额、核销码**一律取自工具返回**，一个字都不许自己算或编。
4. 用户只是在问价 / 问规则（"过山车多少钱""办卡流程有哪些"）时**不要调用** purchase_* 工具，
   那是知识问答，照常回答即可。判断标准：用户有没有让你"买 / 办 / 充"。
5. 办完之后不要再说"点击下方按钮办理"——事已经办完了，直接告诉他结果。
""",
}

# ------------------------------------------------------------------
# 路由关键词（"办事型"问题触发业务 Agent）
# ------------------------------------------------------------------
_LOST_WORDS = ["丢了", "丢失", "不见了", "失物", "招领", "捡到", "捡了", "遗落",
               "找不到了", "弄丢", "找东西", "东西丢了", "物品丢"]
_ACCOUNT_WORDS = ["余额", "我的卡", "持卡", "还剩", "剩多少", "多少币", "几个币",
                  "签到", "打卡", "我的账户", "充了多少", "卡里"]
_CARD_WORDS = ["办卡", "办什么卡", "办张卡", "想办卡", "推荐办", "买卡", "储值卡",
               "充值卡", "充钱", "怎么充值", "送多少币", "办卡优惠"]
# 组合推荐类：需要把"孩子年龄/剩币"与"项目价目/办卡"结合计算的场景
_PLAN_STRONG = ["能玩", "适合", "推荐", "有什么项目", "玩什么", "可以玩", "项目推荐"]
_PLAN_HINT = ["岁", "币", "剩", "孩子", "儿童", "小孩", "陪同", "身高"]
# 「画像复述」问句（C3）：喜好动词 + 疑问词 → 答案在画像里，必须走业务路径。
# 两个词表故意收窄：只放"在问偏好本身"的写法，避免把"小朋友喜欢玩蹦床要注意什么"
# 这类规则问题也吸过来（那种问题知识库有答案，走 RAG 才对）。
_PREF_WORDS = ["喜欢", "偏好", "爱玩", "常玩", "最爱"]
_PREF_QUEST = ["什么项目", "哪些项目", "玩什么", "什么好玩的", "哪些好玩的",
               "喜欢什么", "喜欢哪些", "玩哪些", "什么项目好玩"]
# 余额算术类：「剩下的币够玩几次蹦床」「这些币能玩几轮」——
# 这类问句既没有购买动作词（is_consume_query 不认），"够玩几次"也不在下面任何一张
# 词表里，此前会被判成知识库问题 → 走 RAG → 零资料兜底，**压根没查余额**。
_BALANCE_TIMES_WORDS = ["够玩几次", "能玩几次", "够玩", "能玩几", "还能玩几",
                        "还可以玩几", "够不够玩", "剩的币", "剩下的币", "剩多少币",
                        "还能玩什么", "够玩什么"]
# 消费统计分析类（走统计/图表工具）
_STATS_WORDS = ["帮我分析", "统计分析", "分析一下", "消费统计", "消费分析", "消费情况",
                "最近一周", "最近一个月", "最近一年", "这个月", "上个月", "今年",
                "游玩次数", "排行", "趋势", "消费记录分析", "账单分析", "玩了什么",
                "花多少钱了", "花了多少钱", "办了哪些卡", "充值记录"]
# 办卡/充值的"流程咨询"词（怎么办卡/怎么充值/如何办理…）—— 不涉及个人账户与实时在售卡，
# 属纯知识问答，交 RAG（可走 FAQ 匹配 + 缓存命中），避免每次进业务工具循环的多次 LLM 往返
_CARD_FLOW_WORDS = ["怎么办", "怎么办卡", "怎么办理", "如何办", "如何办理", "怎么办的",
                    "办理流程", "办卡流程", "充值流程", "办卡的流程", "充值的流程",
                    "流程", "步骤", "怎么充值", "怎么充", "如何充值", "去哪办",
                    "在哪办", "哪里办", "在哪充值", "哪里充值", "在哪充"]
# 办卡"事务性"意图词：需实时在售卡/结合账户算账 → 仍走业务 Agent（工具返回权威价）
_CARD_TRANSACTION_WORDS = ["买", "推荐", "划算", "优惠", "哪张", "哪个", "哪档", "什么卡",
                           "送多少", "选哪", "够用"]

# ------------------------------------------------------------------
# 出行规划类（D1/D4/D5）：用户说「明天/周末要带孩子来玩，给个攻略」
# ------------------------------------------------------------------
# 这类问句此前**一句都不命中任何词表**（截图那句是靠「推荐」+「孩子」撞上的），
# 于是既不会挂天气工具、也不会调 plan_trip —— 才出现"说好帮看天气却没看"。
#
# ⚠️ 判据必须**两个词表同时命中**：`_TRIP_WORDS` 里的词单独出现太容易误伤
#   （"营业时间有什么安排""办卡有哪些步骤"都会被「安排/步骤」带偏），
#   所以要求再叠一个**时间锚**（明天/周末/后天…）或**出行动词**。
_TRIP_WORDS = ["攻略", "行程", "规划", "安排", "怎么玩", "玩一天", "怎么安排",
               "帮我们看看", "筹划",
               "带孩子去", "带娃去", "去你们那", "去你们那里", "给我建议"]
# 时间锚 / 出行动词：与 _TRIP_WORDS 同时出现才算行程规划
_TRIP_ANCHOR = ["明天", "后天", "大后天", "周末", "周六", "周日", "这周", "下周",
                "今天", "等会", "一会儿", "五一", "十一", "国庆", "中秋", "寒假",
                "暑假", "来玩", "去玩", "要去", "来这边", "过去玩"]
# ★ 历史习惯判据（第 16 轮补）：用户问「**平时/每次都**玩什么」时问的是**到访统计**，
#   不是"余额还能玩什么"。此前这类问句判不进 is_trip_plan_call → plan_trip 不挂 →
#   模型只能拿余额（810）按 plan_play_combo 的口径排，于是又出现"每个项目各 3 次"。
#   命中此表即视为行程规划（问的是"我过去的习惯"），无条件挂 plan_trip。
_TRIP_HABIT_WORDS = ["平时", "平常", "每次都", "每次来", "每次去", "一直以来",
                     "通常", "以往", "之前来", "以前来", "到访", "来的次数",
                     "去过几次", "来过几次", "玩过几次", "消费习惯", "游玩习惯"]
# 涉及个人账户的业务工具：查询结果因人而异，其答案不得写入公共 Redis 缓存（避免串用户）
PERSONAL_TOOL_NAMES = {"get_my_balance", "daily_checkin",
                       "query_consumption_stats", "generate_stat_charts",
                       # ★ plan_play_combo 的方案是按「用户当前余币 + 本人偏好」算出来的，
                       #   一旦写进公共缓存，用户充了币/花掉币之后再问，模型会照抄旧数字
                       #   （实测：余额 810 的答案被缓存，打桩成 0 币再问，回答仍是"还剩 810 币"）。
                       #   金额答错会直接引发纠纷，绝不缓存。
                       "plan_play_combo",
                       # ★ plan_trip 的「每次平均多少币 / 每项平均几次」同样是**本人消费统计**
                       #   算出来的，写进公共缓存就会让别的用户读到他的消费习惯（隐私 + 答错）。
                       "plan_trip",
                       # 账户管家：个人数据 / 不可逆写操作，答案一律不写公共缓存
                       "get_my_orders", "get_my_card_detail", "check_refund_eligibility",
                       "request_refund_card", "get_redeem_qrcode", "get_my_consumptions",
                       "list_my_favorites", "get_my_profile",
                       # 服务工单：写入个人/园区业务数据（含登记编号），同样不缓存
                       "report_lost_item", "submit_feedback",
                       # 购物/办卡：含个人余币与核销码，且是不可逆的扣币/发卡写操作，
                       # 一旦写进公共缓存就会被别的用户读到，绝不缓存
                       "purchase_projects", "purchase_card"}


def is_business_question(query: str, history: str = "") -> bool:
    """
    关键词预判：是否"办事型"问题（个人账户/办卡/失物/统计/组合推荐）。
    纯知识类（营业时间、票价查询、注意事项等）不在此列，继续走 RAG。
    办卡/充值的纯流程咨询（怎么办卡、怎么充值…）同样不在此列——无个人数据、
    有确定的知识库答案，走 RAG 后可被 Redis 缓存，二次提问秒回。

    延迟说明：**只按本轮问句判断，不拼接历史**。历史是多轮长文本，一旦之前聊过
    "办卡/余额/天气"等话题，后续任意问句都会被误判并交给业务 Agent（多一次多轮 LLM
    往返，实测 8~13s）。真实的追问（"那办卡呢""我剩多少币""就剩10个币能玩什么"）
    本身就带业务关键词，按问句判断即可命中。
    """
    text = (query or "").strip()
    if not text:
        return False
    # 最强的"办事"信号：用户明确要买 / 要办 / 要充（"帮我买三个过山车""我要充200"）。
    # 放在最前面，因为这类问句一个词都不在下面任何一张词表里——此前正是它们掉进 RAG，
    # 由模型现编一段"通用建议"回给用户，而用户要的是把事办了。
    if is_consume_query(text):
        return True
    if any(w in text for w in _BALANCE_TIMES_WORDS):
        return True
    if any(w in text for w in _LOST_WORDS):
        return True
    if any(w in text for w in _ACCOUNT_WORDS):
        return True
    if any(w in text for w in _STATS_WORDS):
        return True
    card_hit = any(w in text for w in _CARD_WORDS)
    if card_hit:
        is_flow = any(w in text for w in _CARD_FLOW_WORDS)
        is_transaction = (
            any(w in text for w in _PLAN_STRONG)
            or any(w in text for w in _ACCOUNT_WORDS)
            or any(w in text for w in _CARD_TRANSACTION_WORDS)
        )
        if is_flow and not is_transaction:
            # 例："怎么办卡充值" → 走 RAG（知识库 FAQ，可缓存）
            return False
        return True
    # 组合推荐：强意图词 + 提示词同时出现（如"剩120币还能玩啥"、"10岁适合玩什么"）
    hit_strong = any(w in text for w in _PLAN_STRONG)
    hit_hint = any(w in text for w in _PLAN_HINT)
    if hit_strong and hit_hint:
        return True
    # 「我女儿喜欢玩什么项目」这类**画像复述**问句：答案就在用户画像里，
    # 知识库必然零命中 → 交给 RAG 只会走到兜底话术。判据 = 喜好动词 + 疑问词。
    # （实测症状："我女儿喜欢玩什么项目？" → "📌 园区知识库里暂时没有收录这个问题"）
    # "为什么…喜欢…"是知识类问法，排除掉。
    if (any(w in text for w in _PREF_WORDS)
            and any(w in text for w in _PREF_QUEST)
            and "为什么" not in text):
        return True
    # 出行规划（D1/D4/D5）：「明天要带一个男孩去你们那玩，给个攻略」
    #   —— 必须走业务 Agent，否则既不会挂天气工具、也不会调 plan_trip。
    if is_trip_query(text):
        return True
    # ★ 历史习惯问句（第 16 轮）：问的是"我**平时**玩什么/去过几次"，
    #   答案在**到访统计**（plan_trip）里，知识库必然零命中 → 交给 RAG 只会走兜底话术。
    #   实测症状："我平时来都玩哪些项目？" → "📌 园区知识库里暂时没有收录这个问题"。
    if any(w in text for w in _TRIP_HABIT_WORDS):
        return True
    # 账户管家（订单/退卡/会员码/明细/收藏/资料）与服务工单（报失/反馈）：
    # 词表定义在各自工具模块，与"工具是否挂给模型"同一真源，避免两边错配。
    return is_account_query(text) or is_service_query(text)


def is_trip_query(query: str) -> bool:
    """是否「出行规划」型问句（决定挂不挂天气/地址/到访统计三件套）。

    判据 = `_TRIP_WORDS` **且** `_TRIP_ANCHOR` 同时命中。

    为什么要求两个词表同时命中（保守优先，与第 13 轮的回答分流同一原则：
    **判不出就放行、误判代价远大于漏判**）：
      · 只有 `_TRIP_WORDS`（"营业时间有什么安排""办卡的步骤"）→ 是知识问答，不该走业务
      · 只有 `_TRIP_ANCHOR`（"明天天气怎么样"）→ 有天气快速通道/地图 Agent 各司其职
      ⇒ 两个都中才是真正的"我要来玩，给我安排一下"。
    """
    text = (query or "").strip()
    if not text:
        return False
    return (any(w in text for w in _TRIP_WORDS)
            and any(w in text for w in _TRIP_ANCHOR))


def is_trip_plan_call(query: str, history: str = "") -> bool:
    """**挂载三件套**（天气 / 园区地址 / 到访统计）的判据。

    比 is_trip_query 宽一档：行程规划问句**或**"带娃 + 时间锚"（用户可能只说
    "明天带一个男孩去你们那玩"，没有"攻略/安排"这类词）。
    实测这条就是截图那句 —— 不挂工具 → 模型只能说"需要我帮你看看下雨情况"。
    """
    if is_trip_query(query):
        return True
    text = (query or "").strip()
    if not text:
        return False
    # ★ 历史习惯问句（第 16 轮补）：问的是"我**平时**玩什么/去过几次"→ 必须挂 plan_trip。
    #   漏了这一条时，模型会拿**余额**（plan_play_combo 口径）冒充"平时"，
    #   于是又把每个项目排成 3 次 —— 用户截图里的 690 币就是这个成因。
    if any(w in text for w in _TRIP_HABIT_WORDS):
        return True
    kid_words = ["孩子", "男孩", "女孩", "儿子", "女儿", "小孩", "小朋友", "娃", "宝宝"]
    return (any(w in text for w in kid_words)
            and any(w in text for w in _TRIP_ANCHOR))


def business_service_tags(query: str) -> list:
    """
    根据问题类别返回可点击的服务出口（供前端渲染服务按钮）：
      recharge    → 去办卡/充值页
      lost_menu   → 打开失物认领列表
      report_lost → 打开报失登记表单
    """
    tags = []
    if any(w in query for w in _LOST_WORDS):
        tags.append("lost_menu")
        tags.append("report_lost")
    if any(w in query for w in _ACCOUNT_WORDS + _CARD_WORDS) or \
            (any(w in query for w in _PLAN_STRONG) and any(w in query for w in _PLAN_HINT)):
        tags.append("recharge")
    return tags


SYSTEM_PROMPT = f"""你是"{PARK_NAME}"微信小程序里的智能业务助手，能够直接帮用户"办成一件事"：查储值卡余额、每日签到、查失物招领、查游乐项目价目、按余额与偏好排游玩组合、推荐办卡方案。

【强制要求】
凡是涉及账户/价格/失物/规则的事实，**必须先调用工具拿真实数据再回答**，禁止凭印象编造。
余额 / 预算 / "这些币还能玩什么" 这类问题**必须调用 plan_play_combo** 出方案，
不许自己心算金额、也不许自己排组合（算错余额或排出超预算的组合是最常见的错误）。

【铁律】
1. 涉及个人账户（余额/我的卡/签到）必须先调用对应工具，把用户身份交给服务端验证，禁止编造币数、卡名或签到状态；
2. 办卡推荐必须以工具返回的在售卡为准，金额与赠送币数不得自行修改；可以分步给出核对用的算式（如 120 = 50 + 40 + 30）；
3. 失物：先调用失物检索工具；找到则告诉用户物品与报失信息，找不到则如实说明并引导登记报失；
4. 价格与项目规则来自"游乐项目价目"工具；需要引用书面规则（退卡/陪游/安全/营业时间等）时调用知识库检索工具取原文，禁止凭印象编造；
5. 如果资料里没有某规则（例如某项目的身高/年龄限制），要如实说"资料里没有明确"，只陈述已知规则（如：旋转木马仅限儿童乘坐、每名儿童可带一名大人免费陪同）；
6. 回答口语化、简洁，控制在 4~6 句内。**能直接办的事就办完再汇报结果**，
   不要只丢一个"建议"、更不要把操作推回给用户（禁止"您可以点击下方按钮自行办理"这类说法）；
   只有确实必须用户本人操作的（如线上支付收银台、改密码 / 换绑手机号），
   才说明下一步，并讲清楚为什么只能他自己来。
7. **用户档案里的「不喜欢」是硬约束**：任何推荐（项目清单、组合方案、办卡建议）都不得
   包含用户明确排斥的项目；用户问到它时可以如实说明它的规则，但要主动提一句
   "你说过不喜欢它，我就不往这个方向推荐"。
8. **"我喜欢/我女儿喜欢玩什么""适合玩什么""帮我安排一下"这类问题，必须先调用
   plan_play_combo**：偏好存在服务端画像里、由工具统一读取。凭档案印象直接罗列项目
   不仅可能漏项，更严重的是**一次工具都不调用会被判定为"无答案"**，
   整轮回答被降级成知识库兜底（实测："我女儿喜欢玩什么项目？"→
   "📌 园区知识库里暂时没有收录这个问题"）。宁可多调一次工具，也不要交出空手回答。

【可用工具】
- get_my_balance：查我的余额与持卡
- daily_checkin：每日签到
- list_sale_cards：在售储值卡（办卡/充值推荐）
- search_lost_and_found：失物库检索
- get_attractions：游乐项目价目与规则（1元=1币）
- plan_play_combo：**按余额与偏好排出可直接下单的组合**（几次蹦床+几次荡秋千+剩余多少币），
  自动避开用户不喜欢的项目。余额/预算/"还能玩什么"这类问题一律先用它，别自己心算
- knowledge_search：园区知识库原文检索
- query_consumption_stats：本人消费统计（项目排行/办卡类型/按月消费）
- generate_stat_charts：为本人消费统计生成可视化图表（PNG），生成后应提示"已在下方为您生成图表"并引用其中的数字

【能力按需开启】
系统会按本轮问题另外开启"账户管家 / 服务工单 / 天气 / 出行规划 / 购物办卡"能力，并在下方追加对应工具与铁律。
**只在下方列出了的工具才可调用**；没有列出的能力说明与本轮问题无关，
不要尝试调用，也不要向用户承诺你能办。"""


class BusinessAgent:
    """园区业务 Agent（OpenAI function calling 自主工具循环）"""

    # ============== 路由 ==============

    def is_business_question(self, query: str, history: str = "") -> bool:
        return is_business_question(query, history)

    def service_tags(self, query: str) -> list:
        return business_service_tags(query)

    # ============== 工具调用循环 ==============

    def _run_tool_loop(self, user: Optional[dict], query: str, profile_note: str = ""):
        """
        执行整个业务 Agent 工具循环（含内部错误处理）。
        返回: (final_answer, tool_call_count, charts, tool_names)
        若模型一步工具都没调用就结束，tool_call_count=0 → 上层判定 fallback。
        tool_names: 本次实际调用的工具名列表（用于上层判断答案是否含个人账户数据）
        charts: 本请求内工具生成的图表 [{chart_id,title,url}]（请求级隔离）
        profile_note: 用户画像摘要（与 RAG 路径同一份口径）。非空时并入本轮 prompt，
            使"推荐/适龄/规则"类回答能结合孩子的年龄与身高，而不是给通用答案。
        """
        from config import LLM_TEXT_MODEL, LLM_TEMPERATURE
        from tools.business_tools import get_business_tools, tools_to_openai

        ctx = {"charts": [], "images": []}
        # ---------- 工具按需挂载（延迟硬约束）----------
        # 工具 schema 会整体进入 prompt，工具越多首字延迟越高。基础业务工具保持原样，
        # 账户管家 / 服务工单 / 天气**只在本轮问句命中各自触发词时**才追加，
        # 因此普通业务问答（如"办卡推荐"）的 prompt 与工具列表与改动前完全一致。
        tools = get_business_tools(user, ctx=ctx)
        extra_prompt = ""
        if is_account_query(query):
            from tools.account_tools import get_account_tools
            tools = tools + get_account_tools(user, ctx=ctx)
            extra_prompt += _EXTRA_PROMPTS["account"]
        if is_service_query(query):
            from tools.service_tools import get_service_tools
            tools = tools + get_service_tools(user, ctx=ctx)
            extra_prompt += _EXTRA_PROMPTS["service"]
        if mentions_weather(query):
            from tools.weather_tools import get_weather_tools
            tools = tools + get_weather_tools()
            extra_prompt += _EXTRA_PROMPTS["weather"]
        # ★ 出行规划三件套（D1/D5）：只在行程类问句时挂。
        #   park_location 与 plan_trip 都在 get_business_tools 里常备，这里只负责
        #   追加"怎么用"的协作顺序说明；普通问答的 prompt 与工具列表**逐字不变**。
        #   同时**兜住**天气工具：用户说"明天带娃来玩"但没提"天气"两个字时，
        #   mentions_weather（已含行程判据）应当已挂上；这里再确认一次，
        #   避免 order 依赖（mentions_weather 若因 import 异常返回 False 仍能兜上）。
        if is_trip_plan_call(query):
            if not mentions_weather(query):
                from tools.weather_tools import get_weather_tools
                tools = tools + get_weather_tools()
                extra_prompt += _EXTRA_PROMPTS["weather"]
            extra_prompt += _EXTRA_PROMPTS["trip"]
        if is_consume_query(query):
            from tools.consume_tools import get_consume_tools
            tools = tools + get_consume_tools(user, ctx=ctx)
            extra_prompt += _EXTRA_PROMPTS["consume"]

        name2run = {t["name"]: t["run"] for t in tools}
        openai_tools = tools_to_openai(tools)

        # 复用全局 LLM 客户端单例：避免每次请求新建 OpenAI 客户端带来的
        # TCP/TLS 握手开销（多轮工具循环会重复建连，实测每轮约 0.3~1s）
        from core.llm_client import llm_client
        client = llm_client.get_llm()
        # ---------- prompt 组装（C3：稳定内容进前缀，变化内容排最后）----------
        # 供应商侧的前缀缓存是**从第 0 个 token 起逐字节比对**的，所以顺序很关键：
        #   tools schema（只随触发词分组变化）→ SYSTEM_PROMPT（模块常量）
        #   → 用户档案（同一账号逐字稳定，family 在前、偏好在后）
        #   → 本轮触发的能力说明 → 最后才是用户问句
        # 原先把「固定不变的【强制要求】」拼在 user 段（变化内容之后），
        # 等于把一段本该命中缓存的固定文字放到了分歧点之后，白白浪费。
        _system = SYSTEM_PROMPT
        if profile_note:
            # 画像只影响"怎么答"，不影响"查什么"：工具的取数口径与顺序完全不变，
            # 因此首字延迟与工具轮数不受影响，只是 system 多了一行摘要。
            _system += (f"\n\n【用户档案】（来自之前对话，回答可自然结合，不要生硬罗列；"
                        f"档案中没写明的项目限制一律以现场工作人员要求为准）\n{profile_note}\n"
                        f"注：「偏好项目」= 该用户提到过且没有排斥的项目；「不喜欢」= 明确排斥，"
                        f"任何推荐都不得包含它。")
        _system += extra_prompt
        messages = [
            {"role": "system", "content": _system},
            {"role": "user", "content": query},
        ]

        tool_calls_total = 0
        tool_names = []
        final_answer = ""
        trip_tool_text = ""          # ★ E1b：plan_trip 的原始返回（含 [[TRIP_FACTS]]）
        other_tool_texts: list = []  # 其它工具的返回（办卡价目/余币… → 判数时豁免）
        for _ in range(6):  # 最多 6 轮工具循环，防失控
            resp = client.chat.completions.create(
                model=LLM_TEXT_MODEL,  # 业务工具循环为纯文字 function calling，走文本模型
                messages=messages,
                tools=openai_tools,
                tool_choice="auto",
                temperature=LLM_TEMPERATURE,
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                final_answer = (msg.content or "").strip()
                break

            tool_calls_total += len(msg.tool_calls)
            messages.append({
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                fn = tc.function
                tool_name = fn.name
                tool_names.append(tool_name)
                try:
                    args = json.loads(fn.arguments or "{}")
                except Exception:
                    args = {}
                run = name2run.get(tool_name)
                if run is None:
                    result = f"未知工具：{tool_name}"
                else:
                    try:
                        result = run(args)
                    except Exception as e:
                        logger.warning(f"业务工具 {tool_name} 执行失败: {e}")
                        result = f"工具 {tool_name} 执行失败：{str(e)[:100]}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })
                if tool_name == "plan_trip":
                    trip_tool_text = str(result)
                else:
                    other_tool_texts.append(str(result))

        # ★ E1b：末端兜底校验 —— 回答里的金额必须都来自 plan_trip 下发的数字。
        #   只判定、不改写（改写要再调一次 LLM，违反"模型响应耗时不变"红线）；
        #   判定结果由上层决定用途（当前：记日志 + 该轮不写公共缓存）。
        #
        #   ★ 只在本轮"数字来源单一"时才做**严格判定**：一旦回答里还掺了别的
        #     带金额的工具（办卡价目 200/320、赠送差额、余币 810…），那些数字不在
        #     TRIP_FACTS 里，硬判就会大面积假红（实测：模型自己算的赠送差额 120/1500
        #     被误报成"编造"）。此时降级为**仅记录**（`strict=False`），
        #     仍然抓得住纯数字编造，但不再把合法的跨工具算术当成错误。
        trip_check = None
        if trip_tool_text:
            try:
                from services.trip_stats import check_trip_numbers
                trip_check = check_trip_numbers(final_answer, trip_tool_text)
                # 本轮还调了别的"可能带金额"的工具 → 判定降级为仅供参考
                trip_check["strict"] = not other_tool_texts
                if trip_check.get("checked") and not trip_check.get("ok"):
                    if trip_check["strict"]:
                        logger.warning(
                            "🚫 出行数字校验失败：回答里出现工具未下发的金额 %s（下发集合 %s）"
                            % (trip_check.get("unknown"), trip_check.get("known")))
                    else:
                        logger.info(
                            "ℹ️ 出行数字与其它工具金额混排（非严格判定，仅供参考）：%s"
                            % trip_check.get("unknown"))
            except Exception as e:  # pragma: no cover - 校验绝不能拖垮回答
                logger.warning(f"出行数字校验异常（忽略）: {e}")

        return (final_answer, tool_calls_total, ctx.get("charts") or [],
                tool_names, ctx.get("images") or [], trip_check)

    # ============== 主流程 ==============

    def process(self, query: str, history: str = "", user: Optional[dict] = None,
                progress_cb=None, profile_note: str = "") -> Dict:
        """
        执行业务 Agent 完整流程
        返回: {"query","answer","used_business_tool","fallback","services","trace"}
        fallback=True（未调用任何工具 / 异常）时由 Orchestrator 降级走 RAG
        profile_note: 用户画像摘要，透传给工具循环（见 _run_tool_loop）
        """
        start = time.time()
        charts: list = []
        images: list = []
        trip_check = None
        try:
            if progress_cb:
                progress_cb("💼 已识别为可执行任务，正在调起业务能力...")
            answer, tool_calls, charts, tool_names, images, trip_check = self._run_tool_loop(
                user, query, profile_note)
        except Exception as e:
            logger.error(f"💼 业务Agent执行异常: {e}")
            return self._fallback_result(query, start, f"agent_error: {str(e)[:120]}")

        if tool_calls == 0:
            # Agent 未调用任何工具 → 交给 RAG（保证答案有知识库支撑）
            logger.info("💼 业务Agent未调用任何工具，交由RAG处理")
            return self._fallback_result(query, start, "no_tool_used")
        if not answer:
            return self._fallback_result(query, start, "empty_answer")

        return {
            "query": query,
            "answer": answer,
            "used_business_tool": True,
            "fallback": False,
            "tool_names": tool_names,
            "services": self.service_tags(query),
            "charts": charts,
            # 工具生成的图片（如会员码二维码）：复用前端已有的 charts 图片卡通道，
            # 避免让模型把长 base64 抄进回答正文（不可靠且会把回答撑爆）
            "images": images,
            # ★ E1b：数值校验结果（None = 本轮没调 plan_trip，不参与判定）。
            #   上层据此决定"该轮是否允许写公共缓存"——编过数字的回答不能被缓存固化。
            "number_check": trip_check,
            "trace": {
                "agent": "business",
                "tool_calls": tool_calls,
                "elapsed": round(time.time() - start, 3),
                "number_check": trip_check,
            },
        }

    @staticmethod
    def _fallback_result(query: str, start: float, reason: str) -> Dict:
        return {
            "query": query,
            "answer": "",
            "used_business_tool": False,
            "fallback": True,
            "services": [],
            "trace": {
                "agent": "business",
                "fallback_reason": reason,
                "elapsed": round(time.time() - start, 3),
            },
        }


# 全局单例
business_agent = BusinessAgent()
