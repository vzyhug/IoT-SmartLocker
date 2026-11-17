# Kịch bản nghiệm thu Smart Locker mô phỏng hoàn toàn

## Chuẩn bị

1. Build `pio run -e wokwi` (VS Code: PlatformIO Build). Build tạo `firmware.merged.bin` gồm bootloader, bảng phân vùng và ứng dụng; mở/restart Wokwi sau build để simulator nạp ảnh đầy đủ.
2. Chạy Mosquitto local cổng 1883, backend MQTT worker (`MQTT_TLS=0`) và API/dashboard như README hướng dẫn.
3. Bật **Private Wokwi IoT Gateway** trong VS Code (Wokwi: Enable Private Wokwi IoT Gateway); firmware dùng `host.wokwi.internal` để tới broker trên máy này. Public IoT Gateway không truy cập localhost/LAN. [Wokwi ESP32 WiFi Networking](https://docs.wokwi.com/guides/esp32-wifi)
4. Tạo đơn A01: shipper UID `AABBCCDD`, khách `11223344`; nếu kiểm tra CN07, đặt `package_uid=DEADBEEF`.
5. Door switch ở vị trí đóng. Toàn bộ ESP32, đầu đọc RFID, thẻ, công tắc, LCD, còi, PIR, MQ2, LED và HX711 là linh kiện ảo. Không kết nối thiết bị thật.

## Giao hàng và nhận hàng

| Bước | Thao tác Wokwi | Kết quả cần thấy |
| --- | --- | --- |
| 1 | Khởi động với Door đóng | `MQTT connected`; trạng thái online trên retained topic |
| 2 | Quét UID khách trước khi gửi | Bị từ chối, chốt mô phỏng không pulse, event `auth_fail` |
| 3 | Quét UID shipper | Cấp quyền giao; LED xanh, còi ngắn, LCD hướng dẫn mở cửa; `unlock_pulsed` |
| 4 | Gạt Door mở | `door_open`; không phát `door_forced` |
| 5 | (CN07) quét thẻ kiện xanh UID `DEADBEEF`, đặt load HX711 tăng ít nhất 0.05 kg | Serial nhận UID kiện; weight sau lớn hơn weight trước |
| 6 | Gạt Door đóng | `door_closed`; Backend ghi `waiting_pickup`; queue được xóa sau `event/ack` |
| 7 | Quét thẻ khách | Cấp quyền pickup |
| 8 | Mở cửa, (CN07) quét lại kiện và giảm tải cân, đóng cửa | `received` nếu UID đúng và khối lượng giảm ≥0.05 kg; nếu sai chuyển `needs_review` |

## OTP và QR mô phỏng bằng đầu vào Serial

Sau khi một đơn ở `waiting_pickup`, gọi `POST /orders/{order_id}/pickup-credentials` với admin token. API trả OTP 6 số và QR token dùng một lần, hết hạn sau 10 phút. Trong Wokwi Serial Monitor, gửi `OTP:<6 số>` hoặc `QR:<token>`; đây là đầu vào phần mềm thay cho keypad/bộ quét QR. Dùng sai 5 lần để xác nhận credential bị khóa 60 giây; nhập đúng mã ở request mới để mở; gửi lại cùng request ID ở tầng MQTT phải trả cùng response mà không phát xung mở lần nữa.

## Cảm biến và phản hồi

- PIR chuyển HIGH: đèn chiếu sáng ảo bật tối đa 30 giây sau chuyển động.
- MQ2 digital output báo khói/gas: LED đỏ, còi và event `smoke_alarm`; clear sensor để reset trạng thái báo.
- Để test cửa mở lâu, cấp quyền, mở Door và đợi ngưỡng (mặc định 30 giây): một event `door_left_open`, LED đỏ.
- Mở Door khi không có session: `door_forced`, LED đỏ và còi cảnh báo.
- Để test timeout cấp quyền, quét thẻ hợp lệ nhưng không mở Door trong 30 giây: `unlock_timeout`, đơn không đổi trạng thái.

## Giao thức và phục hồi

- Xác nhận backend chỉ ACK event sau khi ghi DB; xem log worker và topic `event/ack` theo event ID.
- Dừng Mosquitto trước khi cửa đổi trạng thái; event phải còn trong queue LittleFS. Khởi động broker lại, xác nhận event retry theo thứ tự và queue giảm sau ACK.
- Restart ESP32 simulator sau khi queue event; kiểm tra event ID/sequence không bị dùng lại và backend không cập nhật trùng.
- Gửi `cmd/config` với version tăng dần; config cũ phải bị bỏ qua, config hợp lệ được lưu NVS và có `ack`.
- Gọi API `POST /devices/locker01/config` với body `{"door_open_limit_ms":45000}`; worker phải phát retained command, firmware ACK đúng ID và áp dụng version mới. Gửi lại config version cũ phải được nhận diện idempotent hoặc từ chối, không làm lùi version.
- Gọi API `POST /devices/locker01/unlock` với admin token; firmware chỉ chấp nhận trong thời hạn, cửa đóng và phase idle. Mở rồi đóng Door để xác nhận audit command ID; gửi lại cùng command/nonce phải bị từ chối.
- Dừng broker hoặc backend từng dịch vụ riêng để ghi hành vi reconnect. Firmware Wokwi dùng MQTT local plaintext; không dùng kết quả này để kết luận TLS/ACL đạt.

## Bằng chứng cần lưu

Lưu serial monitor, timestamp và topic/event IDs, trạng thái đơn trên Dashboard, trạng thái queue trước/sau khi reconnect. Với PCN01, lặp 20 lần xác thực online và ghi từng dòng `PCN01 auth-to-unlock latency`; mọi lượt phải dưới 2000 ms. Đây là thời gian từ lúc firmware gửi yêu cầu đến lúc kích xung mở khóa, bao gồm Wokwi→Broker→Backend→Wokwi. Đánh dấu rõ kết quả unit, MQTT smoke hay Wokwi simulator; không suy diễn kết quả mô phỏng thành kết quả phần cứng thật.


## KT08: offline access and resynchronization

1. Create an A01 order for shipper UID `AABBCCDD`; confirm the API queues a config and the worker publishes retained `offline_credentials` that the device acknowledges.
2. After the simulator receives config and NTP time is valid, stop Mosquitto or disconnect MQTT without resetting the simulated ESP32. Scan the shipper UID; expect `Offline access granted` and an unlock pulse. Open and close the door. An unknown UID and a second scan of the consumed UID must not unlock.
3. Restore the broker and repeat with the broker connected but the backend worker stopped. Scan the previously synchronized shipper UID and wait for the 5-second authorization timeout; firmware should then grant cached offline access. A live explicit deny response must remain denied and must not trigger fallback. Start the worker again and verify queued events reconcile in order.
4. Restart broker/worker. Confirm LittleFS replays `auth_fail` for the timed-out online attempt, followed by offline `auth_ok`, `unlock_pulsed`, `door_open`, and `door_closed` in sequence. Backend should set the order to `waiting_pickup`, consume the shipper grant, and queue a new retained config with the customer grant.
5. Scan the shipper again after reconnect or simulator restart; it must be denied. The customer grant should authorize pickup. If the device never received the initial config, offline access must be denied.
6. With NTP unavailable or time untrusted, firmware must deny offline access rather than skip TTL validation. Save serial logs, config payload, event IDs/sequences, DB status, and ACKs.

Backend reconciliation is covered by unit tests. Wokwi runtime remains **unverified** until simulator evidence is saved.
