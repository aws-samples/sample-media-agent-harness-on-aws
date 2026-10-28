import { afterEach, describe, expect, it, vi } from "vitest";
import { __resetPersonalSamplesForTests, ensurePersonalSamples } from "./personalSamples";

const ok = { ok: true as const, value: { seeded: [], skipped: [] } };

describe("ensurePersonalSamples", () => {
  afterEach(() => {
    __resetPersonalSamplesForTests();
    vi.useRealTimers();
  });

  it("asks the Asset_Service to seed the caller's Personal project with the caller's own token", async () => {
    const invoke = vi.fn().mockResolvedValue(ok);
    await ensurePersonalSamples("sub-1", "pPERSONAL", "tok-1", { invoke });
    expect(invoke).toHaveBeenCalledWith({
      userToken: "tok-1", projectId: "pPERSONAL", command: "seed_personal_samples", args: {},
    });
  });

  it("seeds once per actor per process after a success", async () => {
    const invoke = vi.fn().mockResolvedValue(ok);
    await ensurePersonalSamples("sub-1", "pA", "t", { invoke });
    await ensurePersonalSamples("sub-1", "pA", "t", { invoke });
    expect(invoke).toHaveBeenCalledTimes(1);
    await ensurePersonalSamples("sub-2", "pB", "t", { invoke });
    expect(invoke).toHaveBeenCalledTimes(2);
  });

  it("retries on the next list after a refusal or an error, and never throws", async () => {
    const invoke = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 503, error: { code: "unavailable", message: "x" } })
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce(ok);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await ensurePersonalSamples("sub-1", "pA", "t", { invoke });
    await ensurePersonalSamples("sub-1", "pA", "t", { invoke });
    await ensurePersonalSamples("sub-1", "pA", "t", { invoke });
    await ensurePersonalSamples("sub-1", "pA", "t", { invoke });
    expect(invoke).toHaveBeenCalledTimes(3);
    warn.mockRestore();
  });

  it("skips without a token (dev bypass): the Asset_Service could not verify the caller", async () => {
    const invoke = vi.fn().mockResolvedValue(ok);
    await ensurePersonalSamples("sub-1", "pA", undefined, { invoke });
    expect(invoke).not.toHaveBeenCalled();
  });

  it("answers after the wait bound even if the seed is still running", async () => {
    vi.useFakeTimers();
    const invoke = vi.fn().mockReturnValue(new Promise(() => {}));
    const done = vi.fn();
    void ensurePersonalSamples("sub-1", "pA", "t", { invoke, waitMs: 50 }).then(done);
    await vi.advanceTimersByTimeAsync(49);
    expect(done).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(done).toHaveBeenCalled();
  });
});
