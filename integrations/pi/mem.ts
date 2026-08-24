/**
 * mem — local, cross-harness semantic memory tools for Pi.
 *
 * Gives the model NAMED tools (memory_search / memory_add) backed by the shared
 * ~/.mem/store.db, so a local model doesn't have to hand-craft a bash command —
 * which it does far more reliably. Shells out to the `mem` tool via uv with
 * absolute paths (no PATH dependency).
 *
 * Auto-discovered from ~/.pi/agent/extensions/. Hot-reload with /reload.
 */
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { homedir } from "node:os";
import { join } from "node:path";

const execFileAsync = promisify(execFile);
// Absolute so the extension does not depend on PATH. Override either with an
// env var if uv or the repo lives somewhere else.
const UV = process.env.MEM_UV ?? join(homedir(), ".local", "bin", "uv");
const MEM = process.env.MEM_SCRIPT ?? join(homedir(), "Projects", "mem", "mem.py");

async function runMem(args: string[]): Promise<string> {
	const { stdout } = await execFileAsync(UV, ["run", MEM, ...args], {
		timeout: 60000,
		maxBuffer: 4 * 1024 * 1024,
	});
	return stdout;
}

export default function memExtension(pi: ExtensionAPI) {
	pi.registerTool({
		name: "memory_search",
		label: "Search memory",
		description:
			"Search Nick's local cross-harness memory. Use FIRST for recall/context questions, before web-searching.",
		promptSnippet: "Call memory_search before answering recall questions.",
		promptGuidelines: [
			"For 'what/where/when did I…', past decisions, or project status: call memory_search FIRST, then ground on the result.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "What to recall" }),
			k: Type.Optional(Type.Number({ description: "Max results (default 2; raise if you need more)" })),
		}),
		async execute(_toolCallId, params) {
			try {
				const out = await runMem(["query", params.query, "-k", String(params.k ?? 2), "--json"]);
				const hits = JSON.parse(out || "[]") as Array<{ text: string; source: string; score: number }>;
				let text = hits.length
					? hits
							.map((h) => `• ${h.source.split("/").pop()}: ${h.text.replace(/\s+/g, " ").slice(0, 110)}`)
							.join("\n")
					: "No relevant memory found.";
				// hard cap — keep tool output tiny for small-context local models
				if (text.length > 320) text = `${text.slice(0, 320)} …`;
				return { content: [{ type: "text", text }], details: { count: hits.length } };
			} catch (e) {
				return {
					content: [{ type: "text", text: `memory_search error: ${String(e).slice(0, 200)}` }],
					details: {},
				};
			}
		},
	});

	pi.registerTool({
		name: "memory_add",
		label: "Add to memory",
		description:
			"Store a durable fact/decision/preference into Nick's shared memory for later recall. Skip chatter.",
		promptSnippet: "Use memory_add to persist durable facts.",
		parameters: Type.Object({
			text: Type.String({ description: "The fact in one sentence" }),
			kind: Type.Optional(Type.String({ description: "decision | preference | fact | note" })),
		}),
		async execute(_toolCallId, params) {
			try {
				const out = await runMem(["add", params.text, "--source", "pi", "--kind", params.kind ?? "note"]);
				return { content: [{ type: "text", text: out.trim() || "stored" }], details: {} };
			} catch (e) {
				return {
					content: [{ type: "text", text: `memory_add error: ${String(e).slice(0, 200)}` }],
					details: {},
				};
			}
		},
	});
}
