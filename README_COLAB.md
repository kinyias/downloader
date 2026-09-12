# 🎬 Hướng Dẫn Sử Dụng Short Drama CLI Trên Google Colab

Công cụ chạy **thuần túy bằng dòng lệnh (CLI)** trực tiếp trong terminal của Google Colab hoặc máy tính cá nhân.
- 🚀 **Không cần Web UI / Không cần mở port hay tạo tunnel**.
- 📊 **Thanh tiến trình (Progress Bar)** hiển thị trực quan phần trăm, tốc độ tải và tốc độ ghép video FFmpeg.
- ⚡ **Tăng tốc GPU (NVIDIA T4 / A100)**: Tự động nhận diện GPU để ghép video siêu tốc.
- ☁️ **Tự động tải lên storage.to**: Nhận link tải trực tiếp siêu tốc, lưu file tạm trên ổ SSD `/content/downloads`.

---

## ⚡ 1-Click Mở Trên Google Colab

Nhấn vào nút dưới đây để mở Notebook:

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/kinyias/downloader/blob/main/short_drama_colab.ipynb)

---

## 📋 Cách Sử Dụng

### 🌐 1. Khởi Động Giao Diện Web Trực Quan (Web UI với Link Công Khai)
Chạy lệnh sau để khởi động Web UI đầy đủ tính năng và nhận link HTTPS công khai (Cloudflare Tunnel / Localtunnel):
```bash
python colab_runner.py --tunnel auto --port 5000
```
Hệ thống sẽ tự động in ra đường link công khai (ví dụ: `https://xxx.trycloudflare.com`) để bạn bấm vào mở giao diện Web trên trình duyệt máy tính hoặc điện thoại.

---

### 💻 2. Mở Menu Tương Tác Dòng Lệnh (CLI Interactive Menu)
```bash
python cli.py
```
Hiển thị giao diện menu số trực quan: tìm kiếm phim, nhập ID tải, ghép video, đổi thư mục lưu...

---

### 2. Tải toàn bộ phim & Tự động ghép thành 1 file MP4 hoàn chỉnh
```bash
# Cú pháp: python cli.py download <SERIES_ID_HOAC_LINK> --merge
python cli.py download 7369168922572164134 --merge
```

Các tùy chọn mở rộng khi tải:
- `--merge`: Tự động ghép thành 1 video duy nhất sau khi tải xong.
- `--cut-end 3`: Cắt bỏ 3 giây nhạc kết ở cuối mỗi tập trước khi ghép.
- `--mirror`: Lật hình ngang chống bản quyền.
- `--clean-parts`: Tự động xóa các file tập lẻ sau khi đã ghép thành công file FULL.
- `--save-dir /duong/dan`: Chọn thư mục lưu video.

Ví dụ:
```bash
python cli.py download 7369168922572164134 --merge --cut-end 2 --clean-parts
```

---

### 3. Tìm kiếm phim theo tên hoặc thể loại
```bash
python cli.py search "Tổng tài"
```
Hiển thị bảng danh sách phim kèm Series ID, số tập, thể loại để bạn chọn tải ngay.

---

### 4. Ghép các tập video có sẵn trong một thư mục
```bash
python cli.py merge "/content/downloads/Ten_Phim" --output-name "Phim_Hoan_Chinh.mp4"
```
Hiển thị thanh tiến trình FFmpeg (0% -> 100%, tốc độ render e.g. `6.5x`, thời gian còn lại).

---

### 5. Đăng ký thiết bị mới (Device ID)
```bash
python cli.py register
```
Tự động đăng ký và lưu `device_id` & `install_id` vào `config.json`.
