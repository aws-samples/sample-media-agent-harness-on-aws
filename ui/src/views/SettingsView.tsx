import React, { useState } from 'react';
import { theme } from '../styles/tokens';
import { useIsGatewayAdmin } from '../auth/isGatewayAdmin';
import CapabilitiesSettings, { type CapTab } from '../components/settings/CapabilitiesSettings';
import {
  SETTINGS_CLOSE_BUTTON_INSET,
  SETTINGS_CLOSE_BUTTON_SIZE,
} from '../components/settings/panelChrome';
import GeneralSettings from '../components/settings/GeneralSettings';
import ModelSettings from '../components/settings/ModelSettings';
import GatewayListView from './GatewayListView';
import UsersPanel from '../components/settings/UsersPanel';
import SafetySettings from '../components/settings/SafetySettings';
import UserEditor from '../components/settings/UserEditor';
import CenterEditor from '../components/CenterEditor';
import { ImproveCenter } from '../components/improve/ImproveCenter';
import type { DraftCategory } from '../lab-api';

// Each Capabilities sub-section (Skills/Tools/Hooks) is a first-class settings
// nav entry, peer to General/Model — plus the admin-only Gateways registry.
//
// Agents is deliberately absent: it duplicated the workspace's own Agents view
// (main nav), which now owns the whole surface including delete, search, and the
// built-in agents browser. See `CustomAgentCenterList` in WorkspaceView.
type SettingsTab = 'general' | 'model' | CapTab | 'improve' | 'gateways' | 'users' | 'safety';

const CAP_TABS: CapTab[] = ['tools', 'hooks'];

interface SettingsViewProps {
  onBack: () => void;
  initialTab?: string;
}

/**
 * Settings rendered as a centered modal overlay on top of the workspace
 * (matches the locked design). `onBack` closes the modal — invoked by the
 * ✕ button, an overlay-backdrop click, or Escape.
 */
export default function SettingsView({ onBack, initialTab }: SettingsViewProps) {
  // Gateway registry tab is admin-only (R3, R7). The runtime is the
  // authoritative gate — every gateway_* mutation re-checks _is_admin
  // server-side — but hiding the tab keeps non-admin chrome clean.
  const isGatewayAdmin = useIsGatewayAdmin();

  const topTabs: { key: SettingsTab; label: string }[] = [
    { key: 'general', label: 'General' },
    { key: 'model', label: 'Model' },
    // Users (admin-only): a top-level SETTINGS entry, not a Capability — it manages
    // who can administer the harness. Only gateway-admins see it (isGatewayAdmin);
    // the proxy independently enforces requireAdmin (403) server-side.
    ...(isGatewayAdmin ? [{ key: 'users' as SettingsTab, label: 'Users' }] : []),
    // Safety (admin-only): the platform's Bedrock guardrail, for everyone. The proxy enforces requireAdmin too.
    ...(isGatewayAdmin ? [{ key: 'safety' as SettingsTab, label: 'Safety' }] : []),
  ];
  const capabilityTabs: { key: SettingsTab; label: string }[] = [
    { key: 'tools', label: 'Tools' },
    { key: 'hooks', label: 'Hooks' },
    // Improve sits before Gateways and OUTSIDE the admin gate, so every user finds it
    // in the same place. It is a review surface, not a CapTab: like Gateways it renders
    // its own component rather than routing through CapabilitiesSettings.
    { key: 'improve', label: 'Improve' },
    ...(isGatewayAdmin ? [{ key: 'gateways' as SettingsTab, label: 'Gateways' }] : []),
  ];

  const [activeTab, setActiveTab] = useState<SettingsTab>(() => {
    const valid: SettingsTab[] = ['general', 'model', 'tools', 'hooks', 'improve', 'gateways', 'users', 'safety'];
    // Legacy entry points. 'capabilities' predates the per-type tabs;
    // 'agents' and 'skills' both moved to the workspace main nav, and this modal
    // cannot navigate there, so they fall back to Tools rather than rendering a
    // tab the nav no longer lists.
    if (initialTab === 'capabilities' || initialTab === 'agents' || initialTab === 'skills') {
      return 'tools';
    }
    return valid.includes(initialTab as SettingsTab) ? (initialTab as SettingsTab) : 'tools';
  });
  const [editorTarget, setEditorTarget] = useState<{ category: DraftCategory; assetId: string | null } | null>(null);
  // Users "add" opens a dedicated page (like the asset editor). usersReloadKey bumps
  // the UsersPanel key after a create so it re-fetches the directory.
  const [creatingUser, setCreatingUser] = useState(false);
  const [usersReloadKey, setUsersReloadKey] = useState(0);

  // Close a nested editor first, then the Settings modal.
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (editorTarget) setEditorTarget(null);
      else onBack();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editorTarget, onBack]);

  const renderNavItem = (t: { key: SettingsTab; label: string }) => (
    <button key={t.key} onClick={() => { setEditorTarget(null); setCreatingUser(false); setActiveTab(t.key); }}
      style={t.key === activeTab ? activeNavItemStyle : navItemStyle}>
      {t.label}
    </button>
  );

  const isCapTab = CAP_TABS.includes(activeTab as CapTab);

  return (
    <div style={overlayStyle} onClick={(e) => { if (e.target === e.currentTarget) onBack(); }}>
      <div style={modalStyle} role="dialog" aria-modal="true" aria-label="Settings">
        <button onClick={onBack} style={closeBtnStyle} aria-label="Close settings" title="Close">✕</button>

        {/* Settings Nav */}
        <div style={navStyle}>
          <div style={navTitleStyle}>Settings</div>
          {topTabs.map(renderNavItem)}
          <div style={navGroupStyle}>Capabilities</div>
          {capabilityTabs.map(renderNavItem)}
        </div>

        {/* Content */}
        <div style={contentStyle}>
          {/* staging's `onOpenEditor` supersedes our earlier `onCloseSettings`
              approach: instead of dismissing the modal to reveal an editor
              mounted behind it, the editor now renders INSIDE the modal. Same
              problem, better fix — so we take staging's here. */}
          {editorTarget ? (
            <CenterEditor
              key={`${editorTarget.category}-${editorTarget.assetId ?? 'new'}`}
              category={editorTarget.category}
              assetId={editorTarget.assetId}
              onClose={() => setEditorTarget(null)}
              backLabel={`Back to ${capabilityTabs.find(item => item.key === activeTab)?.label ?? 'Capabilities'}`}
            />
          ) : (
            <>
              {activeTab === 'general' && <div style={{ maxWidth: 720 }}><GeneralSettings /></div>}
              {activeTab === 'model' && <div style={{ maxWidth: 720 }}><ModelSettings /></div>}
              {activeTab === 'gateways' && <GatewayListView embedded />}
              {activeTab === 'safety' && <div style={{ maxWidth: 720 }}><SafetySettings /></div>}
              {activeTab === 'users' && (
                creatingUser
                  ? <UserEditor
                      onClose={() => setCreatingUser(false)}
                      onCreated={() => { setCreatingUser(false); setUsersReloadKey((k) => k + 1); }}
                    />
                  : <UsersPanel key={usersReloadKey} onAddUser={() => setCreatingUser(true)} />
              )}
              {/* `position: relative` is load-bearing: ImproveCenter's root is absolutely
                  positioned, so without a positioned ancestor here it anchors to the modal
                  itself and covers the settings nav. `height: 100%` gives its internal
                  `overflowY: auto` a definite height to resolve against. */}
              {activeTab === 'improve' && (
                <div style={{ position: 'relative', height: '100%', minHeight: 0 }}>
                  <ImproveCenter />
                </div>
              )}
              {isCapTab && (
                <CapabilitiesSettings
                  tab={activeTab as CapTab}
                  hideTabBar
                  onOpenEditor={setEditorTarget}
                  onOpenGatewaysTab={isGatewayAdmin ? () => setActiveTab('gateways') : undefined}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed', inset: 0, background: theme.colors.overlay,
  display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: theme.zIndex.modal,
  fontFamily: theme.typography.fontFamily,
};

const modalStyle: React.CSSProperties = {
  position: 'relative', width: 1120, height: 760, maxWidth: '96vw', maxHeight: '94vh',
  background: theme.colors.surface, border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel, boxShadow: theme.elevation.elevated,
  display: 'flex', overflow: 'hidden',
};

// Absolutely positioned, so it floats OVER the content pane instead of taking
// space in it. A panel with its own top-right action therefore has to reserve
// room — see `SETTINGS_CLOSE_BUTTON_GUTTER` in `panelChrome`, which is derived
// from the two metrics below so the reservation cannot drift from the button.
const closeBtnStyle: React.CSSProperties = {
  position: 'absolute',
  top: SETTINGS_CLOSE_BUTTON_INSET,
  right: SETTINGS_CLOSE_BUTTON_INSET,
  width: SETTINGS_CLOSE_BUTTON_SIZE,
  height: SETTINGS_CLOSE_BUTTON_SIZE,
  borderRadius: theme.radius.button, border: 'none', background: 'none',
  color: theme.colors.textSecondary, fontSize: 'var(--fs-lg)', cursor: 'pointer',
  display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: theme.zIndex.base,
};

const navStyle: React.CSSProperties = {
  width: 'clamp(104px, 22vw, 200px)', background: theme.colors.sidebarBg, borderRight: `1px solid ${theme.colors.border}`,
  padding: '16px 10px', flexShrink: 0, overflowY: 'auto',
};

const navTitleStyle: React.CSSProperties = {
  padding: '4px 10px 10px', fontSize: 'var(--fs-md)', fontWeight: 600,
  color: theme.colors.textPrimary,
};

const navGroupStyle: React.CSSProperties = {
  padding: '14px 10px 4px', fontSize: 'var(--fs-3xs)', textTransform: 'uppercase',
  letterSpacing: '0.1em', color: theme.colors.textSecondary, fontWeight: 700,
};

const navItemStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 9, width: '100%',
  padding: '8px 11px', fontSize: 'var(--fs-sm)',
  color: theme.colors.textSecondary, background: 'none', border: 'none',
  borderRadius: theme.radius.button, textAlign: 'left', cursor: 'pointer', marginBottom: 1,
};

const activeNavItemStyle: React.CSSProperties = {
  ...navItemStyle,
  color: theme.colors.primary, background: theme.colors.activeBg,
  fontWeight: 500,
};

const contentStyle: React.CSSProperties = { flex: 1, minWidth: 0, padding: 'clamp(12px, 3vw, 24px) clamp(10px, 3vw, 26px)', overflowY: 'auto', overflowX: 'hidden' };
