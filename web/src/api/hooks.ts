import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { ApiContext, type RunsResponse } from "./context";
import { request, errorMessage, ApiError, withheldRunId, pollingError } from "./client";
import { pending } from "../format";
import type {
  ArticleDetail, ArticleList, PollSettings, PollValues, QueueState, Run, Workflow,
} from "../types";

const ledgerCacheLimit = 20;
const queueListeners = new Set<() => void>();

export function useApi() {
  const context = useContext(ApiContext);
  if (!context) throw new Error("API provider is missing");
  return context;
}

export function useServiceStatus() {
  const { status, serviceError, retryStatus } = useApi();
  return { status, error: serviceError, retry: retryStatus };
}

function useResource<T>(
  path: string | null,
  interval: number | ((data: T | null) => number) = 0,
  shouldPoll?: (data: T) => boolean,
  cache?: Map<string, T>,
) {
  const { token, epoch } = useApi();
  const cached = path ? cache?.get(path) ?? null : null;
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<{
    path: string | null;
    token: string;
    epoch: number;
    data: T | null;
    loading: boolean;
    refreshing: boolean;
    error: string;
    responseRevision: number | null;
  }>({
    path,
    token,
    epoch,
    data: cached,
    loading: !!path && cached === null,
    refreshing: cached !== null,
    error: "",
    responseRevision: null,
  });
  const refresh = useCallback(() => setRevision((n) => n + 1), []);
  const setData = useCallback(
    (data: T) =>
      setState((s) =>
        s.path === path && s.token === token && s.epoch === epoch
          ? { ...s, data }
          : s,
      ),
    [path, token, epoch],
  );

  useEffect(() => {
    if (!path) {
      setState({
        path,
        token,
        epoch,
        data: null,
        loading: false,
        refreshing: false,
        error: "",
        responseRevision: null,
      });
      return;
    }
    const controller = new AbortController();
    let inFlight = false;
    let finished = false;
    let timer: number | undefined;
    let pollInterval = 0;
    function schedule(data: T | null) {
      const next = typeof interval === "function" ? interval(data) : interval;
      if (next === pollInterval) return;
      window.clearInterval(timer);
      pollInterval = next;
      if (next) {
        timer = window.setInterval(() => {
          if (!document.hidden) void load();
        }, next);
      }
    }
    async function load() {
      if (inFlight || finished) return;
      inFlight = true;
      const cachedData = cache?.get(path!);
      if (cache && cachedData !== undefined) {
        // Map insertion order tracks committed query access, including cache hits.
        cache.delete(path!);
        cache.set(path!, cachedData);
      }
      setState((previous) => {
        const sameResource =
          previous.path === path &&
          previous.token === token &&
          previous.epoch === epoch;
        const data = sameResource ? previous.data : cachedData ?? null;
        return {
          path,
          token,
          epoch,
          data,
          loading: data === null,
          refreshing: data !== null,
          error: "",
          responseRevision: null,
        };
      });
      try {
        const data = await request<T>(path!, token, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (cache) {
          cache.delete(path!);
          cache.set(path!, data);
          while (cache.size > ledgerCacheLimit) {
            cache.delete(cache.keys().next().value!);
          }
        }
        setState({
          path,
          token,
          epoch,
          data,
          loading: false,
          refreshing: false,
          error: "",
          responseRevision: revision,
        });
        schedule(data);
        if (shouldPoll && !shouldPoll(data)) {
          finished = true;
          window.clearInterval(timer);
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setState((s) => ({
            ...s,
            loading: false,
            refreshing: false,
            error: errorMessage(error),
          }));
        }
      } finally {
        inFlight = false;
      }
    }
    void load();
    schedule(
      state.path === path && state.token === token && state.epoch === epoch
        ? state.data
        : null,
    );
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [path, token, epoch, revision, interval, shouldPoll, cache]);

  const current =
    state.path === path && state.token === token && state.epoch === epoch
      ? state
      : {
          data: cached,
          loading: !!path && cached === null,
          refreshing: cached !== null,
          error: "",
          responseRevision: null,
        };
  return {
    ...current,
    fresh: current.responseRevision === revision,
    refresh,
    setData,
  };
}

export function useRuns(query: string) {
  const { runsCache } = useApi();
  return useResource<RunsResponse>(
    `/api/runs?${query}`,
    4000,
    undefined,
    runsCache,
  );
}

const pollRun = (run: Run) => pending(run.status);

export function useRun(id: string | null) {
  return useResource<Run>(
    id ? `/api/runs/${encodeURIComponent(id)}/details` : null, 1500, pollRun,
  );
}

export function useWorkflow(id?: string | null) {
  return useResource<Workflow>(
    id === null
      ? null
      : id
        ? `/api/workflows/${encodeURIComponent(id)}`
        : "/api/workflow",
  );
}

export function useArticles(query: string) {
  const { articlesCache } = useApi();
  return useResource<ArticleList>(
    `/api/articles?${query}`, 4000, undefined, articlesCache,
  );
}

const pollArticle = (article: ArticleDetail) =>
  pending(article.latest_run_status ?? "");

export function useArticle(ref: string) {
  return useResource<ArticleDetail>(
    `/api/articles/${encodeURIComponent(ref)}`, 1500, pollArticle,
  );
}

export function useArticleInventory(enabled: boolean) {
  return useResource<ArticleList>(enabled ? "/api/articles?limit=1" : null, 4000);
}

const queueInterval = (queue: QueueState | null) =>
  queue && (queue.paused || queue.running.length || queue.queued.length)
    ? 1500
    : 30000;

export function useQueue() {
  const queue = useResource<QueueState>("/api/queue", queueInterval);
  useEffect(() => {
    queueListeners.add(queue.refresh);
    return () => { queueListeners.delete(queue.refresh); };
  }, [queue.refresh]);
  return queue;
}

// Abandon mutations and their feedback when identity or service changes.
export function useRunControl() {
  const { token, epoch } = useApi();
  const controller = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setBusy(false);
    setError("");
    return () => controller.current?.abort();
  }, [token, epoch]);

  async function mutate<T = unknown>(
    path: string,
    // `null` when the action took effect but the service withheld its result.
    onSuccess?: (data: T | null) => void,
  ): Promise<boolean> {
    if (controller.current && !controller.current.signal.aborted) return false;
    const active = new AbortController();
    controller.current = active;
    setBusy(true);
    setError("");
    try {
      const data = await request<T>(path, token, {
        method: "POST", signal: active.signal,
      });
      if (active.signal.aborted) return false;
      for (const refresh of queueListeners) refresh();
      onSuccess?.(data);
      return true;
    } catch (error) {
      if (!active.signal.aborted && withheldRunId(error)) {
        for (const refresh of queueListeners) refresh();
        onSuccess?.(null);
        return true;
      }
      if (!active.signal.aborted) {
        setError(
          error instanceof ApiError && error.status === 409
            ? "This audit has already finished."
            : errorMessage(error),
        );
      }
      return false;
    } finally {
      if (!active.signal.aborted) setBusy(false);
      if (controller.current === active) controller.current = null;
    }
  }
  return { mutate, busy, error };
}

export function usePollingSettings() {
  return useResource<PollSettings>("/api/polling/settings", 5000);
}

export function usePollingControl(onSettings: (settings: PollSettings) => void) {
  const { token, epoch, retryStatus } = useApi();
  const controller = useRef<AbortController | null>(null);
  const [action, setAction] = useState<"save" | "reset" | "run" | null>(null);
  const busy = action !== null;
  const [error, setError] = useState("");
  useEffect(() => {
    setAction(null);
    setError("");
    return () => controller.current?.abort();
  }, [token, epoch]);

  async function mutate(
    action: "save" | "reset" | "run",
    values?: Partial<PollValues> | { names: string[] },
  ) {
    if (controller.current && !controller.current.signal.aborted) return false;
    const active = new AbortController();
    controller.current = active;
    setAction(action);
    setError("");
    const path = action === "save" ? "settings"
      : action === "reset" ? "settings/reset" : "run";
    try {
      const result = await request<PollSettings>(`/api/polling/${path}`, token, {
        method: action === "save" ? "PUT" : "POST",
        ...(values ? { body: JSON.stringify(values) } : {}),
        signal: active.signal,
      });
      if (active.signal.aborted) return false;
      if (action !== "run") onSettings(result);
      retryStatus();
      return true;
    } catch (error) {
      if (!active.signal.aborted) setError(pollingError(error));
      return false;
    } finally {
      if (!active.signal.aborted) setAction(null);
      if (controller.current === active) controller.current = null;
    }
  }
  return { mutate, busy, error, action };
}
