# tiktok-dl

[English](README.md) · **Tiếng Việt**

> [!WARNING]
> **Dự án chỉ phục vụ mục đích học tập.** Không dùng để đăng lại hay bán nội dung của người khác.

Tải toàn bộ video của một profile TikTok, kèm đầy đủ thông tin.

- Tải nhanh bằng **aria2c**
- **ffmpeg** gắn tiêu đề, tác giả, ngày đăng, mô tả, lượt xem, ảnh bìa, phụ đề vào từng file MP4
- Bài đăng dạng ảnh được ghép thành video slideshow
- Lưu toàn bộ thông tin vào `videos.csv` và `metadata/*.json`
- Tắt giữa chừng thì chạy lại để tải tiếp

## Cài đặt

1. Cài [Python 3.10 trở lên](https://www.python.org/downloads/)
2. Chạy:
   ```bash
   pip install -r requirements.txt
   ```
3. Đặt `aria2c.exe` và `ffmpeg.exe` cạnh file `tiktok-dl.py`
   - aria2c: https://github.com/aria2/aria2/releases
   - ffmpeg: https://www.gyan.dev/ffmpeg/builds/

## Cách dùng

```bash
python tiktok-dl.py @username
```

| Lệnh | Tác dụng |
|---|---|
| `python tiktok-dl.py @username` | Tải hết profile |
| `python tiktok-dl.py @username -n 20` | Chỉ tải 20 bài mới nhất |
| `python tiktok-dl.py @username -o D:\TikTok` | Lưu vào thư mục khác |
| `python tiktok-dl.py @username --metadata-only` | Chỉ lấy thông tin, không tải video |
| `python tiktok-dl.py <link video>` | Tải một video |
| `python tiktok-dl.py https://vt.tiktok.com/XXXXXXXXX/` | Link rút gọn copy từ app TikTok cũng được |
| `python tiktok-dl.py @username --lang vi` | Hiện thông báo bằng tiếng Việt |

File được lưu trong `downloads/<username>/`.

Thông báo mặc định bằng **tiếng Anh**, máy cài tiếng Việt thì tự chuyển sang tiếng Việt.
Dùng `--lang vi` hoặc `--lang en` để tự chọn.

Chữ có màu (xanh lá = xong, đỏ = lỗi, vàng = lý do / cảnh báo) kèm thanh tiến độ chạy trực tiếp.
Dùng `--no-color` (hoặc đặt biến `NO_COLOR=1`) để in chữ trơn.

Chạy `python tiktok-dl.py -h` để xem tất cả tuỳ chọn.

## Log bài không tải được

Bài nào không tải được, tool sẽ báo cho bạn:

- Trên màn hình, mỗi bài lỗi hiện kèm **link bài đăng** và **lý do**.
- Toàn bộ bài lỗi được lưu vào file **`failed.txt`** trong thư mục tải, để bạn tự mở từng link ra kiểm tra.

```
https://www.tiktok.com/@username/video/7705012484368780552
    video quảng cáo / TikTok Shop: TikTok không cung cấp link tải qua web (kể cả khi đã đăng nhập)
```

`failed.txt` được cập nhật sau mỗi lần chạy và tự xoá khi không còn bài nào lỗi.

## Lấy session ID (không bắt buộc)

Chỉ cần khi tải **tài khoản riêng tư bạn đang theo dõi** hoặc **bài giới hạn độ tuổi**.

1. Mở https://www.tiktok.com bằng **Chrome** hoặc **Edge** và đăng nhập.
2. Bấm **F12** để mở DevTools.
3. Vào tab **Application** → **Storage** → **Cookies** → `https://www.tiktok.com`.
   (Firefox: tab **Storage** → **Cookies**.)
4. Gõ `sessionid` vào ô lọc (Filter).
5. Bấm đúp vào cột **Value** của dòng có tên đúng là `sessionid`, rồi copy (là một chuỗi dài gồm chữ và số).
6. Kiểm tra session có dùng được không:
   ```bash
   python tiktok-dl.py --check-session --session-id DAN_SESSIONID_VAO_DAY --lang vi
   ```
   - `✓ Session hợp lệ: đang đăng nhập là @tai_khoan_cua_ban` → dùng được.
   - `✗ Session ID không hợp lệ hoặc đã hết hạn` → copy lại cái mới.
7. Tải kèm session:
   ```bash
   python tiktok-dl.py @username --session-id DAN_SESSIONID_VAO_DAY
   ```
   Tool sẽ tự kiểm tra session trước, nếu không hợp lệ thì dừng lại.

> [!CAUTION]
> `sessionid` có tác dụng như mật khẩu. **Tuyệt đối không chia sẻ** hay đăng lên mạng.
> Nếu bạn đăng xuất TikTok thì session ID sẽ hết hiệu lực, cần copy lại cái mới.
> Nên dùng **tài khoản phụ** cho an toàn.

## Gặp lỗi?

- **Không lấy được danh sách bài:** chạy `pip install -U yt-dlp`
- **Vài bài bị lỗi:** xem file `failed.txt` trong thư mục tải (có link và lý do từng bài), rồi chạy lại đúng lệnh cũ
- **Video quảng cáo / TikTok Shop bị lỗi:** TikTok không cung cấp link tải cho loại video này qua web, kể cả khi đã đăng nhập. Chúng được liệt kê trong `failed.txt` để bạn tự mở xem
- **Tài khoản riêng tư:** thêm `--session-id <cookie sessionid của bạn>`

## Đóng góp

Mọi báo lỗi và pull request đều được hoan nghênh. Xem [CONTRIBUTING.md](CONTRIBUTING.md) (tiếng Anh).

## Miễn trừ trách nhiệm

**Dự án chỉ phục vụ mục đích học tập.** Bạn tự chịu trách nhiệm khi sử dụng.
Hãy tôn trọng bản quyền của người đăng và Điều khoản dịch vụ của TikTok.

Dự án không liên quan tới TikTok hay ByteDance. Tác giả không chịu trách nhiệm cho bất kỳ hành vi sử dụng sai mục đích nào.

## License

MIT © loooveee
