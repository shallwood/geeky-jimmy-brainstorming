# Contributing

Thanks for helping improve Geeky Jimmy’s Brainstorming Tool.

- Keep the app focused on capturing and organizing ideas.
- Preserve private-by-default deployment and the loopback-only Docker binding.
- Do not add notes, database files, tokens, secrets, or personal Tailscale details to commits.
- Keep note depth validation in both the UI and API. The server remains authoritative.
- Add or update standard-library tests for API and data-model changes.
- Run `python3 -m unittest discover -s tests -v` before opening a pull request.
- Discuss features outside Stage 1.1 before implementing them. AI integrations, read-aloud, automated backup, and native clients are explicitly deferred.

The maintainer reviews and approves changes before merge. A repository licence must be selected before public release.
