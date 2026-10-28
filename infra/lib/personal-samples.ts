/**
 * Personal samples: files every user finds, pinned first, in their Personal project (the demo tour's video).
 *
 * The bytes live in the repo under `infra/assets/samples/` and the deploy uploads them ONCE to the Ingest bucket
 * under `samples/` (a `BucketDeployment` in `AgentInfraStack`). The Asset_Service copies them into each user's
 * Personal project when that user's project list loads (`agent/src/ingestion/personal_samples.py`), and its env
 * `PERSONAL_SAMPLES` is this list. Its IAM is scoped to exactly these keys.
 *
 * Bump `version` to hand a NEW copy to every user (a new asset id); users keep the old one until they delete it.
 */
import * as path from "path";

export interface PersonalSample {
  /** The shared object in the Ingest bucket. Must be under `samples/`. */
  readonly key: string;
  /** The file name each user sees. Also the last path segment of their copy. */
  readonly fileName: string;
  readonly contentType: string;
  readonly version: string;
}

/** The local directory the deploy uploads to `samples/` (with its NOTICE). */
export const PERSONAL_SAMPLES_DIR = path.join(__dirname, "../assets/samples");

export const PERSONAL_SAMPLES: readonly PersonalSample[] = [
  { key: "samples/0-demo.mp4", fileName: "0-demo.mp4", contentType: "video/mp4", version: "1" },
];
