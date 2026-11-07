# tiktok-dl

**English** · [Tiếng Việt](README.vi.md)

> [!WARNING]
> **This project is for educational purposes only.** Do not use it to re-upload or sell other people's content.

Download every video from a TikTok profile, with full metadata.

- Fast downloads with **aria2c**
- **ffmpeg** adds title, author, date, description, stats, cover and subtitles to each MP4
- Photo posts are saved as an MP4 slideshow **and** as separate images (+ music)
- Saves all info to `videos.csv` and `metadata/*.json`
- Stop anytime, run again to continue

## Install

1. Install [Python 3.10+](https://www.python.org/downloads/)
2. Run:
   ```bash
   pip install -r requirements.txt
   ```
3. Put `aria2c.exe` and `ffmpeg.exe` next to `tiktok-dl.py`
   - aria2c: https://github.com/aria2/aria2/releases
   - ffmpeg: https://www.gyan.dev/ffmpeg/builds/

## Use

```bash
python tiktok-dl.py username
```

| Command | What it does |
|---|---|
| `python tiktok-dl.py username` | Download the whole profile |
| `python tiktok-dl.py username -n 20` | Only the 20 newest posts |
| `python tiktok-dl.py username -o D:\TikTok` | Save to another folder |
| `python tiktok-dl.py username --metadata-only` | Only save info, no videos |
| `python tiktok-dl.py <video link>` | Download one video |
| `python tiktok-dl.py https://vt.tiktok.com/XXXXXXXXX/` | Short links from the TikTok app work too |
| `python tiktok-dl.py username --lang vi` | Show messages in Vietnamese |
| `python tiktok-dl.py username --no-images` | Photo posts: slideshow video only |

**Ways to enter a profile** (all do the same thing):

```bash
python tiktok-dl.py .user.name
python tiktok-dl.py https://www.tiktok.com/@.user.name
python tiktok-dl.py "@.user.name"
```

> [!NOTE]
> In **PowerShell**, `@` has a special meaning. Type the username without `@`, or put it in quotes.

Files are saved in `downloads/<username>/`.

**Photo posts** are saved three ways: an MP4 slideshow with the music, every image as a JPG
(`..._01.jpg`, `..._02.jpg`, ...) and the music as an MP3. Use `--no-images` for the slideshow only,
or `--no-slideshow` for the images only.

Messages are in **English** by default (Vietnamese if your system language is Vietnamese).
Use `--lang en` or `--lang vi` to choose.

Output is colored (green = done, red = failed, yellow = reason / warning) with a live progress bar.
Use `--no-color` (or set `NO_COLOR=1`) for plain text.

Run `python tiktok-dl.py -h` to see all options.

## Failed downloads log

If a post can't be downloaded, the tool tells you:

- On screen, each failed post is shown with its **link** and the **reason**.
- All failed posts are also saved to **`failed.txt`** in the output folder, so you can open each link and check it yourself.

```
https://www.tiktok.com/@username/video/1234567890123456789
    ad / TikTok Shop video: TikTok does not provide a download link on the web (even when logged in)
```

`failed.txt` lists every post of the profile that still fails, across runs, and is removed when
nothing fails. Posts that can never be downloaded (deleted, ad / TikTok Shop) are marked `(skipped)`
and not tried again on the next runs. Add `--retry-all` to try them anyway.

## Get your session ID (optional)

You only need this for **private accounts you follow** or **age-restricted posts**.

1. Open https://www.tiktok.com in **Chrome** or **Edge** and log in.
2. Press **F12** to open DevTools.
3. Go to the **Application** tab → **Storage** → **Cookies** → `https://www.tiktok.com`.
   (Firefox: **Storage** tab → **Cookies**.)
4. Type `sessionid` in the filter box.
5. Double-click the **Value** of the row named exactly `sessionid` and copy it (a long string of letters and numbers).
6. Check that it works:
   ```bash
   python tiktok-dl.py --check-session --session-id PASTE_YOUR_SESSIONID_HERE
   ```
   - `✓ Session OK: logged in as @your_account` → it works.
   - `✗ Session ID is invalid or expired` → copy a new one.
7. Download with it:
   ```bash
   python tiktok-dl.py username --session-id PASTE_YOUR_SESSIONID_HERE
   ```
   The tool checks the session first and stops if it is not valid.

   **Safer:** put it in the `TIKTOK_SESSIONID` environment variable instead, so it does not end up in
   your command history. In PowerShell:
   ```bash
   $env:TIKTOK_SESSIONID = "PASTE_YOUR_SESSIONID_HERE"
   python tiktok-dl.py username
   ```

> [!CAUTION]
> Your `sessionid` works like your password. **Never share it** or post it online.
> If you log out of TikTok, the session ID stops working and you need to copy a new one.
> Using a **secondary account** is safer.

The tool does not keep your session ID: it is only used while the command runs. It is never put on a
command line other programs can see, and the temporary file yt-dlp needs is deleted right away.

## Problems?

- **Can't get the post list:** run `pip install -U yt-dlp`
- **Some posts failed:** see `failed.txt` in the output folder (link + reason for each post), then run the same command again
- **Ad / TikTok Shop videos fail:** TikTok does not give a download link for these on the web, even when you are logged in. They are listed in `failed.txt` so you can open them yourself
- **Private account:** add `--session-id <your sessionid cookie>`
- **"No posts found":** the account is private or empty, or TikTok blocked the list for a while. Check the profile in your browser and try again later
- **"TikTok is limiting requests (HTTP 429/403)":** the tool slows down and retries by itself. If it keeps
  happening, it stops; wait a while and run the same command again (or use a larger `--wait`, fewer `--workers`)
- **"TikTok's bot protection blocked the request (challenge page)":** TikTok is showing a verification page
  instead of the posts. The tool does not try to get past it and stops after a few in a row. Try again later
  or from another network (VPN off, a different connection). `--session-id` does not help here
- **Ctrl+C:** stops right away. Finished posts are kept; run the same command again to continue
- **"(ffmpeg failed, saved without metadata)":** the video was downloaded and saved, only the title, cover and subtitles could not be added. Update ffmpeg if it keeps happening

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

**This is for educational purposes only.** You are responsible for how you use it.
Respect the creators' copyright and TikTok's Terms of Service.

This project is not affiliated with TikTok or ByteDance. The author is not responsible for any misuse.

## License

MIT © loooveee
