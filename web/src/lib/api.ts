import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./api-schema";
import { readMember } from "./member";

export type Schemas = components["schemas"];
export type Doc = Schemas["Document"];
export type RequestView = Schemas["RequestView"];
export type Answer = Schemas["Answer"];
export type Evidence = Schemas["Evidence"];
export type Budget = Schemas["Budget"];
export type Owned = Schemas["Owned"];

/** There is no login: the typed name rides along on every call, for attribution only. */
const member: Middleware = {
  onRequest({ request }) {
    if (!request.headers.has("X-Member")) {
      request.headers.set("X-Member", encodeURIComponent(readMember()));
    }
    return request;
  },
};

export const api = createClient<paths>({ baseUrl: "" });
api.use(member);

/** Ownership actions keep the member captured when the request was submitted. */
export function memberHeaders(name: string): Record<string, string> {
  return { "X-Member": encodeURIComponent(name) };
}

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
