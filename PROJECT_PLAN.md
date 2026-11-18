# Kế hoạch hoàn thiện Smart Locker N11

## Phạm vi

Demo sử dụng hoàn toàn thiết bị ảo Wokwi, firmware ESP32 mô phỏng, Mosquitto, backend và dashboard local. Không có thiết bị vật lý trong phòng làm việc và không yêu cầu mua/kết nối thiết bị thật. VLAN vật lý, UPS và kiểm tra điện của tủ thật không thuộc nghiệm thu demo.

## Đã triển khai trong repo

- Firmware một ngăn A01: RFID, trạng thái cửa, xung khóa mô phỏng, LED, LCD, buzzer, PIR, MQ2 và HX711 ảo.
- MQTT: auth/event QoS 1, status retained và LWT, telemetry QoS 0, config retained/versioned, admin command outbox, ACK và reconnect backoff.
- Event queue bền vững LittleFS, event ID/sequence lưu NVS, retry và ACK ứng dụng từ worker sau commit DB.
- Backend: quản lý đơn demo, xác thực shipper/khách, giữ ngăn, event ordering/replay, thiết bị/telemetry và kiểm tra package UID + thay đổi cân.
- Dashboard admin có bảo vệ token; xem đơn, ngăn, thiết bị, telemetry, phiên, cảnh báo và bằng chứng kiện.
- Tài liệu GPIO, wiring diagram, kịch bản Wokwi và ma trận kiểm thử đã cập nhật.
- Admin command/config outbox; MQTT command ACK, config retain/version, lệnh maintenance có TTL/nonce và audit.
- Broker test TLS + ACL dùng Mosquitto tạm với cert test; bài đo 20 lượt local MQTT auth đã chạy.
- Backend worker tự reconnect khi Broker khởi động trễ/restart và phục hồi SQLite outbox sau worker restart.
- Quyền RFID ngoại tuyến được gửi qua config retained, firmware lưu một lần dùng trong NVS, xếp sự kiện vào LittleFS và Backend đối soát theo `authorized_at` sau reconnect; unit test bao phủ chu trình.

## Đã xác minh bằng lệnh

- PlatformIO firmware `wokwi`: build thành công (RAM 14.1%, app 56.2%).
- `python -m pytest backend/test_app.py -q`: 19 tests passed in the latest recorded run (4 deprecation/cache warnings).
- `python backend/smoke_mqtt.py`: pass RFID/OTP pickup, từ chối, replay guard, event ordering, command ACK và application event ACK trên Mosquitto local.
- `python backend/smoke_https.py`: chứng chỉ CA/SAN được xác minh, bảo vệ endpoint bằng admin token, listener HTTPS từ chối HTTP plaintext.
- `python backend/smoke_tls_acl.py`: CA/hostname/auth pass, plaintext bị từ chối trên listener TLS và ACL chặn truy cập chéo locker.
- `python backend/latency_mqtt.py`: 20/20 phản hồi; lần chạy hiện tại p50 4.7 ms, max 10.7 ms trên broker local.
- `python backend/smoke_reconnect.py`: worker phục hồi khi broker khởi động muộn/restart và tiếp tục outbox sau worker restart.

Các kiểm tra này không thay thế lượt nghiệm thu trực tiếp trong Wokwi. Cần lưu log simulator cho luồng cửa, cảm biến, offline queue/reboot và thao tác kiện.

## Còn lại để đóng demo

- [ ] Tạo và đặt `WOKWI_CLI_TOKEN` cục bộ từ Wokwi CI Dashboard để chạy automation scenario; không lưu token vào repo. Sau đó xác minh đường kết nối tới broker mà simulator truy cập được.
- [ ] Hoàn tất luồng Wokwi shipper → khách: đã có log người dùng gửi cho thấy RFID UID, yêu cầu cấp quyền, `unlock_pulsed: published` và `Access granted: deposit`; còn cần lưu bằng chứng cửa đóng làm đơn sang `waiting_pickup`, khách nhận thành công và đơn sang `received`.
- [ ] Thực hiện thao tác kiện ảo bằng RFID tag và HX711, gồm UID sai và cân không đổi.
- [ ] Sau khi Wokwi E2E kết nối được, ngắt broker và restart mô phỏng để xác nhận queue LittleFS retry đúng thứ tự, ACK tầng ứng dụng mới xóa event.
- [ ] Sau khi Wokwi E2E kết nối được, kiểm tra config version hợp lệ/cũ và lệnh replay/expiry/nonce trên firmware simulator.
- [ ] Đo latency end-to-end trong Wokwi; protocol MQTT local đã đo 20 lượt (lần mới nhất p50 4.7 ms, max 10.7 ms), TLS/ACL/HTTPS software smoke đã đạt.
- [x] Chạy software service recovery test cho Broker/worker (KT11).
- [ ] Cập nhật KT01–KT14 với log/ảnh thực tế sau các bài thử còn lại.

## Chức năng chưa có/giới hạn demo

- OTP/QR đã có backend cấp mã/token một lần, TTL 10 phút, hash HMAC có salt, giới hạn 5 lần sai/khóa 60 giây và đầu vào Serial Monitor cho ESP32 ảo. Mã được chuyển giao thủ công trong demo; chưa có SMS/email hoặc quét ảnh QR.
- API admin phát config và `cmd/unlock` bảo trì đã triển khai qua outbox; cần kiểm thử expiry/nonce/replay trên Wokwi để hoàn tất xác minh runtime.
- Firmware Wokwi hiện được cấu hình tới broker local bằng MQTT plaintext; cần Private Gateway để route `host.wokwi.internal`. TLS/ACL đã được smoke-test trên broker phần mềm riêng; kết quả đó không xác nhận TLS end-to-end từ Wokwi.
- Một ESP32 quản lý một ngăn A01; chọn ngăn theo kích thước chưa có đầu vào/kích thước đơn trong demo.
- CN07 dùng UID thẻ kiện và chênh lệch cân ảo làm bằng chứng mô phỏng, không nhận diện ngoại quan hoặc xác minh pháp lý.

## Tiêu chí bàn giao

Người khác dựng được broker/backend/dashboard và Wokwi theo README; luồng giao nhận đạt trạng thái đúng; test phần mềm pass; log/ảnh phân biệt rõ kết quả simulator với unit/smoke. Tất cả thành phần vẫn là mô phỏng và dịch vụ local.
