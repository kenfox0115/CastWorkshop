# Security

Report security issues privately rather than opening a public issue. Private vulnerability reporting is enabled. Use the repository's **Security → Report a
vulnerability** button or [private advisory form](https://github.com/kenfox0115/CastWorkshop/security/advisories/new).
If the button is unavailable, use a private contact method explicitly listed by the owner on
their GitHub profile; do not post exploit details publicly to request contact.
Include the affected revision, reproduction steps, and impact without live tokens,
private generated media, or databases. No response-time commitment is currently published.

Secrets belong only in `.env`, which is ignored by Git. Rotate a Telegram token
immediately if it appears in a log, screenshot, commit, or shared archive.

CastWorkshop defaults to `127.0.0.1` (this computer only). Setup offers an explicit
LAN/trusted-network choice, saved as `API_HOST=0.0.0.0`. Runtime honors `API_HOST`.
LAN mode has no built-in login or HTTPS; anyone who can reach the server may access
the interface and generated content. Use it only on a trusted home/LAN/Tailscale
network and restrict inbound access with your operating-system firewall. Public deployment requires external authentication
and transport security; do not expose port 8787 directly to the public internet. Ollama and
ComfyUI can remain bound to localhost. Back up `vps/jobs.db` and generated media
according to your own privacy needs.

The API can modify characters, settings and service endpoints, configure Telegram,
queue GPU work, and retrieve stored media. LAN access grants these capabilities to
other reachable clients; it is not a multi-user permission system. Local-first
does not mean every operation stays offline: setup downloads dependencies and models,
and optional Telegram transport sends content through Telegram.
