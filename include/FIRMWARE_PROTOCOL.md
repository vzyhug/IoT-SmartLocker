# Hợp đồng MQTT firmware

Prefix: `smartlocker/{site_id}/{locker_id}/`. Wokwi local dùng `smartlocker/site01/locker01/`.

| Topic sau prefix | QoS | Retain | Mục đích |
| --- | --- | --- | --- |
| `auth/request` | 1 | Không | `request_id`, compartment, method RFID/OTP/QR, credential |
| `auth/response` | 1 | Không | Kết quả cùng request ID; allow, compartment, nonce, order, action, TTL |
| `event` | 1 | Không | Event ID, request ID, sequence, loại event và bằng chứng cửa/kiện |
| `event/ack` | 1 | Không | Backend ACK event ID sau commit hoặc nhận diện bản ghi trùng |
| `status` | 1 | Có | Online và Last Will offline |
| `telemetry` | 0 | Không | Cửa, phase, PIR, cảnh báo và khối lượng định kỳ |
| `cmd/config` | 1 | Có | Config version hóa; firmware áp dụng cửa mở tối đa, ACK cả version lặp để outbox kết thúc |
| `ack` | 1 | Không | Firmware ACK config/maintenance command theo `command_id` |
| `cmd/unlock` | 1 | Không | Lệnh bảo trì admin có nonce, thời điểm hết hạn, compartment; firmware chống replay qua NVS |

Trong profile Wokwi, RFID gửi `method=rfid`. Có thể mô phỏng đầu vào OTP/QR thuần phần mềm qua Serial Monitor: gửi `OTP:<6-digit-code>` hoặc `QR:<token>`; firmware chuyển cùng hợp đồng `auth/request` và chờ response tương quan. OTP/QR cấp cho khách chỉ hợp lệ ở trạng thái `waiting_pickup`.

Firmware giữ event trong `/events.jsonl` ở LittleFS tới khi nhận ACK ứng dụng. Retry gửi record đầu; backend khử trùng theo event ID và kiểm tra sequence theo locker. Event counter và sequence được lưu NVS trước khi phát. Queue hiện giới hạn 64 sự kiện; đầy queue thì bật LED đỏ và log lỗi, không xóa ngầm event cũ. Khi ACK xóa event, firmware thay queue qua file tạm + backup và phục hồi backup sau restart nếu tiến trình thay file bị gián đoạn. Mount LittleFS không tự format khi có lỗi để tránh âm thầm xóa event.

Auth response chỉ được chấp nhận khi request ID đang chờ, đúng ngăn, nonce khác nonce gần nhất, order/action hợp lệ, TTL từ 1 đến 5000 ms, phản hồi trong 5 giây và cửa đóng. Hết hạn, credential lỗi hoặc phản hồi không hợp lệ không kích hoạt unlock pulse. Khi được phép, firmware ghi `auth_ok` trước xung khóa.

Lệnh bảo trì do API admin tạo trong SQLite outbox và worker phát QoS 1. Payload `cmd/unlock` gồm `command_id`, nonce ngẫu nhiên, `compartment_id`, `action=maintenance_unlock`, `expires_at` Unix time. Firmware yêu cầu đồng hồ đã đồng bộ, lệnh chưa hết hạn (cửa sổ tối đa 15 giây), nonce mới, đúng ngăn và trạng thái idle/cửa đóng. Nonce được ghi NVS trước khi pulse. Firmware ACK command ID; các event mở/đóng cửa dùng cùng command ID làm request ID để lưu audit mà không tác động trạng thái đơn hàng. Config đi cùng outbox, retain và version; command unlock không retain.

Wokwi hiện cấu hình MQTT plaintext tới `host.wokwi.internal` để dùng broker local khi Private IoT Gateway khả dụng. Public Gateway không thể truy cập localhost. Profile firmware triển khai ngoài Wokwi dùng `WiFiClientSecure`, CA cert, tài khoản MQTT, NTP và TLS; chưa được coi là đã kiểm nghiệm end-to-end nếu chưa chạy broker TLS.

Backend cấp OTP 6 chữ số và QR token ngẫu nhiên cho đơn `waiting_pickup`, hết hạn sau 10 phút. Chỉ hash HMAC có salt được lưu; mã/token chỉ trả một lần từ endpoint admin để chuyển giao trong demo. Credential tiêu thụ khi Backend cho phép mở; request QoS 1 trùng `request_id` trả lại response ban đầu (cùng nonce), còn dùng credential với request ID mới bị từ chối. Sau 5 lần sai credential sẽ bị khóa 60 giây.

Status dùng keepalive 30 giây, clean session false, Last Will offline retained và reconnect backoff tăng từ 1 đến tối đa 60 giây. Telemetry publish QoS 0 mỗi 30 giây.

## Payload event cửa đóng

```json
{
  "event_id": "locker01-evt-12",
  "sequence": 18,
  "request_id": "locker01-2-req-3",
  "locker_id": "locker01",
  "compartment_id": "A01",
  "event": "door_closed",
  "package_uid": "DEADBEEF",
  "weight_before": 0.0,
  "weight_after": 0.2
}
```

## Payload lệnh mở bảo trì

```json
{"command_id":"maint-a1b2c3","nonce":"<random-once>","compartment_id":"A01","expires_at":1794926000,"action":"maintenance_unlock"}
```

ACK thiết bị:

```json
{"command":"unlock","command_id":"maint-a1b2c3","accepted":true}
```

Nếu đơn có package UID, backend yêu cầu UID đọc trong khoang khớp và delta cân tối thiểu 0.05 kg theo chiều giao/nhận. Không khớp hoặc không có bằng chứng sẽ chuyển đơn sang `needs_review` và lưu giá trị đọc được để dashboard đối chiếu.


## Quyền RFID ngoại tuyến (KT08)

Backend đóng gói tối đa 8 quyền đang hiệu lực vào `offline_credentials` trong `cmd/config` retained. Mỗi quyền giới hạn theo locker, compartment A01, UID, action (`deposit`/`pickup`), order và `expires_at` (mặc định 24 giờ). Firmware chỉ nhận config version tăng, ghi cấu hình vào NVS rồi ACK. Khi MQTT mất kết nối, firmware yêu cầu đồng hồ đã đồng bộ và quyền chưa hết hạn; nếu không xác minh được thời gian thì từ chối mở. Trước pulse, permission ID được ghi bền vững vào danh sách `offused` của NVS; reboot không cho dùng lại.

Sự kiện offline dùng `request_id=offline-<permission_id>` và mang `offline_permission_id` cùng `authorized_at` (Unix time tại lúc cấp quyền). Khi kết nối lại, LittleFS gửi `auth_ok` trước các event cửa; Backend đối chiếu thời điểm cấp quyền với khoảng hiệu lực, trạng thái đơn và locker/ngăn, tiêu thụ quyền trong DB, tạo session đúng một lần rồi xử lý `unlock_pulsed`, `door_open`, `door_closed` theo sequence. Kiểm tra theo thời điểm cấp quyền cho phép đối soát an toàn cả khi queue chỉ đồng bộ sau khi TTL đã qua. Sau khi giao xong, Backend tạo quyền pickup cho UID khách và queue config retained mới. Phạm vi offline hiện là RFID; OTP/QR cần backend trực tuyến để kiểm tra hash/TTL.

Giới hạn: quyền mới chỉ xuống thiết bị khi thiết bị online nhận config. Thu hồi trên Backend không thể lập tức thu hồi bản quyền đã nằm trên tủ đang offline; TTL 24 giờ giới hạn cửa sổ này. NVS không mã hóa danh sách quyền; đây là demo Wokwi, không phải tuyên bố an toàn vật lý.
