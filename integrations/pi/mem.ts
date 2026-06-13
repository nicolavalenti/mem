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

const execFileAsync = promisify(execFile);
const UV = "/Users/nickvalenti/.local/bin/uv";
const MEM = "/Users/nickvalenti/Projects/mem/mem.py";

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
			"Search Nick's local, cross-harness memory (decisions, preferences, projects, notes). " +
			"Free and local. Use this FIRST for any recall/context question or before web-searching.",
		promptSnippet: "Use memory_search before answering recall/context questions.",
		promptGuidelines: [
			"For any 'what/where/when did I…', past-decision, preference, or project-status question, call memory_search FIRST.",
			"Use the returned snippets as grounded context; only fall back to other sources if nothing relevant comes back.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "What to recall, in natural language" }),
			k: Type.Optional(Type.Number({ description: "Max results (default 5)" })),
		}),
		async execute(_toolCallId, params) {
			try {
				const out = await runMem(["query", params.query, "-k", String(params.k ?? 5), "--json"]);
				const hits = JSON.parse(out || "[]") as Array<{ text: string; source: string; score: number }>;
				const text = hits.length
					? hits
							.map((h) => `• (${h.score}) ${h.source}\n  ${h.text.replace(/\s+/g, " ").slice(0, 400)}`)
							.join("\n\n")
					: "No relevant memory found.";
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
			"Store a durable fact, decision, or preference into Nick's shared local memory so any " +
			"harness can recall it later. Skip transient chatter.",
		promptSnippet: "Use memory_add to persist durable facts/decisions/preferences.",
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
