# Smart Locker N11 — bản demo một ngăn

Luồng hiện có: thẻ RFID của shipper → Backend xác thực đơn/ngăn → ESP32 cấp xung mở khóa → Reed switch ghi cửa mở/đóng → đơn chuyển sang chờ nhận. Thẻ của khách thực hiện tương tự để chuyển đơn sang đã nhận. **Trạng thái đơn hiện được suy ra từ cửa, chưa xác minh có vật hoặc đúng kiện hàng.** Phần CN07 trong tài liệu là thiết kế mở rộng, chưa được hiện thực bằng cảm biến/camera.

## Cấu trúc

- `src/main.cpp`: firmware ESP32 cho một ngăn `A01`.
- `backend/app.py`: API tạo/xem đơn và logic nghiệp vụ SQLite.
- `backend/mqtt_worker.py`: cầu nối MQTT giữa ESP32 và Backend.
- `Tong_hop_project_IoT_SmartLocker_N11.md`: yêu cầu và thiết kế tổng hợp.

## Chuẩn bị

1. Dùng ESP32 DevKit V1, MFRC522, Reed switch, khóa 12 V qua mạch công suất có diode bảo vệ. **Kiểm tra chân trong `src/main.cpp` với sơ đồ đấu dây và mức logic thực tế trước khi cấp điện.** Không nối khóa 12 V trực tiếp vào ESP32.
2. Cài Python dependencies: `python -m pip install -r backend/requirements.txt`.
3. Cài PlatformIO, sau đó sao chép `include/secrets.example.h` thành `include/secrets.h` và điền Wi-Fi, MQTT Broker, CA, tài khoản thiết bị. File này đã được gitignore.
4. Cấu hình MQTT Broker lắng nghe TLS trên 8883 bằng `broker/mosquitto.conf.example` và `broker/acl.example`. Thay các đường dẫn chứng chỉ, tạo password file và dùng tài khoản riêng cho tủ và Backend. Không cho truy cập ẩn danh.

Sơ đồ chân đang dùng trong firmware (chỉ áp dụng cho ESP32 DevKit V1):

| Thiết bị | GPIO ESP32 | Ghi chú |
| --- | --- | --- |
| MFRC522 SCK/MISO/MOSI/SS/RST | 18/19/23/5/27 | Cấp nguồn và mức logic 3,3 V |
| Reed switch | 32 | Đóng cửa kéo xuống GND; mở cửa đọc HIGH |
| Mạch điều khiển khóa | 26 | Xung HIGH 500 ms; khóa dùng nguồn 12 V riêng qua mạch công suất |
| LED xanh/đỏ | 25/33 | Cần điện trở hạn dòng |

Đây là sơ đồ giả định cho bản demo, phải đối chiếu chân của bo và module thực tế. Kiểm tra chiều tác động của relay/MOSFET và trạng thái khóa khi khởi động trước khi gắn vào cửa tủ.

## Chạy Backend

Trong PowerShell, đặt biến môi trường `LOCKER_ADMIN_TOKEN`, `MQTT_USER`, `MQTT_PASSWORD`, `MQTT_CA_FILE`, `MQTT_HOST` theo môi trường của nhóm. Chạy hai terminal từ thư mục gốc:

```powershell
python -m uvicorn app:api --app-dir backend --host 127.0.0.1 --port 8000
python backend/mqtt_worker.py
```

Tạo một đơn qua `POST /orders` với header `Authorization: Bearer <LOCKER_ADMIN_TOKEN>` và JSON ví dụ:

```json
{"id":"ORDER-001","locker_id":"locker01","compartment_id":"A01","shipper_uid":"AABBCCDD","customer_uid":"11223344"}
```

UID phải là chuỗi hex mà firmware in/đọc từ thẻ thực tế. Xem trạng thái bằng `GET /orders` cùng header. Không dùng cùng một thẻ cho cả shipper và khách trong bản demo.

## Chạy mô phỏng Wokwi trên VS Code

Mô phỏng dùng `Wokwi-GUEST`, Broker Mosquitto trên máy qua `host.wokwi.internal:1883` và không dùng TLS. Chế độ này **chỉ dành cho mô phỏng cục bộ**; firmware cho tủ thật vẫn dùng TLS/8883. Wokwi for VS Code cần Private IoT Gateway của extension để đi tới Broker localhost. Cần có extension PlatformIO IDE và Wokwi for VS Code.

1. Đảm bảo Mosquitto trên máy đang chạy và chấp nhận kết nối localhost:1883. Giữ Broker này cho mô phỏng; không đưa cổng 1883 không xác thực ra Internet.
2. Trong PowerShell ở thư mục project, chạy `python -m pip install -r backend/requirements.txt`. Đặt `$env:MQTT_TLS='0'`, `$env:MQTT_HOST='127.0.0.1'`, `$env:MQTT_PORT='1883'`, rồi chạy `python backend/mqtt_worker.py` trong terminal thứ nhất.
3. Đặt `$env:LOCKER_ADMIN_TOKEN` thành một giá trị chỉ dùng cục bộ rồi chạy `python -m uvicorn app:api --app-dir backend --host 127.0.0.1 --port 8000` trong terminal thứ hai.
4. Tạo đơn qua `POST /orders` theo ví dụ trên; dùng `shipper_uid='AABBCCDD'` (thẻ đỏ) và `customer_uid='11223344'` (thẻ xanh lá) của Wokwi. UID trên Wokwi hiển thị có dấu `:`, còn Backend lưu chuỗi hex không dấu.
5. Build firmware: `pio run -e wokwi` (hoặc dùng đường dẫn PlatformIO trên máy). Mở Command Palette của VS Code và chọn **Wokwi: Start Simulator**. `wokwi.toml` trỏ tới firmware vừa build; `diagram.json` chứa sơ đồ đấu nối.
6. Quét thẻ đỏ bằng bảng điều khiển MFRC522. Sau khi LED vàng (xung khóa) và LED xanh bật, chuyển công tắc **Door** sang mở rồi về đóng. `GET /orders` phải chuyển sang `waiting_pickup`. Lặp lại với thẻ xanh lá; trạng thái phải thành `received`.

Nếu ESP32 báo lỗi kết nối MQTT trên Serial Monitor, kiểm tra Wokwi đang dùng Private IoT Gateway, Broker còn lắng nghe 1883 và `host.wokwi.internal` phân giải được. Mô phỏng không kiểm chứng khóa 12 V, cảm biến hàng hay TLS của bản triển khai thật.

Trên máy Windows đang dùng, PlatformIO nằm tại `C:\Users\duyhu\.platformio\penv\Scripts\pio.exe`; có thể chạy `& 'C:\Users\duyhu\.platformio\penv\Scripts\pio.exe' run -e wokwi`. Kiểm tra riêng Broker/Backend bằng `python backend/smoke_mqtt.py` trước khi mở Wokwi.

Xem [PROJECT_PLAN.md](PROJECT_PLAN.md) để biết phần đã có và các việc còn phải hoàn thiện.

## Giới hạn hiện tại

- Firmware chưa có hàng đợi sự kiện offline, xác nhận ghi DB ở tầng ứng dụng, OTP/QR, load cell hoặc camera. Khi mất MQTT, sự kiện có thể mất; chưa đạt CN06/PCN04. Cảnh báo cửa mở quá lâu mới phát sự kiện và LED tại tủ, chưa có giao diện cảnh báo.
- RFID UID có thể bị sao chép; phù hợp trình diễn, chưa phù hợp vận hành thực tế.
- Chưa kiểm thử trên bo và khóa thật. Mục tiêu độ trễ dưới 2 giây, TLS/ACL và các bài KT trong tài liệu cần đo/kiểm chứng tại môi trường triển khai.
