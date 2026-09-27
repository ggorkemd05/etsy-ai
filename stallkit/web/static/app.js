/* The browser front end.
 *
 * It builds argument lists and reads back log lines. It contains no knowledge of Etsy,
 * no validation, and no idea what a tag limit is — every one of those lives in the
 * commands, which the terminal and the desktop window run too. The one rule to keep
 * when editing this file: if you are about to check something here, check it in the
 * command instead, or all three front ends will disagree.
 */

const TOKEN = window.STALLKIT_TOKEN;

const state = {
  lang: "en",
  strings: {},
  languages: [],
  server: null,
  tab: 0,
  job: null,
  polling: false,
};

/* --- talking to the server ------------------------------------------------ */

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: {
      "X-Stallkit-Token": TOKEN,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = (payload && payload.error) || `${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

function t(key, values = {}) {
  let text = state.strings[key];
  if (text === undefined) return key;
  for (const [name, value] of Object.entries(values)) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}

/* --- tiny DOM helpers ----------------------------------------------------- */

const $ = (selector) => document.querySelector(selector);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* --- form building -------------------------------------------------------- */
/* Every control is described by a small object, so a new command is a few lines of
 * data rather than a few lines of DOM. `values` holds what is typed, keyed by name. */

const values = {};

function setValue(name, value) {
  values[name] = value;
  const node = document.querySelector(`[data-name="${name}"]`);
  if (node && node.value !== value) node.value = value;
}

function value(name) {
  return (values[name] ?? "").toString().trim();
}

function card(spec) {
  const body = el("div", { class: "card" }, el("h3", { text: t(spec.title) }));
  if (spec.hint) body.append(el("p", { class: "hint", text: t(spec.hint) }));
  if (spec.steps) {
    body.append(
      el(
        "ol",
        { class: "steps" },
        t(spec.steps)
          .split("\n")
          .filter((line) => line.trim())
          .map((line) => el("li", { text: line.replace(/^\d+\.\s*/, "") })),
      ),
    );
  }
  for (const field of spec.fields || []) body.append(control(field));
  if (spec.actions) {
    body.append(
      el(
        "div",
        { class: "actions" },
        spec.actions.map((action) =>
          el("button", {
            type: "button",
            class: action.primary ? "primary" : action.danger ? "danger" : "",
            text: t(action.label),
            onclick: () => action.run(),
          }),
        ),
      ),
    );
  }
  if (spec.note) body.append(el("p", { class: "note", text: t(spec.note) }));
  return body;
}

function control(field) {
  if (field.kind === "row") {
    return el("div", { class: "row" }, field.fields.map(control));
  }
  if (field.kind === "check") {
    const input = el("input", {
      type: "checkbox",
      "data-name": field.name,
      onchange: (event) => {
        values[field.name] = event.target.checked;
      },
    });
    input.checked = Boolean(values[field.name]);
    return el("label", { class: "check" }, input, el("span", { text: t(field.label) }));
  }

  const wrap = el("label", { class: "field" }, el("span", { text: t(field.label) }));
  let input;
  if (field.kind === "textarea") {
    input = el("textarea", { "data-name": field.name, rows: field.rows || 5 });
  } else if (field.kind === "select") {
    input = el(
      "select",
      { "data-name": field.name },
      field.options.map((option) =>
        el("option", { value: option.value ?? option, text: option.label ?? option }),
      ),
    );
  } else {
    input = el("input", {
      type: field.secret ? "password" : "text",
      "data-name": field.name,
      class: field.mono ? "mono" : "",
      placeholder: field.placeholder || "",
      autocomplete: "off",
      spellcheck: "false",
    });
  }
  input.value = values[field.name] ?? "";
  input.addEventListener("input", (event) => {
    values[field.name] = event.target.value;
  });
  input.addEventListener("change", (event) => {
    values[field.name] = event.target.value;
  });

  if (field.folder) {
    wrap.append(
      el(
        "div",
        { class: "with-button" },
        input,
        el("button", {
          type: "button",
          text: t("browse"),
          onclick: () => pickFolder(field.name),
        }),
      ),
    );
  } else {
    wrap.append(input);
  }
  return wrap;
}

/* --- the log -------------------------------------------------------------- */

function lineClass(text) {
  const head = text.trimStart();
  if (head.startsWith("▶")) return "cmd";
  if (head.startsWith("✓")) return "ok";
  if (head.startsWith("✗") || head.startsWith("Error:")) return "err";
  if (head.startsWith("!") || head.startsWith("→")) return "warn";
  if (head.startsWith("·")) return "dim";
  return "";
}

function appendLines(lines) {
  const log = $("#log");
  for (const line of lines) {
    const css = line.kind === "err" ? lineClass(line.text) || "err" : lineClass(line.text);
    log.append(el("span", { class: css, text: `${line.text}\n` }));
  }
  if (lines.length) {
    $("#log-empty").hidden = true;
    log.scrollTop = log.scrollHeight;
  }
}

function logCommand(args) {
  appendLines([{ kind: "out", text: `▶ stallkit ${args.join(" ")}` }]);
}

/* --- running a command ---------------------------------------------------- */

function setBusy(busy) {
  document.querySelectorAll(".card button, .tabs button").forEach((button) => {
    if (button.id === "prompt-send") return;
    button.disabled = busy;
  });
  const pill = $("#shop-state");
  if (busy) {
    pill.hidden = false;
    pill.className = "pill busy";
    pill.textContent = t("web_busy");
  }
}

async function run(args, { anonymise } = {}) {
  if (state.job && !state.job.done) {
    note(t("web_wait_for_task"), "warn");
    return;
  }
  logCommand(args);
  let job;
  try {
    job = await api("/api/jobs", {
      method: "POST",
      body: { args, anonymise: anonymise ?? $("#anonymise").checked },
    });
  } catch (error) {
    note(String(error.message || error), "err");
    return;
  }
  state.job = { id: job.id, seen: 0, done: false };
  setBusy(true);
  poll();
}

async function poll() {
  if (state.polling) return;
  state.polling = true;
  try {
    while (state.job && !state.job.done) {
      let snapshot;
      try {
        snapshot = await api(
          `/api/jobs/${state.job.id}?since=${state.job.seen}&wait=1`,
        );
      } catch (error) {
        note(String(error.message || error), "err");
        break;
      }
      appendLines(snapshot.lines);
      state.job.seen = snapshot.total;
      showPrompt(snapshot.question);
      if (snapshot.done) {
        state.job.done = true;
        appendLines([
          {
            kind: snapshot.exit_code === 0 ? "out" : "err",
            text:
              snapshot.exit_code === 0
                ? t("done_ok")
                : t("done_fail", { code: snapshot.exit_code }),
          },
        ]);
      }
    }
  } finally {
    state.polling = false;
    setBusy(false);
    showPrompt("");
    await refreshState();
  }
}

function showPrompt(question) {
  const box = $("#prompt");
  box.hidden = !question;
  if (question) {
    $("#prompt-question").textContent = question;
    $("#prompt-answer").focus();
  }
}

async function sendAnswer() {
  if (!state.job) return;
  const input = $("#prompt-answer");
  const text = input.value;
  input.value = "";
  appendLines([{ kind: "out", text: `↳ ${text || "(empty)"}` }]);
  try {
    await api(`/api/jobs/${state.job.id}/answer`, { method: "POST", body: { text } });
  } catch (error) {
    note(String(error.message || error), "err");
  }
}

function note(text, kind = "out") {
  appendLines([{ kind, text: kind === "err" ? `✗ ${text}` : `! ${text}` }]);
}

/* --- dialogs -------------------------------------------------------------- */

function confirmDialog(message) {
  return new Promise((resolve) => {
    const dialog = $("#confirm");
    $("#confirm-body").textContent = message;
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "yes"),
      { once: true },
    );
    dialog.showModal();
  });
}

async function pickFolder(name) {
  const dialog = $("#picker");
  let current = value(name) || state.server.workspace;
  let chosen = current;

  async function draw(path) {
    let listing;
    try {
      listing = await api("/api/browse", { method: "POST", body: { path } });
    } catch (error) {
      note(String(error.message || error), "err");
      return;
    }
    chosen = listing.path;
    $("#picker-path").textContent = listing.path;
    const list = $("#picker-list");
    list.replaceChildren();
    if (listing.parent) {
      list.append(
        el(
          "li",
          {},
          el("button", { type: "button", text: "⤴ ..", onclick: () => draw(listing.parent) }),
        ),
      );
    }
    for (const entry of listing.entries) {
      list.append(
        el(
          "li",
          { class: entry.dir ? "dir" : "file" },
          el("button", {
            type: "button",
            text: `${entry.dir ? "📁" : "📄"} ${entry.name}`,
            onclick: entry.dir ? () => draw(entry.path) : () => {},
          }),
        ),
      );
    }
  }

  await draw(current);
  dialog.showModal();
  return new Promise((resolve) => {
    const finish = (accept) => {
      dialog.close();
      if (accept) {
        setValue(name, chosen);
        savePrefs({ [name]: chosen });
      }
      resolve(accept ? chosen : null);
    };
    $("#picker-choose").onclick = () => finish(true);
    $("#picker-cancel").onclick = () => finish(false);
  });
}

/* --- persistence ---------------------------------------------------------- */

async function savePrefs(payload) {
  try {
    await api("/api/prefs", { method: "POST", body: payload });
  } catch {
    /* A preference that fails to save is not worth interrupting anyone over. */
  }
}

/* --- the tabs ------------------------------------------------------------- */

function workspaceArgs() {
  const folder = value("workspace");
  return folder ? ["--path", folder] : [];
}

function outArgs(name, fallback) {
  const path = value(name) || fallback;
  return path ? ["-o", path] : [];
}

function tabs() {
  const s = state.server;
  return [
    {
      title: "tab_setup",
      cards: [
        {
          title: "setup_app_title",
          hint: "setup_app_hint",
          steps: "setup_app_steps",
          fields: [
            { name: "redirect_uri", label: "callback_label", mono: true },
            { name: "app_reason", label: "app_description_label", mono: false },
          ],
          actions: [
            {
              label: "open_seller_app",
              run: () =>
                window.open("https://www.etsy.com/developers/register-seller-app", "_blank"),
            },
            {
              label: "open_dashboard",
              run: () => window.open("https://www.etsy.com/developers/", "_blank"),
            },
          ],
          note: "setup_app_wait",
        },
        {
          title: "setup_keys_title",
          hint: "setup_keys_hint",
          fields: [
            { name: "keystring", label: "Keystring", mono: true },
            { name: "shared_secret", label: "Shared secret", secret: true, mono: true },
          ],
          actions: [{ label: "save_verify", primary: true, run: saveKeys }],
          note: s.keys.has_keystring ? undefined : "web_local_only",
        },
        {
          title: "setup_connect_title",
          hint: "setup_connect_hint",
          actions: [
            { label: "connect_shop", primary: true, run: () => run(["auth", "login"]) },
            { label: "status", run: () => run(["auth", "status"]) },
            {
              label: "disconnect",
              danger: true,
              run: async () => {
                if (await confirmDialog(t("confirm_disconnect"))) run(["auth", "logout"]);
              },
            },
          ],
        },
        {
          title: "setup_tools_title",
          hint: "setup_tools_hint",
          actions: [
            { label: "run_checks", run: () => run(["setup"]) },
            { label: "status", run: () => run(["doctor"]) },
            { label: "shop_info", run: () => run(["shop", "info"]) },
            { label: "shop_profiles", run: () => run(["shop", "profiles"]) },
            { label: "add_shop", run: () => run(["shops", "add"]) },
          ],
        },
      ],
    },
    {
      title: "tab_design",
      cards: [
        {
          title: "design_setup_title",
          hint: "design_setup_hint",
          actions: [
            { label: "design_check_setup", run: () => run(["design", "status"]) },
            { label: "design_list_styles", run: () => run(["design", "styles"]) },
          ],
        },
        {
          title: "design_draw_title",
          hint: "design_draw_hint",
          fields: [
            { kind: "textarea", name: "concepts", label: "design_concepts", rows: 5 },
            {
              kind: "row",
              fields: [
                { name: "variants", label: "design_variants", placeholder: "2" },
                {
                  kind: "select",
                  name: "style",
                  label: "design_style",
                  options: [
                    "",
                    "flat",
                    "line",
                    "vintage",
                    "watercolour",
                    "boho",
                    "botanical",
                    "kawaii",
                    "geometric",
                    "photoreal",
                  ],
                },
                {
                  kind: "select",
                  name: "shape",
                  label: "design_shape",
                  options: ["square", "portrait", "landscape"],
                },
              ],
            },
            { kind: "check", name: "cutout", label: "design_cutout" },
          ],
          actions: [
            { label: "design_show_prompts", run: () => drawDesigns(true) },
            { label: "design_draw_now", primary: true, run: () => drawDesigns(false) },
          ],
          note: "design_responsibility",
        },
        {
          title: "design_market_title",
          hint: "design_market_hint",
          fields: [
            {
              kind: "row",
              fields: [
                { name: "design_keyword", label: "design_keyword" },
                { name: "design_count", label: "design_count", placeholder: "8" },
              ],
            },
          ],
          actions: [
            { label: "design_show_concepts", run: () => drawFromKeyword(true) },
            { label: "design_draw_now", primary: true, run: () => drawFromKeyword(false) },
          ],
        },
      ],
    },
    {
      title: "tab_drop",
      cards: [
        {
          title: "drop_folder_title",
          hint: "drop_folder_hint",
          fields: [{ name: "workspace", label: "folder", folder: true, mono: true }],
          actions: [
            { label: "create_folder", run: () => run(["drop", "init", ...workspaceArgs()]) },
          ],
        },
        {
          title: "drop_template_title",
          hint: "drop_template_hint",
          fields: [{ name: "template_listing", label: "listing_number" }],
          actions: [
            {
              label: "copy_settings",
              primary: true,
              run: () => {
                const id = value("template_listing");
                if (!/^\d+$/.test(id)) return note(t("need_listing_number"), "err");
                savePrefs({ template_listing: id });
                run(["drop", "template", "--from-listing", id, ...workspaceArgs()]);
              },
            },
          ],
        },
        {
          title: "drop_upload_title",
          hint: "drop_upload_hint",
          actions: [
            {
              label: "check_only",
              run: () => run(["drop", "auto", "--dry-run", ...workspaceArgs()]),
            },
            {
              label: "upload_drafts",
              primary: true,
              run: async () => {
                if (await confirmDialog(t("confirm_upload")))
                  run(["drop", "auto", ...workspaceArgs()]);
              },
            },
          ],
        },
        {
          title: "drop_mockup_title",
          hint: "drop_mockup_hint",
          actions: [
            { label: "prepare_mockups", run: () => run(["drop", "run", ...workspaceArgs()]) },
            {
              label: "preview_print_area",
              run: () => run(["drop", "calibrate", "--preview", ...workspaceArgs()]),
            },
          ],
        },
      ],
    },
    {
      title: "tab_listings",
      cards: [
        {
          title: "export_title",
          hint: "export_hint",
          fields: [
            {
              kind: "row",
              fields: [
                {
                  kind: "select",
                  name: "pull_state",
                  label: "which_listings",
                  options: ["active", "draft", "inactive", "expired", "sold_out"],
                },
                { name: "pull_out", label: "web_save_to", mono: true },
              ],
            },
          ],
          actions: [
            {
              label: "save_as_csv",
              primary: true,
              run: () =>
                run([
                  "listings",
                  "pull",
                  "--state",
                  value("pull_state") || "active",
                  ...outArgs("pull_out", "listings-export.csv"),
                ]),
            },
          ],
        },
        {
          title: "push_title",
          hint: "push_hint",
          fields: [
            { name: "push_csv", label: "csv_file", mono: true },
            { name: "inventory_from", label: "copy_variations_from" },
          ],
          actions: [
            { label: "blank_template", run: () => run(["listings", "template"]) },
            { label: "check_only", run: () => pushListings(true) },
            { label: "send_to_etsy", primary: true, run: () => pushListings(false) },
          ],
        },
      ],
    },
    {
      title: "tab_orders",
      cards: [
        {
          title: "orders_pull_title",
          hint: "orders_pull_hint",
          fields: [
            {
              kind: "row",
              fields: [
                { name: "since", label: "since", placeholder: "30d" },
                { name: "orders_out", label: "web_save_to", mono: true },
              ],
            },
            { kind: "check", name: "unshipped", label: "unshipped_only" },
          ],
          actions: [
            {
              label: "save_as_csv",
              primary: true,
              run: () =>
                run([
                  "orders",
                  "pull",
                  "--since",
                  value("since") || "30d",
                  ...outArgs("orders_out", "orders.csv"),
                  ...(values.unshipped ? ["--unshipped"] : []),
                ]),
            },
          ],
        },
        {
          title: "ship_title",
          hint: "ship_hint",
          fields: [
            { name: "ship_csv", label: "csv_file", mono: true },
            { name: "country", label: "country_code", placeholder: "TR" },
          ],
          actions: [
            { label: "list_carriers", run: () => listCarriers() },
            { label: "check_only", run: () => shipOrders(true) },
            { label: "send_tracking", primary: true, run: () => shipOrders(false) },
          ],
        },
      ],
    },
    {
      title: "tab_seo",
      cards: [
        {
          title: "audit_title",
          hint: "audit_hint",
          fields: [{ name: "audit_out", label: "web_save_to", mono: true }],
          actions: [
            { label: "score_listings", primary: true, run: () => run(["seo", "audit"]) },
            {
              label: "save_report",
              run: () => run(["seo", "audit", ...outArgs("audit_out", "seo-audit.csv")]),
            },
          ],
        },
        {
          title: "keywords_title",
          hint: "keywords_hint",
          fields: [{ name: "keyword", label: "keyword" }],
          actions: [
            {
              label: "research",
              primary: true,
              run: () => {
                const term = value("keyword");
                if (!term) return note(t("need_keyword"), "err");
                run(["seo", "keywords", term]);
              },
            },
          ],
        },
        {
          title: "suggest_title",
          hint: "suggest_hint",
          fields: [
            {
              kind: "row",
              fields: [
                { name: "suggest_listing", label: "listing_number" },
                { name: "suggest_keyword", label: "keyword_optional" },
              ],
            },
          ],
          actions: [
            {
              label: "get_suggestions",
              primary: true,
              run: () => {
                const id = value("suggest_listing");
                if (!/^\d+$/.test(id)) return note(t("need_listing_number"), "err");
                const extra = value("suggest_keyword");
                run(["seo", "suggest", id, ...(extra ? ["--keyword", extra] : [])]);
              },
            },
          ],
        },
      ],
    },
    {
      title: "tab_pinterest",
      cards: [
        {
          title: "pin_app_title",
          hint: "pin_app_hint",
          fields: [
            { name: "pinterest_app_id", label: "Pinterest app id", mono: true },
            { name: "pinterest_app_secret", label: "Pinterest app secret", secret: true },
          ],
          actions: [
            { label: "save", primary: true, run: savePinterestKeys },
            {
              label: "open_pinterest_apps",
              run: () => window.open("https://developers.pinterest.com/apps/", "_blank"),
            },
          ],
        },
        {
          title: "pin_account_title",
          actions: [
            {
              label: "connect_pinterest",
              primary: true,
              run: () => run(["pinterest", "login"]),
            },
            { label: "my_boards", run: () => run(["pinterest", "boards"]) },
            { label: "status", run: () => run(["pinterest", "status"]) },
          ],
        },
        {
          title: "pin_queue_title",
          hint: "pin_queue_hint",
          fields: [
            { name: "pin_listings", label: "listing_numbers" },
            {
              kind: "row",
              fields: [
                { name: "pin_board", label: "board" },
                { name: "pin_images", label: "images", placeholder: "1-6" },
                { name: "pin_per_day", label: "per_day", placeholder: "2" },
              ],
            },
            { kind: "check", name: "pin_ai", label: "pin_ai" },
          ],
          actions: [
            { label: "check_only", run: () => queuePins(true) },
            { label: "add_to_queue", primary: true, run: () => queuePins(false) },
          ],
        },
        {
          title: "pin_post_title",
          hint: "pin_post_hint",
          actions: [
            { label: "post_due", primary: true, run: () => run(["pinterest", "post"]) },
            { label: "show_queue", run: () => run(["pinterest", "list"]) },
          ],
        },
      ],
    },
  ];
}

/* --- command builders ----------------------------------------------------- */

function designOptions() {
  const args = [];
  const variants = value("variants");
  if (variants) args.push("--variants", variants);
  const style = value("style");
  if (style) args.push("--style", style);
  const shape = value("shape");
  if (shape) args.push("--shape", shape);
  return args;
}

async function drawDesigns(dryRun) {
  const concepts = (values.concepts || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (!concepts.length) return note(t("design_need_concepts"), "err");

  const args = ["design", "new", ...concepts, ...designOptions(), ...workspaceArgs()];
  if (values.cutout) args.push("--cutout");
  if (dryRun) return run([...args, "--dry-run"]);
  if (!(await confirmDialog(t("confirm_design", { count: concepts.length })))) return;
  run([...args, "--yes"]);
}

async function drawFromKeyword(dryRun) {
  const keyword = value("design_keyword");
  if (!keyword) return note(t("design_need_keyword"), "err");
  const args = ["design", "from-keyword", keyword, ...designOptions(), ...workspaceArgs()];
  const count = value("design_count");
  if (count) args.push("--designs", count);
  if (dryRun) return run([...args, "--dry-run"]);
  if (!(await confirmDialog(t("confirm_design_keyword")))) return;
  run([...args, "--yes"]);
}

async function pushListings(dryRun) {
  const csv = value("push_csv");
  if (!csv) return note(t("pick_csv"), "err");
  savePrefs({ push_csv: csv });
  const args = ["listings", "push", csv];
  const inventory = value("inventory_from");
  if (inventory) args.push("--inventory-from", inventory);
  if (dryRun) return run([...args, "--dry-run"]);
  if (!(await confirmDialog(t("confirm_push")))) return;
  run([...args, "--yes"]);
}

function listCarriers() {
  const country = value("country");
  if (!country) return note(t("need_country"), "err");
  savePrefs({ country });
  run(["orders", "carriers", "--country", country.toUpperCase()]);
}

async function shipOrders(dryRun) {
  const csv = value("ship_csv");
  if (!csv) return note(t("pick_csv"), "err");
  const country = value("country");
  if (!country) return note(t("need_country"), "err");
  savePrefs({ ship_csv: csv, country });
  const args = ["orders", "ship", csv, "--country", country.toUpperCase()];
  if (dryRun) return run([...args, "--dry-run"]);
  if (!(await confirmDialog(t("confirm_ship")))) return;
  run([...args, "--yes"]);
}

function queuePins(dryRun) {
  const ids = (values.pin_listings || "").split(/[\s,;]+/).filter(Boolean);
  if (!ids.length) return note(t("need_listing_numbers"), "err");
  const board = value("pin_board");
  if (!board) return note(t("need_board"), "err");
  savePrefs({ pin_board: board });
  const args = ["pinterest", "queue", ...ids, "--board", board];
  const perDay = value("pin_per_day");
  if (perDay) args.push("--per-day", perDay);
  const images = value("pin_images");
  if (images) args.push("--images", images);
  if (values.pin_ai) args.push("--ai-modified");
  run(dryRun ? [...args, "--dry-run"] : args);
}

async function saveKeys() {
  try {
    const result = await api("/api/keys", {
      method: "POST",
      body: {
        keystring: value("keystring"),
        shared_secret: value("shared_secret"),
        redirect_uri: value("redirect_uri"),
      },
    });
    appendLines([{ kind: "out", text: t("saved_to", { path: result.saved }) }]);
    setValue("shared_secret", "");
    await refreshState();
    run(["doctor"]);
  } catch (error) {
    note(String(error.message || error), "err");
  }
}

async function savePinterestKeys() {
  try {
    const result = await api("/api/keys", {
      method: "POST",
      body: {
        pinterest_app_id: value("pinterest_app_id"),
        pinterest_app_secret: value("pinterest_app_secret"),
      },
    });
    appendLines([{ kind: "out", text: t("pin_saved", { path: result.saved }) }]);
    setValue("pinterest_app_secret", "");
    await refreshState();
  } catch (error) {
    note(String(error.message || error), "err");
  }
}

/* --- rendering ------------------------------------------------------------ */

function render() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-t]").forEach((node) => {
    node.textContent = t(node.dataset.t);
  });

  const specs = tabs();
  const tabBar = $("#tabs");
  const panes = $("#panes");
  tabBar.replaceChildren();
  panes.replaceChildren();

  specs.forEach((spec, index) => {
    tabBar.append(
      el("button", {
        type: "button",
        role: "tab",
        "aria-selected": index === state.tab ? "true" : "false",
        text: t(spec.title),
        onclick: () => {
          state.tab = index;
          render();
        },
      }),
    );
    panes.append(
      el(
        "div",
        { class: `pane${index === state.tab ? " active" : ""}`, role: "tabpanel" },
        spec.cards.map(card),
      ),
    );
  });

  drawShopPicker();
  drawLanguagePicker();
  drawStatusPill();
  $("#trademark").textContent =
    "The term 'Etsy' is a trademark of Etsy, Inc. This Application uses Etsy's API, " +
    "but is not endorsed or certified by Etsy.";
  $("#version").textContent = `stallkit ${state.server.version}`;
  $("#home-path").textContent = state.server.home;
}

function drawShopPicker() {
  const picker = $("#shop-picker");
  picker.replaceChildren();
  for (const shop of state.server.shops) {
    picker.append(
      el("option", {
        value: shop.id,
        text: shop.name || t("shop_n", { n: shop.index }),
      }),
    );
  }
  picker.value = state.server.shop || "";
  picker.onchange = async () => {
    try {
      state.server = await api("/api/shop", {
        method: "POST",
        body: { shop: picker.value },
      });
      loadValuesFromState();
      render();
    } catch (error) {
      note(String(error.message || error), "err");
    }
  };
}

function drawLanguagePicker() {
  const picker = $("#language-picker");
  picker.replaceChildren();
  for (const [code, label] of state.languages) {
    picker.append(el("option", { value: code, text: label }));
  }
  picker.value = state.lang;
  picker.onchange = () => {
    state.lang = picker.value;
    state.strings = state.allStrings[state.lang];
    savePrefs({ language: state.lang });
    render();
  };
}

function drawStatusPill() {
  const pill = $("#shop-state");
  const keys = state.server.keys;
  pill.hidden = false;
  if (!keys.has_keystring || !keys.has_secret) {
    pill.className = "pill warn";
    pill.textContent = t("status_keys");
    return;
  }
  if (!state.server.connected) {
    pill.className = "pill warn";
    pill.textContent = t("status_disconnected");
    return;
  }
  pill.className = "pill ok";
  pill.textContent = t("status_connected", {
    shop: state.server.shop_name || t("shop"),
  });
}

function loadValuesFromState() {
  const s = state.server;
  setValue("redirect_uri", s.keys.redirect_uri);
  setValue("app_reason", t("app_description_value"));
  setValue("workspace", s.workspace);
  setValue("template_listing", s.prefs.template_listing);
  setValue("push_csv", s.prefs.push_csv);
  setValue("ship_csv", s.prefs.ship_csv);
  setValue("country", s.prefs.country);
  setValue("inventory_from", s.prefs.inventory_from);
  if (values.variants === undefined) setValue("variants", "2");
  if (values.shape === undefined) setValue("shape", "square");
  if (values.since === undefined) setValue("since", "30d");
  if (values.pin_per_day === undefined) setValue("pin_per_day", "2");
  if (values.unshipped === undefined) values.unshipped = true;
  if (values.pin_ai === undefined) values.pin_ai = true;
  if (values.pull_state === undefined) setValue("pull_state", "active");
}

async function refreshState() {
  try {
    state.server = await api("/api/state");
    drawStatusPill();
  } catch {
    /* The page keeps whatever it last knew rather than blanking itself. */
  }
}

/* --- start ---------------------------------------------------------------- */

async function start() {
  const [translations, server] = await Promise.all([api("/api/i18n"), api("/api/state")]);
  state.allStrings = translations.strings;
  state.languages = translations.languages;
  state.server = server;
  state.lang = translations.strings[server.language] ? server.language : "en";
  state.strings = translations.strings[state.lang];

  loadValuesFromState();
  $("#anonymise").checked = Boolean(server.anonymise);
  $("#anonymise").onchange = (event) => savePrefs({ anonymise: event.target.checked });
  $("#prompt-send").onclick = sendAnswer;
  $("#prompt-answer").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendAnswer();
    }
  });
  $("#clear-log").onclick = () => {
    $("#log").replaceChildren();
    $("#log-empty").hidden = false;
  };
  $("#copy-log").onclick = async () => {
    try {
      await navigator.clipboard.writeText($("#log").textContent);
      note(t("copied"));
    } catch {
      note("Copy failed — select the text and copy it yourself.", "err");
    }
  };

  render();
}

start().catch((error) => {
  document.body.append(
    el(
      "p",
      { style: "padding:2rem;color:#b91c1c;font:15px system-ui" },
      `stallkit could not start: ${error.message || error}`,
    ),
  );
});
