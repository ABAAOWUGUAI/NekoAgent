# Security policy

Do not report vulnerabilities, credentials, private chat content, media,
database files or server details in a public issue. Use this repository's
GitHub private vulnerability reporting feature after the maintainer enables it
in **Settings → Code security and analysis**. If it is not available, do not
post sensitive details publicly; open a minimal issue asking the maintainer for
a private reporting channel.

The `main` branch is the supported development version until the first tagged
release. Security fixes should include a minimal reproducer with synthetic
data, an impact description and a regression test where practical.

The project is self-hosted. Operators are responsible for protecting their own
model keys, channel credentials, databases, artifact storage and reverse proxy.
Never expose the Admin API directly to the public Internet during bootstrap.
