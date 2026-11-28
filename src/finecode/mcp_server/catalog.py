"""Built-in MCP tool descriptors.

Ownership: neutral catalog. Exact built-in descriptors, description strings and
stable input schemas. No client or session state.
"""

from __future__ import annotations

from finecode.mcp_server.contracts import ToolDescriptor

__all__ = ["BUILTIN_TOOLS"]

_PROJECT_ARG_DESCRIPTION = (
    "Absolute path to the project directory. Use the list_projects tool to see"
    " available projects."
)

BUILTIN_TOOLS: tuple[ToolDescriptor, ...] = (
    {
        "name": "list_projects",
        "description": "List all projects in the FineCode workspace with their names, paths, and statuses",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_runners",
        "description": "List all extension runners and their status (running, stopped, error). Use this to diagnose failures when actions do not respond.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_resource_usage",
        "description": "Report what the workspace server is doing and what it costs (projects, runners, work/startup slots, in-flight runs, host pressure, peaks). The method itself starts no runner, takes no lease and waits on no lock. Set includeProcesses to also walk every runner's processes: slower, one walk at a time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "includeProcesses": {
                    "type": "boolean",
                    "description": "Include per-process memory footprints.",
                }
            },
        },
    },
    {
        "name": "list_actions",
        "description": "List actions available in the workspace, optionally filtered to a single project. Returns action names and which projects expose them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Omit to list actions across all projects.",
                }
            },
        },
    },
    {
        "name": "get_project_raw_config",
        "description": "Return the resolved (post-preset-merge) configuration for a project. Use this to understand what actions and handlers are configured.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": _PROJECT_ARG_DESCRIPTION}
            },
            "required": ["project"],
        },
    },
    {
        "name": "dump_config",
        "description": "Return the fully resolved project configuration with all presets applied and the presets key removed. Use this to understand the complete effective configuration a project runs with.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": _PROJECT_ARG_DESCRIPTION},
                "format": {
                    "type": "boolean",
                    "description": "Format the dump with the project's formatter (default true).",
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "reload_action",
        "description": (
            "Make a running FineCode workspace pick up an edit to an action or"
            " its handlers, without restarting anything."
            " Covers: edits to the packages that own the action class and its"
            " handler classes — the extension runner re-imports exactly those"
            " top-level packages, in every environment of every target project."
            " Does not cover: an edit to any other package (a shared library, a"
            " module those packages import) — use restart_runner, which replaces"
            " the whole interpreter; or an edit to pyproject.toml or a preset,"
            " which no restart picks up either, because a runner re-reads code"
            " and not configuration — use reload_config for that."
            " Nothing is detected automatically: pick the rung that matches what"
            " you edited. This is the cheapest one, so prefer it when it applies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Name of the action to reload, as returned by the list_actions tool.",
                },
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Omit to reload the action in every project that exposes it.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "restart_runner",
        "description": (
            "Replace the extension runner processes of a project, so a running"
            " FineCode workspace picks up an edit anywhere in the code they"
            " imported."
            " Covers: edits to any Python module a runner has loaded, including"
            " shared libraries no single action owns, and runners that are stuck,"
            " crashed or failed. Refused while an action is running in the target"
            " project — the refusal names what is running, and killInFlightRuns"
            " proceeds anyway and kills it."
            " Does not cover: an edit to pyproject.toml or a preset — a restarted"
            " runner re-reads code, not configuration, so use reload_config for"
            " those."
            " Prefer reload_action when only an action and its handlers changed:"
            " it starts no processes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Required unless allProjects is true.",
                },
                "allProjects": {
                    "type": "boolean",
                    "description": "Restart the runners of every project in the workspace. Supply this or project, never both — the whole workspace is asked for, never defaulted into.",
                },
                "env": {
                    "type": "string",
                    "description": "Name of a single execution environment to restart (e.g. dev_no_runtime). Omit to restart every environment of each target project.",
                },
                "killInFlightRuns": {
                    "type": "boolean",
                    "description": "Proceed even though an action is running in the target, killing it. Every run in flight there dies mid-execution: its caller gets a transport failure, and for an action with side effects nobody can tell whether they completed. Only set this when the run is stuck, or when you know what is running and accept losing it — the refusal names both.",
                },
            },
        },
    },
    {
        "name": "reload_config",
        "description": (
            "Make the configuration on disk take effect for a project, so a"
            " running FineCode workspace stops using the configuration it read"
            " at startup."
            " Covers: every edit to a project's configuration — pyproject.toml,"
            " finecode.toml or a preset: a changed handler parameter, an added"
            " or removed action, a new environment, a handler moved between"
            " environments — and, because"
            " the project's runners are replaced as part of it, every code"
            " change the narrower rungs cover as well. Use it when unsure which"
            " kind of edit you made: it is the rung that is right either way."
            " Refused while an action is running in the target project, since"
            " replacing its runners would kill that run; the refusal names what is"
            " running, and killInFlightRuns proceeds anyway and kills it."
            " Does not cover: an edit to FineCode's own source, which runs in the"
            " workspace server process rather than in a runner — use restart_wm"
            " for that."
            " Costs process startups proportional to the target, so prefer"
            " restart_runner or reload_action when you know the edit was code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": f"{_PROJECT_ARG_DESCRIPTION} Required unless allProjects is true.",
                },
                "allProjects": {
                    "type": "boolean",
                    "description": "Recover every project in the workspace. Supply this or project, never both — the whole workspace is asked for, never defaulted into. Use it after editing a shared preset.",
                },
                "rescan": {
                    "type": "boolean",
                    "description": "Walk the workspace directories again first, picking up projects created since the server started. Needed only when a new project directory appeared.",
                },
                "killInFlightRuns": {
                    "type": "boolean",
                    "description": "Proceed even though an action is running in the target, killing it. Every run in flight there dies mid-execution: its caller gets a transport failure, and for an action with side effects nobody can tell whether they completed. Only set this when the run is stuck, or when you know what is running and accept losing it — the refusal names both.",
                },
            },
        },
    },
    {
        "name": "restart_wm",
        "description": (
            "Replace the FineCode workspace server process itself, so a running"
            " session picks up an edit to FineCode's own code."
            " Covers: every change the narrower rungs cover, plus edits to the"
            " workspace server — the part that reads configuration, routes"
            " actions and owns the runners, and which no reload or runner"
            " restart re-imports because it is the process performing them."
            " Does not cover: nothing above it; this is the widest rung."
            " Every connected client is disconnected and every runner is"
            " stopped. Other clients reconnect on their own and this tool"
            " reports which ones it disturbed; runs in flight anywhere in the"
            " workspace die and are not resumed. Prefer reload_config, which"
            " leaves other projects and other clients alone."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
)
