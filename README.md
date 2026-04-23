## Congress trades mirror (free)

This repo mirrors public congressional trades datasets into GitHub so that the bot can fetch them from `raw.githubusercontent.com`
even when S3 endpoints are blocked by your network.

### What it does
- Downloads:
  - House: `all_transactions.json`
  - Senate: `all_transactions.json`
- Validates JSON
- Commits changes daily via GitHub Actions

### Setup
1. Create a new GitHub repo (public is fine), e.g. `congress-trades-mirror`
2. Push this folder as the repo content
3. Enable Actions (default)

### Resulting raw URLs
After first run, you will have:
- `data/house/all_transactions.json`
- `data/senate/all_transactions.json`

Raw URLs (replace `<you>` / `<repo>`):
- `https://raw.githubusercontent.com/<you>/<repo>/main/data/house/all_transactions.json`
- `https://raw.githubusercontent.com/<you>/<repo>/main/data/senate/all_transactions.json`

