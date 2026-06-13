# 📺 Auto-IPTV Playlist — CodeCloudBD

> Extracts live HLS stream URLs from **stream.codecloud.bd** using a headless
> Chromium browser and auto-commits an M3U playlist **every hour** via GitHub Actions.

---

## 📁 Repo Structure

```
your-repo/
├── .github/
│   └── workflows/
│       └── update_playlist.yml   ← Hourly scheduled workflow
├── scripts/
│   └── extract_stream.py         ← Playwright-based stream extractor
├── playlist/
│   └── fifa_tv.m3u               ← Auto-generated playlist (don't edit)
└── README.md
```

---

## ⚙️ Setup (one-time, 3 steps)

### 1. Upload these files to your GitHub repo (keep folder structure exact)

### 2. Enable GitHub Actions
Go to **Actions tab** → click **"I understand my workflows, go ahead and enable them"**

### 3. Run manually to test
**Actions → Update IPTV Playlist → Run workflow**

No environment variables or secrets needed — everything is configured inside the script.

---

## 📡 Your Playlist URL

After the first successful run, use this URL in any IPTV player:

```
https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/playlist/fifa_tv.m3u
```

Replace `YOUR_USERNAME` and `YOUR_REPO` with your actual GitHub username and repo name.

---

## 🔧 Add More Channels

Edit `scripts/extract_stream.py` and add entries to `MANUAL_CHANNELS`:

```python
MANUAL_CHANNELS = [
    {"name": "FIFA TV",    "url": "https://stream.codecloud.bd/watch/fifa-tv"},
    {"name": "beIN 1",     "url": "https://stream.codecloud.bd/watch/bein-1"},
    {"name": "Star Sports", "url": "https://stream.codecloud.bd/watch/star-sports"},
]
```

---

## 🧠 How It Works

| Step | What happens |
|---|---|
| 1 | Headless Chromium opens `stream.codecloud.bd` |
| 2 | All network requests/responses are intercepted |
| 3 | Any `.m3u8` URL seen in network traffic is captured |
| 4 | Auto-discovers channel links on the page |
| 5 | Visits each channel and captures its stream URL |
| 6 | Writes valid M3U playlist to `playlist/fifa_tv.m3u` |
| 7 | Commits and pushes if anything changed |

---

## ⏱️ Schedule

Runs automatically **every hour** at :00 (e.g. 10:00, 11:00, 12:00…).

GitHub may pause the schedule if your repo has **no activity for 60 days** — just push any commit to re-enable.

---

## 🐛 Troubleshooting

| Error | Fix |
|---|---|
| `No module named 'playwright'` | You have the old workflow — replace `update_playlist.yml` |
| `SyntaxError: f-string backslash` | You have the old script — replace `extract_stream.py` |
| No streams found | The site may need a longer wait — open an issue |
