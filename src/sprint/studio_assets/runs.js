(() => {
  const el = (id) => document.getElementById(id);
  let selected = null,
    mode = "agent",
    cursors = {},
    entries = [],
    partial = {},
    sequence = 0;
  let polling = false,
    visible = false;
  const terminal = new Set(["completed", "failed", "cancelled", "unknown"]);

  async function api(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      const data = await response.json();
      throw Error(
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail),
      );
    }
    return response.json();
  }
  function view(runs) {
    visible = runs;
    el("builder-page").hidden = runs;
    el("runs-page").hidden = !runs;
    el("builder-nav").classList.toggle("active", !runs);
    el("runs-nav").classList.toggle("active", runs);
    if (runs) refresh();
  }
  el("builder-nav").onclick = () => view(false);
  el("runs-nav").onclick = () => view(true);
  document
    .querySelectorAll(".section-link")
    .forEach((button) => button.addEventListener("click", () => view(false)));
  el("refresh-runs").onclick = refresh;
  el("open-launch").onclick = () => {
    el("launch-feedback").textContent = "";
    el("launch-dialog").showModal();
  };
  el("close-launch").onclick = () => el("launch-dialog").close();
  el("start-trial").onclick = async () => {
    el("start-trial").disabled = true;
    el("launch-feedback").textContent = "Creating trial…";
    try {
      const run = await api("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft: window.studioDraft(),
          backend: el("launch-backend").value,
        }),
      });
      el("launch-dialog").close();
      view(true);
      select(run.id);
    } catch (error) {
      el("launch-feedback").textContent = error.message;
    } finally {
      el("start-trial").disabled = false;
    }
  };
  el("cancel-run").onclick = async () => {
    if (!selected) return;
    el("cancel-run").disabled = true;
    try {
      await api(`/api/runs/${selected}/cancel`, { method: "POST" });
      await poll();
    } catch (error) {
      toast(error.message);
    } finally {
      el("cancel-run").disabled = false;
    }
  };
  document.querySelectorAll("[data-stream]").forEach(
    (button) =>
      (button.onclick = () => {
        mode = button.dataset.stream;
        document
          .querySelectorAll("[data-stream]")
          .forEach((other) =>
            other.classList.toggle("selected", other === button),
          );
        render();
      }),
  );
  async function refresh() {
    try {
      const runs = await api("/api/runs"),
        list = el("run-list");
      list.replaceChildren();
      if (!runs.length) {
        const p = document.createElement("p");
        p.textContent = "No trials yet. Start one from your environment.";
        list.append(p);
      }
      for (const run of runs) {
        const button = document.createElement("button"),
          title = document.createElement("strong"),
          meta = document.createElement("span");
        title.textContent = run.name;
        meta.textContent = `${run.status} · ${new Date(run.started_at).toLocaleString()}`;
        button.className = "run-card";
        button.classList.toggle("selected", selected === run.id);
        button.append(title, meta);
        button.onclick = () => select(run.id);
        list.append(button);
      }
      if (!selected && runs.length) select(runs[0].id);
    } catch (error) {
      el("run-list").textContent = error.message;
    }
  }
  function select(id) {
    if (selected !== id) {
      selected = id;
      cursors = {};
      entries = [];
      partial = {};
      sequence = 0;
    }
    el("run-empty").hidden = true;
    el("run-content").hidden = false;
    refresh();
    poll();
  }
  function push(channel, value, timestamp) {
    entries.push({ channel, value, timestamp, id: ++sequence });
    if (entries.length > 3000) entries.splice(0, entries.length - 3000);
  }
  function ingest(channel, event) {
    if (channel !== "agent" && !channel.startsWith("native:")) {
      push(channel, event, event.timestamp);
      return;
    }
    const streamKey = `${event.trial_id}:${event.phase}:${event.stream}`;
    const text = (partial[streamKey] || "") + (event.text || "");
    const lines = text.split("\n");
    partial[streamKey] = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let value;
      try {
        value = JSON.parse(line);
      } catch {
        value = { type: event.phase + " output", text: line };
      }
      push("agent", value, value._recorded_at_utc || event.timestamp);
    }
    // Retain a bounded in-progress line so streaming text remains visible.
    if (partial[streamKey].length > 131072) {
      push(
        "agent",
        { type: "output fragment", text: partial[streamKey] },
        event.timestamp,
      );
      partial[streamKey] = "";
    }
  }
  function display(value) {
    const item = value.item;
    if (item) {
      const label =
        (item.type || "Agent event").replaceAll("_", " ") +
        " · " +
        (value.type || "").replaceAll(".", " ");
      const text = item.text || item.command || JSON.stringify(item, null, 2);
      return [
        label,
        text +
          (item.aggregated_output ? "\n\n" + item.aggregated_output : "") +
          (item.exit_code !== undefined
            ? "\n\nExit code: " + item.exit_code
            : ""),
      ];
    }
    return [
      value.type || value.event || "Worker feedback",
      value.text || JSON.stringify(value, null, 2),
    ];
  }
  function render() {
    const results = mode === "results";
    el("result-log").hidden = !results;
    el("transcript-log").hidden = results;
    if (results) return;
    const log = el("transcript-log");
    log.replaceChildren();
    const matching = entries.filter((entry) =>
      mode === "workers"
        ? entry.channel.startsWith("worker:")
        : entry.channel === mode,
    );
    if (!matching.length) {
      const empty = document.createElement("p");
      empty.className = "empty-stream";
      empty.textContent =
        mode === "agent"
          ? "Waiting for agent output. Provisioning and setup appear in Lifecycle."
          : "No records on this channel yet.";
      log.append(empty);
    }
    for (const entry of matching) {
      const article = document.createElement("article"),
        header = document.createElement("header"),
        title = document.createElement("strong"),
        time = document.createElement("time"),
        content = document.createElement("pre");
      const [label, text] = display(entry.value);
      title.textContent = label;
      time.textContent = entry.timestamp
        ? new Date(entry.timestamp).toLocaleTimeString()
        : "Time unavailable";
      time.title = entry.timestamp || "";
      content.textContent = text;
      header.append(title, time);
      article.append(header, content);
      log.append(article);
    }
    if (mode === "agent")
      for (const text of Object.values(partial).filter(Boolean)) {
        const pre = document.createElement("pre");
        pre.className = "pending-output";
        pre.textContent = text;
        log.append(pre);
      }
    if (entries.length === 3000) {
      const note = document.createElement("p");
      note.textContent =
        "Showing the latest 3,000 events. Complete logs remain in the records directory.";
      log.prepend(note);
    }
    if (el("follow-log").checked) log.scrollTop = log.scrollHeight;
  }
  async function poll() {
    if (!selected || polling || !visible) return;
    polling = true;
    const id = selected;
    try {
      const [state, channels] = await Promise.all([
        api(`/api/runs/${id}`),
        api(
          `/api/runs/${id}/events?cursors=${encodeURIComponent(JSON.stringify(cursors))}`,
        ),
      ]);
      if (id !== selected) return;
      el("run-name").textContent = state.name;
      el("run-id").textContent = state.id;
      el("run-status").textContent = state.status;
      el("run-model").textContent = state.model;
      el("run-budget").textContent = state.minutes + " minutes";
      const rewards = (state.result?.trials || [])
        .map((t) => t.reward)
        .filter((r) => r !== null && r !== undefined);
      el("run-reward").textContent = rewards.length
        ? rewards.join(", ")
        : terminal.has(state.status)
          ? "Unscored"
          : "Pending";
      el("cancel-run").hidden = terminal.has(state.status);
      el("cancel-run").textContent =
        state.status === "cancelling"
          ? "Stopping and cleaning up…"
          : "Cancel trial";
      el("record-location").textContent = state.records;
      const errors = [
        state.error,
        ...(state.result?.trials || [])
          .filter((trial) => trial.error)
          .map(
            (trial) =>
              `${trial.name}: ${trial.error.exception_type || "Execution error"}: ${trial.error.exception_message || JSON.stringify(trial.error)}`,
          ),
        state.cleanup_errors &&
          "Unfinished cleanup: " + JSON.stringify(state.cleanup_errors),
        state.interruption &&
          "Interruption: " + JSON.stringify(state.interruption),
      ].filter(Boolean);
      el("run-errors").hidden = !errors.length;
      el("run-errors").textContent = errors.join("\n");
      el("result-log").textContent = JSON.stringify(
        state.result || {
          status: state.status,
          message: "Final evaluation is not available yet.",
        },
        null,
        2,
      );
      el("saved-review").hidden = !state.result;
      el("saved-review").href = `/api/runs/${id}/review`;
      el("raw-logs").replaceChildren();
      for (const [channel, data] of Object.entries(channels)) {
        if (data.cursor < (cursors[channel] || 0))
          push(channel, { event: "log-reset" }, null);
        cursors[channel] = data.cursor;
        for (const event of data.events) ingest(channel, event);
        const link = document.createElement("a");
        link.href = `/api/runs/${id}/log?channel=${encodeURIComponent(channel)}`;
        link.textContent = channel.startsWith("native:")
          ? "Collected transcript ↓"
          : channel.startsWith("worker:")
            ? "Worker log ↓"
            : channel === "agent"
              ? "Live output log ↓"
              : "Lifecycle log ↓";
        el("raw-logs").append(link);
      }
      el("stream-connection").textContent = terminal.has(state.status)
        ? "Retained records · " + state.status
        : "Live · updated " + new Date().toLocaleTimeString();
      render();
    } catch (error) {
      el("stream-connection").textContent =
        "Connection interrupted · " + error.message;
    } finally {
      polling = false;
    }
  }
  setInterval(() => {
    if (visible) {
      poll();
      refresh();
    }
  }, 2000);
})();
