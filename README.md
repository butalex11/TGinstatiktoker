# 📥 Telegram Social Network Video Downloader Bot

A simple Telegram bot that downloads videos from **Instagram**, **TikTok**, **YouTube Shorts** links sent by users and returns them as downloadable files directly in the chat.

---

## 🚀 How It Works

1\. **User Interaction**  
   A user sends a message containing a **link** in any Telegram chat where the bot is present (private or group). The bot **MUST** have administrator privileges to properly monitor messages and respond to link events

2\. **Link Processing**  
   The bot detects the link, validates it, and fetches the video from the respective platform using supported APIs or scraping tools.

3\. **Video Delivery**  
   Once the video is downloaded, the bot sends it back to the chat as a **file attachment**, along with:
   - The **username** of the sender
   - A **clickable link** to the original video

---

## 📦 Features

- Supports **Instagram Reels**, **TikTok videos**, **YouTube Shorts videos**
- Sends videos as **files** (not just links or previews)
- Includes **attribution** (who sent the link and link to original video URL of downloaded media)
- Processes requests sequentially, one at a time
- **Error reporting to admin group** with detailed logs (option)

---

## 🛠 Requirements

- Docker
- Instagram and TikTok cookie file (optional, for private and 18+ videos)

---

## 📌 Instagram and TikTok Cookies

🧩 Instructions for Google Chrome (and Chromium-based browsers like Brave, Edge)

If you're downloading videos from Instagram or TikTok, you may need to authenticate using your cookies. Here's how to export them:  
🔌 Install the Extension

 Go to the Chrome Web Store and install the "Get cookies.txt LOCALLY" extension  
  👉 Direct Link (https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)

🔐 Log in to Instagram or TikTok

 Open a new tab and go to https://www.instagram.com or https://www.tiktok.com
 
 Log in to your account

📤 Export the Cookies

 While on the Instagram or Tiktok website, click the extensions icon (🧩) in your browser's toolbar  
 Find and click on "Get cookies.txt LOCALLY" from the dropdown list  
 A new window will pop up — click the "Export" button

📁 File is Downloaded

 Your browser will download a file, likely named www.instagram.com_cookies.txt or www.tiktok.com_cookies.txt, to your Downloads folder  
 Rename file to cookies1.txt or cookie_tiktok1.txt and put to ```/path/to/instaloader/cookies``` folder

🔄 **Multiple Sessions for Fault Tolerance**

The bot supports multiple Instagram or TikTok sessions for enhanced reliability. Place multiple cookie files in the same folder with names following the pattern (Instagram):
- `cookies1.txt`
- `cookies2.txt`
- `cookies3.txt`
- And so on...

TikTok pattern:
- `cookie_tiktok1.txt`
- `cookie_tiktok2.txt`
- `cookie_tiktok3.txt`
- And so on...

The bot will automatically iterate through these files if one session becomes invalid or hits a rate limit. This ensures greater fault tolerance and availability. Cookies are always used sequentially, even if one of them fails — the bot will continue with the next available session

---

## 🚨 Error Reporting

The bot includes automatic error reporting functionality:

- When a download fails after all retry attempts (or one of the cookie in rotation also), the bot sends a notification to the admin group
- Error reports include:
  - Timestamp and platform information
  - Brief error description
  - Detailed error log as a text file attachment (including yt-dlp logs)
  - User and chat context information

To enable error reporting, set the `ADMIN_GROUP_ID` environment variable with your admin group's ID.

---

## ⚙️ Setup

1\. Clone the repository:
   ```bash
   git clone https://github.com/butalex11/instatiktoker.git
   cd instatiktoker
   ```

2\. Build and run
   ```bash
   docker build -t media-bot .
   docker run -d \
       --name media-bot \
       --restart unless-stopped \
       -v /etc/localtime:/etc/localtime:ro -v /etc/timezone:/etc/timezone:ro \
       -v /path/to/bot_downloads/temp:/app/bot_temp \
       -v /path/to/instaloader/cookies:/app/cookies \
       -e BOT_TOKEN="YOUR_BOT_TOKEN" \
       -e ALLOWED_GROUP_IDS="YOUR_ALLOWED_GROUP_IDS" \
       -e ADMIN_GROUP_ID="YOUR_ADMIN_GROUP_ID" \
       -e BOT_NOTIFICATIONS="yes" \
   media-bot
   ```

---

## 🔧 Environment Variables

When starting the container, you can customize its behavior by using the following environment variables:

| Variable Name            | Description                                                                                         | Example Value          | Default value|
|---------------------------|-----------------------------------------------------------------------------------------------------|------------------------|-------------------|
| `BOT_TOKEN` (required)                 | Telegram bot token for authentication. Obtainable from [BotFather](https://t.me/botfather).  | `"123456789:ABCDEF-ghijklmnopqrstuvwxyz"` |empty|
| `ALLOWED_GROUP_IDS` (required)         | Comma-separated list of allowed Telegram group IDs where the bot will operate.               | `"-1001234537890,-1001876542210"` |empty|
| `ADMIN_GROUP_ID` (optionally)          | Telegram group ID for admin error notifications. If not set, error reporting is disabled.    | `"-1001224267890"`     |empty|

Feel free to adjust these variables based on your use case.

---

## ⚠️ Disclaimer

This bot was developed with the assistance of artificial intelligence (AI) by a non-programmer. While every effort has been made to ensure the bot's functionality, there may be areas that require further optimization or debugging.

The project utilizes the `yt-dlp` library, which is an open-source tool available on GitHub ([yt-dlp repository](https://github.com/yt-dlp/yt-dlp)). To the best of our knowledge, the use of this library complies with its license. If you believe additional attribution or compliance steps are required, please refer to the library's license and documentation for further details.

---

## ☕️ Buy Me a Coffee

If you find this project helpful and feel like saying thanks, give me a star and you're welcome to send a small tip — even the price of a coffee makes a difference:

  
**USD TON, Toncoin: 🔗 UQCOwl3SXuBelImCEPH6ON4sZAhHJdVI4EDBQ4MtA5FmW6DL**


Thanks for your support — it helps keep the project brewing ☕️✨
