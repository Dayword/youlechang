# ============================================================
# 员工手机端 H5 - /staff/*（返回 HTML，不走前端构建）
# ------------------------------------------------------------
# 为什么把页面塞在后端而不是放进商家 PC 端：
#   核销是**走动中的动作**——工作人员拿着手机在项目边上，扫用户屏幕上的券码。
#   PC 端固定在售票处，够不着。而园区内网没有前端构建产物可托管，
#   所以直接由 FastAPI 吐一个自包含的 H5，扫码即达、零部署。
#
# 两个入口共用同一份页面：
#   GET /staff/verify?c=CODE  扫码落地页（小程序券二维码指向这里）
#   GET /staff/app            员工端首页（直接进 App）
#
# 鉴权（方案 B：同一账号密码 + 降权令牌）：
#   页面本身不挂鉴权（否则扫码者拿不到 HTML），由页面内的登录框调
#   /auth/staff/login 拿 token —— 它带 scope=staff，所以：
#     · /admin/* 一律 403（后端 core.auth.get_current_admin 挡的，不是前端不调用）；
#     · 只能打 /staff/api/* 白名单（见 api/staff_api.py）。
#
# 范围：仪表盘 / 失物招领 / 内容管理 / 办卡管理（查订单 + 现场开卡）都有，
#   首页默认落在「扫码核销 + 办卡管理」；知识库智能体不做，
#   右下角智能体只挂查询类工具（ctx.readonly）。
# ============================================================
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/staff", tags=["员工核销页"])


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#F4F5F7">
<title>鸭先知 · 员工端</title>
<style>
/* ============ 设计令牌：浅色底 + 唯一强调色（珊瑚橙）============
   视觉基调取自浅色高级后台参考稿：白卡 / 浅灰画布 / 超大数字 / 发丝分隔 / 环形+柱状图。
   铁律一：强调色只有一个（--accent 珊瑚橙），其余颜色只承担**语义**（金=现金待处理、
           薄荷=成功已核销、玫红=退款错误），不做装饰。
   铁律二：不用任何颜色渐变（品牌规范）；层级只靠「底色明度 + 1px 发丝线 + 极浅投影」。
   铁律三：品牌名「鸭先知」/ 人设名「鸭鸭」不改；不引入 emoji、不引入卡通图形。
   对比度：橙底一律配近黑字（--on-accent）。白字在 #FF6B3D 上只有 2.83:1，
   近黑字有 6.16:1 —— 既过 AA，也是浅色方案下更干净的做法。 */
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  color-scheme:light;
  --bg:#F4F5F7;--deep:#EBEDF1;
  --surface:#FFFFFF;--surface2:#F1F3F7;
  --ink:#171A20;--ink2:#5C6675;--ink3:#98A1AF;
  --line:#E5E8EE;--line2:#EFF1F5;
  --accent:#FF6B3D;--accent-d:#EA5527;--accent-soft:rgba(255,107,61,.10);
  --amber:#B7791F;--amber-soft:#FDF4E4;--amber-line:#F1E0BE;
  --ok:#1F8A5B;--ok-soft:#E7F5EE;--ok-line:#CBE9DA;
  --err:#D6455D;--err-soft:#FDECEF;--err-line:#F6D2D8;
  --on-accent:#171A20;
  --r:14px;--rs:11px;
  --shadow:0 1px 2px rgba(23,26,32,.05);
  --pop-shadow:0 12px 30px rgba(23,26,32,.16);
}
html{-webkit-text-size-adjust:100%;background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;overscroll-behavior-y:none}
.hide{display:none !important}
button{font-family:inherit}
svg{display:block}

/* ---------- 顶部 AppBar：白底 + 发丝线，与画布同族 ---------- */
.topbar{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.88);
  -webkit-backdrop-filter:saturate(150%) blur(14px);backdrop-filter:saturate(150%) blur(14px);
  border-bottom:1px solid var(--line);
  padding:calc(12px + env(safe-area-inset-top)) 16px 12px;
  display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{display:flex;align-items:center;gap:9px;font-size:15.5px;font-weight:700;
  letter-spacing:.2px;color:var(--ink)}
.brand .mark{width:26px;height:26px;flex:0 0 26px;border-radius:9px;background:var(--accent);
  color:var(--on-accent);font-size:14px;font-weight:800;letter-spacing:0;
  display:flex;align-items:center;justify-content:center}
.who{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink2)}
.who button{background:var(--surface2);border:1px solid var(--line);color:var(--ink);
  border-radius:999px;padding:6px 13px;font-size:12px;font-weight:600}
.who button:active{background:var(--line)}

/* 底部留白要同时避开 tabbar（约 58px）和悬浮球（76→132px），否则滚到底时右下角数据会被球压住 */
.wrap{max-width:560px;margin:0 auto;padding:16px 14px calc(152px + env(safe-area-inset-bottom))}

/* ---------- 卡片 ---------- */
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  padding:16px;margin-bottom:12px;box-shadow:var(--shadow)}
h2{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:700;margin:0 0 12px;
  color:var(--ink);letter-spacing:.2px}
h2::before{content:"";width:3px;height:14px;border-radius:2px;background:var(--accent);flex:0 0 3px}
/* 卡片外的页面级标题（仪表盘整页由多张 chart-card 组成，不再套一层大卡） */
h2.page-hd{font-size:16px;margin:2px 2px 13px}
/* 标题旁的「这是什么」ⓘ：点了从图标上方弹出气泡，数秒自动收回 */
.ih{width:16px;height:16px;flex:0 0 16px;border-radius:50%;border:1px solid var(--line);
  background:var(--surface2);color:var(--ink3);font-size:10.5px;font-weight:800;line-height:1;
  display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;
  font-family:inherit;letter-spacing:0}
.ih:active{background:var(--line)}
.sub{font-size:12.5px;color:var(--ink2);margin:0 0 14px;line-height:1.6}
label{display:block;font-size:12px;font-weight:600;color:var(--ink2);margin:14px 0 6px}
input,select{width:100%;min-height:46px;padding:12px 13px;border:1px solid var(--line);
  border-radius:var(--rs);font-size:15px;color:var(--ink);background:var(--surface);outline:none;
  font-family:inherit}
input::placeholder{color:var(--ink3)}
input:focus,select:focus{border-color:var(--accent);background:var(--surface);
  box-shadow:0 0 0 3px var(--accent-soft)}

/* ---------- 按钮：统一触控高度；主按钮=橙底深字 ---------- */
.btn{display:block;width:100%;min-height:48px;padding:13px;border:0;border-radius:var(--rs);
  font-size:15px;font-weight:600;letter-spacing:.3px;cursor:pointer;margin-top:14px;
  font-family:inherit;transition:transform .06s ease,background .15s ease}
.btn:active{transform:scale(.985)}
.btn-p{background:var(--accent);color:var(--on-accent)}
.btn-p:active{background:var(--accent-d)}
.btn-g{background:var(--surface);color:var(--ink);border:1px solid var(--line)}
.btn-g:active{background:var(--surface2)}
.btn-w{background:var(--amber);color:#fff}
.btn:disabled{opacity:.45;cursor:default;transform:none}
.mini{padding:8px 13px;border-radius:9px;font-size:13px;font-weight:600;margin:0;
  display:inline-block;width:auto;cursor:pointer;font-family:inherit;min-height:34px}
.mini-p{background:var(--accent);color:var(--on-accent);border:0}
.mini-g{background:var(--surface);color:var(--ink);border:1px solid var(--line)}
.mini-r{background:var(--err-soft);color:var(--err);border:1px solid var(--err-line)}
.mini-o{background:var(--amber-soft);color:var(--amber);border:1px solid var(--amber-line)}
.mini:disabled{opacity:.4}
/* 「⋯」触发器：次要操作收进它里面 */
.mini-x{background:var(--surface);color:var(--ink2);border:1px solid var(--line);
  padding:8px 11px;letter-spacing:1px}

/* ---------- 信息行 / 券码 ---------- */
.row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;
  border-bottom:1px solid var(--line2);font-size:14px}
.row:last-child{border-bottom:0}
.row .k{color:var(--ink2);flex-shrink:0}
.row .v{font-weight:600;text-align:right;word-break:break-all}
.code{font:700 32px/1.3 ui-monospace,Menlo,Consolas,monospace;letter-spacing:6px;
  color:var(--accent);text-align:center;margin:10px 0 12px;padding:14px 8px;
  background:var(--accent-soft);border:1px solid var(--line);border-radius:var(--r)}
.pill{display:inline-block;font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:999px}
.pill-w{background:var(--amber-soft);color:var(--amber)}
.pill-ok{background:var(--ok-soft);color:var(--ok)}
.pill-no{background:var(--surface2);color:var(--ink3)}
.pill-r{background:var(--err-soft);color:var(--err)}
.msg{font-size:13px;margin-top:12px;min-height:18px}
.msg.err{color:var(--err)}
.msg.ok{color:var(--ok)}
.hint{font-size:12px;color:var(--ink2);margin-top:10px;line-height:1.6}
.warn{background:var(--amber-soft);border:1px solid var(--amber-line);border-radius:var(--rs);
  padding:11px 13px;font-size:12px;color:var(--amber);margin-top:12px;line-height:1.6}

/* ---------- 分段控件 ---------- */
.seg{display:flex;gap:4px;margin-bottom:12px;background:var(--surface2);
  border:1px solid var(--line);border-radius:12px;padding:4px}
.seg button{flex:1;padding:9px 4px;border-radius:9px;border:0;background:transparent;
  font-size:13px;color:var(--ink2);font-family:inherit;font-weight:600;cursor:pointer}
.seg button.on{background:var(--accent);color:var(--on-accent);box-shadow:0 1px 3px rgba(23,26,32,.14)}

.item{border:1px solid var(--line);border-radius:12px;padding:12px 13px;margin-bottom:10px;
  background:var(--surface)}
.item .t{font-size:14px;font-weight:700;color:var(--ink)}
.item .m{font-size:12.5px;color:var(--ink2);margin-top:4px;line-height:1.6}
.item .acts{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
/* 卡片可点开详情时给一点可点性提示 */
.item.tap{cursor:pointer}
.item.tap:active{background:var(--surface2)}

/* ---------- 首页「扫一扫」大按钮：一天点几十次的那一个，直接给足强调色 ---------- */
.scan-hero{width:100%;display:flex;align-items:center;gap:14px;background:var(--accent);
  color:var(--on-accent);border:0;border-radius:18px;padding:18px 16px;margin-bottom:12px;
  text-align:left;font:inherit;cursor:pointer;transition:transform .06s ease;
  box-shadow:0 10px 24px rgba(255,107,61,.26)}
.scan-hero:active{background:var(--accent-d);transform:scale(.99)}
.scan-ico{width:54px;height:54px;flex:0 0 54px;border-radius:14px;background:rgba(23,26,32,.13);
  display:flex;align-items:center;justify-content:center;color:var(--on-accent)}
.scan-txt{display:flex;flex-direction:column;min-width:0}
.scan-t{font-size:17px;font-weight:800;letter-spacing:.3px}
.scan-s{font-size:12px;color:rgba(23,26,32,.62);margin-top:4px;line-height:1.5}

/* ---------- 扫码浮层（摄像头，保持深底） ---------- */
#scan{position:fixed;inset:0;z-index:60;background:#000;overflow:hidden}
#scan-video{width:100%;height:100%;object-fit:cover}
.scan-mask{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
.scan-frame{width:62vw;max-width:300px;aspect-ratio:1/1;border:2px solid var(--accent);border-radius:18px;
  box-shadow:0 0 0 9999px rgba(0,0,0,.62)}
.scan-tip{position:absolute;left:0;right:0;bottom:110px;text-align:center;color:#fff;font-size:13px}
.scan-close{position:absolute;bottom:calc(34px + env(safe-area-inset-bottom));left:50%;
  transform:translateX(-50%);background:#fff;color:var(--ink);border:0;
  border-radius:999px;padding:12px 32px;font-size:15px;font-weight:700}

/* ---------- 失物照片 ---------- */
.lf-row{display:flex;gap:12px;align-items:flex-start}
.lf-pic{width:66px;height:66px;flex:0 0 66px;border-radius:11px;object-fit:cover;background:var(--surface2);
  border:1px solid var(--line);display:flex;align-items:center;justify-content:center;
  font-size:11px;color:var(--ink3);cursor:zoom-in}
.lf-nopic{cursor:default}
.tag{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:999px;
  padding:3px 9px;font-size:11px;font-weight:600;margin:6px 4px 0 0}
/* ② 卡片详情面板：复用 .dlg.big 的壳，标题靠左、照片占满宽，读起来像一张"档案卡" */
.lf-more{display:inline-block;margin-top:8px;font-size:12px;font-weight:600;color:var(--accent)}
.dlg.big h3{text-align:left;font-size:17px;letter-spacing:0}
.dlg .lf-big{display:block;width:100%;max-height:230px;object-fit:cover;
  border-radius:12px;background:var(--surface2);margin:12px 0 10px}
.dlg .lf-big.lf-nopic{height:88px;display:flex;align-items:center;justify-content:center;
  color:var(--ink3);font-size:13px}
.dlg-tags{display:flex;flex-wrap:wrap;gap:6px;margin:11px 0 4px}

/* ---------- 照片全屏预览（看大图，保持深底） ---------- */
#imgbox{position:fixed;inset:0;z-index:70;background:rgba(12,14,18,.94);display:flex;
  align-items:center;justify-content:center;padding:16px}
#imgbox img{max-width:100%;max-height:100%;border-radius:10px}
#imgbox-close{position:absolute;top:calc(18px + env(safe-area-inset-top));right:18px;
  background:rgba(255,255,255,.18);color:#fff;border:0;border-radius:999px;
  padding:9px 19px;font-size:14px;font-weight:600}

/* ---------- 底部抽屉 / 弹窗 ---------- */
#passbox{position:fixed;inset:0;z-index:65;background:rgba(23,26,32,.42);display:flex;align-items:flex-end}
.pb-inner{background:var(--surface);width:100%;max-height:88vh;border-radius:20px 20px 0 0;
  padding:18px 14px calc(18px + env(safe-area-inset-bottom));overflow-y:auto}
.pb-inner::before{content:"";display:block;width:36px;height:4px;border-radius:2px;
  background:var(--line);margin:-6px auto 12px}
.pb-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.pb-hd b{font-size:16px;color:var(--ink)}
.pb-hd button{background:var(--surface2);border:1px solid var(--line);font-size:17px;color:var(--ink2);
  width:30px;height:30px;border-radius:9px;line-height:1}
.item .acts .mini{flex:0 0 auto}

.pass{display:flex;align-items:center;gap:10px;border:1px solid var(--line);
  border-radius:12px;padding:11px 12px;margin-bottom:8px;background:var(--surface2)}
.pass.dim{opacity:.55}
.pass .pi{min-width:0;flex:1}
.pass .pn{font-size:14px;font-weight:600;color:var(--ink)}
.pass .pc{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
  letter-spacing:1px;color:var(--ink2);margin-top:2px}
.pass .pa{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}
.batch{display:flex;align-items:center;gap:10px;background:var(--surface2);border:1px solid var(--line);
  border-radius:11px;padding:10px 12px;margin:12px 0;flex-wrap:wrap}
.batch label{margin:0;font-size:13px;font-weight:600;color:var(--ink);display:flex;align-items:center;gap:6px}
input[type=checkbox]{width:auto;min-height:auto;padding:0;margin:0;accent-color:var(--accent)}

/* ---------- KPI（账单小卡） ---------- */
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:13px}
.kpi .kl{font-size:11.5px;color:var(--ink2)}
.kpi .kv{font-size:24px;font-weight:800;color:var(--ink);margin-top:3px;letter-spacing:-.8px;
  line-height:1.2;font-variant-numeric:tabular-nums}
.kpi .kv.amber{color:var(--amber)}

/* ============================================================
   仪表盘专用组件：超大数字 + 环形图 + 柱状图
   ============================================================ */

/* 三个超大数字：用 1px 网格线拼成一块，而不是三张各自带边框的卡 */
.hero-kpis{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:var(--r);overflow:hidden;margin-bottom:12px}
.hero-kpis .hk{background:var(--surface);padding:14px 11px;min-width:0}
.hero-kpis .hk .n{font-size:26px;font-weight:800;color:var(--ink);letter-spacing:-1.2px;
  line-height:1.15;font-variant-numeric:tabular-nums;white-space:nowrap}
.hero-kpis .hk .n em{font-style:normal;font-size:12px;font-weight:700;color:var(--ink3);
  margin-left:2px;letter-spacing:0}
.hero-kpis .hk .n.n-hot{color:var(--accent)}
.hero-kpis .hk .l{font-size:10.5px;color:var(--ink2);margin-top:5px;letter-spacing:.2px}

.chart-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  padding:15px;margin-bottom:12px;box-shadow:var(--shadow)}
.cc-hd{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:13px}
.cc-hd b{font-size:13.5px;font-weight:700;color:var(--ink);display:flex;align-items:center;gap:6px}
.cc-hd span{font-size:11px;color:var(--ink3);font-variant-numeric:tabular-nums}

/* 环形图 */
.ring-wrap{display:flex;align-items:center;gap:16px}
.ring{position:relative;width:106px;height:106px;flex:0 0 106px}
.ring svg{width:106px;height:106px;transform:rotate(-90deg)}
.ring-c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:2px}
.ring-c b{font-size:21px;font-weight:800;color:var(--ink);letter-spacing:-.8px;
  line-height:1;font-variant-numeric:tabular-nums}
.ring-c span{font-size:10.5px;color:var(--ink3)}
.legend{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.lg-row{display:flex;align-items:center;gap:7px;font-size:12px;min-width:0;width:100%;
  background:none;border:0;padding:6px 0;font-family:inherit;text-align:left;cursor:pointer;
  color:inherit}
.lg-row i{width:8px;height:8px;border-radius:3px;flex:0 0 8px}
.lg-row .nm{color:var(--ink2);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lg-row .vv{color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}
/* 注意：修饰类一律带 n- 前缀（n-hot / n-warn）。裸 .hot/.warn 会被上面的通用 .warn 规则
   命中，把「数字」变成一块带边框的琥珀色方块（2026-09-28 实拍踩到过）。 */
.lg-row .vv.n-hot{color:var(--accent)}
.lg-row .vv.n-warn{color:var(--amber)}

/* 柱状图：柱高由 JS 给像素值，容器不设固定高，避免标签把高度撑破 */
.bars{display:flex;align-items:flex-end;gap:7px}
.bars .bar{flex:1;min-width:0;display:flex;flex-direction:column;
  align-items:center;justify-content:flex-end;gap:5px}
.bars .bar .bv{font-size:11px;font-weight:700;color:var(--ink2);font-variant-numeric:tabular-nums}
.bars .bar .col{width:100%;max-width:34px;border-radius:5px 5px 3px 3px;background:var(--accent);
  min-height:3px}
.bars .bar .col.dim{background:var(--surface2);border:1px solid var(--line)}
.bars .bar .bl{font-size:10.5px;color:var(--ink3);white-space:nowrap}

/* 知识库规模：小字排版，压在卡片底部 */
.kv-list{display:flex;flex-direction:column}
.kv-list .r{display:flex;justify-content:space-between;gap:12px;padding:8px 0;
  border-bottom:1px solid var(--line2);font-size:12.5px}
.kv-list .r:last-child{border-bottom:0}
.kv-list .r .k{color:var(--ink2)}
.kv-list .r .v{color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}

/* ---------- 时间范围标签（点开弹出日期面板） ---------- */
.rangewrap{position:relative;display:inline-block}
.rangetag{display:inline-flex;align-items:center;gap:5px;background:var(--surface2);
  border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:11.5px;
  font-weight:600;color:var(--ink2);font-family:inherit;cursor:pointer;letter-spacing:.2px}
.rangetag:active{background:var(--line)}
.rangetag .cv{width:9px;height:9px;fill:none;stroke:currentColor;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}

/* ---------- 首页顶部「今日待办」速览条 ---------- */
.strip{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px}
.strip .sc{background:var(--surface);border:1px solid var(--line);border-radius:12px;
  padding:11px 10px;text-align:left;font-family:inherit;cursor:pointer;color:inherit;min-width:0;
  box-shadow:var(--shadow)}
.strip .sc:active{background:var(--surface2)}
.strip .sc .n{font-size:20px;font-weight:800;color:var(--ink);letter-spacing:-.7px;
  line-height:1.15;font-variant-numeric:tabular-nums;white-space:nowrap}
.strip .sc .n.n-hot{color:var(--accent)}
.strip .sc .n.n-warn{color:var(--amber)}
.strip .sc .l{font-size:10.5px;color:var(--ink3);margin-top:3px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}

/* ---------- 底部导航 ---------- */
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;
  background:rgba(255,255,255,.94);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
.tabbar button{flex:1;background:none;border:0;padding:9px 0 8px;font-size:11px;
  color:var(--ink3);font-family:inherit;display:flex;flex-direction:column;align-items:center;gap:3px;
  cursor:pointer;position:relative}
.tabbar button .ic{width:24px;height:24px;display:flex;align-items:center;justify-content:center}
.tabbar button .ic svg{width:22px;height:22px;stroke:currentColor;fill:none;
  stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.tabbar button.on{color:var(--accent)}
.tabbar button.on::before{content:"";position:absolute;top:0;left:50%;transform:translateX(-50%);
  width:22px;height:2px;border-radius:0 0 2px 2px;background:var(--accent)}
.fab{position:fixed;right:16px;bottom:calc(76px + env(safe-area-inset-bottom));z-index:31;
  width:56px;height:56px;border-radius:18px;border:0;background:var(--accent);color:var(--on-accent);
  box-shadow:0 10px 24px rgba(255,107,61,.34);display:flex;align-items:center;justify-content:center;
  cursor:pointer}
.fab svg{width:26px;height:26px;stroke:currentColor;fill:none;stroke-width:1.7;
  stroke-linecap:round;stroke-linejoin:round}

/* ---------- 右下角智能体抽屉 ---------- */
.drawer{position:fixed;inset:0;z-index:40;background:rgba(23,26,32,.42);display:flex;align-items:flex-end}
.drawer .inner{background:var(--surface);width:100%;max-height:86vh;border-radius:20px 20px 0 0;
  padding:18px 14px calc(18px + env(safe-area-inset-bottom));display:flex;flex-direction:column}
.drawer .dh{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.drawer .dh b{font-size:16px;color:var(--ink)}
.drawer .dh button{background:var(--surface2);border:1px solid var(--line);font-size:17px;
  color:var(--ink2);width:30px;height:30px;border-radius:9px;line-height:1}
.chatlog{flex:1;overflow-y:auto;background:var(--bg);border:1px solid var(--line);border-radius:12px;
  padding:11px;min-height:120px;max-height:52vh}
.ev{font-size:13px;padding:9px 11px;border-radius:11px;background:var(--surface);margin-bottom:8px;
  border:1px solid var(--line);white-space:pre-wrap;word-break:break-word;color:var(--ink)}
.ev.user{background:var(--accent-soft);border-color:var(--accent-soft);color:var(--accent-d);font-weight:600}
.ev.done{border-color:var(--ok-line);background:var(--ok-soft)}
.ev.err{border-color:var(--err-line);background:var(--err-soft);color:var(--err)}
.ev .et{font-size:11px;font-weight:600;color:var(--ink2);margin-bottom:4px}
.quickrow{display:flex;gap:7px;overflow-x:auto;padding:2px 0 10px;-webkit-overflow-scrolling:touch}
.quickrow button{white-space:nowrap;padding:8px 13px;border-radius:999px;border:1px solid var(--line);
  background:var(--surface);font-size:12.5px;color:var(--ink2);font-family:inherit;cursor:pointer}

/* ============================================================
   弹出交互族：确认弹窗 / 弹出菜单 / 气泡提示 / 日期面板 / 卡片弹详情
   ------------------------------------------------------------
   公共约定：
     · 遮罩 #popmask 只有一个，谁打开谁负责关；点遮罩 = 原路收回。
     · 浮动层用 position:fixed + JS 算坐标，配合 transform-origin 从触发点「长出来」。
     · 关掉动画（prefers-reduced-motion）时只保留状态切换，不做位移。
   ============================================================ */
#popmask{position:fixed;inset:0;z-index:80;background:rgba(23,26,32,.42)}
#popmask.plain{background:transparent}

/* ---------- ③ 确认弹窗 / ② 卡片弹出的详情面板 ---------- */
.dlg{position:fixed;z-index:81;left:50%;top:50%;width:calc(100% - 56px);max-width:340px;
  transform:translate(-50%,-50%);background:var(--surface);border:1px solid var(--line);
  border-radius:16px;box-shadow:var(--pop-shadow);padding:20px 18px 16px;
  transform-origin:var(--org,50% 50%);animation:dlgIn .24s cubic-bezier(.2,.9,.3,1.15)}
.dlg.big{max-width:420px;max-height:82vh;overflow-y:auto}
@keyframes dlgIn{
  0%{opacity:0;transform:translate(-50%,-50%) scale(.88)}
  62%{opacity:1;transform:translate(-50%,-50%) scale(1.025)}
  100%{opacity:1;transform:translate(-50%,-50%) scale(1)}
}
.dlg .di{width:44px;height:44px;border-radius:14px;background:var(--accent-soft);
  color:var(--accent);display:flex;align-items:center;justify-content:center;margin:0 auto 12px}
.dlg .di svg{width:24px;height:24px;stroke:currentColor;fill:none;stroke-width:1.9;
  stroke-linecap:round;stroke-linejoin:round}
.dlg.danger .di{background:var(--err-soft);color:var(--err)}
.dlg h3{margin:0;font-size:16px;font-weight:700;color:var(--ink);text-align:center;letter-spacing:.2px}
/* 正文类名用 .dtx 而不是 .dp：.dp 是日期面板的浮动容器（position:fixed），
   一旦同名，弹窗正文会被那条规则拽成固定定位的白色方块、把按钮整个盖住（实拍踩过） */
.dlg .dtx{font-size:13px;color:var(--ink2);line-height:1.65;margin:9px 0 0;text-align:center}
.dlg .dacts{display:flex;gap:9px;margin-top:18px}
.dlg .dacts button{flex:1;min-height:44px;border-radius:11px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink);font-size:14.5px;font-weight:600;
  font-family:inherit;cursor:pointer}
.dlg .dacts button.cancel{color:var(--ink2)}
.dlg .dacts button.ok{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.dlg .dacts button.ok.danger{background:var(--err);border-color:var(--err);color:#fff}
.dlg .dacts button:active{transform:scale(.98)}
/* 处理中 / 已完成：原地切换，弹窗不重开 */
.dlg .dstate{display:flex;flex-direction:column;align-items:center;gap:10px;padding:6px 0 4px}
.dlg .dstate .spin{width:26px;height:26px;border-radius:50%;border:2.5px solid var(--line);
  border-top-color:var(--accent);animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.dlg .dstate .tick{width:46px;height:46px;border-radius:50%;background:var(--ok-soft);
  display:flex;align-items:center;justify-content:center;animation:tickIn .3s cubic-bezier(.2,.9,.3,1.4)}
.dlg .dstate .tick svg{width:26px;height:26px;stroke:var(--ok);fill:none;stroke-width:2.4;
  stroke-linecap:round;stroke-linejoin:round}
@keyframes tickIn{0%{transform:scale(.5);opacity:0}100%{transform:scale(1);opacity:1}}
.dlg .dstate .dtext{font-size:13.5px;color:var(--ink2);text-align:center}

/* ---------- ① 按钮弹出菜单 ---------- */
.pop{position:fixed;z-index:81;min-width:172px;background:var(--surface);
  border:1px solid var(--line);border-radius:13px;box-shadow:var(--pop-shadow);
  padding:5px;transform-origin:var(--org,50% 0%);animation:popIn .16s cubic-bezier(.2,.9,.3,1.1)}
@keyframes popIn{0%{opacity:0;transform:scale(.86)}100%{opacity:1;transform:scale(1)}}
.pop .ph{font-size:10.5px;font-weight:700;color:var(--ink3);letter-spacing:.4px;
  padding:7px 10px 5px}
.pop button{display:flex;align-items:center;gap:9px;width:100%;text-align:left;background:none;
  border:0;padding:10px;border-radius:9px;font-size:13.5px;font-weight:600;color:var(--ink);
  font-family:inherit;cursor:pointer}
.pop button:active{background:var(--surface2)}
.pop button.danger{color:var(--err)}
.pop button.mute{color:var(--ink2)}
.pop button svg{width:17px;height:17px;flex:0 0 17px;stroke:currentColor;fill:none;
  stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}

/* ---------- ④ 气泡提示（带指向箭头，数秒自动收回） ---------- */
.tipbox{position:fixed;z-index:82;max-width:236px;background:#2A2F3A;color:#fff;
  border-radius:10px;padding:9px 12px;font-size:12px;line-height:1.6;
  box-shadow:0 10px 26px rgba(23,26,32,.3);animation:tipIn .16s ease}
@keyframes tipIn{0%{opacity:0;transform:translateY(4px)}100%{opacity:1;transform:none}}
.tipbox::after{content:"";position:absolute;left:var(--arrow,50%);bottom:-4px;width:9px;height:9px;
  margin-left:-4.5px;background:#2A2F3A;transform:rotate(45deg);border-radius:1px}
.tipbox.below::after{bottom:auto;top:-4px}

/* ---------- ⑧ 日期面板（从标签下方展开） ---------- */
.dp{position:fixed;z-index:81;width:280px;background:var(--surface);border:1px solid var(--line);
  border-radius:14px;box-shadow:var(--pop-shadow);padding:12px;
  transform-origin:var(--org,50% 0%);animation:popIn .16s cubic-bezier(.2,.9,.3,1.1)}
.dp .quick{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.dp .quick button{padding:7px 11px;border-radius:999px;border:1px solid var(--line);
  background:var(--surface);font-size:12px;font-weight:600;color:var(--ink2);
  font-family:inherit;cursor:pointer}
.dp .quick button.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.dp .mh{display:flex;align-items:center;justify-content:space-between;margin:2px 0 8px}
.dp .mh b{font-size:13px;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}
.dp .mh button{width:30px;height:30px;border-radius:9px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink2);font-size:14px;line-height:1;cursor:pointer;
  font-family:inherit}
.dp .mh button:disabled{opacity:.35}
.dp .grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
.dp .grid .wd{font-size:10px;color:var(--ink3);text-align:center;padding:3px 0}
.dp .grid button{aspect-ratio:1/1;border:0;background:none;border-radius:8px;font-size:12.5px;
  color:var(--ink);font-family:inherit;cursor:pointer;font-variant-numeric:tabular-nums;
  display:flex;align-items:center;justify-content:center}
.dp .grid button:active{background:var(--surface2)}
.dp .grid button.out{color:var(--ink3);opacity:.5}
.dp .grid button.on{background:var(--accent);color:var(--on-accent);font-weight:700}
.dp .grid button.today{box-shadow:inset 0 0 0 1px var(--line)}

/* ---------- 轻提示（替代原生 alert，不打断操作） ---------- */
#toast{position:fixed;left:50%;bottom:calc(150px + env(safe-area-inset-bottom));z-index:90;
  transform:translateX(-50%);max-width:calc(100% - 48px);background:#2A2F3A;color:#fff;
  border-radius:11px;padding:11px 15px;font-size:13px;line-height:1.55;
  box-shadow:0 10px 26px rgba(23,26,32,.3);animation:tipIn .18s ease;text-align:center}
#toast.err{background:var(--err)}

@media (prefers-reduced-motion:reduce){
  .dlg,.pop,.dp,.tipbox,#toast{animation:none}
  .dlg .dstate .spin{animation:none}
}
</style>
</head>
<body>

<div class="topbar">
  <div class="brand"><span class="mark">鸭</span>鸭先知 · 员工端</div>
  <div class="who">
    <span id="who"></span>
    <button id="btn-logout" class="hide">退出</button>
  </div>
</div>

<div class="wrap">

  <!-- ---------- 登录 ---------- -->
  <div id="login" class="card hide">
    <h2>工作人员登录</h2>
     <p class="sub">用园区管理员账号登录。登进来的是降权令牌：能核销、能办卡，
       但进不了商家后台的管理接口。</p>
    <label>账号</label><input id="u" autocomplete="username" placeholder="账号">
    <label>密码</label><input id="p" type="password" autocomplete="current-password" placeholder="密码">
    <button class="btn btn-p" id="btn-login">登录</button>
    <div class="msg" id="login-msg"></div>
  </div>

  <!-- ---------- 首页：扫码核销 + 办卡管理 ---------- -->
  <div id="tab-home" class="hide">
    <button class="scan-hero" id="btn-scan" type="button">
      <span class="scan-ico">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor"
             stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 8.5V6a2 2 0 0 1 2-2h2.5M15.5 4H18a2 2 0 0 1 2 2v2.5M20 15.5V18a2 2 0 0 1-2 2h-2.5M8.5 20H6a2 2 0 0 1-2-2v-2.5"/>
          <rect x="8.5" y="8.5" width="7" height="7" rx="1.5"/>
        </svg>
      </span>
      <span class="scan-txt">
        <span class="scan-t">扫一扫 用户券码</span>
        <span class="scan-s">对准用户「我的游园码」里的二维码</span>
      </span>
    </button>

    <!-- 今日待办速览：数据复用员工端已有的 /dashboard 只读接口，点了直接跳对应页 -->
    <div class="strip" id="home-strip"></div>

    <div class="card">
      <h2>手动输码核销</h2>
      <p class="sub">扫不了时直接输券码，8 位，大小写都认。扫一张核销一张。</p>
      <input id="code" placeholder="券码（8 位）" autocomplete="off" inputmode="text">
      <button class="btn btn-p" id="btn-query">查询</button>
      <div id="qr"></div>
    </div>

    <div class="card">
      <h2>办卡管理</h2>
      <div class="seg" id="pa-seg">
        <button data-sub="orders" class="on">充值账单</button>
        <button data-sub="cons">消费账单</button>
        <button data-sub="issue">现场开卡</button>
      </div>

      <!-- 充值账单：与商家端同一组统计口径 + 状态筛选 + 搜索 + 分页 -->
      <div id="pa-orders">
        <div id="order-stats" class="kpis" style="margin-bottom:12px"></div>
        <div class="seg" id="order-st">
          <button data-st="" class="on">全部</button>
          <button data-st="paid">已支付</button>
          <button data-st="refunded">已退卡</button>
          <button data-st="pending">待支付</button>
        </div>
        <input id="order-kw" placeholder="订单号 / 卡名 / 用户昵称">
        <button class="btn btn-g" id="btn-order-search">搜索</button>
        <div id="order-list" style="margin-top:12px"></div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:6px">
          <span class="hint" id="order-total" style="margin:0"></span>
          <span>
            <button class="mini mini-g" id="order-prev">上一页</button>
            <button class="mini mini-g" id="order-next">下一页</button>
          </span>
        </div>
        <div class="warn">退卡退款、调币属于财务动作，只在商家 PC 端开放；这里只查账、不改动金额。</div>
      </div>

      <!-- 消费账单：与商家端同一份券包数据（含逐张券的核销 / 退款） -->
      <div id="pa-cons" class="hide">
        <div class="seg" id="cons-st">
          <button data-st="" class="on">全部</button>
          <button data-st="unused">未核销</button>
          <button data-st="used">已核销</button>
          <button data-st="refunded">已退款</button>
        </div>
        <button class="btn btn-g" id="btn-cons-refresh">刷新列表</button>
        <div id="cons-list" style="margin-top:12px"></div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:6px">
          <span class="hint" id="cons-total" style="margin:0"></span>
          <span>
            <button class="mini mini-g" id="cons-prev">上一页</button>
            <button class="mini mini-g" id="cons-next">下一页</button>
          </span>
        </div>
        <div class="warn">扫券码核销用上方「扫一扫 / 手动输码」；
          这里按单处理：一次买多张券的给「整单核销 + 单独核销」，已核销的券也能退款。</div>
      </div>

      <div id="pa-issue" class="hide">
        <label>游客手机号（需已在小程序登录过）</label>
        <input id="issue-phone" type="tel" inputmode="numeric" maxlength="11" placeholder="11 位手机号">
        <label>选择卡</label>
        <select id="issue-card"></select>
        <label>柜台实收（元，留空按卡原价）</label>
        <input id="issue-paid" type="number" inputmode="decimal" placeholder="例如 99">
        <button class="btn btn-w" id="btn-issue">开卡并发币</button>
        <div class="msg" id="issue-msg"></div>
      </div>
    </div>
  </div>

  <!-- ---------- 仪表盘 ---------- -->
  <div id="tab-dash" class="hide">
    <h2 class="page-hd">今日运营</h2>
    <div id="dash-body"><p class="hint" style="margin:0">加载中…</p></div>
  </div>

  <!-- ---------- 失物招领 ---------- -->
  <div id="tab-lost" class="hide">
    <div class="card">
      <h2>失物招领</h2>
      <div class="seg" id="lost-seg">
        <button data-st="" class="on">全部</button>
        <button data-st="lost">待认领</button>
        <button data-st="claimed">已认领</button>
      </div>
      <input id="lost-kw" placeholder="失物名称关键词">
      <button class="btn btn-g" id="btn-lost-search">刷新列表</button>
      <div id="lost-list" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h2>登记拾获物</h2>
      <label>名称</label><input id="lost-name" placeholder="例如：粉色水壶">
      <label>照片（可选，AI 会自动补全名称）</label>
      <input id="lost-photo" type="file" accept="image/*" capture="environment">
      <button class="btn btn-p" id="btn-lost-add">登记</button>
      <div class="msg" id="lost-msg"></div>
    </div>
  </div>

  <!-- ---------- 内容管理 ---------- -->
  <div id="tab-content" class="hide">
    <div class="card">
      <h2>内容管理</h2>
      <div class="seg" id="ct-seg">
        <button data-sub="notices" class="on">公告 / 热点</button>
        <button data-sub="feedback">游客反馈</button>
      </div>
      <div id="ct-notices">
        <div id="notice-list"></div>
        <label>新增</label>
        <select id="n-kind"><option value="announcement">园区公告</option><option value="hot">今日热点</option></select>
        <input id="n-title" placeholder="标题" style="margin-top:8px">
        <input id="n-content" placeholder="正文（可选）" style="margin-top:8px">
        <button class="btn btn-p" id="btn-notice-add">发布</button>
        <div class="msg" id="notice-msg"></div>
      </div>
      <div id="ct-feedback" class="hide"><div id="fb-list"></div></div>
    </div>
  </div>

</div>

<!-- ---------- 底部导航 ---------- -->
<nav class="tabbar" id="tabbar">
  <button data-tab="home" class="on"><span class="ic"><svg viewBox="0 0 24 24"><path d="M4 10.6 12 4l8 6.6V20a1 1 0 0 1-1 1h-4.2v-6.2H9.2V21H5a1 1 0 0 1-1-1z"/></svg></span>首页</button>
  <button data-tab="dash"><span class="ic"><svg viewBox="0 0 24 24"><path d="M5.5 20V11M12 20V4.5M18.5 20v-6"/></svg></span>仪表盘</button>
  <button data-tab="lost"><span class="ic"><svg viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/></svg></span>失物</button>
  <button data-tab="content"><span class="ic"><svg viewBox="0 0 24 24"><path d="M4.5 10v4a1 1 0 0 0 1 1h2.7l4.8 3.6V5.4L8.2 9H5.5a1 1 0 0 0-1 1z"/><path d="M17.5 8.2a5.2 5.2 0 0 1 0 7.6"/></svg></span>内容</button>
</nav>

<button class="fab" id="fab" type="button" aria-label="经营小助手"><svg viewBox="0 0 24 24"><path d="M20.5 12a8.5 8.5 0 0 1-12.7 7.3L3.5 20.5l1.2-4.3A8.5 8.5 0 1 1 20.5 12z"/></svg></button>

<div class="drawer hide" id="agent">
  <div class="inner">
    <div class="dh">
      <b>经营小助手（只读）</b>
      <button id="agent-close">×</button>
    </div>
    <div class="quickrow">
      <button data-q="今天的经营情况怎么样">今天经营情况</button>
      <button data-q="现在有哪些待认领的失物">待认领失物</button>
      <button data-q="今天有哪些消费单还没核销">未核销消费单</button>
      <button data-q="现在挂着哪些公告和热点">当前公告热点</button>
    </div>
    <div class="chatlog" id="chatlog"></div>
    <input id="agent-input" placeholder="问点什么，例如：昨天有多少人核销过" style="margin-top:10px">
    <button class="btn btn-p" id="agent-send">发送</button>
    <div class="hint">员工端只能用查询类能力；退款、调币、删除这些危险操作不对员工开放。</div>
  </div>
</div>

<!-- ---------- 扫码浮层（全屏摄像头） ---------- -->
<div id="scan" class="hide">
  <video id="scan-video" playsinline muted></video>
  <div class="scan-mask"><div class="scan-frame"></div></div>
  <div class="scan-tip" id="scan-tip">正在打开摄像头…</div>
  <button class="scan-close" id="scan-close" type="button">取消</button>
</div>

<!-- ---------- 失物照片全屏预览 ---------- -->
<div id="imgbox" class="hide">
  <img id="imgbox-img" alt="失物照片">
  <button id="imgbox-close" type="button">关闭</button>
</div>

<!-- ---------- 弹出交互族：共用一层遮罩 + 一个浮层容器 ----------
     为什么只留一个容器：员工端同一时刻只可能弹一样东西（确认框 / 菜单 / 气泡 / 日期面板 /
     卡片详情），共用一个容器就不可能出现「两层叠着、关不掉上面那层」的状态。
     点遮罩 = 原路收回；谁打开谁负责在关闭时清干净。 -->
<div id="popmask" class="hide"></div>
<div id="poplayer" class="hide"></div>
<div id="toast" class="hide"></div>

<!-- ---------- 消费账单「单独核销」弹窗：逐张券一行，逐张 / 批量 核销与退款 ---------- -->
<div id="passbox" class="hide">
  <div class="pb-inner">
    <div class="pb-hd">
      <b>单独核销 / 退款</b>
      <button id="pb-close" type="button">×</button>
    </div>
    <div id="pb-sub" class="hint" style="margin:0 0 10px"></div>
    <div class="batch">
      <label><input type="checkbox" id="pb-all">全选</label>
      <span class="hint" style="margin:0">已勾 <b id="pb-n">0</b> 张</span>
      <span style="flex:1"></span>
      <button class="mini mini-p" id="pb-verify">批量核销</button>
      <button class="mini mini-r" id="pb-refund">批量退款</button>
    </div>
    <div id="pb-list"></div>
    <div class="msg" id="pb-msg"></div>
    <div class="warn">一券一项一次，每张券单独核销；<b>已核销的券也可以退款</b>
      （游客可能刚上去就下来），退掉的币按原扣币比例退回用户的卡，不可撤销。</div>
  </div>
</div>

<script>
// ============================================================
// 基础工具
// ============================================================
var $ = function (id) { return document.getElementById(id); };
var CODE = (new URLSearchParams(location.search).get('c') || '').trim().toUpperCase();
var API = '/staff/api';

function tk() { return localStorage.getItem('staff_token') || ''; }
function show(id) { $(id).classList.remove('hide'); }
function hide(id) { $(id).classList.add('hide'); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function setMsg(el, text, cls) {
  el.className = 'msg' + (cls ? ' ' + cls : '');
  el.textContent = text || '';
}

function req(path, opts) {
  opts = opts || {};
  var h = { 'Content-Type': 'application/json' };
  if (tk()) { h['Authorization'] = 'Bearer ' + tk(); }
  return fetch(path, { method: opts.method || 'GET', headers: h, body: opts.body })
    .then(function (r) {
      return r.text().then(function (t) {
        var j = {};
        try { j = t ? JSON.parse(t) : {}; } catch (e) { j = {}; }
        return { ok: r.ok, status: r.status, body: j };
      });
    });
}
function errText(res) {
  var d = res.body && res.body.detail;
  if (typeof d === 'string') return d;
  if (d && d.msg) return d.msg;
  return (res.body && res.body.msg) || ('请求失败（' + res.status + '）');
}
function needLogin(res) {
  if (res.status === 401 || res.status === 403) { toLogin(); return true; }
  return false;
}
function toLogin() {
  hide('tab-home'); hide('tab-dash'); hide('tab-lost'); hide('tab-content');
  hide('tabbar'); hide('fab');
  hide('btn-logout');
  show('login');
}

/* ============================================================
   弹出交互族（共用一层遮罩 + 一个浮层容器）
   ------------------------------------------------------------
   为什么不用浏览器原生 confirm/alert：它们在手机上长得就像"网页弹框"，
   和 App 的质感完全不搭；更关键的是原生框做不了"确认后原地切换成完成态"，
   只能关掉再弹一个。这里给 4 种最常用的：
     ③ 确认弹窗 ① 按钮弹出菜单 ④ 气泡提示 ⑧ 日期面板
   外加失物卡片的 ② 卡片弹详情（从原位放大成居中面板）。
   ============================================================ */
var ICON_ASK = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>'
  + '<path d="M9.6 9.2a2.5 2.5 0 1 1 3.2 2.4c-.6.2-.8.7-.8 1.3v.4"/>'
  + '<path d="M12 16.6h.01"/></svg>';
var ICON_TICK = '<svg viewBox="0 0 24 24"><path d="m5 12.6 4.6 4.6L19 7.6"/></svg>';
var ICON_ERR = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>'
  + '<path d="M12 7.8v5"/><path d="M12 16.2h.01"/></svg>';
var ICON_WARN = '<svg viewBox="0 0 24 24">'
  + '<path d="M10.3 4.4 2.9 17.6A1.8 1.8 0 0 0 4.5 20.3h15a1.8 1.8 0 0 0 1.6-2.7L13.7 4.4a1.8 1.8 0 0 0-3.4 0z"/>'
  + '<path d="M12 9.6v4"/><path d="M12 16.7h.01"/></svg>';
var ICON_EYE = '<svg viewBox="0 0 24 24"><path d="M2.6 12S6 5.8 12 5.8 21.4 12 21.4 12 18 18.2 12 18.2 2.6 12 2.6 12z"/>'
  + '<circle cx="12" cy="12" r="3"/></svg>';
var ICON_CHECK = '<svg viewBox="0 0 24 24"><path d="M20 6.5 9.6 17 4 11.4"/></svg>';
var ICON_BACK = '<svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.6-5.9"/>'
  + '<path d="M4 4.5V10h5.5"/></svg>';
var ICON_REFUND = '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7"/>'
  + '<path d="M21 4v5h-5"/></svg>';
var ICON_BOX = '<svg viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/>'
  + '<path d="m4 7.5 8 4.5 8-4.5M12 12v9"/></svg>';

var POP_CLOSE = null;    // 关闭时要跑的清理逻辑（谁打开谁注册）
var POP_KIND = '';       // 当前浮层类型，给"自动收回"之类的定时器做判据

/* 打开浮层：遮罩 + 容器一起显示。onClose 在每次关闭时跑一次。 */
function openLayer(html, onClose, kind) {
  $('poplayer').innerHTML = html;
  show('popmask'); show('poplayer');
  POP_CLOSE = onClose || null;
  POP_KIND = kind || 'generic';
}
/* 只换内容、不动遮罩：弹窗原地切状态、日期面板翻月都用它 */
function repaintLayer(html) { $('poplayer').innerHTML = html; }

function closePop() {
  hide('poplayer'); hide('popmask');
  $('poplayer').innerHTML = '';
  $('poplayer').onclick = null;
  var f = POP_CLOSE; POP_CLOSE = null; POP_KIND = '';
  if (f) { f(); }
}
$('popmask').onclick = closePop;

/* 把浮层摆到锚点旁边：默认贴下方，放不下就翻到上方，并把动画原点对到锚点中心 */
function placeFloat(el, anchor, gap) {
  var r = anchor.getBoundingClientRect();
  var w = el.offsetWidth, h = el.offsetHeight;
  var left = Math.min(Math.max(10, r.right - w), window.innerWidth - w - 10);
  var top = r.bottom + (gap || 6);
  var oy = '0px';
  if (top + h > window.innerHeight - 12) {
    top = Math.max(12, r.top - h - (gap || 6));
    oy = h + 'px';
  }
  var ox = Math.min(Math.max(10, r.left + r.width / 2 - left), w - 10);
  el.style.left = left + 'px';
  el.style.top = top + 'px';
  el.style.setProperty('--org', ox + 'px ' + oy);
}

/* ---------- ③ 确认弹窗：轻微回弹入场，确认后**原地**切成完成态 ---------- */
function confirmDialog(opt) {
  opt = opt || {};
  var danger = !!opt.danger;
  openLayer(
    '<div class="dlg' + (danger ? ' danger' : '') + '">'
    + '<div class="di">' + (opt.icon || (danger ? ICON_WARN : ICON_ASK)) + '</div>'
    + '<h3>' + esc(opt.title || '确认操作？') + '</h3>'
    + (opt.body ? '<p class="dtx">' + esc(opt.body) + '</p>' : '')
    + '<div class="dacts">'
    + '<button class="cancel" data-dlg="no">' + esc(opt.cancel || '取消') + '</button>'
    + '<button class="ok' + (danger ? ' danger' : '') + '" data-dlg="yes">'
    + esc(opt.ok || '确认') + '</button></div></div>', null, 'dlg');
  return new Promise(function (resolve) {
    $('poplayer').onclick = function (e) {
      var b = e.target.closest ? e.target.closest('[data-dlg]') : null;
      if (!b) { return; }
      $('poplayer').onclick = null;
      if (b.getAttribute('data-dlg') !== 'yes') { closePop(); resolve(false); return; }
      dialogBusy();          // 原地切成「处理中」——弹窗不重开，视线不跳
      resolve(true);
    };
  });
}
function dialogBusy(text) {
  repaintLayer('<div class="dlg"><div class="dstate"><div class="spin"></div>'
    + '<div class="dtext">' + esc(text || '处理中…') + '</div></div></div>');
}
function dialogDone(text, cb) {
  repaintLayer('<div class="dlg"><div class="dstate">'
    + '<div class="tick">' + ICON_TICK + '</div>'
    + '<div class="dtext">' + esc(text || '已完成') + '</div></div></div>');
  setTimeout(function () { closePop(); if (cb) { cb(); } }, 900);
}
function dialogFail(text) {
  openLayer('<div class="dlg danger"><div class="di">' + ICON_ERR + '</div>'
    + '<h3>没能完成</h3><p class="dtx">' + esc(text || '请稍后重试') + '</p>'
    + '<div class="dacts"><button class="ok danger" data-dlg="no">知道了</button></div></div>',
    null, 'dlg');
  $('poplayer').onclick = function (e) {
    if (e.target.closest && e.target.closest('[data-dlg]')) {
      $('poplayer').onclick = null; closePop();
    }
  };
}

/* ---------- 通用轻提示：替代原生 alert，不打断当前操作 ---------- */
var TOAST_T = null;
function toast(msg, kind) {
  var el = $('toast');
  el.textContent = String(msg);
  el.className = (kind === 'err') ? 'err' : '';
  show('toast');
  if (TOAST_T) { clearTimeout(TOAST_T); }
  // 字多的（比如"当前不是安全上下文…"那段）要多留一会儿，否则读不完
  TOAST_T = setTimeout(function () { hide('toast'); },
    Math.min(6000, 1800 + String(msg).length * 70));
}

/* ---------- ① 按钮弹出菜单：以按钮为原点展开，点外部原路收回 ---------- */
function openPop(anchor, items, onPick, head) {
  var inner = (head ? '<div class="ph">' + esc(head) + '</div>' : '')
    + items.map(function (it) {
        return '<button data-k="' + esc(it.k) + '"'
          + (it.danger ? ' class="danger"' : (it.mute ? ' class="mute"' : '')) + '>'
          + (it.icon || '') + '<span>' + esc(it.label) + '</span></button>';
      }).join('');
  openLayer('<div class="pop">' + inner + '</div>', null, 'pop');
  placeFloat($('poplayer').firstChild, anchor, 6);
  $('poplayer').onclick = function (e) {
    var b = e.target.closest ? e.target.closest('[data-k]') : null;
    if (!b) { return; }
    $('poplayer').onclick = null;
    var k = b.getAttribute('data-k');
    closePop();
    onPick(k);
  };
}

/* ---------- ④ 气泡提示：带指向箭头，数秒自动收回 ---------- */
var TIP_T = null;
function showTip(anchor, text) {
  var r = anchor.getBoundingClientRect();
  openLayer('<div class="tipbox">' + esc(text) + '</div>', null, 'tip');
  var box = $('poplayer').firstChild;
  var cx = r.left + r.width / 2;
  var w = box.offsetWidth, h = box.offsetHeight;
  var left = Math.min(Math.max(10, cx - w / 2), window.innerWidth - w - 10);
  var top = r.top - h - 9;
  if (top < 8) { top = r.bottom + 9; box.classList.add('below'); }
  box.style.left = left + 'px';
  box.style.top = top + 'px';
  // 气泡贴边时不能简单取 50%，否则箭头指不到图标
  box.style.setProperty('--arrow', (cx - left) + 'px');
  if (TIP_T) { clearTimeout(TIP_T); }
  TIP_T = setTimeout(function () {
    TIP_T = null;
    if (POP_KIND === 'tip') { closePop(); }   // 期间用户开了别的浮层就别乱关
  }, 4200);
}

/* ---------- ② 卡片弹出详情：从卡片原位放大成居中面板 ---------- */
function openFromCard(cardEl, html) {
  openLayer('<div class="dlg big" id="carddlg">' + html + '</div>', null, 'card');
  var dlg = $('carddlg');
  if (!dlg || !cardEl) { return; }
  var a = cardEl.getBoundingClientRect(), d = dlg.getBoundingClientRect();
  dlg.style.setProperty('--org',
    (a.left + a.width / 2 - d.left) + 'px ' + (a.top + a.height / 2 - d.top) + 'px');
}

/* ---------- ⑧ 日期面板：点标签从下方弹出，选完收回并更新标签文字 ---------- */
var DASH_DAYS = 7;            // 仪表盘统计窗口（天）→ /dashboard?days=N
var DP_Y = 0, DP_M = 0, DP_EL = null;

function ymd(dt) {
  var m = dt.getMonth() + 1, d = dt.getDate();
  return dt.getFullYear() + '-' + (m < 10 ? '0' : '') + m + '-' + (d < 10 ? '0' : '') + d;
}
function midNight(dt) { var c = new Date(dt.getTime()); c.setHours(0, 0, 0, 0); return c; }

function openDatePanel(anchor) {
  DP_EL = anchor;
  var t = midNight(new Date());
  DP_Y = t.getFullYear(); DP_M = t.getMonth();
  openLayer('<div class="dp"></div>', null, 'date');
  paintDatePanel();
}
function paintDatePanel() {
  var today = midNight(new Date());
  var startSel = new Date(today.getTime() - (DASH_DAYS - 1) * 86400000);
  var lead = (new Date(DP_Y, DP_M, 1).getDay() + 6) % 7;      // 周一当第一列
  var total = new Date(DP_Y, DP_M + 1, 0).getDate();
  var atNow = (DP_Y === today.getFullYear() && DP_M === today.getMonth());
  var cells = '';
  for (var i = 0; i < lead; i++) { cells += '<span></span>'; }
  for (var d = 1; d <= total; d++) {
    var dt = new Date(DP_Y, DP_M, d);
    var cls = [];
    if (ymd(dt) === ymd(startSel)) { cls.push('on'); }
    if (ymd(dt) === ymd(today)) { cls.push('today'); }
    if (dt.getTime() > today.getTime()) { cls.push('out'); }
    cells += '<button data-day="' + ymd(dt) + '"'
      + (cls.length ? ' class="' + cls.join(' ') + '"' : '') + '>' + d + '</button>';
  }
  var wd = ['一', '二', '三', '四', '五', '六', '日'].map(function (w) {
    return '<span class="wd">' + w + '</span>';
  }).join('');
  repaintLayer('<div class="dp">'
    + '<div class="quick">' + [7, 30, 90].map(function (n) {
        return '<button data-q="' + n + '"' + (DASH_DAYS === n ? ' class="on"' : '')
          + '>近 ' + n + ' 天</button>';
      }).join('') + '</div>'
    + '<div class="mh"><button data-mv="-1">‹</button>'
    + '<b>' + DP_Y + ' 年 ' + (DP_M + 1) + ' 月</b>'
    + '<button data-mv="1"' + (atNow ? ' disabled' : '') + '>›</button></div>'
    + '<div class="grid">' + wd + cells + '</div>'
    + '<p class="hint" style="margin:9px 0 0">选哪天，就从那天统计到今天。</p>'
    + '</div>');
  placeFloat($('poplayer').firstChild, DP_EL, 6);
  $('poplayer').onclick = function (e) {
    var b = e.target.closest ? e.target.closest('[data-q],[data-mv],[data-day]') : null;
    if (!b) { return; }
    var q = b.getAttribute('data-q');
    if (q) { pickDays(Number(q)); return; }
    var v = b.getAttribute('data-mv');
    if (v) {
      DP_M += Number(v);
      if (DP_M < 0) { DP_M = 11; DP_Y -= 1; }
      if (DP_M > 11) { DP_M = 0; DP_Y += 1; }
      paintDatePanel();
      return;
    }
    var day = b.getAttribute('data-day');
    if (!day) { return; }
    var diff = Math.round((midNight(new Date()).getTime()
      - new Date(day + 'T00:00:00').getTime()) / 86400000) + 1;
    pickDays(Math.max(1, Math.min(365, diff)));
  };
}
function pickDays(n) { DASH_DAYS = n; closePop(); loadDash(); }
function rangeLabel() {
  if (DASH_DAYS === 7 || DASH_DAYS === 30 || DASH_DAYS === 90) { return '近 ' + DASH_DAYS + ' 天'; }
  var start = new Date(midNight(new Date()).getTime() - (DASH_DAYS - 1) * 86400000);
  return ymd(start) + ' 起';
}

// ============================================================
// 登录 / 退出
// ============================================================
function afterLogin(user) {
  $('who').textContent = (user && (user.nickname || user.username)) || '';
  show('btn-logout');
  hide('login');
  show('tabbar'); show('fab');
  switchTab('home');
  if (CODE) { $('code').value = CODE; doQuery(); }
}

$('btn-login').onclick = function () {
  var u = $('u').value.trim(), p = $('p').value;
  if (!u || !p) { setMsg($('login-msg'), '请填写账号和密码', 'err'); return; }
  $('btn-login').disabled = true;
  req('/auth/staff/login', { method: 'POST', body: JSON.stringify({ username: u, password: p }) })
    .then(function (res) {
      $('btn-login').disabled = false;
      if (!res.ok) { setMsg($('login-msg'), errText(res), 'err'); return; }
      var d = (res.body && res.body.data) || {};
      if (!d.token) { setMsg($('login-msg'), '登录返回异常', 'err'); return; }
      localStorage.setItem('staff_token', d.token);
      localStorage.setItem('staff_user', JSON.stringify(d.user || {}));
      setMsg($('login-msg'), '');
      afterLogin(d.user || {});
    })
     .catch(function () {
       $('btn-login').disabled = false;
       // 带上当前地址：一线员工在手机上开错地址（比如用了连不通的局域网 IP），
       // 或在后端还在加载的那 30 秒里点了登录，光一句「无法连接后端」根本没法定位。
       // 看到地址就能立刻分清是"地址错了"还是"后端还没起来"。
       setMsg($('login-msg'),
              '无法连接后端（当前地址 ' + location.origin + '）。'
              + '请确认后端已启动完成（约 30 秒）且与手机在同一网络', 'err');
     });
};

$('btn-logout').onclick = function () {
  localStorage.removeItem('staff_token');
  localStorage.removeItem('staff_user');
  location.reload();
};

// ============================================================
// 底部导航
// ============================================================
var LOADED = {};
function switchTab(name) {
  ['home', 'dash', 'lost', 'content'].forEach(function (t) {
    if (t === name) { show('tab-' + t); } else { hide('tab-' + t); }
  });
  var btns = $('tabbar').querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = (btns[i].getAttribute('data-tab') === name) ? 'on' : '';
  }
  if (name === 'dash' && !LOADED.dash) { LOADED.dash = 1; loadDash(); }
  if (name === 'lost' && !LOADED.lost) { LOADED.lost = 1; loadLost(); }
  if (name === 'content' && !LOADED.content) { LOADED.content = 1; loadNotices(); }
  if (name === 'home' && !LOADED.home) {
    LOADED.home = 1;
    loadCards();
    loadOrders(1);          // 默认子页是「充值账单」，先把它填上
    PA_LOADED.orders = 1;
    loadHomeStrip();        // 今日待办速览（复用只读的 /dashboard）
  }
}
$('tabbar').onclick = function (e) {
  var b = e.target.closest ? e.target.closest('button') : null;
  if (b) switchTab(b.getAttribute('data-tab'));
};

// ============================================================
// 首页：扫码核销
// ============================================================
var ORDER_NO = '';
var PASSES = [];
var SEL = {};

function stPill(s) {
  var m = { unused: ['pill-w', '未核销'], used: ['pill-ok', '已核销'], refunded: ['pill-no', '已退款'] };
  var v = m[s] || ['pill-no', s || '-'];
  return '<span class="pill ' + v[0] + '">' + v[1] + '</span>';
}

function renderPassList() {
  if (!PASSES.length) { return '<p class="hint">这单没有券明细（历史订单），只能整单核销。</p>'; }
  var html = '<div class="batch">'
    + '<label><input type="checkbox" id="sel-all">全选</label>'
    + '<span class="hint" style="margin:0">已勾 <b id="sel-n">0</b> 张</span>'
    + '<span style="flex:1"></span>'
    + '<button class="mini mini-p" id="btn-batch-verify">批量核销</button>'
    + '<button class="mini mini-r" id="btn-batch-refund">批量退款</button>'
    + '</div>';
  PASSES.forEach(function (p) {
    var off = p.status === 'refunded';
    html += '<div class="pass' + (off ? ' dim' : '') + '">'
      + '<input type="checkbox" data-code="' + esc(p.item_code) + '"' + (off ? ' disabled' : '') + '>'
      + '<div class="pi"><div class="pn">' + esc(p.project_name) + ' ×1</div>'
      + '<div class="pc">' + esc(p.item_code) + '</div></div>'
      + '<div class="pa">'
      + '<span class="pill ' + (p.status === 'used' ? 'pill-ok' : (off ? 'pill-no' : 'pill-w')) + '">'
      + (p.status === 'used' ? '已核销' : (off ? '已退款' : '未核销')) + '</span>';
    if (p.status === 'unused') {
      html += '<button class="mini mini-p" data-v="' + esc(p.item_code) + '">核销</button>';
    }
    if (!off) {
      html += '<button class="mini mini-r" data-r="' + esc(p.item_code) + '">退款</button>';
    }
    html += '</div></div>';
  });
  return html;
}

function renderQr(d) {
  var multi = (d.pass_items || []).length > 1;
  var html = '<div class="code">' + esc(d.item_code || d.verify_code || CODE) + '</div>';
  if (d.kind === 'order') {
    var names = (d.items || []).map(function (i) { return i.name + '×' + i.qty; }).join('、');
    html += '<div class="row"><span class="k">整单</span><span class="v">' + esc(names || '历史消费单') + '</span></div>';
    html += '<div class="row"><span class="k">合计</span><span class="v">' + (d.total_coins || 0) + ' 币</span></div>';
  } else {
    html += '<div class="row"><span class="k">项目</span><span class="v">' + esc(d.project_name || '-') + '</span></div>';
    html += '<div class="row"><span class="k">单价</span><span class="v">' + (d.unit_coins || 0) + ' 币</span></div>';
    html += '<div class="row"><span class="k">归属</span><span class="v">' + esc(d.phone || '—') + '</span></div>';
  }
  html += '<div class="row"><span class="k">本单券</span><span class="v">共 ' + (d.pass_total || 0)
    + ' 张 · 待玩 ' + (d.pass_unused || 0) + ' 张</span></div>';
  html += '<div style="text-align:center;margin-top:10px">'
    + stPill(d.kind === 'order' ? d.status : d.item_status) + '</div>';

  // 单项目 → 直接核销；多项目 → 整单核销 + 单独核销（逐张/批量）
  if (d.kind !== 'order') {
    html += '<button class="btn btn-p" id="btn-item">' + (multi ? '核销扫到的这一张' : '确认核销') + '</button>';
  }
  if (multi || d.kind === 'order') {
    html += '<button class="btn btn-g" id="btn-order">整单全部核销</button>';
  }
  html += '<div class="msg" id="qr-msg"></div>';
  if (multi) {
    html += '<div style="margin-top:12px"><div class="hint" style="margin-bottom:8px">'
      + '单独核销：逐张处理（已核销的也可以退款）</div><div id="pass-list">'
      + renderPassList() + '</div></div>';
  } else if ((d.pass_items || []).length === 1) {
    html += '<div style="margin-top:12px" id="pass-list">' + renderPassList() + '</div>';
  }
  return html;
}

function doQuery() {
  var code = ($('code').value || '').trim().toUpperCase();
  if (!code) { $('qr').innerHTML = '<div class="msg err">请输入券码</div>'; return; }
  $('qr').innerHTML = '<div class="msg">查询中…</div>';
  req(API + '/consumptions/by-code/' + encodeURIComponent(code)).then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { $('qr').innerHTML = '<div class="msg err">' + esc(errText(res)) + '</div>'; return; }
    var d = (res.body && res.body.data) || {};
    ORDER_NO = d.order_no || '';
    PASSES = d.pass_items || [];
    SEL = {};
    $('qr').innerHTML = renderQr(d);
    bindQr(d);
  }).catch(function () {
    $('qr').innerHTML = '<div class="msg err">无法连接后端</div>';
  });
}

function refreshQuery() { doQuery(); }

function bindQr(d) {
  var bi = $('btn-item');
  if (bi) {
    var st = d.kind === 'order' ? '' : (d.item_status || 'unused');
    bi.disabled = (st !== 'unused');
    bi.onclick = function () {
      verifyCodes([d.item_code], '确认核销这一张');
    };
  }
  var bo = $('btn-order');
  if (bo) {
    bo.disabled = !(d.pass_unused > 0);
    bo.onclick = function () {
      if (!ORDER_NO) return;
      confirmDialog({
        title: '整单核销？',
        body: '这一单剩余未核销的券会一次性全部核销，不可撤销。',
        ok: '确认核销', icon: ICON_CHECK
      }).then(function (yes) {
        if (!yes) { return; }
        doVerify(API + '/consumptions/' + encodeURIComponent(ORDER_NO) + '/verify', 'PUT', '整单已核销', null, true);
      });
    };
  }
  var all = $('sel-all');
  if (all) {
    all.onchange = function () {
      PASSES.forEach(function (p) {
        if (p.status !== 'refunded') { SEL[p.item_code] = all.checked; }
      });
      syncSel();
    };
  }
  var list = $('pass-list');
  if (list) {
    list.onclick = function (e) {
      var t = e.target;
      if (t.tagName === 'INPUT' && t.getAttribute('data-code')) {
        SEL[t.getAttribute('data-code')] = t.checked; syncSel(); return;
      }
      var v = t.getAttribute('data-v');
      if (v) { verifyCodes([v], '已核销这一张'); return; }
      var r = t.getAttribute('data-r');
      if (r) { askRefund([r]); return; }
      if (t.id === 'btn-batch-verify') {
        var codes = PASSES.filter(function (p) { return SEL[p.item_code] && p.status === 'unused'; })
          .map(function (p) { return p.item_code; });
        if (!codes.length) { toast('请先勾选要核销的券', 'err'); return; }
        confirmDialog({
          title: '批量核销 ' + codes.length + ' 张券？',
          body: '这些券会一次性核销，不可撤销。',
          ok: '确认核销', icon: ICON_CHECK
        }).then(function (yes) {
          if (yes) { verifyCodes(codes, '批量核销', true); }
        });
      }
      if (t.id === 'btn-batch-refund') {
        var rc = PASSES.filter(function (p) { return SEL[p.item_code] && p.status !== 'refunded'; })
          .map(function (p) { return p.item_code; });
        if (!rc.length) { toast('请先勾选要退款的券', 'err'); return; }
        askRefund(rc);
      }
    };
  }
  syncSel();
}

function syncSel() {
  var n = PASSES.filter(function (p) { return SEL[p.item_code] && p.status !== 'refunded'; }).length;
  var el = $('sel-n');
  if (el) el.textContent = n;
  var boxes = document.querySelectorAll('#pass-list input[data-code]');
  for (var i = 0; i < boxes.length; i++) {
    var c = boxes[i].getAttribute('data-code');
    boxes[i].checked = !!SEL[c];
  }
}

function verifyCodes(codes, label, useDialog) {
  doVerify(API + '/consumptions/items/verify', 'POST', label || '已核销', { codes: codes }, useDialog);
}

/* 核销执行器。useDialog 只有在「已经弹过确认框」时才为 true ——
   单张核销一天点几十次，绝不能每次都弹框；只有批量 / 整单才走弹窗那条路，
   好让确认框原地切成「已完成」而不是关掉再闪一个提示。 */
function doVerify(path, method, okText, body, useDialog) {
  var bs = document.querySelectorAll('#pass-list button, #btn-item, #btn-order');
  for (var i = 0; i < bs.length; i++) { bs[i].disabled = true; }
  req(path, { method: method, body: body ? JSON.stringify(body) : undefined }).then(function (res) {
    if (needLogin(res)) { if (useDialog) { closePop(); } return; }
    if (!res.ok) {
      var m = $('qr-msg');
      if (m) setMsg(m, errText(res), 'err');
      if (useDialog) { dialogFail(errText(res)); }
      refreshQuery();
      return;
    }
    var msg = (res.body && res.body.msg) || okText;
    var m2 = $('qr-msg');
    if (m2) setMsg(m2, msg, 'ok');
    if (useDialog) { dialogDone(msg, refreshQuery); } else { setTimeout(refreshQuery, 450); }
  }).catch(function () {
    var m3 = $('qr-msg');
    if (m3) setMsg(m3, '无法连接后端', 'err');
    if (useDialog) { dialogFail('无法连接后端'); }
    refreshQuery();
  });
}

function askRefund(codes) {
  var total = PASSES.filter(function (p) { return codes.indexOf(p.item_code) >= 0; })
    .reduce(function (n, p) { return n + (p.unit_coins || 0); }, 0);
  var used = PASSES.filter(function (p) { return codes.indexOf(p.item_code) >= 0 && p.status === 'used'; }).length;
  confirmDialog({
    danger: true,
    title: codes.length > 1 ? '退款 ' + codes.length + ' 张券？' : '确认退款？',
    body: '共 ' + total + ' 币' + (used ? '，其中 ' + used + ' 张已核销、同样会退' : '')
      + '。币会按原扣币比例退回用户的卡，不可撤销。',
    ok: '确认退款'
  }).then(function (yes) {
    if (!yes) { return; }
    var bs = document.querySelectorAll('#pass-list button');
    for (var i = 0; i < bs.length; i++) { bs[i].disabled = true; }
    req(API + '/consumptions/items/refund', { method: 'POST', body: JSON.stringify({ codes: codes }) })
      .then(function (res) {
        if (needLogin(res)) { closePop(); return; }
        var m = $('qr-msg');
        if (!res.ok) {
          if (m) setMsg(m, errText(res), 'err');
          dialogFail(errText(res)); refreshQuery(); return;
        }
        var ok = (res.body && res.body.msg) || '退款成功';
        if (m) setMsg(m, ok, 'ok');
        dialogDone(ok, refreshQuery);
      })
      .catch(function () {
        var m2 = $('qr-msg');
        if (m2) setMsg(m2, '无法连接后端', 'err');
        dialogFail('无法连接后端'); refreshQuery();
      });
  });
}

$('btn-query').onclick = doQuery;

// ============================================================
// 扫一扫（摄像头识别用户券码）
// ------------------------------------------------------------
// 为什么不用第三方解码库（jsQR 之类）：园区内网不一定有外网，
// 从 CDN 拉脚本会直接失败；把解码器内联进页面又太重。
// 所以走浏览器自带的 BarcodeDetector（安卓 Chrome / Edge 都有），
// 没有就**明确降级**到手动输码 —— 券码本来就是 8 位且去掉了易混字符，手输是可接受的。
//
// ⚠️ 硬约束：getUserMedia 只在**安全上下文**可用（https 或 localhost）。
//    现场如果拿 http://192.168.x.x:8000 访问，浏览器根本不给摄像头，
//    这时只能手输，或者给后端配 https。这一点必须在提示里讲清楚，别让人以为是坏了。
// ============================================================
function extractCode(text) {
  // 小程序券码二维码里装的是完整地址 ".../staff/verify?c=CODE"，扫出来是整串
  var t = String(text || '').trim();
  var m = /[?&]c=([A-Za-z0-9]+)/.exec(t);
  if (m) return m[1].toUpperCase();
  var m2 = /\b[A-Za-z0-9]{8}\b/.exec(t);
  return m2 ? m2[0].toUpperCase() : t.toUpperCase();
}

var SCAN_STREAM = null, SCAN_STOP = true;
function closeScan() {
  SCAN_STOP = true;
  if (SCAN_STREAM) {
    try { SCAN_STREAM.getTracks().forEach(function (tr) { tr.stop() }); } catch (e) { }
    SCAN_STREAM = null;
  }
  var v = $('scan-video'); if (v) { try { v.pause() } catch (e) { } v.srcObject = null; }
  $('scan').classList.add('hide');
}
function focusCode(msg) {
  if (msg) toast(msg, 'err');
  var c = $('code');
  if (c) { c.focus(); if (c.scrollIntoView) c.scrollIntoView({ block: 'center' }); }
}
function openScan() {
  var insecure = !window.isSecureContext;
  var noCam = !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia;
  if (insecure || noCam) {
    focusCode('当前地址不是安全上下文（浏览器只在 https 或 localhost 下给摄像头），'
      + '所以这里扫不了。请手动输入券码；要扫码就给后端配 https，或在电脑本机用 localhost 打开。');
    return;
  }
  if (!window.BarcodeDetector) {
    focusCode('这个浏览器不支持自动识别二维码（iOS Safari 目前不支持）。请手动输入券码。');
    return;
  }
  $('scan').classList.remove('hide');
  $('scan-tip').textContent = '正在打开摄像头…';
  navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
    .then(function (stream) {
      SCAN_STREAM = stream;
      SCAN_STOP = false;
      var v = $('scan-video');
      v.srcObject = stream;
      v.setAttribute('playsinline', '');
      return v.play();
    })
    .then(function () {
      var det = new window.BarcodeDetector({ formats: ['qr_code'] });
      $('scan-tip').textContent = '把二维码放进框里';
      (function tick() {
        if (SCAN_STOP) return;
        det.detect($('scan-video')).then(function (list) {
          if (SCAN_STOP) return;
          if (list && list.length) {
            var code = extractCode(list[0].rawValue);
            closeScan();
            $('code').value = code;
            doQuery();
            return;
          }
          setTimeout(tick, 200);   // 别用 rAF：200ms 一帧足够，还省电
        }).catch(function () { if (!SCAN_STOP) setTimeout(tick, 200); });
      })();
    })
    .catch(function (e) {
      closeScan();
      focusCode('打不开摄像头：' + ((e && e.name) || e || '未知原因')
        + '。请手动输入券码，或检查浏览器的相机权限。');
    });
}
$('btn-scan').onclick = openScan;
$('scan-close').onclick = closeScan;

// ---------- 失物照片全屏预览 ----------
function closeImgbox() { $('imgbox').classList.add('hide'); $('imgbox-img').src = ''; }
$('imgbox-close').onclick = closeImgbox;
$('imgbox').onclick = function (e) { if (e.target === $('imgbox')) closeImgbox(); };
$('code').addEventListener('keydown', function (e) { if (e.key === 'Enter') doQuery(); });

// ============================================================
// 首页：办卡管理（充值账单 / 消费账单 / 现场开卡）
// ------------------------------------------------------------
// 三块同口径对齐商家端的「办卡账单」页（充值账单 + 消费记录），
// 接口直接复用 admin handler（/staff/api 白名单），差别只有一条：
//   退卡退款、调币是财务动作，**不给员工**（与"右下角危险操作不对员工开放"同一条线），
//   所以充值账单只查账、不动金额；消费侧的核销 / 退款本来就是现场动作，照给。
// ============================================================
var PA_LOADED = {};
var orderPage = 1, orderPages = 1, orderStatus = '';
var consPage = 1, consPages = 1, consStatus = '';
var CONS = [];

$('pa-seg').onclick = function (e) {
  var b = e.target.closest ? e.target.closest('button') : null;
  if (!b) return;
  var sub = b.getAttribute('data-sub');
  var btns = $('pa-seg').querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = (btns[i].getAttribute('data-sub') === sub) ? 'on' : '';
  }
  hide('pa-orders'); hide('pa-cons'); hide('pa-issue');
  if (sub === 'orders') {
    show('pa-orders');
    if (!PA_LOADED.orders) { PA_LOADED.orders = 1; loadOrders(1); }
  } else if (sub === 'cons') {
    show('pa-cons');
    if (!PA_LOADED.cons) { PA_LOADED.cons = 1; loadCons(1); }
  } else {
    show('pa-issue'); loadCards();
  }
};

function loadCards() {
  req(API + '/cards').then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) return;
    // admin_list_cards 的 data 直接是数组
    var list = (res.body && res.body.data) || [];
    var sel = $('issue-card');
    sel.innerHTML = list.filter(function (c) { return (c.status || 'on_sale') === 'on_sale'; })
      .map(function (c) {
        return '<option value="' + c.card_id + '">' + esc(c.name) + '（¥'
          + (c.price_yuan != null ? c.price_yuan : 0) + '）</option>';
      }).join('') || '<option value="">暂无在售卡</option>';
  });
}

function yuan(fen) { return '¥' + ((fen || 0) / 100).toFixed(2); }

function renderOrderStats(s) {
  if (!s) { $('order-stats').innerHTML = ''; return; }
  var cards = [
    ['累计收款', yuan(s.paid_amount), (s.paid_count || 0) + ' 笔成交'],
    ['今日收款', yuan(s.today_paid_amount), ''],
    ['累计退款', yuan(s.refunded_amount), (s.refunded_count || 0) + ' 笔退卡'],
    ['待支付', String(s.pending_count == null ? '-' : s.pending_count), '笔待处理']
  ];
  $('order-stats').innerHTML = cards.map(function (c) {
    return '<div class="kpi"><div class="kl">' + esc(c[0]) + '</div><div class="kv">' + esc(c[1]) + '</div>'
      + (c[2] ? '<div class="kl" style="margin-top:2px">' + esc(c[2]) + '</div>' : '') + '</div>';
  }).join('');
}

$('order-st').onclick = function (e) {
  var b = e.target.closest ? e.target.closest('button') : null;
  if (!b) return;
  orderStatus = b.getAttribute('data-st') || '';
  var btns = $('order-st').querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = ((btns[i].getAttribute('data-st') || '') === orderStatus) ? 'on' : '';
  }
  loadOrders(1);
};

function loadOrders(p) {
  if (p) orderPage = p;
  var kw = ($('order-kw').value || '').trim();
  $('order-list').innerHTML = '<p class="hint" style="margin:0">加载中…</p>';
  req(API + '/orders?page=' + orderPage + '&page_size=10&status=' + encodeURIComponent(orderStatus)
      + '&keyword=' + encodeURIComponent(kw)).then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { $('order-list').innerHTML = '<p class="hint" style="margin:0">' + esc(errText(res)) + '</p>'; return; }
    var d = (res.body && res.body.data) || {};
    var items = d.items || [];
    orderPages = d.pages || 1;
    renderOrderStats(d.stats);
    $('order-total').textContent = '共 ' + (d.total || 0) + ' 条 · ' + orderPage + '/' + orderPages + ' 页';
    if (!items.length) { $('order-list').innerHTML = '<p class="hint" style="margin:0">没有符合条件的订单</p>'; return; }
    $('order-list').innerHTML = items.map(function (o) {
      var st = o.status === 'paid' ? ['pill-ok', '已支付'] : (o.status === 'refunded' ? ['pill-r', '已退卡'] : ['pill-w', '待支付']);
      return '<div class="item"><div class="t">' + esc(o.card_name || '-') + ' · ' + yuan((o.amount_yuan || 0) * 100) + '</div>'
        + '<div class="m">' + esc(o.nickname || o.username || ('用户' + (o.user_id || '-')))
        + ' · ' + esc(o.paid_at || o.created_at || '-') + '<br>单号 ' + esc(o.order_no) + '</div>'
        + '<div class="acts"><span class="pill ' + st[0] + '">' + st[1] + '</span>'
        + '<span class="hint" style="margin:0">余币 ' + (o.coins || 0) + '</span></div></div>';
    }).join('');
  }).catch(function () {
    $('order-list').innerHTML = '<p class="hint" style="margin:0">无法连接后端</p>';
  });
}
$('btn-order-search').onclick = function () { loadOrders(1); };
$('order-prev').onclick = function () { if (orderPage > 1) loadOrders(orderPage - 1); };
$('order-next').onclick = function () { if (orderPage < orderPages) loadOrders(orderPage + 1); };

// ============================================================
// 消费账单（一单一券包：整单核销 / 单独核销逐张处理 / 退款）
// ============================================================
$('cons-st').onclick = function (e) {
  var b = e.target.closest ? e.target.closest('button') : null;
  if (!b) return;
  consStatus = b.getAttribute('data-st') || '';
  var btns = $('cons-st').querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = ((btns[i].getAttribute('data-st') || '') === consStatus) ? 'on' : '';
  }
  renderCons();
};
$('btn-cons-refresh').onclick = function () { loadCons(consPage); };
$('cons-prev').onclick = function () { if (consPage > 1) loadCons(consPage - 1); };
$('cons-next').onclick = function () { if (consPage < consPages) loadCons(consPage + 1); };

function loadCons(p, cb) {
  if (p) consPage = p;
  $('cons-list').innerHTML = '<p class="hint" style="margin:0">加载中…</p>';
  req(API + '/consumptions?page=' + consPage + '&page_size=20').then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { $('cons-list').innerHTML = '<p class="hint" style="margin:0">' + esc(errText(res)) + '</p>'; return; }
    var d = (res.body && res.body.data) || {};
    CONS = d.items || [];
    // 这个接口只回 total/page/page_size，页数自己算（与商家端 PC 同样的口径）
    consPages = Math.max(1, Math.ceil((d.total || 0) / (d.page_size || 20)));
    $('cons-total').textContent = '共 ' + (d.total || 0) + ' 条 · ' + (d.page || consPage) + '/' + consPages + ' 页';
    renderCons();
    if (cb) cb();
  }).catch(function () {
    $('cons-list').innerHTML = '<p class="hint" style="margin:0">无法连接后端</p>';
  });
}

/* ① 这一单"回不了头"的动作（退款、整单核销）：收进「⋯」菜单，卡面只留当场要按的那颗。
   菜单项 a=动作码 v=参数，拼成 "a|v" 传给 consPick。 */
function consMenu(c) {
  var m = [];
  if (!c || c.status === 'refunded') { return m; }
  var ps = c.pass_items || [];
  if (ps.length > 1) {
    m.push({ a: 'cv', v: c.order_no, label: '整单核销' });
    m.push({ a: 'cr', v: c.order_no, label: '整单退款', danger: true });
  } else if (ps.length === 1) {
    m.push({ a: 'ir', v: ps[0].item_code, label: '退款', danger: true });
  } else {
    // 迁移前的历史单没有券行，只能整单处理
    m.push({ a: 'cr', v: c.order_no, label: '整单退款', danger: true });
  }
  return m;
}
/* 菜单里挑中一项后分派到原来的动作函数上（不复制业务逻辑） */
function consPick(k) {
  var i = k.indexOf('|');
  var a = k.slice(0, i), v = k.slice(i + 1);
  if (a === 'cv') { consVerifyOrder(v); return; }
  if (a === 'cr') { consRefundOrder(v); return; }
  if (a === 'ir') {
    var one = null;
    for (var j = 0; j < CONS.length; j++) {
      (CONS[j].pass_items || []).forEach(function (p) { if (p.item_code === v) { one = p; } });
    }
    consRefundCodes([v], (one && one.unit_coins) || 0, (one && one.status === 'used') ? 1 : 0);
  }
}
function menuBtn(m) {
  return '<button class="mini ' + (m.danger ? 'mini-r' : 'mini-o') + '" data-' + m.a + '="'
    + esc(m.v) + '">' + esc(m.label) + '</button>';
}

function consRow(c) {
  var passes = c.pass_items || [];
  var multi = passes.length > 1;
  var single = passes.length === 1 ? passes[0] : null;
  var names = (c.items || []).map(function (i) { return i.name + '×' + (i.qty || 1); }).join('、');
  var st = c.status === 'used' ? ['pill-ok', '已核销']
    : (c.status === 'refunded' ? ['pill-r', '已退款'] : ['pill-w', '未核销']);
  var html = '<div class="item"><div class="t">' + esc(names || '消费') + ' · ' + (c.total_coins || 0) + ' 币</div>'
    + '<div class="m">' + esc(c.nickname || '匿名游客') + (c.phone ? ' · ' + esc(c.phone) : '')
    + ' · ' + esc((c.created_at || '-').slice(0, 16)) + '<br>单号 ' + esc(c.order_no)
    + ((c.pass_total || 0) > 0 ? ' · 券 ' + c.pass_total + ' 张，待玩 ' + (c.pass_unused || 0) + ' 张' : ' · 历史单（无券明细）')
    + '</div><div class="acts"><span class="pill ' + st[0] + '">' + st[1] + '</span>'
    + '<span class="hint" style="margin:0">卡内 ' + (c.card_coins || 0) + ' 币 · 奖励 ' + (c.bonus_coins || 0) + ' 币</span>';
  var prim = '';
  if (c.status !== 'refunded') {
    if (multi) {
      prim = '<button class="mini mini-p" data-cp="' + esc(c.order_no) + '">单独核销('
        + passes.length + ')</button>';
    } else if (single) {
      if (single.status === 'unused') {
        prim = '<button class="mini mini-p" data-iv="' + esc(single.item_code) + '">核销</button>';
      }
    } else if (c.status !== 'used') {
      prim = '<button class="mini mini-o" data-cv="' + esc(c.order_no) + '">整单核销</button>';
    }
  }
  html += prim;
  var menu = consMenu(c);
  if (!prim && menu.length === 1) {
    // 没有主操作、只剩一个动作时不必折叠：点「⋯」再点一下纯属多一步
    html += menuBtn(menu[0]);
  } else if (menu.length) {
    html += '<button class="mini mini-x more" data-mno="' + esc(c.order_no)
      + '" aria-label="更多操作">⋯</button>';
  }
  return html + '</div></div>';
}

function renderCons() {
  if (!CONS.length) {
    $('cons-list').innerHTML = '<p class="hint" style="margin:0">还没有消费记录（游客在首页「游乐消费」选项目扣币后生成）</p>';
    return;
  }
  var rows = consStatus
    ? CONS.filter(function (c) { return (c.status || 'unused') === consStatus; }) : CONS;
  if (!rows.length) {
    $('cons-list').innerHTML = '<p class="hint" style="margin:0">这一页没有该状态的单，换一页或点「全部」看看</p>';
    return;
  }
  $('cons-list').innerHTML = rows.map(consRow).join('');
}

/* 账单页动作执行器。
   useDialog=true 表示「上游已经弹了确认框」，这里要把状态接着演下去：
   提交中 → 原地切成「已完成」→ 关掉再刷新。单张核销（一天几十次）走 false，
   只用一条轻提示，不拿弹窗挡路。 */
function consAct(path, method, body, doneText, useDialog) {
  function refresh() {
    loadCons(consPage, function () {
      // 抽屉开着就顺手刷一遍券状态：不然点完核销还显示"未核销"，像没生效
      if (!$('passbox').classList.contains('hide') && PB_ORDER) { openPassBox(PB_ORDER); }
    });
  }
  function ok(msg) {
    if (useDialog) { dialogDone(msg, refresh); } else { toast(msg); refresh(); }
  }
  function bad(msg) {
    if (useDialog) { dialogFail(msg); } else { toast(msg, 'err'); }
  }
  if (useDialog) { dialogBusy('正在提交…'); }
  req(path, { method: method, body: body ? JSON.stringify(body) : undefined }).then(function (res) {
    if (needLogin(res)) { if (useDialog) { closePop(); } return; }
    if (!res.ok) { bad(errText(res)); return; }
    ok((res.body && res.body.msg) || doneText || '已完成');
  }).catch(function () { bad('无法连接后端'); });
}

function consVerifyOrder(no) {
  confirmDialog({
    title: '整单核销？',
    body: '这一单剩余未核销的券会一次性全部核销，不可撤销。',
    ok: '确认核销', icon: ICON_CHECK
  }).then(function (yes) {
    if (yes) { consAct(API + '/consumptions/' + encodeURIComponent(no) + '/verify', 'PUT', null, '整单已核销', true); }
  });
}
function consVerifyCodes(codes, label, useDialog) {
  consAct(API + '/consumptions/items/verify', 'POST', { codes: codes },
    label || '已核销', useDialog);
}
function consRefundCodes(codes, total, usedN) {
  confirmDialog({
    danger: true,
    title: codes.length > 1 ? '退款 ' + codes.length + ' 张券？' : '确认退款？',
    body: '共 ' + total + ' 币' + (usedN ? '，其中 ' + usedN + ' 张已核销、同样会退' : '')
      + '。币会按原扣币比例退回用户的卡，不可撤销。',
    ok: '确认退款'
  }).then(function (yes) {
    if (yes) { consAct(API + '/consumptions/items/refund', 'POST', { codes: codes }, '退款完成', true); }
  });
}
function consRefundOrder(no) {
  confirmDialog({
    danger: true,
    title: '整单退款？',
    body: '这一单所有还没退过的券都会退掉（含已核销的），不可撤销。',
    ok: '确认退款'
  }).then(function (yes) {
    if (yes) { consAct(API + '/consumptions/' + encodeURIComponent(no) + '/refund', 'POST', null, '退款完成', true); }
  });
}

$('cons-list').onclick = function (e) {
  var t = e.target.closest ? e.target.closest('button') : null;
  if (!t) return;
  // ① 「⋯」：以按钮为原点弹出次要操作，点遮罩原路收回
  var mo = t.getAttribute('data-mno');
  if (mo) {
    var row = null;
    for (var j = 0; j < CONS.length; j++) {
      if (CONS[j].order_no === mo) { row = CONS[j]; break; }
    }
    if (!row) return;
    openPop(t, consMenu(row).map(function (m) {
      return { k: m.a + '|' + m.v, label: m.label, danger: m.danger };
    }), consPick, '这一单还能做的');
    return;
  }
  var cv = t.getAttribute('data-cv');
  if (cv) { consVerifyOrder(cv); return; }
  var cp = t.getAttribute('data-cp');
  if (cp) { openPassBox(cp); return; }
  var iv = t.getAttribute('data-iv');
  if (iv) { consVerifyCodes([iv]); return; }
  var ir = t.getAttribute('data-ir');
  if (ir) { consPick('ir|' + ir); return; }
  var cr = t.getAttribute('data-cr');
  if (cr) { consRefundOrder(cr); return; }
};

// ---------- 「单独核销」弹窗：逐张券一行，可批量勾选 ----------
var PB_ORDER = '', PB_ITEMS = [], PB_SEL = {};

function closePb() {
  $('passbox').classList.add('hide');
  PB_ORDER = ''; PB_ITEMS = []; PB_SEL = {};
}

function openPassBox(orderNo) {
  var c = null;
  for (var i = 0; i < CONS.length; i++) { if (CONS[i].order_no === orderNo) { c = CONS[i]; break; } }
  if (!c) { closePb(); return; }   // 操作后这一单可能已翻页/改状态，找不到就关掉
  PB_ORDER = orderNo;
  PB_ITEMS = c.pass_items || [];
  PB_SEL = {};
  // 默认只勾「未核销」的：现场多数场景就是把还没玩的那几张核销掉
  PB_ITEMS.forEach(function (p) { if (p.status === 'unused') PB_SEL[p.item_code] = true; });
  $('pb-sub').textContent = (c.nickname || '匿名游客') + (c.phone ? ' · ' + c.phone : '')
    + ' · 共 ' + PB_ITEMS.length + ' 张券 · 还剩 ' + (c.pass_unused || 0) + ' 张待玩';
  setMsg($('pb-msg'), '');
  renderPb();
  show('passbox');
}

function renderPb() {
  if (!PB_ITEMS.length) {
    $('pb-list').innerHTML = '<p class="hint" style="margin:0">这单没有券明细（历史订单），'
      + '请用列表里的「整单核销 / 整单退款」。</p>';
    syncPb();
    return;
  }
  var html = '';
  PB_ITEMS.forEach(function (p) {
    var off = p.status === 'refunded';
    html += '<div class="pass' + (off ? ' dim' : '') + '">'
      + '<input type="checkbox" data-code="' + esc(p.item_code) + '"' + (off ? ' disabled' : '') + '>'
      + '<div class="pi"><div class="pn">' + esc(p.project_name) + ' ×1</div>'
      + '<div class="pc">' + esc(p.item_code) + '</div></div>'
      + '<div class="pa">'
      + '<span class="pill ' + (p.status === 'used' ? 'pill-ok' : (off ? 'pill-no' : 'pill-w')) + '">'
      + (p.status === 'used' ? '已核销' : (off ? '已退款' : '未核销')) + '</span>'
      + '<span class="hint" style="margin:0">' + (p.unit_coins || 0) + ' 币</span>';
    if (p.status === 'unused') {
      html += '<button class="mini mini-p" data-v="' + esc(p.item_code) + '">核销</button>';
    }
    if (!off) {
      html += '<button class="mini mini-r" data-r="' + esc(p.item_code) + '">退款</button>';
    }
    html += '</div></div>';
  });
  $('pb-list').innerHTML = html;
  syncPb();
}

function syncPb() {
  var n = PB_ITEMS.filter(function (p) { return PB_SEL[p.item_code] && p.status !== 'refunded'; }).length;
  var el = $('pb-n');
  if (el) el.textContent = n;
  var boxes = document.querySelectorAll('#pb-list input[data-code]');
  for (var i = 0; i < boxes.length; i++) {
    boxes[i].checked = !!PB_SEL[boxes[i].getAttribute('data-code')];
  }
  var all = $('pb-all');
  if (all) {
    all.checked = PB_ITEMS.length > 0 && PB_ITEMS.every(function (p) {
      return p.status === 'refunded' || PB_SEL[p.item_code];
    });
  }
}

$('pb-close').onclick = closePb;
$('passbox').onclick = function (e) { if (e.target === $('passbox')) closePb(); };
$('pb-all').onchange = function () {
  var on = $('pb-all').checked;
  PB_ITEMS.forEach(function (p) { if (p.status !== 'refunded') { PB_SEL[p.item_code] = on; } });
  syncPb();
};
$('pb-verify').onclick = function () {
  var codes = PB_ITEMS.filter(function (p) { return PB_SEL[p.item_code] && p.status === 'unused'; })
    .map(function (p) { return p.item_code; });
  if (!codes.length) { toast('请先勾选要核销的券（只有「未核销」的能核销）', 'err'); return; }
  confirmDialog({
    title: '核销 ' + codes.length + ' 张券？',
    body: '这一单里勾选的 ' + codes.length + ' 张未核销券会一次性核销。',
    ok: '确认核销', icon: ICON_CHECK
  }).then(function (yes) {
    if (yes) { consVerifyCodes(codes, '已核销 ' + codes.length + ' 张', true); }
  });
};
$('pb-refund').onclick = function () {
  var sel = PB_ITEMS.filter(function (p) { return PB_SEL[p.item_code] && p.status !== 'refunded'; });
  if (!sel.length) { toast('请先勾选要退款的券', 'err'); return; }
  consRefundCodes(
    sel.map(function (p) { return p.item_code; }),
    sel.reduce(function (n, p) { return n + (p.unit_coins || 0); }, 0),
    sel.filter(function (p) { return p.status === 'used'; }).length
  );
};
$('pb-list').onclick = function (e) {
  var t = e.target;
  if (t.tagName === 'INPUT' && t.getAttribute('data-code')) {
    PB_SEL[t.getAttribute('data-code')] = t.checked; syncPb(); return;
  }
  var v = t.getAttribute('data-v');
  if (v) { consVerifyCodes([v]); return; }
  var r = t.getAttribute('data-r');
  if (r) {
    var p = null;
    PB_ITEMS.forEach(function (x) { if (x.item_code === r) p = x; });
    consRefundCodes([r], (p && p.unit_coins) || 0, (p && p.status === 'used') ? 1 : 0);
  }
};

$('btn-issue').onclick = function () {
  var phone = ($('issue-phone').value || '').trim();
  var cardId = Number($('issue-card').value || 0);
  var paid = ($('issue-paid').value || '').trim();
  if (!/^\\d{11}$/.test(phone)) { setMsg($('issue-msg'), '请填写 11 位手机号', 'err'); return; }
  if (!cardId) { setMsg($('issue-msg'), '请选择要开的卡', 'err'); return; }
  confirmDialog({
    title: '为 ' + phone + ' 开卡并发币？',
    body: (paid ? '线下已收 ' + paid + ' 元。' : '未填收款金额。')
      + '线下收款不经过微信支付，记录会留档。',
    ok: '确认开卡'
  }).then(function (yes) {
    if (!yes) { return; }
    $('btn-issue').disabled = true;
    var payload = { phone: phone, card_id: cardId };
    if (paid) payload.paid_yuan = Number(paid);
    req(API + '/cards/issue', { method: 'POST', body: JSON.stringify(payload) }).then(function (res) {
      $('btn-issue').disabled = false;
      if (needLogin(res)) return;
      if (!res.ok) { setMsg($('issue-msg'), errText(res), 'err'); return; }
      setMsg($('issue-msg'), (res.body && res.body.msg) || '开卡成功', 'ok');
      $('issue-paid').value = '';
      loadOrders(1);
    }).catch(function () { $('btn-issue').disabled = false; setMsg($('issue-msg'), '无法连接后端', 'err'); });
  });
};

// ============================================================
// 仪表盘
// ============================================================
/* ============================================================
   仪表盘：超大数字 + 环形图（今日待办）+ 柱状图（问答意图分布）
   数据全部来自只读的 /staff/api/dashboard（= admin.dashboard 的同一份口径，
   意图分布也是「读取侧实时聚合」那一份，不另起一套算法）。
   本段纯展现层：不新增接口、不动 /staff/api/* 白名单。
   ============================================================ */
var RING_R = 42;
var RING_C = 2 * Math.PI * RING_R;   // 环形周长（半径 42）

/* 标题旁的 ⓘ：点一下弹气泡解释这个指标是怎么算的（员工会问"这数哪来的"） */
var CHEV = '<svg class="cv" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
function ih(text) {
  return '<button class="ih" type="button" data-tip="' + esc(text) + '" aria-label="说明">?</button>';
}
/* 全局捕获阶段处理 ⓘ：先于卡片自己的点击逻辑，避免点说明反而把详情打开 */
document.addEventListener('click', function (e) {
  var t = e.target && e.target.closest ? e.target.closest('[data-tip]') : null;
  if (!t) { return; }
  e.stopPropagation();
  showTip(t, t.getAttribute('data-tip'));
}, true);

/* 环形图：items = [{label,val,color}]。多段之间留 3 单位缺口，只有一段时画满整圈。 */
function ringSvg(items) {
  var total = 0, nz = 0, i;
  for (i = 0; i < items.length; i++) {
    total += items[i].val;
    if (items[i].val > 0) { nz++; }
  }
  var segs = '', acc = 0;
  if (total > 0) {
    for (i = 0; i < items.length; i++) {
      var v = items[i].val;
      if (v <= 0) { continue; }
      var len = RING_C * v / total;
      var draw = (nz > 1) ? Math.max(len - 3, 1) : len;
      var off = -(acc + (nz > 1 ? 1.5 : 0));
      segs += '<circle cx="53" cy="53" r="' + RING_R + '" fill="none" stroke="' + items[i].color
        + '" stroke-width="11" stroke-dasharray="' + draw.toFixed(2) + ' '
        + (RING_C - draw).toFixed(2) + '" stroke-dashoffset="' + off.toFixed(2) + '"></circle>';
      acc += len;
    }
  }
  return '<svg viewBox="0 0 106 106"><circle cx="53" cy="53" r="' + RING_R
    + '" fill="none" stroke="#EBEDF1" stroke-width="11"></circle>' + segs + '</svg>';
}

/* 切到「办卡管理」的某个子页：直接点已有的分段按钮，不复制那份切换逻辑 */
function paGo(sub) {
  var bs = $('pa-seg').querySelectorAll('button');
  for (var i = 0; i < bs.length; i++) {
    if (bs[i].getAttribute('data-sub') === sub) { bs[i].click(); return; }
  }
}

/* 待办/图例的统一跳转 */
function jumpTo(t) {
  if (t === 'lost') { switchTab('lost'); return; }
  if (t === 'cons') { switchTab('home'); paGo('cons'); return; }
  if (t === 'orders') { switchTab('home'); paGo('orders'); return; }
  switchTab('home');
}

function loadDash() {
  $('dash-body').innerHTML = '<p class="hint" style="margin:0">加载中…</p>';
  req(API + '/dashboard?days=' + DASH_DAYS).then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { $('dash-body').innerHTML = esc(errText(res)); return; }
    renderDash((res.body && res.body.data) || {});
  }).catch(function () { $('dash-body').innerHTML = '无法连接后端'; });
}

function renderDash(d) {
  var todo = d.todo || {};
  var kb = d.knowledge_base || {};
  var dist = d.intent_distribution || [];
  var days = d.days || 7;
  var n = function (v) { return (v == null) ? '-' : v; };
  var num = function (v) { return Number(v) || 0; };

  // ---- 1. 三个超大数字（今日口径）----
  var hero = [
    [n(d.today_questions), '', '今日问答', 'n-hot'],
    [n(d.today_new_users), '', '今日新增用户', ''],
    [n(d.today_paid_amount), '元', '今日收款', '']
  ];
  var html = '<div class="hero-kpis">' + hero.map(function (h) {
    return '<div class="hk"><div class="n ' + h[3] + '">' + esc(h[0])
      + (h[1] ? '<em>' + esc(h[1]) + '</em>' : '') + '</div>'
      + '<div class="l">' + esc(h[2]) + '</div></div>';
  }).join('') + '</div>';

  // ---- 2. 环形图：今日待办构成（点图例直达对应页）----
  // 三段用「强调橙 + 琥珀 + 绿」的中调值：白卡上要看得清，同时比正文语义色（#B7791F/#1F8A5B）亮一档
  var items = [
    { label: '待核销消费单', val: num(todo.unused_consumptions), color: '#FF6B3D', jump: 'cons', cls: 'n-hot' },
    { label: '待认领失物', val: num(todo.lost_items), color: '#E9A23B', jump: 'lost', cls: 'n-warn' },
    { label: '待支付订单', val: num(todo.pending_orders), color: '#2FA36E', jump: 'orders', cls: '' }
  ];
  var todoTotal = items[0].val + items[1].val + items[2].val;
  html += '<div class="chart-card"><div class="cc-hd"><b>今日待办'
    + ih('待核销消费单 = 游客扣币后还没核销的券；待认领失物 = 失物库里还挂着没人认领的。'
       + '点图例任意一行可以直接跳过去处理。') + '</b><span>'
    + (todoTotal ? '共 ' + todoTotal + ' 项待处理' : '暂无积压') + '</span></div>'
    + '<div class="ring-wrap"><div class="ring">' + ringSvg(items)
    + '<div class="ring-c"><b>' + todoTotal + '</b><span>待处理</span></div></div>'
    + '<div class="legend">' + items.map(function (it) {
        return '<button class="lg-row" data-jump="' + it.jump + '">'
          + '<i style="background:' + it.color + '"></i>'
          + '<span class="nm">' + esc(it.label) + '</span>'
          + '<span class="vv ' + it.cls + '">' + it.val + '</span></button>';
      }).join('') + '</div></div></div>';

  // ---- 3. 柱状图：问答意图分布（近 N 天，与商家端大盘同一口径）----
  var maxV = 1, k;
  for (k = 0; k < dist.length; k++) { maxV = Math.max(maxV, num(dist[k].cnt)); }
  html += '<div class="chart-card"><div class="cc-hd"><b>问答意图分布'
    + ih('按关键词规则把每条问句归到 6 类意图，读取时用当前规则实时聚合——'
       + '规则升级后历史分布会跟着变，不会因为老数据没回填而失真。') + '</b>'
    + '<button class="rangetag" type="button" data-range="1">' + CHEV
    + esc(rangeLabel()) + '</button></div>'
    + '<div class="bars">' + dist.map(function (it) {
        var v = num(it.cnt);
        var h = v > 0 ? Math.max(4, Math.round(v / maxV * 72)) : 3;
        return '<div class="bar"><div class="bv">' + v + '</div>'
          + '<div class="col' + (v > 0 ? '' : ' dim') + '" style="height:' + h + 'px"></div>'
          + '<div class="bl">' + esc(it.intent) + '</div></div>';
      }).join('') + '</div>'
    + '<p class="hint" style="margin:8px 0 0;text-align:center">共 '
    + n(d.intent_total) + ' 条问答</p></div>';

  // ---- 4. 知识库规模 ----
  var kbRows = [
    ['知识库文档', n(kb.documents)],
    ['文本切片', n(kb.chunks)],
    ['向量条数', n(kb.milvus_entities)],
    ['BM25 文档', n(kb.bm25_documents)]
  ];
  html += '<div class="chart-card"><div class="cc-hd"><b>知识库规模'
    + ih('同一批资料的三种表示：切片进 BM25 做关键词匹配，向量进 Milvus 做语义匹配，'
       + '两路按 0.6 / 0.4 融合后再交给模型。') + '</b><span>累计问答 '
    + n(d.total_questions) + ' 次</span></div>'
    + '<div class="kv-list">' + kbRows.map(function (r) {
        return '<div class="r"><span class="k">' + esc(r[0]) + '</span>'
          + '<span class="v">' + esc(r[1]) + '</span></div>';
      }).join('') + '</div></div>';

  $('dash-body').innerHTML = html;
}

$('dash-body').onclick = function (e) {
  var rg = e.target.closest ? e.target.closest('[data-range]') : null;
  if (rg) { openDatePanel(rg); return; }
  var b = e.target.closest ? e.target.closest('[data-jump]') : null;
  if (b) { jumpTo(b.getAttribute('data-jump')); }
};

// ============================================================
// 首页：今日待办速览（复用同一个只读 /dashboard；拉不到就整块留空，不打扰主流程）
// ============================================================
function loadHomeStrip() {
  var box = $('home-strip');
  if (!box) { return; }
  req(API + '/dashboard').then(function (res) {
    if (needLogin(res) || !res.ok) { box.innerHTML = ''; return; }
    var d = (res.body && res.body.data) || {};
    var todo = d.todo || {};
    var n = function (v) { return (v == null) ? '-' : v; };
    var num = function (v) { return Number(v) || 0; };
    var cells = [
      [n(todo.unused_consumptions), '待核销消费单', 'cons', num(todo.unused_consumptions) > 0 ? 'n-hot' : ''],
      [n(todo.lost_items), '待认领失物', 'lost', num(todo.lost_items) > 0 ? 'n-warn' : ''],
      [n(todo.pending_orders), '待支付订单', 'orders', '']
    ];
    box.innerHTML = cells.map(function (c) {
      return '<button class="sc" data-jump="' + c[2] + '"><div class="n ' + c[3] + '">'
        + esc(c[0]) + '</div><div class="l">' + esc(c[1]) + '</div></button>';
    }).join('');
  }).catch(function () { box.innerHTML = ''; });
}

$('home-strip').onclick = function (e) {
  var b = e.target.closest ? e.target.closest('[data-jump]') : null;
  if (b) { jumpTo(b.getAttribute('data-jump')); }
};

// ============================================================
// 失物招领
// ============================================================
var lostStatus = '';
var LOST = [];          // 当前这一页失物，卡片详情面板要按 id 取回原始字段
$('lost-seg').onclick = function (e) {
  var b = e.target.closest ? e.target.closest('button') : null;
  if (!b) return;
  lostStatus = b.getAttribute('data-st') || '';
  var btns = $('lost-seg').querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = (btns[i].getAttribute('data-st') === lostStatus) ? 'on' : '';
  }
  loadLost();
};

function loadLost() {
  var kw = ($('lost-kw').value || '').trim();
  $('lost-list').innerHTML = '<p class="hint" style="margin:0">加载中…</p>';
  req(API + '/lost-found/items?page=1&page_size=30&status=' + encodeURIComponent(lostStatus)
    + '&keyword=' + encodeURIComponent(kw)).then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { $('lost-list').innerHTML = '<p class="hint" style="margin:0">' + esc(errText(res)) + '</p>'; return; }
    var d = (res.body && res.body.data) || {};
    var items = d.items || [];
    LOST = items;
    if (!items.length) { $('lost-list').innerHTML = '<p class="hint" style="margin:0">没有失物记录</p>'; return; }
    $('lost-list').innerHTML = items.map(function (it) {
      var claimed = (it.status || 'lost') === 'claimed';
      // 照片是关键：员工要靠它对认领人描述的东西，之前**压根没渲染**，等于没有图。
      // photo_url 是相对地址（/static/lost_found/xxx），同源直接用即可。
      var pic = it.photo_url
        ? '<img class="lf-pic" src="' + esc(it.photo_url) + '" alt="失物照片"'
          + ' data-big="' + esc(it.photo_url) + '">'
        : '<div class="lf-pic lf-nopic">无照片</div>';
      var ai = it.ai || {};
      var tags = [];
      if (ai.category) tags.push(String(ai.category));
      if (ai.color && ai.color !== '不确定') tags.push(String(ai.color));
      (ai.features || []).slice(0, 2).forEach(function (f) {
        if (f) tags.push(String(f).slice(0, 16));
      });
      var tagHtml = tags.length
        ? '<div>' + tags.map(function (t) { return '<span class="tag">' + esc(t) + '</span>' }).join('') + '</div>'
        : '';
      return '<div class="item tap" data-lost="' + it.id + '"><div class="lf-row">' + pic
        + '<div style="flex:1;min-width:0">'
        + '<div class="t">' + esc(it.name) + '</div>'
        + '<div class="m">' + (it.source === 'merchant' ? '员工登记' : '游客报失')
        + ' · ' + esc(it.reporter || '') + ' · ' + esc(it.created_at || '') + '</div>'
        + tagHtml
        + '<div class="acts"><span class="pill ' + (claimed ? 'pill-ok' : 'pill-w') + '">'
        + (claimed ? '已认领' : (it.status === 'pending' ? '待审核' : '待认领')) + '</span>'
        + (claimed
          ? '<button class="mini mini-o" data-lid="' + it.id + '" data-lst="lost">恢复待认领</button>'
          : '<button class="mini mini-p" data-lid="' + it.id + '" data-lst="claimed">标记已认领</button>')
        + '</div>'
        + '<span class="lf-more">点卡片看大图与详情 ›</span>'
        + '</div></div></div>';
    }).join('');
  }).catch(function () { $('lost-list').innerHTML = '<p class="hint" style="margin:0">无法连接后端</p>'; });
}
$('btn-lost-search').onclick = loadLost;

function lostSetStatus(btn) {
  var id = btn.getAttribute('data-lid');
  var st = btn.getAttribute('data-lst');
  btn.disabled = true;
  req(API + '/lost-found/items/' + id + '/status', { method: 'PUT', body: JSON.stringify({ status: st }) })
    .then(function (res) {
      if (needLogin(res)) return;
      if (!res.ok) { toast(errText(res), 'err'); btn.disabled = false; return; }
      toast(st === 'claimed' ? '已标记为已认领' : '已恢复为待认领');
      loadLost();
    }).catch(function () { toast('无法连接后端', 'err'); btn.disabled = false; });
}

/* ② 卡片弹出详情：列表里只有 66px 缩略图和 2 个标签，认领核对时需要看全 */
function lostDetail(card) {
  var id = card.getAttribute('data-lost'), it = null;
  for (var i = 0; i < LOST.length; i++) {
    if (String(LOST[i].id) === String(id)) { it = LOST[i]; break; }
  }
  if (!it) { return; }
  var claimed = (it.status || 'lost') === 'claimed';
  var ai = it.ai || {};
  var tags = [];
  if (ai.category) { tags.push(String(ai.category)); }
  if (ai.color && ai.color !== '不确定') { tags.push(String(ai.color)); }
  (ai.features || []).forEach(function (f) { if (f) { tags.push(String(f)); } });
  var rows = [
    ['来源', it.source === 'merchant' ? '员工登记' : '游客报失'],
    ['报失人', it.reporter || '-'],
    ['登记时间', it.created_at || '-'],
    ['失物编号', '#' + it.id]
  ];
  openFromCard(card, '<h3>' + esc(it.name || '（未命名）') + '</h3>'
    + (it.photo_url
      ? '<img class="lf-big" src="' + esc(it.photo_url) + '" alt="失物照片"'
        + ' data-big="' + esc(it.photo_url) + '">'
      : '<div class="lf-big lf-nopic">无照片</div>')
    + '<span class="pill ' + (claimed ? 'pill-ok' : 'pill-w') + '">'
    + (claimed ? '已认领' : (it.status === 'pending' ? '待审核' : '待认领')) + '</span>'
    + (tags.length ? '<div class="dlg-tags">' + tags.map(function (x) {
        return '<span class="tag">' + esc(x) + '</span>';
      }).join('') + '</div>' : '')
    + '<div class="kv-list">' + rows.map(function (r) {
        return '<div class="r"><span class="k">' + esc(r[0]) + '</span>'
          + '<span class="v">' + esc(r[1]) + '</span></div>';
      }).join('') + '</div>'
    + '<div class="dacts"><button class="cancel" data-dlg="no">关闭</button>'
    + '<button class="ok" data-lid="' + it.id + '" data-lst="' + (claimed ? 'lost' : 'claimed')
    + '">' + (claimed ? '恢复待认领' : '标记已认领') + '</button></div>');
  $('poplayer').onclick = function (e) {
    var b = e.target.closest ? e.target.closest('[data-dlg],[data-lid],[data-big]') : null;
    if (!b) { return; }
    var big = b.getAttribute('data-big');
    if (big) { $('imgbox-img').src = big; $('imgbox').classList.remove('hide'); return; }
    if (b.getAttribute('data-dlg') === 'no') { closePop(); return; }
    if (!b.getAttribute('data-lid')) { return; }
    closePop();
    lostSetStatus(b);
  };
}

$('lost-list').onclick = function (e) {
  var t = e.target;
  // 先看是不是点了照片（缩略图只有 66px，认领核对要看大图）
  var big = t.getAttribute && t.getAttribute('data-big');
  if (big) { $('imgbox-img').src = big; $('imgbox').classList.remove('hide'); return; }
  var btn = t.closest ? t.closest('[data-lid]') : null;
  if (btn) { lostSetStatus(btn); return; }
  // ② 其余位置（卡片空白处）＝ 从这张卡原位放大成详情面板
  var card = t.closest ? t.closest('[data-lost]') : null;
  if (card) { lostDetail(card); }
};

$('btn-lost-add').onclick = function () {
  var name = ($('lost-name').value || '').trim();
  var file = $('lost-photo').files[0];
  if (!name && !file) { setMsg($('lost-msg'), '请填写名称或上传照片', 'err'); return; }
  var fd = new FormData();
  fd.append('name', name);
  if (file) fd.append('photo', file);
  $('btn-lost-add').disabled = true;
  fetch(API + '/lost-found/items', { method: 'POST', headers: { 'Authorization': 'Bearer ' + tk() }, body: fd })
    .then(function (r) { return r.text().then(function (t) {
      var j = {}; try { j = t ? JSON.parse(t) : {}; } catch (e) {}
      return { ok: r.ok, status: r.status, body: j };
    }); })
    .then(function (res) {
      $('btn-lost-add').disabled = false;
      if (needLogin(res)) return;
      if (!res.ok) { setMsg($('lost-msg'), errText(res), 'err'); return; }
      setMsg($('lost-msg'), '已登记', 'ok');
      $('lost-name').value = ''; $('lost-photo').value = '';
      loadLost();
    }).catch(function () { $('btn-lost-add').disabled = false; setMsg($('lost-msg'), '无法连接后端', 'err'); });
};

// ============================================================
// 内容管理
// ============================================================
$('ct-seg').onclick = function (e) {
  var b = e.target.closest ? e.target.closest('button') : null;
  if (!b) return;
  var sub = b.getAttribute('data-sub');
  var btns = $('ct-seg').querySelectorAll('button');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = (btns[i].getAttribute('data-sub') === sub) ? 'on' : '';
  }
  if (sub === 'notices') { show('ct-notices'); hide('ct-feedback'); loadNotices(); }
  else { hide('ct-notices'); show('ct-feedback'); loadFeedback(); }
};

var NOTICES = [];
function loadNotices() {
  $('notice-list').innerHTML = '<p class="hint" style="margin:0">加载中…</p>';
  req(API + '/notices?page=1&page_size=50').then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { $('notice-list').innerHTML = '<p class="hint" style="margin:0">' + esc(errText(res)) + '</p>'; return; }
    NOTICES = (((res.body || {}).data || {}).items) || [];
    if (!NOTICES.length) { $('notice-list').innerHTML = '<p class="hint" style="margin:0">还没有公告</p>'; return; }
    $('notice-list').innerHTML = NOTICES.map(function (n) {
      var on = (n.status || 'on') === 'on';
      return '<div class="item"><div class="t">' + esc(n.title) + '</div>'
        + '<div class="m">' + (n.kind === 'hot' ? '今日热点' : '园区公告')
        + (n.content ? ' · ' + esc(n.content) : '') + '</div>'
        + '<div class="acts"><span class="pill ' + (on ? 'pill-ok' : 'pill-no') + '">'
        + (on ? '上架中' : '已下架') + '</span>'
        + '<button class="mini ' + (on ? 'mini-o' : 'mini-p') + '" data-nid="' + n.id + '">'
        + (on ? '下架' : '重新上架') + '</button>'
        + '</div></div>';
    }).join('');
  }).catch(function () { $('notice-list').innerHTML = '<p class="hint" style="margin:0">无法连接后端</p>'; });
}
$('notice-list').onclick = function (e) {
  var t = e.target;
  var id = t.getAttribute && t.getAttribute('data-nid');
  if (!id) return;
  var cur = null;
  for (var i = 0; i < NOTICES.length; i++) {
    if (String(NOTICES[i].id) === String(id)) { cur = NOTICES[i]; break; }
  }
  if (!cur) return;
  t.disabled = true;
  // 这个接口是"整体替换"语义（kind/title 必填），只能回传整条再改状态
  req(API + '/notices/' + id, {
    method: 'PUT',
    body: JSON.stringify({
      kind: cur.kind || 'announcement',
      title: cur.title || '',
      content: cur.content || '',
      sort: cur.sort || 0,
      status: (cur.status || 'on') === 'on' ? 'off' : 'on'
    })
  }).then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { toast(errText(res), 'err'); t.disabled = false; return; }
    loadNotices();
  }).catch(function () { toast('无法连接后端', 'err'); t.disabled = false; });
};

$('btn-notice-add').onclick = function () {
  var kind = $('n-kind').value;
  var title = ($('n-title').value || '').trim();
  if (!title) { setMsg($('notice-msg'), '请填写标题', 'err'); return; }
  $('btn-notice-add').disabled = true;
  req(API + '/notices', {
    method: 'POST',
    body: JSON.stringify({ kind: kind, title: title, content: ($('n-content').value || '').trim() })
  }).then(function (res) {
    $('btn-notice-add').disabled = false;
    if (needLogin(res)) return;
    if (!res.ok) { setMsg($('notice-msg'), errText(res), 'err'); return; }
    setMsg($('notice-msg'), '已发布', 'ok');
    $('n-title').value = ''; $('n-content').value = '';
    loadNotices();
  }).catch(function () { $('btn-notice-add').disabled = false; setMsg($('notice-msg'), '无法连接后端', 'err'); });
};

function loadFeedback() {
  $('fb-list').innerHTML = '<p class="hint" style="margin:0">加载中…</p>';
  req(API + '/notices/feedback?page=1&page_size=30').then(function (res) {
    if (needLogin(res)) return;
    if (!res.ok) { $('fb-list').innerHTML = '<p class="hint" style="margin:0">' + esc(errText(res)) + '</p>'; return; }
    var items = ((res.body && res.body.data) || {}).items || [];
    if (!items.length) { $('fb-list').innerHTML = '<p class="hint" style="margin:0">还没有反馈</p>'; return; }
    $('fb-list').innerHTML = items.map(function (f) {
      return '<div class="item"><div class="t">' + esc(f.content || '（无内容）') + '</div>'
        + '<div class="m">' + esc(f.nickname || '游客') + ' · '
        + esc(f.created_at || '') + '</div></div>';
    }).join('');
  }).catch(function () { $('fb-list').innerHTML = '<p class="hint" style="margin:0">无法连接后端</p>'; });
}

// ============================================================
// 右下角智能体（只读）
// ============================================================
$('fab').onclick = function () { show('agent'); };
$('agent-close').onclick = function () { hide('agent'); };
$('agent').onclick = function (e) { if (e.target === $('agent')) hide('agent'); };

function addEv(cls, title, text) {
  var d = document.createElement('div');
  d.className = 'ev' + (cls ? ' ' + cls : '');
  d.innerHTML = (title ? '<div class="et">' + esc(title) + '</div>' : '')
    + (text ? '<div>' + esc(text) + '</div>' : '');
  $('chatlog').appendChild(d);
  $('chatlog').scrollTop = $('chatlog').scrollHeight;
  return d;
}

function onAgentEvent(e) {
  if (e.type === 'step') {
    if (e.status === 'running') { addEv('', e.tool, '正在执行…'); return; }
    addEv(e.status === 'failed' ? 'err' : '', e.tool, e.report || e.brief || '');
  } else if (e.type === 'summary') {
    addEv('done', '结论', e.text || '');
  } else if (e.type === 'notice' || e.type === 'error') {
    addEv(e.type === 'error' ? 'err' : '', '', e.content || '');
  }
}

var agentBusy = false;
function askAgent(msg) {
  if (agentBusy) return;
  agentBusy = true;
  $('agent-send').disabled = true;
  addEv('user', '', msg);
  fetch(API + '/assistant/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tk() },
    body: JSON.stringify({ message: msg })
  }).then(function (res) {
    if (res.status === 401 || res.status === 403) { toLogin(); return; }
    if (!res.ok || !res.body) {
      return res.text().then(function (t) {
        var j = {}; try { j = JSON.parse(t); } catch (e) {}
        addEv('err', '', (j && j.detail) || '请求失败');
      });
    }
    var reader = res.body.getReader();
    var dec = new TextDecoder();
    var buf = '';
    function pump() {
      return reader.read().then(function (r) {
        if (r.done) {
          agentBusy = false;
          $('agent-send').disabled = false;
          return;
        }
        buf += dec.decode(r.value, { stream: true });
        var i;
        while ((i = buf.indexOf('\\n\\n')) >= 0) {
          var frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          var lines = frame.split('\\n');
          for (var k = 0; k < lines.length; k++) {
            if (lines[k].indexOf('data:') !== 0) continue;
            try { onAgentEvent(JSON.parse(lines[k].slice(5).trim())); } catch (err) {}
          }
        }
        return pump();
      });
    }
    return pump();
  }).catch(function () {
    addEv('err', '', '无法连接后端');
  }).then(function () {
    agentBusy = false;
    $('agent-send').disabled = false;
  });
}
$('agent-send').onclick = function () {
  var v = ($('agent-input').value || '').trim();
  if (!v) return;
  $('agent-input').value = '';
  askAgent(v);
};
$('agent-input').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') { $('agent-send').click(); }
});
document.querySelector('.quickrow').onclick = function (e) {
  var q = e.target.getAttribute && e.target.getAttribute('data-q');
  if (q) askAgent(q);
};

// ============================================================
// 启动
// ============================================================
if (tk()) {
  var u = {};
  try { u = JSON.parse(localStorage.getItem('staff_user') || '{}'); } catch (e) {}
  afterLogin(u);
} else {
  toLogin();
}
</script>
</body>
</html>
"""


@router.get("/verify", response_class=HTMLResponse, summary="员工手机扫码核销页（券二维码落地）")
async def staff_verify_page() -> HTMLResponse:
    """扫码落地页。券码从 query 参数 c 读取（页面内 JS 解析后自动查询）。

    与 /staff/app 返回同一份页面：员工端是一个单页应用，扫码只是它的一个入口，
    没必要维护两份核销 UI（改一处漏一处）。
    """
    return HTMLResponse(content=_PAGE)


@router.get("/app", response_class=HTMLResponse, summary="员工端主页（扫码+办卡/仪表盘/失物/内容）")
async def staff_app_page() -> HTMLResponse:
    """员工端入口，不带券码。"""
    return HTMLResponse(content=_PAGE)
