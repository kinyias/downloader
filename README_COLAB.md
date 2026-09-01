# 🚀 Hướng Dẫn Chạy Trên Google Colab

Dự án này đã được tối ưu hóa hoàn toàn để chạy trên **Google Colab** miễn phí, hỗ trợ GPU tăng tốc ghép video và lưu trực tiếp vào **Google Drive**.

---

## ⚡ Cách 1: Mở nhanh bằng 1 Click (Khuyến khích)

Nhấn vào huy hiệu dưới đây để mở trực tiếp Notebook trên Google Colab:

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/kinyias/downloader/blob/main/short_drama_colab.ipynb)

---

## 📋 Các Bước Thực Hiện Trên Google Colab

### Bước 1: Chọn môi trường GPU (Tùy chọn nhưng nên dùng)
1. Trên giao diện Google Colab, vào menu **Runtime** (Thời gian chạy) -> **Change runtime type** (Thay đổi loại thời gian chạy).
2. Chọn **T4 GPU** rồi nhấn **Save** (Lưu).
*(GPU giúp tăng tốc độ ghép video lên gấp 5-10 lần so với CPU).*

### Bước 2: Chạy ô Kiểm tra GPU & Kết nối Google Drive
- Cho phép Colab kết nối với Google Drive của bạn. Video tải về và video ghép sẽ được lưu vĩnh viễn vào thư mục:
  `/content/drive/MyDrive/ShortDrama_Downloads`

### Bước 3: Cài đặt thư viện
- Chạy ô cài đặt: hệ thống sẽ tự động clone repo, cài đặt `ffmpeg`, `pycryptodome`, `flask`, `requests`...

### Bước 4: Khởi động Web Server & Cloudflare Tunnel
- Chạy ô khởi động:
  ```bash
  !python colab_runner.py --tunnel cloudflare
  ```
- Terminal sẽ in ra một đường dẫn công khai:
  `👉 https://xxxx-xxxx-xxxx.trycloudflare.com`
- **Click vào link này để mở giao diện Web UI** trên máy tính hoặc điện thoại của bạn!

---

## 💻 Cách 2: Tải Nhanh Bằng Dòng Lệnh Trực Tiếp (CLI Mode)

Nếu không cần mở giao diện Web, bạn có thể tải toàn bộ các tập của một bộ phim trực tiếp bằng lệnh:

```python
# Thay thế 7369168922572164134 bằng series_id bạn muốn tải
!python colab_runner.py --cli 7369168922572164134
```

Toàn bộ các tập sẽ được tải với thanh tiến trình và lưu ngay vào Google Drive của bạn!

---

## ⚙️ Các Tùy Chọn Khi Chạy `colab_runner.py`

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--tunnel` | `cloudflare` | Cổng kết nối công khai: `cloudflare` (miễn phí, không cần đăng ký), `ngrok`, `colab`, `none` |
| `--port` | `5000` | Cổng chạy Flask |
| `--save-dir` | Auto Google Drive | Đường dẫn thư mục lưu video tải về |
| `--ngrok-token` | `""` | Token nếu chọn `--tunnel ngrok` |
| `--cli` | `""` | Nhập `series_id` để tải thẳng không cần bật web server |
