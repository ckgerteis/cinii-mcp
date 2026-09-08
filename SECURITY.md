# Security policy

## Supported versions

The latest release on the [releases page](https://github.com/ckgerteis/cinii-mcp/releases) is
supported. Fixes are released as a new version; earlier versions are not patched in place, because
each version is a citable artefact with its own DOI.

## What counts

This is a stdio server that Claude Desktop (or another MCP client) starts on your own machine. It
speaks to one public scholarly API over HTTPS, reads credentials from environment variables that the
client passes it, and can write an append-only receipts ledger to a folder you name. Reports of the
following are security reports and are wanted:

- A credential or a part of one appearing anywhere other than the request header it belongs in: in
  an envelope, a receipt, a log line, an error message, a test fixture.
- A receipt that can be altered without `verify` noticing, or a chain that verifies when it should
  not.
- A path, a name or a value from the author's or a contributor's machine written into the
  repository, the bundle or the installer.
- The installer writing outside the folders it was told about, or touching a `claude_desktop_config.json`
  entry it was not asked about.
- A dependency with a published vulnerability that this server's use of it reaches.

Rate limiting by the upstream API, and the upstream API's own faults, are not security reports;
open an ordinary issue.

## Reporting

Use GitHub's private vulnerability reporting on this repository (Security → Report a vulnerability),
or email christopher.gerteis@gmail.com with "cinii-mcp security" in the subject. Say which version, how to reproduce, and
what you saw. Please do not open a public issue for something that exposes a credential until a fix
is released.

You will get an acknowledgement within a week. A confirmed report is fixed in the next release and
credited in `CHANGELOG.md` unless you ask otherwise.
