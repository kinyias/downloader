# 🎬 Hướng Dẫn Sử Dụng Short Drama CLI Trên Google Colab

Công cụ chạy **thuần túy bằng dòng lệnh (CLI)** trực tiếp trong terminal của Google Colab hoặc máy tính cá nhân.
- 🚀 **Không cần Web UI / Không cần mở port hay tạo tunnel**.
- 📊 **Thanh tiến trình (Progress Bar)** hiển thị trực quan phần trăm, tốc độ tải và tốc độ ghép video FFmpeg.
- ⚡ **Tăng tốc GPU (NVIDIA T4 / A100)**: Tự động nhận diện GPU để ghép video siêu tốc.
- ☁️ **Lưu trực tiếp vào Google Drive**: `/content/drive/MyDrive/ShortDrama_Downloads`.

---

## ⚡ 1-Click Mở Trên Google Colab

Nhấn vào nút dưới đây để mở Notebook:

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/kinyias/downloader/blob/main/short_drama_colab.ipynb)

---

## 📋 Các Lệnh CLI Chính

### 1. Mở Menu Tương Tác (Dễ dùng nhất)
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
python cli.py merge "/content/drive/MyDrive/ShortDrama_Downloads/Ten_Phim" --output-name "Phim_Hoan_Chinh.mp4"
```
Hiển thị thanh tiến trình FFmpeg (0% -> 100%, tốc độ render e.g. `6.5x`, thời gian còn lại).

---

### 5. Đăng ký thiết bị mới (Device ID)
```bash
python cli.py register
```
Tự động đăng ký và lưu `device_id` & `install_id` vào `config.json`.
