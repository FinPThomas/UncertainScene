# 0001 GitHub is the only persistent store
Date: 2026-10-09   Path: infra   Status: accepted

Context: compute is Colab, possibly across several Google accounts, and sessions end without
warning. Google Drive is per account and its FUSE mount dropped mid-run in a previous project.

Decision: code, run state and small results live in git; big files live in GitHub release
assets; Colab local disk is scratch only. Every step pushes its results and state when it ends.

Why: any account with the token can resume any run; nothing is lost when a session dies except
the step in progress.

Consequences: files over 5 MB never go in git; steps must declare their resource needs so a
session does not start work it cannot finish.
