# erg — agent productivity monitor

Part of the [DocumentDrivenDX](https://github.com/DocumentDrivenDX) family. Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.

Tokens, output and machine load across Claude Code and OpenAI Codex, by hour/day, project, model, agent and machine.
Python 3.8+, stdlib only. Install with `pip install .` (gives an `erg` command) or run `bin/erg` from a checkout. Everything stays local (SQLite at `~/.local/share/erg/erg.db`).

    erg sync --full        # first run: scrape this machine (~1 min for months of logs)
    erg sync               # incremental, local + every configured host
    erg serve --sync-every 15   # dashboard at http://127.0.0.1:8787, re-sync every 15 min

## Remote machines (nothing to install)
    erg add-host gpu-box you@gpu-box [--roots ~/code]
    erg sync --host gpu-box
The collector is streamed over SSH (`ssh host python3 - collect < collect.py`); rows are tagged with the host name.

## Performance counters
Automatic where present: **iStat Menus 7** history and the a generic **endpoint-metrics CSV** (see `docs/adapters.md`) are read on every sync (iStat's fine-grained tiers expire, so keep `serve --sync-every` running to archive them).
Otherwise (Linux/remote hosts, keyboard-active time) run the built-in sampler:
History cannot be reconstructed, so run the sampler (30 s cadence):

    erg sampler                  # writes a launchd agent / systemd unit and prints the activation command
    erg sampler --host gpu-box   # same on a remote host
Samples land in `~/.local/share/erg/perf.ndjson` on each machine and are pulled by `sync`.

## What is measured
- **Tokens**: fresh input, cache read, cache write, output (reasoning shown separately) per API call, de-duplicated across streamed/forked sessions. Codex `cached_input` is subtracted from input.
- **Output**: git commits (author-date, by your emails; code vs doc lines; lockfiles/data/generated excluded; agent via Co-Authored-By) plus agent Edit/Write/apply_patch lines.
- **Your input**: prompts, chars, words, est. tokens (chars/4) and keyboard-active time (macOS HID idle). Prompt text is never stored. Raw keystroke counting needs OS input-monitoring permission and is not implemented.

Specs: `docs/helix/` (PRD, architecture).

## Databricks AI Gateway
    erg add-gateway --profile my-profile     # warehouse auto-discovered; identity from the profile
`sync` then pulls `system.ai_gateway.usage` through the Databricks CLI's existing login (no credentials stored; if the login expired it prints `databricks auth login -p …`). Rows land as tool `gateway` with the requester kept, so the dashboard's **Whose** filter shows just you, everyone, or one requester. Gateway calls that exactly match a local Claude/Codex call (same output + cache-read tokens within 2 min) are marked duplicates and dropped from totals. The *Sources & coverage* card shows each source per day.

## Privacy
Everything is local. Prompt text is never stored (only length). The Databricks adapter stores other requesters' identifiers from the gateway table you can already read; delete `machine='databricks'` rows to purge them.

## Development
    python -m unittest discover -s tests

## License
Licensed under either of [Apache License 2.0](LICENSE-APACHE) or [MIT license](LICENSE-MIT) at your option.
Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in this project, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or conditions.
