# AGENTS.md — JARVIS Development Guidelines

## 1\. Project Instructions

Before starting any development, read and follow the existing project documentation:

- Features specification
- Development roadmap
- Technology stack recommendations
- `PROJECT_AUDIT.md`, if available

Treat these documents as the primary source of project requirements.

## 2\. Development Rules

- Follow the development roadmap sequentially.
- Implement one feature set at a time.
- Do not skip features or development phases.
- Preserve existing working functionality.
- Follow the specified lightweight architecture.
- Keep assistant RAM consumption within the 1.3 GB target.
- Avoid unnecessary dependencies and heavyweight frameworks.
- Write modular, readable and maintainable Python code.
- Test each feature before marking it complete.
- Do not claim a feature is complete unless it works.

## 3\. Git Restrictions

**STRICTLY PROHIBITED:**

- Do not create Git commits.
- Do not push to any remote repository.
- Do not amend, rebase or rewrite commits.
- Do not perform automated Git releases.

All Git commits, pushes and repository management will be performed manually by me.

You may inspect Git status and history when necessary, but never modify repository history.

## 4\. Changelog Management

Maintain a `CHANGELOG.md` in the project root.

Follow this exact versioning convention:

**Feature-set completion:**

Each successfully completed feature set increments the patch version.

Example:

- Feature set 4.1 completed → `v0.0.1`
- Feature set 4.2 completed → `v0.0.2`
- Feature set 4.3 completed → `v0.0.3`

**Phase completion:**

After all feature sets belonging to a phase are completed and verified, increment the middle version number and reset the patch number.

Example:

- Phase 1 completed → `v0.1.0`
- Phase 2 completed → `v0.2.0`
- Phase 3 completed → `v0.3.0`

Continue this convention throughout development.

For every completed feature set, record:

- Version number.
- Feature set number and name.
- Implemented functionality.
- Important files created or modified.
- Relevant bug fixes.
- Testing status.

Do not increment versions for incomplete or unverified features.

Maintain the changelog chronologically, with the latest release at the top.

## 5\. README.md Management

Maintain `README.md` as the primary project overview.

Keep it updated whenever relevant changes occur, including:

- Project description.
- Current capabilities.
- Technology stack.
- Installation instructions.
- Configuration instructions.
- How to run JARVIS.
- Required dependencies.
- Current development status.
- Known limitations.

Do not let README instructions become outdated.

## 6\. .gitignore Management

Maintain a proper `.gitignore` file in the project root.

- Ignore Python cache files (`__pycache__/`, `*.pyc`).
- Ignore virtual environments (`venv/`, `.venv/`).
- Ignore environment variables and secrets (`.env`, API keys, credentials).
- Ignore temporary files, logs and cache directories.
- Ignore generated databases and temporary user data where appropriate.
- Ignore downloaded model files and large binary assets.
- Ignore IDE-specific and operating-system-generated files.
- Never ignore essential source code, configuration templates or documentation.
- Never modify existing ignore rules without checking their purpose.

Keep `.gitignore` updated whenever new dependencies, generated files or project directories are introduced.

**Important:** Do not delete or modify existing user data simply because it is ignored by Git.

## 7\. Completion Protocol

After completing each feature set:

1. Test the implementation.
2. Verify that existing features still work.
3. Measure memory usage where relevant.
4. Update `CHANGELOG.md`.
5. Update `README.md` if necessary.
6. Report completed work and modified files.
7. Identify remaining tasks and known issues.
8. Wait for my approval before proceeding to the next feature set.

## 8\. Most Important Principle

**Build incrementally, preserve working code, prioritize reliability and efficiency, and never make autonomous Git commits or pushes.**