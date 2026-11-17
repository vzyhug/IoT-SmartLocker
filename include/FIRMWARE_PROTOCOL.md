# Hợp đồng bản tin firmware một ngăn

Prefix: `smartlocker/{site_id}/{locker_id}/`. Firmware hiện cố định `site01/locker01/A01` trong Wokwi; bản bo thật lấy site/locker từ `include/secrets.h`.

| Topic sau prefix | Chiều | Payload chính |
| --- | --- | --- |
| `auth/request` | ESP32 → Backend | `request_id`, `compartment_id`, `method="rfid"`, `credential` (UID hex không dấu `:`) |
| `auth/response` | Backend → ESP32 | `request_id`, `allow`, `compartment_id`; nếu cho phép: `nonce`, `order_id`, `action` (`deposit`/`pickup`), `ttl_ms` |
| `event` | ESP32 → Backend | `event_id`, `request_id`, `locker_id`, `compartment_id`, `event` |

Firmware chỉ nhận phản hồi cho `request_id` đang chờ trong tối đa 5 giây, đúng ngăn `A01`, `ttl_ms` > 0 và ≤ 5000, `nonce` khác nonce vừa dùng, `order_id` không rỗng và `action` hợp lệ. Firmware không mở khi cửa đang mở hoặc khi phản hồi bị từ chối. `request_id` gồm `locker_id`, số lần boot lưu NVS và bộ đếm trong boot. `event_id` dùng bộ đếm lưu NVS trước khi phát để không lặp sau reset thông thường.

Các `event` hiện có: `unlock_pulsed`, `door_open`, `door_closed`, `door_forced`, `door_left_open`. `door_forced` có `request_id` rỗng. Backend chỉ được hoàn tất đơn sau cặp `door_open` → `door_closed` cùng phiên; `unlock_pulsed` không chứng minh chốt thực sự đã nhả.

**Giới hạn giao thức hiện tại:** PubSubClient gửi các sự kiện bằng QoS 0; không có hàng đợi offline hoặc ACK sau khi DB ghi. Nếu mất Broker đúng lúc cửa chuyển trạng thái, sự kiện có thể mất. Người 2 và 3 cần thống nhất thêm `event/ack` (ACK chứa `event_id` sau khi DB commit), chính sách retry và xử lý phiên timeout trước khi đánh dấu CN06 đạt. Không bật quyền mở offline cho tới khi thiết kế quyền một lần, hạn dùng và dấu đã sử dụng qua reset được kiểm thử.
