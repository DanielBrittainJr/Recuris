/** Recuris' phase capability contract enforced inside one DSH process.
 *
 * The ordinary DSH workspace sandbox is the outer boundary.  This plugin adds
 * the narrower Recuris boundary: only the tools named for this phase are
 * visible, and reads/searches/mutations are limited to permissions generated
 * by Driver._cc_settings().  The driver still audits the resulting trace and
 * filesystem inventory, so this is enforcement in addition to verification.
 */

import fs from "node:fs";
import path from "node:path";

export const name = "recuris-dsh-scope-guard";
export const inject = ["tools", "systemPrompt", "subagents"];

const SEARCHABLE_CONTEXT_VERSION = "prime-search-v1";
const CONTEXT_WORKSPACE_VERSION = "recuris-prime-v1";

const TOOL_MAP = Object.freeze({
  Read: "read",
  Glob: "glob",
  Grep: "grep",
  Edit: "edit",
  Write: "write",
  Context: "context",
});

function requiredEnv(key) {
  const value = String(process.env[key] ?? "").trim();
  if (!value) throw new Error(`${key} is required`);
  return value;
}

function canonical(value) {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function slash(value) {
  return value.replaceAll("\\", "/");
}

function inside(candidate, root) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function globRegex(pattern) {
  let out = "^";
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index];
    if (char === "*") {
      if (pattern[index + 1] === "*") {
        index += 1;
        if (pattern[index + 1] === "/") {
          index += 1;
          out += "(?:.*/)?";
        } else {
          out += ".*";
        }
      } else {
        out += "[^/]*";
      }
    } else if (char === "?") {
      out += "[^/]";
    } else {
      out += char.replace(/[|\\{}()[\]^$+?.]/g, "\\$&");
    }
  }
  return new RegExp(`${out}$`, process.platform === "win32" ? "i" : "");
}

function segmentRegex(pattern) {
  let out = "^";
  for (const char of pattern) {
    if (char === "*") out += ".*";
    else if (char === "?") out += ".";
    else out += char.replace(/[|\\{}()[\]^$+?.]/g, "\\$&");
  }
  return new RegExp(`${out}$`, process.platform === "win32" ? "i" : "");
}

function epsilonClosure(states, segments) {
  const closed = new Set(states);
  const pending = [...closed];
  while (pending.length) {
    const index = pending.pop();
    if (segments[index] === "**" && !closed.has(index + 1)) {
      closed.add(index + 1);
      pending.push(index + 1);
    }
  }
  return closed;
}

// Whether a glob can match the directory itself or any descendant.  Recuris
// uses wildcard denies such as ma_runs/*/*.jsonl; a directory search is safe
// only when none of those languages intersects the requested subtree.
function globCouldMatchUnder(pattern, directoryRelative) {
  const patternSegments = slash(pattern).split("/").filter(Boolean);
  const directorySegments = slash(directoryRelative).split("/").filter(Boolean);
  let states = epsilonClosure(new Set([0]), patternSegments);
  for (const candidateSegment of directorySegments) {
    const next = new Set();
    for (const index of states) {
      if (index >= patternSegments.length) continue;
      const patternSegment = patternSegments[index];
      if (patternSegment === "**") next.add(index);
      else if (segmentRegex(patternSegment).test(candidateSegment)) next.add(index + 1);
    }
    states = epsilonClosure(next, patternSegments);
    if (!states.size) return false;
  }
  return states.size > 0;
}

function parseRule(raw, workspace) {
  const match = /^(Read|Edit|Write)\((.*)\)$/.exec(String(raw));
  if (!match) return undefined;
  const tool = match[1];
  const target = slash(match[2].trim()).replace(/^\.\//, "");
  const directory = target.endsWith("/**");
  const literal = directory ? target.slice(0, -3).replace(/\/$/, "") : target;
  const wildcard = /[*?]/.test(literal);
  return {
    tool,
    target,
    directory,
    wildcard,
    absolute: wildcard ? undefined : canonical(path.resolve(workspace, literal)),
    pattern: globRegex(target),
  };
}

function relativeForPolicy(candidate, workspace) {
  const relative = path.relative(workspace, candidate);
  if (relative === "") return ".";
  if (relative.startsWith("..") || path.isAbsolute(relative)) return undefined;
  return slash(relative);
}

function matchesRule(candidate, rule, workspace) {
  if (!rule.wildcard && rule.absolute !== undefined) {
    return rule.directory ? inside(candidate, rule.absolute) : candidate === rule.absolute;
  }
  const relative = relativeForPolicy(candidate, workspace);
  return relative !== undefined && rule.pattern.test(relative);
}

function pathArgument(name, args) {
  if (name === "read" || name === "edit" || name === "write") {
    return typeof args.file_path === "string" ? args.file_path : "";
  }
  if (name === "glob" || name === "grep") {
    return typeof args.path === "string" ? args.path : "";
  }
  return "";
}

function envFlag(key, fallback = true) {
  const raw = String(process.env[key] ?? (fallback ? "1" : "0")).trim().toLowerCase();
  return !["0", "false", "off", "no"].includes(raw);
}

function positiveEnv(key, fallback) {
  const value = Number(process.env[key] ?? fallback);
  if (!Number.isInteger(value) || value < 1) throw new Error(`${key} must be a positive integer`);
  return value;
}

function positiveArgument(args, key, fallback, maximum) {
  const raw = args[key];
  if (raw === undefined) return fallback;
  if (!Number.isInteger(raw) || raw < 1 || raw > maximum) {
    throw new Error(`${key} must be an integer from 1 to ${maximum}`);
  }
  return raw;
}

function nonNegativeArgument(args, key, fallback, maximum) {
  const raw = args[key];
  if (raw === undefined) return fallback;
  if (!Number.isInteger(raw) || raw < 0 || raw > maximum) {
    throw new Error(`${key} must be an integer from 0 to ${maximum}`);
  }
  return raw;
}

function searchableReadDefinition(baseRead, config) {
  const properties = baseRead.parameters?.properties;
  const outputProperties = baseRead.output?.schema?.properties;
  if (!properties || !outputProperties) {
    throw new Error("DSH read definition is incompatible with searchable context");
  }

  return {
    ...baseRead,
    description: `${baseRead.description} Set query to search one approved file and return only matching lines plus bounded context.`,
    parameters: {
      ...baseRead.parameters,
      properties: {
        ...properties,
        query: {
          type: "string",
          description: "Optional case-insensitive literal to search for inside this approved file. Omit for an ordinary line-window read.",
        },
        case_sensitive: {
          type: "boolean",
          description: "Use case-sensitive matching for query. Defaults to false.",
        },
        context_lines: {
          type: "integer",
          minimum: 0,
          maximum: config.maxContextLines,
          description: `Lines of context on each side of a query match. Defaults to ${config.defaultContextLines}.`,
        },
        max_matches: {
          type: "integer",
          minimum: 1,
          maximum: config.maxMatches,
          description: `Maximum matching lines to retain. Defaults to ${config.defaultMatches}.`,
        },
      },
    },
    output: {
      ...baseRead.output,
      schema: {
        ...baseRead.output.schema,
        properties: {
          ...outputProperties,
          search: {
            type: "object",
            additionalProperties: false,
            properties: {
              query: { type: "string" },
              caseSensitive: { type: "boolean" },
              matchCount: { type: "integer" },
              scannedFrom: { type: "integer" },
              scannedThrough: { type: "integer" },
              scanComplete: { type: "boolean" },
              maxMatchesReached: { type: "boolean" },
              outputTruncated: { type: "boolean" },
            },
            required: [
              "query", "caseSensitive", "matchCount", "scannedFrom",
              "scannedThrough", "scanComplete", "maxMatchesReached", "outputTruncated",
            ],
          },
        },
      },
      render(args, value) {
        if (value.search === undefined) return baseRead.output.render(args, value);
        const search = value.search;
        const matchLabel = search.maxMatchesReached
          ? `at least ${search.matchCount}`
          : String(search.matchCount);
        const status = search.scanComplete
          ? `Scanned lines ${search.scannedFrom}-${search.scannedThrough} of ${value.totalLines}.`
          : `Scan budget stopped at line ${search.scannedThrough} of ${value.totalLines}; narrow the query or continue with offset=${search.scannedThrough + 1}.`;
        const lines = value.lines.map((line) => `${line.number}: ${line.text}`).join("\n");
        const truncation = search.outputTruncated
          ? "\nSelected context exceeded the requested limit; use a smaller context_lines value or continue with an offset."
          : "";
        return [{
          type: "text",
          text: `<path>${value.path}</path>\n<type>search-results</type>\n<content>\nQuery: ${search.query}\nMatches retained: ${matchLabel}\n${status}${truncation}\n\n${lines || "(no matching lines)"}\n</content>`,
        }];
      },
    },
    async execute(args, exec) {
      const query = typeof args.query === "string" ? args.query.trim() : "";
      if (!query) return baseRead.execute(args, exec);

      const start = positiveArgument(args, "offset", 1, Number.MAX_SAFE_INTEGER);
      const outputLimit = positiveArgument(
        args, "limit", config.defaultOutputLines, config.readLimit,
      );
      const contextLines = nonNegativeArgument(
        args, "context_lines", config.defaultContextLines, config.maxContextLines,
      );
      const maxMatches = positiveArgument(args, "max_matches", config.defaultMatches, config.maxMatches);
      const caseSensitive = args.case_sensitive === true;
      const needle = caseSensitive ? query : query.toLocaleLowerCase();
      const cached = new Map();
      const matches = [];
      let cursor = start;
      let totalLines = 0;
      let displayPath = String(args.file_path ?? "");
      let scannedThrough = start - 1;

      while (matches.length < maxMatches && scannedThrough - start + 1 < config.maxScanLines) {
        const remaining = config.maxScanLines - (scannedThrough - start + 1);
        const window = await baseRead.execute({
          file_path: args.file_path,
          offset: cursor,
          limit: Math.min(config.scanChunkLines, remaining),
        }, exec);
        displayPath = window.path;
        totalLines = window.totalLines;
        if (!window.lines.length) break;
        for (const line of window.lines) {
          cached.set(line.number, line.text);
          const haystack = caseSensitive ? line.text : line.text.toLocaleLowerCase();
          if (matches.length < maxMatches && haystack.includes(needle)) matches.push(line.number);
        }
        scannedThrough = window.lines.at(-1).number;
        if (scannedThrough >= totalLines) break;
        cursor = scannedThrough + 1;
      }

      const selectedNumbers = new Set();
      for (const number of matches) {
        for (let selected = Math.max(start, number - contextLines);
          selected <= Math.min(totalLines, number + contextLines); selected += 1) {
          if (cached.has(selected)) selectedNumbers.add(selected);
        }
      }
      const selected = [...selectedNumbers]
        .sort((left, right) => left - right)
        .map((number) => ({ number, text: cached.get(number) }));
      const outputTruncated = selected.length > outputLimit;
      const lines = selected.slice(0, outputLimit);
      return {
        path: displayPath,
        offset: lines.at(0)?.number ?? start,
        lines,
        totalLines,
        search: {
          query,
          caseSensitive,
          matchCount: matches.length,
          scannedFrom: start,
          scannedThrough,
          scanComplete: scannedThrough >= totalLines,
          maxMatchesReached: matches.length >= maxMatches,
          outputTruncated,
        },
      };
    },
  };
}

function textOfContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const values = [];
  for (const block of content) {
    if (typeof block === "string") values.push(block);
    else if (block && typeof block === "object" && block.type === "text") {
      values.push(String(block.text ?? ""));
    } else if (block && typeof block === "object" && block.type === "tool-call") {
      values.push(`tool call ${String(block.name ?? "unknown")}: ${String(block.arguments ?? "")}`);
    } else if (block && typeof block === "object" && block.type === "tool-result") {
      values.push(`tool result: ${textOfContent(block.content)}`);
    }
  }
  return values.filter(Boolean).join("\n");
}

function sessionFiles(root) {
  if (!fs.existsSync(root)) return [];
  const found = [];
  const pending = [root];
  while (pending.length) {
    const directory = pending.pop();
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const candidate = path.join(directory, entry.name);
      if (entry.isDirectory()) pending.push(candidate);
      else if (entry.isFile() && entry.name === "session.jsonl") found.push(candidate);
    }
  }
  return found;
}

function sessionHeader(file) {
  const text = fs.readFileSync(file, "utf8");
  const first = text.split(/\r?\n/, 1)[0];
  if (!first.trim()) return undefined;
  try {
    const value = JSON.parse(first);
    return value?.type === "session" ? value : undefined;
  } catch {
    return undefined;
  }
}

function rootSessionFile(sessionRoot, rootSessionId) {
  const files = sessionFiles(sessionRoot);
  return files.find((file) => String(sessionHeader(file)?.id ?? "") === rootSessionId)
    ?? files.find((file) => sessionHeader(file)?.origin !== "subagent");
}

function loadState(statePath) {
  if (fs.existsSync(statePath)) {
    try {
      const value = JSON.parse(fs.readFileSync(statePath, "utf8"));
      if (value?.version === CONTEXT_WORKSPACE_VERSION
        && value.entries && typeof value.entries === "object"
        && Array.isArray(value.operations)) return value;
    } catch {
      // A corrupt run-local state file is replaced with an empty state below.
    }
  }
  return {
    version: CONTEXT_WORKSPACE_VERSION,
    entries: {},
    operations: [],
    createdAtMs: Date.now(),
    updatedAtMs: Date.now(),
  };
}

function saveState(statePath, state) {
  state.updatedAtMs = Date.now();
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  fs.writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
}

function historyRecords(promptPath, sessionRoot, rootSessionId, state) {
  const records = [];
  if (fs.existsSync(promptPath)) {
    records.push({ source: "phase-prompt", text: fs.readFileSync(promptPath, "utf8") });
  }
  const file = rootSessionFile(sessionRoot, rootSessionId);
  if (file !== undefined) {
    const lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
    for (const raw of lines) {
      if (!raw.trim()) continue;
      let row;
      try {
        row = JSON.parse(raw);
      } catch {
        continue;
      }
      const data = row?.data && typeof row.data === "object" ? row.data : {};
      const message = data?.message && typeof data.message === "object" ? data.message : {};
      let text = "";
      if (["user/message", "assistant/message", "tool/result"].includes(row.type)) {
        text = textOfContent(message.content);
      } else if (row.type === "tool/call") {
        text = `tool call ${String(data.name ?? "unknown")}: ${String(data.arguments ?? "")}`;
      } else if (String(row.type ?? "").includes("compaction")) {
        text = String(data.summary ?? data.text ?? "");
      }
      if (text.trim()) {
        records.push({
          source: `${String(row.type)}#${String(row.seq ?? "?")}`,
          text,
        });
      }
    }
  }
  for (const [key, entry] of Object.entries(state.entries)) {
    records.push({ source: `memory:${key}`, text: String(entry.value ?? "") });
  }
  return records;
}

function searchRecords(records, query, maxResults, contextChars) {
  const needle = query.toLocaleLowerCase();
  const matches = [];
  for (const record of records) {
    const haystack = record.text.toLocaleLowerCase();
    const index = haystack.indexOf(needle);
    if (index < 0) continue;
    const start = Math.max(0, index - contextChars);
    const end = Math.min(record.text.length, index + query.length + contextChars);
    const prefix = start > 0 ? "…" : "";
    const suffix = end < record.text.length ? "…" : "";
    matches.push({
      source: record.source,
      excerpt: `${prefix}${record.text.slice(start, end).replace(/\s+/g, " ").trim()}${suffix}`,
    });
    if (matches.length >= maxResults) break;
  }
  return matches;
}

function contextText(operation, text, meta = {}) {
  return { operation, text, meta };
}

function requireString(args, name, maximum) {
  const value = typeof args[name] === "string" ? args[name].trim() : "";
  if (!value) throw new Error(`${name} is required for this Context operation`);
  if (value.length > maximum) throw new Error(`${name} exceeds ${maximum} characters`);
  return value;
}

function contextDefinition(config) {
  return {
    name: "context",
    description: "Search the complete phase transcript after context pruning, save/load persistent run-local working memory, or ask a fresh read-only local-Qwen context worker to synthesize evidence. Use search before repeating old reads; remember compact decisions or state needed later; delegate only when independent evidence synthesis is worth another model call.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        operation: {
          type: "string",
          enum: ["search", "remember", "get", "list", "delegate"],
          description: "search history/memory, remember a value, get/list memory, or delegate read-only evidence synthesis.",
        },
        query: {
          type: "string",
          description: "Literal transcript/memory query. For delegate, optionally selects prior evidence to include.",
        },
        key: { type: "string", description: "Memory key for remember/get." },
        value: { type: "string", description: "Persistent run-local value for remember." },
        question: { type: "string", description: "Self-contained evidence question for delegate." },
        max_results: { type: "integer", minimum: 1, maximum: 50 },
        context_chars: { type: "integer", minimum: 40, maximum: 2000 },
      },
      required: ["operation"],
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          operation: { type: "string" },
          text: { type: "string" },
          meta: { type: "object", additionalProperties: true },
        },
        required: ["operation", "text", "meta"],
      },
      render: (_args, value) => [{ type: "text", text: value.text }],
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const startedAtMs = Date.now();
      const operation = String(args.operation ?? "");
      let outcome = "success";
      let resultMeta = {};
      try {
        if (operation === "remember") {
          const key = requireString(args, "key", 64);
          if (!/^[A-Za-z0-9._-]+$/.test(key)) {
            throw new Error("key may contain only letters, numbers, dot, underscore, and hyphen");
          }
          const value = requireString(args, "value", config.maxValueChars);
          const nextEntries = { ...config.state.entries, [key]: { value, updatedAtMs: Date.now() } };
          if (Object.keys(nextEntries).length > config.maxEntries) {
            throw new Error(`working memory is limited to ${config.maxEntries} keys`);
          }
          const totalChars = Object.values(nextEntries)
            .reduce((total, entry) => total + String(entry.value ?? "").length, 0);
          if (totalChars > config.maxTotalChars) {
            throw new Error(`working memory is limited to ${config.maxTotalChars} characters`);
          }
          config.state.entries = nextEntries;
          resultMeta = { key, valueChars: value.length, entries: Object.keys(nextEntries).length };
          return contextText(operation, `Remembered ${key} (${value.length} characters).`, resultMeta);
        }
        if (operation === "get") {
          const key = requireString(args, "key", 64);
          const entry = config.state.entries[key];
          if (entry === undefined) return contextText(operation, `No memory named ${key}.`, { key, found: false });
          resultMeta = { key, found: true, valueChars: String(entry.value).length };
          return contextText(operation, `[memory:${key}]\n${String(entry.value)}`, resultMeta);
        }
        if (operation === "list") {
          const entries = Object.entries(config.state.entries).map(([key, entry]) => ({
            key,
            valueChars: String(entry.value ?? "").length,
            updatedAtMs: entry.updatedAtMs,
          }));
          resultMeta = { entries };
          const text = entries.length
            ? entries.map((entry) => `${entry.key} (${entry.valueChars} chars)`).join("\n")
            : "Working memory is empty.";
          return contextText(operation, text, resultMeta);
        }
        if (operation === "search") {
          const query = requireString(args, "query", 1000);
          const maxResults = positiveArgument(args, "max_results", 12, 50);
          const contextChars = positiveArgument(args, "context_chars", 320, 2000);
          const records = historyRecords(
            config.promptPath, config.sessionRoot, String(exec.agent.session.header.id), config.state,
          );
          const matches = searchRecords(records, query, maxResults, contextChars);
          resultMeta = { query, matches: matches.length, corpusRecords: records.length };
          const text = matches.length
            ? matches.map((match) => `[${match.source}] ${match.excerpt}`).join("\n\n")
            : `No phase-history or working-memory match for: ${query}`;
          return contextText(operation, text, resultMeta);
        }
        if (operation === "delegate") {
          if (config.delegateCount >= config.maxDelegations) {
            throw new Error(`Context delegate budget exhausted (${config.maxDelegations} per phase)`);
          }
          const question = requireString(args, "question", 4000);
          const query = typeof args.query === "string" ? args.query.trim() : "";
          let evidence = "";
          let evidenceMatches = 0;
          if (query) {
            const records = historyRecords(
              config.promptPath, config.sessionRoot, String(exec.agent.session.header.id), config.state,
            );
            const matches = searchRecords(records, query, 12, 320);
            evidenceMatches = matches.length;
            evidence = matches.map((match) => `[${match.source}] ${match.excerpt}`).join("\n\n");
          }
          config.delegateCount += 1;
          const catalog = config.readTargets.length
            ? config.readTargets.map((target) => `- ${target}`).join("\n")
            : "- No filesystem reads are available; use only supplied context.";
          const childPrompt = `Question:\n${question}\n\nApproved readable targets:\n${catalog}`
            + (evidence ? `\n\nSelected prior phase context:\n${evidence}` : "")
            + "\n\nAnswer concisely with the exact evidence used. You are read-only. Do not propose or perform edits.";
          const controller = new AbortController();
          const forwardAbort = () => controller.abort(exec.signal.reason ?? "parent context call aborted");
          exec.signal.addEventListener("abort", forwardAbort, { once: true });
          const timer = setTimeout(
            () => controller.abort(`context worker exceeded ${config.childTimeoutMs} ms`),
            config.childTimeoutMs,
          );
          let run;
          try {
            run = await config.ctx.subagents.start("spawn", {
              label: "recuris-context-worker",
              prompt: [{ type: "text", text: childPrompt }],
              parent: exec.agent,
              signal: controller.signal,
              maxDepth: 1,
              toolFilter: { allow: config.readOnlyTools },
              persona: "You are a focused read-only context researcher. Resolve only the delegated evidence question, use the smallest sufficient reads/searches, cite concrete file paths or supplied transcript sources, and return a concise answer. Never mutate files.",
              agentOptions: { maxTokens: config.childMaxTokens },
            });
            const child = await run.result;
            const answer = textOfContent(child.output).slice(0, config.maxChildOutputChars);
            resultMeta = {
              childSessionId: String(run.id),
              stopReason: child.stopReason,
              evidenceMatches,
              outputChars: answer.length,
            };
            return contextText(
              operation,
              answer || `(context worker ended ${String(child.stopReason)} without text)`,
              resultMeta,
            );
          } finally {
            clearTimeout(timer);
            exec.signal.removeEventListener("abort", forwardAbort);
            if (run !== undefined) await run.dispose();
          }
        }
        throw new Error(`unsupported Context operation: ${operation}`);
      } catch (error) {
        outcome = "error";
        resultMeta = { error: error instanceof Error ? error.message : String(error) };
        throw error;
      } finally {
        config.state.operations.push({
          operation,
          outcome,
          startedAtMs,
          elapsedMs: Date.now() - startedAtMs,
          ...resultMeta,
        });
        if (config.state.operations.length > 256) config.state.operations.shift();
        saveState(config.statePath, config.state);
      }
    },
  };
}

function searchableContextText(readTargets, requestedTools) {
  const searchable = requestedTools.includes("grep")
    ? "Use Grep on an approved exact file or safely searchable approved directory when you need a fact across evidence."
    : "Grep is not enabled in this phase. Search an approved exact file with Read's query argument when you need a fact.";
  const catalog = readTargets.length
    ? `\nApproved readable targets for this phase:\n${readTargets.map((target) => `- ${target}`).join("\n")}`
    : "";
  const context = requestedTools.includes("context")
    ? ` Complete phase history and named working memory persist outside the active context through Context (${CONTEXT_WORKSPACE_VERSION}). Use Context search when a fact may be in an older message/result, remember compact decisions or state that must survive pruning, and delegate only when a fresh read-only local-Qwen evidence synthesis is worth an extra model call. A delegate starts with zero parent conversation and cannot edit.`
    : "";
  return `Searchable phase context (${SEARCHABLE_CONTEXT_VERSION}). Treat approved evidence as external memory instead of loading it wholesale. ${searchable} Use ordinary Read with explicit offset and limit for the smallest surrounding window, expanding only when evidence requires it. Reuse prior results rather than repeating identical retrievals. Required reads and final verification still take priority over retrieval efficiency.${context}${catalog}`;
}

function writeReport(reportPath, value) {
  fs.mkdirSync(path.dirname(reportPath), { recursive: true });
  fs.writeFileSync(reportPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

export function apply(ctx) {
  const workspace = canonical(requiredEnv("RECURIS_DSH_WORKSPACE"));
  const scopePath = requiredEnv("RECURIS_DSH_SCOPE_FILE");
  const reportPath = requiredEnv("RECURIS_DSH_SCOPE_REPORT");
  const requestedClaudeTools = requiredEnv("RECURIS_DSH_TOOLS")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const unknown = requestedClaudeTools.filter((tool) => TOOL_MAP[tool] === undefined);
  if (unknown.length) throw new Error(`unsupported Recuris tools: ${unknown.join(", ")}`);

  const requestedTools = [...new Set(requestedClaudeTools.map((tool) => TOOL_MAP[tool]))];
  if (!requestedTools.length) throw new Error("Recuris supplied an empty tool surface");

  const settings = JSON.parse(fs.readFileSync(scopePath, "utf8"));
  const permissions = settings?.permissions;
  if (!permissions || !Array.isArray(permissions.allow) || !Array.isArray(permissions.deny)) {
    throw new Error("Recuris scope file lacks permissions.allow/deny arrays");
  }
  const allowRules = permissions.allow.map((rule) => parseRule(rule, workspace)).filter(Boolean);
  const denyRules = permissions.deny.map((rule) => parseRule(rule, workspace)).filter(Boolean);
  const searchableContextEnabled = envFlag("RECURIS_DSH_SEARCHABLE_CONTEXT", true);
  const searchableReadConfig = {
    readLimit: positiveEnv("RECURIS_DSH_READ_LIMIT", 240),
    scanChunkLines: positiveEnv("RECURIS_DSH_SEARCH_CHUNK_LINES", 200),
    maxScanLines: positiveEnv("RECURIS_DSH_SEARCH_MAX_SCAN_LINES", 50000),
    defaultContextLines: positiveEnv("RECURIS_DSH_SEARCH_CONTEXT_LINES", 2),
    maxContextLines: positiveEnv("RECURIS_DSH_SEARCH_MAX_CONTEXT_LINES", 20),
    defaultMatches: positiveEnv("RECURIS_DSH_SEARCH_MATCHES", 20),
    maxMatches: positiveEnv("RECURIS_DSH_SEARCH_MAX_MATCHES", 100),
    defaultOutputLines: positiveEnv("RECURIS_DSH_SEARCH_OUTPUT_LINES", 120),
  };
  if (searchableReadConfig.defaultContextLines > searchableReadConfig.maxContextLines) {
    throw new Error("RECURIS_DSH_SEARCH_CONTEXT_LINES exceeds RECURIS_DSH_SEARCH_MAX_CONTEXT_LINES");
  }
  if (searchableReadConfig.defaultMatches > searchableReadConfig.maxMatches) {
    throw new Error("RECURIS_DSH_SEARCH_MATCHES exceeds RECURIS_DSH_SEARCH_MAX_MATCHES");
  }
  if (searchableReadConfig.scanChunkLines > searchableReadConfig.readLimit) {
    throw new Error("RECURIS_DSH_SEARCH_CHUNK_LINES exceeds RECURIS_DSH_READ_LIMIT");
  }
  if (searchableReadConfig.defaultOutputLines > searchableReadConfig.readLimit) {
    throw new Error("RECURIS_DSH_SEARCH_OUTPUT_LINES exceeds RECURIS_DSH_READ_LIMIT");
  }
  const readTargets = [...new Set(
    allowRules.filter((rule) => rule.tool === "Read").map((rule) => rule.target),
  )];
  const contextEnabled = requestedTools.includes("context");
  const contextStatePath = contextEnabled ? requiredEnv("RECURIS_DSH_CONTEXT_STATE") : "";
  const contextState = contextEnabled ? loadState(contextStatePath) : undefined;
  const promptPath = requiredEnv("RECURIS_DSH_PROMPT_FILE");
  const sessionRoot = requiredEnv("RECURIS_DSH_SESSION_ROOT");
  const childMaxTokens = positiveEnv("RECURIS_DSH_CONTEXT_CHILD_MAX_TOKENS", 1536);
  const childTimeoutMs = positiveEnv("RECURIS_DSH_CONTEXT_CHILD_TIMEOUT_MS", 20000);
  const maxDelegations = positiveEnv("RECURIS_DSH_CONTEXT_MAX_DELEGATIONS", 3);
  const readOnlyTools = requestedTools.filter((tool) => ["read", "glob", "grep"].includes(tool));
  if (contextEnabled) {
    saveState(contextStatePath, contextState);
    ctx.tools.register(contextDefinition({
      ctx,
      state: contextState,
      statePath: contextStatePath,
      promptPath,
      sessionRoot,
      readTargets,
      readOnlyTools,
      childMaxTokens,
      childTimeoutMs,
      maxDelegations,
      delegateCount: 0,
      maxEntries: 128,
      maxValueChars: 32768,
      maxTotalChars: 262144,
      maxChildOutputChars: 12000,
    }));
  }

  ctx.on("agent/created", ({ agent }) => {
    const isChild = agent.session.header.origin === "subagent";
    const toolsForAgent = isChild ? readOnlyTools : requestedTools;
    const deniedRequests = new Map();
    const deny = (execution, reason) => {
      const signature = `${execution.name}\0${JSON.stringify(execution.arguments ?? {})}\0${reason}`;
      const attempts = (deniedRequests.get(signature) ?? 0) + 1;
      deniedRequests.set(signature, attempts);
      if (attempts >= 3) {
        queueMicrotask(() => agent.cancel({
          kind: "hook",
          reason: `Recuris stopped three identical denied tool requests: ${execution.name}`,
        }));
        return `${reason}; repeated denied request ${attempts}/3, cancelling this agent turn`;
      }
      return reason;
    };
    let restrictionError = "";
    try {
      agent.ctx.tools.restrict({ allow: toolsForAgent });
      if (searchableContextEnabled && toolsForAgent.includes("read")) {
        const baseRead = agent.ctx.tools.get("read", agent);
        if (baseRead === undefined) throw new Error("DSH read tool is unavailable after restriction");
        agent.ctx.tools.register(searchableReadDefinition(baseRead, searchableReadConfig));
      }
      if (searchableContextEnabled && toolsForAgent.some((tool) => ["read", "grep", "glob"].includes(tool))) {
        agent.ctx.systemPrompt.section({
          name: "recuris:searchable-context",
          order: 190,
          text: searchableContextText(readTargets, toolsForAgent),
        });
      }
    } catch (error) {
      restrictionError = error instanceof Error ? error.message : String(error);
    }

    agent.ctx.tools.guard((execution) => {
      if (restrictionError) {
        return deny(execution, `permission denied: tool restriction failed: ${restrictionError}`);
      }
      if (!toolsForAgent.includes(execution.name)) {
        return deny(
          execution,
          `permission denied: ${execution.name} is not enabled for this Recuris phase`,
        );
      }
      if (execution.name === "context") {
        return isChild
          ? deny(execution, "permission denied: delegated context workers cannot recursively delegate")
          : undefined;
      }

      const rawPath = pathArgument(execution.name, execution.arguments ?? {});
      if (!rawPath.trim()) {
        return deny(
          execution,
          `permission denied: ${execution.name} requires an explicit scoped path`,
        );
      }
      const candidate = canonical(path.resolve(workspace, rawPath));

      if (execution.name === "read") {
        const denied = denyRules.some((rule) => rule.tool === "Read" && matchesRule(candidate, rule, workspace));
        const allowed = allowRules.some((rule) => rule.tool === "Read" && matchesRule(candidate, rule, workspace));
        return denied || !allowed ? deny(
          execution,
          `permission denied: read path is outside this Recuris phase: ${rawPath}`,
        ) : undefined;
      }

      if (execution.name === "edit" || execution.name === "write") {
        const policyTool = execution.name === "edit" ? "Edit" : "Write";
        const denied = denyRules.some((rule) => rule.tool === policyTool && matchesRule(candidate, rule, workspace));
        const allowed = allowRules.some((rule) => rule.tool === policyTool && matchesRule(candidate, rule, workspace));
        return denied || !allowed ? deny(
          execution,
          `permission denied: ${execution.name} path is outside this Recuris phase: ${rawPath}`,
        ) : undefined;
      }

      // Grep may safely target one exact allowed file. Directory searches need
      // a readable subtree whose language does not intersect any deny glob.
      const allowedExactFile = execution.name === "grep" && allowRules.some(
        (rule) => rule.tool === "Read" && !rule.directory && matchesRule(candidate, rule, workspace),
      );
      const allowedDirectory = allowRules.some(
        (rule) => rule.tool === "Read" && rule.directory && rule.absolute !== undefined
          && inside(candidate, rule.absolute),
      );
      const crossesDeniedRead = denyRules.some((rule) => {
        if (rule.tool !== "Read") return false;
        if (allowedExactFile) return matchesRule(candidate, rule, workspace);
        const relative = relativeForPolicy(candidate, workspace);
        if (relative === undefined) return true;
        if (rule.wildcard) return globCouldMatchUnder(rule.target, relative);
        if (matchesRule(candidate, rule, workspace)) return true;
        return rule.absolute !== undefined && inside(rule.absolute, candidate);
      });
      return (!allowedExactFile && !allowedDirectory) || crossesDeniedRead ? deny(
        execution,
        `permission denied: ${execution.name} path is outside this Recuris phase: ${rawPath}`,
      ) : undefined;
    });

    // schemas() without a scope intentionally returns DSH's global registry.
    // Pass the agent so the report proves the same restricted view presented
    // to this phase's model (and used for execution resolution).
    if (isChild) return;
    const visibleTools = agent.ctx.tools.schemas(agent).map((schema) => schema.name).sort();
    const expectedTools = [...requestedTools].sort();
    const report = {
      workspace,
      requestedClaudeTools,
      expectedTools,
      visibleTools,
      restrictionError,
      exactToolSurface: restrictionError === ""
        && JSON.stringify(visibleTools) === JSON.stringify(expectedTools),
      allowRuleCount: allowRules.length,
      denyRuleCount: denyRules.length,
      searchableContext: {
        enabled: searchableContextEnabled,
        patternVersion: SEARCHABLE_CONTEXT_VERSION,
        readTargets,
        enhancedReadSearch: searchableContextEnabled && requestedTools.includes("read"),
      },
      contextWorkspace: {
        enabled: contextEnabled,
        version: CONTEXT_WORKSPACE_VERSION,
        persistentHistory: contextEnabled,
        persistentWorkingMemory: contextEnabled,
        readOnlyContextWorkers: contextEnabled,
        childMaxTokens,
        childTimeoutMs,
        maxDelegations,
        childTools: readOnlyTools,
        repeatedDeniedRequestLimit: 3,
      },
    };
    writeReport(reportPath, report);
    if (restrictionError) {
      throw new Error(`Recuris DSH tool-surface setup failed: ${restrictionError}`);
    }
  });
}
