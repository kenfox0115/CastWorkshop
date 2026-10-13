# Security

Report security issues privately to the repository owner rather than opening a public
issue. Include reproduction steps and the affected revision without including live
Telegram tokens, generated private media, or user databases.

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
