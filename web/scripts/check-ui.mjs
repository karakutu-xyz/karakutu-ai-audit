// Offline behavioral checks using the already-installed TypeScript and React.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { setImmediate as settle } from "node:timers/promises";

const root = fileURLToPath(new URL("../src/", import.meta.url));
const require = createRequire(new URL("../package.json", import.meta.url));
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { parse: parseHtml } = require("parse5");
const events = new EventTarget();
let href = "https://workbench.invalid/runs";
let historyState = null;
let operation = "";
const historyCalls = [];
const scrollCalls = [];
const browser = {
  scrollY: 0,
  setTimeout() {},
  clearTimeout() {},
  scrollTo(x, y) {
    scrollCalls.push([x, y]);
    this.scrollY = y;
  },
  location: {
    get href() {
      return href;
    },
    get origin() {
      return new URL(href).origin;
    },
    assign(value) {
      href = value;
    },
  },
  history: {
    get state() {
      return historyState;
    },
    pushState(state, _, url) {
      operation = "push";
      historyCalls.push({ operation, state, url: String(url) });
      historyState = state;
      href = String(url);
    },
    replaceState(state, _, url) {
      operation = "replace";
      historyCalls.push({ operation, state, url: String(url) });
      historyState = state;
      href = String(url);
    },
  },
  addEventListener: events.addEventListener.bind(events),
  removeEventListener: events.removeEventListener.bind(events),
  dispatchEvent: events.dispatchEvent.bind(events),
};
const cache = new Map();
let capturedQuery;
let runsState;
let runState;
let snapshotState;
let articlesState;
let articleState;
const pollSettingsFixture = Object.fromEntries(Object.entries({
  polling_enabled: true, interval_minutes: 5, lookback_days: 7,
  auto_audit_enabled: false, auto_audit_max_per_poll: 5,
}).map(([name, value]) => [name, { value, default: value, source: "env" }]));
const hooks = {
  useApi: () => ({
    token: "",
    epoch: 0,
    ledgerEpoch: { current: { token: "", epoch: 0 } },
    articlesLedgerEpoch: { current: { token: "", epoch: 0 } },
    setToken() {},
  }),
  useRuns: (query) => {
    capturedQuery = new URLSearchParams(query);
    return runsState;
  },
  useArticles: (query) => {
    capturedQuery = new URLSearchParams(query);
    return articlesState;
  },
  useArticle: () => articleState,
  useArticleInventory: () => ({ data: { total: 1 }, loading: false }),
  useQueue: () => ({
    data: { paused: false, max_concurrent_runs: 1, running: [], queued: [] },
    loading: false,
    error: "",
    refresh() {},
  }),
  usePollingSettings: () => ({
    data: pollSettingsFixture, error: "", refresh() {},
  }),
  usePollingControl: () => ({ mutate: async () => {}, busy: false, error: "" }),
  useRunControl: () => ({ mutate: async () => true, busy: false, error: "" }),
  useRun: () => runState,
  useWorkflow: () => snapshotState,
};

function load(file) {
  if (cache.has(file)) return cache.get(file).exports;
  const module = { exports: {} };
  cache.set(file, module);
  const localRequire = (name) => {
    if (name === "react" && file.endsWith("router.tsx")) {
      // The router snapshot is read synchronously in these static-render tests.
      return {
        ...React,
        useSyncExternalStore: (_, getSnapshot) => getSnapshot(),
        useLayoutEffect() {},
      };
    }
    if (name === "react" && /\/(RunsPage|ArticlesPage)\.tsx$/.test(file)) {
      // Static markup checks leave layout effects to the behavioral harness below.
      return { ...React, useLayoutEffect() {} };
    }
    if (!name.startsWith(".")) return require(name);
    const base = path.resolve(path.dirname(file), name);
    if (base === path.join(root, "api/hooks")) return hooks;
    const extension = [".tsx", ".ts"].find((ext) => {
      try {
        readFileSync(base + ext);
        return true;
      } catch {
        return false;
      }
    });
    return load(base + extension);
  };
  evaluateModule(file, localRequire, {}, module);
  return module.exports;
}

// Compile actual source; substitute only the browser, React hooks and transport.
function evaluateModule(file, localRequire, globals = {}, module = { exports: {} }) {
  const source = ts.transpileModule(readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
    fileName: file,
  }).outputText;
  vm.runInNewContext(`(function(require,module,exports){${source}\n})`, {
    window: browser,
    Event,
    Element: TestElement,
    URL,
    URLSearchParams,
    AbortController,
    setTimeout,
    clearTimeout,
    console,
    ...globals,
  })(localRequire, module, module.exports);
  return module.exports;
}

class TestElement {
  constructor(interactive = false) {
    this.interactive = interactive;
  }

  closest() {
    return this.interactive ? this : null;
  }
}

const router = load(path.join(root, "router.tsx"));

function visit(address, state = null) {
  href = address;
  historyState = state;
  browser.dispatchEvent(new Event("popstate"));
}

function click(overrides = {}) {
  return {
    button: 0,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...overrides,
  };
}

const render = (component, props) =>
  renderToStaticMarkup(React.createElement(component, props));
const baseRun = {
  id: "run_fictional",
  workflow_id: "workflow_fictional",
  category: "live",
  origin: "api",
  status: "failed",
  created_at: "2026-11-01T10:00:00Z",
  finished_at: "2026-11-01T10:00:03Z",
  label: null,
  article_title: "Synthetic audit interface check",
  verdict: "AUDIT_FAILED",
  synthetic: true,
  reactions: [],
};

function resultFixture(run = baseRun) {
  return {
    schema: "karakutu-ai-audit/audit-result", schema_version: "2.0",
    record_id: null, lineage: null,
    article: { ref: "ref_fictional", archive_id: null,
      url: "https://news.example/fictional", content_sha256: "a".repeat(64) },
    run: { id: run.id, origin: run.origin ?? "benchmark", category: run.category,
      status: run.status, created_at: run.created_at, finished_at: run.finished_at,
      workflow_id: run.workflow_id },
    verdict: run.verdict, flags: [], findings: null, classification: null,
    stages: [], evidence: null, error: null,
    notice: "Machine triage record. No verdict, including POSSIBLE_CENSORSHIP, " +
      "is a finding of censorship; a person decides on independent evidence.",
  };
}

test("route matching decodes identifiers and rejects malformed or unknown addresses", () => {
  for (const name of ["articles", "runs", "benchmarks", "settings"]) {
    assert.equal(router.matchRoute(`/${name}`).name, name);
  }
  assert.equal(router.matchRoute("/").name, "root");
  assert.equal(router.matchRoute("/runs/run%20fictional").params.id, "run fictional");
  assert.equal(
    router.matchRoute("/articles/ref%2Ffictional").params.ref,
    "ref/fictional",
  );
  for (const invalid of ["/unknown", "/runs/a/b", "/runs/%E0%A4%A"]) {
    assert.equal(router.matchRoute(invalid).name, "not-found");
  }
});

test("navigation preserves query/state and chooses push or replacement", () => {
  router.navigate("/runs?origin=live&status=failed&offset=50", { replace: true });
  assert.equal(operation, "replace");
  assert.equal(router.useRoute().query.get("offset"), "50");
  router.navigate("/runs/run_fictional", {
    state: { runsBack: "/runs?origin=live&offset=50" },
  });
  assert.equal(operation, "push");
  assert.equal(historyState.runsBack, "/runs?origin=live&offset=50");
});

test("Link intercepts only ordinary same-origin left clicks", () => {
  const event = click();
  router.Link({ href: "/benchmarks" }).props.onClick(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(new URL(href).pathname, "/benchmarks");
  for (const overrides of [
    { button: 1 },
    { ctrlKey: true },
    { metaKey: true },
    { shiftKey: true },
    { altKey: true },
    { defaultPrevented: true },
  ]) {
    const event = click(overrides);
    visit("https://workbench.invalid/runs");
    router.Link({ href: "/benchmarks" }).props.onClick(event);
    assert.equal(new URL(href).pathname, "/runs");
  }
  for (const props of [
    { target: "_blank" },
    { target: "_self" },
    { download: "run.json" },
    { href: "https://outside.invalid/" },
    { onClick: (e) => e.preventDefault() },
  ]) {
    visit("https://workbench.invalid/runs");
    router.Link({ href: "/benchmarks", ...props }).props.onClick(click());
    assert.equal(new URL(href).pathname, "/runs");
  }
});

test("ledger sends all filters to the server and renders the returned page", () => {
  visit(
    "https://workbench.invalid/runs?origin=live&verdict=AUDIT_FAILED" +
    "&status=failed&workflow_id=workflow_fictional&offset=50",
  );
  runsState = {
    data: {
      items: [baseRun, { ...baseRun, id: "run_other", status: "succeeded" }],
      total: 120,
    },
    loading: false,
    error: "",
    refresh() {},
  };
  const html = render(load(path.join(root, "pages/RunsPage.tsx")).RunsPage);
  assert.equal(capturedQuery.get("category"), "live");
  assert.equal(capturedQuery.get("verdict"), "AUDIT_FAILED");
  assert.equal(capturedQuery.get("workflow_id"), "workflow_fictional");
  assert.equal(capturedQuery.get("limit"), "50");
  assert.equal(capturedQuery.get("offset"), "50");
  assert.equal(capturedQuery.get("status"), "failed");
  assert.equal(capturedQuery.has("origin"), false);
  assert.ok(html.includes("/runs/run_fictional"));
  assert.ok(html.includes("/runs/run_other"));
  assert.ok(!html.includes("Status filters this page only"));
  assert.ok(html.includes('role="region" aria-label="Run ledger" tabindex="0"'));
  assert.ok(html.includes('<th scope="col">Article</th>'));
  assert.ok(!html.includes('<th scope="col">Run</th>'));
  assert.ok(html.includes("of 120 runs"));
  const benchmarkHtml = render(
    load(path.join(root, "pages/RunsPage.tsx")).RunsPage,
    { benchmark: true },
  );
  assert.equal(capturedQuery.get("category"), "benchmark");
  assert.equal(capturedQuery.get("status"), "failed");
  assert.ok(!benchmarkHtml.includes('<option value="live">'));
});

test("ledger maps origin choices and keeps benchmark category isolated", () => {
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  for (const origin of ["", "benchmark", "live", "api", "webhook", "article", "poll"]) {
    visit(`https://workbench.invalid/runs?origin=${origin}`);
    render(RunsPage);
    const category = ["benchmark", "live"].includes(origin) ? origin : null;
    assert.equal(capturedQuery.get("category"), category);
    assert.equal(capturedQuery.get("origin"), category ? null : origin || null);
    render(RunsPage, { benchmark: true });
    assert.equal(capturedQuery.get("category"), "benchmark");
    assert.equal(capturedQuery.has("origin"), false);
  }
});

test("changing ledger status resets paging and preserves other filters", () => {
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  visit("https://workbench.invalid/runs?origin=api&status=failed&offset=50");
  const select = findNode(
    RunsPage({}),
    (node) => node.type === "select" && node.props.value === "failed",
  );
  select.props.onChange({ target: { value: "succeeded" } });
  const query = new URL(href).searchParams;
  assert.equal(query.get("status"), "succeeded");
  assert.equal(query.get("origin"), "api");
  assert.equal(query.has("offset"), false);
  assert.equal(operation, "replace");
});

test("run detail presents ordered, collapsed exact records and restores its ledger address", () => {
  const run = {
    ...baseRun,
    article_ref: "ref_fictional",
    verdict: "POSSIBLE_CENSORSHIP",
    expected_verdict: "INCONCLUSIVE",
    error: null,
    input: {
      removed_article: {
        url: `https://example.invalid/check-${Date.now()}`,
        title: baseRun.article_title,
        content: "Fictional interface verification input.",
      },
      similar_articles: [],
    },
    model: {},
    output: {
      findings: "Synthetic evidence requires a human review.",
      flags: ["SOURCE_DEAD"],
      stages: [
        {
          stage: "similarity",
          explanation: "Skipped: no comparison candidates.",
        },
      ],
    },
    steps: [
      {
        name: "similarity",
        status: "skipped",
        duration_ms: 1250,
        model: null,
        model_calls: [],
        evidence: [],
        input: { test: true },
        output: { explanation: "Skipped: no comparison candidates." },
      },
      {
        name: "classification",
        status: "completed",
        duration_ms: 120000,
        model: null,
        model_calls: [{}],
        evidence: [],
        input: { fictional: true },
        output: { explanation: "Fictional classification explanation." },
      },
    ],
  };
  runState = {
    data: run,
    loading: false,
    error: "",
    refresh() {},
    setData() {},
  };
  snapshotState = {
    data: null,
    error: "Cannot reach the audit service.",
    refresh() {},
  };
  historyState = { runsBack: "/runs?origin=live&offset=50" };
  const html = render(
    load(path.join(root, "pages/RunDetailPage.tsx")).RunDetailPage,
    { id: run.id },
  );
  assert.ok(html.includes("/runs?origin=live&amp;offset=50"));
  assert.ok(html.includes('href="/articles/ref_fictional"'));
  assert.equal(
    html.split("This is not a finding of censorship; a person decides.").length - 1,
    1,
  );
  let previous = -1;
  for (const title of [
    "Classify",
    "Find a moved copy",
    "Screen public interest",
    "Check related removals",
    "Verify the source",
    "Find independent evidence",
  ]) {
    const position = html.indexOf(title);
    assert.ok(position > previous);
    previous = position;
  }
  assert.ok(html.includes('aria-expanded="false"'));
  assert.ok(html.includes('aria-controls="step-similarity"'));
  assert.ok(html.includes('aria-label="Find a moved copy"'));
  assert.ok(html.includes('id="step-similarity" hidden=""'));
  assert.ok(!html.includes("<summary>Exact input JSON</summary>"));
  assert.ok(!html.includes("<summary>Exact output JSON</summary>"));
  assert.ok(html.includes("No model call"));
  assert.ok(html.includes("Not recorded"));
  assert.ok(!html.includes("No model calls recorded"));
  assert.ok(html.includes("2 min 0 s"));
  assert.ok(!html.includes("120.000 s"));
  for (const label of [
    "1.3 s",
    "Skipped: no comparison candidates.",
    "Step input",
    "Exact output JSON",
    "Workflow snapshot unavailable",
    "Retry snapshot",
    "Export JSON",
    "Human review",
    "Flagged for human review",
    "Expected verdict",
  ]) {
    assert.ok(html.includes(label), label);
  }
  runState.error = "Fictional refresh failure";
  const refreshed = render(
    load(path.join(root, "pages/RunDetailPage.tsx")).RunDetailPage,
    { id: run.id },
  );
  assert.ok(refreshed.includes(run.article_title));
  assert.ok(refreshed.includes("Fictional refresh failure"));
  assert.ok(refreshed.includes("Try again"));
});

test("workflow refresh errors keep the snapshot and offer retry", () => {
  const { WorkflowView } = load(
    path.join(root, "features/workflow/WorkflowView.tsx"),
  );
  const workflow = {
    id: "workflow_fictional",
    created_at: baseRun.created_at,
    definition: { steps: [], sources: { "fictional.py": "# Synthetic source" } },
  };
  const props = {
    workflow,
    error: "Fictional service outage",
    onRetry() {},
  };
  const html = render(WorkflowView, props);
  assert.ok(html.includes(workflow.id));
  assert.ok(html.includes("# Synthetic source"));
  assert.ok(html.includes("Could not refresh workflow snapshot."));
  assert.ok(html.includes("Fictional service outage"));
  assert.ok(html.includes("Retry snapshot"));
  const unavailable = render(WorkflowView, { ...props, workflow: null });
  assert.ok(unavailable.includes("Workflow snapshot unavailable."));
  assert.ok(unavailable.includes("Retry snapshot"));
  assert.ok(!unavailable.includes(workflow.id));
  const loading = render(WorkflowView, { ...props, workflow: null, error: "" });
  assert.ok(loading.includes("Loading workflow snapshot…"));
  assert.ok(!loading.includes("Retry snapshot"));
});

test("empty, loading and authentication failures provide readable next steps", () => {
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  visit("https://workbench.invalid/runs");
  runsState = { data: null, loading: true, error: "", refresh() {} };
  assert.ok(render(RunsPage).includes("Loading runs…"));
  runsState = {
    data: { items: [], total: 0 },
    loading: false,
    error: "",
    refresh() {},
  };
  assert.ok(render(RunsPage).includes("No runs yet"));
  runsState = {
    data: null,
    loading: false,
    error: "Access requires an API token. Add it in Settings.",
    refresh() {},
  };
  const html = render(RunsPage);
  assert.ok(html.includes('href="/settings"'));
  assert.ok(html.includes("Try again"));
});

test("empty run ledgers distinguish filters and avoid benchmark self-links", () => {
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  runsState = {
    data: { items: [], total: 0 }, loading: false, error: "", refresh() {},
  };
  for (const benchmark of [false, true]) {
    const page = benchmark ? "benchmarks" : "runs";
    visit(`https://workbench.invalid/${page}`);
    let html = render(RunsPage, { benchmark });
    assert.ok(html.includes("No runs yet"));
    assert.ok(!html.includes("No runs match these filters"));
    assert.equal(html.includes('href="/benchmarks"'), !benchmark);
    assert.ok(html.includes(benchmark
      ? "Choose New benchmark run to submit a controlled case."
      : "Create a case from Benchmarks."));
    const filters = ["status=failed", "verdict=INCONCLUSIVE", "workflow_id=wf_fake"];
    if (!benchmark) filters.push("origin=live", "origin=article");
    for (const filter of filters) {
      visit(`https://workbench.invalid/${page}?${filter}`);
      html = render(RunsPage, { benchmark });
      assert.ok(html.includes("No runs match these filters"));
      assert.ok(html.includes("Adjust the filters, or create a case from Benchmarks."));
      assert.equal(html.includes('href="/benchmarks"'), !benchmark);
    }
  }
});

const format = load(path.join(root, "format.ts"));

test("shared duration formatting covers seconds, minutes and pending runs", () => {
  for (const [ms, expected] of [
    [null, "—"],
    [0, "0.0 s"],
    [200, "0.2 s"],
    [6200, "6.2 s"],
    [10000, "10 s"],
    [14600, "14.6 s"],
    [59000, "59 s"],
    [60000, "1 min 0 s"],
    [94000, "1 min 34 s"],
    [120000, "2 min 0 s"],
  ]) {
    assert.equal(format.duration(ms), expected);
  }
  assert.equal(
    format.runDuration({
      ...baseRun,
      finished_at: "2026-11-01T10:02:00Z",
    }),
    "2 min 0 s",
  );
  for (const status of ["queued", "running"]) {
    assert.equal(
      format.runDuration({ ...baseRun, status, finished_at: null }),
      "In progress",
    );
  }
  assert.equal(format.runDuration({ ...baseRun, finished_at: null }), "—");
});

test("ledger labels omit repeated titles and prefixed copies without changing records", () => {
  for (const label of [
    null,
    "",
    "  ",
    baseRun.article_title,
    `DEMO · ${baseRun.article_title}`,
    `Demo: ${baseRun.article_title}`,
    `DEMO — ${baseRun.article_title.toUpperCase()}`,
    `  DEMO ·  ${baseRun.article_title}  `,
  ]) {
    const run = { ...baseRun, label };
    assert.equal(format.ledgerLabel(run), "");
    assert.equal(run.label, label);
  }
  for (const label of ["Independent comparison", "Synthetic audit", "Short title"]) {
    assert.equal(format.ledgerLabel({ ...baseRun, label }), label);
  }
  assert.equal(
    format.ledgerLabel({ ...baseRun, label: "Distinct case", article_title: "" }),
    "Distinct case",
  );
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  visit("https://workbench.invalid/runs");
  runsState = {
    data: {
      items: [{ ...baseRun, label: `DEMO · ${baseRun.article_title}` }],
      total: 1,
    },
    loading: false,
    refreshing: true,
    refresh() {},
  };
  const html = render(RunsPage);
  assert.equal(html.split(baseRun.article_title).length - 1, 1);
  assert.ok(html.includes("Refreshing…"));
  assert.ok(!html.includes("Loading runs…"));
});

test("run model uses its recorded name, then distinct step models, then a clear fallback", () => {
  const steps = [
    { model: "step-a", model_calls: [{}] },
    { model: "step-a", model_calls: [{}] },
    { model: "step-b", model_calls: [{}] },
    { model: null, model_calls: [{}] },
  ];
  assert.equal(format.runModel({ model: { name: "run-model" }, steps }), "run-model");
  assert.equal(format.runModel({ model: {}, steps }), "step-a, step-b");
  assert.equal(
    format.runModel({ model: {}, steps: [{ model: null, model_calls: [{}] }] }),
    "Not recorded",
  );
  assert.equal(format.runModel({ model: {}, steps: [] }), "Not recorded");
});

function findNode(node, predicate) {
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  for (const child of React.Children.toArray(node.props?.children)) {
    const match = findNode(child, predicate);
    if (match) return match;
  }
  return null;
}

test("ledger row navigation preserves filters and ignores controls and modified clicks", () => {
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  visit("https://workbench.invalid/runs?origin=live&status=failed");
  runsState = {
    data: { items: [baseRun], total: 1 },
    loading: false,
    refresh() {},
  };
  const rowComponent = findNode(
    RunsPage({}),
    (node) => node.props?.run === baseRun,
  );
  assert.ok(rowComponent);
  const row = rowComponent.type(rowComponent.props);
  const ledgerAddress = href;
  row.props.onClick(click({ target: new TestElement() }));
  assert.equal(new URL(href).pathname, "/runs/run_fictional");
  assert.equal(historyState.runsBack, new URL(ledgerAddress).pathname +
    new URL(ledgerAddress).search);
  for (const overrides of [
    { button: 1 },
    { ctrlKey: true },
    { metaKey: true },
    { shiftKey: true },
    { altKey: true },
    { defaultPrevented: true },
    { target: new TestElement(true) },
  ]) {
    visit(ledgerAddress);
    row.props.onClick(click({ target: new TestElement(), ...overrides }));
    assert.equal(href, ledgerAddress);
  }
});

function hookRuntime(context = {}) {
  const slots = [];
  const frames = [];
  let cursor = 0;
  let dirty = false;
  let effects = [];
  let hook;
  const sameDeps = (a, b) =>
    a?.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const reactHooks = {
    ...React,
    useContext: () => context,
    useSyncExternalStore: (_, getSnapshot) => getSnapshot(),
    useRef(initial) {
      const index = cursor++;
      if (!slots[index]) slots[index] = { value: { current: initial } };
      return slots[index].value;
    },
    useState(initial) {
      const index = cursor++;
      if (!slots[index]) {
        slots[index] = { value: typeof initial === "function" ? initial() : initial };
      }
      return [slots[index].value, (next) => {
        slots[index].value = typeof next === "function"
          ? next(slots[index].value)
          : next;
        dirty = true;
      }];
    },
    useCallback(callback, deps) {
      return reactHooks.useMemo(() => callback, deps);
    },
    useMemo(factory, deps) {
      const index = cursor++;
      if (!sameDeps(slots[index]?.deps, deps)) {
        slots[index] = { value: factory(), deps };
      }
      return slots[index].value;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (sameDeps(slots[index]?.deps, deps)) return;
      effects.push(() => {
        slots[index]?.cleanup?.();
        slots[index] = { deps, cleanup: effect() };
      });
    },
    useLayoutEffect(effect, deps) {
      reactHooks.useEffect(effect, deps);
    },
  };
  return {
    reactHooks,
    frames,
    render(nextHook) {
      if (nextHook) hook = nextHook;
      let result;
      do {
        dirty = false;
        cursor = 0;
        result = hook();
        frames.push(result);
        const pendingEffects = effects;
        effects = [];
        for (const effect of pendingEffects) effect();
      } while (dirty);
      return result;
    },
    unmount() {
      for (const slot of slots) slot.cleanup?.();
      slots.length = 0;
      effects = [];
    },
  };
}

function resourceHarness() {
  const api = { token: "", epoch: 0, runsCache: new Map(), articlesCache: new Map(),
    retryStatus: () => { api.statusRefreshes = (api.statusRefreshes || 0) + 1; } };
  let cacheToken = api.token;
  let cacheEpoch = api.epoch;
  const runtime = hookRuntime(api);
  const { reactHooks, frames } = runtime;
  const requests = [];
  const timers = new Map();
  const document = { hidden: false };
  let nextTimer = 0;
  const resource = evaluateModule(
    path.join(root, "api/hooks.ts"),
    (name) => {
      if (name === "react") return reactHooks;
      if (name === "./context") return { ApiContext: {} };
      if (name === "../format") return format;
      if (name === "./client") {
        const client = load(path.join(root, "api/client.ts"));
        return {
          ApiError: client.ApiError,
          withheldRunId: client.withheldRunId,
          pollingError: client.pollingError,
          errorMessage: (error) => error.message,
          request: (path, token, { signal, method, body }) => new Promise((resolve, reject) => {
            requests.push({ path, token, signal, method, body, resolve, reject });
          }),
        };
      }
      throw new Error(`Unexpected import: ${name}`);
    },
    {
      document,
      window: {
        setInterval(callback, interval) {
          timers.set(++nextTimer, { callback, interval });
          return nextTimer;
        },
        clearInterval(id) {
          timers.delete(id);
        },
      },
    },
  );
  let hook;
  return {
    api,
    document,
    requests,
    frames,
    timers,
    render(name, ...args) {
      if (name) hook = () => resource[name](...args);
      if (cacheToken !== api.token || cacheEpoch !== api.epoch) {
        cacheToken = api.token;
        cacheEpoch = api.epoch;
        api.runsCache = new Map();
        api.articlesCache = new Map();
      }
      return runtime.render(hook);
    },
    poll() {
      for (const { callback } of [...timers.values()]) callback();
    },
    unmount: runtime.unmount,
  };
}

test("same-epoch refresh and poll retain data; path changes clear and cancel", async () => {
  const harness = resourceHarness();
  let state = harness.render("useRuns", "limit=50");
  assert.equal(state.loading, true);
  assert.equal(state.refreshing, false);
  assert.equal([...harness.timers.values()][0].interval, 4000);
  const first = { items: [baseRun], total: 1 };
  harness.requests.at(-1).resolve(first);
  await settle();
  state = harness.render();
  assert.equal(state.data, first);
  assert.equal(state.loading, false);

  state.refresh();
  state = harness.render();
  assert.equal(state.data, first);
  assert.equal(state.loading, false);
  assert.equal(state.refreshing, true);
  harness.requests.at(-1).resolve(first);
  await settle();
  assert.equal(harness.render().refreshing, false);

  harness.poll();
  assert.equal(harness.render().data, first);
  assert.equal(harness.render().refreshing, true);
  const pollingRequest = harness.requests.at(-1);
  pollingRequest.reject(new Error("Fictional service outage"));
  await settle();
  state = harness.render();
  assert.equal(state.data, first);
  assert.equal(state.refreshing, false);
  assert.equal(state.error, "Fictional service outage");

  state.refresh();
  state = harness.render();
  assert.equal(state.data, first);
  assert.equal(state.error, "");
  assert.equal(state.refreshing, true);
  const retryRequest = harness.requests.at(-1);
  state = harness.render("useRuns", "limit=50&offset=50");
  assert.equal(state.data, null);
  assert.equal(state.loading, true);
  assert.equal(retryRequest.signal.aborted, true);
  // A stale response from the old path cannot replace this page.
  retryRequest.resolve({ items: [], total: 999 });
  await settle();
  assert.equal(harness.render().data, null);
  const currentRequest = harness.requests.at(-1);
  harness.unmount();
  assert.equal(currentRequest.signal.aborted, true);
  assert.equal(harness.timers.size, 0);
});

for (const [name, argument, data] of [
  ["useRuns", "limit=50", { items: [baseRun], total: 1 }],
  ["useRun", baseRun.id, baseRun],
  ["useArticles", "limit=50", { items: [], total: 0 }],
  ["useArticle", "ref_fictional", { latest_run_status: "running", runs: [] }],
  ["useQueue", undefined, { paused: true, running: [], queued: [] }],
  ["useWorkflow", undefined, { id: "workflow_fictional" }],
  ["useWorkflow", "workflow_fictional", { id: "workflow_fictional" }],
]) {
  for (const change of ["mode", "token"]) {
    const title = `${name}(${argument}): ${change} invalidates cached data`;
    test(title, async () => {
      const harness = resourceHarness();
      harness.render(name, argument);
      harness.requests.at(-1).resolve(data);
      await settle();
      let state = harness.render();
      assert.equal(state.data, data);

      // A same-epoch refresh failure retains every kind of resource.
      state.refresh();
      state = harness.render();
      assert.equal(state.data, data);
      assert.equal(state.refreshing, true);
      harness.requests.at(-1).reject(new Error("Fictional refresh failure"));
      await settle();
      state = harness.render();
      assert.equal(state.data, data);
      assert.equal(state.loading, false);
      assert.equal(state.refreshing, false);
      assert.equal(state.error, "Fictional refresh failure");

      state.refresh();
      state = harness.render();
      const staleRequest = harness.requests.at(-1);
      const staleSetData = state.setData;
      if (change === "mode") harness.api.epoch++;
      // Exercise token identity independently of the provider's epoch increment.
      else harness.api.token = "fictional-token";
      const firstFrame = harness.frames.length;
      state = harness.render();
      assert.equal(harness.frames[firstFrame].data, null);
      assert.equal(harness.frames[firstFrame].loading, true);
      assert.equal(harness.frames[firstFrame].error, "");
      assert.equal(state.data, null);
      assert.equal(state.loading, true);
      assert.equal(state.refreshing, false);
      assert.equal(staleRequest.signal.aborted, true);
      assert.equal(harness.requests.at(-1).token, harness.api.token);
      staleSetData(data);
      staleRequest.resolve(data);
      await settle();
      assert.equal(harness.render().data, null);

      harness.requests.at(-1).reject(new Error("Fictional reload failure"));
      await settle();
      state = harness.render();
      assert.equal(state.data, null);
      assert.equal(state.loading, false);
      assert.equal(state.refreshing, false);
      assert.equal(state.error, "Fictional reload failure");

      state.refresh();
      state = harness.render();
      assert.equal(state.data, null);
      assert.equal(state.loading, true);
      harness.requests.at(-1).resolve(data);
      await settle();
      state = harness.render();
      assert.equal(state.data, data);
      assert.equal(state.error, "");
      harness.unmount();
    });
  }
}

test("run polling handles visibility, overlap, completion and manual refresh", async () => {
  const harness = resourceHarness();
  harness.render("useRun", baseRun.id);
  assert.equal(harness.requests[0].path, `/api/runs/${baseRun.id}/details`);
  assert.equal([...harness.timers.values()][0].interval, 1500);
  harness.poll();
  assert.equal(harness.requests.length, 1);
  harness.requests[0].resolve({ ...baseRun, status: "running" });
  await settle();
  harness.render();
  harness.document.hidden = true;
  harness.poll();
  assert.equal(harness.requests.length, 1);
  harness.document.hidden = false;
  harness.poll();
  assert.equal(harness.requests.length, 2);
  harness.requests[1].resolve(baseRun);
  await settle();
  let state = harness.render();
  assert.equal(harness.timers.size, 0);
  state.refresh();
  state = harness.render();
  assert.equal(state.data, baseRun);
  assert.equal(state.refreshing, true);
  assert.equal(harness.requests.length, 3);
  harness.unmount();
});

test("disabling a workflow path clears data and cancels its request", async () => {
  const harness = resourceHarness();
  harness.render("useWorkflow", "fictional-workflow");
  harness.requests[0].resolve({ id: "fictional-workflow" });
  await settle();
  assert.ok(harness.render().data);
  const state = harness.render("useWorkflow", null);
  assert.equal(state.data, null);
  assert.equal(state.loading, false);
  assert.equal(state.refreshing, false);
  assert.equal(harness.requests[0].signal.aborted, true);
  assert.equal(harness.render("useWorkflow", "fictional-workflow").data, null);
  harness.unmount();
});

function navigationHarness() {
  const runtime = hookRuntime();
  const events = new EventTarget();
  const calls = [];
  const timeouts = new Map();
  let nextTimeout = 0;
  let address = "https://workbench.invalid/runs?origin=live&offset=50";
  const window = {
    scrollY: 0,
    setTimeout(callback, delay) {
      timeouts.set(++nextTimeout, { callback, delay });
      return nextTimeout;
    },
    clearTimeout(id) { timeouts.delete(id); },
    location: {
      get href() { return address; },
      get origin() { return new URL(address).origin; },
    },
    history: {
      state: { runsBack: "/benchmarks" },
      scrollRestoration: "auto",
      replaceState(state, _, url) {
        calls.push({ operation: "replace", state, url: String(url) });
        this.state = state;
        address = String(url);
      },
      pushState(state, _, url) {
        calls.push({ operation: "push", state, url: String(url) });
        this.state = state;
        address = String(url);
      },
    },
    scrollTo(x, y) {
      calls.push({ operation: "scroll", x, y });
      this.scrollY = y;
    },
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
  };
  const router = evaluateModule(
    path.join(root, "router.tsx"),
    (name) => name === "react" ? runtime.reactHooks : require(name),
    {
      window,
      document: {
        getElementById(id) {
          return { focus: (options) => calls.push({ operation: "focus", id, options }) };
        },
      },
    },
  );
  return {
    router,
    window,
    calls,
    runtime,
    timeouts,
    render: (ready = true) => runtime.render(() => {
      router.usePageNavigation();
      router.useScrollRestoration(ready);
    }),
    traverse(url, state) {
      address = url;
      window.history.state = state;
      window.dispatchEvent(new Event("popstate"));
    },
  };
}

test("history saves before push and restores traversal without focus or top reset", () => {
  const harness = navigationHarness();
  const { router, window, calls } = harness;
  assert.equal(window.history.scrollRestoration, "manual");
  harness.render();
  calls.length = 0;
  const ledgerAddress = window.location.href;
  window.scrollY = 1480;
  router.navigate("/runs/run_fictional", { state: { runsBack: "/runs?offset=50" } });
  assert.equal(calls[0].operation, "replace");
  assert.equal(calls[0].url, ledgerAddress);
  assert.equal(calls[0].state.scrollY, 1480);
  assert.equal(calls[0].state.runsBack, "/benchmarks");
  assert.equal(calls[1].operation, "push");
  const ledgerState = calls[0].state;
  harness.render();
  assert.deepEqual(calls.at(-2), { operation: "scroll", x: 0, y: 0 });
  assert.equal(calls.at(-1).operation, "focus");
  assert.equal(calls.at(-1).id, "main");
  assert.equal(calls.at(-1).options.preventScroll, true);

  window.scrollY = 720;
  const writes = calls.length;
  window.dispatchEvent(new Event("scroll"));
  assert.equal(calls.length, writes, "scroll events do not write browser history");
  const detailState = window.history.state;
  assert.equal(detailState.runsBack, "/runs?offset=50");
  calls.length = 0;
  harness.traverse(ledgerAddress, ledgerState);
  harness.render(false);
  assert.equal(calls.length, 0, "wait for ledger rows to render");
  harness.render(true);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { operation: "scroll", x: 0, y: 1480 });
  harness.render(true);
  assert.equal(calls.length, 1, "a background refresh must not restore again");

  calls.length = 0;
  harness.traverse("https://workbench.invalid/runs/run_fictional", detailState);
  harness.render();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { operation: "scroll", x: 0, y: 720 });

  calls.length = 0;
  router.navigate("/settings", { replace: true });
  harness.render();
  assert.deepEqual(calls.at(-2), { operation: "scroll", x: 0, y: 0 });
  assert.equal(calls.at(-1).operation, "focus");
});

test("history traversal between ledger queries restores each entry without a top reset", () => {
  const { router, window, calls, render, traverse } = navigationHarness();
  render();
  window.scrollY = 900;
  const firstAddress = window.location.href;
  router.navigate("/runs?offset=100");
  const firstState = calls.at(-2).state;
  assert.equal(window.history.state.scrollY, 900);
  calls.length = 0;
  render();
  assert.equal(calls.length, 0, "query navigation does not move page focus");
  traverse(firstAddress, firstState);
  render();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { operation: "scroll", x: 0, y: 900 });
});

test("ledger remounts render each cached query immediately while revalidating", async () => {
  const harness = resourceHarness();
  const first = { items: [baseRun], total: 120 };
  const second = { items: [{ ...baseRun, id: "run_second_page" }], total: 120 };
  harness.render("useRuns", "limit=50&offset=0");
  harness.requests.at(-1).resolve(first);
  await settle();
  harness.render();
  harness.render("useRuns", "limit=50&offset=50");
  harness.requests.at(-1).resolve(second);
  await settle();
  harness.render();
  harness.unmount();

  for (const [offset, expected] of [[0, first], [50, second]]) {
    const firstFrame = harness.frames.length;
    const requestCount = harness.requests.length;
    let state = harness.render("useRuns", `limit=50&offset=${offset}`);
    assert.equal(harness.frames[firstFrame].data, expected);
    assert.equal(harness.frames[firstFrame].loading, false);
    assert.equal(state.data, expected);
    assert.equal(state.refreshing, true);
    assert.equal(harness.requests.length, requestCount + 1);
    runsState = state;
    visit(`https://workbench.invalid/runs?offset=${offset}`);
    const html = render(load(path.join(root, "pages/RunsPage.tsx")).RunsPage);
    assert.ok(html.includes(expected.items[0].id));
    assert.ok(!html.includes("Loading runs…"));
    assert.ok(html.includes("Refreshing…"));

    harness.requests.at(-1).reject(new Error("Fictional revalidation failure"));
    await settle();
    state = harness.render();
    assert.equal(state.data, expected);
    assert.equal(state.error, "Fictional revalidation failure");
    harness.unmount();
  }
});

for (const change of ["mode", "token"]) {
  test(`ledger remount after ${change} cannot reuse a previous epoch query`, async () => {
    const harness = resourceHarness();
    const first = { items: [baseRun], total: 1 };
    harness.render("useRuns", "limit=50");
    harness.requests.at(-1).resolve(first);
    await settle();
    harness.render();
    harness.unmount();
    if (change === "mode") harness.api.epoch++;
    else harness.api.token = "fictional-token";
    const firstFrame = harness.frames.length;
    let state = harness.render("useRuns", "limit=50");
    assert.equal(harness.frames[firstFrame].data, null);
    assert.equal(state.loading, true);
    harness.requests.at(-1).reject(new Error("Fictional new service outage"));
    await settle();
    state = harness.render();
    assert.equal(state.data, null);
    assert.equal(state.error, "Fictional new service outage");
    harness.unmount();
  });
}

function ledgerHarness(pageName = "RunsPage") {
  const api = {
    token: "",
    epoch: 0,
    ledgerEpoch: { current: { token: "", epoch: 0 } },
    articlesLedgerEpoch: { current: { token: "", epoch: 0 } },
  };
  const runtime = hookRuntime();
  const readiness = [];
  let state = { data: null, loading: true, error: "", refresh() {} };
  const page = evaluateModule(
    path.join(root, `pages/${pageName}.tsx`),
    (name) => {
      if (name === "react") return runtime.reactHooks;
      if (name === "../api/hooks") {
        return { ...hooks, useApi: () => api, useRuns: () => state,
          useArticles: () => state,
          useArticleInventory: () => ({ data: { total: 1 } }) };
      }
      if (name === "../router") {
        return { ...router, useScrollRestoration: (ready) => readiness.push(ready) };
      }
      if (name.startsWith(".")) {
        const base = path.resolve(root, "pages", name);
        return load(base + (existsSync(base + ".tsx") ? ".tsx" : ".ts"));
      }
      return require(name);
    },
  );
  return {
    api,
    readiness,
    unmount: runtime.unmount,
    render(data, benchmark = false, fresh = true) {
      state = { data, fresh, loading: !data, error: "", refresh() {} };
      const tree = runtime.render(() => page[pageName]({ benchmark }));
      return renderToStaticMarkup(tree);
    },
  };
}

test("ledger clamps stale offsets to the last page and drops offset for empty results", () => {
  const harness = ledgerHarness();
  for (const [total, offset, expected] of [
    [27, 50, null],
    [127, 200, "100"],
    [100, 100, "50"],
    [1, 50, null],
    [0, 50, null],
    [0, 0, null],
  ]) {
    visit(`https://workbench.invalid/runs?origin=live&status=failed&offset=${offset}`);
    const html = harness.render({ items: [], total });
    assert.equal(operation, "replace");
    assert.equal(new URL(href).searchParams.get("offset"), expected);
    assert.equal(new URL(href).searchParams.get("origin"), "live");
    assert.equal(new URL(href).searchParams.get("status"), "failed");
    assert.ok(!html.includes("No runs match these filters"));
    assert.ok(!html.includes("class=\"pagination\""));
    assert.equal(harness.readiness.at(-1), false);
  }
  visit("https://workbench.invalid/runs?offset=50");
  const valid = harness.render({ items: [baseRun], total: 127 });
  assert.equal(new URL(href).searchParams.get("offset"), "50");
  assert.ok(valid.includes("51–51"));
  assert.equal(harness.readiness.at(-1), true);
  const noItems = harness.render({ items: [], total: 127 });
  assert.ok(noItems.includes("0 of 127 runs"));
  assert.ok(!noItems.includes("51–50"));
  harness.unmount();
});

for (const change of ["mode", "token"]) {
  test(`ledger drops offset on ${change}, including a switch while unmounted`, () => {
    const harness = ledgerHarness();
    for (const unmount of [false, true]) {
      visit("https://workbench.invalid/benchmarks?verdict=AUDIT_FAILED&offset=50");
      harness.render({ items: [baseRun], total: 127 }, true);
      if (unmount) harness.unmount();
      if (change === "mode") harness.api.epoch++;
      else harness.api.token += "fictional-token";
      harness.render(null, true);
      assert.equal(operation, "replace");
      assert.equal(new URL(href).searchParams.has("offset"), false);
      assert.equal(new URL(href).searchParams.get("verdict"), "AUDIT_FAILED");
    }
    harness.unmount();
  });
}

test("API provider replaces the query cache before rendering a new mode or token", async () => {
  const runtime = hookRuntime();
  const requests = [];
  const timers = [];
  const provider = evaluateModule(
    path.join(root, "api/context.tsx"),
    (name) => {
      if (name === "react") return runtime.reactHooks;
      if (name === "./client") {
        return {
          errorMessage: () => "Fictional status error",
          request: () => new Promise((resolve) => requests.push({ resolve })),
        };
      }
      return require(name);
    },
    {
      window: {
        setInterval(callback) { return timers.push(callback); },
        clearInterval() {},
      },
    },
  );
  const renderProvider = () => runtime.render(
    () => provider.ApiProvider({ children: null }).props.value,
  );
  let api = renderProvider();
  const initialCache = api.runsCache;
  initialCache.set("/api/runs?limit=50", { items: [baseRun], total: 1 });
  requests.at(-1).resolve({ mode: "live" });
  await settle();
  api = renderProvider();
  assert.equal(api.runsCache, initialCache);

  timers.at(-1)();
  requests.at(-1).resolve({ mode: "test" });
  await settle();
  let firstFrame = runtime.frames.length;
  api = renderProvider();
  assert.notEqual(runtime.frames[firstFrame].runsCache, initialCache);
  assert.equal(runtime.frames[firstFrame].runsCache.size, 0);
  assert.equal(api.epoch, 1);
  assert.equal(api.ledgerEpoch.current.epoch, 0, "remember an unmounted ledger epoch");

  const modeCache = api.runsCache;
  modeCache.set("/api/runs?limit=50", { items: [baseRun], total: 1 });
  api.setToken("fictional-token");
  firstFrame = runtime.frames.length;
  api = renderProvider();
  assert.notEqual(runtime.frames[firstFrame].runsCache, modeCache);
  assert.equal(runtime.frames[firstFrame].runsCache.size, 0);
  assert.equal(api.token, "fictional-token");
  assert.equal(api.epoch, 2);
  runtime.unmount();
});

test("cached totals never clamp or abort revalidation of a now-valid page", async () => {
  const resource = resourceHarness();
  const ledger = ledgerHarness();
  const query = "limit=50&offset=50";
  resource.render("useRuns", query);
  resource.requests.at(-1).resolve({ items: [], total: 27 });
  await settle();
  assert.equal(resource.render().fresh, true);
  resource.unmount();

  visit("https://workbench.invalid/runs?offset=50");
  let state = resource.render("useRuns", query);
  const request = resource.requests.at(-1);
  assert.equal(state.fresh, false);
  ledger.render(state.data, false, state.fresh);
  assert.equal(new URL(href).searchParams.get("offset"), "50");
  assert.equal(request.signal.aborted, false);
  request.resolve({ items: [baseRun], total: 51 });
  await settle();
  state = resource.render();
  assert.equal(state.fresh, true);
  const html = ledger.render(state.data, false, state.fresh);
  assert.equal(new URL(href).searchParams.get("offset"), "50");
  assert.ok(html.includes("51–51"));
  assert.equal(request.signal.aborted, false);
  resource.unmount();
  ledger.unmount();
});

test("a failed cached revalidation cannot authorize an offset clamp", async () => {
  const resource = resourceHarness();
  const ledger = ledgerHarness();
  resource.render("useRuns", "limit=50&offset=50");
  resource.requests.at(-1).resolve({ items: [], total: 27 });
  await settle();
  resource.render();
  resource.unmount();
  visit("https://workbench.invalid/runs?offset=50");
  resource.render("useRuns", "limit=50&offset=50");
  resource.requests.at(-1).reject(new Error("Fictional revalidation failure"));
  await settle();
  const state = resource.render();
  assert.equal(state.fresh, false);
  ledger.render(state.data, false, state.fresh);
  assert.equal(new URL(href).searchParams.get("offset"), "50");
  resource.unmount();
  ledger.unmount();
});

test("ledger cache retains at most 20 queries and evicts least recently used", async () => {
  const harness = resourceHarness();
  const query = (index) => `limit=50&offset=${index * 50}`;
  const key = (index) => `/api/runs?${query(index)}`;
  for (let index = 0; index < 20; index++) {
    harness.render("useRuns", query(index));
    harness.requests.at(-1).resolve({ items: [baseRun], total: 1500 });
    await settle();
    harness.render();
    assert.equal(harness.api.runsCache.size, index + 1);
  }
  harness.unmount();
  const count = harness.requests.length;
  assert.ok(harness.render("useRuns", query(0)).data);
  assert.equal(harness.requests.length, count + 1, "cache hits still revalidate");
  harness.render("useRuns", query(20));
  harness.requests.at(-1).resolve({ items: [baseRun], total: 1500 });
  await settle();
  harness.render();
  assert.equal(harness.api.runsCache.size, 20);
  assert.ok(harness.api.runsCache.has(key(0)), "a cached read updates recency");
  assert.ok(!harness.api.runsCache.has(key(1)), "oldest unused query is evicted");
  assert.ok(harness.api.runsCache.has(key(20)));
  harness.unmount();
  assert.equal(harness.api.runsCache.size, 20, "unmount keeps the bounded cache");
  assert.equal(harness.render("useRuns", query(1)).data, null);
  harness.unmount();
});

function detailNavigationHarness() {
  const harness = navigationHarness();
  let state = { data: null, loading: true, error: "" };
  const page = evaluateModule(
    path.join(root, "pages/RunDetailPage.tsx"),
    (name) => {
      if (name === "react") return harness.runtime.reactHooks;
      if (name === "../router") return harness.router;
      if (name === "../api/hooks") {
        return {
          useApi: () => ({ token: "", epoch: 0 }),
          useRun: () => state,
          useWorkflow: () => ({ data: null, error: "" }),
        };
      }
      if (name.startsWith(".")) {
        const base = path.resolve(root, "pages", name);
        return load(base + (existsSync(base + ".tsx") ? ".tsx" : ".ts"));
      }
      return require(name);
    },
  );
  return {
    ...harness,
    renderDetail(next = state) {
      state = next;
      return harness.runtime.render(() => page.RunDetailPage({ id: baseRun.id }));
    },
  };
}

test("run detail restores history only after its run or terminal error renders", () => {
  for (const error of [false, true]) {
    const harness = detailNavigationHarness();
    const { calls, window } = harness;
    harness.renderDetail();
    calls.length = 0;
    harness.traverse("https://workbench.invalid/runs/run_fictional", {
      scrollKey: "detail-saved",
      scrollY: 920,
    });
    window.scrollY = 0;
    harness.renderDetail();
    assert.equal(calls.length, 0, "loading detail must not consume restoration");
    harness.renderDetail(error
      ? { data: null, loading: false, error: "Fictional run load failure" }
      : {
          data: {
            ...baseRun,
            error: null,
            input: { removed_article: { content: "Fictional text." } },
            model: {},
            output: null,
            steps: [],
          },
          loading: false,
          error: "",
        });
    assert.deepEqual(calls, [{ operation: "scroll", x: 0, y: 920 }]);
    assert.equal(harness.timeouts.size, 0);
    window.scrollY = 123;
    harness.renderDetail({ data: null, loading: true, error: "" });
    harness.renderDetail({ data: null, loading: false, error: "Fictional error" });
    assert.equal(calls.length, 1, "later updates must not restore again");
    harness.runtime.unmount();
  }
});

test("history restoration expires after five seconds without moving later content", () => {
  const harness = detailNavigationHarness();
  harness.traverse("https://workbench.invalid/runs/run_fictional", {
    scrollKey: "slow-detail",
    scrollY: 1400,
  });
  harness.renderDetail();
  const timeout = [...harness.timeouts.values()][0];
  assert.equal(timeout.delay, 5000);
  timeout.callback();
  harness.window.scrollY = 230;
  harness.window.dispatchEvent(new Event("scroll"));
  harness.renderDetail({ data: null, loading: false, error: "Fictional timeout" });
  assert.equal(harness.calls.filter((call) => call.operation === "scroll").length, 0);
  harness.router.navigate("/settings");
  assert.equal(harness.calls.at(-2).state.scrollY, 230);
  harness.runtime.unmount();
});

const pollingFixture = {
  enabled: true, interval_minutes: 5, auto_audit_enabled: false,
  auto_audit_max_per_poll: 5, last: null,
  lookback_days: 7, history: [], last_success_at: null,
  polling_enabled: true, paused: false, running: false,
  reason: null, next_poll_at: null,
  source: Object.fromEntries(Object.keys(pollSettingsFixture).map((name) => [name, "env"])),
};

test("Settings waits for service and workflow content, or ends on either error", () => {
  const runtime = hookRuntime();
  let status = null;
  let error = "";
  let workflow = { data: null, error: "" };
  const readiness = [];
  const page = evaluateModule(
    path.join(root, "pages/SettingsPage.tsx"),
    (name) => {
      if (name === "react") return runtime.reactHooks;
      if (name === "../router") {
        return { ...router, useScrollRestoration: (ready) => readiness.push(ready) };
      }
      if (name === "../api/hooks") {
        return {
          useApi: () => ({ token: "" }),
          useServiceStatus: () => ({ status, error }),
          useWorkflow: () => workflow,
        };
      }
      if (name.startsWith(".")) {
        const base = path.resolve(root, "pages", name);
        return load(base + (existsSync(base + ".tsx") ? ".tsx" : ".ts"));
      }
      return require(name);
    },
  );
  const check = (expected) => {
    runtime.render(() => page.SettingsPage());
    assert.equal(readiness.at(-1), expected);
  };
  check(false);
  status = { mode: "test", polling: pollingFixture };
  check(false);
  workflow = { data: { id: "workflow_fictional" }, error: "" };
  check(true);
  status = null;
  check(false);
  error = "Fictional status error";
  workflow = { data: null, error: "" };
  check(true);
  error = "";
  workflow.error = "Fictional workflow error";
  check(true);
  // Settings renders the safe state labels rather than configuration alone.
  error = "";
  workflow = { data: null, error: "" };
  for (const [archive, label] of Object.entries({
    disabled: "Disabled · classification catalog and related-removal evidence unavailable",
    unchecked: "Not checked yet",
    ok: "OK",
    wallet_missing: "Archive wallet not found",
    wallet_unreadable: "Archive wallet cannot be read",
    auth_unavailable: "Archive sign-in failed",
    unreachable: "Archive unreachable",
    rejected: "Archive refused the request",
  })) {
    status = { mode: "live", archive_reads_enabled: true, archive,
      polling: pollingFixture };
    const tree = runtime.render(() => page.SettingsPage());
    const html = renderToStaticMarkup(tree);
    assert.ok(html.includes(label), archive);
    assert.ok(!html.includes(">Enabled</"), archive);
  }
  for (const [search, label] of Object.entries({
    disabled: "Disabled",
    unchecked: "SearXNG · not checked yet",
    ok: "SearXNG · reachable",
    unreachable: "SearXNG · unreachable at the configured SEARXNG_URL",
    rejected: "SearXNG · refused the request (is JSON output enabled?)",
    degraded: "SearXNG · reachable, but its search engines are not answering (blocked or rate-limited)",
  })) {
    status = { mode: "live", search_provider: "searxng", search,
      polling: pollingFixture };
    const html = renderToStaticMarkup(runtime.render(() => page.SettingsPage()));
    assert.ok(html.includes(label), search);
    assert.ok(!html.includes(">searxng</"), search);
  }
  for (const [polling, expected] of [
    [pollingFixture, ["Every 5 min", "Off", "Not yet"]],
    [{ ...pollingFixture, enabled: false, interval_minutes: 0 }, ["Off"]],
    [{ ...pollingFixture, auto_audit_enabled: true, last: {
      started_at: "2026-11-01T00:00:00Z", finished_at: "2026-11-01T00:00:01Z",
      outcome: "ok", reason: null, found: 7, new: 4, queued: 2, unreadable: 3,
    } }, ["On (up to 5 per poll)", "found 7, new 4, queued 2", "unreadable 3"]],
    ...["import_in_progress", "queue_paused", "archive_error", "archive_incomplete",
      "archive_page_failed", "archive_page_malformed", "archive_page_cap",
      "poll_error"].map(
      (reason) => [{ ...pollingFixture, last: {
        started_at: "2026-11-01T00:00:00Z",
        finished_at: "2026-11-01T00:00:01Z", outcome: "skipped", reason,
        found: 0, new: 0, queued: 0, unreadable: 1,
      } }, ["skipped:", {
        import_in_progress: "Import in progress", queue_paused: "Queue paused",
        archive_error: "Archive read failed", poll_error: "Polling failed",
        archive_incomplete: "Archive listing incomplete",
        archive_page_failed: "Archive page read failed",
        archive_page_malformed: "Archive page malformed",
        archive_page_cap: "Archive page limit reached",
      }[reason], "unreadable 1"]],
    ),
  ]) {
    status = { mode: "live", polling };
    const html = renderToStaticMarkup(runtime.render(() => page.SettingsPage()));
    for (const text of expected) assert.ok(html.includes(text), text);
  }
  status = { mode: "live", polling: { ...pollingFixture, history: [{
    started_at: "2026-11-01T00:00:00Z", finished_at: "2026-11-01T00:00:01Z",
    outcome: "skipped", reason: "archive_page_cap", found: 11, new: 5,
    updated: 3, queued: 2, unreadable: 1,
  }] } };
  const historyHtml = renderToStaticMarkup(runtime.render(() => page.SettingsPage()));
  for (const text of ["7 days", "Sync history", "Updated", "Archive page limit reached",
    "<td>11</td>", "<td>5</td>", "<td>3</td>", "<td>2</td>"]) {
    assert.ok(historyHtml.includes(text), text);
  }
  runtime.unmount();
});

function fictionalArticle(overrides = {}) {
  return {
    ref: "ref_fictional", title: "Fictional ledger report", source: "Imagined Ledger",
    host: "ledger.invalid", url: "https://ledger.invalid/report",
    published_at: "2026-11-01T08:00:00Z", removed_at: null,
    category: "live", status: "not_audited", latest_run_id: null,
    latest_run_status: null, latest_verdict: null, latest_run_at: null, run_count: 0,
    content: "An invented article for an offline interface check.",
    language: null, archive_id: null, snapshot: null,
    links: { original_url: "https://ledger.invalid/report", archive_url: null },
    runs: [], ...overrides,
  };
}

// Match DOMParser's scripting-disabled document and structural node interface.
// Template content deliberately stays outside childNodes, as in the browser.
function articleParse(html) {
  const adapt = (node) => ({
    nodeType: ({ "#text": 3, "#comment": 8, "#document": 9,
      "#document-fragment": 11 })[node.nodeName] ?? 1,
    nodeName: node.nodeName.toLowerCase(),
    nodeValue: node.nodeName === "#text" ? node.value : null,
    childNodes: [],
  });
  const source = parseHtml(html, { scriptingEnabled: false });
  const document = adapt(source);
  const stack = [{ source, target: document }];
  while (stack.length) {
    const { source, target } = stack.pop();
    for (const child of source.childNodes ?? []) {
      const adapted = adapt(child);
      target.childNodes.push(adapted);
      stack.push({ source: child, target: adapted });
    }
  }
  return document;
}

const articleHtml = load(path.join(root, "features/articles/articleHtml.ts"));
// VM-created arrays have another realm's prototype; compare plain local values.
const textBlocks = (html) => Array.from(
  articleHtml.htmlToBlocks(html, articleParse),
  (block) => ({ ...block }),
);
const prose = (...texts) => texts.map((text) => ({ text, pre: false }));

test("only archive text with markup is parsed; other text never changes", () => {
  const parseNever = () => { throw new Error("parsed non-archive or markup-free text"); };
  for (const text of ["x<y and z>w", "x<p+1 and y>0", "x<b and y>0", "<p>a</p>", "&amp;"]) {
    assert.equal(articleHtml.articleBlocks(text, false, parseNever), null, text);
  }
  assert.equal(articleHtml.articleBlocks("Plain archive text, no markup.", true, parseNever),
    null);
});

test("archive paragraphs follow HTML parsing, block boundaries and whitespace", () => {
  assert.deepEqual(textBlocks("<p>a<p>b"), prose("a", "b"));
  assert.deepEqual(textBlocks("<table><tr><td>a<td>b</table>"), prose("a · b"));
  assert.deepEqual(textBlocks("<dl><dt>t<dd>d</dl>"), prose("t", "d"));
  assert.deepEqual(textBlocks("<pre>  x\n  y</pre>"),
    [{ text: "  x\n  y", pre: true }]);
  assert.deepEqual(textBlocks("<p>a <code>b</code> c</p>"), prose("a b c"));
  assert.deepEqual(textBlocks("<p> a\t\n b&nbsp; c<br> d<br/>e </p>"),
    prose("a b c\nd\ne"));
  assert.deepEqual(textBlocks("<p> \t </p><pre></pre>"), []);
  assert.deepEqual(textBlocks("<table><tr><th>a<th>b<tr><td>c<td>d</table>"),
    prose("a · b", "c · d"));
  assert.deepEqual(textBlocks("<pre>x&nbsp;\t<code> y</code></pre>"),
    [{ text: "x\u00a0\t y", pre: true }]);
});

test("archive display skips hidden subtrees and preserves textarea text", () => {
  for (const hidden of [
    "<script>hidden</script>", "<style>hidden</style>",
    "<svg><svg>hidden<!-- hidden --></svg>hidden</svg>",
    "<math><mi>hidden</mi></math>",
    "<noscript><p>hidden</p></noscript>",
    "<template><p>hidden</p></template>",
    "<iframe>hidden</iframe>", "<object>hidden</object>", "<embed>",
    "<canvas>hidden</canvas>", "<select><option>hidden</option></select>",
    "<button>hidden</button>",
  ]) {
    assert.deepEqual(textBlocks(`<p>a</p>${hidden}<p>b</p>`), prose("a", "b"));
  }
  assert.deepEqual(textBlocks("<head><title>hidden</title></head><body>shown"),
    prose("shown"));
  assert.deepEqual(textBlocks("<textarea>a<b>c</textarea>"),
    [{ text: "a<b>c", pre: true }]);
  assert.deepEqual(textBlocks("<p>a</p><textarea>  x\n y</textarea><p>b</p>"),
    [...prose("a"), { text: "  x\n y", pre: true }, ...prose("b")]);
});

test("archive detection, leading noscript and block cells follow the review cases", () => {
  const blocks = (content) => Array.from(
    articleHtml.articleBlocks(content, true, articleParse) ?? [], (block) => ({ ...block }));
  assert.deepEqual(blocks('<p title="a<b">visible'), prose("visible"));
  assert.notEqual(articleHtml.articleBlocks('<img src="fictional.png">', true, articleParse), null);
  assert.deepEqual(blocks('<img src="fictional.png">'), []);
  assert.deepEqual(blocks("<noscript>hidden</noscript><p>shown</p>"), prose("shown"));
  assert.deepEqual(blocks("<table><tr><td><p>a</p></td><td><p>b</p></td></tr></table>"),
    prose("a", "b"));
  assert.deepEqual(blocks("<table><tr><td>a</td><td><b>b</b></td></tr></table>"), prose("a · b"));
  assert.deepEqual(blocks("<dl><dt>t<dd>d"), prose("t", "d"));
  assert.deepEqual(blocks("<noscript>hidden"), []);
  assert.deepEqual(blocks("<img src=fictional.png>"), []);
  assert.deepEqual(blocks("<table><tr><td>a</td><td><p>b</p></td></tr></table>"),
    prose("a", "b"));
  assert.deepEqual(blocks("<table><tr><td><p>a</p></td><td>b</td></tr></table>"),
    prose("a", "b"));
});

test("cell separators stay linear with many whitespace-only parts", () => {
  const n = 8_000;
  const row = "<table><tr><td>" + "<span> </span>".repeat(n) + "a</td>" +
    "<td>b</td>".repeat(n) + "</tr></table>";
  const document = articleParse(row);
  const start = performance.now();
  const [block] = articleHtml.htmlToBlocks(row, () => document);
  assert.equal(block.text, "a" + " · b".repeat(n));
  assert.ok(performance.now() - start < 200, "separator handling is not linear");
});

test("archive formatting stops at the tag limit", () => {
  const limit = articleHtml.MAX_TAG_OPENINGS;
  const within = "<p>a</p>" + "<b>x</b>".repeat(limit - 1);
  assert.notEqual(articleHtml.articleBlocks(within, true, articleParse), null);
  assert.equal(articleHtml.articleBlocks(within + "<b>y</b>", true, articleParse), null);
});

test("archive parser handles quoted attributes, comments and entities once", () => {
  assert.deepEqual(textBlocks('<p title="a>b<c">text</p>'), prose("text"));
  assert.deepEqual(textBlocks("<p>a</p><![CDATA[x]]><p>b</p>"), prose("a", "b"));
  assert.deepEqual(textBlocks("a<!-- x -->b"), prose("ab"));
  assert.deepEqual(textBlocks("&ldquo;Q&rdquo; &eacute; &scedil; &#351; &amp;lt;"),
    prose("“Q” é ş ş &lt;"));
});

function deepDivs(depth, text) {
  let node = { nodeType: 3, nodeName: "#text", nodeValue: text, childNodes: [] };
  for (let level = 0; level < depth; level++) {
    node = { nodeType: 1, nodeName: "DIV", nodeValue: null, childNodes: [node] };
  }
  return { nodeType: 9, nodeName: "#document", nodeValue: null, childNodes: [node] };
}

for (const [label, content, expected] of [
  ["64k stray angle brackets", "<".repeat(64_000), prose("<".repeat(64_000))],
  ["50k script openers", "<script>".repeat(50_000), []],
  ["50k SVG levels", "<svg>".repeat(50_000), []],
  ["50k div levels", "<div>".repeat(50_000) + "text", prose("text")],
]) {
  test(`archive HTML traversal handles ${label} within 1 second`, () => {
    // A 50k-deep tree checks the walker never recurses. It is built directly:
    // parse5 needs ~25 s for this depth, and the page never parses it (tag limit).
    const document = label === "50k div levels" ? deepDivs(50_000, "text") : null;
    const start = performance.now();
    if (label === "64k stray angle brackets") {
      const shown = articleHtml.articleBlocks(content, true, articleParse);
      assert.deepEqual(Array.from(shown, (block) => ({ ...block })), expected);
    } else {
      // Past the tag limit the page shows raw text without asking the parser at all.
      assert.equal(articleHtml.articleBlocks(content, true, () => {
        throw new Error("parsed despite the tag limit");
      }), null);
    }
    const blocks = articleHtml.htmlToBlocks(content,
      document ? () => document : articleParse);
    assert.deepEqual(Array.from(blocks, (block) => ({ ...block })), expected);
    assert.ok(performance.now() - start < 1_000, `${label} exceeded 1 second`);
  });
}

test("article markup formats archives only and falls back to exact raw text", () => {
  const { ArticleText } = load(path.join(root, "features/articles/ArticleText.tsx"));
  const content = "<p>First &amp; fictional.</p><p>Second.</p>";
  const render = (overrides) => renderToStaticMarkup(React.createElement(
    ArticleText, { article: fictionalArticle(overrides), parse: articleParse },
  ));
  for (const identity of [{ archive_id: "fictional" }, { ref: "ka-fictional" }]) {
    const html = render({ content, ...identity });
    assert.ok(html.includes("<p>First &amp; fictional.</p><p>Second.</p>"));
    assert.ok(!html.includes("&lt;p&gt;"));
  }
  const raw = render({ content });
  assert.ok(raw.includes('<div class="removed-text-body">' +
    "&lt;p&gt;First &amp;amp; fictional.&lt;/p&gt;&lt;p&gt;Second.&lt;/p&gt;</div>"));
  const fallback = render({
    content: "<script>hidden</script>", archive_id: "fictional",
  });
  assert.ok(!fallback.includes("hidden"));
  assert.ok(fallback.includes("The stored article has no readable text."));
  const pre = render({ content: "<pre>  x\n y</pre>", archive_id: "fictional" });
  assert.ok(pre.includes("<pre>  x\n y</pre>"));
  assert.ok(render({ content: "" }).includes("No article text is stored yet."));
});

function fictionalStory(overrides = {}) {
  return {
    verdict: "INCONCLUSIVE", verdict_label: "Inconclusive",
    headline: "The audit could not establish what happened.",
    sentences: ["Fictional summary sentence."], failed_steps: [], skipped_steps: [],
    human_review_note: "This is not a finding of censorship; a person decides.",
    steps: [
      { name: "classification", title: "Classify", status: "completed",
        outcome: "pass", line: "Fictional stored explanation." },
      { name: "source", title: "Verify the source", status: "skipped",
        outcome: null, line: "Skipped: fictional early stop." },
    ], ...overrides,
  };
}

function articleDetailHarness(initial = fictionalArticle()) {
  const runtime = hookRuntime();
  const api = { token: "", epoch: 0, articlesCache: new Map(),
    status: { mode: "live" } };
  let article = initial;
  let run = null;
  let tree;
  let refreshes = 0;
  const requests = [];
  const client = load(path.join(root, "api/client.ts"));
  const page = evaluateModule(path.join(root, "pages/ArticleDetailPage.tsx"), (name) => {
    if (name === "react") return runtime.reactHooks;
    if (name === "../api/hooks") return {
      useApi: () => api,
      useArticle: () => ({
        data: article, loading: false, error: "", setData: (next) => { article = next; },
        refresh: () => { refreshes++; },
      }),
      useRun: () => ({ data: run, error: "", loading: false, refresh() {},
        setData: (value) => { run = value; } }),
    };
    if (name === "../api/client") return {
      ...client,
      auditArticle: (ref, token, signal) => new Promise((resolve, reject) => {
        requests.push({ ref, token, signal, resolve, reject });
      }),
    };
    if (name.startsWith(".")) {
      const base = path.resolve(root, "pages", name);
      return load(base + (existsSync(base + ".tsx") ? ".tsx" : ".ts"));
    }
    return require(name);
  });
  return {
    api, requests, client,
    get article() { return article; },
    get refreshes() { return refreshes; },
    get run() { return run; },
    stopControl() {
      return findNode(tree, (node) => node.type?.name === "StopRunButton");
    },
    setArticle(value) { article = value; },
    setRun(value) { run = value; },
    render() {
      tree = runtime.render(() => page.ArticleDetailPage({ articleRef: article.ref }));
      return renderToStaticMarkup(tree);
    },
    start() {
      return findNode(tree, (node) =>
        node.props?.className === "button primary article-audit-button").props.onClick();
    },
    unmount: runtime.unmount,
  };
}

test("cancelled runs have a neutral status, no verdict and pending steps", () => {
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  const { RunDetailPage } = load(path.join(root, "pages/RunDetailPage.tsx"));
  const cancelled = {
    ...baseRun,
    status: "cancelled",
    verdict: null,
    expected_verdict: "INCONCLUSIVE",
    output: null,
    steps: [],
    model: {},
    error: { code: "cancelled", message: "A person stopped this audit." },
    input: {
      removed_article: {
        url: "https://fictional.invalid/stopped",
        content: "Fabricated stopped audit input.",
      },
    },
  };
  runState = { data: cancelled, error: "", refresh() {} };
  snapshotState = { data: null, error: "", refresh() {} };
  const html = render(RunDetailPage, { id: cancelled.id });
  assert.ok(html.includes("Stopped by a person"));
  assert.ok(html.includes("This is not a result"));
  assert.ok(html.includes('class="chip status-chip">Cancelled'));
  assert.equal((html.match(/>Pending</g) ?? []).length, 6);
  assert.ok(!html.includes("Pending verdict"));
  assert.ok(!html.includes('class="chip verdict-'));
  assert.ok(!html.includes("Expected verdict:"));
  assert.ok(!html.includes("Stop audit"));
  runsState = {
    data: { items: [cancelled], total: 1 },
    error: "",
    refresh() {},
  };
  visit("https://workbench.invalid/runs?status=cancelled");
  const ledger = render(RunsPage);
  assert.equal(capturedQuery.get("status"), "cancelled");
  assert.ok(ledger.includes('<option value="cancelled" selected="">'));
  assert.ok(!ledger.includes("Pending verdict"));
  assert.ok(!ledger.includes('class="chip verdict-'));
  for (const status of ["queued", "running"]) {
    runState = { ...runState, data: { ...cancelled, status, error: null } };
    assert.ok(render(RunDetailPage, { id: cancelled.id }).includes("Stop audit"));
  }
});

test("queue polling stays fast for pause or work and slows when idle", async () => {
  const harness = resourceHarness();
  harness.render("useQueue");
  for (const data of [
    { paused: true, running: [], queued: [] },
    { paused: false, running: ["run_one"], queued: [] },
    { paused: false, running: [], queued: ["run_two"] },
  ]) {
    assert.equal(harness.requests.at(-1).path, "/api/queue");
    harness.requests.at(-1).resolve(data);
    await settle();
    assert.equal(harness.render().data, data);
    assert.equal(harness.timers.size, 1);
    assert.equal([...harness.timers.values()][0].interval, 1500);
    harness.poll();
  }
  harness.requests.at(-1).resolve({ paused: false, running: [], queued: [] });
  await settle();
  const state = harness.render();
  assert.equal(harness.timers.size, 1);
  assert.equal([...harness.timers.values()][0].interval, 30000);
  state.refresh();
  harness.render();
  assert.equal(harness.timers.size, 1);
  harness.unmount();
});

test("run cancellation stops polling and mutation uses safe conflict feedback", async () => {
  const harness = resourceHarness();
  harness.render("useRun", baseRun.id);
  harness.requests.at(-1).resolve({ ...baseRun, status: "cancelled", verdict: null });
  await settle();
  harness.render();
  assert.equal(harness.timers.size, 0);
  harness.unmount();

  const control = resourceHarness();
  let state = control.render("useRunControl");
  const endpoint = `/api/runs/${baseRun.id}/cancel`;
  const result = state.mutate(endpoint);
  assert.equal(control.requests[0].path, endpoint);
  assert.equal(control.requests[0].method, "POST");
  assert.equal(control.render().busy, true);
  assert.equal(await control.render().mutate(endpoint), false);
  const { ApiError } = load(path.join(root, "api/client.ts"));
  control.requests[0].reject(new ApiError(409));
  assert.equal(await result, false);
  state = control.render();
  assert.equal(state.error, "This audit has already finished.");
  assert.equal(state.busy, false);
  const retry = state.mutate(endpoint);
  control.requests[1].resolve({});
  assert.equal(await retry, true);
  state = control.render();
  assert.equal(state.error, "");
  assert.equal(state.busy, false);
  const stale = state.mutate(endpoint);
  control.api.epoch++;
  control.render();
  assert.equal(control.requests[2].signal.aborted, true);
  control.requests[2].resolve({});
  assert.equal(await stale, false);
  assert.equal(control.render().busy, false);
  // A stop that took effect but whose result the service withheld counts as done.
  const stopped = [];
  const withheld = control.render().mutate(endpoint, (data) => stopped.push(data));
  control.requests[3].reject(new ApiError(409, "result_withheld", baseRun.id));
  assert.equal(await withheld, true);
  assert.deepEqual(stopped, [null]);
  assert.equal(control.render().error, "");
  control.unmount();
});

test("a withheld article audit is followed, not reported as a failure", async () => {
  const harness = articleDetailHarness();
  harness.render();
  const action = harness.start();
  harness.requests[0].reject(new harness.client.ApiError(409, "result_withheld", "run_new"));
  await action;
  const html = harness.render();
  assert.ok(!html.includes("An audit for this article is already running."));
  assert.equal(harness.refreshes, 1);
  harness.unmount();
});

function controlHarness(file, name, props) {
  const runtime = hookRuntime();
  const module = evaluateModule(
    path.join(root, file),
    (request) => {
      if (request === "react") return runtime.reactHooks;
      if (request === "../api/hooks") return hooks;
      if (!request.startsWith(".")) return require(request);
      const base = path.resolve(root, path.dirname(file), request);
      return load(base + (existsSync(base + ".tsx") ? ".tsx" : ".ts"));
    },
  );
  return {
    render: () => runtime.render(() => module[name](props)),
    unmount: runtime.unmount,
  };
}

test("root replaces to Articles and the shell routes both article addresses", () => {
  const runtime = hookRuntime();
  visit("https://workbench.invalid/");
  const app = evaluateModule(path.join(root, "App.tsx"), (name) => {
    if (name === "react") return runtime.reactHooks;
    if (name === "./router") return { ...router, usePageNavigation() {} };
    if (name === "./api/context") return { ApiProvider: () => null };
    if (name.startsWith(".")) return {};
    return require(name);
  }, { document: { title: "" } });
  const workbench = app.default().props.children;
  const routePage = workbench.type().props.children;
  runtime.render(() => routePage.type());
  assert.equal(new URL(href).pathname, "/articles");
  assert.equal(operation, "replace");
  assert.equal(router.matchRoute("/articles/ref_fictional").name, "article");
  runtime.unmount();
});

test("Articles sends search, status, verdict, category and paging to the API", () => {
  const { ArticlesPage } = load(path.join(root, "pages/ArticlesPage.tsx"));
  const article = fictionalArticle({ status: "failed", latest_verdict: "AUDIT_FAILED" });
  articlesState = { data: { items: [article], total: 120 }, loading: false,
    error: "", refresh() {} };
  visit("https://workbench.invalid/articles?q=fictional&status=failed" +
    "&verdict=AUDIT_FAILED&category=benchmark&offset=50");
  let html = render(ArticlesPage);
  for (const [name, value] of Object.entries({ q: "fictional", status: "failed",
    verdict: "AUDIT_FAILED", category: "benchmark", offset: "50", limit: "50" })) {
    assert.equal(capturedQuery.get(name), value);
  }
  assert.ok(html.includes('/articles/ref_fictional'));
  assert.ok(html.includes('aria-label="Article ledger"'));
  assert.ok(html.includes("51–51 of 120 articles"));
  visit("https://workbench.invalid/articles");
  render(ArticlesPage);
  assert.equal(capturedQuery.has("category"), false);
  visit("https://workbench.invalid/articles?category=all");
  render(ArticlesPage);
  assert.equal(capturedQuery.has("category"), false);
  visit("https://workbench.invalid/articles?category=live");
  render(ArticlesPage);
  assert.equal(capturedQuery.get("category"), "live");
  articlesState.data = { items: [], total: 0 };
  html = render(ArticlesPage);
  assert.ok(html.includes("No articles match these filters"));
  assert.ok(html.includes('href="/articles"'));
  articlesState.data = { items: [article], total: 1 };
  visit("https://workbench.invalid/articles?q=fictional");
  const tree = ArticlesPage();
  const search = findNode(tree, (node) => node.type === "input");
  search.props.onChange({ target: { value: "new query" } });
  assert.equal(new URL(href).searchParams.get("q"), "new query");
  assert.equal(operation, "replace");
  const rowComponent = findNode(tree, (node) => node.props?.article === article);
  const row = rowComponent.type(rowComponent.props);
  const ledgerAddress = href;
  for (const overrides of [{ ctrlKey: true }, { metaKey: true }, { button: 1 },
    { shiftKey: true }, { altKey: true }, { target: new TestElement(true) }]) {
    row.props.onClick(click({ target: new TestElement(), ...overrides }));
    assert.equal(href, ledgerAddress);
  }
  row.props.onClick(click({ target: new TestElement() }));
  assert.equal(new URL(href).pathname, "/articles/ref_fictional");
  assert.ok(historyState.articlesBack.includes("q=fictional"));
});

test("article view shows first audit, re-run, running progress and one review note", () => {
  const harness = articleDetailHarness();
  let html = harness.render();
  assert.ok(html.includes("Run first audit"));
  assert.ok(html.includes("No audit yet."));
  assert.ok(html.includes("Runs the audit with the current workflow."));
  assert.ok(!html.includes("earlier runs stay in history"));
  assert.ok(!html.includes("No longer available"));
  const summary = fictionalStory();
  const latest = { ...baseRun, summary, status: "succeeded", verdict: "INCONCLUSIVE" };
  harness.setArticle(fictionalArticle({ runs: [latest], run_count: 1 }));
  html = harness.render();
  assert.ok(html.includes("Re-run audit"));
  assert.ok(html.includes("What the audit did"));
  assert.ok(html.includes("Starts a new run; earlier runs stay in history."));
  assert.ok(html.includes("Skipped: fictional early stop."));
  assert.ok(html.includes("Fictional stored explanation."));
  // The stored note is the one disclaimer: it appears once, never next to a second copy.
  assert.equal(html.split(summary.human_review_note).length - 1, 1);
  const otherNote = "Fictional stored review note.";
  for (const [note, shown] of [[otherNote, otherNote], [null, "This is not a finding of censorship; a person decides."]]) {
    const noted = { ...latest, summary: { ...summary, human_review_note: note } };
    harness.setArticle(fictionalArticle({ runs: [noted], run_count: 1 }));
    const notedHtml = harness.render();
    assert.equal(notedHtml.split(shown).length - 1, 1);
    if (note) assert.ok(!notedHtml.includes("This is not a finding of censorship; a person decides."));
  }
  harness.setArticle(fictionalArticle({ runs: [latest], run_count: 1 }));
  html = harness.render();
  assert.ok(html.includes(`/runs/${latest.id}`));
  harness.setArticle(fictionalArticle({ runs: [{ ...latest,
    status: "running", summary: fictionalStory({ steps: [
      ...summary.steps.slice(0, 1), { ...summary.steps[1], status: "running" },
    ] }) }], run_count: 1 }));
  html = harness.render();
  assert.ok(html.includes("Audit in progress"));
  assert.ok(html.includes("Audit running · step 2 of 6"));
  assert.ok(html.includes('disabled=""'));
  harness.unmount();
});

test("Articles offers archive import only in live mode with archive reads, and guides the empty page", () => {
  const { ArticlesPage } = load(path.join(root, "pages/ArticlesPage.tsx"));
  const originalApi = hooks.useApi;
  const originalInventory = hooks.useArticleInventory;
  const withStatus = (status) => () => ({ ...originalApi(),
    status: { polling: pollingFixture, ...status }, articlesCache: new Map() });
  const live = { mode: "live", archive_reads_enabled: true };
  try {
    hooks.useArticleInventory = () => ({ data: { total: 0 }, loading: false });
    articlesState = { data: { items: [], total: 0 }, loading: false, error: "", refresh() {} };
    visit("https://workbench.invalid/articles");
    hooks.useApi = withStatus(live);
    let html = render(ArticlesPage);
    assert.ok(html.includes("Import from archive"));
    assert.ok(html.includes("Use Import from archive above"));
    assert.ok(!html.includes("Last sync"));
    hooks.useApi = withStatus({ ...live, polling: { ...pollingFixture,
      last_success_at: "2026-11-01T10:15:00Z" } });
    assert.ok(render(ArticlesPage).includes("Last sync"));
    hooks.useApi = withStatus({ ...live, polling: { ...pollingFixture,
      enabled: false, last_success_at: "2026-11-01T10:15:00Z" } });
    assert.ok(!render(ArticlesPage).includes("Last sync"));
    hooks.useApi = withStatus({ mode: "live", archive_reads_enabled: false });
    html = render(ArticlesPage);
    assert.ok(!html.includes(">Import from archive<"));
    assert.ok(html.includes("Archive reads are off on this server"));
    hooks.useApi = withStatus({ mode: "test", archive_reads_enabled: false });
    html = render(ArticlesPage);
    assert.ok(!html.includes(">Import from archive<"));
    assert.ok(html.includes("Articles appear here when imported or submitted for audit."));
    hooks.useApi = withStatus(live);
    hooks.useArticleInventory = originalInventory;
    articlesState.data = { items: [
      fictionalArticle({ ref: "ka-1", removed_at: null, removal_checked: false }),
      fictionalArticle({ ref: "ka-2", removed_at: null, removal_checked: true }),
    ], total: 2 };
    html = render(ArticlesPage);
    assert.equal(html.split("Not checked yet").length - 1, 1);
  } finally {
    hooks.useApi = originalApi;
    hooks.useArticleInventory = originalInventory;
  }
});

test("archive import submits the day count, reports counts or the safe error code", async () => {
  const runtime = hookRuntime();
  const client = load(path.join(root, "api/client.ts"));
  const api = { token: "fictional-token", epoch: 0, articlesCache: new Map([["q", {}]]),
    status: { mode: "live", archive_reads_enabled: true } };
  const calls = [];
  let refreshed = 0;
  const module = evaluateModule(path.join(root, "components/ArchiveImport.tsx"), (name) => {
    if (name === "react") return runtime.reactHooks;
    if (name === "../api/hooks") return { useApi: () => api };
    if (name === "../api/client") return { ...client, importArticles: (days, token) =>
      new Promise((resolve, reject) => { calls.push({ days, token, resolve, reject }); }) };
    if (name.startsWith(".")) return load(path.join(root, "components", name.slice(2)) + ".tsx");
    return require(name);
  });
  const view = () => runtime.render(() => module.ArchiveImport({ onImported: () => { refreshed++; } }));
  try {
    let tree = view();
    const form = () => findNode(tree, (node) => node.type === "form");
    findNode(tree, (node) => node.type === "input").props.onChange({ target: { value: "7" } });
    tree = view();
    // Two submits before the next render: only one import may start.
    const handler = form().props.onSubmit;
    const submitted = handler({ preventDefault() {} });
    const doubled = handler({ preventDefault() {} });
    assert.equal(calls.length, 1);
    await doubled;
    tree = view();
    assert.ok(renderToStaticMarkup(tree).includes("Importing…"));
    assert.equal(calls[0].days, 7);
    assert.equal(calls[0].token, "fictional-token");
    calls[0].resolve({ imported: 3, updated: 1, skipped: 2 });
    await submitted;
    tree = view();
    assert.ok(renderToStaticMarkup(tree).includes("Imported 3 · updated 1 · skipped 2"));
    // The result sits with the help text in the width-neutral notes row (see the layout check).
    const notes = findNode(tree, (node) => node.props?.className === "archive-import-notes");
    assert.ok(renderToStaticMarkup(notes).includes("Imported 3 · updated 1 · skipped 2"));
    assert.ok(renderToStaticMarkup(notes).includes('id="archive-import-help"'));
    assert.equal(api.articlesCache.size, 0);
    assert.equal(refreshed, 1);
    const failed = form().props.onSubmit({ preventDefault() {} });
    calls[1].reject(new client.ApiError(409, "archive_unavailable"));
    await failed;
    tree = view();
    const html = renderToStaticMarkup(tree);
    assert.ok(html.includes("(archive_unavailable)"));
    assert.ok(!html.includes("Imported 3"));
    const errorNotes = findNode(tree, (node) => node.props?.className === "archive-import-notes");
    assert.ok(renderToStaticMarkup(errorNotes).includes("(archive_unavailable)"),
      "the error sits in the notes row, never as a direct flex item of the form");
    assert.equal(refreshed, 1);
    findNode(tree, (node) => node.type === "input").props.onChange({ target: { value: "91" } });
    tree = view();
    assert.equal(findNode(tree, (node) => node.type === "button").props.disabled, true);
    await form().props.onSubmit({ preventDefault() {} });
    assert.equal(calls.length, 2);
  } finally {
    runtime.unmount();
  }
  assert.equal(module.importError(new client.ApiError(401)),
    "Check the API token in Settings before importing.");
  assert.equal(module.importError(new client.ApiError(401, "unauthorized")),
    "Check the API token in Settings before importing. (unauthorized)");
  assert.ok(module.importError(new client.ApiError(403, "untrusted_origin"))
    .includes("ALLOWED_ORIGINS"));
  assert.ok(!module.importError(new client.ApiError(403, "untrusted_origin")).includes("token"));
  assert.equal(module.importError(new client.ApiError(403, "fictional_refusal")),
    "The service could not complete this request. Please try again. (fictional_refusal)");
  assert.equal(module.importError(new client.ApiError(422, "validation_error")),
    "Choose between 1 and 90 days. (validation_error)");
  assert.ok(module.importError(new client.ApiError(409, "import_in_progress"))
    .startsWith("An import is already running."));
});

test("ledger heading keeps its title column when the import shows a result or an error", () => {
  // There is no layout engine here, so check the two rules that prevent the collapse: at
  // max-content the import form is one long line (about 1240 px) and its auto column took the
  // whole row, leaving the title column 0 px wide.
  const css = readFileSync(path.join(root, "styles/ledger.css"), "utf8");
  const rule = (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(css);
    assert.ok(match, `${selector} rule exists`);
    return match[1];
  };
  const columns = /grid-template-columns:\s*([^;]+);/.exec(rule(".ledger-heading"))[1];
  const titleMin = /^minmax\(\s*(.+?)\s*,\s*1fr\s*\)/.exec(columns.trim());
  assert.ok(titleMin, `title column is a minmax track: ${columns}`);
  assert.notEqual(titleMin[1], "0", "title column needs a non-zero minimum");
  const notes = rule(".archive-import-notes");
  assert.match(notes, /contain:\s*inline-size/);
  assert.match(notes, /flex-basis:\s*100%/);
  assert.ok(!/\.archive-import\s*>\s*\.(help|error-box)/.test(css),
    "import text lives only in the notes row, never as a direct flex item");
});

test("article rail offers Stop audit only while the latest audit is queued or running", () => {
  for (const status of ["queued", "running"]) {
    const latest = { ...baseRun, id: `run_${status}`, status, verdict: null,
      summary: fictionalStory({ verdict: null }) };
    const harness = articleDetailHarness(fictionalArticle({ runs: [latest], run_count: 1 }));
    harness.api.articlesCache.set("fictional query", { items: [] });
    const html = harness.render();
    assert.ok(html.includes("Audit in progress"));
    assert.ok(html.includes(">Stop audit<"), status);
    const control = harness.stopControl();
    assert.equal(control.props.run.id, latest.id);
    assert.equal(control.props.run.status, status);
    const before = harness.refreshes;
    const stopped = { ...baseRun, id: latest.id, status: "cancelled", verdict: null };
    harness.setRun({ ...latest, steps: [], input: {}, output: null });
    harness.render();
    harness.stopControl().props.onStopped(resultFixture(stopped));
    assert.equal(harness.run.id, stopped.id);
    assert.equal(harness.run.status, "cancelled");
    assert.equal(harness.api.articlesCache.size, 0);
    assert.equal(harness.refreshes, before + 1);
    harness.unmount();
  }
  // The run poll can report the stop before the cancel request answers; the button then
  // unmounts and its callback never fires. The page must still refresh on the transition.
  {
    const latest = { ...baseRun, id: "run_polled", status: "running", verdict: null,
      summary: fictionalStory({ verdict: null }) };
    const harness = articleDetailHarness(fictionalArticle({ runs: [latest], run_count: 1 }));
    harness.api.articlesCache.set("fictional query", { items: [] });
    harness.render();
    const before = harness.refreshes;
    harness.setRun({ ...baseRun, id: "run_polled", status: "cancelled", verdict: null, steps: [] });
    harness.render();
    assert.equal(harness.refreshes, before + 1);
    assert.equal(harness.api.articlesCache.size, 0);
    assert.equal(harness.stopControl(), null);
    harness.render();
    assert.equal(harness.refreshes, before + 1, "refreshes once per transition");
    harness.unmount();
  }
  for (const status of ["succeeded", "failed", "cancelled"]) {
    const latest = { ...baseRun, status, summary: fictionalStory() };
    const harness = articleDetailHarness(fictionalArticle({ runs: [latest], run_count: 1 }));
    assert.ok(!harness.render().includes(">Stop audit<"), status);
    assert.equal(harness.stopControl(), null);
    harness.unmount();
  }
});

test("saved live and benchmark articles can be audited in both modes", async () => {
  for (const [mode, category] of [
    ["live", "live"],
    ["test", "live"],
    ["live", "benchmark"],
    ["test", "benchmark"],
  ]) {
    const harness = articleDetailHarness(fictionalArticle({ category }));
    harness.api.status = { mode };
    const html = harness.render();
    const button = html.match(/<button[^>]*article-audit-button[^>]*>/)?.[0];
    assert.ok(button);
    assert.ok(!button.includes('disabled=""'));
    assert.ok(!html.includes("Live audits are switched off on this server."));
    assert.ok(!button.includes("aria-describedby"));
    const action = harness.start();
    assert.equal(harness.requests.length, 1);
    assert.equal(harness.requests[0].ref, harness.article.ref);
    harness.requests[0].reject(new harness.client.ApiError(404));
    await action;
    assert.ok(harness.render().includes(
      "This resource is unavailable. Refresh the list and try again.",
    ));
    harness.unmount();
  }
});

test("audit story numbers evidence footnotes from 1 across steps", () => {
  const { AuditStory } = load(path.join(root, "features/steps/AuditStory.tsx"));
  const summary = fictionalStory({ steps: [
    { name: "similarity", title: "Find a moved copy", status: "completed",
      outcome: "inconclusive", line: "Fictional candidates." },
    { name: "source", title: "Verify the source", status: "completed",
      outcome: "pass", line: "Fictional source check." },
  ] });
  const run = { ...baseRun, steps: [
    { name: "similarity", status: "completed", evidence: [], output: { details: {
      candidates: [{ url: "https://paper.invalid/a", status: 200 },
        { url: "https://paper.invalid/b", status: 404 }] } } },
    { name: "source", status: "completed", evidence: [], output: { details: {
      attribution_quote: "A fictional quote" } } },
  ] };
  const html = render(AuditStory, { summary, run });
  assert.deepEqual([...html.matchAll(/<li value="(\d+)"/g)].map((m) => m[1]),
    ["1", "2", "3"]);
});

test("article POST presents the accepted run immediately and preserves history", async () => {
  const old = { ...baseRun, summary: fictionalStory() };
  const harness = articleDetailHarness(fictionalArticle({ runs: [old], run_count: 1 }));
  harness.render();
  const action = harness.start();
  assert.ok(harness.render().includes("Starting audit…"));
  const accepted = { ...baseRun, id: "run_new", origin: "article", status: "queued",
    verdict: null, summary: fictionalStory({ verdict: null, steps: [] }) };
  assert.equal(harness.requests[0].ref, "ref_fictional");
  harness.requests[0].resolve(resultFixture(accepted));
  await action;
  const html = harness.render();
  assert.equal(harness.article.runs[0].id, "run_new");
  assert.equal(harness.article.runs[1].id, old.id);
  assert.equal(harness.article.run_count, 2);
  assert.equal(harness.refreshes, 1);
  assert.ok(html.includes("Audit running · step 1 of 6"));
  harness.unmount();
});

for (const [status, message] of [
  [409, "An audit for this article is already running."],
  [401, "Check the API token in Connection settings"],
  [403, "Check the API token in Connection settings"],
  [404, "This resource is unavailable. Refresh the list and try again."],
]) {
  test(`article POST ${status} shows a safe inline message`, async () => {
    const harness = articleDetailHarness();
    harness.render();
    const action = harness.start();
    harness.requests[0].reject(new harness.client.ApiError(status));
    await action;
    const html = harness.render();
    assert.ok(html.includes(message));
    assert.equal(harness.refreshes, status === 409 ? 1 : 0);
    if ([401, 403].includes(status)) assert.ok(html.includes('href="/settings"'));
    harness.unmount();
  });
}

test("service epoch changes abort article submissions and discard action errors", async () => {
  const harness = articleDetailHarness();
  harness.render();
  const action = harness.start();
  harness.api.epoch++;
  harness.render();
  assert.equal(harness.requests[0].signal.aborted, true);
  harness.requests[0].resolve({ ...baseRun, id: "run_stale" });
  await action;
  const html = harness.render();
  assert.ok(html.includes("Run first audit"));
  assert.equal(harness.article.runs.length, 0);
  harness.unmount();
});

test("article polling completes and refresh restarts it after a first audit", async () => {
  const harness = resourceHarness();
  harness.render("useArticle", "ref_fictional");
  harness.requests[0].resolve(fictionalArticle());
  await settle();
  let state = harness.render();
  assert.equal(harness.timers.size, 0);
  state.refresh();
  harness.render();
  harness.requests.at(-1).resolve(fictionalArticle({ latest_run_status: "running" }));
  await settle();
  harness.render();
  assert.equal(harness.timers.size, 1);
  harness.document.hidden = true;
  const count = harness.requests.length;
  harness.poll();
  assert.equal(harness.requests.length, count);
  harness.document.hidden = false;
  harness.poll();
  harness.poll();
  assert.equal(harness.requests.length, count + 1);
  harness.requests.at(-1).resolve(fictionalArticle({ latest_run_status: "succeeded" }));
  await settle();
  state = harness.render();
  assert.equal(harness.timers.size, 0);
  assert.equal(state.data.latest_run_status, "succeeded");
  harness.unmount();
});

test("article list cached queries survive remount and isolate token epochs", async () => {
  const harness = resourceHarness();
  const first = { items: [fictionalArticle()], total: 1, limit: 50, offset: 0 };
  harness.render("useArticles", "category=live&limit=50");
  harness.requests.at(-1).resolve(first);
  await settle();
  harness.render();
  harness.unmount();
  assert.equal(harness.render("useArticles", "category=live&limit=50").data, first);
  harness.unmount();
  harness.api.token = "fictional-token";
  assert.equal(harness.render("useArticles", "category=live&limit=50").data, null);
  harness.unmount();
});

test("story evidence uses stored measurements and never treats a failed probe as dead", () => {
  const { evidenceSummary, originalIsDead } = load(
    path.join(root, "features/steps/evidenceSummary.ts"),
  );
  const original = "https://ledger.invalid/report";
  const step = { name: "source", output: { details: {
    attribution_quote: "Fictional credited passage.", self_containment: 0.994,
    dead_source: { url: "https://source.invalid/report", status: 404 },
  } }, evidence: [{ type: "probe_removed_url", output: null }] };
  assert.equal(originalIsDead(step, original), false);
  let lines = evidenceSummary(step);
  assert.ok(lines.some((line) => line.text.includes("99.4% containment")));
  assert.ok(lines.some((line) => line.text.includes("HTTP 404")));
  assert.ok(lines.some((line) => line.quote === "Fictional credited passage."));
  for (const status of [403, 404, 410, 500]) {
    step.evidence[0].output = { url: original, status };
    assert.equal(originalIsDead(step, original), [404, 410].includes(status));
  }
  step.evidence[0].output = { url: "https://source.invalid/report", status: 404 };
  assert.equal(originalIsDead(step, original), false);
  lines = evidenceSummary({ name: "web_search", output: { details: {
    matches: ["https://copy.invalid/report"], hits_searched: 7, live_pages_read: 2,
    search_queries: 3, search_queries_failed: 1, web_hits: 4,
    archive_candidates: 2, cited_source_leads: 1,
  } } });
  assert.ok(lines.some((line) => line.text === "Story match · copy.invalid"));
  for (const text of ["7 total candidates", "4 search hits", "2 archive candidates",
    "1 cited-source leads", "3 search queries attempted", "1 search queries failed"]) {
    assert.ok(lines.some((line) => line.text === text), text);
  }
  const failed = evidenceSummary({ name: "web_search", output: { details: {
    search_queries: 3, search_queries_failed: 3, hits_searched: 2,
    web_hits: 0, archive_candidates: 2, cited_source_leads: 0,
  } } });
  assert.ok(failed.some((line) =>
    line.text === "Web search failed; the web was not searched."));
  const historical = evidenceSummary({ name: "web_search", output: { details: {
    hits_searched: 5,
  } } });
  assert.equal(historical.length, 1);
  assert.equal(historical[0].text, "5 total candidates");
  assert.equal(evidenceSummary({ name: "classification", output: {} }).length, 0);
});

test("evidence lines say why a page could not be read", () => {
  const { evidenceSummary } = load(path.join(root, "features/steps/evidenceSummary.ts"));
  const lines = evidenceSummary({ name: "similarity", output: { details: { candidates: [
    { url: "https://ledger.invalid/a", status: "unreadable",
      reason: "HTTP 403 (blocked/erroring, not gone)" },
    { url: "https://ledger.invalid/b", status: "unreadable", reason: "ConnectError (unreachable, not provably gone)" },
    { url: "https://ledger.invalid/c", status: "live" },
  ] } }, evidence: [{ type: "fetch_candidates", output: {
    dead: [{ url: "https://copy.invalid/gone", status: 404, reason: "HTTP 404" }],
  } }] });
  assert.ok(lines.some((line) =>
    line.text === "ledger.invalid · HTTP 403 (blocked/erroring, not gone)"));
  assert.ok(lines.some((line) =>
    line.text === "ledger.invalid · ConnectError (unreachable, not provably gone)"));
  assert.ok(lines.some((line) => line.text === "ledger.invalid"));
  // A reason that only repeats the HTTP status is not shown twice.
  assert.ok(lines.some((line) => line.text === "copy.invalid · HTTP 404"));
  assert.ok(!lines.some((line) => line.text.includes("HTTP 404 · HTTP 404")));
  // An older run keeps the reason only in the fetch trace: one line per page, with the reason.
  const older = evidenceSummary({ name: "similarity", output: { details: { candidates: [
    { url: "https://ledger.invalid/a", status: "unreadable" },
  ] } }, evidence: [{ type: "fetch_candidates", output: { unreadable: [
    { url: "https://ledger.invalid/a", reason: "HTTP 403 (blocked/erroring, not gone)" },
  ] } }] });
  assert.equal(older.map((line) => line.text).join("\n"),
    "ledger.invalid · HTTP 403 (blocked/erroring, not gone)");
  // Older traces stored the response's own content type: never shown, only "not HTML".
  // Unknown reason text is dropped. A live page keeps both its status and its measurement.
  const mixed = evidenceSummary({ name: "similarity", output: { details: { candidates: [
    { url: "https://ledger.invalid/live", status: "live", old_in_new: 0.35 },
  ] } }, evidence: [{ type: "fetch_candidates", output: {
    live: [{ url: "https://ledger.invalid/live", status: 200 }],
    unreadable: [
      { url: "https://ledger.invalid/doc", reason: "not HTML (application/fabricated-private-words)" },
      { url: "https://ledger.invalid/odd", reason: "fabricated private upstream words" },
    ],
  } }] });
  const texts = mixed.map((line) => line.text).join("\n");
  assert.ok(!texts.includes("fabricated"), texts);
  assert.ok(texts.includes("ledger.invalid · not HTML"), texts);
  assert.equal(mixed.filter((line) => line.url === "https://ledger.invalid/live").length, 1);
  const live = mixed.find((line) => line.url === "https://ledger.invalid/live").text;
  assert.ok(live.includes("HTTP 200") && live.includes("containment"), live);
});

test("Articles clamps only fresh offsets and resets its own epoch across routes", () => {
  const harness = ledgerHarness("ArticlesPage");
  visit("https://workbench.invalid/articles?q=fictional&offset=100");
  harness.render({ items: [], total: 51 }, false, false);
  assert.equal(new URL(href).searchParams.get("offset"), "100");
  harness.render({ items: [], total: 51 });
  assert.equal(new URL(href).searchParams.get("offset"), "50");
  assert.equal(new URL(href).searchParams.get("q"), "fictional");
  for (const change of ["epoch", "token"]) {
    harness.unmount();
    visit("https://workbench.invalid/articles?status=failed&offset=50");
    if (change === "epoch") harness.api.epoch++;
    else harness.api.token = "fictional-token";
    // A visit to Runs already consumed that ledger's reset, but not Articles'.
    harness.api.ledgerEpoch.current = {
      token: harness.api.token, epoch: harness.api.epoch,
    };
    harness.render(null);
    assert.equal(new URL(href).searchParams.has("offset"), false);
    assert.equal(new URL(href).searchParams.get("status"), "failed");
  }
  harness.unmount();
});

test("stop controls confirm before mutations and refresh after success", async () => {
  const original = hooks.useRunControl;
  const calls = [];
  hooks.useRunControl = () => ({
    busy: false,
    error: "",
    mutate: async (path, onSuccess) => {
      calls.push(path);
      onSuccess?.(resultFixture({ ...baseRun, status: "cancelled", verdict: null }));
      return true;
    },
  });
  let refreshes = 0;
  let stoppedRun;
  const control = controlHarness(
    "components/StopRunButton.tsx", "StopRunButton",
    {
      run: { id: "run fictional", status: "running" }, variant: "compact",
      onStopped: (run) => { refreshes++; stoppedRun = run; },
    },
  );
  try {
    let tree = control.render();
    const button = findNode(tree, (node) => node.type === "button");
    assert.equal(button.props["aria-label"], "Stop audit run fictional");
    let propagated = true;
    tree.props.onClick({ stopPropagation() { propagated = false; } });
    assert.equal(propagated, false);
    button.props.onClick();
    tree = control.render();
    const dialog = findNode(tree, (node) => typeof node.type === "function");
    const markup = render(dialog.type, dialog.props);
    assert.ok(markup.includes("Stop this audit?"));
    assert.ok(markup.includes("Steps already recorded are kept."));
    assert.equal(calls.length, 0);
    await dialog.props.onConfirm();
    assert.deepEqual(calls, ["/api/runs/run%20fictional/cancel"]);
    assert.equal(refreshes, 1);
    assert.equal(stoppedRun.run.status, "cancelled");
    tree = control.render();
    assert.equal(findNode(tree, (node) => typeof node.type === "function"), null);
  } finally {
    control.unmount();
    hooks.useRunControl = original;
  }
});

test("queue bar reports counts, toggles pause and confirms Stop queued", async () => {
  const original = hooks.useRunControl;
  const calls = [];
  hooks.useRunControl = () => ({
    busy: false, error: "",
    mutate: async (path) => { calls.push(path); return true; },
  });
  let refreshes = 0;
  const queue = {
    data: { paused: false, running: ["run_one"], queued: ["run_two"] },
    error: "", refresh() { refreshes++; },
  };
  const control = controlHarness("components/QueueBar.tsx", "QueueBar", {
    queue, onChanged() { refreshes++; },
  });
  try {
    let tree = control.render();
    assert.ok(renderToStaticMarkup(tree).includes("1 running · 1 queued"));
    const button = (label) => findNode(tree,
      (node) => node.type === "button" && node.props.children === label);
    await button("Pause").props.onClick();
    queue.data.paused = true;
    tree = control.render();
    assert.ok(renderToStaticMarkup(tree).includes("Queue paused"));
    await button("Resume").props.onClick();
    button("Stop queued").props.onClick();
    tree = control.render();
    const dialog = findNode(tree, (node) => typeof node.type === "function");
    assert.ok(render(dialog.type, dialog.props).includes("Running audits continue."));
    assert.deepEqual(calls, ["/api/queue/pause", "/api/queue/resume"]);
    await dialog.props.onConfirm();
    assert.deepEqual(calls, [
      "/api/queue/pause", "/api/queue/resume", "/api/queue/cancel",
    ]);
    assert.equal(refreshes, 6);
  } finally {
    control.unmount();
    hooks.useRunControl = original;
  }
});

test("stop buttons are reusable with only run and hidden for terminal runs", () => {
  const { StopRunButton } = load(path.join(root, "components/StopRunButton.tsx"));
  for (const status of ["queued", "running", "succeeded", "failed", "cancelled"]) {
    const html = render(StopRunButton, { run: { id: "fictional", status } });
    assert.equal(html.includes("Stop audit"), ["queued", "running"].includes(status));
  }
});

test("shell queue state links to the single Runs control bar", () => {
  const { QueueIndicator } = load(path.join(root, "components/QueueIndicator.tsx"));
  const original = hooks.useQueue;
  try {
    for (const [data, label] of [
      [{ paused: false, running: ["one", "two"], queued: ["a", "b", "c"] },
        "2 running · 3 queued"],
      [{ paused: true, running: [], queued: [] }, "Queue paused"],
      [{ paused: false, running: [], queued: [] }, "Idle"],
    ]) {
      hooks.useQueue = () => ({ data });
      const html = render(QueueIndicator);
      assert.ok(html.includes(label));
      assert.ok(html.includes('href="/runs"'));
      assert.ok(!html.includes("<button"));
    }
  } finally {
    hooks.useQueue = original;
  }
});

test("cancelled detail shows neutral stopped steps while exact history stays failed", () => {
  const { RunDetailPage } = load(path.join(root, "pages/RunDetailPage.tsx"));
  const step = {
    name: "classification", status: "failed", input: {},
    output: { details: { error_type: "CancelledError" } },
    model_calls: [], evidence: [], duration_ms: 1,
  };
  runState = {
    data: {
      ...baseRun, status: "cancelled", verdict: null, output: null,
      error: null, input: { removed_article: { content: "Fictional text." } },
      steps: [step], model: {},
      summary: {
        verdict_label: "Stopped by a person",
        headline: "A person stopped this audit before it finished. This is not a result.",
        steps: [{ name: "classification", status: "stopped" }],
      },
    },
    error: "", refresh() {},
  };
  snapshotState = { data: null, error: "", refresh() {} };
  const html = render(RunDetailPage, { id: baseRun.id });
  assert.ok(html.includes('class="chip status-chip">Stopped'));
  assert.ok(!html.includes("verdict-failure"));
  assert.ok(!html.includes('class="chip verdict-'));
  assert.equal((html.match(/>Pending</g) ?? []).length, 5);
  assert.equal(step.status, "failed");
});

test("step input accepts saved shapes beside exact system and user messages", () => {
  const { StepTable } = load(path.join(root, "features/steps/StepTable.tsx"));
  const { ModelCalls } = load(path.join(root, "features/steps/ModelCalls.tsx"));
  const call = {
    request: { model: "fictional-model", messages: [
      { role: "system", content: "Invented system instructions." },
      { role: "user", content: "Invented exact prompt text." },
      { role: "assistant", content: "Invented assistant context." },
    ] },
    parsed_output: { reasoning: "Invented response." },
  };
  for (const input of [
    { article: { ref: "run.input.removed_article", content_chars: 200000 } },
    { article: { content: "Invented historical article." }, prior_stages: [] },
  ]) {
    const step = { name: "relevance", status: "completed", input, output: {},
      duration_ms: 1, model_calls: [call], evidence: [] };
    const html = render(StepTable, {
      run: { status: "succeeded", steps: [step] }, workflow: null,
    });
    assert.ok(html.includes("Step input"));
    assert.ok(html.includes("Exact model prompt"));
    assert.ok(html.includes("Invented system instructions."));
    assert.ok(html.includes("Invented exact prompt text."));
    assert.ok(html.includes("Exact output JSON"));
    const prompt = render(ModelCalls, { step, promptsOnly: true });
    assert.ok(!prompt.includes("Invented assistant context."));
    assert.ok(!prompt.includes("Invented response."));
  }
});


test("poll origin has a readable label and server filter", () => {
  const { RunsPage } = load(path.join(root, "pages/RunsPage.tsx"));
  assert.equal(format.originLabel({ category: "live", origin: "poll" }),
    "Live · Auto-audit");
  visit("https://workbench.invalid/runs?origin=poll");
  const html = render(RunsPage);
  assert.equal(capturedQuery.get("origin"), "poll");
  assert.ok(html.includes('value="poll"'));
  assert.ok(html.includes("Live · Auto-audit"));
});


test("polling controls load, save, reset and run through the guarded API", async () => {
  const harness = resourceHarness();
  harness.render("usePollingSettings");
  assert.equal(harness.requests[0].path, "/api/polling/settings");
  harness.requests[0].resolve(pollSettingsFixture);
  await settle();
  assert.equal(harness.render().data, pollSettingsFixture);
  assert.equal([...harness.timers.values()][0].interval, 5000);
  harness.unmount();

  const control = resourceHarness();
  const saved = [];
  const onSettings = (data) => saved.push(data);
  let state = control.render("usePollingControl", onSettings);
  const saving = state.mutate("save", { interval_minutes: 12 });
  let call = control.requests.at(-1);
  assert.equal(call.path, "/api/polling/settings");
  assert.equal(call.method, "PUT");
  assert.deepEqual(JSON.parse(call.body), { interval_minutes: 12 });
  assert.equal(control.render().busy, true);
  await control.render().mutate("run");
  assert.equal(control.requests.length, 1, "mutations cannot overlap");
  call.resolve(pollSettingsFixture);
  await saving;
  state = control.render();
  assert.deepEqual(saved, [pollSettingsFixture]);
  assert.equal(control.api.statusRefreshes, 1);
  assert.equal(state.busy, false);

  const reset = state.mutate("reset", { names: ["interval_minutes"] });
  call = control.requests.at(-1);
  assert.equal(call.path, "/api/polling/settings/reset");
  assert.equal(call.method, "POST");
  assert.deepEqual(JSON.parse(call.body), { names: ["interval_minutes"] });
  call.resolve(pollSettingsFixture);
  await reset;
  const { ApiError } = load(path.join(root, "api/client.ts"));
  for (const [code, expected] of [
    ["poll_running", "An archive poll is already running."],
    ["test_mode", "Polling is off in test mode."],
    ["archive_disabled", "Enable archive reads to poll."],
  ]) {
    const polling = control.render().mutate("run");
    call = control.requests.at(-1);
    assert.equal(call.path, "/api/polling/run");
    assert.equal(call.method, "POST");
    assert.equal(control.render().action, "run");
    call.reject(new ApiError(409, code));
    await polling;
    assert.equal(control.render().error, expected);
  }
  const stale = control.render().mutate("save", { polling_enabled: false });
  call = control.requests.at(-1);
  control.api.epoch++;
  state = control.render();
  assert.equal(call.signal.aborted, true);
  assert.equal(state.error, "");
  assert.equal(state.busy, false);
  call.resolve(pollSettingsFixture);
  await stale;
  assert.equal(saved.length, 2, "a stale response cannot replace current controls");
  control.unmount();
});

test("Polling form keeps ranges, sources, pause and manual-run reasons visible", async () => {
  const runtime = hookRuntime();
  const api = { token: "", epoch: 0 };
  const actions = [];
  let data = pollSettingsFixture;
  let onSettings;
  const component = evaluateModule(
    path.join(root, "features/polling/PollingControls.tsx"),
    (name) => {
      if (name === "react") return runtime.reactHooks;
      if (name === "../../api/hooks") return {
        useApi: () => api,
        usePollingSettings: () => ({ data, error: "", refresh() {} }),
        usePollingControl: (callback) => {
          onSettings = callback;
          return { busy: false, error: "", mutate: async (...args) => {
            actions.push(JSON.parse(JSON.stringify(args)));
            return true;
          } };
        },
      };
      if (name.startsWith(".")) {
        return load(path.resolve(root, "features/polling", name) + ".tsx");
      }
      return require(name);
    },
  );
  let polling = { ...pollingFixture };
  let mode = "live";
  const render = () => runtime.render(() => component.PollingControls({ polling, mode }));
  let tree = render();
  for (const max of [1440, 30, 50]) {
    const input = findNode(tree, (node) => node.props?.type === "number"
      && node.props.max === max);
    assert.equal(input.props.min, 1);
    assert.equal(input.props.step, 1);
    assert.equal(input.props.required, true);
  }
  const button = (label) => findNode(render(), (node) => node.type === "button"
    && node.props.children === label);
  button("Pause polling").props.onClick();
  assert.deepEqual(actions.at(-1), ["save", { polling_enabled: false }]);
  const toggle = findNode(tree, (node) => node.props?.type === "checkbox");
  toggle.props.onChange({ target: { checked: true } });
  assert.deepEqual(actions.at(-1), ["save", { auto_audit_enabled: true }]);
  const interval = findNode(tree, (node) => node.props?.type === "number"
    && node.props.max === 1440);
  interval.props.onChange({ target: { valueAsNumber: 14 } });
  tree = render();
  await findNode(tree, (node) => node.type === "form")
    .props.onSubmit({ preventDefault() {} });
  assert.deepEqual(actions.at(-1), ["save", { interval_minutes: 14 }]);
  onSettings({ ...data, interval_minutes: { value: 14, default: 5, source: "ui" } });
  tree = render();
  assert.match(renderToStaticMarkup(tree), /Set here/);
  const reset = findNode(tree, (node) => node.props?.["aria-label"]
    === "Reset interval minutes to default");
  assert.equal(reset.props.disabled, false);
  await reset.props.onClick();
  assert.deepEqual(actions.at(-1), ["reset", { names: ["interval_minutes"] }]);
  polling = { ...pollingFixture, enabled: false, paused: true };
  assert.equal(button("Poll now").props.disabled, false);
  button("Poll now").props.onClick();
  assert.deepEqual(actions.at(-1), ["run"]);
  polling.running = true;
  assert.equal(button("Poll now").props.disabled, true);
  assert.match(renderToStaticMarkup(render()), /Polling now/);
  polling = { ...pollingFixture, reason: "archive_disabled" };
  assert.equal(button("Poll now").props.disabled, true);
  assert.match(renderToStaticMarkup(render()), /Enable archive reads/);
  mode = "test";
  assert.match(renderToStaticMarkup(render()), /Off in test mode/);
  assert.equal(button("Poll now").props.disabled, true);
  api.epoch++;
  tree = render();
  assert.match(renderToStaticMarkup(tree), /From .env/);
  assert.equal(findNode(tree, (node) => node.props?.type === "number"
    && node.props.max === 1440).props.value, 5);
  runtime.unmount();
});
