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

export const api = createClient<paths>({ baseUrl: "" });
api.use(expired);

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
