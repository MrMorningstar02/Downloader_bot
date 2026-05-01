import asyncio
import logging
import os
import shutil
from telethon import TelegramClient, events, Button
import yt_dlp
from pathlib import Path
import tempfile
import queue
import http.server
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Render Health Check Server
class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        return # Silent logs for health checks

def run_health_check():
    try:
        port = int(os.environ.get("PORT", 8080))
        server = http.server.HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        logger.info(f"Health check server started on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Health check server failed to start: {e}")
        logger.info("The bot will continue to run, but Render health checks may fail if this happens on the server.")

API_ID = 30598720
API_HASH = "283fbc7ac0723e792f039b63c0952ede"
BOT_TOKEN = "8645304686:AAGqPZV2k9rtPTNJy1bLA8nQ-4ToeN03m8E"

client = TelegramClient(
    'video_downloader_bot', # Changed name to force a fresh session
    API_ID, 
    API_HASH,
    connection_retries=10,
    retry_delay=2,
    flood_sleep_threshold=120
)

url_store = {}

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"Bot is online! Logged in as: {me.first_name} (@{me.username})")
    await client.run_until_disconnected()

def check_dependencies():
    if not shutil.which('ffmpeg'):
        logger.warning("FFmpeg not found! High quality merges will fail. Only single-file formats (usually 720p or lower) will work.")
        return False
    return True

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    ffmpeg_status = "" if check_dependencies() else "\n\n⚠️ *Warning: FFmpeg not found on server. Quality might be limited.*"
    await event.reply(
        f"Video Downloader Bot\n\nSend video links. Supports 1000+ sites!\n\nUp to 2GB files\nFull speed mode{ffmpeg_status}",
        parse_mode='markdown'
    )

@client.on(events.NewMessage(func=lambda e: e.text and e.text.startswith(('http://', 'https://'))))
async def handle_url(event):
    url = event.text.strip()
    msg_id = str(event.message.id)
    chat_id = str(event.chat_id)
    
    store_key_msg = f"{chat_id}_{msg_id}"
    url_store[store_key_msg] = url

    buttons_msg = await event.reply(
        "Select quality:",
        buttons=[
            [Button.inline("Best", b"best"), Button.inline("1080p", b"1080")],
            [Button.inline("720p", b"720"), Button.inline("480p", b"480")],
            [Button.inline("360p", b"360")]
        ]
    )
    
    store_key_btn = f"{chat_id}_{buttons_msg.id}"
    url_store[store_key_btn] = url
    
    async def cleanup_url_store():
        await asyncio.sleep(3600)
        url_store.pop(store_key_msg, None)
        url_store.pop(store_key_btn, None)
        
    asyncio.create_task(cleanup_url_store())

@client.on(events.CallbackQuery)
async def handle_quality(event):
    quality = event.data.decode()

    button_msg = await event.get_message()
    if not button_msg:
        await event.edit("Error")
        return

    chat_id = str(event.chat_id)
    msg_id = str(button_msg.reply_to_msg_id) if button_msg.reply_to_msg_id else str(button_msg.id)

    store_key_msg = f"{chat_id}_{msg_id}"
    store_key_btn = f"{chat_id}_{button_msg.id}"

    url = url_store.get(store_key_msg) or url_store.get(store_key_btn)
    if not url:
        await event.edit("Session expired.")
        return

    status_msg = await event.edit("Downloading...")
    loop = asyncio.get_event_loop()
    video_path = None

    prog_queue = queue.Queue()
    upload_done = [False]

    def dl_progress(d):
        try:
            if d.get('status') == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                if total > 0:
                    percent = int((downloaded / total) * 100)
                    prog_queue.put(('dl', percent))
        except:
            pass

    def ul_progress(current, total):
        try:
            if total > 0:
                percent = int((current / total) * 100)
                prog_queue.put(('ul', percent))
        except:
            pass

    async def progress_updater():
        last_dl = -1
        last_ul = -1
        while not upload_done[0]:
            try:
                # Get all pending items to avoid backlog and lag
                items = []
                while True:
                    try:
                        items.append(prog_queue.get_nowait())
                    except queue.Empty:
                        break
                
                if items:
                    # Only care about the latest update for each type
                    latest_dl = next((i[1] for i in reversed(items) if i[0] == 'dl'), None)
                    latest_ul = next((i[1] for i in reversed(items) if i[0] == 'ul'), None)

                    if latest_dl is not None and latest_dl >= last_dl + 3:
                        last_dl = latest_dl
                        bar = "█" * (latest_dl // 5) + "░" * (20 - latest_dl // 5)
                        try:
                            await status_msg.edit(f"DL: {bar} {latest_dl}%")
                        except:
                            pass
                    
                    if latest_ul is not None and latest_ul >= last_ul + 3:
                        last_ul = latest_ul
                        bar = "█" * (latest_ul // 5) + "░" * (20 - latest_ul // 5)
                        try:
                            await status_msg.edit(f"UL: {bar} {latest_ul}%")
                        except:
                            pass
                
                await asyncio.sleep(1) # Throttling edits to avoid Telegram rate limits
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Progress updater error: {e}")
                break

    prog_task = asyncio.create_task(progress_updater())

    quality_formats = {
        'best': 'bestvideo+bestaudio/best',
        '1080': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        '720': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        '480': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
        '360': 'bestvideo[height<=360]+bestaudio/best[height<=360]',
    }

    try:
        def download_video(requested_quality):
            temp_dir = tempfile.gettempdir()
            output = Path(temp_dir) / f"vd_{chat_id}_{msg_id}.%(ext)s"
            
            cookies_file = Path("cookies.txt")
            
            opts = {
                'format': requested_quality,
                'outtmpl': str(output),
                'quiet': True,
                'no_warnings': True,
                'merge_output_format': 'mp4',
                'concurrent_fragment_downloads': 16,
                'retries': 20,
                'fragment_retries': 20,
                'socket_timeout': 60,
                'source_address': '0.0.0.0', # Force IPv4 to prevent connection reset issues on Windows
                'geo_bypass': True,
                'buffersize': 131072,
                'extractor_args': {
                    'youtube': {'player_client': ['ios', 'android', 'web']},
                    'general': {'legacy_ssl': [True]}
                },
                'progress_hooks': [dl_progress],
                'nocheckcertificate': True,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Sec-Fetch-Mode': 'navigate',
                }
            }
            
            if cookies_file.exists():
                opts['cookiefile'] = str(cookies_file)
                logger.info(f"Using cookies from {cookies_file}")

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            
            files = [str(f) for f in Path(temp_dir).glob(f"vd_{chat_id}_{msg_id}.*") if not str(f).endswith('.part') and not str(f).endswith('.ytdl')]
            return files[0] if files else None

        # Try specific quality first
        primary_format = quality_formats.get(quality, quality_formats['best'])
        try:
            video_path = await loop.run_in_executor(None, download_video, primary_format)
        except Exception as e:
            if "Requested format is not available" in str(e) or quality != 'best':
                logger.warning(f"Quality {quality} failed, falling back to best. Error: {e}")
                # Fallback to absolute best single file or merged
                video_path = await loop.run_in_executor(None, download_video, 'best')
            else:
                raise e

        if not video_path or not Path(video_path).exists():
            await status_msg.edit("Download failed. Possible reasons: protected content, region lock, or unsupported site.")
            return

        await status_msg.edit("Uploading...")

        await client.send_file(
            event.chat_id,
            str(video_path),
            caption="Downloaded via bot",
            supports_streaming=True,
            reply_to=int(msg_id) if msg_id.isdigit() else button_msg.reply_to_msg_id,
            progress_callback=ul_progress
        )

        await status_msg.delete()

    except Exception as e:
        error_text = str(e)
        logger.error(f"Error: {error_text}")
        
        display_error = "Error: Internal download error."
        if "Sign in to confirm you're not a bot" in error_text:
            display_error = "Error: YouTube blocked this server's IP. Please upload 'cookies.txt' to bypass this."
        elif "empty media response" in error_text or "Instagram" in error_text:
            display_error = "Error: Instagram/Site blocked the request. Please upload 'cookies.txt' to authenticate."
        elif "ffmpeg" in error_text.lower():
            display_error = "Error: Server missing FFmpeg. Please install it to allow video merging."
        elif "403" in error_text:
            display_error = "Error: Forbidden (403). The site might be blocking the server or requires cookies."
        else:
            # Show a snippet of the actual error to help debugging
            display_error = f"Error: {error_text[:100]}..."
        
        try:
            await status_msg.edit(display_error)
        except:
            pass
    finally:
        upload_done[0] = True
        try:
            prog_task.cancel()
        except:
            pass

        try:
            if video_path and Path(video_path).exists():
                os.remove(video_path)
        except:
            pass

        try:
            for f in Path(tempfile.gettempdir()).glob(f"vd_{chat_id}_{msg_id}.*"):
                try:
                    os.remove(f)
                except:
                    pass
        except:
            pass

        url_store.pop(store_key_msg, None)
        url_store.pop(store_key_btn, None)

if __name__ == "__main__":
    check_dependencies()
    # Start Render health check server
    threading.Thread(target=run_health_check, daemon=True).start()
    
    print("Initializing bot...")
    client.loop.run_until_complete(main())
