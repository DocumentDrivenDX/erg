# Codex billing data sources

Inspected 2026-10-27: installed macOS app `com.openai.codex`, version
26.1013.40934 (20770), `/Applications/ChatGPT.app`.
This is static code evidence, not a captured invoice or an API stability promise.

## UI reads

The bundled `webview/assets/page-d40b312a477a.js` function `Qs` renders the
monthly price from `monthlyAmounts[pricePlan]`, currency and minor-unit exponent.
The imported pricing hook in `app-initial-90eb42395b7d.js` joins the current
account entitlement to `GET /checkout_pricing_config/configs/{country_code}`.
It uses `entitlement.billing_currency`, `has_active_subscription` and
`last_active_subscription.purchase_origin_platform`. No numeric SKU-to-price
mapping should be hardcoded in erg. A displayed price is not an invoice.

Account entitlement reads use `GET /accounts/check/v4-2023-04-27`; the desktop
access-input bridge also has `GET /wham/accounts/check`. Consumer billing has
`GET /subscriptions?account_id=...`.

Current limits and credit balance use `/wham/usage`; banked reset inventory uses
`GET /wham/rate-limit-reset-credits`; paginated reset history uses
`GET /wham/rate-limit-reset-credits/history`. The UI explicitly exposes reset
history separately from current reset inventory. Inventory changes alone do not
prove a purchase or its price. Never invoke reset consumption or billing mutation
endpoints.

These paths are routed through the authenticated application request client.
The existing native app-server `account/read` and `account/rateLimits/read`
expose coarse plan, limits, credit balance and available reset count, but not
monthly SKU pricing or complete billing/reset history. The installed CodexBar
0.78 CLI adapter also returned the coarse plan/limits/credit balance. Its web
adapter failed during the read-only check; no response bodies or credentials
were retained.

## Integration status

The operator explicitly authorized existing native-login credential use on
2026-10-27. The deployed file-backed adapter keeps credentials in memory and uses
only bounded GET requests to the fixed official host. It never logs or stores
credentials, raw responses or arbitrary HTTP error bodies; redirects and billing
mutations are disabled. It polls at most once per fifteen minutes per credential
version. Keyring-only stores currently remain unsupported.

Live capture succeeded for usage, reset inventory and reset history. The account
plan is `promax`. Five provider history events were observed: three uses and two
grants, with zero currently available reset credits. Pricing configuration,
entitlement detail and subscription requests returned HTML HTTP 403 responses
from Cloudflare using the native login, both Python and system HTTPS transports.
The adapter preserves these fixed endpoint statuses and the dashboard displays
the access failure. No cause beyond the observed 403 is established.

The earlier $500 screenshot observation remains labeled as such. It is not an
automatic price read, paid invoice or reset purchase. Price and paid-charge
capture are not verified end to end; invoice normalization cannot be qualified
until an actual authorized response is available. Successful subscription reads
currently record transport availability only, never unverified payment amounts.

Session route payer attribution is implemented independently: exact request
principals override owner-only evidence; configuration snapshots cannot establish
historical payers; conflicting principals remain unknown; endpoint changes
cannot inherit the old endpoint's payer. Explicit operator bindings retain
precedence. Usage projection definition version 3 requires republishing existing
rollups before the changed policy appears in cached reporting. Account metadata
for forward-scoped route principals is eligible for the existing approved export.
The collector, uploader and reporter were deployed. All five reset event IDs
and exact revisions were verified in `erg.telemetry.current_records`, with one
row per ID. Local and Databricks Accounts reports both show five events, three
uses and two grants. The original native forward boundary remains unchanged;
only newly observed approved billing metadata was added.
