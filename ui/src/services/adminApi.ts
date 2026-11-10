// Admin API client (C2 services layer). Data access for the gateway-admins
// surface, separated from the UsersPanel component so the component owns
// presentation/state and this module owns transport + error mapping. All calls
// go through authFetch (JWT attached; /admin is in PROTECTED_PREFIXES).
import { authFetch } from '../auth/authFetch';

export interface AdminUser {
  username: string;
  sub?: string;
  email?: string;
  status?: string;
  enabled?: boolean;
  admin?: boolean;
}

/** Thrown on a non-2xx admin response; `status` lets callers special-case 403. */
export class AdminApiError extends Error {
  constructor(public readonly status: number, message?: string) {
    super(message ?? `admin request failed (${status})`);
    this.name = 'AdminApiError';
  }
}

/** GET /admin/users — read-only Cognito directory with per-user admin flag. */
export async function listUsers(): Promise<AdminUser[]> {
  const res = await authFetch('/admin/users', { headers: { accept: 'application/json' } });
  if (!res.ok) throw new AdminApiError(res.status);
  const body = await res.json();
  return Array.isArray(body?.users) ? (body.users as AdminUser[]) : [];
}

/** POST /admin/users — create a user by email (Cognito emails an invite). Only
 *  email is required; first/last name are optional (given_name/family_name). */
export async function addUser(input: { email: string; firstName?: string; lastName?: string }): Promise<void> {
  const res = await authFetch('/admin/users', {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new AdminApiError(res.status);
}

/** POST /admin/users/:u/disable — soft delete (blocks sign-in; reversible). */
export async function disableUser(username: string): Promise<void> {
  const res = await authFetch(`/admin/users/${encodeURIComponent(username)}/disable`, {
    method: 'POST', headers: { accept: 'application/json' },
  });
  if (!res.ok) throw new AdminApiError(res.status);
}

/** POST /admin/users/:u/enable — undo a soft delete. */
export async function enableUser(username: string): Promise<void> {
  const res = await authFetch(`/admin/users/${encodeURIComponent(username)}/enable`, {
    method: 'POST', headers: { accept: 'application/json' },
  });
  if (!res.ok) throw new AdminApiError(res.status);
}

/** POST|DELETE /admin/users/:username/admin — grant/revoke the gateway-admins role. */
export async function setUserAdmin(username: string, admin: boolean): Promise<void> {
  const res = await authFetch(`/admin/users/${encodeURIComponent(username)}/admin`, {
    method: admin ? 'POST' : 'DELETE',
    headers: { accept: 'application/json' },
  });
  if (!res.ok) throw new AdminApiError(res.status);
}

/** POST /admin/users/:username/reset-password — trigger a Cognito password reset. */
export async function resetUserPassword(username: string): Promise<void> {
  const res = await authFetch(`/admin/users/${encodeURIComponent(username)}/reset-password`, {
    method: 'POST',
    headers: { accept: 'application/json' },
  });
  if (!res.ok) throw new AdminApiError(res.status);
}

/** DELETE /admin/users/:username — permanently delete a Cognito user (destructive). */
export async function deleteUser(username: string): Promise<void> {
  const res = await authFetch(`/admin/users/${encodeURIComponent(username)}`, {
    method: 'DELETE',
    headers: { accept: 'application/json' },
  });
  if (!res.ok) throw new AdminApiError(res.status);
}

// ---- The platform's Bedrock guardrail (Settings -> Safety; ui/server/guardrailSettings.ts) ----

export interface GuardrailRef { id: string; version: string; name: string }
export interface GuardrailOption { id: string; name: string; description: string; versions: string[] }
export interface GuardrailSettingsView {
  guardrails: GuardrailOption[];
  /** What every agent turn runs with: the admin's pick, or null (none, the default). */
  effective: GuardrailRef | null;
}

/** GET /admin/guardrails */
export async function getGuardrails(): Promise<GuardrailSettingsView> {
  const res = await authFetch('/admin/guardrails', { headers: { accept: 'application/json' } });
  if (!res.ok) throw new AdminApiError(res.status);
  const b = await res.json();
  return {
    guardrails: Array.isArray(b?.guardrails) ? b.guardrails : [],
    effective: b?.effective ?? null,
  };
}

/** PUT /admin/guardrails — a guardrail version, or `null` (none). */
export async function setGuardrail(pick: { id: string; version: string } | null): Promise<GuardrailRef | null> {
  const res = await authFetch('/admin/guardrails', {
    method: 'PUT',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(pick ?? { none: true }),
  });
  if (!res.ok) {
    let message: string | undefined;
    try { message = (await res.json())?.error?.message; } catch { /* not JSON */ }
    throw new AdminApiError(res.status, message);
  }
  return (await res.json())?.effective ?? null;
}
