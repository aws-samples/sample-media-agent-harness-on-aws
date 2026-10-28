import { describe, expect, it } from "vitest";
import { projectJob, projectOutputs } from "./jobsProjection";

const base = { jobId: "P#assemble", trioId: "node|assemble", status: "SUCCEEDED" };

describe("/jobs projects the outputs a completed plan node made (plan chainer)", () => {
  it("carries well-formed entries and drops the rest", () => {
    const job = projectJob({
      ...base,
      outputs: [
        { uri: "s3://b/projects/p/sessions/s/output/film.mp4", key: "projects/p/sessions/s/output/film.mp4", kind: "video" },
        { uri: "s3://b/x.json", key: "x.json", kind: "data" },
        { key: "artifacts/s/output/a.png", kind: "image" },
        "junk",
        null,
      ],
    });
    expect(job.outputs).toEqual([
      { uri: "s3://b/projects/p/sessions/s/output/film.mp4", key: "projects/p/sessions/s/output/film.mp4", kind: "video" },
    ]);
  });

  it("is absent until recorded, and an empty record stays empty", () => {
    expect("outputs" in projectJob(base)).toBe(false);
    expect(projectJob({ ...base, outputs: [] }).outputs).toEqual([]);
    expect(projectOutputs([])).toEqual([]);
  });
});
