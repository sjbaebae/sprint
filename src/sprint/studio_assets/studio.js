const $ = (id) => document.getElementById(id);
const key = "sprint-studio-draft-v1";
$("builder").addEventListener("submit", (event) => event.preventDefault());
const placeholder =
  "def verify(context):\n    # Define independent checks for the submitted artifact.\n    # Never execute submitted code beside hidden tests.\n    raise NotImplementedError('Add task-specific verification')\n";
const defaults = {
  name: "",
  objective: "",
  minutes: 60,
  actions: 200,
  internet: "full",
  model: "gpt-6-astra",
  harness: "bounded-codex",
  evaluation: "custom",
  expected: "42",
  verifier: placeholder,
  files: {},
};
let draft = { ...defaults },
  active = "task";
try {
  draft = { ...defaults, ...JSON.parse(localStorage.getItem(key) || "{}") };
} catch {}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("toast").hidden = true), 5500);
}
function save() {
  try {
    localStorage.setItem(key, JSON.stringify(draft));
    $("save-state").textContent = "Draft saved locally";
  } catch {
    $("save-state").textContent = "Draft not saved";
  }
}
function update() {
  $("breadcrumb-name").textContent = draft.name || "Untitled environment";
  $("summary-model").textContent =
    draft.model === "gpt-6-astra" ? "Astra · Codex" : draft.model;
  $("summary-budget").textContent =
    `${draft.minutes} min · ${draft.harness === "bounded-codex" ? draft.actions + " actions" : "harness limits"}`;
  $("summary-internet").textContent =
    draft.internet === "full" ? "Full access" : "Disabled";
  $("summary-evaluation").textContent =
    draft.evaluation === "exact" ? "Exact answer" : "Custom verifier";
  $("exact-fields").hidden = draft.evaluation !== "exact";
  $("custom-fields").hidden = draft.evaluation !== "custom";
  $("file-count").textContent = Object.keys(draft.files).length;
  const checks = {
    "check-task": Boolean(draft.name && draft.objective.trim()),
    "check-eval":
      draft.evaluation === "exact" ||
      Boolean(
        draft.verifier.trim() &&
        !draft.verifier.includes("NotImplementedError"),
      ),
  };
  for (const [id, ok] of Object.entries(checks)) {
    $(id).classList.toggle("done", ok);
    $(id).querySelector("span").textContent = ok ? "✓" : "○";
  }
  const list = $("workspace-files");
  list.replaceChildren();
  for (const name of Object.keys(draft.files)) {
    const row = document.createElement("div"),
      text = document.createElement("span"),
      remove = document.createElement("button");
    text.textContent = name;
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Remove " + name);
    remove.onclick = () => {
      delete draft.files[name];
      save();
      update();
    };
    row.append(text, remove);
    list.append(row);
  }
}
for (const name of Object.keys(defaults).filter((x) => x !== "files")) {
  $(name).value = draft[name];
  $(name).addEventListener("input", () => {
    draft[name] = ["minutes", "actions"].includes(name)
      ? Number($(name).value)
      : $(name).value;
    save();
    update();
  });
}
function panel(name) {
  active = name;
  document
    .querySelectorAll("[data-content]")
    .forEach((el) => (el.hidden = el.dataset.content !== name));
  document.querySelectorAll("[data-panel]").forEach((el) => {
    const selected = el.dataset.panel === name;
    el.classList.toggle("selected", selected);
    if (el.hasAttribute("role")) el.setAttribute("aria-selected", selected);
  });
  const sections = ["task", "workspace", "evaluation", "constraints"],
    next = sections[sections.indexOf(name) + 1];
  $("next").textContent = next
    ? "Next: " + next[0].toUpperCase() + next.slice(1) + " →"
    : "Review environment →";
}
document
  .querySelectorAll("[data-panel]")
  .forEach((el) => (el.onclick = () => panel(el.dataset.panel)));
$("next").onclick = () => {
  const sections = ["task", "workspace", "evaluation", "constraints"];
  const next = sections[sections.indexOf(active) + 1];
  if (next) panel(next);
  else preview();
};
document.querySelectorAll("[data-template]").forEach(
  (el) =>
    (el.onclick = () => {
      if (
        (draft.objective || Object.keys(draft.files).length) &&
        !window.confirm("Replace this draft with the example?")
      )
        return;
      draft = { ...defaults, files: {} };
      if (el.dataset.template === "answer") {
        Object.assign(draft, {
          name: "first-environment",
          objective:
            "Compute 6 × 7. Write the answer to /app/submission/answer.txt.",
          evaluation: "exact",
        });
      } else {
        Object.assign(draft, {
          name: "incremental-parser",
          objective:
            "Improve the supplied incremental JSON parser so it correctly handles input arriving in multiple chunks, including an incomplete final token. Preserve its public API and existing correct behavior. Add regression tests and submit the modified source tree under /app/submission. Explain the change in a short README.",
          files: {
            "README.md":
              "Replace this file with the repository and public tests for your task.\n",
          },
        });
      }
      for (const name of Object.keys(defaults).filter((x) => x !== "files"))
        $(name).value = draft[name];
      save();
      update();
    }),
);
$("upload").onchange = async (event) => {
  const additions = {};
  try {
    for (const file of event.target.files) {
      if (file.size > 2 * 1024 * 1024)
        throw Error("Each file must fit within the 2 MiB workspace limit.");
      const content = await file.text();
      if (content.includes("\0"))
        throw Error("Only text files are supported in this first version.");
      additions[file.name] = content;
    }
    const files = { ...draft.files, ...additions };
    if (
      Object.keys(files).length > 100 ||
      new TextEncoder().encode(Object.values(files).join("")).length >
        2 * 1024 * 1024
    )
      throw Error("Workspace exceeds 100 files or 2 MiB.");
    draft.files = files;
    save();
    update();
  } catch (error) {
    toast(error.message);
  }
  event.target.value = "";
};
async function request(path) {
  if (!draft.name || !draft.objective.trim()) {
    panel("task");
    throw Error("Add an environment name and task instructions first.");
  }
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      throw Error("The draft is too large or the local server rejected it.");
    }
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  return response;
}
async function preview() {
  try {
    const response = await request("/api/preview"),
      data = await response.json();
    $("validation-status").textContent = data.validation;
    $("warnings").replaceChildren();
    for (const warning of data.warnings) {
      const p = document.createElement("p");
      p.textContent = warning;
      $("warnings").append(p);
    }
    const tabs = $("code-tabs");
    tabs.replaceChildren();
    for (const [name, code] of Object.entries(data.files).filter(
      ([name]) => name !== "studio.json" && name !== ".gitignore",
    )) {
      const button = document.createElement("button");
      button.textContent = name;
      button.onclick = () => {
        tabs
          .querySelectorAll("button")
          .forEach((x) => x.classList.remove("selected"));
        button.classList.add("selected");
        $("code-preview").textContent = code;
      };
      tabs.append(button);
    }
    tabs.firstChild?.click();
    $("review").showModal();
  } catch (error) {
    toast(error.message);
  }
}
async function download() {
  try {
    const response = await request("/api/export");
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = draft.name + ".zip";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(
      "Project downloaded. Review and validate the verifier before launching.",
    );
  } catch (error) {
    toast(error.message);
  }
}
$("preview").onclick = preview;
$("export").onclick = download;
$("review-export").onclick = download;
$("close-review").onclick = () => $("review").close();
update();
