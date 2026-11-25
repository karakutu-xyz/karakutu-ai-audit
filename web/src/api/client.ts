import type { ArticleImported, AuditResult } from "../types";

export class ApiError extends Error {
  /** The server's safe error code (`{"error": {"code": ...}}`), when it sent one. */
  constructor(public status: number, public code: string | null = null) {
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
  if (!response.ok) throw new ApiError(response.status, await errorCode(response));
  return response.json() as Promise<T>;
}

// Only a short lowercase identifier is kept; anything else in an error body is ignored.
async function errorCode(response: Response): Promise<string | null> {
  try {
    const code = (await response.json())?.error?.code;
    return typeof code === "string" && /^[a-z0-9_]{1,64}$/.test(code) ? code : null;
  } catch {
    return null;
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
