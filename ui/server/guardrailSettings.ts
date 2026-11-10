// The platform's Bedrock guardrail, chosen by an admin (Settings -> Safety).
//
// ONE guardrail for the whole platform (docs/superpowers/specs/2026-11-09-bedrock-guardrails-design.md). The deploy
// creates a default and writes `<RUNTIME_ARN_PARAM_PATH>/GUARDRAIL_DEFAULT`; an admin's pick is
// `<path>/GUARDRAIL_SELECTED`, which the deploy never manages, so it survives every deploy. Both hold JSON
// `{id, version, name}`, or SELECTED holds `none` (the admin turned guardrails off). The Main and Task agents read
// them at most once a minute (agent/src/model/guardrail.py), so a change applies within about a minute.
//
// Served under `/admin/guardrails` (admin-only; `requireAdmin` runs first in app.ts):
//   GET  -> { guardrails: [{id, name, description, versions}], selected, default, effective }
//   PUT  {id, version} | {useDefault: true} | {none: true} -> { effective }
// `useDefault` follows the deploy's default (a policy change CI publishes applies on its own); {id, version} pins.
// A pick must be a PUBLISHED version (never DRAFT) of a guardrail in this account: ids, never ARNs.

import type { SsmCall } from "./runtimeArns";

export interface GuardrailRef { id: string; version: string; name: string }
export interface GuardrailOption { id: string; name: string; description: string; versions: string[] }

/** `ListGuardrails` (no id: the guardrails; with `guardrailIdentifier`: that guardrail's versions). */
export type ListGuardrails = (input: { guardrailIdentifier?: string; nextToken?: string }) =>
  Promise<{ guardrails?: { id?: string; name?: string; description?: string; version?: string }[]; nextToken?: string }>;

export const SELECTED = "GUARDRAIL_SELECTED";
export const DEFAULT = "GUARDRAIL_DEFAULT";
export const NONE = "none";
/** GUARDRAIL_SELECTED value meaning "follow GUARDRAIL_DEFAULT" (same as never chosen). */
export const FOLLOW_DEFAULT = "default";

const ID = /^[a-z0-9]{1,64}$/;
const VERSION = /^[0-9]{1,8}$/;

export function parseRef(raw: unknown): GuardrailRef | null {
  let v: any = raw;
  if (typeof raw === "string") {
    try { v = JSON.parse(raw); } catch { return null; }
  }
  if (!v || typeof v !== "object") return null;
  const id = String(v.id ?? ""), version = String(v.version ?? "");
  if (!ID.test(id) || !VERSION.test(version)) return null;
  return { id, version, name: String(v.name ?? "").slice(0, 100) };
}

/** Every guardrail in the account with its published versions (DRAFT dropped), newest version last. */
export async function listOptions(list: ListGuardrails, max = 50): Promise<GuardrailOption[]> {
  const out: GuardrailOption[] = [];
  let token: string | undefined;
  do {
    const page = await list(token ? { nextToken: token } : {});
    for (const g of page.guardrails ?? []) {
      if (!g.id || !ID.test(g.id) || out.length >= max) continue;
      out.push({ id: g.id, name: g.name ?? g.id, description: g.description ?? "", versions: [] });
    }
    token = out.length >= max ? undefined : page.nextToken;
  } while (token);
  await Promise.all(out.map(async (g) => {
    let vt: string | undefined;
    do {
      const page = await list({ guardrailIdentifier: g.id, ...(vt ? { nextToken: vt } : {}) });
      for (const v of page.guardrails ?? []) if (v.version && VERSION.test(v.version)) g.versions.push(v.version);
      vt = page.nextToken;
    } while (vt);
    g.versions.sort((a, b) => Number(a) - Number(b));
  }));
  return out.filter((g) => g.versions.length > 0);
}

export interface GuardrailSettings {
  /** GUARDRAIL_SELECTED: a ref, `none`, or null (never chosen). */
  selected: GuardrailRef | "none" | null;
  default: GuardrailRef | null;
  /** What the agents apply: the admin's pick, else the default. */
  effective: GuardrailRef | null;
}

export async function readSettings(ssm: SsmCall, path: string): Promise<GuardrailSettings> {
  const resp = await ssm("GetParameters", { Names: [`${path}/${SELECTED}`, `${path}/${DEFAULT}`] });
  const values: Record<string, string> = {};
  for (const p of resp?.Parameters ?? []) {
    if (typeof p?.Name === "string" && typeof p.Value === "string") values[p.Name.split("/").pop()!] = p.Value;
  }
  const raw = values[SELECTED];
  const selected = raw === undefined || raw.trim() === FOLLOW_DEFAULT ? null : raw.trim() === NONE ? "none" : parseRef(raw);
  const dflt = parseRef(values[DEFAULT]);
  return { selected, default: dflt, effective: selected === "none" ? null : selected ?? dflt };
}

export type SaveResult =
  | { ok: true; effective: GuardrailRef | null }
  | { ok: false; status: number; code: string; message: string };

/** Validate an admin's pick against the account's published versions and write it. */
export async function saveSelection(
  ssm: SsmCall, list: ListGuardrails, path: string, body: unknown,
): Promise<SaveResult> {
  const b = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  if (b.none === true) {
    await ssm("PutParameter", { Name: `${path}/${SELECTED}`, Value: NONE, Type: "String", Overwrite: true });
    return { ok: true, effective: null };
  }
  if (b.useDefault === true) {
    await ssm("PutParameter", { Name: `${path}/${SELECTED}`, Value: FOLLOW_DEFAULT, Type: "String", Overwrite: true });
    return { ok: true, effective: (await readSettings(ssm, path)).default };
  }
  const ref = parseRef({ id: b.id, version: b.version });
  if (!ref) return { ok: false, status: 400, code: "invalid_guardrail", message: "Send {id, version}, {useDefault: true} or {none: true}" };
  const options = await listOptions(list);
  const match = options.find((g) => g.id === ref.id);
  if (!match || !match.versions.includes(ref.version)) {
    return { ok: false, status: 404, code: "guardrail_not_found",
      message: "No published version of that guardrail in this account" };
  }
  const value: GuardrailRef = { id: ref.id, version: ref.version, name: match.name };
  await ssm("PutParameter", { Name: `${path}/${SELECTED}`, Value: JSON.stringify(value), Type: "String", Overwrite: true });
  return { ok: true, effective: value };
}
