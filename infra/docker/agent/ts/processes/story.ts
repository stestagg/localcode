#!/usr/bin/env bun
/** Review and implement one story in two OpenCode turns in the same clone. */

import { mkdir } from "node:fs/promises";
import { basename, dirname } from "node:path";

import { type ParseError, parse, printParseErrorCode } from "jsonc-parser";

import { LlmError, main } from "../src/llm/errors.ts";
import { StoryProcess, type StoryWork } from "../src/process/index.ts";
import { session } from "../src/session/index.ts";

const REPO = "/work/repo";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new LlmError(`${name} did not reach the story process`);
  return value;
}

function rolePrompt(name: string): string {
  let roles: unknown;
  try {
    roles = JSON.parse(required("LOCALCODE_ROLE_PROMPTS"));
  } catch (error) {
    if (error instanceof LlmError) throw error;
    throw new LlmError(`LOCALCODE_ROLE_PROMPTS is not valid JSON: ${error}`);
  }
  if (typeof roles !== "object" || roles === null || Array.isArray(roles)) {
    throw new LlmError("LOCALCODE_ROLE_PROMPTS must be an object");
  }
  const prompt = (roles as Record<string, unknown>)[name];
  if (typeof prompt !== "string" || !prompt.trim()) {
    throw new LlmError(`story process has no prompt for role '${name}'`);
  }
  return prompt;
}

interface RunOptions {
  cwd?: string;
  capture?: boolean;
}

async function run(argv: string[], options: RunOptions = {}): Promise<string> {
  const capture = options.capture ?? false;
  const child = Bun.spawn(argv, {
    cwd: options.cwd ?? REPO,
    env: process.env,
    stdin: "ignore",
    stdout: capture ? "pipe" : "inherit",
    stderr: "inherit",
  });
  const output = capture
    ? new Response(child.stdout as ReadableStream<Uint8Array>).text()
    : Promise.resolve("");
  const code = await child.exited;
  const text = await output;
  if (code !== 0) {
    throw new LlmError(`${argv[0]} ${argv[1] ?? ""} failed (${code})`.trim());
  }
  return text.trim();
}

async function configureGiteaMcp(): Promise<void> {
  const home = required("HOME");
  const root = process.env.XDG_CONFIG_HOME ?? `${home}/.config`;
  const config = `${root}/opencode/opencode.json`;
  let source: string;
  try {
    source = await Bun.file(config).text();
  } catch {
    throw new LlmError(
      "no OpenCode configuration reached the story process; configure a model first",
    );
  }

  const errors: ParseError[] = [];
  const parsed: unknown = parse(source, errors, {
    allowTrailingComma: true,
    disallowComments: false,
  });
  const first = errors[0];
  if (first) {
    throw new LlmError(
      `OpenCode config is invalid: ${printParseErrorCode(first.error)} at offset ${first.offset}`,
    );
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new LlmError("OpenCode config must be an object");
  }

  const settings = parsed as Record<string, unknown>;
  const existing = settings.mcp;
  const mcp =
    typeof existing === "object" && existing !== null && !Array.isArray(existing)
      ? (existing as Record<string, unknown>)
      : {};
  mcp.gitea = {
    type: "local",
    command: [
      "gitea-mcp",
      "-t",
      "stdio",
      "-H",
      required("LOCALCODE_GITEA_URL"),
      "-O",
      "pull_request_write",
    ],
    enabled: true,
    environment: {
      GITEA_ACCESS_TOKEN: "{env:LOCALCODE_TOKEN}",
    },
  };
  settings.mcp = mcp;
  await Bun.write(config, `${JSON.stringify(settings, null, 2)}\n`);
}

async function opencode(title: string, prompt: string): Promise<void> {
  console.log(`story: starting OpenCode turn '${title}'`);
  await run(["opencode", "run", "--title", title, prompt]);
}

function names(text: string): string[] {
  return text.split("\0").filter(Boolean);
}

async function changedPaths(): Promise<Set<string>> {
  const paths = await Promise.all([
    run(["git", "diff", "--name-only", "-z"], { capture: true }),
    run(["git", "diff", "--cached", "--name-only", "-z"], { capture: true }),
    run(["git", "ls-files", "--others", "--exclude-standard", "-z"], {
      capture: true,
    }),
  ]);
  return new Set(paths.flatMap(names));
}

class Work implements StoryWork {
  readonly story = required("LOCALCODE_STORY_PATH");
  readonly persona = required("LOCALCODE_PERSONA");
  readonly personaPrompt = required("LOCALCODE_PERSONA_PROMPT");
  readonly preDevPrompt = rolePrompt("story-pre-dev");
  readonly developerPrompt = rolePrompt("story-developer");
  readonly owner = required("LOCALCODE_OWNER");
  readonly repository = required("LOCALCODE_REPO");
  readonly base = required("LOCALCODE_BASE");
  readonly branch = required("LOCALCODE_BRANCH");
  readonly gitea = required("LOCALCODE_GITEA_URL").replace(/\/$/, "");
  readonly token = required("LOCALCODE_TOKEN");
  #reviewedStory = "";

  async preDevelopment(): Promise<void> {
    await opencode(
      `pre-development: ${basename(this.story)}`,
      `# Persona: ${this.persona}\n\n${this.personaPrompt}\n\n` +
        `# Role: story-pre-dev\n\n${this.preDevPrompt}\n\n` +
        `You are reviewing ${this.owner}/${this.repository}. The selected story is ` +
        `\`${this.story}\`. Read that file and inspect the current codebase closely. ` +
        `You may edit that story in place when the code shows that it is inaccurate, ` +
        `ambiguous, incomplete, or not verifiable. Do not change any other file. ` +
        `Do not implement, commit, branch, push, or open a pull request. Finish once ` +
        `the story is ready for a developer to implement without another product or ` +
        `technical clarification.`,
    );
  }

  async prepareDevelopment(): Promise<void> {
    const changed = await changedPaths();
    const unexpected = [...changed].filter((path) => path !== this.story);
    if (unexpected.length) {
      throw new LlmError(
        `pre-development changed files other than the selected story: ${unexpected.join(", ")}`,
      );
    }

    try {
      this.#reviewedStory = await Bun.file(`${REPO}/${this.story}`).text();
    } catch {
      throw new LlmError(`pre-development removed the selected story: ${this.story}`);
    }

    // The story crosses into the development turn as an ordinary local file,
    // not a commit. The functional branch still starts exactly at main, so
    // metadata cannot leak into the pull-request diff.
    await run(["git", "restore", "--staged", "--worktree", "--", this.story]);
    await run(["git", "switch", "--quiet", this.base]);
    await run(["git", "switch", "--quiet", "-c", this.branch]);

    const exclude = `${REPO}/.git/info/exclude`;
    const current = await Bun.file(exclude).text();
    await Bun.write(exclude, `${current}${current.endsWith("\n") ? "" : "\n"}.localcode/\n`);
    await mkdir(dirname(`${REPO}/${this.story}`), { recursive: true });
    await Bun.write(`${REPO}/${this.story}`, this.#reviewedStory);

    // The review turn had no remote mutation tools. Add the one Gitea tool the
    // developer needs only now, immediately before its separate OpenCode turn.
    await configureGiteaMcp();
  }

  async implement(): Promise<void> {
    await opencode(
      `development: ${basename(this.story)}`,
      `# Persona: ${this.persona}\n\n${this.personaPrompt}\n\n` +
        `# Role: story-developer\n\n${this.developerPrompt}\n\n` +
        `Implement the reviewed story at \`${this.story}\` in ` +
        `${this.owner}/${this.repository}. Its pre-development edits are visible ` +
        `in this working tree even though .localcode is deliberately excluded from ` +
        `git. The current branch is \`${this.branch}\`, based on \`${this.base}\`.\n\n` +
        `Complete the implementation and tests, run the relevant checks, commit all ` +
        `functional changes, and push the current branch to origin. Do not commit ` +
        `anything under .localcode. Then use the connected Gitea MCP pull-request ` +
        `tool to open a pull request in ${this.owner}/${this.repository} from ` +
        `\`${this.branch}\` into \`${this.base}\`. Give the pull request a useful ` +
        `title and a body that identifies \`${this.story}\`, summarizes the change, ` +
        `and records the tests run. Do not merge it. The task is not complete until ` +
        `the branch is pushed and the Gitea MCP confirms that the pull request is open.`,
    );
  }

  async finish(): Promise<string> {
    const branch = await run(["git", "branch", "--show-current"], { capture: true });
    if (branch !== this.branch) {
      throw new LlmError(`development ended on ${branch || "no branch"}, expected ${this.branch}`);
    }

    const pending = await run(
      ["git", "status", "--porcelain", "--", ".", ":!.localcode"],
      { capture: true },
    );
    if (pending) {
      throw new LlmError("development left uncommitted functional changes");
    }

    const ahead = Number(
      await run(["git", "rev-list", "--count", `${this.base}..HEAD`], {
        capture: true,
      }),
    );
    if (!Number.isInteger(ahead) || ahead < 1) {
      throw new LlmError("development made no commits beyond the base branch");
    }

    const metadata = await run(
      ["git", "diff", "--name-only", `${this.base}...HEAD`, "--", ".localcode"],
      { capture: true },
    );
    if (metadata) {
      throw new LlmError("development included .localcode metadata in the functional commits");
    }

    const head = await run(["git", "rev-parse", "HEAD"], { capture: true });
    const remote = await run(
      ["git", "ls-remote", "--heads", "origin", `refs/heads/${this.branch}`],
      { capture: true },
    );
    if (remote.split(/\s+/)[0] !== head) {
      throw new LlmError(`development did not push ${this.branch} to origin`);
    }

    const response = await fetch(
      `${this.gitea}/api/v1/repos/${encodeURIComponent(this.owner)}/` +
        `${encodeURIComponent(this.repository)}/pulls?state=open&limit=50`,
      { headers: { Authorization: `token ${this.token}` } },
    );
    if (!response.ok) {
      throw new LlmError(`could not verify the pull request (${response.status})`);
    }
    const pulls: unknown = await response.json();
    const pull = Array.isArray(pulls)
      ? pulls.find((item) => {
          if (typeof item !== "object" || item === null) return false;
          const candidate = item as Record<string, any>;
          return candidate.head?.ref === this.branch && candidate.base?.ref === this.base;
        })
      : undefined;
    if (typeof pull !== "object" || pull === null) {
      throw new LlmError(`Gitea has no open pull request for ${this.branch}`);
    }

    const url = (pull as Record<string, unknown>).html_url;
    if (typeof url !== "string" || !url) {
      throw new LlmError("Gitea returned a pull request without a URL");
    }
    return url;
  }
}

await main("story", async () => {
  const story = required("LOCALCODE_STORY_PATH");
  if (!/^[A-Za-z0-9._/-]+$/.test(story)) {
    throw new LlmError("story path contains unsupported characters");
  }
  if (!story.startsWith(".localcode/stories/") || story.includes("..")) {
    throw new LlmError("story path is outside .localcode/stories/");
  }
  await session(async (client) => {
    await new StoryProcess(client, new Work()).run();
  });
});
