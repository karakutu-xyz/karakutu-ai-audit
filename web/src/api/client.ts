import type { ArticleImported, AuditResult } from "../types";

export class ApiError extends Error {
  /** The server's safe error code (`{"error": {"code": ...}}`), when it sent one. */
  constructor(
    public status: number,
    public code: string | null = null,
    /** The run a withheld result belongs to (`result_withheld`), when sent. */
    public runId: string | null = null,
  ) {
    super(
      status === 401
        ? "Access requires an API token. Add it in Settings."
        : status === 422
          ? "The request could not be accepted. Check the required fields and try again."
          : status === 404
            ? "This resource is unavailable. Refresh the list and try again."
            : "The service could not complete this request. Please try again.",
    );
  }
}

export async function request<T>(
  path: string,
  token: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new Error(
      "Cannot reach the audit service. Check that the local API is running.",
    );
  }
  if (!response.ok) {
    const { code, runId } = await errorBody(response);
    throw new ApiError(response.status, code, runId);
  }
  return response.json() as Promise<T>;
}

/**
 * The action took effect but its result was withheld by the service's safety checks
 * (409 `result_withheld`): returns the run id to follow through its details, else null.
 */
export function withheldRunId(error: unknown): string | null {
  return error instanceof ApiError && error.code === "result_withheld" ? error.runId : null;
}

// Only a short lowercase code and a run id are kept; anything else in an error body is ignored.
async function errorBody(
  response: Response,
): Promise<{ code: string | null; runId: string | null }> {
  try {
    const error = (await response.json())?.error;
    const code = error?.code;
    const runId = error?.run_id;
    return {
      code: typeof code === "string" && /^[a-z0-9_]{1,64}$/.test(code) ? code : null,
      runId: typeof runId === "string" && /^run_[A-Za-z0-9_]{1,80}$/.test(runId) ? runId : null,
    };
  } catch {
    return { code: null, runId: null };
  }
}

export function importArticles(days: number, token: string, signal: AbortSignal) {
  return request<ArticleImported>(`/api/articles/import?days=${days}`, token, {
    method: "POST", signal,
  });
}

export function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}

export function auditArticle(ref: string, token: string, signal: AbortSignal) {
  return request<AuditResult>(`/api/articles/${encodeURIComponent(ref)}/audit`, token, {
    method: "POST", signal,
  });
}

export function articleAuditError(error: unknown, liveDisabled: boolean) {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return "An audit for this article is already running.";
    }
    if (error.status === 401 || error.status === 403) {
      return "Check the API token in Connection settings before starting an audit.";
    }
    if (error.status === 404 && liveDisabled) {
      return "Live audits are switched off on this server.";
    }
  }
  return errorMessage(error);
}
