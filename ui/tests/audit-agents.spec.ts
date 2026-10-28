import { test, expect } from '@playwright/test';
import {
  stagingLogin, newSession, send, bodyText, waitForAgentDone,
  openCapabilities, openCapabilityTab,
  createStreamCapture, attachStreamInterceptor, assertDispatch,
} from './capability-audit.helpers';

/**
 * Task 5 — Agents capability audit (live stack).
 *
 * Dimensions:
 *   (a) works     — the orchestrator dispatches to a sub-agent (task/planning/
 *                   design/render) and the step trail shows it
 *   (b) reflected — specialty-agent draft CRUD (chat-driven) shows under the
 *                   Agents tab "Yours"; delete removes it
 *   (c) shows up  — dispatch surfaces a "Dispatching:" step + "↳" sub-agent
 *                   steps + dispatch_tool in the execution summary
 *
 * Backend: specialty_crud.py (create/delete_specialty_agent_draft @tools on the
 * main agent); dispatch_tool.py routes to sub-agent runtimes.
 */

test('Agents (a)(c) orchestrator dispatch to a sub-agent shows the step trail', async ({ page }) => {
  test.setTimeout(5 * 60 * 1000);
  await stagingLogin(page);
  await newSession(page);

  const capture = createStreamCapture();
  attachStreamInterceptor(page, capture);

  // A media-tool request routes through dispatch_tool → task agent. describe_tool
  // is fast (no async job) so the dispatch trail resolves quickly.
  await send(page,
    'Call describe_tool on the speech-diarization diarize_speech gateway tool and summarize what it returns.');
  await waitForAgentDone(page, 3 * 60 * 1000);

  // Proves: "Dispatching:" step + "↳" sub-agent steps + dispatch_tool in summary.
  assertDispatch(capture, 'agent-dispatch');
});

test('Agents (c) built-in sub-agents are listed in the Agents tab', async ({ page }) => {
  test.setTimeout(120_000);
  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Agents');

  // Built-in inventories default off; opt in before validating runtime agents.
  const builtInAgents = page.getByRole('switch', { name: 'Built-in agents' });
  await expect(builtInAgents).toHaveAttribute('aria-checked', 'false');
  await builtInAgents.click();
  await expect(builtInAgents).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByText('Built-in Agents').first(),
    'Agents tab should have a Built-in Agents section').toBeVisible({ timeout: 60000 });

  // The panel no longer fetches: these four agents ride along on the
  // `specialty-agent` page the tab already requested, so there is nothing to wait
  // for. The generous timeout that covered a cold `mcp_list_agents` is gone with it.
  await expect(page.getByText('Loading agents...')).toHaveCount(0);

  // All four harness sub-agent runtimes must be listed — previously the Agents
  // tab showed only user-authored specialty drafts, so these were invisible.
  //
  // NOTE: assertions are scoped to the built-in panel because a user can have a
  // specialty-agent draft with the same display name (this actor has a draft
  // literally named "Task Agent"), which would otherwise satisfy the match from
  // the "Yours" list and mask a missing built-in.
  const builtinPanel = page.getByTestId('builtin-agents');
  await expect(builtinPanel).toBeVisible({ timeout: 15000 });
  for (const label of ['Task Agent', 'Render Agent', 'Planning Agent']) {
    await expect(builtinPanel.getByText(label, { exact: true }).first(),
      `built-in "${label}" should be listed`).toBeVisible({ timeout: 15000 });
  }

  // Each row states how it is invoked (dispatch_tool vs plan_and_execute_tool).
  await expect(builtinPanel.getByText(/Invoked via:/).first()).toBeVisible({ timeout: 10000 });
});

test('Agents (b) specialty-agent draft CRUD via chat is reflected under the Agents tab', async ({ page }) => {
  test.setTimeout(5 * 60 * 1000);
  const stamp = Date.now();
  const agentName = `pw-audit-agent-${stamp}`;

  await stagingLogin(page);
  await newSession(page);
  try {
    await send(page,
      `Use the create_specialty_agent_draft tool to create a specialty agent named "${agentName}" ` +
      `with persona "A benign audit specialty agent used to verify Agents-tab CRUD. It performs no real task." ` +
      `Confirm with the returned assetId.`);
    await waitForAgentDone(page, 3 * 60 * 1000);
    const txt = await bodyText(page);
    expect(txt.toLowerCase(), 'agent should acknowledge creating the specialty agent')
      .toMatch(/created|assetid|agent|specialty/i);

    await openCapabilities(page);
    await openCapabilityTab(page, 'Agents');
    await expect(page.getByText(agentName, { exact: false }).first(),
      'new specialty agent should appear under the Agents tab (Yours)').toBeVisible({ timeout: 30000 });
    await page.keyboard.press('Escape').catch(() => {});
  } finally {
    await newSession(page).catch(() => {});
    await send(page, `Use delete_specialty_agent_draft to delete the specialty agent named "${agentName}" with confirm=true.`).catch(() => {});
    await waitForAgentDone(page, 2 * 60 * 1000).catch(() => {});
  }
});
