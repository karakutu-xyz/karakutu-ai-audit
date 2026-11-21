import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { errorMessage, request } from "./client";
import type { ArticleList, RunSummary, ServiceStatus } from "../types";

export type RunsResponse = { items: RunSummary[]; total: number };

export const ApiContext = createContext<{
  token: string;
  setToken: (value: string) => void;
  status: ServiceStatus | null;
  serviceError: string;
  epoch: number;
  runsCache: Map<string, RunsResponse>;
  articlesCache: Map<string, ArticleList>;
  articlesLedgerEpoch: { current: { token: string; epoch: number } };
  ledgerEpoch: { current: { token: string; epoch: number } };
  retryStatus: () => void;
} | null>(null);

export function ApiProvider({ children }: { children: ReactNode }) {
  const [token, updateToken] = useState("");
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [serviceError, setServiceError] = useState("");
  const [epoch, setEpoch] = useState(0);
  const runsCache = useMemo(() => new Map<string, RunsResponse>(), [token, epoch]);
  const articlesCache = useMemo(
    () => new Map<string, ArticleList>(), [token, epoch],
  );
  const articlesLedgerEpoch = useRef({ token, epoch });
  // Remember the last ledger epoch even when a different route is mounted.
  const ledgerEpoch = useRef({ token, epoch });
  const [retry, setRetry] = useState(0);
  // Keep the last observed mode through an outage to detect a service swap on recovery.
  const mode = useRef<ServiceStatus["mode"] | null>(null);
  const setToken = useCallback((value: string) => {
    updateToken(value);
    setStatus(null);
    setEpoch((n) => n + 1);
  }, []);
  const retryStatus = useCallback(() => setRetry((n) => n + 1), []);

  useEffect(() => {
    let controller: AbortController | null = null;
    async function load() {
      controller?.abort();
      const current = new AbortController();
      controller = current;
      try {
        const service = await request<ServiceStatus>("/api/status", token, {
          signal: current.signal,
        });
        if (current.signal.aborted) return;
        if (mode.current && mode.current !== service.mode) {
          setEpoch((n) => n + 1);
        }
        mode.current = service.mode;
        setStatus(service);
        setServiceError("");
      } catch (error) {
        if (!current.signal.aborted) {
          setStatus(null);
          setServiceError(errorMessage(error));
        }
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      window.clearInterval(timer);
      controller?.abort();
    };
  }, [token, retry]);

  return (
    <ApiContext
      value={{
        token,
        setToken,
        status,
        serviceError,
        epoch,
        runsCache,
        articlesCache,
        articlesLedgerEpoch,
        ledgerEpoch,
        retryStatus,
      }}
    >
      {children}
    </ApiContext>
  );
}
