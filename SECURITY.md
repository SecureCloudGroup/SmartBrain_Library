# Security

Please report vulnerabilities privately through GitHub's **Report a vulnerability** (Security tab) on this repository. Do not open a public issue.

What this repo can affect: SmartBrain installs fetch data from the sources listed here, so a malicious record is the main risk. The defences are:
- only the maintainer merges changes;
- CI schema checks: https only, public hosts, no credentials, no undeclared parameters;
- the app runs the same validator, fetches only through its SSRF-guarded network layer, and treats every response as untrusted data;
- packs are signed and pinned on first use, and older packs are refused (no rollback).
