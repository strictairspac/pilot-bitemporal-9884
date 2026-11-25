import { api } from "../api/workbench.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { progress, status } from "../components/common/status.js";
import { table } from "../components/common/table.js";
import { poll } from "../hooks/poll.js";
import { el } from "../utils/dom.js";
import { time } from "../utils/format.js";

const ACTIVE = new Set(["queued", "running"]);

function reportState(run) {
  return el("div", {class: "stack", id: "run-state"}, status(run.status),
    el("p", {}, ACTIVE.has(run.status) ? "后台正在分段复算，页面会自动更新。" : run.failure || run.result?.summary),
    progress(run),
    run.status === "done" ? el("a", {class: "button", href: `#evidence/${run.id}`}, "查看业务证据") : null);
}

function active(run) {
  return run && ACTIVE.has(run.status);
}

export async function runsPage(config, route, signal) {
  const items = await api.runs();
  const chosen = route.id ? route.id : items[0]?.id;
  // The list projection omits the full result payload; load the chosen batch in full.
  const latest = chosen ? await api.run(chosen, signal) : null;
  const detail = latest ? reportState(latest) : empty("进入资料页，选用一个业务样本并提交执行。");
  async function refresh() {
    try {
      const next = await api.run(latest.id, signal);
      if (!signal.aborted) {
        detail.replaceChildren(...reportState(next).childNodes);
        const badge = document.querySelector(`.status[data-run="${next.id}"]`);
        if (badge) badge.replaceWith(status(next.status));
      }
      return active(next);
    } catch (error) {
      if (!signal.aborted) detail.append(el("p", {role: "alert"}, error.message));
      return false;
    }
  }
  if (active(latest)) poll(refresh, signal);
  return el("div", {class: "page"},
    pageHeading(config.modules[2], config.parameter_label),
    latest ? section(latest.case_title, detail) : detail,
    section("执行批次", items.length ? table(["资料", "编号", "参数", "状态", "进度", "提交时间"],
      items.map(run => [el("a", {"data-run": run.id, href: `#runs/${run.id}`}, run.case_title), run.id,
        run.parameter, status(run.status, run.id),
        run.total_segments ? `${run.done_trips}/${run.total_trips}` : "—", time(run.created_at)]), "执行批次") :
      empty("暂无执行记录。")));
}
