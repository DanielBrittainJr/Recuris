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
export const inject = ["tools"];

const TOOL_MAP = Object.freeze({
  Read: "read",
  Glob: "glob",
  Grep: "grep",
  Edit: "edit",
  Write: "write",
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

  ctx.on("agent/created", ({ agent }) => {
    let restrictionError = "";
    try {
      agent.ctx.tools.restrict({ allow: requestedTools });
    } catch (error) {
      restrictionError = error instanceof Error ? error.message : String(error);
    }

    agent.ctx.tools.guard((execution) => {
      if (restrictionError) return `permission denied: tool restriction failed: ${restrictionError}`;
      if (!requestedTools.includes(execution.name)) {
        return `permission denied: ${execution.name} is not enabled for this Recuris phase`;
      }

      const rawPath = pathArgument(execution.name, execution.arguments ?? {});
      if (!rawPath.trim()) {
        return `permission denied: ${execution.name} requires an explicit scoped path`;
      }
      const candidate = canonical(path.resolve(workspace, rawPath));

      if (execution.name === "read") {
        const denied = denyRules.some((rule) => rule.tool === "Read" && matchesRule(candidate, rule, workspace));
        const allowed = allowRules.some((rule) => rule.tool === "Read" && matchesRule(candidate, rule, workspace));
        return denied || !allowed
          ? `permission denied: read path is outside this Recuris phase: ${rawPath}`
          : undefined;
      }

      if (execution.name === "edit" || execution.name === "write") {
        const policyTool = execution.name === "edit" ? "Edit" : "Write";
        const denied = denyRules.some((rule) => rule.tool === policyTool && matchesRule(candidate, rule, workspace));
        const allowed = allowRules.some((rule) => rule.tool === policyTool && matchesRule(candidate, rule, workspace));
        return denied || !allowed
          ? `permission denied: ${execution.name} path is outside this Recuris phase: ${rawPath}`
          : undefined;
      }

      // Recuris requires Glob/Grep to name an explicitly readable directory.
      const allowedDirectory = allowRules.some(
        (rule) => rule.tool === "Read" && rule.directory && rule.absolute !== undefined
          && inside(candidate, rule.absolute),
      );
      const crossesDeniedRead = denyRules.some((rule) => {
        if (rule.tool !== "Read") return false;
        if (matchesRule(candidate, rule, workspace)) return true;
        return rule.absolute !== undefined && inside(rule.absolute, candidate);
      });
      return !allowedDirectory || crossesDeniedRead
        ? `permission denied: ${execution.name} path is outside this Recuris phase: ${rawPath}`
        : undefined;
    });

    // schemas() without a scope intentionally returns DSH's global registry.
    // Pass the agent so the report proves the same restricted view presented
    // to this phase's model (and used for execution resolution).
    const visibleTools = agent.ctx.tools.schemas(agent).map((schema) => schema.name).sort();
    const expectedTools = [...requestedTools].sort();
    writeReport(reportPath, {
      workspace,
      requestedClaudeTools,
      expectedTools,
      visibleTools,
      restrictionError,
      exactToolSurface: restrictionError === ""
        && JSON.stringify(visibleTools) === JSON.stringify(expectedTools),
      allowRuleCount: allowRules.length,
      denyRuleCount: denyRules.length,
    });
  });
}
