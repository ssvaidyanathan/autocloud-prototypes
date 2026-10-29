# Agent Skills Repository

This directory contains domain knowledge, diagnostic playbooks, and troubleshooting guides structured according to the open Agent Skills format.

## Directory Structure

```text
skills/
├── README.md                   # This documentation
├── skills.yaml                 # Mapping from agents/specialists to assigned skills
└── <skill-name>/               # Kebab-case directory matching frontmatter name
    ├── SKILL.md                # Main skill file with YAML frontmatter and instructions
    └── references/             # Optional detailed reference documents (*.md)
        └── <reference>.md      # Deep-dive guides, descriptors, and cookbooks
```

## Creating a New Skill

1. Create a directory using a kebab-case name (lowercase alphanumeric and hyphens, max 64 characters):
   ```bash
   mkdir agent/skills/service-payments-api
   ```
2. Create `SKILL.md` with required YAML frontmatter:
   ```markdown
   ---
   name: service-payments-api
   description: Architecture, endpoints, SLOs, and failure modes for Payments API.
   ---

   # Payments API Runbook
   ...
   ```
3. (Optional) Add detailed reference markdown documents in `references/`:
   ```bash
   mkdir agent/skills/service-payments-api/references
   touch agent/skills/service-payments-api/references/error_codes.md
   ```
4. Register the skill in `skills.yaml` under the appropriate specialist, `dispatcher`, `synthesizer`, or `all`.

## Security & Architectural Constraints

- **No Executables**: `scripts/` directories are strictly prohibited and will be rejected at load time and during CI.
- **Read-Only**: Skills provide contextual guidance and runbook instructions only; they cannot declare tools or perform state mutations.
- **Progressive Disclosure**: SkillsLoader loads only names and descriptions initially. Full instruction bodies and references are loaded on demand.
