# Smart Locker N11 — bản demo một ngăn

**Phạm vi:** demo này dùng 100% linh kiện mô phỏng trong Wokwi và phần mềm chạy local; không dùng thiết bị vật lý. LED vàng mô phỏng xung khóa, switch mô phỏng cảm biến cửa, MFRC522 là đầu đọc ảo. Mọi hướng dẫn về khóa/nguồn/UPS thật chỉ thuộc hướng phát triển sau demo.

Luồng demo: thẻ RFID của shipper → Backend xác thực đơn/ngăn → ESP32 phát xung khóa mô phỏng → công tắc cửa → đơn chuyển sang chờ nhận. Thẻ khách thực hiện tương tự để chuyển đơn sang đã nhận. CN07 dùng thẻ RFID kiện và HX711 ảo để đối chiếu UID cùng thay đổi khối lượng; đây là bằng chứng mô phỏng, không nhận dạng bằng hình ảnh.

## Cấu trúc

- `src/main.cpp`: firmware ESP32 cho một ngăn `A01`.
- `backend/app.py`: API tạo/xem đơn và logic nghiệp vụ SQLite.
- `backend/mqtt_worker.py`: cầu nối MQTT giữa ESP32 và Backend.
- `Tong_hop_project_IoT_SmartLocker_N11.md`: yêu cầu và thiết kế tổng hợp.

## Chuẩn bị

1. Trong Wokwi dùng ESP32 DevKit C V4, MFRC522, switch Door và LED thay khóa; xem sơ đồ linh kiện ảo trong `diagram.json` và [include/HARDWARE.md](include/HARDWARE.md).
2. Cài Python dependencies: `python -m pip install -r backend/requirements.txt`.
3. Cài PlatformIO. Với Wokwi, dùng profile `wokwi` (Wokwi-GUEST và broker local plaintext); không cần `secrets.h`. File này chỉ dùng cho profile triển khai TLS ngoài Wokwi.
4. Chạy Mosquitto local theo `broker/mosquitto.local.conf.example`. TLS/ACL được kiểm tra riêng bằng `python backend/smoke_tls_acl.py`, dùng broker và chứng chỉ tạm; Wokwi vẫn kết nối broker loopback plaintext.

Sơ đồ chân đang dùng trong firmware (chỉ áp dụng cho ESP32 DevKit V1):

| Thiết bị | GPIO ESP32 | Ghi chú |
| --- | --- | --- |
| MFRC522 SCK/MISO/MOSI/SS/RST | 18/19/23/5/27 | Cấp nguồn và mức logic 3,3 V |
| Reed switch | 32 | Đóng cửa kéo xuống GND; mở cửa đọc HIGH |
| LED khóa mô phỏng, LED xanh/đỏ | 26/25/33 | GPIO26 HIGH 500 ms; chỉ là tín hiệu LED ảo |
| LCD, buzzer | I2C 21/22, GPIO4 | Hướng dẫn và âm báo ảo |
| PIR, MQ2 digital | GPIO34/35 | Hiện diện và cảnh báo cảm biến ảo |
| HX711 DT/SCK, đèn hiện diện | GPIO16/17/14 | Bằng chứng khối lượng và chiếu sáng mô phỏng |

Đây là sơ đồ linh kiện ảo của Wokwi. Không lắp khóa, nguồn hay cảm biến vật lý.

## Chạy Backend

Trong PowerShell, đặt biến môi trường `LOCKER_ADMIN_TOKEN`, `LOCKER_OTP_PEPPER`, `MQTT_USER`, `MQTT_PASSWORD`, `MQTT_CA_FILE`, `MQTT_HOST` theo môi trường của nhóm. `LOCKER_OTP_PEPPER` phải là bí mật ổn định; nếu bỏ trống demo dùng `LOCKER_ADMIN_TOKEN`. Chạy hai terminal từ thư mục gốc:

```powershell
python -m uvicorn app:api --app-dir backend --host 127.0.0.1 --port 8000
python backend/mqtt_worker.py
```

Tạo một đơn qua `POST /orders` với header `Authorization: Bearer <LOCKER_ADMIN_TOKEN>` và JSON ví dụ:

```json
{"id":"ORDER-001","locker_id":"locker01","compartment_id":"A01","shipper_uid":"AABBCCDD","customer_uid":"11223344"}
```

UID là chuỗi hex của thẻ RFID ảo mà firmware đọc trong Wokwi. Xem trạng thái bằng `GET /orders` cùng header. Không dùng cùng một thẻ cho cả shipper và khách trong bản demo.

Sau khi shipper đã gửi kiện và đơn chuyển sang `waiting_pickup`, phát hành OTP/QR thử nghiệm bằng API admin:

```powershell
$headers = @{ Authorization = "Bearer $env:LOCKER_ADMIN_TOKEN" }
$pickup = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/orders/ORDER-001/pickup-credentials `
  -Headers $headers
$pickup.otp       # Mã OTP 6 số, hết hạn sau 10 phút
$pickup.qr_payload # Payload để đưa vào QR ở ứng dụng hoàn chỉnh
```

Trong Wokwi Serial Monitor, gửi `OTP:<mã>` hoặc `QR:<token>` (token nằm trong `$pickup.qr_token`). Credential được lưu dạng HMAC có salt, dùng một lần; sau 5 lần sai sẽ khóa credential 60 giây. Serial Monitor là đầu vào ảo cho demo, không phải keypad/camera. API trả credential đúng một lần cho admin để demo; chưa tích hợp SMS/email hay trình quét ảnh QR.

Admin có thể queue config hoặc lệnh mở bảo trì khi tủ online. Config retained/versioned:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/devices/locker01/config `
  -Headers @{ Authorization = "Bearer $env:LOCKER_ADMIN_TOKEN" } `
  -ContentType application/json -Body '{"door_open_limit_ms":45000}'
```

Lệnh bảo trì có nonce/expiry và chỉ nên dùng trong mô phỏng:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/devices/locker01/unlock `
  -Headers @{ Authorization = "Bearer $env:LOCKER_ADMIN_TOKEN" }
```

Xem trạng thái command/audit trong Dashboard.

## Chạy mô phỏng Wokwi trên VS Code

Mô phỏng dùng `Wokwi-GUEST`, Broker Mosquitto local qua `host.wokwi.internal:1883` và không dùng TLS. Đường tới localhost/LAN cần Private IoT Gateway; Public Gateway không truy cập được mạng local. Serial Monitor đã được người dùng xác nhận nhận RFID và nhận quyền deposit từ Backend. Cấu hình TLS/8883 trong repo kiểm tra riêng broker phần mềm, chưa phải TLS end-to-end từ Wokwi. Cần có extension PlatformIO IDE và Wokwi for VS Code.


Để chạy automation Wokwi bằng CLI, cần cài Wokwi CLI và tạo CI token riêng trong Wokwi CI Dashboard theo [hướng dẫn CLI chính thức](https://docs.wokwi.com/wokwi-ci/cli-usage). License của extension VS Code không thay thế CI token. Chỉ đặt `WOKWI_CLI_TOKEN` trong shell cục bộ; không commit hoặc gửi token qua chat. CLI lint sơ đồ đạt, nhưng phiên làm việc này chưa có CI token để chạy cloud simulation scenario. Theo [tài liệu Wi-Fi của Wokwi](https://docs.wokwi.com/guides/esp32-wifi), Public Gateway không thể truy cập Mosquitto local; cần Private Gateway hoặc broker mà simulator truy cập được để chạy MQTT end-to-end.

1. Đảm bảo Mosquitto trên máy đang chạy và chấp nhận kết nối localhost:1883. Giữ Broker này cho mô phỏng; không đưa cổng 1883 không xác thực ra Internet.
2. Trong PowerShell ở thư mục project, chạy `python -m pip install -r backend/requirements.txt`. Đặt `$env:MQTT_TLS='0'`, `$env:MQTT_HOST='127.0.0.1'`, `$env:MQTT_PORT='1883'`, rồi chạy `python backend/mqtt_worker.py` trong terminal thứ nhất.
3. Đặt `$env:LOCKER_ADMIN_TOKEN` thành một giá trị chỉ dùng cục bộ rồi chạy `python -m uvicorn app:api --app-dir backend --host 127.0.0.1 --port 8000` trong terminal thứ hai.
4. Tạo đơn qua `POST /orders` theo ví dụ trên; dùng `shipper_uid='AABBCCDD'` (thẻ đỏ) và `customer_uid='11223344'` (thẻ xanh lá) của Wokwi. UID trên Wokwi hiển thị có dấu `:`, còn Backend lưu chuỗi hex không dấu.
5. Build firmware: `pio run -e wokwi` (hoặc dùng đường dẫn PlatformIO trên máy). Build tự ghép bootloader, partition table có LittleFS và ứng dụng vào `.pio/build/wokwi/firmware.merged.bin`; `wokwi.toml` trỏ tới ảnh đầy đủ này. Mở Command Palette của VS Code và chọn **Wokwi: Start Simulator**. `diagram.json` chứa sơ đồ linh kiện ảo.
6. Quét thẻ đỏ bằng bảng điều khiển MFRC522. Sau khi LED vàng (xung khóa) và LED xanh bật, chuyển công tắc **Door** sang mở rồi về đóng. `GET /orders` phải chuyển sang `waiting_pickup`. Lặp lại với thẻ xanh lá; trạng thái phải thành `received`.

### Dashboard quản trị

Khi API đang chạy, mở `http://127.0.0.1:8000/dashboard` và nhập giá trị `LOCKER_ADMIN_TOKEN` đã cấu hình. Dashboard tự làm mới mỗi 10 giây, hiển thị đơn/ngăn, thiết bị và telemetry, phiên, sự kiện, cảnh báo và bằng chứng kiện ảo. API dữ liệu `/dashboard/data` và API quản trị đơn đều yêu cầu header `Authorization: Bearer <LOCKER_ADMIN_TOKEN>`; không đưa token thật lên máy dùng chung.

Nếu ESP32 mô phỏng báo lỗi kết nối MQTT trên Serial Monitor, kiểm tra Wokwi đang dùng Private IoT Gateway, Broker còn lắng nghe 1883 và `host.wokwi.internal` phân giải được. Queue sự kiện được giữ ở LittleFS đến khi nhận application ACK; xem bước kiểm tra reconnect trong [include/WOKWI_TEST.md](include/WOKWI_TEST.md).

Trên máy Windows đang dùng, PlatformIO nằm tại `C:\Users\duyhu\.platformio\penv\Scripts\pio.exe`; có thể chạy `& 'C:\Users\duyhu\.platformio\penv\Scripts\pio.exe' run -e wokwi`. Kiểm tra riêng Broker/Backend bằng `python backend/smoke_mqtt.py` trước khi mở Wokwi.

Xem [PROJECT_PLAN.md](PROJECT_PLAN.md) để biết phần đã có và các việc còn phải hoàn thiện; [TEAM_WORK.md](TEAM_WORK.md) ghi phân công ba người và ba nhánh.

Người tích hợp xem [docs/INTEGRATION_QA.md](docs/INTEGRATION_QA.md) để biết hợp đồng MQTT, broker mẫu, lệnh chạy smoke test và bảng kết quả KT01–KT14.

Các lệnh kiểm tra phần mềm (chạy lần lượt trên một broker local):

```powershell
python -m pytest backend/test_app.py -q
python backend/smoke_mqtt.py
python backend/smoke_tls_acl.py
python backend/smoke_https.py
python backend/latency_mqtt.py
python backend/smoke_reconnect.py
```

## Giới hạn hiện tại

- OTP/QR có backend phát mã/token pickup một lần và đầu vào Serial Monitor trong Wokwi; chuyển mã cho khách còn thủ công, chưa có SMS/email hoặc quét ảnh QR. API admin phát config và lệnh `cmd/unlock` bảo trì đã có; cần kiểm tra expiry/nonce/replay runtime trên Wokwi.
- Queue offline/ACK đã triển khai nhưng cần ngắt kết nối và restart trong Wokwi để xác nhận end-to-end.
- RFID UID có thể bị sao chép; phù hợp trình diễn, chưa phù hợp vận hành thực tế.
- Wokwi end-to-end sau bổ sung cảm biến ảo, latency 20 lượt, TLS/ACL, reconnect/restart và KT13–14 trên simulator vẫn cần được chạy/lưu bằng chứng; không có phần cứng vật lý trong phạm vi demo.
