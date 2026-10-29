let threadId = null, lastMd = "", gotFirst = false;

function fmt(s) { return s + "s"; }
function show(id) { document.getElementById(id).classList.remove("hide"); }
function status(text) { show("status"); document.getElementById("status-text").textContent = text; }
function done() { document.getElementById("status").classList.add("hide"); setBtn(false); }
function setBtn(b) { document.getElementById("btn-start").disabled = b; }

function addEvent(label, html, elapsed) {
  const div = document.createElement("div");
  div.className = "ev";
  div.innerHTML = `<span class="tag">${label}</span><span class="t">${fmt(elapsed ?? "")}</span><span class="body">${html}</span>`;
  const tl = document.getElementById("timeline");
  tl.appendChild(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
}

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

function handle(data) {
  if (data.type === "start") {
    threadId = data.thread_id;
    status("研究中…（" + esc(data.topic) + "）");
    addEvent("🧵 研究线程", "<code>" + esc(threadId) + "</code>", "");
  } else if (data.type === "node_done") {
    let body = "";
    if (data.node === "coordinator") body = "生成研究任务书：<i>" + esc(data.brief.slice(0, 80)) + "…</i>";
    else if (data.node === "planner") {
      body = (data.replan_count
        ? "第 " + data.replan_count + " 轮重规划，针对缺口新增 "
        : "拆解出 ") + data.subquestions.length + " 个子问题：<ul>" +
        data.subquestions.map(s => "<li>" + esc(s) + "</li>").join("") + "</ul>";
      if (data.memory_reused && data.memory_reused.length) {
        body += "🧠 复用历史研究 " + data.memory_reused.length + " 条（未重新检索）：<ul>" +
          data.memory_reused.map(h => "<li>" + esc(h.subquestion) + "（相似度 " + h.similarity +
            "，" + h.age_days + " 天前）</li>").join("") + "</ul>";
      }
    }
    else if (data.node === "researcher") body = "完成「" + esc(data.subquestion) + "」（" + data.n_sources + " 个来源）";
    else if (data.node === "compactor") body = data.compaction && data.compaction.applied
      ? "笔记压缩 " + data.compaction.before_chars + "→" + data.compaction.after_chars + " 字（" + data.compaction.ratio + "×，来源列表不变）"
      : "笔记长度未超阈值，跳过压缩";
    else if (data.node === "verifier") body = (data.conflicts && data.conflicts.length
      ? "发现 " + data.conflicts.length + " 处来源冲突：<ul>" + data.conflicts.slice(0, 3).map(c => "<li>" + esc(c) + "</li>").join("") + "</ul>"
      : "交叉比对完成，未发现明显冲突")
      + (data.gaps && data.gaps.length ? "另有 " + data.gaps.length + " 处待补缺口（将触发重规划）" : "");
    else if (data.node === "writer") body = "报告撰写完成";
    else if (data.node === "archivist") body = data.memory_written
      ? "落档 " + data.memory_written + " 条笔记到研究档案（下次同题研究可复用）" : "无新增笔记可落档";
    addEvent(data.label, body, data.elapsed_s);
  } else if (data.type === "interrupt") {
    document.getElementById("review-title").textContent =
      "⏸ human-in-the-loop：请审核" + (data.title || "内容");
    document.getElementById("brief-view").textContent = data.content || "";
    document.getElementById("brief-edit").value = data.content || "";
    show("review-panel");
    done();
  } else if (data.type === "report") {
    lastMd = data.markdown;
    document.getElementById("report").innerHTML = data.html;
    const s = data.stats;
    const budgetChip = s.budget_exceeded
      ? "<span class='chip warn-chip'>⚠ 触发预算上限（" + esc(s.budget_exceeded) + "）</span>"
      : "<span class='chip'>剩余额度 " + s.remaining.llm_calls_left + " 次调用 / " + s.remaining.seconds_left + "s</span>";
    const compactChip = s.compaction && s.compaction.applied
      ? "<span class='chip'>笔记压缩 " + s.compaction.before_chars + "→" + s.compaction.after_chars + " 字</span>" : "";
    const degradedChip = (s.degradations && s.degradations.length)
      ? "<span class='chip warn-chip'>⚠ 依赖降级 " + s.degradations.length + " 次</span>" : "";
    const audit = s.citation_audit || {};
    const auditChip = (audit.invalid_citations || audit.unknown_links)
      ? "<span class='chip warn-chip'>⚠ 引用校验移除 " +
        (audit.invalid_citations || 0) + " 引用 / " + (audit.unknown_links || 0) + " 链接</span>" : "";
    const memoryChip = s.memory_reused
      ? "<span class='chip'>🧠 复用历史研究 " + s.memory_reused + " 条</span>"
      : (s.memory_written ? "<span class='chip'>📝 落档 " + s.memory_written + " 条</span>" : "");
    document.getElementById("stats").innerHTML = [
      "子问题 <b>" + s.subquestions + "</b>", "笔记 <b>" + s.notes + "</b>",
      "LLM 调用 <b>" + s.llm_calls + "</b>", "tokens <b>" + s.input_tokens + " + " + s.output_tokens + "</b>",
      "成本估算 <b>$" + s.cost_usd_est + "</b>", "耗时 <b>" + fmt(s.elapsed_s) + "</b>",
    ].map(c => `<span class="chip">${c}</span>`).join("") + budgetChip + compactChip + memoryChip + degradedChip + auditChip;
    show("report-panel");
    done();
  } else if (data.type === "error") {
    addEvent("❌ 出错", `<span class="err">${esc(data.message)}</span>`, "");
    done();
  }
}

function authHeaders() {
  const token = document.getElementById("auth-token").value.trim();
  return token ? { Authorization: "Bearer " + token } : {};
}

async function recoverReport() {
  if (lastMd || !threadId) return false;
  try {
    const resp = await fetch("/api/report/" + encodeURIComponent(threadId), {
      headers: authHeaders(),
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    if (!data.report) return false;
    lastMd = data.report;
    document.getElementById("report").innerHTML =
      data.html || "<pre>" + esc(data.report) + "</pre>";
    show("report-panel");
    addEvent("📄 报告已恢复", "SSE 已结束，已从 checkpoint 取回报告", "");
    return true;
  } catch (_) {
    return false;
  }
}

function listen(url, body) {
  setBtn(true);
  show("timeline-panel");
  gotFirst = false;
  (async () => {
    let resp;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
        body: JSON.stringify(body),
      });
    } catch (e) {
      addEvent("❌ 请求失败", `<span class='err'>${esc(e.message || "网络错误")}</span>`, "");
      done();
      return;
    }
    if (!resp.ok) {
      let msg = "HTTP " + resp.status;
      try {
        const j = await resp.json();
        if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch (_) {}
      addEvent("❌ 请求失败", `<span class='err'>${esc(msg)}</span>`, "");
      done();
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (!chunk) continue;
          const line = chunk.split("\n").find(l => l.startsWith("data: "));
          if (!line) continue;
          gotFirst = true;
          handle(JSON.parse(line.slice(6)));
        }
      }
    } catch (e) {
      addEvent("❌ 连接中断", `<span class='err'>${esc(e.message || "读取响应失败")}</span>`, "");
    } finally {
      if (!lastMd) {
        const recovered = await recoverReport();
        if (!recovered && threadId) {
          addEvent("⚠ 报告暂未取到", "线程 " + esc(threadId) + " 仍在收尾，可稍后刷新重试", "");
        }
      }
      done();
    }
  })();
}

function updateScaleHint() {
  const sub = parseInt(document.getElementById("max-sub").value, 10);
  const rnd = parseInt(document.getElementById("rounds").value, 10);
  const hint = document.getElementById("scale-hint");
  if (!(sub >= 1) || !(rnd >= 1)) { hint.textContent = "子问题上限与检索轮数都需 ≥ 1"; return; }
  const est = sub * rnd;
  const budget = parseInt(document.body.dataset.maxLlmCalls, 10) || 0;
  hint.textContent = "预计笔记阶段 ≈ " + est + " 次 LLM 调用（预算 " + budget + " 次）"
    + (est > budget ? "　⚠ 可能提前触发预算上限，报告会标注研究受限" : "");
}

function startResearch() {
  const topic = document.getElementById("topic").value.trim();
  if (!topic) { alert("请输入研究主题"); return; }
  ["timeline-panel", "review-panel", "report-panel"].forEach(id => document.getElementById(id).classList.add("hide"));
  document.getElementById("timeline").innerHTML = "";
  const points = [];
  if (document.getElementById("review-brief").checked) points.push("brief");
  if (document.getElementById("review-plan").checked) points.push("plan");
  const maxSub = parseInt(document.getElementById("max-sub").value, 10);
  const rounds = parseInt(document.getElementById("rounds").value, 10);
  if (!(maxSub >= 1) || !(rounds >= 1)) { alert("子问题上限与检索轮数都需 ≥ 1"); return; }
  listen("/api/research/stream", { topic: topic, points: points, max_sub: maxSub, rounds: rounds });
}

function toggleEdit() {
  document.getElementById("brief-view").classList.toggle("hide");
  document.getElementById("brief-edit").classList.toggle("hide");
  document.getElementById("btn-resume").classList.toggle("hide");
}

function resume(value) {
  document.getElementById("review-panel").classList.add("hide");
  listen("/api/resume/stream", { thread_id: threadId, value: value });
}

function resumeFromEdit() { resume(document.getElementById("brief-edit").value); }

function copyMd() { navigator.clipboard.writeText(lastMd); }

document.getElementById("btn-start").addEventListener("click", startResearch);
document.getElementById("max-sub").addEventListener("input", updateScaleHint);
document.getElementById("rounds").addEventListener("input", updateScaleHint);
document.getElementById("btn-confirm").addEventListener("click", () => resume(""));
document.getElementById("btn-edit").addEventListener("click", toggleEdit);
document.getElementById("btn-resume").addEventListener("click", resumeFromEdit);
document.getElementById("btn-copy").addEventListener("click", copyMd);

updateScaleHint();
