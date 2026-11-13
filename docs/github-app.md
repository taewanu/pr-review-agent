# Register the GitHub App

This page covers registering the GitHub App the daemon posts as, and why it needs each permission.

The daemon posts as the App, under the name `<app-name>[bot]`. You register the App on github.com.

## Steps

1. Open [github.com/settings/apps/new](https://github.com/settings/apps/new). Pick any name. GitHub requires a homepage URL but the daemon does not use it. Your fork's URL works.
2. Uncheck **Webhook → Active**. The daemon polls, so it needs no public URL.
3. Under **Repository permissions**, grant these five. Leave everything else at *No access*.

   | Permission | Access | Used for |
   |---|---|---|
   | Checks | Read and write | the `review` check |
   | Contents | Read and write | resolving review threads ([why](#why-contents-needs-write-access)) |
   | Issues | Read and write | the status comment |
   | Pull requests | Read and write | the review and its findings |
   | Metadata | Read-only | required by GitHub |

4. Choose **Only on this account** and create the App.
5. Copy the **App ID** from the App's **General** page into `GITHUB_APP_ID` in `.env`.
6. On the same page, click **Generate a private key**. Move the downloaded file to where the daemon reads it:

   ```bash
   mkdir -p ~/.pr-review-agent
   mv ~/Downloads/*.private-key.pem ~/.pr-review-agent/app.pem
   chmod 600 ~/.pr-review-agent/app.pem
   ```

   GitHub offers each key for download once. If you lose it, generate a new one on the same page.

7. Open **Install App**, choose **Only select repositories**, and pick the repositories to review. The daemon finds the installation itself, so you copy no installation ID.

## Installing on more repositories

A repository you install the App on later is reviewed from the next polling cycle.

At startup, the daemon logs `App not installed on: <repo>` once, as an error. Each polling cycle then skips that repository with `skipped (App not installed): <repo>`.

## Changing permissions

After you change an installed App's permissions, approve the change at [github.com/settings/installations](https://github.com/settings/installations). Until you approve it, the App's tokens carry the old permissions.

## Why Contents needs write access

Contents: write is needed only to resolve review threads. GitHub's `resolveReviewThread` mutation rejects an App that has only Pull requests: write, with `Resource not accessible by integration`. GitHub's permission docs do not list this ([community discussion 44650](https://github.com/orgs/community/discussions/44650)). With Contents: read, reviews post but threads never resolve.

The permission allows writing to the repository's contents. The daemon writes none: no commits, branches, or file edits. The review agent never sees the App's token ([Security](../README.md#security), [ADR 0036](adr/0036-github-app-identity.md) decision 5).
