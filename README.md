# TG-CoursestreamBot — Watermark Worker (GitHub Actions)

Google Colab ऐवजी **GitHub Actions** वर चालणारा watermark worker.
Free tier वर चालतो — CPU mode (libx264 ultrafast).

---

## 📁 Project Structure

```
tg-watermark-worker/
├── .github/
│   └── workflows/
│       └── watermark_worker.yml   ← GitHub Actions workflow
├── worker/
│   └── watermark_worker.py        ← Main worker script
├── requirements.txt
└── README.md
```

---

## ⚙️ Setup Steps

### 1. GitHub Repository तयार करा
```bash
git init
git add .
git commit -m "Initial watermark worker"
git remote add origin https://github.com/YOUR_USERNAME/tg-watermark-worker.git
git push -u origin main
```

### 2. GitHub Secrets Set करा
GitHub Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name    | Value                            |
|----------------|----------------------------------|
| `KOYEB_URL`    | `https://your-app.koyeb.app`     |
| `COLAB_SECRET` | तुझा secret key (COLAB_SECRET)   |

> **Note:** Bot token, api_id, api_hash Koyeb server कडून poll response मध्ये येतात — secrets मध्ये टाकण्याची गरज नाही.

### 3. Worker Start करा
- GitHub Repo → **Actions tab** → "TG Watermark Worker" → **Run workflow**

### 4. Auto-restart (Optional)
Workflow मध्ये `schedule: cron: '0 */5 * * *'` असल्यामुळे दर 5 तासांनी auto-restart होतो.
GitHub Actions limit 6 hours/run आहे — 5 तास safe margin आहे.

---

## 🔧 Optional Variables
GitHub Repo → **Settings → Variables → Actions → New repository variable**

| Variable       | Default | Description          |
|----------------|---------|----------------------|
| `POLL_INTERVAL`| `5`     | Poll interval seconds|

---

## 🎮 Encoder Tiers

| Tier   | Mode                     | GitHub Free Runner |
|--------|--------------------------|--------------------|
| Tier 1 | CUDA hwaccel + h264_nvenc| ❌ (GPU नाही)       |
| Tier 2 | h264_nvenc only          | ❌ (GPU नाही)       |
| **Tier 3** | **libx264 ultrafast**| **✅ (auto-select)**|

GitHub free runner वर CPU mode (Tier 3) auto-select होतो.
GPU हवा असल्यास paid self-hosted runner लागेल.

---

## 🛑 Stop Worker
Koyeb server कडून `/colabstop` command पाठवल्यावर worker gracefully stop होतो.
किंवा GitHub Actions → workflow run → **Cancel** बटण दाबा.

---

## 📊 Logs
GitHub Actions → workflow run → job logs मध्ये real-time output दिसतो.
