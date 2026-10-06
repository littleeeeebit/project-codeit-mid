import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./api-schema";

export type Schemas = components["schemas"];
export type Doc = Schemas["Document"];
export type RequestView = Schemas["RequestView"];
export type Answer = Schemas["Answer"];
export type Evidence = Schemas["Evidence"];
export type Budget = Schemas["Budget"];
export type Owned = Schemas["Owned"];

/** Every call rides on the session cookie. A 401 after sign-in means the session ended: sign in again. */
const expired: Middleware = {
  onResponse({ request, response }) {
    if (response.status === 401 && !new URL(request.url).pathname.startsWith("/api/auth/")) {
      window.location.replace("/api/auth/login");  // an API route that leads to the hub; Back skips the dead page
    }
    return response;
  },
};

/** Tabs share one session cookie, so another tab's sign-in changes whose session this screen sends. Every call names
 *  the account this screen loaded as; when the server's session is someone else's, it refuses the call and the page
 *  reloads, so no form, result or key status of the old account survives under the new one. */
let account: string | null = null;

export function bindAccount(name: string): void {
  account = name;
}

/** The header naming this screen's account, for the few calls that bypass `api` (the answer stream). */
export function accountHeaders(): Record<string, string> {
  return account === null ? {} : { "X-BidMate-Account": encodeURIComponent(account) };
}

const sameAccount: Middleware = {
  onRequest({ request }) {
    for (const [name, value] of Object.entries(accountHeaders())) request.headers.set(name, value);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 409 && response.headers.get("X-BidMate-Account-Changed") === "1") window.location.reload();
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: "" });
api.use(expired, sameAccount);

/** The service's Korean message for a refused call, or a generic one when the server gave none. */
export function errorText(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return "입력값을 확인하세요.";
  }
  return "서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.";
}

/** Path of a managed original; the server resolves only (doc_id, source_hash) pairs it manages. */
export function originalHref(docId: string, sourceHash: string): string {
  return `/api/originals/${encodeURIComponent(docId)}/${encodeURIComponent(sourceHash)}`;
}
