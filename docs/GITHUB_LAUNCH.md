# GitHub public-launch checklist

These are recommendations and owner actions, not settings already applied.

## Discovery

- Description: **Create persistent AI characters with their own personalities,
  voices, and appearances. Generate conversations, images, and videos locally.**
- Suggested topics: `generative-ai`, `local-ai`, `ai-characters`, `character-creation`,
  `comfyui`, `ollama`, `image-generation`, `video-generation`, `text-to-speech`,
  `ai-video`, `python`, `open-source`.
- No project website has been verified; leave Website blank unless the owner supplies one.
- Add an owner-approved social preview and README screenshot/demo using non-private media.
- Topics help discovery; Git version tags identify specific revisions. They serve different purposes.

## Community and security settings

- Enable Issues and check the bug/feature templates. Decide whether Discussions is
  useful; enabling it is optional and requires owner approval.
- Enable private vulnerability reporting when the repository becomes public and
  verify the Security reporting button before announcing the beta. If a private
  reporting channel is needed beforehand, the owner must supply a real contact method.
- Review commit author contact metadata, tracked history, and any Actions logs before visibility changes.
- Enable applicable dependency/security alerts and review dependency update proposals.
  Prefer a small Dependabot configuration for Python requirements and GitHub Actions.
- After public launch, protect `main`: require pull requests and passing offline CI,
  and prevent force pushes. Choose review requirements suitable for the maintainer count.
- Run the new Actions workflow and inspect both Python jobs; no passing CI result is assumed.

## First release

- Finish [release acceptance](RELEASE_CHECKLIST.md), especially fresh setup, live
  media quality, and stop/recovery. Record hardware, OS, dependency versions and results.
- Review the [draft release notes](PUBLIC_BETA_RELEASE.md) and update their validation section.
- With owner approval, tag the accepted revision `v0.9.0-beta.1` and create
  **CastWorkshop 0.9 Public Beta**, marked as a pre-release.
- Build the source ZIP from that exact revision, verify its manifest/checksum and
  exclusions, then attach the ZIP and checksum. Models and private state must stay excluded.
- Treat public visibility and beta publication as separate approvals. After public
  visibility, preserve normal update history; do not keep amending the initial commit.

See GitHub's [topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics),
[releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository),
and [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository)
documentation for current procedures.
