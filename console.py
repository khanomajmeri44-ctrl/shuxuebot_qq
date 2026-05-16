"""Web 控制台。

这里保留原有的嵌入式控制台实现，并把它从宿主与业务逻辑中分离出来。
当前控制台仅暴露 DeepSeek 相关语言模型配置。
"""

from . import shared
from .shared import *
from .shared import _FLASK_AVAILABLE, _console_log_buffer
from .personality import PersonalityCore
from .brain import BrainInterpreter
import hmac
import secrets
import copy

# 10.5 Web 控制台 (ConsoleServer)
# ==========================================
_CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>淑雪 · 控制台</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0c0e14;--bg2:#12151e;--bg3:#181c28;--bd:#222736;
  --acc:#7eb8f7;--acc2:#f7a97e;--grn:#6cf7a0;--red:#f77e7e;--ylw:#f7d97e;--pur:#c07ef7;
  --tx:#bcc8e0;--tx2:#58637a;--tx3:#30394d;
  --mono:'JetBrains Mono',monospace;--sans:'Noto Sans SC',sans-serif;--r:6px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:14px}
.layout{display:grid;grid-template-columns:340px 1fr;grid-template-rows:46px 1fr;height:100vh}
.topbar{grid-column:1/-1;display:flex;align-items:center;gap:10px;padding:0 18px;background:var(--bg2);border-bottom:1px solid var(--bd)}
.logo{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--acc);letter-spacing:.1em}
.dot{width:7px;height:7px;border-radius:50%;background:var(--grn);box-shadow:0 0 7px var(--grn);animation:blink 2.5s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.dot.off{background:var(--red);box-shadow:0 0 7px var(--red);animation:none}
.st{font-family:var(--mono);font-size:11px;color:var(--tx2)}.sp{flex:1}
.be-tag{font-family:var(--mono);font-size:11px;padding:3px 10px;border-radius:20px;border:1px solid var(--bd);color:var(--acc2);background:rgba(247,169,126,.07)}
/* sidebar */
.sidebar{background:var(--bg2);border-right:1px solid var(--bd);display:flex;flex-direction:column;overflow:hidden}
.tabs{display:flex;border-bottom:1px solid var(--bd);flex-shrink:0}
.tb{flex:1;padding:9px 0;background:none;border:none;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:600;color:var(--tx2);letter-spacing:.06em;border-bottom:2px solid transparent;transition:all .15s}
.tb.on{color:var(--acc);border-bottom-color:var(--acc)}.tb:hover:not(.on){color:var(--tx)}
.sbody{flex:1;overflow-y:auto;padding:14px 14px 20px}
.sbody::-webkit-scrollbar{width:3px}.sbody::-webkit-scrollbar-thumb{background:var(--bd);border-radius:2px}
/* form */
.fg{margin-bottom:16px}
.fl{display:block;font-family:var(--mono);font-size:10px;font-weight:700;color:var(--tx2);letter-spacing:.1em;text-transform:uppercase;margin-bottom:5px}
.fl .bg{display:inline-block;margin-left:5px;padding:1px 5px;font-size:9px;border-radius:3px;background:rgba(126,184,247,.1);color:var(--acc)}
.fl-row{display:flex;align-items:center;gap:6px;margin-bottom:5px}
.fl-row .fl{margin-bottom:0}
/* tooltip */
.tip-wrap{position:relative;display:inline-flex;align-items:center}
.tip-icon{width:14px;height:14px;border-radius:50%;background:var(--bg3);border:1px solid var(--bd);color:var(--tx2);font-size:9px;font-family:var(--mono);display:flex;align-items:center;justify-content:center;cursor:default;user-select:none;flex-shrink:0}
.tip-icon:hover{border-color:var(--acc);color:var(--acc)}
.tooltip{position:absolute;left:20px;top:50%;transform:translateY(-50%);z-index:100;
  background:var(--bg3);border:1px solid var(--acc);border-radius:var(--r);
  padding:8px 11px;min-width:200px;max-width:260px;
  font-family:var(--sans);font-size:12px;color:var(--tx);line-height:1.6;
  box-shadow:0 4px 20px rgba(0,0,0,.5);
  opacity:0;pointer-events:none;transition:opacity .15s}
.tip-wrap:hover .tooltip{opacity:1;pointer-events:auto}
.tooltip .tt{font-family:var(--mono);font-size:10px;color:var(--acc);margin-bottom:4px;display:block}
input[type=text],input[type=number],textarea,select{
  width:100%;background:var(--bg3);border:1px solid var(--bd);border-radius:var(--r);
  color:var(--tx);font-family:var(--mono);font-size:12px;padding:7px 9px;
  outline:none;transition:border .15s;-webkit-appearance:none}
input:focus,textarea:focus{border-color:var(--acc)}
textarea{resize:vertical;min-height:160px;line-height:1.65;font-family:var(--sans);font-size:13px}
.rg{display:flex;gap:6px}
.rb{flex:1;padding:7px 0;text-align:center;cursor:pointer;border:1px solid var(--bd);border-radius:var(--r);font-family:var(--mono);font-size:11px;color:var(--tx2);transition:all .15s;user-select:none}
.rb.on{border-color:var(--acc);color:var(--acc);background:rgba(126,184,247,.08)}.rb:hover:not(.on){color:var(--tx);border-color:var(--tx3)}
/* system-managed note */
.sys-note{background:rgba(247,169,126,.05);border:1px solid rgba(247,169,126,.2);border-radius:var(--r);padding:8px 10px;margin-top:6px;font-size:11px;color:var(--acc2);line-height:1.6}
.sys-note b{font-weight:600}
/* persona boundary indicator */
.persona-box{border:1px dashed rgba(126,184,247,.25);border-radius:var(--r);padding:8px;margin-bottom:6px}
.persona-box .pb-label{font-family:var(--mono);font-size:9px;color:var(--acc);letter-spacing:.08em;margin-bottom:6px;display:flex;align-items:center;gap:5px}
.persona-box .pb-label::after{content:'';flex:1;height:1px;background:rgba(126,184,247,.15)}
.btn-save{width:100%;padding:9px;margin-top:6px;background:rgba(126,184,247,.1);border:1px solid var(--acc);border-radius:var(--r);color:var(--acc);font-family:var(--mono);font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.07em;transition:all .15s}
.btn-save:hover{background:rgba(126,184,247,.2)}
.smsg{font-family:var(--mono);font-size:11px;text-align:center;margin-top:7px;height:15px}
.smsg.ok{color:var(--grn)}.smsg.err{color:var(--red)}
hr.dv{border:none;border-top:1px solid var(--bd);margin:14px 0}
/* main area tabs */
.main-area{display:flex;flex-direction:column;overflow:hidden}
.main-tabs{display:flex;border-bottom:1px solid var(--bd);background:var(--bg2);flex-shrink:0}
.mt-btn{padding:10px 18px;background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--tx2);letter-spacing:.06em;transition:all .15s}
.mt-btn.on{color:var(--acc);border-bottom-color:var(--acc)}.mt-btn:hover:not(.on){color:var(--tx)}
/* chat */
.chat-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden}
.ch{display:flex;align-items:center;gap:10px;padding:9px 18px;border-bottom:1px solid var(--bd);background:var(--bg2);flex-shrink:0}
.av{width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,#7eb8f7,#b07ef7);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:#fff;flex-shrink:0}
.cn{font-weight:500;font-size:14px}.cs{font-size:11px;color:var(--tx2);font-family:var(--mono)}
.btn-clr{padding:4px 11px;background:none;border:1px solid var(--bd);border-radius:var(--r);color:var(--tx2);font-family:var(--mono);font-size:10px;cursor:pointer;transition:all .15s}
.btn-clr:hover{border-color:var(--red);color:var(--red)}
.btn-top{padding:4px 10px;background:none;border:1px solid var(--bd);border-radius:var(--r);color:var(--tx2);font-family:var(--mono);font-size:10px;cursor:pointer;transition:all .15s}
.btn-top:hover{border-color:var(--acc);color:var(--acc)}
.btn-top.warn:hover{border-color:var(--ylw);color:var(--ylw)}
.btn-top.danger:hover{border-color:var(--red);color:var(--red)}
.msgs{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:14px}
.msgs::-webkit-scrollbar{width:3px}.msgs::-webkit-scrollbar-thumb{background:var(--bd);border-radius:2px}
.msg{display:flex;gap:9px;max-width:80%;animation:fu .18s ease-out}
@keyframes fu{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}
.msg.u{margin-left:auto;flex-direction:row-reverse}
.ma{width:26px;height:26px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}
.msg.b .ma{background:linear-gradient(135deg,#7eb8f7,#b07ef7);color:#fff}
.msg.u .ma{background:var(--bg3);border:1px solid var(--bd);color:var(--tx2)}
.mb{display:flex;flex-direction:column;gap:3px}.msg.u .mb{align-items:flex-end}
.bub{padding:9px 13px;border-radius:11px;line-height:1.7;font-size:14px;word-break:break-word;white-space:pre-wrap}
.msg.b .bub{background:var(--bg3);border:1px solid var(--bd);border-top-left-radius:3px}
.msg.u .bub{background:rgba(126,184,247,.1);border:1px solid rgba(126,184,247,.2);border-top-right-radius:3px}
.bub.err{background:rgba(247,126,126,.07);border-color:rgba(247,126,126,.25);color:var(--red)}
.mt-ts{font-family:var(--mono);font-size:10px;color:var(--tx3)}
.typing{display:flex;align-items:center;gap:4px;padding:9px 13px}
.td{width:5px;height:5px;border-radius:50%;background:var(--tx2);animation:tp .85s infinite}
.td:nth-child(2){animation-delay:.14s}.td:nth-child(3){animation-delay:.28s}
@keyframes tp{0%,60%,100%{transform:none;opacity:.35}30%{transform:translateY(-5px);opacity:1}}
.sys{text-align:center;font-family:var(--mono);font-size:10px;color:var(--tx3)}
.sys span{display:inline-block;padding:3px 10px;border-radius:20px;border:1px solid var(--bd);background:var(--bg2)}
.cia{padding:12px 18px;border-top:1px solid var(--bd);background:var(--bg2);flex-shrink:0;display:flex;gap:9px;align-items:flex-end}
.iw{flex:1}
#inp{width:100%;min-height:42px;max-height:130px;resize:none;background:var(--bg3);border:1px solid var(--bd);border-radius:9px;color:var(--tx);font-family:var(--sans);font-size:14px;padding:10px 13px;outline:none;transition:border .15s;line-height:1.5;overflow-y:auto}
#inp:focus{border-color:var(--acc)}#inp::placeholder{color:var(--tx3)}
.btn-s{width:42px;height:42px;flex-shrink:0;background:var(--acc);border:none;border-radius:9px;color:#0c0e14;font-size:17px;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center}
.btn-s:hover{background:#a6d1ff;transform:scale(1.04)}.btn-s:disabled{background:var(--bd);color:var(--tx3);cursor:default;transform:none}
.ih{font-family:var(--mono);font-size:10px;color:var(--tx3);margin-top:4px;padding-left:2px}
/* log panel */
.log-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}
.log-toolbar{display:flex;align-items:center;gap:8px;padding:8px 16px;border-bottom:1px solid var(--bd);background:var(--bg2);flex-shrink:0}
.log-toolbar span{font-family:var(--mono);font-size:11px;color:var(--tx2)}
.log-filter{display:flex;gap:5px}
.lf{padding:3px 9px;border:1px solid var(--bd);border-radius:12px;background:none;font-family:var(--mono);font-size:10px;color:var(--tx2);cursor:pointer;transition:all .12s;letter-spacing:.04em}
.lf:hover{color:var(--tx);border-color:var(--tx3)}
.lf.on{background:rgba(126,184,247,.1);border-color:var(--acc);color:var(--acc)}
.lf.l-warn.on{background:rgba(247,217,126,.1);border-color:var(--ylw);color:var(--ylw)}
.lf.l-err.on{background:rgba(247,126,126,.1);border-color:var(--red);color:var(--red)}
.lf.l-suc.on{background:rgba(108,247,160,.1);border-color:var(--grn);color:var(--grn)}
.lf.l-ai.on{background:rgba(192,126,247,.1);border-color:var(--pur);color:var(--pur)}
.log-sp{flex:1}
.btn-log-clr{padding:3px 9px;border:1px solid var(--bd);border-radius:12px;background:none;font-family:var(--mono);font-size:10px;color:var(--tx2);cursor:pointer;transition:all .12s}
.btn-log-clr:hover{border-color:var(--red);color:var(--red)}
.log-scroll{flex:1;overflow-y:auto;padding:10px 14px 16px;font-family:var(--mono);font-size:12px;display:flex;flex-direction:column;gap:4px}
.log-scroll::-webkit-scrollbar{width:3px}.log-scroll::-webkit-scrollbar-thumb{background:var(--bd);border-radius:2px}
.log-row{display:grid;grid-template-columns:76px 72px 92px minmax(0,1fr);gap:8px;padding:7px 9px;border:1px solid rgba(34,39,54,.55);border-radius:6px;background:rgba(18,21,30,.45);align-items:start;animation:fu .12s ease-out}
.log-row .lr-ts{color:var(--tx3);font-size:11px;line-height:1.5}
.log-row .lr-ts em{display:block;font-style:normal;font-size:9px;color:var(--tx3);opacity:.65}
.log-row .lr-lv{font-size:11px;line-height:1.5;font-weight:700}
.log-row .lr-tag{color:var(--tx2);font-size:11px;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log-row .lr-msg{color:var(--tx);line-height:1.55;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;font-size:12px}
.log-cell{min-width:0;display:flex;flex-direction:column;align-items:flex-start}
.log-row.lv-ERROR{background:rgba(247,126,126,.055);border-color:rgba(247,126,126,.2)}
.log-row.lv-WARN{background:rgba(247,217,126,.05);border-color:rgba(247,217,126,.18)}
.log-row.lv-AI{background:rgba(192,126,247,.045);border-color:rgba(192,126,247,.16)}
.log-row.lv-SUCCESS{background:rgba(108,247,160,.04);border-color:rgba(108,247,160,.14)}
.lv-INFO .lr-lv{color:var(--acc)}.lv-SUCCESS .lr-lv{color:var(--grn)}
.lv-WARN .lr-lv{color:var(--ylw)}.lv-ERROR .lr-lv{color:var(--red)}
.lv-AI .lr-lv{color:var(--pur)}.lv-SOC .lr-lv{color:var(--acc2)}
.log-empty{text-align:center;color:var(--tx3);font-size:11px;padding:30px 0}
.log-row.long:not(.open) .lr-msg{max-height:4.7em;overflow:hidden;position:relative}
.log-row.long:not(.open) .lr-msg::after{content:'';position:absolute;left:0;right:0;bottom:0;height:1.7em;background:linear-gradient(transparent,var(--bg3));pointer-events:none}
.log-more{display:inline-flex;align-items:center;margin-top:6px;padding:2px 9px;border:1px solid var(--bd);border-radius:10px;background:rgba(126,184,247,.08);color:var(--acc);font-family:var(--mono);font-size:10px;cursor:pointer;position:relative;z-index:2}
.log-more:hover{border-color:var(--acc);background:rgba(126,184,247,.12)}
/* about */
.kv{display:flex;justify-content:space-between;margin-bottom:5px;font-family:var(--mono);font-size:11px}
.kv .k{color:var(--tx2)}.kv .v{color:var(--tx)}
.ac{background:var(--bg3);border:1px solid var(--bd);border-radius:var(--r);padding:13px;margin-bottom:11px}
.ac h4{font-family:var(--mono);font-size:10px;color:var(--acc);margin-bottom:7px;letter-spacing:.08em}
.ac p{font-size:12px;color:var(--tx2);line-height:1.75}
</style>
</head>
<body>
<div class="layout">
<header class="topbar">
  <span class="logo">SHUXUE · CONSOLE</span>
  <div class="dot" id="qdot"></div>
  <span class="st" id="qst">QQ 未连接</span>
  <div class="sp"></div>
  <span class="be-tag" id="betag">-</span>
  <button class="btn-top warn" onclick="restartBot()">重启项目</button>
  <button class="btn-top danger" onclick="shutdownBot()">关闭项目</button>
  <button class="btn-top" onclick="logout()">退出</button>
</header>

<aside class="sidebar">
  <div class="tabs">
    <button class="tb on" data-tab="s">⚙ 设置</button>
    <button class="tb" data-tab="a">ⓘ 关于</button>
  </div>

  <!-- 设置 -->
  <div class="sbody" id="tab-s">
    <div class="fg">
      <label class="fl">AI 后端</label>
      <div class="rg">
        <div class="rb" data-v="deepseek" id="rb-ds">DeepSeek</div>
      </div>
    </div>
    <div id="sec-ds">
      <div class="fg">
        <label class="fl">DeepSeek API Key</label>
        <input type="password" id="deepseek_key" placeholder="sk-...">
      </div>
      <div class="fg">
        <label class="fl">模型 <span class="bg">deepseek-v4-pro</span></label>
        <input type="text" id="deepseek_model">
      </div>
      <div class="fg">
        <div class="fl-row">
          <label class="fl">Temperature</label>
          <div class="tip-wrap">
            <div class="tip-icon">?</div>
            <div class="tooltip">
              <span class="tt">TEMPERATURE</span>
              控制回复的随机性与创意度。<br>
              <b>值越高</b>（→2.0）回复越天马行空、有个性；<br>
              <b>值越低</b>（→0.0）回复越稳定保守、重复性高。<br>
              DeepSeek 推荐范围：<b>0.7 ~ 1.3</b><br>
              当前人设建议：<b>1.1 ~ 1.2</b>
            </div>
          </div>
        </div>
        <input type="number" id="deepseek_temperature" min="0" max="2" step="0.05">
      </div>
    </div>
    <hr class="dv">

    <!-- System Prompt：仅人设 -->
    <div class="fg">
      <label class="fl">人设 Prompt <span class="bg">BASE_PROMPT</span></label>
      <div class="persona-box">
        <div class="pb-label">✦ 可编辑区域</div>
        <textarea id="system_prompt" rows="12"></textarea>
      </div>
      <div class="sys-note">
        <b>以下内容由系统自动管理，无需填写：</b><br>
        · 当前时间 / 星期 / 时段状态（时空感知）<br>
        · 可用表情名库（<code>{{EMOTE_LIST}}</code> 占位符自动替换）
      </div>
    </div>

    <div class="fg">
      <div class="fl-row">
        <label class="fl">Max Tokens</label>
        <div class="tip-wrap">
          <div class="tip-icon">?</div>
          <div class="tooltip">
            <span class="tt">MAX TOKENS</span>
            单次回复允许的最大长度。<br>
            1 token ≈ 0.75 个中文字 / 0.5 个英文词。<br>
            <b>512</b> ≈ 约 380 字（适合简短对话）<br>
            <b>1024</b> ≈ 约 760 字（默认，均衡）<br>
            <b>2048</b> ≈ 约 1500 字（长回复/代码）<br>
            注：越大不代表越好，过大会让淑雪废话连篇。
          </div>
        </div>
      </div>
      <input type="number" id="max_tokens" min="64" max="8192" step="64">
    </div>

    <button class="btn-save" onclick="save()">保存设置</button>
    <div class="smsg" id="smsg"></div>
  </div>

  <!-- 关于 -->
  <div class="sbody" id="tab-a" style="display:none">
    <div class="ac"><h4>SHUXUE BOT CONSOLE</h4>
      <p>控制台与 Bot 同进程运行，修改设置立即生效，无需重启。<br>
         聊天消息以管理员（哥哥）身份进入完整记忆系统。</p>
    </div>
    <div class="ac"><h4>运行状态</h4>
      <div class="kv"><span class="k">AI 后端</span><span class="v" id="rt-be">-</span></div>
      <div class="kv"><span class="k">QQ 连接</span><span class="v" id="rt-qq">-</span></div>
      <div class="kv"><span class="k">ADMIN UID</span><span class="v" id="rt-uid">-</span></div>
      <div class="kv"><span class="k">控制台端口</span><span class="v" id="rt-port">-</span></div>
    </div>
    <div class="ac"><h4>设计说明</h4>
      <p>
        · <b>人设 Prompt</b>：仅控制角色性格、语言风格、规则。时间感知和表情库系统自动注入，不受此处影响。<br>
        · <b>Temperature</b>：影响创意度，悬停 ? 查看详情。<br>
        · <b>Max Tokens</b>：影响回复长度，悬停 ? 查看详情。<br>
        · <b>日志面板</b>：实时显示 Bot 运行日志，支持级别过滤。
      </p>
    </div>
  </div>
</aside>

<!-- 主区域 -->
<div class="main-area">
  <div class="main-tabs">
    <button class="mt-btn on" data-main="chat">💬 对话</button>
    <button class="mt-btn" data-main="log">📋 日志</button>
  </div>

  <!-- 对话面板 -->
  <div class="chat-wrap" id="main-chat">
    <div class="ch">
      <div class="av">雪</div>
      <div><div class="cn">淑雪</div><div class="cs">控制台 · 管理员模式</div></div>
      <div class="sp"></div>
      <button class="btn-clr" onclick="clearChat()">清空对话</button>
    </div>
    <div class="msgs" id="msgs"><div class="sys"><span>控制台已就绪 · 消息以哥哥身份发送</span></div></div>
    <div class="cia">
      <div class="iw">
        <textarea id="inp" placeholder="输入消息，Enter 发送..." rows="1"></textarea>
        <div class="ih">Enter 发送 · Shift+Enter 换行</div>
      </div>
      <button class="btn-s" id="bsend" onclick="sendMsg()">↑</button>
    </div>
  </div>

  <!-- 日志面板 -->
  <div class="log-wrap" id="main-log" style="display:none">
    <div class="log-toolbar">
      <span>实时日志</span>
      <div class="log-filter">
        <button class="lf on" data-lv="ALL"    onclick="setFilter(this,'ALL')">全部</button>
        <button class="lf" data-lv="INFO"       onclick="setFilter(this,'INFO')">INFO</button>
        <button class="lf l-suc" data-lv="SUCCESS" onclick="setFilter(this,'SUCCESS')">OK</button>
        <button class="lf l-warn" data-lv="WARN"   onclick="setFilter(this,'WARN')">WARN</button>
        <button class="lf l-err" data-lv="ERROR"   onclick="setFilter(this,'ERROR')">ERR</button>
        <button class="lf l-ai" data-lv="AI"       onclick="setFilter(this,'AI')">AI</button>
      </div>
      <div class="log-sp"></div>
      <button class="btn-log-clr" onclick="clearLogs()">清空</button>
    </div>
    <div class="log-scroll" id="log-scroll">
      <div class="log-empty" id="log-empty">等待日志...</div>
    </div>
  </div>
</div>
</div>

<script>
const SID='con_'+Math.random().toString(36).slice(2);
let busy=false, logFilter='ALL', lastLogId=0, logRows=[], autoScroll=true;
function ts(){return new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function safeCls(s){return String(s||'INFO').replace(/[^a-zA-Z0-9_-]/g,'_')}

// ── 主区 tab 切换 ──
document.querySelectorAll('.mt-btn').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('.mt-btn').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    document.getElementById('main-chat').style.display=b.dataset.main==='chat'?'flex':'none';
    document.getElementById('main-log').style.display=b.dataset.main==='log'?'flex':'none';
  };
});

// ── 左侧 tab 切换 ──
document.querySelectorAll('.tb').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('.tb').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    document.getElementById('tab-s').style.display=b.dataset.tab==='s'?'':'none';
    document.getElementById('tab-a').style.display=b.dataset.tab==='a'?'':'none';
  };
});

// ── 后端切换 ──
function setBackend(v){
  document.querySelectorAll('.rb').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
  document.getElementById('sec-ds').style.display=v==='deepseek'?'':'none';
  document.getElementById('betag').textContent='deepseek';
  document.getElementById('rt-be').textContent='deepseek';
}
document.querySelectorAll('.rb').forEach(b=>b.onclick=()=>setBackend(b.dataset.v));

// ── 加载配置 ──
async function loadCfg(){
  const r=await fetch('/console/api/config');const d=await r.json();
  document.getElementById('deepseek_key').value=d.deepseek_key||'';
  document.getElementById('deepseek_model').value=d.deepseek_model||'deepseek-v4-pro';
  document.getElementById('deepseek_temperature').value=d.deepseek_temperature??1.15;
  document.getElementById('system_prompt').value=d.system_prompt||'';
  document.getElementById('max_tokens').value=d.max_tokens||1024;
  document.getElementById('rt-uid').textContent=d.admin_uid||'-';
  document.getElementById('rt-port').textContent=d.console_port||'-';
  document.getElementById('rt-qq').textContent=d.qq_connected?'已连接':'未连接';
  const dot=document.getElementById('qdot'),qst=document.getElementById('qst');
  if(d.qq_connected){dot.classList.remove('off');qst.textContent='QQ 已连接';}
  else{dot.classList.add('off');qst.textContent='QQ 未连接';}
  setBackend('deepseek');
}

// ── 保存配置 ──
async function save(){
  const btn=document.querySelector('.btn-save'),msg=document.getElementById('smsg');
  const cfg={
    deepseek_key:document.getElementById('deepseek_key').value.trim(),
    deepseek_model:document.getElementById('deepseek_model').value.trim(),
    deepseek_temperature:parseFloat(document.getElementById('deepseek_temperature').value)||1.15,
    system_prompt:document.getElementById('system_prompt').value,
    max_tokens:parseInt(document.getElementById('max_tokens').value)||1024,
  };
  btn.textContent='保存中…';btn.disabled=true;
  try{
    const r=await fetch('/console/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    const d=await r.json();
    if(d.ok){msg.textContent='✓ 已保存，立即生效';msg.className='smsg ok';setBackend('deepseek');}
    else{msg.textContent='✗ '+(d.error||'失败');msg.className='smsg err';}
  }catch(e){msg.textContent='✗ '+e.message;msg.className='smsg err';}
  finally{btn.textContent='保存设置';btn.disabled=false;setTimeout(()=>{msg.textContent='';},3000);}
}

// ── 对话 ──
function scrollB(){const e=document.getElementById('msgs');e.scrollTop=e.scrollHeight}
function addMsg(role,text,isErr=false){
  const e=document.getElementById('msgs');const d=document.createElement('div');
  d.className='msg '+(role==='bot'?'b':'u');
  const bub=isErr?`<div class="bub err">${esc(text)}</div>`:`<div class="bub">${esc(text)}</div>`;
  d.innerHTML=`<div class="ma">${role==='bot'?'雪':'我'}</div><div class="mb">${bub}<div class="mt-ts">${ts()}</div></div>`;
  e.appendChild(d);scrollB();
}
function showTyping(){
  const e=document.getElementById('msgs');const d=document.createElement('div');
  d.id='typing';d.className='msg b';
  d.innerHTML='<div class="ma">雪</div><div class="mb"><div class="bub"><div class="typing"><div class="td"></div><div class="td"></div><div class="td"></div></div></div></div>';
  e.appendChild(d);scrollB();
}
function hideTyping(){document.getElementById('typing')?.remove()}
async function sendMsg(){
  if(busy)return;
  const inp=document.getElementById('inp');const txt=inp.value.trim();if(!txt)return;
  inp.value='';inp.style.height='';busy=true;document.getElementById('bsend').disabled=true;
  addMsg('user',txt);showTyping();
  try{
    const r=await fetch('/console/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:SID,message:txt})});
    const d=await r.json();hideTyping();
    if(d.ok){addMsg('bot',d.reply);}else{addMsg('bot','错误：'+(d.error||'未知'),true);}
  }catch(e){hideTyping();addMsg('bot','网络错误：'+e.message,true);}
  finally{busy=false;document.getElementById('bsend').disabled=false;inp.focus();}
}
async function clearChat(){
  await fetch('/console/api/chat/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:SID})});
  document.getElementById('msgs').innerHTML='<div class="sys"><span>对话已清空 · '+ts()+'</span></div>';
}
document.getElementById('inp').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}});
document.getElementById('inp').addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,130)+'px';});

// ── 日志 ──
const LV_COLOR={'INFO':'var(--acc)','SUCCESS':'var(--grn)','WARN':'var(--ylw)','ERROR':'var(--red)','AI':'var(--pur)','SOC':'var(--acc2)'};
function setFilter(btn,lv){
  document.querySelectorAll('.lf').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');logFilter=lv;renderLogs();
}
function renderLogs(){
  const el=document.getElementById('log-scroll');
  const rows=logFilter==='ALL'?logRows:logRows.filter(r=>r.level===logFilter);
  if(rows.length===0){el.innerHTML='<div class="log-empty">暂无日志</div>';return;}
  el.innerHTML=rows.map(r=>{
    const level=esc(r.level||'INFO'), cls=safeCls(r.level), tag=esc(r.tag||'SYS');
    return `
    <div class="log-row lv-${cls}" data-id="${r.id||0}">
      <span class="lr-ts">${esc(r.time||'--:--:--')}<em>#${r.id||0}</em></span>
      <span class="lr-lv">${level}</span>
      <span class="lr-tag" title="[${tag}]">[${tag}]</span>
      <span class="lr-msg">${esc(r.msg)}</span>
    </div>`;
  }).join('');
  if(autoScroll)el.scrollTop=el.scrollHeight;
}
function clearLogs(){logRows=[];renderLogs();}
async function pollLogs(){
  try{
    const r=await fetch('/console/api/logs?since='+lastLogId);
    const d=await r.json();
    if(d.entries&&d.entries.length){
      const el=document.getElementById('log-scroll');
      const wasBottom=el.scrollHeight-el.scrollTop-el.clientHeight<30;
      for(const e of d.entries){logRows.push(e);if(logRows.length>1200)logRows.shift();}
      lastLogId=d.last_id;
      autoScroll=wasBottom;
      renderLogs();
      document.getElementById('log-empty')?.remove();
    }
  }catch{}
}

// ── 状态轮询 ──
async function pollStatus(){
  try{
    const d=await(await fetch('/console/api/config')).json();
    const dot=document.getElementById('qdot'),qst=document.getElementById('qst');
    if(d.qq_connected){dot.classList.remove('off');qst.textContent='QQ 已连接';}
    else{dot.classList.add('off');qst.textContent='QQ 未连接';}
    document.getElementById('rt-qq').textContent=d.qq_connected?'已连接':'未连接';
  }catch{}
}
async function logout(){
  await fetch('/console/logout',{method:'POST'});
  location.href='/console/login';
}
async function processAction(action){
  const label=action==='restart'?'重启项目':'关闭项目';
  if(!confirm(`确定要${label}吗？`)) return;
  try{
    const r=await fetch(`/console/api/process/${action}`,{method:'POST'});
    const d=await r.json();
    if(d.ok){
      addMsg('bot',`【系统】${d.message}`);
      if(action==='shutdown'){
        setTimeout(()=>{document.body.innerHTML='<div style="padding:30px;color:#bcc8e0;font-family:monospace">SHUXUE BOT 已关闭</div>';},700);
      }else{
        setTimeout(()=>location.reload(),2500);
      }
    }else{
      addMsg('bot','错误：'+(d.error||'操作失败'),true);
    }
  }catch(e){
    addMsg('bot',`【系统】${label}指令已发送，连接即将中断。`);
  }
}
function restartBot(){processAction('restart')}
function shutdownBot(){processAction('shutdown')}

// UI patches kept last so they override older inline handlers after page load.
const expandedLogs=new Set();
function applyQQState(d){
  const ok=!!(d&&d.qq_connected);
  const dot=document.getElementById('qdot'),qst=document.getElementById('qst'),rt=document.getElementById('rt-qq');
  if(dot)dot.classList.toggle('off',!ok);
  if(qst)qst.textContent=ok?'QQ 已连接':'QQ 未连接';
  if(rt)rt.textContent=ok?'已连接':'未连接';
}
loadCfg=async function(){
  const r=await fetch('/console/api/config');const d=await r.json();
  document.getElementById('deepseek_key').value=d.deepseek_key||'';
  document.getElementById('deepseek_model').value=d.deepseek_model||'deepseek-v4-pro';
  document.getElementById('deepseek_temperature').value=d.deepseek_temperature??1.15;
  document.getElementById('system_prompt').value=d.system_prompt||'';
  document.getElementById('max_tokens').value=d.max_tokens||1024;
  document.getElementById('rt-uid').textContent=d.admin_uid||'-';
  document.getElementById('rt-port').textContent=d.console_port||'-';
  applyQQState(d);
  setBackend('deepseek');
};
pollStatus=async function(){
  try{applyQQState(await(await fetch('/console/api/config')).json());}catch{}
};
function toggleLog(id){
  id=String(id);
  if(expandedLogs.has(id))expandedLogs.delete(id);else expandedLogs.add(id);
  renderLogs();
}
renderLogs=function(){
  const el=document.getElementById('log-scroll');
  const rows=logFilter==='ALL'?logRows:logRows.filter(r=>r.level===logFilter);
  if(rows.length===0){el.innerHTML='<div class="log-empty">暂无日志</div>';return;}
  el.innerHTML=rows.map(r=>{
    const level=esc(r.level||'INFO'), cls=safeCls(r.level), tag=esc(r.tag||'SYS');
    const id=String(r.id||0), msg=String(r.msg??''), isLong=msg.length>260||msg.split('\n').length>4;
    const open=expandedLogs.has(id), btn=isLong?`<button class="log-more" type="button" data-log-toggle="${id}">${open?'收起':'展开'}</button>`:'';
    return `
    <div class="log-row lv-${cls} ${isLong?'long':''} ${open?'open':''}" data-id="${id}">
      <span class="lr-ts">${esc(r.time||'--:--:--')}<em>#${id}</em></span>
      <span class="lr-lv">${level}</span>
      <span class="lr-tag" title="[${tag}]">[${tag}]</span>
      <span class="log-cell"><span class="lr-msg">${esc(msg)}</span>${btn}</span>
    </div>`;
  }).join('');
  if(autoScroll)el.scrollTop=el.scrollHeight;
};
document.getElementById('log-scroll').addEventListener('click',e=>{
  const btn=e.target.closest('[data-log-toggle]');
  if(!btn)return;
  e.preventDefault();
  e.stopPropagation();
  toggleLog(btn.dataset.logToggle);
});

loadCfg();
setInterval(pollLogs,1500);
setInterval(pollStatus,5000);
document.getElementById('inp').focus();
</script>
</body>
</html>"""

_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>淑雪 · 登录</title>
<style>
:root{--bg:#0c0e14;--panel:#12151e;--field:#181c28;--bd:#222736;--acc:#7eb8f7;--red:#f77e7e;--tx:#bcc8e0;--muted:#58637a}
*{box-sizing:border-box}html,body{height:100%;margin:0;background:var(--bg);color:var(--tx);font-family:"Segoe UI","Microsoft YaHei",sans-serif}
body{display:grid;place-items:center}
.box{width:min(360px,calc(100vw - 32px));background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:24px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.logo{font:700 13px ui-monospace,Consolas,monospace;letter-spacing:.12em;color:var(--acc);margin-bottom:8px}
.sub{font-size:12px;color:var(--muted);margin-bottom:22px}
label{display:block;font:700 10px ui-monospace,Consolas,monospace;letter-spacing:.1em;color:var(--muted);margin:14px 0 6px}
input{width:100%;background:var(--field);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:10px 12px;outline:none}
input:focus{border-color:var(--acc)}
button{width:100%;margin-top:18px;padding:10px;border:1px solid var(--acc);border-radius:6px;background:rgba(126,184,247,.12);color:var(--acc);font-weight:700;cursor:pointer}
button:hover{background:rgba(126,184,247,.2)}
.err{min-height:18px;margin-top:12px;color:var(--red);font-size:12px;text-align:center}
</style>
</head>
<body>
<form class="box" method="post" action="/console/login">
  <div class="logo">SHUXUE · CONSOLE</div>
  <div class="sub">请输入控制台账号密码</div>
  <label>USERNAME</label>
  <input name="username" autocomplete="username" autofocus>
  <label>PASSWORD</label>
  <input name="password" type="password" autocomplete="current-password">
  <button type="submit">登录</button>
  <div class="err">{{ERROR}}</div>
</form>
</body>
</html>"""


if _FLASK_AVAILABLE:
    from .shared import (
        _Flask, _flask_request, _jsonify, _threading,
        _flask_session, _flask_redirect,
    )

    class ConsoleServer:
        """
        Web 控制台：与 Bot 同进程运行，通过 run_coroutine_threadsafe
        把聊天请求投进 Bot 的 asyncio 事件循环。
        修改设置直接改 GlobalConfig / PersonalityCore 类属性，立即对新消息生效。
        """
        def __init__(self, bot_instance):
            self.bot = bot_instance
            self.app = _Flask(__name__)
            self.app.secret_key = GlobalConfig.CONSOLE_SESSION_SECRET or secrets.token_hex(32)
            self.app.logger.disabled = True
            import logging as _lg
            _lg.getLogger("werkzeug").setLevel(_lg.ERROR)
            self._setup()

        def _setup(self):
            app = self.app

            def _is_logged_in() -> bool:
                return bool(_flask_session.get("console_authed"))

            def _wants_json() -> bool:
                return _flask_request.path.startswith("/console/api/")

            @app.before_request
            def require_login():
                path = _flask_request.path.rstrip("/")
                if path in ("/console/login",):
                    return None
                if path.startswith("/console") and not _is_logged_in():
                    if _wants_json():
                        return _jsonify({"ok": False, "error": "未登录"}), 401
                    return _flask_redirect("/console/login")
                return None

            @app.errorhandler(Exception)
            def handle_console_error(exc):
                try:
                    from werkzeug.exceptions import HTTPException
                    if isinstance(exc, HTTPException):
                        return exc
                except Exception:
                    pass
                audit.log("ERROR", "CONSOLE", f"WebUI error:\n{traceback.format_exc()}")
                if _wants_json():
                    return _jsonify({"ok": False, "error": str(exc)}), 500
                return "WebUI internal error", 500

            @app.route("/console/login", methods=["GET", "POST"])
            def login():
                if _flask_request.method == "GET":
                    if _is_logged_in():
                        return _flask_redirect("/console/")
                    return _LOGIN_HTML.replace("{{ERROR}}", "")
                username = str(_flask_request.form.get("username") or "")
                password = str(_flask_request.form.get("password") or "")
                ok_user = hmac.compare_digest(username, GlobalConfig.CONSOLE_USERNAME)
                ok_pass = hmac.compare_digest(password, GlobalConfig.CONSOLE_PASSWORD)
                if ok_user and ok_pass and GlobalConfig.CONSOLE_USERNAME and GlobalConfig.CONSOLE_PASSWORD:
                    _flask_session.clear()
                    _flask_session["console_authed"] = True
                    return _flask_redirect("/console/")
                return _LOGIN_HTML.replace("{{ERROR}}", "账号或密码错误"), 401

            @app.route("/console/logout", methods=["POST", "GET"])
            def logout():
                _flask_session.clear()
                if _flask_request.method == "POST":
                    return _jsonify({"ok": True})
                return _flask_redirect("/console/login")

            @app.route("/console/")
            @app.route("/console")
            def index():
                return _CONSOLE_HTML

            @app.route("/console/api/config", methods=["GET"])
            def cfg_get():
                return _jsonify(self._read_cfg())

            @app.route("/console/api/config", methods=["POST"])
            def cfg_post():
                data = _flask_request.get_json(force=True)
                bot_loop = shared.get_bot_loop()
                if bot_loop:
                    future = asyncio.run_coroutine_threadsafe(
                        self._apply_cfg_on_bot_loop(data),
                        bot_loop,
                    )
                    try:
                        err = future.result(timeout=10)
                    except Exception as e:
                        return _jsonify({"ok": False, "error": str(e)}), 500
                else:
                    err = self._apply_cfg(data)
                if err:
                    return _jsonify({"ok": False, "error": err}), 400
                return _jsonify({"ok": True})

            @app.route("/console/api/chat", methods=["POST"])
            def chat():
                bot_loop = shared.get_bot_loop()
                if not bot_loop:
                    return _jsonify({"ok": False, "error": "Bot 事件循环尚未启动"}), 503
                data = _flask_request.get_json(force=True)
                msg  = (data.get("message") or "").strip()
                if not msg:
                    return _jsonify({"ok": False, "error": "消息不能为空"}), 400

                coro = BrainInterpreter.process_interaction(
                    tid          = int(GlobalConfig.ADMIN_UID),
                    raw_input    = msg,
                    nickname     = "哥哥",
                    target_type  = "private",
                    bot_instance = self.bot,
                    sender_uid   = int(GlobalConfig.ADMIN_UID),
                )
                future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
                try:
                    responses = future.result(timeout=60)
                except asyncio.TimeoutError:
                    return _jsonify({"ok": False, "error": "AI 响应超时（>60s）"}), 504
                except Exception as e:
                    return _jsonify({"ok": False, "error": str(e)}), 500

                clean = []
                for r in (responses or []):
                    r = re.sub(r"\[CQ:image[^\]]*\]", "[图片]", r)
                    r = re.sub(r"\[CQ:[^\]]*\]", "", r).strip()
                    if r:
                        clean.append(r)
                return _jsonify({"ok": True, "reply": "\n".join(clean) if clean else "……"})

            @app.route("/console/api/chat/clear", methods=["POST"])
            def chat_clear():
                return _jsonify({"ok": True})

            @app.route("/console/api/process/restart", methods=["POST"])
            def process_restart():
                self._schedule_process_action("restart")
                return _jsonify({"ok": True, "message": "重启指令已接收，进程即将重新启动。"})

            @app.route("/console/api/process/shutdown", methods=["POST"])
            def process_shutdown():
                self._schedule_process_action("shutdown")
                return _jsonify({"ok": True, "message": "关闭指令已接收，进程即将退出。"})

            @app.route("/console/api/logs", methods=["GET"])
            def logs():
                try:
                    since = int(_flask_request.args.get("since", 0))
                except (TypeError, ValueError):
                    since = 0
                entries = [e for e in list(_console_log_buffer) if e["id"] > since]
                last_id = entries[-1]["id"] if entries else since
                return _jsonify({"entries": entries, "last_id": last_id})

        def _read_cfg(self) -> dict:
            personality_cfg = shared._CONFIG.get("personality")
            configured_prompt = (
                personality_cfg.get("base_prompt")
                if isinstance(personality_cfg, dict) else None
            )
            return {
                "deepseek_key":         GlobalConfig.DEEPSEEK_KEY,
                "deepseek_model":       GlobalConfig.DEEPSEEK_MODEL,
                "deepseek_temperature": GlobalConfig.DEEPSEEK_TEMPERATURE,
                "max_tokens":           GlobalConfig.MAX_TOKENS,
                "system_prompt":        configured_prompt or PersonalityCore.BASE_PROMPT,
                "admin_uid":            GlobalConfig.ADMIN_UID,
                "console_port":         GlobalConfig.CONSOLE_PORT,
                "qq_connected":         bool(self.bot and self.bot.is_qq_connected()),
            }

        def _apply_cfg(self, data: dict):
            try:
                if "deepseek_key"         in data: GlobalConfig.DEEPSEEK_KEY         = str(data["deepseek_key"]).strip()
                if "deepseek_model"       in data: GlobalConfig.DEEPSEEK_MODEL       = str(data["deepseek_model"]).strip() or "deepseek-v4-pro"
                if "deepseek_temperature" in data: GlobalConfig.DEEPSEEK_TEMPERATURE = float(data["deepseek_temperature"])
                if "max_tokens"           in data: GlobalConfig.MAX_TOKENS           = int(data["max_tokens"])
                if "system_prompt"        in data:
                    PersonalityCore.BASE_PROMPT = str(data["system_prompt"])
                    if hasattr(PersonalityCore, "_SYS_ENV_INFO"):
                        PersonalityCore._SYS_ENV_INFO = ""
                self._persist_cfg(data)
                audit.log("SUCCESS", "CONSOLE", f"配置已保存: model={GlobalConfig.DEEPSEEK_MODEL}")
                return None
            except Exception as e:
                return str(e)

        def _persist_cfg(self, data: dict):
            cfg = copy.deepcopy(shared._CONFIG)
            cfg.setdefault("api", {})
            cfg.setdefault("models", {})
            if "deepseek_key" in data:
                cfg["api"]["deepseek_key"] = str(data["deepseek_key"]).strip()
            if "deepseek_model" in data:
                cfg["models"]["deepseek"] = str(data["deepseek_model"]).strip() or "deepseek-v4-pro"
            if "deepseek_temperature" in data:
                cfg["models"]["deepseek_temperature"] = float(data["deepseek_temperature"])
            if "max_tokens" in data:
                cfg["models"]["max_tokens"] = int(data["max_tokens"])
            if "system_prompt" in data:
                cfg.setdefault("personality", {})
                cfg["personality"]["base_prompt"] = str(data["system_prompt"])
            shared.save_config_patch(cfg)

        async def _apply_cfg_on_bot_loop(self, data: dict):
            """在 Bot 主事件循环中应用配置，避免跨线程直接修改状态。"""
            return self._apply_cfg(data)

        def _schedule_process_action(self, action: str):
            def _runner():
                time.sleep(0.8)
                if action == "restart":
                    audit.log("WARN", "CONSOLE", "Web 控制台请求重启项目。")
                    try:
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                    except Exception:
                        audit.log("ERROR", "CONSOLE", f"重启失败:\n{traceback.format_exc()}")
                        os._exit(1)
                elif action == "shutdown":
                    audit.log("WARN", "CONSOLE", "Web 控制台请求关闭项目。")
                    os._exit(0)

            t = _threading.Thread(
                target=_runner,
                daemon=True,
                name=f"ConsoleProcess{action.title()}",
            )
            t.start()

        def start(self):
            port = GlobalConfig.CONSOLE_PORT
            t = _threading.Thread(
                target=lambda: self.app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
                daemon=True, name="ConsoleFlask"
            )
            t.start()
            audit.log("SUCCESS", "CONSOLE", f"Web 控制台已启动 → http://localhost:{port}/console/")
            print(f"\033[32m  控制台地址: http://localhost:{port}/console/\033[0m")

else:
    class ConsoleServer:
        def __init__(self, bot_instance): pass

        def start(self): audit.log("WARN", "CONSOLE", "flask 未安装，控制台已禁用。pip install flask")


# ==========================================
# 10. 淑雪通讯宿主
# ==========================================

