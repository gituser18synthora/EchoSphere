# User instruction: never change live code

The user established this standing rule on 2026-09-21 for all future work:
**Never modify live/production code. Make code changes only in the local
workspace.**

- Do not edit, copy, sync, deploy, or otherwise replace code on the live server.
- Do not restart live services to activate local code changes.
- A request to investigate a live issue permits read-only investigation, not
  live code changes. Fix and test the code locally.
- Earlier authorization to deploy the MDND English fix was specific to that
  completed task and does not authorize future live changes.
- Keep this restriction in force unless the user explicitly revises it.

# Production server: identity and approval before changes

The user established these rules on 2026-11-13:

- Treat `aurexion-voicebot` (`root@192.168.60.187`, SSH port `2232`) as
  the production VoiceBot server. It is separate from `echosphere-live`
  (`192.168.60.123`, SSH port `2232`).
- The existing local SSH alias uses `~/.ssh/echosphere_production`.
  Key-based login was verified with read-only commands on 2026-11-13.
- Read-only investigation is permitted. Before any production change,
  describe the exact proposed action and obtain the user's explicit approval
  for that action. This includes configuration, SSH keys/access, files,
  permissions, packages, service operations, and database writes or migrations.
- Access to the server, earlier approvals, and rollout documents or runbooks
  do not authorize future production changes. Verify the target server before
  any approved action.
- The standing prohibition on live code changes above remains in force.
  Make and test code changes only in the local workspace; approval for a
  configuration or database change does not revise that prohibition.

# User preference: language for prompts and instructions

The user established this standing preference on 2026-11-09:

- Always write prompts and instructions provided to the user in English,
  including reusable prompts, project instructions, and step-by-step directions.
- Explanations and general conversation must use Roman Hinglish, not
  Devanagari Hindi, following the user's preference established on 2026-11-10.
- Apply this preference in future conversations for this workspace without
  asking the user to repeat it, unless the user explicitly changes it.
