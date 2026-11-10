// SafetySettings — the platform's Bedrock guardrail (admin-only; docs/superpowers/specs/2026-11-09-bedrock-guardrails-design.md).
//
// ONE guardrail for the whole platform: every agent reply, sub-agent, plan node and external custom-agent call runs with
// it. Guardrails are made in Amazon Bedrock; this only chooses which published version applies. A change reaches the
// agents within about a minute.
import React, { useEffect, useState } from 'react';
import { theme } from '../../styles/tokens';
import { getGuardrails, setGuardrail } from '../../services/adminApi';
import type { GuardrailSettingsView } from '../../services/adminApi';

const NONE = '__none__';
const DEFAULT = '__default__';

const keyOf = (id: string, version: string) => `${id}:${version}`;

export default function SafetySettings() {
  const [view, setView] = useState<GuardrailSettingsView | null>(null);
  const [choice, setChoice] = useState<string>(DEFAULT);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getGuardrails()
      .then((v) => {
        if (cancelled) return;
        setView(v);
        // A pin of the default's own version shows as the default entry (the same guardrail today).
        const pinsDefault = v.selected && v.selected !== 'none' && v.default
          && v.selected.id === v.default.id && v.selected.version === v.default.version;
        setChoice(v.selected === 'none' ? NONE : v.selected && !pinsDefault ? keyOf(v.selected.id, v.selected.version) : DEFAULT);
      })
      .catch(() => { if (!cancelled) setError("Couldn't load guardrails."); });
    return () => { cancelled = true; };
  }, []);

  const save = async () => {
    if (!view) return;
    setSaving(true); setError(null); setSaved(false);
    try {
      let pick: { id: string; version: string } | 'default' | null;
      if (choice === NONE) pick = null;
      else if (choice === DEFAULT) pick = 'default';
      else { const [id, version] = choice.split(':'); pick = { id, version }; }
      const effective = await setGuardrail(pick);
      setView({ ...view, effective, selected: pick === 'default' ? null : effective ?? 'none' });
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : "Couldn't save.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <h2 style={titleStyle}>Safety</h2>
      <p style={leadStyle}>
        The content guardrail every agent reply runs with, for everyone on this platform. Guardrails are created in
        Amazon Bedrock; choose which one applies. It covers the agents' text, not prompts sent to media-generation tools.
      </p>
      {!view && !error && <div style={metaStyle}>Loading…</div>}
      {view && (
        <>
          <div style={metaStyle} data-testid="guardrail-effective">
            In use: <strong>{view.effective ? `${view.effective.name || view.effective.id} (v${view.effective.version})` : 'None'}</strong>
          </div>
          <label style={labelStyle} htmlFor="guardrail-select">Content guardrail</label>
          <select id="guardrail-select" aria-label="Content guardrail" value={choice}
            onChange={(e) => { setChoice(e.target.value); setSaved(false); }} style={selectStyle}>
            {/* One entry per published version. The default's entry FOLLOWS the default (a policy change CI publishes
                applies on its own); any other entry pins that version. */}
            {view.default && !view.guardrails.some((g) => g.id === view.default!.id && g.versions.includes(view.default!.version)) && (
              <option value={DEFAULT}>{view.default.name || view.default.id} (v{view.default.version}) · platform default</option>
            )}
            {view.guardrails.flatMap((g) => [...g.versions].reverse().map((v) => {
              const isDefault = !!view.default && view.default.id === g.id && view.default.version === v;
              return (
                <option key={keyOf(g.id, v)} value={isDefault ? DEFAULT : keyOf(g.id, v)}>
                  {g.name} (v{v}){isDefault ? ' · platform default' : ''}
                </option>
              );
            }))}
            <option value={NONE}>None (no guardrail)</option>
          </select>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
            <button type="button" onClick={() => void save()} disabled={saving} style={saveStyle}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            {saved && <span style={metaStyle} role="status">Saved. Applies to new replies within a minute.</span>}
          </div>
        </>
      )}
      {error && <div role="alert" style={errorStyle}>{error}</div>}
    </div>
  );
}

const titleStyle: React.CSSProperties = { margin: '0 0 4px', fontSize: 'var(--fs-lg)', fontWeight: 600, color: theme.colors.textPrimary };
const leadStyle: React.CSSProperties = { margin: '0 0 16px', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const metaStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, marginBottom: 12 };
const labelStyle: React.CSSProperties = { display: 'block', fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary, marginBottom: 6 };
const selectStyle: React.CSSProperties = {
  width: '100%', maxWidth: 480, height: 34, padding: '0 10px', borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.borderStrong}`, background: theme.colors.surface, color: theme.colors.textPrimary,
  font: 'inherit', fontSize: 'var(--fs-sm)',
};
const saveStyle: React.CSSProperties = {
  height: 32, padding: '0 16px', border: 'none', borderRadius: theme.radius.button, background: theme.colors.primary,
  color: theme.colors.onPrimary, font: 'inherit', fontSize: 'var(--fs-sm)', fontWeight: 600, cursor: 'pointer',
};
const errorStyle: React.CSSProperties = { marginTop: 12, fontSize: 'var(--fs-xs)', color: theme.colors.error };
