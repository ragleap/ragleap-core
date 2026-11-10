# Single sign-on (OpenID Connect)

People can sign in to the AI Office with the account they already use at your identity provider (for example Keycloak,
Entra ID or Okta), including the second factor that provider enforces. It is **off by default** and needs a provider that
supports OpenID Connect. A provider that runs inside your own network works with no internet.

**Status:** written and tested against a fake provider only. We have not yet tried a real Keycloak, Entra ID or Okta. Please
tell us what you find. Menu names at your provider differ, so use its own documentation for the steps there.

## What you need

- An OpenID Connect client at your provider of the "web application with a client secret" kind, using the authorization
  code flow. The redirect address to register is `https://<your RagLeap address>/auth/callback`.
- RagLeap must be served over **https** (put a reverse proxy in front). The sign-in sends a session token back to the
  browser, so it must not travel in clear text. Plain http is accepted only for `localhost` or when you set
  `OIDC_ALLOW_INSECURE_HTTP=true` (for tests).
- The RagLeap server must be able to reach the provider (its discovery, token and key addresses).
- Apply the single sign-on block of `db/schema.sql` to an existing database before you turn it on (back up first; the
  block only adds tables and is safe to repeat). New installs have it already.

## Settings (in `.env`)

SSO is on only when the first four are all set. Setting only some of them is reported in the server log.

| Setting | Meaning | Default |
|---|---|---|
| `OIDC_ISSUER` | The provider's issuer address. It must match the `iss` the provider puts in tokens exactly. | |
| `OIDC_CLIENT_ID` | The client's id at the provider | |
| `OIDC_CLIENT_SECRET` | The client's secret at the provider | |
| `OIDC_REDIRECT_URL` | `https://<your RagLeap address>/auth/callback` | |
| `OIDC_SCOPES` | Scopes to ask for (`openid` is always added) | `openid email profile` |
| `OIDC_REQUIRE_MFA` | Refuse sign-ins that the provider does not report as multi-factor | off |
| `OIDC_MFA_ACR_VALUES` | Comma separated `acr` values that count as multi-factor, for providers that report it that way | |
| `OIDC_EMAIL_CLAIM` | Which claim holds the email address | `email` |
| `OIDC_REQUIRE_EMAIL_VERIFIED` | Require the provider to say the email is verified. Set to `false` only for a provider that does not send `email_verified`, and only if you trust its directory. | on |
| `OIDC_ALLOW_INSECURE_HTTP` | Allow plain http to the provider (tests only) | off |
| `SSO_SESSION_HOURS` | A session ends after this many hours (1 to 168) | 8 |
| `SSO_IDLE_MINUTES` | A session ends after this many minutes without use (5 to 1440) | 30 |

## Who may sign in

Only people you have already added. Single sign-on never creates accounts.

1. Add the person in the Users tab (or `POST /users`) and give them a role.
2. Register the email they use at the provider: the Users tab shows a "Register email" field for each person when SSO is
   on, or call `POST /users/{id}/identities` with `{"email": "..."}` as the owner.
3. The person opens the AI Office and chooses "Sign in with SSO". On the first sign-in RagLeap remembers the provider's own
   account id for that person. After that the account id counts, not the email, so a changed or reused email address at
   the provider cannot take the account over.

A person who is not registered, or is disabled, gets the same message either way.

## Sessions

- A session is a random token that works in the same `x-api-key` header as a personal key, with the same roles. RagLeap
  stores only a hash of it.
- It ends at the time limit, after the idle limit, when the person signs out, when the owner ends it ("End sessions" in the
  Users tab, or `DELETE /users/{id}/sessions`), and **on the very next request after the person is disabled**.
- Every sign-in and every refused sign-in is written to the audit record (Audit tab).

## Second factor

The provider performs the second factor. With `OIDC_REQUIRE_MFA=true` RagLeap refuses a sign-in unless the provider's token
shows one: `amr` contains `mfa`, or two different methods (for example `pwd` and `otp`), or `acr` is one of the values in
`OIDC_MFA_ACR_VALUES`. Make sure your provider actually enforces and reports the second factor, then test it with a
person who has none.

## Keep a way in

Keep at least one owner **personal key**, stored safely, in case the provider is down or misconfigured. If you also set
`SHARED_KEY_DISABLED=true`, remember that the lock-out guard counts personal keys only.

## Trying it first

Set up a throwaway provider (for example Keycloak in development mode, following its documentation) with one client and
two test users, one with a second factor and one without. Then:

1. Set the four settings and `OIDC_ALLOW_INSECURE_HTTP=true` if you use plain http on localhost.
2. Register a test user's email in the Users tab and sign in with SSO.
3. Check the Audit tab, end the session from the Users tab, and confirm the next request is refused.
4. Turn on `OIDC_REQUIRE_MFA=true` and check that the user without a second factor is refused.

## Rate limit on the sign-in addresses

`/auth/login` and `/auth/callback` are public, so each client address may use each of them at most
`AUTH_SSO_MAX_PER_MINUTE` times per minute (default 30, range 1 to 10000). Over the limit, `/auth/login` answers 429 with a
`Retry-After` header and `/auth/callback` sends the person back to the dashboard with a short message. Refused requests
are not counted, so waiting is enough. The first refusal per client and minute writes one `sso_rate_limited` line
(`docs/audit-log-lines.md`). The state is in memory, per process, and `AUTH_THROTTLE=off` switches it off together
with the API key throttle. Behind a reverse proxy, list the proxy in `TRUSTED_PROXIES`; without it every person behind the
proxy shares one counter. The number of sign-ins in progress is also capped at 1000.

## Not covered yet

SAML, LDAP, group-to-role mapping and automatic account creation.
