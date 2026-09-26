# Canvas Tracker Agent Rules

## Git Releases and Tagging Policy
1. **Tags are strictly immutable**: Never move or force-push a git tag once created and pushed to the remote repository.
2. **Pre-tag validation**: Before creating or pushing any version tag (e.g. `v0.1.1`):
   - Run and pass all tests locally (`python -m unittest discover -v`).
   - Validate portable launcher scripts and dependencies.
   - Verify GitHub Actions workflow runner compatibility (e.g., ensure no deprecated runner images).
3. **Handling post-tag failures**: If an issue or bug is discovered after a release tag has been pushed, do NOT re-tag. Fix the issue on a new commit and bump the version to a new tag (e.g., `v0.1.1`).
