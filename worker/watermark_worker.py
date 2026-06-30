# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║       TG-CoursestreamBot — Watermark Worker (GitHub Actions) v3            ║
# ║       Max Speed: NVENC → GPU encode → CPU fallback                         ║
# ║       Pipeline: next job download+ffmpeg overlaps with current upload      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

import subprocess, os, sys, uuid, time, threading, asyncio, requests, re

# ── Config from Environment Variables ─────────────────────────────────────────
KOYEB_URL = os.environ.get("KOYEB_URL") or "https://sick-nikaniki-shubham8208989-6d7c863a.koyeb.app"
COLAB_SECRET = os.environ.get("COLAB_SECRET") or "e000011561fb55e2965b3ec743c658202f1989673"
UPLOAD_BOT_TOKEN = os.environ.get("UPLOAD_BOT_TOKEN")
POLL_INTERVAL  = int(os.environ.get('POLL_INTERVAL', '5'))
FFMPEG_TIMEOUT = int(os.environ.get('FFMPEG_TIMEOUT', '14400'))  # 4 hours

HEADERS   = {'X-Secret-Key': COLAB_SECRET}
WORKER_ID = str(uuid.uuid4())[:12]

print(f'🆔 Worker ID : {WORKER_ID}')
print(f'🌐 Koyeb URL : {KOYEB_URL}')
print(f'⏱ Poll every: {POLL_INTERVAL}s')


# ── Telethon import ────────────────────────────────────────────────────────────
try:
    from pyrogram import Client
    from pyrogram.errors import FloodWait
    import tgcrypto
except ImportError:
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'telethon'], check=True)
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import DocumentAttributeVideo


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ENCODER DETECTION (3-tier)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FFMPEG_BIN = 'ffmpeg'

def _test_encode(extra_input_args, encode_args):
    try:
        cmd = (
            [FFMPEG_BIN]
            + extra_input_args
            + ['-f', 'lavfi', '-i', 'nullsrc=s=320x240:d=1']
            + encode_args
            + ['-f', 'null', '-', '-y']
        )
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False

def detect_encoder():
    """
    Tier 1: hwaccel cuda + h264_nvenc  — full GPU pipeline
    Tier 2: h264_nvenc only            — CPU decode + GPU encode
    Tier 3: libx264 ultrafast          — CPU all cores
    """
    if _test_encode(
        ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'],
        ['-c:v', 'h264_nvenc', '-preset', 'p1', '-b:v', '2M']
    ):
        print('✅ Encoder: Tier 1 — CUDA hwaccel + h264_nvenc (full GPU)')
        return 'tier1'

    if _test_encode(
        [],
        ['-c:v', 'h264_nvenc', '-preset', 'p1', '-b:v', '2M']
    ):
        print('✅ Encoder: Tier 2 — h264_nvenc (GPU encode, CPU decode)')
        return 'tier2'

    print('✅ Encoder: Tier 3 — libx264 ultrafast (CPU all cores)')
    return 'tier3'

ENCODER_TIER = detect_encoder()
print(f'✅ Encoder mode ready: {ENCODER_TIER}')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def make_bar(pct, width=20):
    filled = int(width * pct / 100)
    return f"[{'█'*filled}{'░'*(width-filled)}] {pct:.1f}%"


def get_video_meta(path):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,duration',
             '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=30
        )
        parts = r.stdout.strip().split(',')
        w   = int(parts[0])   if len(parts) > 0 and parts[0].strip().isdigit() else 1280
        h   = int(parts[1])   if len(parts) > 1 and parts[1].strip().isdigit() else 720
        dur = float(parts[2]) if len(parts) > 2 and parts[2].strip() not in ('', 'N/A') else 0.0
        if dur <= 0:
            r2 = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True, text=True, timeout=30
            )
            try:
                dur = float(r2.stdout.strip())
            except Exception:
                dur = 0.0
        return int(dur), w, h
    except Exception as e:
        print(f'⚠️ get_video_meta error: {e}')
        return 0, 1280, 720


def extract_thumb_from_video(video_path, duration):
    try:
        seek = max(1, int(duration * 0.2)) if duration > 0 else 5
        thumb_path = video_path + '_thumb.jpg'
        r = subprocess.run(
            ['ffmpeg', '-ss', str(seek), '-i', video_path,
             '-vframes', '1', '-q:v', '2', '-y', thumb_path],
            capture_output=True, timeout=30
        )
        if r.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            print(f'✅ Thumbnail extracted at {seek}s')
            return thumb_path
        print(f'⚠️ Thumbnail extract failed (returncode={r.returncode})')
    except Exception as e:
        print(f'⚠️ extract_thumb error: {e}')
    return None


def get_thumb(job, video_path, duration):
    thumb_url = job.get('thumbnail_url') or job.get('thumb_url')
    if thumb_url:
        try:
            custom_path = video_path + '_custom_thumb.jpg'
            r = subprocess.run(['wget', '-q', '-O', custom_path, thumb_url], timeout=30)
            if r.returncode == 0 and os.path.exists(custom_path) and os.path.getsize(custom_path) > 0:
                print('✅ Custom thumbnail downloaded')
                return custom_path
            print('⚠️ Custom thumb fail → video extract fallback')
        except Exception as e:
            print(f'⚠️ Thumb URL error: {e}')
    return extract_thumb_from_video(video_path, duration)


# ── Koyeb API calls ────────────────────────────────────────────────────────────

_progress_seq = {}

def koyeb_progress(job_id, msg):
    _progress_seq[job_id] = _progress_seq.get(job_id, 0) + 1
    try:
        requests.post(f'{KOYEB_URL}/api/colab/progress',
                      json={'job_id': job_id, 'message': msg,
                            'worker_id': WORKER_ID, 'seq': _progress_seq[job_id]},
                      headers=HEADERS, timeout=10)
    except:
        pass

def koyeb_done(job_id):
    try:
        requests.post(f'{KOYEB_URL}/api/colab/done',
                      json={'job_id': job_id},
                      headers=HEADERS, timeout=10)
    except:
        pass

def koyeb_failed(job_id, error):
    try:
        requests.post(f'{KOYEB_URL}/api/colab/failed',
                      json={'job_id': job_id, 'error': str(error)},
                      headers=HEADERS, timeout=10)
    except:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FLOODWAIT HANDLING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_flood_wait_seconds(err):
    """
    Pyrogram FloodWait exception (.value attribute) किंवा raw error string
    ("A wait of 242 seconds is required") दोन्हीतून seconds काढतो.
    FloodWait नसेल तर None return करतो.
    """
    try:
        from pyrogram.errors import FloodWait
        if isinstance(err, FloodWait):
            return int(err.value)
    except Exception:
        pass

    err_str = str(err)
    if 'FLOOD_WAIT' not in err_str.upper() and 'flood' not in err_str.lower():
        return None

    m = re.search(r'wait of (\d+) seconds', err_str, re.IGNORECASE)
    if m:
        return int(m.group(1))

    m = re.search(r'FLOOD_WAIT_(\d+)', err_str.upper())
    if m:
        return int(m.group(1))

    return None


def wait_with_progress(job_id, fname, total_seconds, reason='FloodWait'):
    """
    FloodWait संपेपर्यंत थांबतो आणि दर 30 sec ला Koyeb la remaining time
    progress म्हणून पाठवतो. job कधीच failed मानला जात नाही — फक्त paused.
    """
    print(f'⏳ {reason}: {total_seconds}s थांबतोय (job: {job_id})')
    remaining = total_seconds
    while remaining > 0:
        mins, secs = divmod(remaining, 60)
        koyeb_progress(
            job_id,
            f"⏳ *{reason} — Telegram limit*\n📄 `{fname}`\n"
            f"⏱ Remaining: `{mins}m {secs}s`\n"
            f"🔁 आपोआप resume होईल, काही करायची गरज नाही."
        )
        sleep_chunk = min(30, remaining)
        time.sleep(sleep_chunk)
        remaining -= sleep_chunk
    print(f'✅ {reason} संपला — resume करतोय (job: {job_id})')

def poll_job():
    try:
        r = requests.get(f'{KOYEB_URL}/api/colab/poll',
                         params={'worker_id': WORKER_ID},
                         headers=HEADERS, timeout=15)
        data = r.json()
        return data.get('job'), data.get('api_id'), data.get('api_hash'), data.get('stop', False)
    except Exception as e:
        print(f'⚠️ poll error: {e}')
        return None, None, None, False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FFMPEG COMMAND BUILDER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_ffmpeg_cmd(input_path, output_path, drawtext, tier):
    if tier == 'tier1':
        return (
            [FFMPEG_BIN,
             '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
             '-extra_hw_frames', '4',
             '-i', input_path,
             '-vf', f'hwdownload,format=nv12,{drawtext},hwupload_cuda',
             '-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'll',
             '-b:v', '1M', '-maxrate', '2M', '-bufsize', '4M',
             '-gpu', '0', '-c:a', 'copy', '-movflags', '+faststart',
             '-progress', 'pipe:2', '-stats_period', '3', '-y', output_path],
            'GPU full (hwaccel+nvenc)'
        )
    elif tier == 'tier2':
        return (
            [FFMPEG_BIN,
             '-i', input_path,
             '-vf', drawtext,
             '-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'll',
             '-b:v', '1M', '-maxrate', '2M', '-bufsize', '4M',
             '-gpu', '0', '-c:a', 'copy', '-movflags', '+faststart',
             '-progress', 'pipe:2', '-stats_period', '3', '-y', output_path],
            'GPU encode (nvenc)'
        )
    else:
        return (
            [FFMPEG_BIN,
             '-i', input_path,
             '-vf', drawtext,
             '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
             '-threads', '0', '-tune', 'fastdecode',
             '-c:a', 'copy', '-movflags', '+faststart',
             '-progress', 'pipe:2', '-stats_period', '3', '-y', output_path],
            'CPU libx264 ultrafast'
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DOWNLOAD + FFMPEG PROCESSOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def download_file(file_url, dest_path, job_id, job):
    fname = job['file_name']
    total = job.get('file_size', 0)
    koyeb_progress(job_id,
        f"📥 *Downloading...*\n📄 `{fname}`\n📌 Msg: `{job['msg_id']}`\n{make_bar(0)}")
    try:
        r = requests.get(file_url, headers=HEADERS, stream=True, timeout=60)
        r.raise_for_status()
        content_len = int(r.headers.get('Content-Length', total or 0))
        downloaded, start, last_update = 0, time.time(), time.time()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if time.time() - last_update >= 3 and content_len > 0:
                        pct   = min(downloaded / content_len * 100, 99)
                        speed = (downloaded / 1024 / 1024) / (time.time() - start)
                        koyeb_progress(job_id,
                            f"📥 *Downloading...*\n📄 `{fname}`\n{make_bar(pct)}\n"
                            f"📌 Msg: `{job['msg_id']}`\n"
                            f"`{downloaded/1024/1024:.1f} / {content_len/1024/1024:.1f} MB`\n"
                            f"🚀 `{speed:.2f} MB/s`")
                        last_update = time.time()
        elapsed = time.time() - start
        print(f'✅ Downloaded: {downloaded/1024/1024:.1f} MB in {elapsed:.0f}s')
        return True
    except Exception as e:
        return False, str(e)


def get_duration(input_path):
    for cmd in [
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
        [FFMPEG_BIN, '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            val = float(r.stdout.strip())
            if val > 0:
                return val
        except:
            pass
    return 0


def run_ffmpeg_with_progress(cmd, label, total_dur, job_id, job):
    fname   = job['file_name']
    process = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, text=True)
    start, cur_t, last_upd = time.time(), 0.0, time.time()

    try:
        for line in process.stderr:
            if line.strip().startswith('out_time_ms='):
                try:
                    val = int(line.split('=')[1])
                    if val > 0:
                        cur_t = val / 1_000_000
                except:
                    pass
            if time.time() - last_upd >= 4:
                elapsed = time.time() - start
                if total_dur > 0 and cur_t > 0:
                    pct = min(cur_t / total_dur * 100, 99)
                    eta = (elapsed / pct * (100 - pct)) if pct > 0 else 0
                    koyeb_progress(job_id,
                        f"💧 *Watermark applying...*\n📄 `{fname}`\n📌 Msg: `{job['msg_id']}`\n"
                        f"⚙️ `{label}`\n{make_bar(pct)}\n"
                        f"⏱ `{int(elapsed//60)}m{int(elapsed%60)}s` | ETA: `{int(eta//60)}m{int(eta%60)}s`")
                else:
                    koyeb_progress(job_id,
                        f"💧 *Watermark applying...*\n📄 `{fname}`\n📌 Msg: `{job['msg_id']}`\n"
                        f"⚙️ `{label}`\n⏱ `{int(elapsed//60)}m{int(elapsed%60)}s` elapsed...")
                last_upd = time.time()
    except Exception as e:
        print(f'⚠️ stderr read error: {e}')

    try:
        process.wait(timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        return False, 'FFmpeg timeout (4hr)'

    if process.returncode != 0:
        return False, f'FFmpeg exit: {process.returncode}'

    print(f'✅ FFmpeg done: {time.time()-start:.0f}s ({label})')
    return True, None


def run_ffmpeg_remux_only(input_path, output_path, job_id, job):
    """Watermark text नसेल तर — फक्त remux (-c copy), re-encode नाही. खूप जलद."""
    koyeb_progress(job_id,
        f"📦 *Processing (no watermark)...*\n📄 `{job['file_name']}`\n"
        f"📌 Msg: `{job['msg_id']}`\n⚙️ `remux (copy)`")
    cmd = [FFMPEG_BIN, '-i', input_path, '-c', 'copy', '-movflags', '+faststart',
           '-y', output_path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            print('✅ Remux (copy) done — re-encode skip झाला')
            return True, None
        err = r.stderr.decode(errors='ignore')[-500:] if r.stderr else 'unknown'
        print(f'⚠️ Remux copy failed: {err} → fallback to plain file copy')
        # fallback: ffmpeg remux fail झालं तरी file as-is वापरा
        import shutil
        shutil.copyfile(input_path, output_path)
        return True, None
    except Exception as e:
        print(f'⚠️ Remux exception: {e} → fallback to plain file copy')
        try:
            import shutil
            shutil.copyfile(input_path, output_path)
            return True, None
        except Exception as e2:
            return False, str(e2)


def run_ffmpeg(input_path, output_path, wm_text, job_id, job):
    if not wm_text or not wm_text.strip():
        return run_ffmpeg_remux_only(input_path, output_path, job_id, job)

    total_dur = get_duration(input_path)
    safe_text = wm_text.replace("'", "\\'").replace(':', '\\:')
    drawtext  = (f"drawtext=text='{safe_text}':fontcolor=white:fontsize=36:"
                 f"shadowcolor=black:shadowx=2:shadowy=2:x=w-tw-20:y=h-th-20")

    current_tier = ENCODER_TIER
    while True:
        cmd, label = build_ffmpeg_cmd(input_path, output_path, drawtext, current_tier)
        koyeb_progress(job_id,
            f"💧 *Watermark applying...*\n📄 `{job['file_name']}`\n"
            f"📌 Msg: `{job['msg_id']}`\n⚙️ `{label}`\n{make_bar(0)}")

        ok, err = run_ffmpeg_with_progress(cmd, label, total_dur, job_id, job)
        if ok:
            return True, None

        print(f'⚠️ {label} failed: {err}')
        if current_tier == 'tier1':
            koyeb_progress(job_id, f"⚠️ GPU full failed → GPU encode only...\n📄 `{job['file_name']}`")
            if os.path.exists(output_path): os.remove(output_path)
            current_tier = 'tier2'
        elif current_tier == 'tier2':
            koyeb_progress(job_id, f"⚠️ GPU encode failed → CPU fallback...\n📄 `{job['file_name']}`")
            if os.path.exists(output_path): os.remove(output_path)
            current_tier = 'tier3'
        else:
            return False, f'सगळे encoders fail: {err}'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TELEGRAM UPLOADER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def telegram_send(output_path, job_id, job, api_id, api_hash):

    from pyrogram import Client
    from pyrogram.raw.types import InputChannel
    from pyrogram.raw.functions.messages import SendMedia
    import tgcrypto

    fname = job['file_name']
    bot_token = UPLOAD_BOT_TOKEN or job['bot_token']
    send_chat_id = job['send_to_chat_id']
    access_hash = job.get('send_to_access_hash', 0)
    topic_id = job.get('send_to_topic_id') or None
    caption = job.get('caption') or fname
    log_chat_id = job.get('log_chat_id') or None
    log_access_hash = job.get('log_access_hash', 0)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f'📤 Upload start: {size_mb:.1f}MB → chat {send_chat_id}')

    dur, w, h = get_video_meta(output_path)
    thumb_path = get_thumb(job, output_path, dur)

    start = time.time()
    last_update = [0]

    def progress(current, total):
        if time.time() - last_update[0] >= 3:
            pct = current / total * 100 if total else 0
            speed = (current / 1024 / 1024) / (time.time() - start)

            koyeb_progress(
                job_id,
                f"📤 Uploading...\n{make_bar(pct)}\n🚀 `{speed:.2f} MB/s`"
            )
            last_update[0] = time.time()

    async def warm_peer(app, chat_id, ahash):
        if not ahash:
            return
        try:
            from pyrogram.raw.types import InputChannel as RawInputChannel
            from pyrogram.raw.functions.channels import GetChannels
            channel_id = abs(chat_id) - 1000000000000
            raw_peer = RawInputChannel(channel_id=channel_id, access_hash=ahash)
            await app.invoke(GetChannels(id=[raw_peer]))
            print(f'✅ Peer resolved via access_hash: {channel_id}')
        except Exception as pe:
            print(f'⚠️ Peer pre-resolve warning ({chat_id}): {pe}')

    async def upload():
        app = Client(
            "worker",
            api_id=int(api_id),
            api_hash=api_hash,
            bot_token=bot_token,
            in_memory=True
        )

        await app.start()

        try:
            # Peer cache warm करा — access_hash असेल तर raw InputChannel वापरा
            await warm_peer(app, send_chat_id, access_hash)

            sent_msg = await app.send_video(
                chat_id=send_chat_id,
                video=output_path,
                caption=caption,
                duration=dur,
                width=w,
                height=h,
                thumb=thumb_path if thumb_path else None,
                supports_streaming=True,
                progress=progress
            )

            # ── LOG_CHANNEL backup copy ──────────────────────────────────
            # जुन्या flow मध्ये प्रत्येक file logChannel ला पण कॉपी होत असे.
            # इथे re-upload टाळून, set channel ला आधीच गेलेल्या message ची
            # copy log channel ला पाठवतो (bandwidth वाचतो).
            if log_chat_id:
                try:
                    await warm_peer(app, log_chat_id, log_access_hash)
                    await app.copy_message(
                        chat_id=log_chat_id,
                        from_chat_id=send_chat_id,
                        message_id=sent_msg.id,
                        caption=caption,
                    )
                    print(f'✅ Log channel backup copied → {log_chat_id}')
                except Exception as le:
                    # Log backup fail झाला तरी मुख्य upload success मानायचा
                    print(f'⚠️ Log channel backup failed: {le}')

            return True, None   # ✅ IMPORTANT

        except Exception as e:
            return False, str(e)

        finally:
            await app.stop()

    try:
        result = asyncio.run(upload())

        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)

        if not result or len(result) != 2:
            return False, "invalid upload response"

        return result

    except Exception as e:
        return False, str(e)

def do_upload_phase(job_id, fname, output_path, job, api_id, api_hash):
    upload_ok = False
    err = None
    try:
        attempt = 1
        max_attempts = 3
        flood_retries = 0
        max_flood_retries = 20  # सलग खूप जास्त FloodWait आल्यास infinite loop टाळायला cap

        while attempt <= max_attempts:
            if attempt > 1:
                koyeb_progress(job_id,
                    f"🔄 *Upload retry {attempt}/{max_attempts}...*\n📄 `{fname}`")
                time.sleep(15)

            ok, err = telegram_send(output_path, job_id, job, api_id, api_hash)
            if ok:
                upload_ok = True
                break

            wait_secs = extract_flood_wait_seconds(err)
            if wait_secs is not None:
                flood_retries += 1
                if flood_retries > max_flood_retries:
                    print(f'❌ FloodWait खूप वेळा आला ({flood_retries}x) — job fail मानतोय')
                    break
                # FloodWait → attempt counter वाढवत नाही, फक्त थांबून परत तोच attempt करतो
                wait_with_progress(job_id, fname, wait_secs + 5, reason='FloodWait')
                continue

            print(f'❌ Upload attempt {attempt} failed: {err}')
            attempt += 1

        if not upload_ok:
            koyeb_failed(job_id, err)
            koyeb_progress(job_id,
                f'❌ Upload failed (3 attempts)\n📄 `{fname}`\n`{err}`')
            return

        koyeb_done(job_id)
        koyeb_progress(job_id,
            f"✅ *Done!*\n📄 `{fname}`\n💧 Watermark applied\n📤 Telegram ला पाठवलं")
        print(f'✅ Job {job_id} done!')

    except Exception as e:
        print(f'❌ Upload phase exception: {e}')
        koyeb_failed(job_id, str(e))
        koyeb_progress(job_id, f'❌ Upload error\n📄 `{fname}`\n`{e}`')
    finally:
        if upload_ok:
            try:
                if os.path.exists(output_path): os.remove(output_path)
            except: pass


def process_job_download_ffmpeg(job):
    job_id = job['job_id']
    fname  = job['file_name']
    furl   = job['file_url']
    wm     = job['watermark_text']

    tier_label = {'tier1': 'GPU full', 'tier2': 'GPU encode', 'tier3': 'CPU'}.get(ENCODER_TIER, ENCODER_TIER)
    print(f'\n🎯 Job: {job_id} | {fname} | WM: {wm} | Encoder: {tier_label}')

    ext         = os.path.splitext(fname)[1] or '.mp4'
    input_path  = f'/tmp/wm_input_{job_id}{ext}'
    output_path = f'/tmp/wm_output_{job_id}{ext}'

    try:
        ok = download_file(furl, input_path, job_id, job)
        if ok is not True:
            err = ok[1] if isinstance(ok, tuple) else 'download failed'
            koyeb_failed(job_id, err)
            koyeb_progress(job_id, f'❌ Download failed\n📄 `{fname}`\n`{err}`')
            return None, job_id, fname

        ok, err = run_ffmpeg(input_path, output_path, wm, job_id, job)
        if not ok:
            koyeb_failed(job_id, err)
            koyeb_progress(job_id, f'❌ FFmpeg failed\n📄 `{fname}`\n`{err}`')
            return None, job_id, fname

        return output_path, job_id, fname

    except Exception as e:
        print(f'❌ Exception (download/ffmpeg): {e}')
        koyeb_failed(job_id, str(e))
        koyeb_progress(job_id, f'❌ Error\n📄 `{fname}`\n`{e}`')
        return None, job_id, fname

    finally:
        try:
            if os.path.exists(input_path): os.remove(input_path)
        except: pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN LOOP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    print(f'\n🚀 Worker started (GitHub Actions pipelined v3)!')
    print(f'   Koyeb  : {KOYEB_URL}')
    print(f'   Encoder: {ENCODER_TIER}')
    print(f'   Poll   : {POLL_INTERVAL}s\n')

    job_count    = 0
    alive_tick   = 0
    upload_thread = None

    while True:
        try:
            job, api_id, api_hash, stop = poll_job()

            if stop:
                print('🛑 Stop command received — चालू job पूर्ण करून थांबतोय...')
                if upload_thread and upload_thread.is_alive():
                    upload_thread.join()
                print('✅ Worker stopped by server command.')
                break

            if job and api_id and api_hash:
                job_count += 1
                print(f'📦 Job #{job_count}: {job["job_id"]}')

                output_path, job_id, fname = process_job_download_ffmpeg(job)

                if output_path:
                    if upload_thread and upload_thread.is_alive():
                        print('⏳ मागचा upload पूर्ण होण्याची वाट...')
                        upload_thread.join()

                    upload_thread = threading.Thread(
                        target=do_upload_phase,
                        args=(job_id, fname, output_path, job, api_id, api_hash),
                        daemon=True,
                    )
                    upload_thread.start()
            else:
                alive_tick += 1
                if alive_tick % (60 // POLL_INTERVAL) == 0:
                    print(f'💓 Alive | Done: {job_count} | {ENCODER_TIER} | {time.strftime("%H:%M:%S")}')

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print('\n🛑 KeyboardInterrupt — stopping...')
            if upload_thread and upload_thread.is_alive():
                upload_thread.join()
            break
        except Exception as e:
            print(f'❌ Loop error: {e}')
            time.sleep(POLL_INTERVAL * 2)


if __name__ == '__main__':
    main()
