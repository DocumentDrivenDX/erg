# Privacy and local data

## Defaults

Installing erg starts no collector, reporter, proxy or export service. Default
collection writes usage counts, prompt sizes, session identifiers, local paths,
repository/work metadata, machine metrics and available account metadata to local
SQLite. It does not retain prompt/response or tool bodies without history enrollment.
Paths, repository names, titles, email/account identifiers and usage are sensitive
metadata even when no bodies are retained. Erg has no automatic telemetry upload.

The ledger defaults to `~/.local/share/erg/erg-v2.db`. `ERG_HOME` or `ERG_DATABASE`
select another ledger location; `ERG_STATE_DIR` selects collector state. State
includes discovery/checkpoints, machine identity, configuration, logs and managed
runtime copies. `~/.config/erg/` may contain user configuration. Protect these paths
and backups as private data. Do not commit ledgers, auth files or real transcripts.

## Network and existing authentication

Local storage and reporting work offline. Collection can perform read-only account
and repository enrichment when the relevant tools/logins are available:

- The Codex account probe invokes the installed `codex app-server` for current
  identity and rate limits. The separate billing reader supports an existing
  file-backed ChatGPT login in the discovered Codex home. It reads `auth.json`,
  holds its bearer token in memory and sends allowlisted HTTPS GET requests to
  `chatgpt.com`. It does not log in, refresh tokens, buy credits, consume resets
  or persist tokens/raw billing responses. Selected account/plan/quota/reset
  metadata is retained. Unsupported/denied responses remain unavailable.
- PR/commit enrichment can use the existing GitHub CLI login for metadata reads.
- Explicit SSH hosts, gateway reads, remote rate catalogs, Databricks reporting
  and exports use configured destinations and existing authentication. A local
  ledger is not automatically enrolled in central delivery.

These enrichment requests are network activity, even though they do not upload the
session corpus. An offline machine may show unavailable enrichment. For an isolated
trial use a separate OS account/home with no agent credentials or cloud configuration.

## Optional body retention and export

`erg history enroll` opts the selected state/ledger into forward-only retention of
new native session records. Depending on source, those records include prompts,
responses, tool arguments/results and files discussed by agents. Existing history
is baseline, not silently imported by enrollment. Explicit retained repair/import
commands are separate actions. Direct inference capture/proxying requires its own
explicit setup.

Credential fields in transport/metadata envelopes are excluded. This is not a
redaction guarantee for arbitrary content: secrets typed into a prompt, returned
by a tool or embedded in a file can remain in enrolled bodies. Do not enable
content capture for sessions whose content you cannot retain.

Central export requires explicit configuration and enrollment. It may transmit
eligible retained content and metadata to the configured destination. Treat it as
experimental in this candidate. Review destination access, retention and boundaries
before enabling it; local expiration does not delete remote copies.

## Reporter access

`erg serve` binds to `127.0.0.1` by default. The local dashboard has no authentication
and can expose captured history. Do not bind it to a shared network interface or
publish it through a tunnel without adding appropriate access controls.

## Retention, stopping and removal

There is no default automatic expiry for retained content or metadata.
`erg history expire --before EPOCH` previews eligible body expiry; add `--apply`
to expire bodies. Pending projection/delivery can protect records. Identity
`tombstones`, usage and other metadata remain; this is not secure filesystem/WAL
erasure. Source agents and backups have their own retention policies.

To stop foreground collection/reporting, use Ctrl-C. On macOS, `erg service stop`
and `erg reporter stop` stop the managed jobs. Installed LaunchAgents can restart
at login: after stopping, remove their `dev.ddx.erg.collector.plist` and
`dev.ddx.erg.reporter.plist` entries from `~/Library/LaunchAgents` to disable them.
If you explicitly enabled other sampler/delivery/backend jobs, stop and remove
those as well. Remove erg's extension paths from Pi settings and its Claude hook
entries, preserving unrelated settings and consulting installer backups. Remove
any cron/systemd entries you added.

Uninstall the Python package with `python -m pip uninstall erg` in its environment.
Uninstalling does not delete state or managed runtime copies. After stopping jobs,
you can separately remove the selected ledger/state/config directories and their
backups if you no longer need the data. Preserve other applications' credentials
and native histories. No removal command is run automatically.
