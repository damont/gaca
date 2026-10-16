# Security policy

GACA is a server that holds your chats and alerts and sits next to an agent that can run commands, so security problems here
matter. Thank you for reporting them privately.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting: on this repository, open the **Security** tab and choose **Report a vulnerability**. Include
what you found, how to reproduce it, and what you think the impact is. You will get a reply within a few days, and I will tell
you when it is fixed and credit you if you like.

The same applies to the [Android](https://github.com/damont/gaca-android) and [iPhone](https://github.com/damont/gaca-ios) apps.

## What is supported

Only the latest `main`. GACA has no numbered releases yet, so update with `git pull && ./install.sh`.

## What to know about the design

The README's [Security model](README.md#security-model) section describes what protects an install: the server listens only on
loopback, the tunnel is the only way in, pairing codes are single-use and short-lived, phones and Hermes hold individually
revocable credentials, and Hermes' token is scoped. Reports that show one of those claims is wrong are exactly what this
policy is for.

Things that are by design, and not vulnerabilities: someone with shell access to the machine can read its files, and a paired
phone has full access to its owner's data, so revoke phones you no longer trust (`gaca-api device revoke`).
