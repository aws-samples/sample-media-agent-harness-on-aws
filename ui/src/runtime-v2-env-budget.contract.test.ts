/**
 * AgentCore Runtime V2 caps a runtime's environment-variable payload at 1536 bytes.
 *
 * The first V2 fan-out (2026-10-01) failed on the task runtime for exactly this reason and left
 * the staging stack in UPDATE_ROLLBACK_FAILED: the update prepares the env "for a V2 agent" and
 * the service refuses a payload over the cap ("The environment variable payload is N bytes,
 * exceeding the 1536-byte maximum supported for V2 agents"). Bulk config now lives in
 * RuntimeConfigSecret and is hydrated at startup (agent/src/runtime_config_bootstrap.py).
 *
 * Byte size can't be computed from source (values are CloudFormation references), so this guards
 * the shape that keeps every env far under the cap: a small number of keys, and the two big
 * runtimes sourcing their config from the secret instead of inline. Main and task now carry 4 keys
 * each: the sub-agent runtime ARNs moved out of env into SSM parameters (they also serialised the
 * runtime updates; see infra/test/runtime-parallel.synth.test.ts). Add new runtime config to
 * `runtimeConfig` (the secret), not here.
 *
 * Read as text because `infra/` has no test runner (same approach as
 * outbox-environment.contract.test.ts).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const code = readFileSync(resolve(here, '../../infra/lib/agent-infra-stack.ts'), 'utf8')
  .replace(/^\s*\/\/.*$/gm, '');

/** The `environmentVariables: {...}` literal inside `new agentcore.Runtime(this, "<id>", {...})`. */
function runtimeEnvBlock(constructId: string): string {
  const start = code.indexOf(`new agentcore.Runtime(this, "${constructId}"`);
  expect(start, `runtime ${constructId} not found`).toBeGreaterThan(-1);
  const open = code.indexOf('environmentVariables:', start);
  let i = code.indexOf('{', open) + 1;
  let depth = 1;
  while (i < code.length && depth > 0) {
    if (code[i] === '{') depth += 1;
    else if (code[i] === '}') depth -= 1;
    i += 1;
  }
  return code.slice(open, i);
}

const keysOf = (block: string) => [...block.matchAll(/^\s*([A-Z][A-Z0-9_]*)\s*:/gm)].map((m) => m[1]);

// Ten ARN-sized values stays under 1536 B with margin; today's largest block has 8.
const MAX_ENV_KEYS = 10;

describe('Runtime V2 env-var budget (1536-byte cap)', () => {
  const runtimes = [
    'AwsMcpRuntime',
    'RenderAgentRuntime',
    'TaskAgentRuntime',
    'PlanningAgentRuntime',
    'AgentRuntime',
  ];

  it.each(runtimes)('%s keeps its env small enough for V2', (id) => {
    const keys = keysOf(runtimeEnvBlock(id));
    expect(keys.length, `${id} env has ${keys.length} keys: ${keys.join(', ')}. ` +
      `Move static config into runtimeConfig (RuntimeConfigSecret) instead.`)
      .toBeLessThanOrEqual(MAX_ENV_KEYS);
  });

  it.each(['TaskAgentRuntime', 'AgentRuntime'])(
    '%s sources its bulk config from RuntimeConfigSecret',
    (id) => {
      const keys = keysOf(runtimeEnvBlock(id));
      expect(keys).toContain('RUNTIME_CONFIG_SECRET_ARN');
      // These used to be inline and are what pushed the env over the cap.
      for (const moved of ['ASYNC_PLAN_CLIENT_SECRET', 'M2M_CLIENT_SECRET', 'ASSET_INDEX_TABLE',
        'HARNESS_PLATFORM_GATEWAY_URL', 'DELEGATION_SIGNING_KEY_ARN']) {
        expect(keys, `${id} must not inline ${moved}`).not.toContain(moved);
      }
    },
  );

  it.each(runtimes)('%s does not inline another runtime\'s ARN', (id) => {
    // An inline `<other>.agentRuntimeArn` is a CloudFormation edge between runtimes, and it is
    // what made render → task → main update one after another. They are SSM parameters now.
    const keys = keysOf(runtimeEnvBlock(id));
    for (const arn of ['TASK_AGENT_RUNTIME_ARN', 'PLANNING_AGENT_RUNTIME_ARN',
      'RENDER_AGENT_RUNTIME_ARN']) {
      expect(keys, `${id} must not inline ${arn}`).not.toContain(arn);
    }
  });

  it('declares PlatformVersion V2 explicitly for every runtime', () => {
    // Undeclared V2 was drift left by the failed fan-out; a fresh env (prod) would get V1.
    expect(code).toMatch(/renderAgentRuntime\.node\.defaultChild[\s\S]{0,120}"PlatformVersion",\s*"V2"/);
    expect(code).toMatch(
      /for \(const rt of \[mcpRuntime, taskAgentRuntime, planningAgentRuntime, agentRuntime\]\)[\s\S]{0,160}"PlatformVersion", "V2"/,
    );
  });
});
