# Tích hợp giao thức và kết quả kiểm thử

## Phạm vi demo

Toàn bộ tủ được mô phỏng bằng ESP32 và linh kiện ảo trong Wokwi. Mosquitto, backend, SQLite và dashboard chạy bằng phần mềm trên máy phát triển. Không sử dụng hay yêu cầu thiết bị vật lý. Hạ tầng TLS/ACL có thể chạy trên broker phần mềm; VLAN, UPS và đặc tính điện của tủ thật không phải tiêu chí của demo này.

## Hợp đồng MQTT hiện thực

Prefix demo: `smartlocker/site01/locker01/`.

| Topic | QoS/retain | Trạng thái triển khai |
| --- | --- | --- |
| `auth/request`, `auth/response` | 1 / không retain | RFID shipper/khách và OTP/QR pickup; response khớp `request_id`, nonce, TTL; response idempotent khi QoS 1 giao lặp |
| `event` | 1 / không retain | Firmware lưu JSONL ở LittleFS, giữ nguyên event ID khi retry |
| `event/ack` | 1 / không retain | Worker ACK sau khi DB commit hoặc xác nhận event trùng đã tồn tại; ACK đúng `event_id` xóa bản ghi queue |
| `status` | 1 / retain | Online sau kết nối; Last Will offline retained; keepalive 30s, persistent session và backoff 1–60s |
| `telemetry` | 0 / không retain | Firmware gửi trạng thái cửa, pha hoạt động, PIR, cảnh báo và cân định kỳ 30s |
| `cmd/config` | 1 / retain | API admin queue config; worker phát retain, firmware kiểm tra version, lưu NVS và ACK |
| `ack` | 1 / không retain | Firmware ACK `command_id`; worker kết thúc outbox command |
| `cmd/unlock` | 1 / không retain | API admin tạo command TTL + nonce; firmware chống replay, khóa cửa sai trạng thái và audit door cycle |

Backend khử trùng event ID và kiểm tra sequence tăng dần theo tủ. Chu trình đơn chỉ đổi trạng thái sau `door_open` rồi `door_closed` cùng session. Đơn có `package_uid` yêu cầu UID kiện khớp và thay đổi khối lượng đúng chiều tối thiểu 50 g; bằng chứng được ghi vào SQLite và dashboard. Mô phỏng kiện dùng thẻ RFID ảo bổ sung và HX711 ảo; ngưỡng/cân cần được hiệu chuẩn theo mô hình Wokwi.

## Kết quả đã chạy

| Bài thử | Kết quả | Bằng chứng |
| --- | --- | --- |
| Firmware ESP32 môi trường `wokwi` | Đạt build | PlatformIO tạo full flash image gồm bootloader, partition table và app; RAM 14.1%, app dùng 56.2% vùng app ở build mới nhất |
| Sơ đồ Wokwi | Đạt lint | `wokwi-cli lint`: không còn warning; hai thông tin về định danh board ESP32/MFRC522 là mức info từ linter |
| Backend nghiệp vụ và dashboard | Đạt | `python -m pytest backend/test_app.py -q`: 19 passed trong lần chạy gần nhất (4 deprecation/cache warnings) |
| MQTT end-to-end qua Mosquitto local | Đạt | `python backend/smoke_mqtt.py`: config/maintenance outbox and ACK, RFID shipper/customer, OTP pickup, replay/giữ ngăn, sai thứ tự và application event ACK |
| HTTPS API local | Đạt phần mềm | `python backend/smoke_https.py`: chứng chỉ CA/SAN tạm được xác minh, admin authentication hoạt động, HTTP plaintext bị từ chối ở cổng TLS |
| Bằng chứng kiện UID + cân | Đạt ở unit test | Kiểm tra đúng UID và delta khối lượng; chưa chạy phần cảm biến trong UI Wokwi |
| RFID online và xung mở khóa trong Wokwi | Đạt một phần theo Serial Monitor người dùng cung cấp | Đã quan sát `RFID UID`, `Authorization requested`, `Event unlock_pulsed: published` và `Access granted: deposit, WOKWI-001`; firmware hiện in `PCN01 auth-to-unlock latency` để thu 20 lượt simulator; chưa có log/dashboard chứng minh chu trình cửa đổi đơn sang `waiting_pickup`, khách pickup và đơn sang `received` |
| Mất mạng, reboot và phục hồi LittleFS | Chưa xác nhận trên simulator | Firmware đã có queue/retry/ACK và phân vùng LittleFS; chưa xác minh runtime mất mạng/restart vì Wokwi E2E hiện chưa thông suốt |
| Outbox config/unlock command | Đạt phần mềm | Unit test API/admin/nonce payload/maintenance event; MQTT smoke xác nhận config publish và device ACK đóng command |
| OTP/QR pickup | Đạt logic backend; chưa chạy Wokwi E2E | Credential sau deposit, HMAC salted, TTL 10 phút, one-time, khóa sau 5 lần sai; firmware nhận `OTP:`/`QR:` qua Serial Monitor |
| Broker/backend worker restart | Đạt phần mềm | `python backend/smoke_reconnect.py`: worker lên trước broker, reconnect sau broker restart và resume SQLite outbox sau worker restart |
| Config version, TTL/nonce firmware | Chưa xác nhận end-to-end | Firmware build đạt; cần chạy config replay, expiry và unlock nonce trên Wokwi |
| Mosquitto TLS và ACL | Đạt theo phạm vi broker phần mềm | `python backend/smoke_tls_acl.py` dùng CA/server cert tạm, xác thực hostname/user; ACL cho topic đúng và chặn chéo locker |
| Latency 20 lượt PCN01 | Đạt một phần ở local protocol | `python backend/latency_mqtt.py`: lần chạy mới nhất 20/20 MQTT auth responses, p50=4.7 ms, max=10.7 ms; chưa gồm Wokwi firmware và xung actuator |

## Ma trận KT theo tài liệu tổng hợp

| ID | Kết quả hiện tại | Ghi chú |
| --- | --- | --- |
| KT01 | Đạt một phần trên Wokwi | Serial Monitor do người dùng cung cấp chứng minh RFID online được cấp quyền deposit và firmware phát `unlock_pulsed`; chưa có log đủ chu kỳ cửa/backend order `waiting_pickup` → khách nhận → `received` |
| KT02 | Đạt logic backend | Unit test RFID sai quyền, OTP/QR sai, one-time, expiry/attempt lock; thao tác OTP/QR runtime trên Wokwi chưa xác nhận |
| KT03 | Đạt một phần | 20/20 lượt local broker/backend dưới 2 giây; chưa đo end-to-end ESP32 nhận credential tới pulse như mốc KT03 |
| KT04 | Đạt một phần trong demo local | MQTT TLS/CA/hostname/auth, ACL và HTTPS API smoke pass; Wokwi firmware hiện cấu hình local broker plaintext 1883; chưa có kết nối TLS end-to-end từ firmware Wokwi |
| KT05 | Không áp dụng hạ tầng vật lý | Nếu cần minh họa, dùng network namespace/firewall phần mềm riêng |
| KT06 | Đạt ở smoke broker | Client locker01 publish/subscribe đúng topic; publish và delivery của locker02 bị ACL chặn trên Mosquitto TLS tạm |
| KT07 | Đạt một phần | Backend chặn request/event/maintenance command replay; firmware kiểm nonce/TTL; chưa chạy thử phát lại lệnh/response trong Wokwi |
| KT08 | Đạt backend/unit; Wokwi runtime chưa xác nhận | Quyền RFID offline được đồng bộ qua config retained, đánh dấu đã dùng bền vững trong NVS, event được xếp hàng LittleFS và backend đối soát khi reconnect. Firmware cũng thử quyền offline sau timeout nếu broker còn kết nối nhưng backend không phản hồi; phản hồi từ chối rõ ràng vẫn bị từ chối. Unit test xác nhận chu trình và chống phát lại; cần chạy thao tác mất broker/backend và reboot trên simulator để có bằng chứng firmware runtime. |
| KT09 | Đạt một phần | Logic `door_forced`, `door_left_open`, buzzer/LED có; cần xác nhận toàn tuyến bằng Wokwi |
| KT10 | Không áp dụng phần cứng | UPS vật lý không thuộc demo; kiểm tra reboot mô phỏng thuộc KT12 |
| KT11 | Đạt ở software services | Reconnect khi broker vắng/restart và resume outbox sau worker restart đã pass; API process độc lập với worker |
| KT12 | Đạt một phần | NVS event counter/sequence và chống event trùng; cần xác nhận restart ESP32 trong Wokwi |
| KT13 | Đạt logic ở unit test | Kiểm tra không đổi cân bị chuyển needs_review; chưa hoàn tất thao tác HX711 trên giao diện Wokwi |
| KT14 | Đạt logic ở unit test | UID đúng/sai và chiều cân giao/nhận được đối chiếu; chưa chạy thao tác thẻ kiện ảo đúng/sai trên Wokwi |

PCN03 đã được cài đặt bằng đèn hiện diện ảo tự tắt sau 30 giây và LCD backlight tự tắt sau 60 giây; hành vi runtime trong Wokwi chưa được xác nhận. Wokwi CLI 0.29.1 đã được cài cục bộ; `wokwi-cli lint` không còn warning (còn hai thông tin về định danh board ESP32 và MFRC522). Chưa chạy automation simulator bằng CLI vì máy hiện không có `WOKWI_CLI_TOKEN`; CI token tạo riêng tại [Wokwi CI Dashboard](https://wokwi.com/dashboard/ci), khác license của extension VS Code. Runtime Wokwi qua extension đã có bằng chứng một phần do người dùng cung cấp (RFID online authorization và xung mở khóa); các luồng còn lại vẫn cần log simulator.


## Lệnh kiểm tra

```powershell
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run -e wokwi
python -m pytest backend/test_app.py -q
python backend/smoke_mqtt.py
python backend/smoke_tls_acl.py
python backend/smoke_https.py
python backend/latency_mqtt.py
python backend/smoke_reconnect.py
```

Chạy từng smoke lần lượt, không chạy đồng thời trên cùng topic/broker. Smoke MQTT và latency cần Mosquitto đang chạy trên `127.0.0.1:1883`. TLS/ACL và reconnect smoke tự tạo broker tạm; cần `mosquitto`, `mosquitto_passwd`, `openssl` (cho TLS) và paho-mqtt; không sửa broker Wokwi. Chạy firmware qua PlatformIO/Wokwi theo `wokwi.toml`; không cần thiết bị vật lý.
