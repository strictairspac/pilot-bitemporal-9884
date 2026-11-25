import { States } from "../../constants/states.js";
import { el } from "../../utils/dom.js";

export function status(value, id) {
  return el("span", {class: `status state-${value}`, "data-state": value, "data-run": id}, States[value] || value);
}

export function progress(run) {
  const total = run.total_trips || 0;
  const done = run.done_trips || 0;
  if (!run.total_segments) return null;
  return el("p", {class: "progress", "data-progress": `${done}/${total}`},
    `已复算 ${done} / ${total} 个航次（${run.done_segments}/${run.total_segments} 段）`);
}
