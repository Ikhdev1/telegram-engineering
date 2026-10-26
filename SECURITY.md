# Security policy

This repository contains an AI skill, offline tooling and a reference bot. It is not a hosted Telegram service and does not need your production credentials to investigate a problem.

## Reporting

Do not put a working exploit against someone else's bot, real token, session string, personal conversation or private database in a public issue.

If the repository's **Security → Report a vulnerability** option is available, use that private channel for sensitive reports. Otherwise, use a private contact listed on the owner's GitHub profile; a public issue should contain only a non-sensitive description and a request for a private reporting channel. Private reporting availability is not assumed.

A useful report states the affected version/file, the trust boundary crossed, a minimal no-network reproduction and the observed effect. Describe local payload validation, dispatcher/DB behavior and live Telegram observations separately.

## If a credential was exposed

Revoke or rotate it with its provider first. Removing it from a later commit does not remove earlier history or revoke access. Do not paste a replacement token into an issue or pull request.

## Scope and limitations

- The validator is a bounded API-snapshot check, not an authoritative Telegram parser or a general secret scanner.
- Hidden or disabled buttons do not enforce permissions. The server must check identity, role, object scope and current state.
- The gallery is illustrative client-side state, not authentication or a Telegram client.
- Offline tests do not prove production infrastructure, payment-provider behavior or live client rendering.

Version-specific fixes are documented in [CHANGELOG.md](CHANGELOG.md). No response-time or support-period guarantee is implied.
