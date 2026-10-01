# Private dashboard Worker

Serves the private dashboard at **me.aadityad.dev**, starts the data repo's `act.yml`
when you tap a button, and sends the 8am email. No npm dependencies.

```
phone -> Cloudflare Access (your email only) -> Worker -> GitHub (data repo)
                                                  |-> dispatches act.yml (the only thing that writes)
                                                  `-> Resend (daily email, from a cron trigger)
```

## Setup, in order

You need a Cloudflare account with `aadityad.dev` on it, and Node 18 or newer.

### 1. Create the Cloudflare Access app (the login in front of the page)

1. Cloudflare dashboard, **Zero Trust** (left sidebar). The first time, pick a team name; note the resulting domain, `<team>.cloudflareaccess.com`.
2. **Access controls**, **Applications**, **Add an application**, **Self-hosted**.
3. Application name `OSS Scout`. Add a public hostname: subdomain `me`, domain `aadityad.dev`, no path. Session duration `24 hours` is fine.
4. **Policies**: add a policy named `Owner only`, action **Allow**, rule **Emails** equal to `aaditya.d.desai@gmail.com`. Nothing else, no "Everyone".
5. **Login methods**: enable **One-time PIN** (emails you a code) and/or **GitHub**. Turn off every method you don't use.
6. Save. Reopen the application, **Overview**, and copy the **Application Audience (AUD) Tag**.
7. Put both values in `wrangler.toml`: `ACCESS_TEAM_DOMAIN = "<team>.cloudflareaccess.com"` and `ACCESS_AUD = "<the AUD tag>"`. Until you do, every request is refused.

### 2. Create the GitHub token the Worker uses

This is **not** the token that opens PRs (that one is `SUBMIT_TOKEN`, stored only in the data repo). This one can only read the dashboard and start the workflow.

1. GitHub, **Settings**, **Developer settings**, **Personal access tokens**, **Fine-grained tokens**, **Generate new token**.
2. Name `oss-scout-dashboard`, expiration 90 days (or longer; put a reminder in your calendar).
3. **Repository access**: *Only select repositories*, choose `aadityad12/oss-scout-data` and nothing else.
4. **Repository permissions**: **Contents: Read-only**, **Actions: Read and write**. Leave everything else at "No access" (Metadata read is added automatically).
5. Generate, copy the token (starts with `github_pat_`).

### 3. Deploy

```bash
cd worker
npx wrangler login            # opens a browser; log in to Cloudflare
npx wrangler deploy           # creates the Worker and the me.aadityad.dev custom domain
```

If wrangler says the domain is already in use, delete any leftover DNS record for `me` in the Cloudflare DNS tab and deploy again.

### 4. Set the two secrets

```bash
npx wrangler secret put GITHUB_TOKEN     # paste the fine-grained token from step 2
npx wrangler secret put RESEND_API_KEY   # from step 5
```

### 5. Set up email with Resend

1. Sign up at resend.com, **API Keys**, **Create API Key**. Name `oss-scout`, permission
   **Sending access**, domain **All domains** (the only choice before a domain is added; the
   key can only send, and the Worker only ever emails you). Copy it (starts with `re_`) for step 4.
2. **Domains**, **Add Domain**, `aadityad.dev`. Add the DNS records Resend shows (Cloudflare DNS tab) and click **Verify**. Emails then send from `OSS Scout <scout@aadityad.dev>`.
3. No domain yet? Add `MAIL_FROM = "OSS Scout <onboarding@resend.dev>"` under `[vars]` in `wrangler.toml` and redeploy. That sender can only email the address you signed up to Resend with, which is enough for one person.

### 6. Install the workflow the Worker starts

From the repo root, in a checkout of the data repo's `main` branch:

```bash
SCOUT_DATA_DIR=/path/to/oss-scout-data python3 -m scout init-data
```

Commit and push `.github/workflows/act.yml` (with the `.claude/` files it also writes) to `main`. Then in the data repo, **Settings**, **Secrets and variables**, **Actions**, add `SUBMIT_TOKEN`: a classic token with only the `public_repo` scope.

### 7. Check it

1. Open <https://me.aadityad.dev>. You should get the Access login, then the dashboard. Anything else (another email, another browser without a login) must be refused.
2. Tap **Later** on an item and watch the run appear on the page. Or run a dry run from GitHub: Actions, **act**, **Run workflow**, key `owner/repo#123`, action `submit`, tick *dry_run*.
3. Email: Cloudflare dashboard, Workers, `oss-scout-dashboard`, **Triggers**, cron, and check **Logs** after 15:00 UTC, or wait for the morning.

## Day to day

- **Logs**: `npx wrangler tail`.
- **Timing**: the cron `0 15 * * *` is 8am Pacific in summer and 7am in winter; edit `wrangler.toml` if you mind.
- **Rotating the GitHub token**: generate a new one, `npx wrangler secret put GITHUB_TOKEN`.
- **Tests**: `cd worker && node --test`.

## What the Worker accepts

- Every request needs a valid Cloudflare Access token for your email (checked again here, not just at the edge).
- `GET /` serves `dashboard.html` from the data repo, `GET /api/runs` lists the last five `act.yml` runs.
- `POST /api/act` needs `content-type: application/json` and an `Origin` of `https://me.aadityad.dev`. `action` must be one of `submit`, `post`, `followup`, `approve`, `later`, `skip`; `key` must look like `owner/repo#123`; the title is at most 256 characters and the text at most 60000 (GitHub caps workflow inputs at 65,535 characters in total, so in practice about 45,000).
- It never writes to GitHub itself. It only starts the workflow.
